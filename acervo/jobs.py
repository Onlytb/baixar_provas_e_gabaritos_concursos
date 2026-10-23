from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from .db import connect, discover_document, export_csv, now, records, root_dir, upsert_record
from .files import download_origin
from .http import AccessRequired, Cancelled, Client, HumanActionRequired, ProcessingFailure, SourceFailure
from .model import Filters, norm, role_matches
from .preference import visible_origin_links
from .sources import PCI, QCONCURSOS_PROVAS, pagination, pci_categories, pci_detail, pci_rows, qconcursos_facets, qconcursos_rows, qconcursos_search_url


def interrupted(root=None):
    """Only at service boot, before accepting new jobs."""
    with connect(root) as c:
        c.execute("UPDATE execucoes SET estado='interrompida',mensagem='Serviço reiniciado; execute novamente para continuar aproveitando o acervo.',atualizado_em=? WHERE estado IN ('iniciando','executando')", (now(),))


def create_job(action, params, root=None, launch=True):
    root = Path(root or root_dir()).resolve()
    try:
        with connect(root) as c:
            job = c.execute("INSERT INTO execucoes(acao,parametros,estado,criado_em,atualizado_em) VALUES(?,?,'iniciando',?,?)", (action, json.dumps(params, ensure_ascii=False), now(), now())).lastrowid
    except sqlite3.IntegrityError as e:
        raise ValueError("Já existe uma coleta ativa. Aguarde ou cancele antes de iniciar outra.") from e
    if launch:
        log_dir = root / "logs"
        log_dir.mkdir(exist_ok=True)
        try:
            with (log_dir / f"execucao-{job}.log").open("ab") as log:
                options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
                process = subprocess.Popen([sys.executable, "-m", "acervo.worker", str(job)], cwd=Path(__file__).resolve().parents[1], env={**os.environ, "ACERVO_DIR": str(root)}, stdout=log, stderr=subprocess.STDOUT, **options)
            with connect(root) as c:
                c.execute("UPDATE execucoes SET pid=? WHERE id=?", (process.pid, job))
        except Exception as e:
            with connect(root) as c:
                c.execute("UPDATE execucoes SET estado='erro',mensagem=? WHERE id=?", (str(e), job))
            raise
    return job


def cancel_job(job, root=None):
    with connect(root) as c:
        c.execute("UPDATE execucoes SET cancelar=1 WHERE id=? AND estado IN ('iniciando','executando')", (job,))


def recent_jobs(root=None):
    with connect(root) as c:
        jobs = [dict(r) for r in c.execute("SELECT * FROM execucoes ORDER BY id DESC LIMIT 10")]
        for j in jobs:
            if j["estado"] in ("iniciando", "executando") and j["pid"]:
                try:
                    os.kill(j["pid"], 0)
                except ProcessLookupError:
                    c.execute("UPDATE execucoes SET estado='interrompida',mensagem='Processo encerrado. Execute novamente para continuar.' WHERE id=? AND estado IN ('iniciando','executando')", (j["id"],))
                    j["estado"] = "interrompida"
                except PermissionError:
                    pass
    return jobs


