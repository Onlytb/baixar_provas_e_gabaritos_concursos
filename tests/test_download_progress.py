import sqlite3
from pathlib import Path

import pytest

from test_acervo import pdf, record, root, soup
from acervo.db import catalog_groups, connect, discover_document, documents, export_catalog_csv, export_csv, initialize, records
from acervo.files import download_origin, import_pdf, safe_path, store_pdf
from acervo.http import AccessRequired, Cancelled, Client, HumanActionRequired, SourceUnavailable, TemporaryFailure
from acervo.jobs import Runner, create_job, recent_jobs
from acervo.model import Filters
from acervo.preference import exam_scope, visible_origin_links
from acervo.sources import fafipa_detail, kind_for


PARANAGUA = "https://fafipa.selecao.net.br/concurso/3201-prefeitura-do-municipio-de-paranagua-pr-semedi-quadro-geral"
ANNEX = "https://anexos.cdn.selecao.net.br/uploads/281/concursos/3201/anexos/"
HTML = '''<h1>Prefeitura do Município de Paranaguá - PR - SEMEDI - QUADRO GERAL</h1>
<p>Edital nº 01/2018</p><button data-aba="cargos">Cargos e Vagas</button>
<div data-aba="cargos" hidden><div class="det-row"><span>Auxiliar Administrativo</span><b>8 vagas</b></div>
<div class="det-row"><span>Auxiliar de Serviços Gerais</span><b>25 vagas</b></div>
<div class="det-row"><span>Motorista</span><b>20 vagas</b></div></div>
<div data-aba="documentos" style="display:none">
<a href="%s3328.pdf">Anexo II - Edital nº 10.01-2018 - Gabarito Definitivo e os Pareceres dos Recursos Deferidos</a>
<a href="%s3258.pdf">Anexo Único do Edital nº 07.01-2018 - Divulgação do Gabarito Preliminar</a>
<a href="%s3257.pdf">Edital nº 07.01-2018 - Divulgação do Gabarito Preliminar, Caderno de Questões e Prazo para Recursos</a>
</div>''' % (ANNEX, ANNEX, ANNEX)


def origin(root, recs, url, kind, name):
    with connect(root) as c:
        return discover_document(c, recs, url, kind, name)


def job_row(root, job):
    with connect(root) as c:
        return dict(c.execute("SELECT * FROM execucoes WHERE id=?", (job,)).fetchone())


def test_paranagua_hidden_tabs_and_annex_classification():
    rows, docs = fafipa_detail(soup(HTML), PARANAGUA)
    assert [r["cargo"] for r in rows] == ["Auxiliar Administrativo", "Auxiliar de Serviços Gerais", "Motorista"]
    assert {Path(d["url"]).name: d["tipo"] for d in docs} == {
        "3328.pdf": "gabarito definitivo", "3258.pdf": "gabarito preliminar", "3257.pdf": "comunicado"}
    assert kind_for("ANEXO – DIVULGAÇÃO DO GABARITO OBJETIVO PRELIMINAR") == "gabarito preliminar"
    assert kind_for("Edital - Divulgação do caderno de questões") == "comunicado"
    assert rows[0]["edital"] == "01/2018"


def test_preference_before_filters_search_csv_and_preserves_file(root):
    rec = record(root)
    pre = import_pdf(pdf("Palavraexclusivadopreliminar"), [rec], "gabarito preliminar", "Gabarito preliminar", root)
    path = safe_path(documents(rec, root)[0]["caminho"], root)
    assert documents(rec, root, query="Palavraexclusivadopreliminar")
    final = origin(root, [rec], ANNEX + "3328.pdf", "gabarito definitivo", "Gabarito definitivo")
    assert [d["id"] for d in documents(rec, root)] == [final]
    assert documents(root=root, query="Palavraexclusivadopreliminar") == []
    assert records(root, Filters(tipos=["gabarito preliminar"])) == []
    assert records(root)[0]["gabarito_situacao"] == "identificado"
    assert "gabarito preliminar" not in export_csv(root).decode("utf-8")
    assert path.is_file()
    with connect(root) as c:
        assert c.execute("SELECT id FROM documentos WHERE id=?", (pre,)).fetchone()


