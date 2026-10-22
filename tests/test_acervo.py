import io
import json
import sqlite3
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from acervo.db import connect, discover_document, documents, initialize, records, upsert_record
from acervo.files import import_pdf, safe_path, store_pdf
from acervo.http import AccessRequired, Cancelled, Client
from acervo.jobs import Runner, cancel_job, create_job, interrupted
from acervo.model import DEFAULT_ROLES, Filters
from acervo.sources import fafipa_contests, fafipa_detail, kind_for, pagination, pci_detail, pci_rows, pdf_links


def soup(html):
    return BeautifulSoup(html, "html.parser")


@pytest.fixture
def root(tmp_path):
    initialize(tmp_path)
    return tmp_path


def record(root, **overrides):
    params = dict(fonte="PCI", url="https://www.pciconcursos.com.br/provas/download/exemplo", cargo="Analista Administrativo", orgao="Prefeitura de Curitiba/PR", banca="FAFIPA", ano=2024)
    params.update(overrides)
    with connect(root) as c:
        return upsert_record(c, **params)


def pdf(text=None):
    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    if text:
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_migration_preserves_legacy_and_backup(tmp_path):
    c = sqlite3.connect(tmp_path / "provas.sqlite3")
    c.execute("CREATE TABLE provas(url_detalhe TEXT PRIMARY KEY,cargo TEXT,ano TEXT,orgao TEXT,banca TEXT,grupos TEXT,categoria TEXT,atualizado_em TEXT)")
    for n in range(28):
        c.execute("INSERT INTO provas VALUES(?,?,?,?,?,?,?,?)", (f"https://www.pciconcursos.com.br/provas/download/{n}", "Agente Administrativo", "2006", "Pref. Curitiba/PR", "IF/PR", "", "agente-administrativo", ""))
    c.commit()
    c.close()
    initialize(tmp_path)
    initialize(tmp_path)
    assert len(records(tmp_path)) == 28
    backups = list((tmp_path / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as c:
        assert c.execute("SELECT count(*) FROM provas").fetchone()[0] == 28
        assert c.execute("PRAGMA user_version").fetchone()[0] == 0


def test_filters_and_or_and_accents(root):
    record(root)
    record(root, url="https://x/2", banca="UFPR", orgao="Pref. Curitiba/PR", cargo="Administrador", ano=2006)
    record(root, url="https://x/3", banca="FGV", orgao="Câmara de São Paulo/SP", cargo="Analista Administrativo")
    record(root, url="https://x/4", ano=None)
    assert len(records(root, Filters(foco=True))) == 3
    assert len(records(root, Filters(cargos=["administrador", "analista administrativo"], bancas=["ufpr", "fgv"]))) == 2
    assert len(records(root, Filters(municipios=["sao paulo"], ufs=["SP"]))) == 1
    assert len(records(root, Filters(ano_inicio=2020))) == 2
    assert len(records(root, Filters(ano_fim=2010))) == 1


def test_default_public_management_excludes_support_roles():
    f = Filters(cargos=DEFAULT_ROLES)
    assert f.matches({"cargo": "Analista de Gestão Pública"})
    assert not f.matches({"cargo": "Assistente Administrativo - Técnico em Gestão Pública"})
    assert Filters(cargos=["Técnico em Gestão Pública"]).matches({"cargo": "Técnico em Gestão Pública"})


def test_no_fixed_banca_or_city_in_parser():
    html = '<table><tr><th>Prova</th><th>Ano</th><th>Órgão</th><th>Organizadora</th></tr><tr><td><a href="/provas/download/x">Analista Administrativo</a></td><td>2025</td><td>Pref. Recife/PE</td><td>FGV</td></tr></table>'
    assert pci_rows(soup(html))[0]["banca"] == "FGV"
    with pytest.raises(ValueError):
        pci_rows(soup("<p>layout mudou</p>"))


def test_javascript_link_is_not_mistaken_for_captcha():
    html = '<h1>Provas para Download</h1><a href="javascript:void(0)" class="prova-pdf-link" data-arquivo="prova.pdf" data-acao="baixar">Baixar</a>'
    with pytest.raises(ValueError, match="Link dinâmico"):
        pci_detail(soup(html), "https://www.pciconcursos.com.br/provas/download/x")
    captcha = '<h1>Provas para Download</h1><div class="cf-turnstile"></div>'
    assert pci_detail(soup(captcha), "https://www.pciconcursos.com.br/provas/download/x") == ("verificação necessária", [])
    assert pci_detail(soup("<h1>Provas para Download</h1>"), "https://www.pciconcursos.com.br/x")[0] == "sem arquivos"
    with pytest.raises(ValueError):
        pci_detail(soup("<h1>Erro</h1>"), "https://www.pciconcursos.com.br/x")


def test_view_download_dedup_without_hash_guessing():
    html = '<li><a href="/provas/123/abc1f/p.pdf" data-acao="ver">ícone</a><a href="/provas/123/abc1/p.pdf" data-acao="baixar">ícone</a><a href="javascript:void(0)" data-acao="compartilhar">Compartilhar</a></li><li><a href="/provas/123/q/gabarito.pdf">Gabarito definitivo</a></li>'
    docs = pdf_links(soup(html), "https://www.pciconcursos.com.br/provas/download/x")
    assert len(docs) == 2
    assert docs[0]["url"].endswith("/abc1/p.pdf")
    assert docs[1]["tipo"] == "gabarito definitivo"


@pytest.mark.parametrize("label,kind", [("Edital - Divulgação do gabarito preliminar e caderno de questões", "comunicado"), ("Gabarito Definitivo", "gabarito definitivo"), ("Gabarito preliminar", "gabarito preliminar"), ("Caderno de questões - Analista Administrativo", "prova")])
def test_classify(label, kind):
    assert kind_for(label) == kind


def test_fafipa_roles_and_announcements():
    html = '<h1>Concurso Público do Município de Foz do Iguaçu - PR</h1><p>Edital nº 001/2026</p><h2>Cargos e vagas</h2><ul><li>Agente Administrativo Júnior 1 vaga + cadastro de reserva</li><li>Administrador Cadastro de reserva</li></ul><h2>Documentos</h2><a href="https://anexos-r2.selecao.net.br/x.pdf">Edital - Divulgação do caderno de questões</a><a href="https://anexos-r2.selecao.net.br/g.pdf">Gabarito definitivo</a>'
    rows, docs = fafipa_detail(soup(html), "https://fafipa.selecao.net.br/informacoes/1/")
    assert [r["cargo"] for r in rows] == ["Agente Administrativo Júnior", "Administrador"]
    assert rows[0]["ano"] == 2026
    assert [d["tipo"] for d in docs] == ["comunicado", "gabarito definitivo"]


def test_fafipa_current_div_layout():
    html = '<h1>Concurso Público do Município de Curitiba - PR</h1><p>Edital nº 5/2026</p><button data-aba="cargos">Cargos e Vagas</button><div data-aba="cargos"><h2>Cargos e vagas</h2><div class="det-row"><span>Analista Administrativo</span><b>10 vagas</b></div></div>'
    rows, _ = fafipa_detail(soup(html), "https://fafipa.selecao.net.br/informacoes/1/")
    assert rows[0]["cargo"] == "Analista Administrativo"


def test_fafipa_current_index_routes():
    html = '<a href="https://fafipa.selecao.net.br/concurso/4170-concurso-publico-foz">Mais informações</a><a href="/informacoes/4100/">Concurso antigo</a><a href="https://other.example/concurso/4170-x">Outro portal</a>'
    assert len(fafipa_contests(soup(html))) == 2


def test_view_download_in_different_sections():
    html = '<div><a href="/a/preview/p.pdf">Ver p.pdf</a></div><div><a href="/a/download/p.pdf">Baixar p.pdf</a></div>'
    docs = pdf_links(soup(html), "https://www.pciconcursos.com.br/provas/download/x")
    assert len(docs) == 1 and "/download/p.pdf" in docs[0]["url"]


def test_pagination_no_year_shortcut():
    html = '<a href="/provas/analista-administrativo/2">2</a><a href="/provas/analista-administrativo/21">21</a><a href="/provas/download/x">prova</a>'
    assert len(pagination(soup(html), "https://www.pciconcursos.com.br/provas/analista-administrativo")) == 2


def test_import_dedup_many_roles_and_sources_and_search(root):
    first = record(root)
    second = record(root, url="https://x/2", cargo="Administrador")
    data = pdf("Licitacoes e gestao de projetos")
    with connect(root) as c:
        discover_document(c, [first], "https://anexos-r2.selecao.net.br/prova.pdf", "prova", "prova.pdf")
    a = store_pdf([data[:2], data[2:]], [first], "https://anexos-r2.selecao.net.br/prova.pdf", "prova", "prova.pdf", root)
    b = import_pdf(data, [second], "prova", "outra-origem.pdf", root)
    assert a == b
    assert len(documents(root=root)) == 1
    assert len(documents(first, root)) == len(documents(second, root)) == 1
    assert len(documents(root=root)[0]["origens"]) == 2
    found = documents(root=root, query="licitações projetos")
    assert len(found) == 1 and "Licitacoes" in found[0]["trecho"]
    assert documents(root=root, query='" OR * :') == []
    initialize(root)
    assert len(documents(root=root, query="gestao")) == 1


def test_blank_pdf_and_invalid_html(root):
    rec = record(root)
    import_pdf(pdf(), [rec], "prova", "digitalizado.pdf", root)
    assert documents(rec, root)[0]["texto_estado"] == "sem texto pesquisável"
    with pytest.raises(ValueError, match="não é PDF"):
        import_pdf(b"<html>captcha</html>", [rec], "prova", "falso.pdf", root)
    assert not list((root / "temporarios").glob("*.part"))
    with pytest.raises(ValueError):
        safe_path("../fora.pdf", root)


def test_job_exclusion_cancel_and_restart(root):
    job = create_job("catalogar", {}, root, launch=False)
    with pytest.raises(ValueError, match="coleta ativa"):
        create_job("catalogar", {}, root, launch=False)
    cancel_job(job, root)
    assert Runner(job, root).run() == "cancelada"
    other = create_job("categorias", {}, root, launch=False)
    interrupted(root)
    with connect(root) as c:
        assert c.execute("SELECT estado FROM execucoes WHERE id=?", (other,)).fetchone()[0] == "interrompida"


def test_robots_and_redirect_validation():
    from protego import Protego
    client = Client()
    client.robots["https://www.pciconcursos.com.br"] = Protego.parse("User-agent: *\nDisallow: /*.pdf\n")
    with pytest.raises(AccessRequired):
        client.allowed("https://www.pciconcursos.com.br/provas/x.pdf")
    for url in ["http://www.pciconcursos.com.br/a", "https://evil.example/a", "https://fafipa.selecao.net.br:8443/a", "https://user@fafipa.selecao.net.br/a"]:
        with pytest.raises(AccessRequired):
            client.validate(url)


def test_repeated_records_and_documents_are_idempotent(root):
    rec = record(root)
    assert record(root) == rec
    data = pdf("Orcamento publico")
    one = import_pdf(data, [rec], "prova", "a.pdf", root)
    assert import_pdf(data, [rec], "prova", "a.pdf", root) == one
    assert len(records(root)) == len(documents(root=root)) == 1


def test_fafipa_job_discovers_current_links_and_preserves_role_scope(root, monkeypatch):
    url = 'https://fafipa.selecao.net.br/concurso/4161-municipio'
    index = soup(f'<a href="{url}">Mais informações</a>')
    detail = soup('<h1>Concurso Público do Município de Três Barras - SC</h1><p>Edital nº 001/2026</p><div data-aba="cargos"><div class="det-row"><span>Analista Administrativo</span><b>1 vaga</b></div><div class="det-row"><span>Auxiliar Administrativo</span><b>1 vaga</b></div></div><a href="https://anexos-r2.selecao.net.br/g.pdf">Gabarito definitivo</a><a href="https://anexos-r2.selecao.net.br/aux.pdf">Caderno Auxiliar Administrativo</a><a href="https://anexos-r2.selecao.net.br/r.pdf">Resultado final</a>')
    for _ in range(2):
        job = create_job("catalogar", {"filtros": Filters(cargos=["Analista Administrativo"], fontes=["FAFIPA"]).to_dict()}, root, launch=False)
        runner = Runner(job, root)
        monkeypatch.setattr(runner, "page", lambda u: detail if u == url else index)
        assert runner.run() == "concluída"
    assert len(records(root)) == 1
    docs = documents(records(root)[0]["id"], root)
    assert len(docs) == 1 and docs[0]["tipos"] == "gabarito definitivo"


def test_failed_index_is_partial_not_success(root, monkeypatch):
    job = create_job("catalogar", {"filtros": {"fontes": ["FAFIPA"]}}, root, launch=False)
    runner = Runner(job, root)
    monkeypatch.setattr(runner, "page", lambda u: soup("<p>Layout desconhecido</p>"))
    assert runner.run() == "parcial"


def test_corrupt_file_is_repaired_on_reimport(root):
    rec = record(root)
    data = pdf("Gestao de pessoas")
    did = import_pdf(data, [rec], "prova", "a.pdf", root)
    path = safe_path(documents(rec, root)[0]["caminho"], root)
    path.write_bytes(b"invalido")
    assert import_pdf(data, [rec], "prova", "a.pdf", root) == did
    assert path.read_bytes() == data


def test_app_starts_without_errors(root, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("ACERVO_DIR", str(root))
    record(root)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    assert not app.exception
    assert [t.label for t in app.tabs] == ["Buscar provas", "Meu acervo", "Importar arquivos"]


def test_app_pdf_search_and_import_form(root, monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("ACERVO_DIR", str(root))
    rec = record(root)
    import_pdf(pdf("Orcamento e licitacoes"), [rec], "gabarito definitivo", "gabarito.pdf", root)
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py", default_timeout=30).run()
    query = next(el for el in app.text_input if el.label == "Pesquisar no texto dos PDFs")
    query.input("licitações").run()
    assert not app.exception
    assert any("Orcamento" in el.value for el in app.info)
    next(el for el in app.radio if el.label == "Vincular a").set_value("Novo concurso / cargo").run()
    assert not app.exception
    assert any(el.label == "Órgão público" for el in app.text_input)