class Runner:
    def __init__(self, job, root=None):
        self.job = job
        self.root = Path(root or root_dir())
        self.last_check = 0
        self.client = Client(check=self.check)
        with connect(self.root) as c:
            row = c.execute("SELECT * FROM execucoes WHERE id=?", (job,)).fetchone()
            self.params = json.loads(row["parametros"])
            self.action = row["acao"]
        self.filters = Filters(**self.params.get("filtros", {}))

    def check(self):
        if time.monotonic() - self.last_check < .2:
            return
        self.last_check = time.monotonic()
        with connect(self.root) as c:
            row = c.execute("SELECT cancelar FROM execucoes WHERE id=?", (self.job,)).fetchone()
            if row and row[0]:
                raise Cancelled("Cancelado pelo usuário")

    def status(self, stage=None, filename=None, total=None, reset=False, outcome=None):
        fields = {"atualizado_em": now()}
        if stage is not None:
            fields["etapa"] = stage
        if filename is not None:
            fields["arquivo_atual"] = filename
        if reset:
            fields.update(total=total, processados=0)
        with connect(self.root) as c:
            c.execute("UPDATE execucoes SET " + ",".join(f"{key}=?" for key in fields) + " WHERE id=?", (*fields.values(), self.job))
            if outcome:
                if outcome not in {"novos", "reaproveitados", "bloqueados", "falhas_download", "pagina"}:
                    raise ValueError("Resultado de progresso desconhecido")
                counter = "" if outcome == "pagina" else f",{outcome}={outcome}+1"
                c.execute("UPDATE execucoes SET processados=processados+1" + counter + " WHERE id=?", (self.job,))

    def log(self, message, error=False, pending=False):
        with connect(self.root) as c:
            c.execute("INSERT INTO eventos(execucao_id,momento,nivel,mensagem) VALUES(?,?,?,?)", (self.job, now(), "pendência" if pending else "erro" if error else "info", message))
            c.execute("UPDATE execucoes SET atualizado_em=?,mensagem=?,erros=erros+? WHERE id=?", (now(), message, int(error), self.job))
        print(message, flush=True)

    def page(self, url):
        self.check()
        self.status(filename=url)
        return self.client.html(url)

    def categories(self):
        categories = pci_categories(self.page(PCI + "/provas/"))
        if not categories:
            raise ValueError("Índice de cargos PCI não reconhecido")
        with connect(self.root) as c:
            c.executemany("INSERT INTO categorias VALUES('PCI',?,?) ON CONFLICT(fonte,url) DO UPDATE SET nome=excluded.nome", [(label, url) for url, label in categories.items()])
        self.log(f"Índice atualizado: {len(categories)} cargos PCI")
        self.status(outcome="pagina")
        return categories

    def qconcursos_facets(self, soup=None):
        soup = soup or self.page(QCONCURSOS_PROVAS)
        facets = qconcursos_facets(soup)
        if not facets:
            raise ValueError("Filtros do Qconcursos não reconhecidos; o layout pode ter mudado.")
        with connect(self.root) as c:
            c.execute("DELETE FROM facetas_qconcursos")
            c.executemany("INSERT INTO facetas_qconcursos(campo,nome,valor) VALUES(?,?,?)", [(f["campo"], f["nome"], f["id"]) for f in facets])
        self.log(f"Filtros Qconcursos atualizados: {len(facets)} opções")
        self.status(outcome="pagina")
        return facets

    def details_pci(self, row, rec):
        try:
            state, docs = pci_detail(self.page(row["url"]), row["url"])
            with connect(self.root) as c:
                for doc in docs:
                    discover_document(c, [rec], doc["url"], doc["tipo"], doc["nome"])
                c.execute("UPDATE registros SET estado=?,erro='',atualizado_em=? WHERE id=?", (state, now(), rec))
            self.log(f"{row['cargo']} | {row['orgao']}: {state} ({len(docs)} documentos)")
        except Cancelled:
            raise
        except Exception as e:
            state = "acesso restrito" if isinstance(e, AccessRequired) else "erro de leitura"
            with connect(self.root) as c:
                c.execute("UPDATE registros SET estado=?,erro=? WHERE id=?", (state, str(e), rec))
            self.log(row["url"] + ": " + str(e), error=True)

    def scan_pci(self):
        try:
            cats = self.categories()
        except Cancelled:
            raise
        except Exception as e:
            self.log("Índice de cargos indisponível: " + str(e), error=True)
            with connect(self.root) as c:
                cats = {r["url"]: r["nome"] for r in c.execute("SELECT * FROM categorias WHERE fonte='PCI'")}
            if not cats:
                cats = {PCI + "/provas/analista-administrativo": "Analista Administrativo", PCI + "/provas/administrador": "Administrador"}
                self.log("Usando somente categorias conhecidas; cobertura parcial.", error=True)
        selected = {url: label for url, label in cats.items() if not self.filters.cargos or any(role_matches(term, label) for term in self.filters.cargos)}
        if not selected:
            self.log("Nenhuma categoria PCI corresponde aos cargos escolhidos.")
        seen_records = set()
        for index, category in selected.items():
            pending = deque([index])
            visited = set()
            while pending:
                url = pending.popleft()
                if url in visited:
                    continue
                visited.add(url)
                try:
                    soup = self.page(url)
                    rows = pci_rows(soup, category)
                    pending.extend(n for n in pagination(soup, index) if n not in visited)
                    count = 0
                    for row in rows:
                        if not self.filters.matches(row) or row["url"] in seen_records:
                            continue
                        seen_records.add(row["url"])
                        with connect(self.root) as c:
                            rec = upsert_record(c, **row)
                        self.details_pci(row, rec)
                        count += 1
                    self.log(f"{category}: página catalogada, {count} correspondências | {url}")
                    self.status(outcome="pagina")
                except Cancelled:
                    raise
                except Exception as e:
                    self.log(url + ": " + str(e), error=True)

    def scan_qconcursos(self):
        with connect(self.root) as c:
            facets = [dict(r) for r in c.execute("SELECT campo,nome,valor id FROM facetas_qconcursos")]
        try:
            facets = self.qconcursos_facets()
        except Cancelled:
            raise
        except Exception as e:
            self.log("Não foi possível atualizar os filtros do Qconcursos; usando opções já catalogadas: " + str(e), error=True)
        pending = deque([qconcursos_search_url(self.filters, facets)])
        facet_labels = {}
        for facet in facets:
            facet_labels.setdefault(facet["campo"], set()).add(norm(facet["nome"]))
        visited_pages = set()
        seen_rows = set()
        while pending:
            url = pending.popleft()
            if url in visited_pages:
                continue
            visited_pages.add(url)
            try:
                soup = self.page(url)
                rows = qconcursos_rows(soup, url)
                next_pages = pagination(soup, url)
                pending.extend(next_url for next_url in next_pages if next_url not in visited_pages)
                matched = 0
                for row in rows:
                    row["areas_atuacao"] = "; ".join(v for v in self.filters.areas_atuacao if norm(v) in facet_labels.get("by_acting_area", set()))
                    row["areas_formacao"] = "; ".join(v for v in self.filters.areas_formacao if norm(v) in facet_labels.get("by_formation_area", set()))
                    if not row["escolaridade"]:
                        row["escolaridade"] = "; ".join(v for v in self.filters.escolaridades if norm(v) in facet_labels.get("by_scholarity", set()))
                    if not self.filters.matches(row) or row["url"] in seen_rows:
                        continue
                    seen_rows.add(row["url"])
                    docs = row.pop("documentos")
                    with connect(self.root) as c:
                        rec = upsert_record(c, **row)
                        for doc in docs:
                            discover_document(c, [rec], doc["url"], doc["tipo"], doc["nome"])
                        c.execute("UPDATE registros SET estado=?,erro='',atualizado_em=? WHERE id=?", ("catalogado" if docs else "sem arquivos", now(), rec))
                    matched += 1
                self.log(f"Qconcursos: {matched} provas correspondentes | {url}")
                self.status(outcome="pagina")
            except Cancelled:
                raise
            except Exception as e:
                self.log(url + ": " + str(e), error=True)

    def download(self):
        self.status("Atualizando documentos nas fontes", "", reset=True)
        ids = self.params.get("registros")
        if ids is None:
            ids = [r["id"] for r in records(self.root, self.filters)]
        if not ids:
            self.status("Preparando downloads", "", total=0, reset=True)
            self.log("Nenhum registro selecionado.")
            return
        selected_records = [r for r in records(self.root) if r["id"] in ids]
        for row in selected_records:
            if row["id"] in ids and row["fonte"] == "PCI":
                self.details_pci(row, row["id"])
        retry_ids = set(self.params.get("documentos", []))
        retry_only = bool(self.params.get("somente_pendentes"))
        with connect(self.root) as c:
            origins = list({o["id"]: o for o in visible_origin_links(c, ids)
                            if not o["url"].startswith("importacao:")
                            and (not retry_ids or o["documento_id"] in retry_ids)
                            and (not retry_only or o["estado"] != "baixado")
                            and (not self.filters.tipos or o["tipo"] in self.filters.tipos)}.values())
        grouped_origins = {}
        for origin in origins:
            grouped_origins.setdefault(origin["documento_id"], []).append(origin)
        self.status("Baixando e indexando PDFs", "", total=len(grouped_origins), reset=True)
        for alternatives in grouped_origins.values():
            self.check()
            failures = []
            succeeded = None
            for origin in alternatives:
                self.status(filename=origin["nome"])
                try:
                    _, outcome = download_origin(self.client, origin, self.root, report=True)
                    succeeded = (origin, outcome)
                    break
                except Cancelled:
                    raise
                except Exception as e:
                    if isinstance(e, HumanActionRequired):
                        state, code = "intervenção necessária", "requer_intervencao"
                    elif isinstance(e, SourceFailure):
                        state, code = e.origin_state, e.code
                    elif isinstance(e, AccessRequired):
                        state, code = "falha de processamento", "falha_processamento"
                    elif isinstance(e, (OSError, ValueError)):
                        state, code = "falha de processamento", "falha_processamento"
                    else:
                        state, code = "falha temporária", "falha_temporaria"
                    with connect(self.root) as c:
                        status_code = getattr(e, "status_code", None)
                        c.execute("UPDATE origens_documento SET estado=?,erro=?,motivo_codigo=?,http_status=?,tentado_em=? WHERE id=?", (state, str(e), code, status_code, now(), origin["id"]))
                    failures.append((origin, e, state))
                    self.log(origin["url"] + ": " + str(e))
            if succeeded:
                origin, outcome = succeeded
                self.status(outcome=outcome)
                self.log(("PDF reaproveitado: " if outcome == "reaproveitados" else "PDF salvo: ") + origin["nome"])
            elif failures:
                needs_user = any(state == "intervenção necessária" for _, _, state in failures)
                self.status(outcome="bloqueados" if needs_user else "falhas_download")
                self.log("Nenhuma origem pública concluiu o download de " + alternatives[0]["nome"], error=not needs_user, pending=needs_user)

    def run(self):
        state = "concluída"
        with connect(self.root) as c:
            c.execute("UPDATE execucoes SET estado='executando',pid=?,atualizado_em=? WHERE id=?", (os.getpid(), now(), self.job))
        try:
            self.check()
            if self.action == "categorias":
                self.status("Atualizando índice de cargos", "")
                self.categories()
            elif self.action == "facetas_qconcursos":
                self.status("Atualizando filtros do Qconcursos", "")
                self.qconcursos_facets()
            elif self.action == "download":
                self.download()
            elif self.action == "catalogar":
                for source in self.filters.fontes or ["PCI", "QCONCURSOS"]:
                    self.check()
                    self.status("Catalogando " + source, "")
                    if source == "PCI":
                        self.scan_pci()
                    elif source == "QCONCURSOS":
                        self.scan_qconcursos()
                if self.params.get("download"):
                    self.download()
            else:
                raise ValueError("Ação desconhecida")
            self.check()
            with connect(self.root) as c:
                errors = c.execute("SELECT erros+bloqueados FROM execucoes WHERE id=?", (self.job,)).fetchone()[0]
            if errors:
                state = "parcial"
        except Cancelled:
            state = "cancelada"
        except Exception as e:
            state = "erro"
            self.log(str(e), error=True)
        finally:
            try:
                temporary = self.root / "catalogo.csv.part"
                temporary.write_bytes(export_csv(self.root))
                temporary.replace(self.root / "catalogo.csv")
            except Exception as e:
                self.log("Não foi possível atualizar catalogo.csv: " + str(e), error=True)
                if state == "concluída":
                    state = "parcial"
            with connect(self.root) as c:
                progress = dict(c.execute("SELECT * FROM execucoes WHERE id=?", (self.job,)).fetchone())
                summary = (f"Execução {state}. {progress['novos']} PDFs novos, "
                           f"{progress['reaproveitados']} reaproveitados, {progress['bloqueados']} pendentes de importação manual "
                           f"e {progress['falhas_download']} falhas de download.") if progress["total"] is not None else "Execução " + state + ". Arquivos já salvos foram preservados."
                other_errors = progress["erros"] - progress["falhas_download"]
                if other_errors:
                    summary += f" {other_errors} falhas de consulta/exportação; veja os eventos."
                c.execute("UPDATE execucoes SET estado=?,etapa=?,arquivo_atual='',atualizado_em=?,mensagem=? WHERE id=?", (state, "Execução " + state, now(), summary, self.job))
            self.client.session.close()
        return state
