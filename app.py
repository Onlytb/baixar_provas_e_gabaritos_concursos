from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from uuid import uuid4

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from acervo.db import catalog_groups, connect, documents, export_catalog_csv, export_csv, initialize, records, root_dir, upsert_record, valid_document_ids
from acervo.files import import_pdf, safe_path, store_pdf
from acervo.jobs import cancel_job, create_job, recent_jobs
from acervo.model import DEFAULT_ROLES, EXTRA_ROLES, KINDS, TOPICS, Filters, terms

st.set_page_config(page_title="Acervo de provas", page_icon="📚", layout="wide")
initialize()
st.markdown("""<style>
.stApp {background: #f5f7fb;} [data-testid="stSidebar"] {background: #eaf0f6;}
h1,h2,h3 {color: #18334d;} [data-testid="stMetric"] {background: white; padding: 16px; border-radius: 12px; border: 1px solid #dce5ef;}
.stButton>button[kind="primary"] {background: #136b70; border-color: #136b70;}
div[data-testid="stTabs"] {margin-top: 1rem;}
</style>""", unsafe_allow_html=True)


def start(action, params):
    try:
        job = create_job(action, params)
        st.session_state["execucao_iniciada"] = job
        st.rerun()
    except Exception as e:
        st.error(str(e))


def record_label(row):
    return f"#{row['id']} · {row['ano'] or 'Ano não informado'} · {row['cargo']} · {row['orgao']} · {row['banca']}"


def file_detail(doc, key):
    st.write(f"**{doc['nome']}**")
    st.caption(f"{doc['tipos'] or 'Documento'} · {doc['texto_estado']} · {doc['estados'] or 'pendente'}")
    if doc.get("trecho"):
        st.info(doc["trecho"])
    if doc["erros"]:
        st.warning(doc["erros"])
    for origin in doc["origens"]:
        if origin["url"].startswith("https://"):
            st.link_button("Abrir documento na fonte · " + origin["tipo"], origin["url"])
    if doc["caminho"]:
        path = safe_path(doc["caminho"])
        if not path.is_file():
            st.warning("O arquivo não está na pasta persistente. Baixe novamente ou importe o PDF.")
            return
        data = path.read_bytes()
        st.download_button("Baixar PDF do acervo", data, file_name=f"documento-{doc['id']}.pdf", mime="application/pdf", key=f"download-{key}")
        if len(data) <= 20 * 1024 * 1024:
            if st.toggle("Visualizar PDF", key=f"preview-{key}"):
                encoded = base64.b64encode(data).decode()
                components.html(f'<iframe title="Visualizador de PDF" src="data:application/pdf;base64,{encoded}" width="100%" height="650"></iframe>', height=665)
        else:
            st.caption("Use o download para visualizar arquivos acima de 20 MB.")


with st.sidebar:
    st.markdown("### Filtros da busca")
    st.caption("Valores do mesmo campo: OU. Campos diferentes: E. Vazio: sem restrição.")
    with connect() as c:
        available = [r[0] for r in c.execute("SELECT DISTINCT nome FROM categorias ORDER BY nome")]
    cargos = st.multiselect("Cargos / termos do cargo", sorted(set(DEFAULT_ROLES + EXTRA_ROLES + available)), default=DEFAULT_ROLES, accept_new_options=True)
    extra = st.checkbox("Incluir cargos administrativos de apoio", help="Assistente, Agente, Auxiliar e Técnico Administrativo")
    if extra:
        cargos = list(dict.fromkeys(cargos + EXTRA_ROLES))
    foco = st.checkbox("Foco no meu concurso", value=True, help="Dentro dos cargos selecionados: FAFIPA OU Prefeitura de Curitiba. Desmarque para buscar em outras bancas e órgãos.")
    fontes = st.multiselect("Fontes", ["PCI", "FAFIPA"], default=["PCI", "FAFIPA"])
    a, b = st.columns(2)
    inicio = a.number_input("Ano inicial", min_value=0, max_value=2100, value=0, step=1)
    fim = b.number_input("Ano final", min_value=0, max_value=2100, value=0, step=1)
    st.caption("Ano 0 = sem limite. O ano do concurso é usado quando não há data da prova.")
    bancas = terms(st.text_input("Bancas", placeholder="FAFIPA; UFPR"))
    orgaos = terms(st.text_input("Órgãos", placeholder="Curitiba; Câmara"))
    municipios = terms(st.text_input("Municípios", placeholder="Curitiba; Paranavaí"))
    ufs = [u.upper() for u in terms(st.text_input("UFs", placeholder="PR; SC"))]
    tipos = st.multiselect("Tipos de documento", KINDS)
    st.caption("Separe valores de texto com ponto e vírgula. Tipos filtram documentos já identificados.")
    st.divider()
    st.caption("Perfil: Analista Administrativo · Prefeitura de Curitiba · FAFIPA · Edital 5/2026")
    with st.expander("Temas do edital"):
        st.write("\n".join("• " + t for t in TOPICS))
        st.caption("Sugestões para organizar o estudo. Cargo semelhante não comprova equivalência de conteúdo.")
        edital = Path(os.environ.get("EDITAL_PATH", "Edital_base.pdf"))
        if edital.is_file():
            st.download_button("Abrir edital de referência", edital.read_bytes(), "Edital_base.pdf", "application/pdf")