def test_shared_preliminary_remains_visible_only_for_uncovered_role(root):
    a = record(root)
    b = record(root, cargo="Motorista")
    pre = origin(root, [a, b], ANNEX + "3258.pdf", "gabarito preliminar", "Gabarito preliminar")
    final = origin(root, [a], ANNEX + "3328.pdf", "gabarito definitivo", "Gabarito definitivo - Analista Administrativo")
    assert [d["id"] for d in documents(a, root)] == [final]
    assert [d["id"] for d in documents(b, root)] == [pre]
    shared = next(d for d in documents(root=root) if d["id"] == pre)
    assert shared["registros_visiveis"] == [b]
    with connect(root) as c:
        assert not any(o["documento_id"] == pre for o in visible_origin_links(c, [a]))


@pytest.mark.parametrize("pre,final,hidden", [
    ("1ª etapa", "2ª etapa", False), ("Tipo A", "Tipo B", False),
    ("Manhã", "Tarde", False), ("", "Tipo A", False),
    ("Prova objetiva", "Prova discursiva", False), ("", "Reaplicação", False),
    ("Prova A", "Prova B", False), ("Nível médio", "Nível superior", False),
    ("Prova aplicada em 01/02/2026", "Prova aplicada em 02/02/2026", False),
    ("1ª fase - Tipo A", "Fase 1 - Tipo A", True),
    ("12/03/2018", "05/04/2018", True),
])
def test_exam_scope_is_conservative(root, pre, final, hidden):
    rec = record(root)
    pid = origin(root, [rec], ANNEX + "p.pdf", "gabarito preliminar", "Gabarito preliminar " + pre)
    origin(root, [rec], ANNEX + "f.pdf", "gabarito definitivo", "Gabarito definitivo " + final)
    assert (pid not in {d["id"] for d in documents(rec, root)}) == hidden


def test_other_contest_cannot_replace_preliminary(root):
    a = record(root)
    b = record(root, url=PARANAGUA)
    pre = origin(root, [a], ANNEX + "p.pdf", "gabarito preliminar", "Gabarito preliminar")
    origin(root, [b], ANNEX + "f.pdf", "gabarito definitivo", "Gabarito definitivo")
    assert documents(a, root)[0]["id"] == pre


