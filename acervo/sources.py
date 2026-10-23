from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .model import location, norm, year

PCI = "https://www.pciconcursos.com.br"
FAFIPA = "https://fafipa.selecao.net.br"
QCONCURSOS = "https://www.qconcursos.com"
QCONCURSOS_PROVAS = QCONCURSOS + "/questoes-de-concursos/provas"

QCONCURSOS_FACET_FIELDS = {
    "by_examining_board": "bancas",
    "by_institution": "orgaos",
    "by_job": "cargos",
    "application_year": "anos_aplicacao",
    # Older cached/form variants used by_year; normalize it to the live query key.
    "by_year": "anos_aplicacao",
    "by_scholarity": "escolaridades",
    "by_acting_area": "areas_atuacao",
    "by_formation_area": "areas_formacao",
}


def pagination(soup, url):
    """Follow only real pagination links in the same index (never detail links)."""
    current = urlparse(url)
    if "pciconcursos" in current.netloc:
        base_path = re.sub(r"/\d+/?$", "", current.path).rstrip("/")
        pattern = re.compile(re.escape(base_path) + r"/\d+/?$")
        return sorted({urljoin(url, a["href"]) for a in soup.select("a[href]") if pattern.fullmatch(urlparse(urljoin(url, a["href"])).path) and urlparse(urljoin(url, a["href"])).netloc == current.netloc})
    if current.netloc == urlparse(QCONCURSOS).netloc and current.path.rstrip("/") == "/questoes-de-concursos/provas":
        links = set()
        for a in soup.select('a[rel="next"][href], a[href]'):
            target = urljoin(url, a.get("href", ""))
            parsed = urlparse(target)
            if parsed.netloc != current.netloc or parsed.path.rstrip("/") != current.path.rstrip("/"):
                continue
            if any(key == "page" for key, _ in parse_qsl(parsed.query)):
                links.add(target)
        return sorted(links)
    return sorted({urljoin(url, a["href"]) for a in soup.select("a[href]") if urlparse(urljoin(url, a["href"])).netloc == current.netloc and "pg=" in a["href"] and "situacao=" in a["href"] and urlparse(urljoin(url, a["href"])).path in ("", "/")})


def qconcursos_facets(soup):
    """Extract available filter label/id pairs from the public proofs filter form."""
    result = []
    for item in soup.select('input[name][value]'):
        name = item.get("name", "").removesuffix("[]")
        if name not in QCONCURSOS_FACET_FIELDS:
            continue
        if name == "by_year":
            name = "application_year"
        label = ""
        if item.get("id"):
            label_node = soup.find("label", attrs={"for": item["id"]})
            if label_node:
                label = label_node.get_text(" ", strip=True)
        if not label:
            parent = item.find_parent("label") or item.parent
            label = parent.get_text(" ", strip=True) if parent else ""
            label = label.replace(item.get("value", ""), " ").strip()
        value = item.get("value", "").strip()
        if value and label:
            result.append({"campo": name, "id": value, "nome": label})
    for select in soup.select("select[name]"):
        name = select.get("name", "").removesuffix("[]")
        if name not in QCONCURSOS_FACET_FIELDS:
            continue
        if name == "by_year":
            name = "application_year"
        for option in select.select("option[value]"):
            value = option.get("value", "").strip()
            label = option.get_text(" ", strip=True)
            if value and label and value != "0":
                result.append({"campo": name, "id": value, "nome": label})
    return list({(x["campo"], x["id"]): x for x in result}.values())