filters = Filters(cargos=cargos, bancas=bancas, orgaos=orgaos, municipios=municipios, ufs=ufs, fontes=fontes, tipos=tipos, ano_inicio=inicio or None, ano_fim=fim or None, foco=foco)
valid_years = not (inicio and fim and inicio > fim)

st.caption("ESTUDO COM PROVAS REAIS")
st.title("Seu acervo de concursos")
st.write("Encontre provas, reúna gabaritos e pesquise o material para Analista Administrativo.")
all_records = records()
with connect() as c:
    ready_ids = valid_document_ids(c)
    searchable = c.execute("SELECT count(*) FROM documentos WHERE id IN (%s) AND texto_estado='pesquisável'" % (",".join("?" for _ in ready_ids) or "NULL"), tuple(ready_ids)).fetchone()[0]
cols = st.columns(3)
cols[0].metric("Cargos / concursos catalogados", len(all_records))
cols[1].metric("PDFs íntegros no acervo", len(ready_ids))
cols[2].metric("PDFs pesquisáveis", searchable)


@st.fragment(run_every="3s")
def progress(location):
    jobs = recent_jobs()
    if not jobs:
        return
    latest = jobs[0]
    active = latest["estado"] in ("iniciando", "executando")
    with st.container(border=True):
        label = f"Execução #{latest['id']} · {latest['etapa'] if active else latest['estado']}"
        st.status(label, state="running" if active else "error" if latest["estado"] in ("erro", "parcial", "interrompida") else "complete")
        if latest["arquivo_atual"]:
            st.caption("Em processamento: " + latest["arquivo_atual"])
        if latest["total"] is not None:
            total = latest["total"]
            st.progress(min(latest["processados"] / total, 1.0) if total else 0.0,
                        text=f"{latest['processados']} de {total} arquivos processados")
            counters = st.columns(4)
            for col, title, field in zip(counters, ["PDFs novos", "Reaproveitados", "Importação manual", "Falhas"],
                                         ["novos", "reaproveitados", "bloqueados", "erros"]):
                col.metric(title, latest[field])
        elif latest["processados"]:
            st.caption(f"Páginas/índices processados: {latest['processados']}")
        message = latest["mensagem"] or "Preparando a coleta…"
        if not active and latest["estado"] in ("parcial", "erro", "interrompida"):
            st.warning(message)
        else:
            st.write(message)
        st.caption("Atualização automática a cada 3 segundos. Você pode continuar usando o acervo ou fechar esta aba.")
        if active and st.button("Cancelar coleta", key=f"cancel-{location}-{latest['id']}", disabled=bool(latest["cancelar"])):
            cancel_job(latest["id"])
            st.info("Cancelamento solicitado. Uma requisição em andamento pode levar até 35 segundos.")
        with connect() as c:
            events = [dict(r) for r in c.execute("SELECT momento,nivel,mensagem FROM eventos WHERE execucao_id=? ORDER BY id DESC LIMIT 25", (latest["id"],))]
            blocked = [dict(r) for r in c.execute("""SELECT DISTINCT o.nome,o.url,o.erro FROM origens_documento o
                JOIN eventos e ON instr(e.mensagem,o.url || ':')=1
                WHERE e.execucao_id=? AND e.nivel='pendência' AND o.estado='intervenção necessária' LIMIT 10""", (latest["id"],))]
        if blocked:
            with st.expander("Arquivos pendentes de importação manual"):
                st.write("Abra o documento na fonte, baixe pelo navegador e envie o PDF na aba Importar arquivos.")
                for origin in blocked:
                    st.link_button(origin["nome"], origin["url"])
                    st.caption(origin["erro"])
        if not active:
            if st.button("Atualizar resultados", key=f"refresh-{location}-{latest['id']}"):
                st.rerun()
            if latest["estado"] in ("parcial", "erro", "interrompida", "cancelada"):
                if st.button("Executar novamente com os mesmos filtros", key=f"retry-{location}-{latest['id']}"):
                    start(latest["acao"], json.loads(latest["parametros"]))
        with st.expander("Eventos e execuções anteriores"):
            if events:
                st.dataframe(pd.DataFrame(events), hide_index=True, width="stretch")
            st.dataframe(pd.DataFrame([{k: j[k] for k in ["id", "acao", "estado", "processados", "erros", "criado_em"]} for j in jobs]), hide_index=True, width="stretch")