def test_v1_upgrade_backs_up_and_is_idempotent(root):
    rec = record(root, fonte="PCI")
    data = pdf("Respostas")
    name = "Anexo Único - Divulgação do Gabarito Preliminar"
    did = store_pdf([data], [rec], ANNEX + "3258.pdf", "comunicado", name, root)
    job = create_job("download", {"registros": [rec]}, root, launch=False)
    # Reconstruct the real v1 execution table to exercise the migration.
    with connect(root) as c:
        for field in ["etapa", "arquivo_atual", "total", "novos", "reaproveitados", "bloqueados", "falhas_download"]:
            c.execute(f"ALTER TABLE execucoes DROP COLUMN {field}")
        c.execute("PRAGMA user_version=1")
    initialize(root)
    initialize(root)
    backups = list((root / "backups").glob("antes-migracao-v4-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT tipo FROM origens_documento").fetchone()[0] == "comunicado"
    doc = documents(rec, root)[0]
    assert doc["id"] == did and doc["tipos"] == "comunicado"
    assert safe_path(doc["caminho"], root).read_bytes() == data
    assert job_row(root, job)["estado"] == "iniciando"
    assert job_row(root, job)["total"] is None


def test_rediscovery_updates_metadata_without_losing_links(root):
    a = record(root)
    b = record(root, cargo="Motorista")
    did = origin(root, [a], ANNEX + "p.pdf", "comunicado", "Antigo")
    assert origin(root, [b], ANNEX + "p.pdf", "gabarito preliminar", "Novo") == did
    assert documents(a, root)[0]["tipos"] == "gabarito preliminar"
    assert documents(b, root)[0]["nome"] == "Novo"


def test_qconcursos_download_uses_cataloged_origins_and_skips_preliminary(root, monkeypatch):
    qurl = "https://www.qconcursos.com/questoes-de-concursos/provas/fafipa-2025-exemplo"
    rec = record(root, fonte="QCONCURSOS", url=qurl)
    origin(root, [rec], "https://arquivos.qconcursos.com/prova/arquivo_gabarito/1/preliminar.pdf", "gabarito preliminar", "Gabarito preliminar")
    origin(root, [rec], "https://arquivos.qconcursos.com/prova/arquivo_gabarito/1/definitivo.pdf", "gabarito definitivo", "Gabarito definitivo")
    job = create_job("download", {"registros": [rec]}, root, launch=False)
    runner = Runner(job, root)
    pages, downloads = [], []
    def page(url):
        pages.append(url)
        return soup(HTML)
    def download(client, doc, root, *, report):
        downloads.append(doc["url"])
        return doc["documento_id"], "novos"
    monkeypatch.setattr(runner, "page", page)
    monkeypatch.setattr("acervo.jobs.download_origin", download)
    assert runner.run() == "concluída"
    assert pages == []
    assert downloads == ["https://arquivos.qconcursos.com/prova/arquivo_gabarito/1/definitivo.pdf"]
    row = job_row(root, job)
    assert row["total"] == row["processados"] == row["novos"] == 1


def test_preliminary_type_filter_cannot_download_superseded_origin(root, monkeypatch):
    rec = record(root, fonte="Manual")
    origin(root, [rec], ANNEX + "p.pdf", "gabarito preliminar", "Gabarito preliminar")
    origin(root, [rec], ANNEX + "f.pdf", "gabarito definitivo", "Gabarito definitivo")
    job = create_job("download", {"registros": [rec], "filtros": {"tipos": ["gabarito preliminar"]}}, root, launch=False)
    def download(*args, **kwargs):
        pytest.fail("Preliminar substituído não deve ser baixado")
    monkeypatch.setattr("acervo.jobs.download_origin", download)
    assert Runner(job, root).run() == "concluída"
    assert job_row(root, job)["total"] == 0


def test_log_does_not_count_items_and_status_survives_new_runner(root):
    job = create_job("download", {}, root, launch=False)
    runner = Runner(job, root)
    runner.status("Baixando", "a.pdf", total=4, reset=True)
    runner.log("Iniciando download")
    runner.log("Aguardando servidor")
    assert job_row(root, job)["processados"] == 0
    runner.status(outcome="novos")
    Runner(job, root)
    row = recent_jobs(root)[0]
    assert (row["etapa"], row["arquivo_atual"], row["total"], row["processados"]) == ("Baixando", "a.pdf", 4, 1)


def test_download_outcomes_and_summary(root, monkeypatch):
    rec = record(root, fonte="Manual")
    for n in range(4):
        origin(root, [rec], ANNEX + f"{n}.pdf", "prova", f"Prova {n}")
    job = create_job("download", {"registros": [rec]}, root, launch=False)
    def download(client, doc, root, *, report):
        n = int(Path(doc["url"]).stem)
        if n == 2:
            raise AccessRequired("Coleta automática não permitida pelo robots.txt")
        if n == 3:
            raise ValueError("Resposta não é PDF")
        return doc["documento_id"], "novos" if n == 0 else "reaproveitados"
    monkeypatch.setattr("acervo.jobs.download_origin", download)
    assert Runner(job, root).run() == "parcial"
    row = job_row(root, job)
    assert [row[k] for k in ["total", "processados", "novos", "reaproveitados", "bloqueados", "falhas_download", "erros"]] == [4, 4, 1, 1, 0, 2, 2]
    assert row["arquivo_atual"] == ""
    assert "importação manual" in row["mensagem"]


def test_successful_alternative_prevents_manual_pending_count(root, monkeypatch):
    rec = record(root, fonte="Manual")
    first_url, second_url = ANNEX + "mirror-one.pdf", ANNEX + "mirror-two.pdf"
    did = origin(root, [rec], first_url, "prova", "Prova principal")
    with connect(root) as c:
        c.execute("INSERT INTO origens_documento(documento_id,url,tipo,nome) VALUES(?,?,?,?)", (did, second_url, "prova", "Prova alternativa"))
    job = create_job("download", {"registros": [rec]}, root, launch=False)
    attempted = []
    def download(client, doc, root, *, report):
        attempted.append(doc["url"])
        if doc["url"] == first_url:
            raise HumanActionRequired("captcha")
        with connect(root) as c:
            c.execute("UPDATE origens_documento SET estado='baixado',erro='',motivo_codigo='baixado' WHERE documento_id=?", (did,))
        return did, "novos"
    monkeypatch.setattr("acervo.jobs.download_origin", download)
    assert Runner(job, root).run() == "concluída"
    row = job_row(root, job)
    assert attempted == [first_url, second_url]
    assert [row["total"], row["processados"], row["bloqueados"], row["novos"]] == [1, 1, 0, 1]
    with connect(root) as c:
        assert {r[0] for r in c.execute("SELECT estado FROM origens_documento WHERE documento_id=?", (did,))} == {"baixado"}


def test_cancel_during_download_does_not_complete_item(root, monkeypatch):
    rec = record(root, fonte="Manual")
    origin(root, [rec], ANNEX + "p.pdf", "prova", "Prova")
    job = create_job("download", {"registros": [rec]}, root, launch=False)
    def download(*args, **kwargs):
        raise Cancelled("Cancelado")
    monkeypatch.setattr("acervo.jobs.download_origin", download)
    assert Runner(job, root).run() == "cancelada"
    assert job_row(root, job)["processados"] == 0
    assert job_row(root, job)["total"] == 1


def test_real_download_reports_reuse_and_repairs_corruption(root):
    rec = record(root)
    data = pdf("Gabarito")
    url = ANNEX + "f.pdf"
    did = store_pdf([data], [rec], url, "gabarito definitivo", "Gabarito definitivo", root)
    with connect(root) as c:
        expected_referer = c.execute("SELECT c.url FROM concursos c JOIN registros r ON r.concurso_id=c.id WHERE r.id=?", (rec,)).fetchone()[0]
    with connect(root) as c:
        doc_origin = dict(c.execute("SELECT * FROM origens_documento WHERE url=?", (url,)).fetchone())
    class Response:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, size): return iter([data])
    class Client:
        calls = 0
        def check(self): pass
        def get(self, url, *args, **kwargs):
            self.calls += 1
            assert kwargs["purpose"] == "document"
            assert kwargs["referer"] == expected_referer
            return Response()
    client = Client()
    assert download_origin(client, doc_origin, root, report=True) == (did, "reaproveitados")
    assert client.calls == 0
    safe_path(documents(rec, root)[0]["caminho"], root).write_bytes(b"corrompido")
    assert download_origin(client, doc_origin, root, report=True) == (did, "novos")
    assert client.calls == 1