def qconcursos_search_url(filters, facet_rows=(), page=1):
    """Build Qconcursos' public filter URL from labels discovered on its own form."""
    facet_index = {}
    for item in facet_rows:
        facet_index.setdefault(item["campo"], {})[norm(item["nome"])] = item["id"]
    selected = {
        "by_examining_board": filters.bancas,
        "by_institution": filters.orgaos,
        "by_job": filters.cargos,
        "by_scholarity": filters.escolaridades,
        "by_acting_area": filters.areas_atuacao,
        "by_formation_area": filters.areas_formacao,
    }
    params = []
    for field, labels in selected.items():
        index = facet_index.get(field, {})
        for label in labels:
            value = index.get(norm(label))
            if value:
                params.append((field + "[]", value))
    selected_years = {year(value) for value in filters.anos_aplicacao}
    if filters.ano_inicio or filters.ano_fim:
        selected_years.update(
            parsed_year for label in facet_index.get("application_year", {})
            if (parsed_year := year(label)) is not None
            and (filters.ano_inicio is None or parsed_year >= filters.ano_inicio)
            and (filters.ano_fim is None or parsed_year <= filters.ano_fim)
        )
    if selected_years:
        year_index = facet_index.get("application_year", {}) or facet_index.get("by_year", {})
        for label, value in year_index.items():
            parsed_year = year(label)
            if parsed_year in selected_years:
                params.append(("application_year[]", value))
    if page > 1:
        params.append(("page", str(page)))
    return QCONCURSOS_PROVAS + (("?" + urlencode(params)) if params else "")


def _q_file_kind(label):
    text = norm(label)
    if "gabarito" in text:
        return "gabarito"
    if "alterac" in text or "justificativa" in text:
        return "alterações"
    if "edital" in text or "regulamento" in text:
        return "edital"
    if "prova" in text or "caderno" in text:
        return "prova"
    return None


def qconcursos_rows(soup, base_url=QCONCURSOS_PROVAS):
    """Parse proof cards and their directly linked PDFs from a Qconcursos index page."""
    rows = []
    for link in soup.select('a[href*="/questoes-de-concursos/provas/"]'):
        href = urljoin(base_url, link.get("href", ""))
        parsed = urlparse(href)
        if parsed.netloc != urlparse(QCONCURSOS).netloc or not re.fullmatch(r"/questoes-de-concursos/provas/[a-z0-9-]+/?", parsed.path):
            continue
        title = link.get_text(" ", strip=True)
        if not title:
            continue
        card = link
        for _ in range(7):
            parent = card.parent
            if parent is None:
                break
            card = parent
            if card.select('a[href*="arquivos.qconcursos.com/"]'):
                break
        title = re.sub(r"^Prova(?:\s+de|\s+para)?\s*", "", title, flags=re.I).strip()
        bits = [part.strip() for part in title.split(" - ") if part.strip()]
        if len(bits) < 4:
            raise ValueError("Título de prova Qconcursos não reconhecido: " + title)
        bank, exam_year = bits[0], bits[1]
        cargo = bits[-1]
        orgao = " - ".join(bits[2:-1])
        city, uf = location(orgao)
        card_text = card.get_text(" ", strip=True)
        level = re.search(r"Nível\s+(Fundamental(?:\s+(?:incompleto|completo))?|Médio(?:\s+técnico)?|Superior)", card_text, re.I)
        docs = []
        for file_link in card.select('a[href*="arquivos.qconcursos.com/"]'):
            file_url = urljoin(base_url, file_link.get("href", ""))
            fp = urlparse(file_url)
            label = file_link.get_text(" ", strip=True) or file_link.get("aria-label", "") or file_link.get("title", "")
            kind = _q_file_kind(label)
            if fp.scheme != "https" or fp.hostname != "arquivos.qconcursos.com" or not fp.path.lower().endswith(".pdf") or not kind:
                continue
            docs.append({"url": file_url, "tipo": kind, "nome": label or fp.path.rsplit("/", 1)[-1]})
        rows.append({
            "fonte": "QCONCURSOS", "url": href, "cargo": cargo, "ano": int(exam_year) if exam_year.isdigit() else None,
            "orgao": orgao, "banca": bank, "municipio": city, "uf": uf, "categoria": "",
            "escolaridade": level.group(1).strip() if level else "", "documentos": list({d["url"]: d for d in docs}.values()),
        })
    return list({row["url"]: row for row in rows}.values())


