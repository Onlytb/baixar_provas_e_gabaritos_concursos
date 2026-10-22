from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader

from .db import connect, discover_document, now, root_dir

MAX_BYTES = 200 * 1024 * 1024


def safe_path(relative, root=None):
    root = Path(root or root_dir()).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Arquivo fora da pasta do acervo")
    return path


def extract_text(path):
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            return "", "texto protegido"
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return text, "pesquisável" if text.strip() else "sem texto pesquisável"
    except Exception:
        return "", "falha na extração de texto"


def store_pdf(chunks, record_ids, source_url, kind, name, root=None, check=None):
    root = Path(root or root_dir())
    temp_dir = root / "temporarios"
    temp_dir.mkdir(parents=True, exist_ok=True)
    temporary = temp_dir / (uuid4().hex + ".pdf.part")
    digest = hashlib.sha256()
    total = 0
    head = b""
    try:
        with temporary.open("wb") as out:
            for chunk in chunks:
                if check:
                    check()
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError("PDF excede o limite de 200 MB")
                head = (head + chunk)[:5]
                out.write(chunk)
                digest.update(chunk)
        if head != b"%PDF-":
            raise ValueError("Resposta não é PDF (pode ser uma página de verificação)")
        # Validate structure before admitting bytes to the library.
        PdfReader(temporary)
        sha = digest.hexdigest()
        relative = f"pdfs/{sha[:2]}/{sha}.pdf"
        destination = safe_path(relative, root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
        text, text_state = extract_text(destination)
        with connect(root) as c:
            doc = discover_document(c, record_ids, source_url, kind, name)
            existing = c.execute("SELECT id FROM documentos WHERE sha256=?", (sha,)).fetchone()
            if existing and existing[0] != doc:
                target = existing[0]
                c.execute("INSERT OR IGNORE INTO documento_registro SELECT ?,registro_id FROM documento_registro WHERE documento_id=?", (target, doc))
                c.execute("UPDATE origens_documento SET documento_id=? WHERE documento_id=?", (target, doc))
                c.execute("DELETE FROM documento_registro WHERE documento_id=?", (doc,))
                c.execute("DELETE FROM textos WHERE rowid=?", (doc,))
                c.execute("DELETE FROM documentos WHERE id=?", (doc,))
                doc = target
            c.execute("UPDATE documentos SET sha256=?,caminho=?,texto_estado=?,atualizado_em=? WHERE id=?", (sha, relative, text_state, now(), doc))
            c.execute("UPDATE origens_documento SET estado='baixado',erro='',motivo_codigo='baixado',http_status=NULL WHERE documento_id=?", (doc,))
            c.execute("DELETE FROM textos WHERE rowid=?", (doc,))
            if text.strip():
                c.execute("INSERT INTO textos(rowid,conteudo) VALUES(?,?)", (doc, text))
        return doc
    finally:
        temporary.unlink(missing_ok=True)


def import_pdf(data, record_ids, kind, name, root=None):
    if not record_ids:
        raise ValueError("Selecione pelo menos um cargo/concurso")
    if len(data) > MAX_BYTES:
        raise ValueError("PDF excede o limite de 200 MB")
    # Content-addressed import URLs deduplicate the same upload while keeping download URLs.
    sha = hashlib.sha256(data).hexdigest()
    return store_pdf([data], record_ids, f"importacao:{sha}:{kind}", kind, name, root)


def download_origin(client, origin, root=None, *, report=False):
    with connect(root) as c:
        origin = dict(c.execute("SELECT * FROM origens_documento WHERE id=?", (origin["id"],)).fetchone())
        doc = c.execute("SELECT * FROM documentos WHERE id=?", (origin["documento_id"],)).fetchone()
        record_rows = c.execute("SELECT r.registro_id,c.url FROM documento_registro r JOIN registros x ON x.id=r.registro_id JOIN concursos c ON c.id=x.concurso_id WHERE r.documento_id=?", (doc["id"],)).fetchall()
        record_ids = [r["registro_id"] for r in record_rows]
        referer = record_rows[0]["url"] if record_rows else None
    if doc["caminho"]:
        path = safe_path(doc["caminho"], root)
        if path.is_file():
            with path.open("rb") as fh:
                actual = hashlib.file_digest(fh, "sha256").hexdigest()
            if actual == doc["sha256"]:
                if doc["texto_estado"] == "pendente":
                    text, state = extract_text(path)
                    with connect(root) as c:
                        c.execute("UPDATE documentos SET texto_estado=? WHERE id=?", (state, doc["id"]))
                        c.execute("DELETE FROM textos WHERE rowid=?", (doc["id"],))
                        if text.strip():
                            c.execute("INSERT INTO textos(rowid,conteudo) VALUES(?,?)", (doc["id"], text))
                with connect(root) as c:
                    c.execute("UPDATE origens_documento SET estado='baixado',erro='',motivo_codigo='baixado',http_status=NULL WHERE documento_id=?", (doc["id"],))
                return (doc["id"], "reaproveitados") if report else doc["id"]
    with client.get(origin["url"], stream=True, purpose="document", referer=referer) as response:
        with connect(root) as c:
            c.execute("UPDATE origens_documento SET tentado_em=?,http_status=? WHERE id=?", (now(), response.status_code, origin["id"]))
        doc_id = store_pdf(response.iter_content(128 * 1024), record_ids, origin["url"], origin["tipo"], origin["nome"], root, client.check)
        return (doc_id, "novos") if report else doc_id