def test_document_download_skips_robots_but_discovery_checks_it(monkeypatch):
    client = Client()
    calls = []
    class Response:
        status_code = 200
        headers = {}
        def raise_for_status(self): pass
        def close(self): pass
    def request(url, stream=False, **kwargs):
        calls.append((url, kwargs))
        return Response()
    original_request = client._request
    monkeypatch.setattr(client, "_request", request)
    response = client.get(ANNEX + "3258.pdf", purpose="document", referer=PARANAGUA)
    assert response.status_code == 200
    assert calls == [(ANNEX + "3258.pdf", {"purpose": "document", "referer": PARANAGUA})]
    assert not client.robots
    def deny(url):
        raise AccessRequired("robots bloqueou descoberta")
    monkeypatch.setattr(client, "allowed", deny)
    monkeypatch.setattr(client, "_request", original_request)
    with pytest.raises(AccessRequired, match="robots"):
        client.get("https://fafipa.selecao.net.br/concurso/3201")


@pytest.mark.parametrize("status,error", [(403, HumanActionRequired), (404, SourceUnavailable), (429, TemporaryFailure)])
def test_http_document_failure_categories(monkeypatch, status, error):
    client = Client()
    class Response:
        status_code = status
        headers = {}
        def close(self): pass
        def raise_for_status(self): pass
    monkeypatch.setattr(client, "_request", lambda *a, **k: Response())
    if status == 429:
        monkeypatch.setattr(client, "sleep", lambda _: None)
    with pytest.raises(error):
        client.get(ANNEX + "file.pdf", purpose="document")