def pci_categories(soup):
    result = {}
    for a in soup.select("a[href]"):
        url = urljoin(PCI, a["href"])
        if urlparse(url).netloc != "www.pciconcursos.com.br":
            continue
        if re.fullmatch(r"/provas/[a-z][a-z0-9-]*/?", urlparse(url).path):
            label = a.get_text(" ", strip=True)
            if label:
                result[url.rstrip("/")] = label
    return result


def pci_rows(soup, category=""):
    table = next((t for t in soup.select("table") if all(w in norm(t.get_text(" ", strip=True)) for w in ["orgao", "organizadora"])), None)
    if table is None:
        raise ValueError("Tabela de provas não encontrada; o layout pode ter mudado.")
    rows = []
    for tr in table.select("tr"):
        cols = tr.find_all("td", recursive=False)
        if len(cols) < 4:
            continue
        a = cols[0].find("a", href=True)
        if not a:
            continue
        url = urljoin(PCI, a["href"])
        if urlparse(url).netloc != "www.pciconcursos.com.br" or not urlparse(url).path.startswith("/provas/download/"):
            continue
        org = cols[2].get_text(" ", strip=True)
        city, uf = location(org)
        rows.append(dict(fonte="PCI", url=url, cargo=a.get_text(" ", strip=True), ano=year(cols[1].get_text()), orgao=org, banca=cols[3].get_text(" ", strip=True), municipio=city, uf=uf, categoria=category))
    return rows


def kind_for(label):
    text = norm(label)
    # An annex can contain the answers even when its title says "Divulgação".
    answer_annex = text.startswith("anexo") and "gabarito" in text
    announcement = "divulgacao" in text and "gabarito" in text and any(t in text for t in ("prazo para recurso", "caderno de quest"))
    if not answer_annex and (announcement or any(t in text for t in ["divulgacao", "prazo para recurso", "convocacao", "resultado"])):
        return "comunicado"
    if "gabarito" in text:
        if "definitiv" in text or "oficial" in text:
            return "gabarito definitivo"
        return "gabarito preliminar" if "preliminar" in text else "gabarito"
    if "caderno" in text or "questoes" in text or ("prova" in text and "local" not in text):
        return "prova"
    if "edital" in text or "regulamento" in text:
        return "edital"
    return "comunicado"


def pdf_links(soup, base_url):
    """Group sibling view/download actions by explicit filename/context, never guess hashes."""
    selected = {}
    for a in soup.select("a[href]"):
        action = norm(a.get("data-acao", ""))
        label = a.get_text(" ", strip=True) or a.get("title", "")
        if action == "compartilhar" or norm(label).startswith("compartilhar"):
            continue
        url = urljoin(base_url, a["href"])
        if urlparse(url).scheme != "https" or not urlparse(url).path.lower().endswith(".pdf"):
            continue
        name = a.get("data-arquivo") or urlparse(url).path.rsplit("/", 1)[-1]
        parent = a.find_parent(["li", "tr"])
        if parent is None:
            parent = a.parent
        context = parent.get_text(" ", strip=True) if parent else label
        kind = kind_for(label + " " + name.replace("_", " "))
        if kind == "comunicado" and "pciconcursos" in urlparse(base_url).netloc:
            kind = "gabarito" if "gabarito" in norm(context + name) else "prova"
        explicit_action = action in ("ver", "baixar") or norm(label).startswith(("ver ", "baixar "))
        key = (name.lower(), kind, "acoes") if explicit_action else (url, kind)
        priority = 2 if action == "baixar" or norm(label).startswith("baixar") or a.has_attr("download") else 1
        if key not in selected or priority > selected[key][0]:
            selected[key] = (priority, dict(url=url, tipo=kind, nome=label or name))
    # Exact repeated links are redundant even if repeated outside the file card.
    return list({v[1]["url"]: v[1] for v in selected.values()}.values())


