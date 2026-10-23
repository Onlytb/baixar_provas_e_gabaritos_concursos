from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .model import Filters, location, norm, year
from .preference import visible_origin_links


def now():
    return datetime.now(timezone.utc).isoformat()


def root_dir():
    return Path(os.environ.get("ACERVO_DIR", "acervo_pci")).resolve()


@contextmanager
def connect(root=None):
    root = Path(root or root_dir())
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "provas.sqlite3", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE concursos(id INTEGER PRIMARY KEY, fonte TEXT NOT NULL, url TEXT NOT NULL UNIQUE, orgao TEXT NOT NULL, banca TEXT NOT NULL, ano INTEGER, municipio TEXT, uf TEXT, edital TEXT DEFAULT '', escolaridade TEXT DEFAULT '', areas_atuacao TEXT DEFAULT '', areas_formacao TEXT DEFAULT '');
CREATE TABLE registros(id INTEGER PRIMARY KEY, concurso_id INTEGER NOT NULL REFERENCES concursos(id), cargo TEXT NOT NULL, cargo_norm TEXT NOT NULL, categoria TEXT DEFAULT '', estado TEXT DEFAULT 'catalogado', erro TEXT DEFAULT '', etiquetas TEXT DEFAULT '[]', atualizado_em TEXT, UNIQUE(concurso_id,cargo_norm));
CREATE TABLE documentos(id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE, caminho TEXT, nome TEXT NOT NULL, texto_estado TEXT DEFAULT 'pendente', atualizado_em TEXT);
CREATE TABLE origens_documento(id INTEGER PRIMARY KEY, documento_id INTEGER NOT NULL REFERENCES documentos(id), url TEXT NOT NULL UNIQUE, tipo TEXT NOT NULL, nome TEXT, estado TEXT DEFAULT 'pendente', erro TEXT DEFAULT '', motivo_codigo TEXT DEFAULT 'aguardando_nova_tentativa', http_status INTEGER, tentado_em TEXT);
CREATE TABLE documento_registro(documento_id INTEGER NOT NULL REFERENCES documentos(id), registro_id INTEGER NOT NULL REFERENCES registros(id), PRIMARY KEY(documento_id,registro_id));
CREATE VIRTUAL TABLE textos USING fts5(conteudo, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE execucoes(id INTEGER PRIMARY KEY, acao TEXT NOT NULL, parametros TEXT NOT NULL, estado TEXT NOT NULL, cancelar INTEGER DEFAULT 0, criado_em TEXT, atualizado_em TEXT, pid INTEGER, mensagem TEXT DEFAULT '', processados INTEGER DEFAULT 0, erros INTEGER DEFAULT 0);
CREATE UNIQUE INDEX uma_execucao ON execucoes((1)) WHERE estado IN ('iniciando','executando');
CREATE TABLE eventos(id INTEGER PRIMARY KEY, execucao_id INTEGER REFERENCES execucoes(id), momento TEXT, nivel TEXT, mensagem TEXT);
CREATE TABLE categorias(fonte TEXT, nome TEXT, url TEXT, PRIMARY KEY(fonte,url));
CREATE TABLE facetas_qconcursos(campo TEXT NOT NULL, nome TEXT NOT NULL, valor TEXT NOT NULL, PRIMARY KEY(campo,valor));
CREATE INDEX vinculos_registro ON documento_registro(registro_id);
"""


def initialize(root=None):
    root = Path(root or root_dir())
    removed_paths = []
    migrated_v4 = False
    with connect(root) as c:
        c.execute("PRAGMA journal_mode=WAL")
        version = c.execute("PRAGMA user_version").fetchone()[0]
        if version >= 1:
            if version < 4:
                backup_dir = root / "backups"
                backup_dir.mkdir(exist_ok=True)
                with sqlite3.connect(backup_dir / (datetime.now().strftime("antes-migracao-v4-%Y%m%d-%H%M%S") + ".sqlite3")) as backup:
                    c.backup(backup)
                if version < 2:
                    migrate_v2(c)
                migrate_v3(c)
                removed_paths = migrate_v4(c)
                migrated_v4 = True
        else:
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "provas" in tables:
                backup_dir = root / "backups"
                backup_dir.mkdir(exist_ok=True)
                with sqlite3.connect(backup_dir / (datetime.now().strftime("antes-migracao-%Y%m%d-%H%M%S") + ".sqlite3")) as backup:
                    c.backup(backup)
            c.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            if "provas" in tables:
                for old in c.execute("SELECT * FROM provas").fetchall():
                    upsert_record(c, fonte="PCI", url=old["url_detalhe"], cargo=old["cargo"], ano=old["ano"], orgao=old["orgao"] or "", banca=old["banca"] or "", categoria=old["categoria"] or "")
            if "arquivos" in tables:
                for old in c.execute("SELECT * FROM arquivos").fetchall():
                    rec = c.execute("SELECT r.id FROM registros r JOIN concursos cc ON cc.id=r.concurso_id WHERE cc.url=?", (old["url_detalhe"],)).fetchone()
                    if not rec:
                        continue
                    doc = discover_document(c, [rec[0]], old["url_pdf"], old["tipo"] or "prova", old["nome_original"] or "arquivo.pdf")
                    old_path = Path(old["caminho"] or "")
                    candidates = [old_path, root / old_path, root / "pdfs" / str(old_path).replace("\\", "/").split("pdfs/")[-1]]
                    path = next((p.resolve() for p in candidates if p.is_file() and p.resolve().is_relative_to(root.resolve())), None)
                    if path:
                        c.execute("UPDATE documentos SET caminho=?,sha256=?,texto_estado='pendente' WHERE id=?", (path.relative_to(root.resolve()).as_posix(), old["sha256"] or None, doc))
                        c.execute("UPDATE origens_documento SET estado='baixado' WHERE documento_id=?", (doc,))
            c.execute("PRAGMA user_version=1")
            migrate_v2(c)
            migrate_v3(c)
            removed_paths = migrate_v4(c)
            migrated_v4 = True
    if migrated_v4:
        pdf_root = (root / "pdfs").resolve()
        for relative in removed_paths:
            path = (root / relative).resolve()
            if path.is_relative_to(pdf_root) and path.suffix.lower() == ".pdf":
                path.unlink(missing_ok=True)
        temporary = root / "catalogo.csv.part"
        temporary.write_bytes(export_csv(root))
        temporary.replace(root / "catalogo.csv")


def migrate_v2(c):
    from .sources import kind_for

    # The same transaction upgrades old catalogs and fresh databases.
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    if c.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return
    for name, declaration in {
        "etapa": "TEXT DEFAULT 'Preparando'", "arquivo_atual": "TEXT DEFAULT ''",
        "total": "INTEGER", "novos": "INTEGER DEFAULT 0",
        "reaproveitados": "INTEGER DEFAULT 0", "bloqueados": "INTEGER DEFAULT 0",
        "falhas_download": "INTEGER DEFAULT 0",
    }.items():
        c.execute(f"ALTER TABLE execucoes ADD COLUMN {name} {declaration}")
    origins = c.execute("""SELECT DISTINCT o.id,o.nome FROM origens_documento o
        JOIN documento_registro dr ON dr.documento_id=o.documento_id
        JOIN registros r ON r.id=dr.registro_id JOIN concursos cc ON cc.id=r.concurso_id
        WHERE cc.fonte='FAFIPA' AND o.url LIKE 'https://%'""").fetchall()
    for origin in origins:
        c.execute("UPDATE origens_documento SET tipo=? WHERE id=?", (kind_for(origin["nome"]), origin["id"]))
    c.execute("PRAGMA user_version=2")


def migrate_v3(c):
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    if c.execute("PRAGMA user_version").fetchone()[0] >= 3:
        return
    if "edital" not in {row[1] for row in c.execute("PRAGMA table_info(concursos)")}:
        c.execute("ALTER TABLE concursos ADD COLUMN edital TEXT DEFAULT ''")
    columns = {row[1] for row in c.execute("PRAGMA table_info(origens_documento)")}
    for column, declaration in {
        "motivo_codigo": "TEXT DEFAULT 'aguardando_nova_tentativa'",
        "http_status": "INTEGER", "tentado_em": "TEXT",
    }.items():
        if column not in columns:
            c.execute(f"ALTER TABLE origens_documento ADD COLUMN {column} {declaration}")
    c.execute("""UPDATE origens_documento SET estado='pendente',erro='',
        motivo_codigo='aguardando_nova_tentativa',http_status=NULL
        WHERE erro LIKE '%robots.txt%' AND estado='intervenção necessária'""")
    c.execute("PRAGMA user_version=3")


def migrate_v4(c):
    """Remove the FAFIPA catalog and preserve only files still linked elsewhere."""
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    if c.execute("PRAGMA user_version").fetchone()[0] >= 4:
        return []
    columns = {row[1] for row in c.execute("PRAGMA table_info(concursos)")}
    for column in ("escolaridade", "areas_atuacao", "areas_formacao"):
        if column not in columns:
            c.execute(f"ALTER TABLE concursos ADD COLUMN {column} TEXT DEFAULT ''")
    c.execute("CREATE TABLE IF NOT EXISTS facetas_qconcursos(campo TEXT NOT NULL, nome TEXT NOT NULL, valor TEXT NOT NULL, PRIMARY KEY(campo,valor))")
    c.execute("DELETE FROM categorias WHERE fonte='FAFIPA'")
    c.execute("DELETE FROM facetas_qconcursos")
    c.execute("DELETE FROM documento_registro WHERE registro_id IN (SELECT r.id FROM registros r JOIN concursos cc ON cc.id=r.concurso_id WHERE cc.fonte='FAFIPA')")
    c.execute("DELETE FROM registros WHERE concurso_id IN (SELECT id FROM concursos WHERE fonte='FAFIPA')")
    c.execute("DELETE FROM concursos WHERE fonte='FAFIPA'")
    orphaned = c.execute("SELECT id,caminho FROM documentos WHERE NOT EXISTS(SELECT 1 FROM documento_registro dr WHERE dr.documento_id=documentos.id)").fetchall()
    for row in orphaned:
        c.execute("DELETE FROM origens_documento WHERE documento_id=?", (row["id"],))
        c.execute("DELETE FROM textos WHERE rowid=?", (row["id"],))
        c.execute("DELETE FROM documentos WHERE id=?", (row["id"],))
    c.execute("PRAGMA user_version=4")
    return [row["caminho"] for row in orphaned if row["caminho"]]


def upsert_record(c, *, fonte, url, cargo, orgao, banca, ano=None, municipio=None, uf=None, categoria="", edital="", escolaridade="", areas_atuacao="", areas_formacao=""):
    city, state = location(orgao)
    c.execute("INSERT INTO concursos(fonte,url,orgao,banca,ano,municipio,uf,edital,escolaridade,areas_atuacao,areas_formacao) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET orgao=excluded.orgao,banca=excluded.banca,ano=excluded.ano,municipio=excluded.municipio,uf=excluded.uf,edital=CASE WHEN excluded.edital<>'' THEN excluded.edital ELSE concursos.edital END,escolaridade=CASE WHEN excluded.escolaridade<>'' THEN excluded.escolaridade ELSE concursos.escolaridade END,areas_atuacao=CASE WHEN excluded.areas_atuacao<>'' THEN excluded.areas_atuacao ELSE concursos.areas_atuacao END,areas_formacao=CASE WHEN excluded.areas_formacao<>'' THEN excluded.areas_formacao ELSE concursos.areas_formacao END", (fonte, url, orgao, banca, year(ano), municipio or city, uf or state, edital, escolaridade, areas_atuacao, areas_formacao))
    contest = c.execute("SELECT id FROM concursos WHERE url=?", (url,)).fetchone()[0]
    c.execute("INSERT INTO registros(concurso_id,cargo,cargo_norm,categoria,atualizado_em) VALUES(?,?,?,?,?) ON CONFLICT(concurso_id,cargo_norm) DO UPDATE SET cargo=excluded.cargo,categoria=excluded.categoria,atualizado_em=excluded.atualizado_em", (contest, cargo, norm(cargo), categoria, now()))
    return c.execute("SELECT id FROM registros WHERE concurso_id=? AND cargo_norm=?", (contest, norm(cargo))).fetchone()[0]


def discover_document(c, record_ids, url, kind, name):
    origin = c.execute("SELECT documento_id FROM origens_documento WHERE url=?", (url,)).fetchone()
    if origin:
        doc = origin[0]
        c.execute("UPDATE origens_documento SET tipo=?,nome=? WHERE url=?", (kind, name, url))
        c.execute("UPDATE documentos SET nome=?,atualizado_em=? WHERE id=?", (name, now(), doc))
    else:
        doc = c.execute("INSERT INTO documentos(nome,atualizado_em) VALUES(?,?)", (name, now())).lastrowid
        c.execute("INSERT INTO origens_documento(documento_id,url,tipo,nome) VALUES(?,?,?,?)", (doc, url, kind, name))
    for rec in record_ids:
        c.execute("INSERT OR IGNORE INTO documento_registro VALUES(?,?)", (doc, rec))
    return doc


def records(root=None, filters=None):
    with connect(root) as c:
        result = [dict(r) for r in c.execute("""SELECT r.*,c.fonte,c.url,c.orgao,c.banca,c.ano,c.municipio,c.uf,c.edital,c.escolaridade,c.areas_atuacao,c.areas_formacao,c.id concurso_id
        FROM registros r JOIN concursos c ON c.id=r.concurso_id ORDER BY c.ano DESC,c.orgao,r.cargo""")]
        visible = visible_origin_links(c)
        saved_documents = valid_document_ids(c, root)
    by_record = {}
    for origin in visible:
        by_record.setdefault(origin["registro_id"], []).append(origin)
    f = filters or Filters()
    for r in result:
        origins = by_record.get(r["id"], [])
        r["tipos"] = ",".join(sorted({o["tipo"] for o in origins}))
        r["tipos_baixados"] = ",".join(sorted({o["tipo"] for o in origins if o["documento_id"] in saved_documents}))
        r["baixados"] = len({o["documento_id"] for o in origins if o["documento_id"] in saved_documents})
        for kind in ("prova", "gabarito"):
            found = any(t.startswith(kind) for t in (r["tipos"] or "").split(","))
            saved = any(t.startswith(kind) for t in (r["tipos_baixados"] or "").split(","))
            suffix = "a" if kind == "prova" else "o"
            r[kind + "_situacao"] = "salv" + suffix if saved else "identificad" + suffix if found else "importação necessária" if r["estado"] in ("verificação necessária", "acesso restrito") else "não identificad" + suffix
    return [r for r in result if f.matches(r) and (not f.tipos or any(t in (r["tipos"] or "").split(",") for t in f.tipos))]


@lru_cache(maxsize=4096)
def _valid_pdf_cached(path, expected_sha, size, modified):
    try:
        with open(path, "rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest() == expected_sha
    except OSError:
        return False


def valid_document_ids(c, root=None):
    root = Path(root or root_dir()).resolve()
    result = set()
    for row in c.execute("SELECT id,caminho,sha256 FROM documentos WHERE caminho IS NOT NULL AND sha256 IS NOT NULL"):
        path = (root / row["caminho"]).resolve()
        try:
            if path.is_relative_to(root) and path.is_file() and _valid_pdf_cached(str(path), row["sha256"], path.stat().st_size, path.stat().st_mtime_ns):
                result.add(row["id"])
        except OSError:
            pass
    return result


def documents(record_id=None, root=None, query=""):
    args = []
    sql = """SELECT d.*,group_concat(DISTINCT o.tipo) tipos,group_concat(DISTINCT o.estado) estados,group_concat(DISTINCT o.erro) erros FROM documentos d LEFT JOIN origens_documento o ON o.documento_id=d.id WHERE 1=1"""
    if record_id is not None:
        sql += " AND EXISTS(SELECT 1 FROM documento_registro dr WHERE dr.documento_id=d.id AND dr.registro_id=?)"
        args.append(record_id)
    if query.strip():
        # Quote tokens so user punctuation cannot become FTS operators or syntax errors.
        tokens = query.replace('"', ' ').split()
        if tokens:
            sql += " AND d.id IN (SELECT rowid FROM textos WHERE textos MATCH ?)"
            args.append(" AND ".join('"' + t + '"' for t in tokens))
    sql += " GROUP BY d.id ORDER BY d.id DESC"
    with connect(root) as c:
        visible = visible_origin_links(c, [record_id] if record_id is not None else None)
        by_doc = {}
        for origin in visible:
            by_doc.setdefault(origin["documento_id"], {})[origin["id"]] = origin
        rows = [dict(r) for r in c.execute(sql, args)]
        rows = [r for r in rows if r["id"] in by_doc]
        for row in rows:
            row["origens"] = list(by_doc[row["id"]].values())
            row["nome"] = row["origens"][0]["nome"] or row["nome"]
            row["registros_visiveis"] = sorted({o["registro_id"] for o in visible if o["documento_id"] == row["id"]})
            for field, key in [("tipos", "tipo"), ("estados", "estado"), ("erros", "erro")]:
                row[field] = ",".join(sorted({o[key] or "" for o in row["origens"]}))
            if query.strip() and args:
                excerpt = c.execute("SELECT snippet(textos,0,'[',']',' … ',35) FROM textos WHERE rowid=? AND textos MATCH ?", (row["id"], args[-1])).fetchone()
                row["trecho"] = excerpt[0] if excerpt else ""
    return rows


def catalog_groups(root=None, filters=None, query="", view="available"):
    """Shared, integrity-checked availability view used by the library and export."""
    filtered_records = records(root, filters)
    matching = documents(root=root, query=query)
    by_record = {}
    for doc in matching:
        doc["available"] = bool(doc["caminho"] and _document_path_valid(doc, root))
        for record_id in doc["registros_visiveis"]:
            by_record.setdefault(record_id, []).append(doc)
    # If a text search is active, only matching docs participate in the current view.
    groups = {}
    matching_ids = {x["id"] for x in matching}
    for row in filtered_records:
        docs = [d for d in by_record.get(row["id"], []) if d["id"] in matching_ids]
        available_docs = [d for d in docs if d["available"]]
        pending_docs = [d for d in docs if not d["available"]]
        key = row.get("concurso_id") or row["url"]
        group = groups.setdefault(key, {"id": key, "orgao": row["orgao"], "ano": row["ano"], "banca": row["banca"], "municipio": row["municipio"], "uf": row["uf"], "edital": row.get("edital", ""), "url": row["url"], "fonte": row["fonte"], "records": [], "available_docs": {}, "pending_docs": {}})
        group["records"].append(row)
        for doc in available_docs:
            group["available_docs"].setdefault(doc["id"], doc)
        for doc in pending_docs:
            group["pending_docs"].setdefault(doc["id"], doc)
    for key, group in list(groups.items()):
        group["records"].sort(key=lambda r: r["cargo"])
        group["available_docs"] = list(group["available_docs"].values())
        group["pending_docs"] = list(group["pending_docs"].values())
        if view == "available" and not group["available_docs"]:
            del groups[key]
        elif view == "pending" and not group["pending_docs"]:
            del groups[key]
    return list(groups.values())


def _document_path_valid(doc, root=None):
    if not doc.get("caminho") or not doc.get("sha256"):
        return False
    root = Path(root or root_dir()).resolve()
    path = (root / doc["caminho"]).resolve()
    try:
        return path.is_relative_to(root) and path.is_file() and _valid_pdf_cached(str(path), doc["sha256"], path.stat().st_size, path.stat().st_mtime_ns)
    except OSError:
        return False


def export_csv(root=None, filters=None):
    out = io.StringIO(newline="")
    w = csv.writer(out, delimiter=";")
    w.writerow(["id", "cargo", "ano", "orgao", "banca", "municipio", "uf", "fonte", "url_origem", "situacao", "etiquetas", "tipo", "arquivo", "url_documento", "estado", "erro"])
    for r in records(root, filters):
        docs = documents(r["id"], root) or [None]
        for d in docs:
            origins = d["origens"] if d else [None]
            for o in origins:
                w.writerow([r["id"], r["cargo"], r["ano"], r["orgao"], r["banca"], r["municipio"], r["uf"], r["fonte"], r["url"], r["estado"], ", ".join(json.loads(r["etiquetas"])), o["tipo"] if o else "", d["caminho"] if d else "", o["url"] if o else "", o["estado"] if o else "", o["erro"] if o else r["erro"]])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def export_catalog_csv(root=None, filters=None, query="", view="available"):
    out = io.StringIO(newline="")
    w = csv.writer(out, delimiter=";")
    w.writerow(["concurso", "edital", "ano", "banca", "município", "UF", "cargo", "tipo", "documento", "disponibilidade", "arquivo", "origem", "motivo"])
    for group in catalog_groups(root, filters, query, view):
        docs = group["available_docs"] if view == "available" else group["pending_docs"]
        for doc in docs:
            cargo_names = [r["cargo"] for r in group["records"] if r["id"] in doc["registros_visiveis"]]
            for origin in doc["origens"]:
                w.writerow([group["orgao"], group["edital"], group["ano"], group["banca"], group["municipio"], group["uf"], ", ".join(cargo_names), origin["tipo"], doc["nome"], "disponível" if doc["available"] else origin["estado"], doc["caminho"] or "", origin["url"], origin.get("erro", "")])
    return ("\ufeff" + out.getvalue()).encode("utf-8")