def test_catalog_views_require_intact_file_and_export_scope(root):
    first = record(root, fonte="FAFIPA", url=PARANAGUA, edital="01/2018")
    second = record(root, fonte="FAFIPA", url=PARANAGUA + "-outro", cargo="Motorista", edital="02/2018")
    good = import_pdf(pdf("Orcamento planejamento"), [first], "prova", "caderno.pdf", root)
    bad = origin(root, [first], ANNEX + "bad.pdf", "gabarito", "Gabarito pendente")
    corrupt = import_pdf(pdf("Arquivo que sera corrompido"), [second], "prova", "prova-corrompida.pdf", root)
    with connect(root) as c:
        path = c.execute("SELECT caminho FROM documentos WHERE id=?", (corrupt,)).fetchone()[0]
    safe_path(path, root).write_bytes(b"corrupcao")
    available = catalog_groups(root, view="available")
    pending = catalog_groups(root, view="pending")
    assert len(available) == 1 and available[0]["available_docs"][0]["id"] == good
    assert {d["id"] for g in pending for d in g["pending_docs"]} == {bad, corrupt}
    csv_ready = export_catalog_csv(root, view="available").decode("utf-8")
    csv_pending = export_catalog_csv(root, view="pending").decode("utf-8")
    assert "caderno.pdf" in csv_ready and "Gabarito pendente" not in csv_ready
    assert "Gabarito pendente" in csv_pending and "caderno.pdf" not in csv_pending


def test_app_library_starts_with_available_contests_and_has_pending_view(root, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("ACERVO_DIR", str(root))
    rec = record(root, fonte="FAFIPA", url=PARANAGUA, orgao="Prefeitura do Município de Paranaguá - PR", municipio="Paranaguá", edital="01/2018")
    import_pdf(pdf("Caderno de prova"), [rec], "prova", "caderno.pdf", root)
    origin(root, [rec], ANNEX + "pendente.pdf", "gabarito definitivo", "Gabarito definitivo")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    assert not app.exception
    assert next(r for r in app.radio if r.label == "Visão do acervo").value == "Disponíveis no acervo"
    assert "Paranaguá" in str(next(x for x in app.selectbox if x.label == "Concurso").options)
    next(r for r in app.radio if r.label == "Visão do acervo").set_value("Catalogados e pendentes").run()
    assert not app.exception
    assert any(b.label == "Tentar downloads pendentes" for b in app.button)


def test_v2_migration_requeues_only_old_robots_blocks_without_downloading(root):
    rec = record(root)
    url = ANNEX + "robots.pdf"
    origin(root, [rec], url, "prova", "Prova")
    with connect(root) as c:
        c.execute("UPDATE origens_documento SET estado='intervenção necessária',erro='robots.txt disallow',motivo_codigo='requer_intervencao' WHERE url=?", (url,))
        c.execute("ALTER TABLE concursos DROP COLUMN edital")
        for col in ["motivo_codigo", "http_status", "tentado_em"]:
            c.execute(f"ALTER TABLE origens_documento DROP COLUMN {col}")
        c.execute("PRAGMA user_version=2")
    initialize(root)
    with connect(root) as c:
        row = c.execute("SELECT estado,erro,motivo_codigo,http_status FROM origens_documento WHERE url=?", (url,)).fetchone()
        assert tuple(row) == ("pendente", "", "aguardando_nova_tentativa", None)
        assert c.execute("PRAGMA user_version").fetchone()[0] == 4
    assert not list((root / "pdfs").rglob("*.pdf"))


def test_app_download_panel_start_recovery_and_fast_finish(root, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("ACERVO_DIR", str(root))
    record(root)
    original_create = create_job
    monkeypatch.setattr("acervo.jobs.create_job", lambda action, params: original_create(action, params, root, launch=False))
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    next(b for b in app.button if b.label.startswith("Baixar todos os")).click().run()
    assert not app.exception
    job = recent_jobs(root)[0]["id"]
    runner = Runner(job, root)
    runner.status("Baixando e indexando PDFs", "caderno.pdf", total=4, reset=True)
    runner.status(outcome="novos")
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    assert not app.exception
    assert any("caderno.pdf" in c.value for c in app.caption)
    assert any(m.label == "PDFs novos" and m.value == "1" for m in app.metric)
    assert len([b for b in app.button if b.label == "Cancelar coleta"]) == 1
    with connect(root) as c:
        c.execute("UPDATE execucoes SET estado='parcial',etapa='Execução parcial',bloqueados=3,processados=4,mensagem='3 pendentes de importação manual' WHERE id=?", (job,))
    app.run()
    assert not app.exception
    assert any("importação manual" in w.value for w in app.warning)
    assert not any(b.label == "Cancelar coleta" for b in app.button)