search_tab, library_tab, import_tab = st.tabs(["Buscar provas", "Meu acervo", "Importar arquivos"])

with search_tab:
    st.subheader("Pesquisar e catalogar")
    st.write("Escolha os filtros à esquerda. A pesquisa consulta as fontes e acrescenta os resultados ao catálogo acumulado.")
    st.caption("PCI: quando houver verificação, abra a origem, baixe no navegador e importe o PDF. FAFIPA: somente documentos públicos; comunicados e cadernos são identificados separadamente.")
    if not valid_years:
        st.error("O ano inicial deve ser menor ou igual ao ano final.")
    a, b = st.columns([1, 2])
    if a.button("Pesquisar e catalogar", type="primary", disabled=not valid_years):
        start("catalogar", {"filtros": filters.to_dict()})
    if b.button("Atualizar índice de cargos do PCI"):
        start("categorias", {})
    if st.button("Atualizar tabela de resultados"):
        st.rerun()
    found = records(filters=filters)
    st.caption(f"Registros correspondentes aos filtros atuais: {len(found)}. Durante a coleta, atualize a tabela para ver novos registros.")
    if found:
        data = pd.DataFrame([{"Selecionar": False, "ID": r["id"], "Ano": r["ano"], "Cargo": r["cargo"], "Órgão": r["orgao"], "Banca": r["banca"], "Fonte": r["fonte"], "Prova": r["prova_situacao"], "Gabarito": r["gabarito_situacao"], "Situação": r["estado"], "PDFs salvos": r["baixados"], "Documentos": r["tipos"] or "a identificar", "Origem": r["url"]} for r in found])
        edited = st.data_editor(data, hide_index=True, disabled=[col for col in data.columns if col != "Selecionar"], column_config={"Origem": st.column_config.LinkColumn("Origem")}, width="stretch", key="search-results")
        selected = edited.loc[edited["Selecionar"], "ID"].tolist()
        a, b = st.columns(2)
        if a.button("Baixar selecionados", disabled=not selected):
            start("download", {"registros": selected, "filtros": filters.to_dict()})
        if b.button(f"Baixar todos os {len(found)} resultados filtrados"):
            start("download", {"registros": [r["id"] for r in found], "filtros": filters.to_dict()})
    else:
        st.info("Nenhum resultado com esses filtros ainda. Inicie uma pesquisa ou amplie os cargos e desmarque o foco no concurso.")
    progress("busca")