def pci_detail(soup, url):
    docs = pdf_links(soup, url)
    if docs:
        return "disponível", docs
    security_text = norm(soup.get_text(" "))
    if soup.select(".cf-turnstile, #captcha-provas, .g-recaptcha, [data-sitekey]") or any(t in security_text for t in ("verificacao de seguranca", "confirme que voce e humano", "captcha", "cloudflare turnstile")):
        return "verificação necessária", []
    text = norm(soup.get_text(" ", strip=True))
    if any(s in text for s in ["area do candidato", "acesso restrito", "faca login"]):
        return "acesso restrito", []
    if soup.select_one(".prova-pdf-link[data-arquivo]"):
        raise ValueError("Link dinâmico de PDF sem URL reconhecível; revisão do layout necessária.")
    if not any(s in text for s in ["provas para download", "cargo:", "download dos arquivos"]):
        raise ValueError("Página de detalhes não reconhecida; possível mudança de layout.")
    return "sem arquivos", []


def fafipa_contests(soup):
    return sorted({urljoin(FAFIPA, a["href"]) for a in soup.select("a[href]") if urlparse(urljoin(FAFIPA, a["href"])).netloc == urlparse(FAFIPA).netloc and re.fullmatch(r"/(?:informacoes/\d+/?|concurso/\d+(?:-[^/?#]+)?/?)", urlparse(urljoin(FAFIPA, a["href"])).path)})


def fafipa_detail(soup, url):
    h1 = soup.find("h1")
    if h1 is None:
        raise ValueError("Concurso FAFIPA sem título; possível mudança de layout.")
    title = h1.get_text(" ", strip=True)
    text = soup.get_text(" ", strip=True)
    # Locate the actual cargos panel, not links elsewhere on the page.
    roles = []
    panel = soup.select_one('div[data-aba="cargos"], section[data-aba="cargos"]')
    if panel:
        roles = [el.get_text(" ", strip=True) for el in panel.select(".det-row > span:first-child")]
    heading = soup.find(lambda t: t.name in ("h2", "h3") and "cargos e vagas" in norm(t.get_text(" ", strip=True)))
    if heading and not roles:
        for el in heading.next_elements:
            if getattr(el, "name", None) in ("h2", "h3"):
                break
            if getattr(el, "name", None) in ("li", "tr"):
                label = el.get_text(" ", strip=True)
                role = re.split(r"\s+(?:\d+\s+vagas?\b|cadastro de reserva\b)", label, flags=re.I)[0].strip()
                if role and norm(role) not in ("cargo", "cargo vagas"):
                    roles.append(role)
    if not roles:
        # Older versions use tables, cells or strong labels in a dedicated panel.
        panel = soup.select_one("#cargos, #cargos-vagas, [data-panel='cargos']")
        if panel:
            for item in panel.select("tbody tr, li"):
                cell = item.find("td") if item.name == "tr" else item
                if cell:
                    role = re.split(r"\s+(?:\d+\s+vagas?\b|cadastro de reserva\b)", cell.get_text(" ", strip=True), flags=re.I)[0]
                    roles.append(role.strip())
    if not roles:
        raise ValueError("Lista de cargos não reconhecida; revisão manual necessária.")
    org = re.sub(r"(?i)^(?:concurso público|processo seletivo(?: simplificado| público)?)\s+(?:do|da|de)\s+", "", title)
    city, uf = location(org)
    edital = re.search(r"Edital\s*(?:n[º°.o]*\s*)?([\d.]+/\d{4})", text, re.I)
    data = dict(fonte="FAFIPA", url=url, orgao=org, banca="FAFIPA", ano=int(edital[1].rsplit("/", 1)[1]) if edital else None, edital=edital[1] if edital else "", municipio=city, uf=uf)
    docs = [d for d in pdf_links(soup, url) if d["tipo"] != "comunicado" or any(t in norm(d["nome"]) for t in ("gabarito", "caderno de quest", "cadernos de quest"))]
    return [dict(data, cargo=role, categoria="") for role in dict.fromkeys(roles)], docs