with library_tab:
    st.subheader("Consultar o acervo")
    apply_filters = st.checkbox("Aplicar os filtros da busca ao acervo", value=False)
    query = st.text_input("Pesquisar no texto dos PDFs", placeholder="licitações, gestão de projetos, orçamento…")
    st.caption("A pesquisa exige todos os termos, ignora acentos e usa somente o texto extraído dos PDFs. Digitalizações sem texto permanecem disponíveis por metadados.")
    view = st.radio("Visão do acervo", ["Disponíveis no acervo", "Catalogados e pendentes"], horizontal=True, index=0)
    view_key = "available" if view == "Disponíveis no acervo" else "pending"
    groups = catalog_groups(filters=filters if apply_filters else None, query=query, view=view_key)
    st.download_button("Exportar esta visão em CSV", export_catalog_csv(filters=filters if apply_filters else None, query=query, view=view_key), "acervo.csv", "text/csv")
    count_docs = len({d["id"] for g in groups for d in (g["available_docs"] if view_key == "available" else g["pending_docs"])})
    st.caption(f"{len(groups)} concursos · {sum(len(g['records']) for g in groups)} cargos · {count_docs} documentos nesta visão. Arquivos compartilhados são contados uma vez por concurso.")
    if groups:
        selected_contest = st.selectbox("Concurso", range(len(groups)), format_func=lambda i: " · ".join(x for x in [groups[i]['orgao'], str(groups[i]['ano'] or 'Ano não informado'), groups[i]['banca'], groups[i]['municipio'], groups[i]['uf'], ("Edital " + groups[i]['edital']) if groups[i]['edital'] else ""] if x))
        group = groups[selected_contest]
        records_here = group["records"]
        if group["url"].startswith("https://"):
            st.link_button("Abrir página do concurso", group["url"])
        st.markdown("**Cargos** · " + " · ".join(r["cargo"] for r in records_here))
        docs = group["available_docs"] if view_key == "available" else group["pending_docs"]
        saved_types = {o["tipo"] for d in group["available_docs"] for o in d["origens"]}
        if "prova" in saved_types:
            situation = "Prova disponível" + (" e gabarito disponível" if any(t.startswith("gabarito") for t in saved_types) else "")
        elif any(t.startswith("gabarito") for t in saved_types):
            situation = "Somente gabarito disponível"
        elif "comunicado" in saved_types:
            situation = "Apenas comunicado disponível"
        else:
            situation = "Materiais catalogados, sem PDF íntegro no acervo"
        st.info(situation)
        if view_key == "pending":
            retryable = [d for d in docs if any(o["estado"] != "intervenção necessária" for o in d["origens"] if not o["url"].startswith("importacao:"))]
            if retryable and st.button("Tentar downloads pendentes", type="primary"):
                start("download", {"registros": [r["id"] for r in records_here], "documentos": [d["id"] for d in retryable], "somente_pendentes": True})
        progress("acervo")
        if not docs:
            st.info("Nenhum documento corresponde a esta visão e aos filtros atuais.")
        for doc in docs:
            cargo_names = [r["cargo"] for r in records_here if r["id"] in doc["registros_visiveis"]]
            with st.expander(f"{doc['tipos']} · {doc['nome']} · {', '.join(cargo_names) or 'Cargo geral'}", expanded=bool(query.strip())):
                st.caption("Cargos: " + (", ".join(cargo_names) or "material compartilhado/geral"))
                if doc["available"]:
                    file_detail(doc, f"lib-{doc['id']}-{group['id']}")
                else:
                    if doc["caminho"]:
                        st.warning("O PDF associado está ausente ou corrompido; ele não conta como disponível até ser reparado.")
                    for origin in doc["origens"]:
                        st.write(f"**{origin['nome'] or doc['nome']}** · {origin['tipo']} · {origin['estado']}")
                        if origin["erro"]:
                            st.warning(origin["erro"])
                        if origin["estado"] == "intervenção necessária" and origin["url"].startswith("https://"):
                            st.link_button("Resolver pendência na fonte", origin["url"])
                        elif origin["url"].startswith("https://"):
                            st.link_button("Abrir documento na fonte", origin["url"])
                    manual_origins = [o for o in doc["origens"] if o["estado"] == "intervenção necessária" or o["url"].startswith("importacao:")]
                    if manual_origins:
                        upload = st.file_uploader("PDF baixado na fonte ou reparo do arquivo local", type=["pdf"], key=f"resolve-upload-{doc['id']}")
                        if st.button("Resolver pendência com este PDF", disabled=upload is None, key=f"resolve-save-{doc['id']}"):
                            source = next((o for o in manual_origins if o["url"].startswith("https://")), manual_origins[0])
                            try:
                                store_pdf([upload.getvalue()], doc["registros_visiveis"], source["url"], source["tipo"], source["nome"] or upload.name, root_dir())
                                st.success("PDF validado e vinculado ao concurso.")
                            except Exception as e:
                                st.error("Não foi possível importar este PDF: " + str(e))
                with st.expander("Organizar: etiquetas e vínculos"):
                    for record in records_here:
                        tags = st.multiselect(f"Etiquetas · {record['cargo']}", sorted(set(TOPICS + json.loads(record["etiquetas"]))), default=json.loads(record["etiquetas"]), accept_new_options=True, key=f"tags-{record['id']}-{doc['id']}")
                        if st.button("Salvar etiquetas", key=f"tag-save-{record['id']}-{doc['id']}"):
                            with connect() as c:
                                c.execute("UPDATE registros SET etiquetas=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), record["id"]))
                    linked = st.multiselect("Vincular também a outros cargos", [r["id"] for r in all_records if r["id"] not in {x['id'] for x in records_here}], format_func=lambda i: record_label(next(r for r in all_records if r["id"] == i)), key=f"link-{doc['id']}-{group['id']}")
                    if st.button("Salvar vínculos", disabled=not linked, key=f"link-save-{doc['id']}-{group['id']}"):
                        with connect() as c:
                            c.executemany("INSERT OR IGNORE INTO documento_registro VALUES(?,?)", [(doc["id"], i) for i in linked])
                        st.success("Vínculos salvos.")
    else:
        st.info("Nenhum concurso nesta visão. Ajuste a pesquisa/filtros ou use a outra visão.")

with import_tab:
    st.subheader("Importar PDFs")
    st.write("Após baixar um arquivo no navegador, envie-o aqui. O mesmo documento pode atender a vários cargos.")
    mode = st.radio("Vincular a", ["Registros existentes", "Novo concurso / cargo"], horizontal=True)
    with st.form("import-form", clear_on_submit=False):
        targets = []
        if mode == "Registros existentes":
            options = {r["id"]: r for r in all_records}
            targets = st.multiselect("Selecione os cargos/concursos", list(options), format_func=lambda i: record_label(options[i]))
            new_roles = new_org = new_bank = new_url = new_city = new_uf = ""
            new_year = 0
        else:
            new_roles = st.text_input("Cargo(s), separados por ponto e vírgula", value="Analista Administrativo")
            new_org = st.text_input("Órgão público", value="Prefeitura de Curitiba")
            new_bank = st.text_input("Banca", value="FAFIPA")
            new_year = st.number_input("Ano do concurso (0 se desconhecido)", min_value=0, max_value=2100, value=2026)
            new_city = st.text_input("Município", value="Curitiba")
            new_uf = st.text_input("UF", value="PR", max_chars=2)
            new_url = st.text_input("Página de origem (opcional)", placeholder="https://…")
        kind = st.selectbox("Tipo dos arquivos desta importação", KINDS)
        uploads = st.file_uploader("Arquivos PDF (até 200 MB cada)", type=["pdf"], accept_multiple_files=True)
        st.caption("Para tipos diferentes, faça importações separadas. Arquivos repetidos são reconhecidos pelo conteúdo.")
        submitted = st.form_submit_button("Importar e indexar", type="primary")
    if submitted:
        if not uploads:
            st.error("Selecione pelo menos um PDF.")
        elif mode == "Registros existentes" and not targets:
            st.error("Selecione pelo menos um cargo/concurso.")
        elif mode != "Registros existentes" and (not terms(new_roles) or not new_org.strip()):
            st.error("Informe cargo e órgão público.")
        elif new_url and not new_url.startswith("https://"):
            st.error("A página de origem deve começar com https://.")
        else:
            if mode != "Registros existentes":
                url = new_url.strip() or "manual:" + uuid4().hex
                with connect() as c:
                    targets = [upsert_record(c, fonte="Manual", url=url, cargo=role, orgao=new_org.strip(), banca=new_bank.strip(), ano=new_year or None, municipio=new_city.strip(), uf=new_uf.upper()) for role in terms(new_roles)]
            for upload in uploads:
                try:
                    with st.spinner("Importando e extraindo texto de " + upload.name):
                        doc_id = import_pdf(upload.getvalue(), targets, kind, upload.name)
                    st.success(f"{upload.name}: importado e vinculado (documento #{doc_id}).")
                except Exception as e:
                    st.error(f"{upload.name}: {e}")
            (root_dir() / "catalogo.csv").write_bytes(export_csv())
            st.info("Abra Meu acervo para consultar. Use a atualização da página para atualizar os contadores.")
