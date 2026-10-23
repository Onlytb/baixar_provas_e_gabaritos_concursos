from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

DEFAULT_ROLES = ["Analista Administrativo", "Administrador", "Gestão Pública"]
EXTRA_ROLES = ["Assistente Administrativo", "Agente Administrativo", "Auxiliar Administrativo", "Técnico Administrativo"]
KINDS = ["prova", "gabarito preliminar", "gabarito definitivo", "gabarito", "alterações", "edital", "comunicado"]
TOPICS = ["Língua Portuguesa", "Informática", "Legislação Municipal de Curitiba", "Estatística e Raciocínio Lógico", "Tecnologia e Inteligência Artificial", "Administração Pública e Gestão Estratégica", "Gestão de Projetos", "Gestão de Processos", "Gestão de Pessoas", "Materiais, Logística e Patrimônio", "Orçamento Público", "Governança, Riscos e Controles", "Comunicação Organizacional", "Administração Digital, LGPD e LAI", "Licitações e Contratos"]


def norm(value):
    return re.sub(r"\s+", " ", "".join(c for c in unicodedata.normalize("NFKD", str(value or "")) if not unicodedata.combining(c))).strip().lower()


def slug(value):
    return re.sub(r"[^a-z0-9]+", "-", norm(value)).strip("-") or "arquivo"


def terms(value):
    return [v.strip() for v in value.split(";") if v.strip()]


def role_matches(term, role):
    term, role = norm(term), norm(role)
    if term == "gestao publica" and re.search(r"\b(assistente|agente|auxiliar|tecnico)\b", role):
        return False
    return term in role


def year(value):
    match = re.search(r"\b(?:19|20)\d{2}\b", str(value or ""))
    return int(match[0]) if match else None


def location(orgao):
    match = re.search(r"[/\-]\s*([A-Z]{2})\b", orgao)
    uf = match[1] if match else ""
    city = re.sub(r"[/\-]\s*[A-Z]{2}\b.*", "", orgao)
    city = re.sub(r"(?i)^.*?(?:pref(?:eitura|\.)?|município|municipio|câmara(?: municipal)?|camara(?: municipal)?)\s*(?:de|do|da)?\s+", "", city).strip()
    return city, uf


@dataclass
class Filters:
    cargos: list[str] = field(default_factory=list)
    bancas: list[str] = field(default_factory=list)
    orgaos: list[str] = field(default_factory=list)
    municipios: list[str] = field(default_factory=list)
    ufs: list[str] = field(default_factory=list)
    fontes: list[str] = field(default_factory=list)
    tipos: list[str] = field(default_factory=list)
    escolaridades: list[str] = field(default_factory=list)
    areas_atuacao: list[str] = field(default_factory=list)
    areas_formacao: list[str] = field(default_factory=list)
    anos_aplicacao: list[str] = field(default_factory=list)
    ano_inicio: int | None = None
    ano_fim: int | None = None
    foco: bool = False

    def matches(self, row, check_role=True):
        if check_role and self.cargos and not any(role_matches(v, row.get("cargo", "")) for v in self.cargos):
            return False
        for values, key in [(self.bancas, "banca"), (self.orgaos, "orgao"), (self.municipios, "municipio"), (self.fontes, "fonte"), (self.escolaridades, "escolaridade"), (self.areas_atuacao, "areas_atuacao"), (self.areas_formacao, "areas_formacao")]:
            if key in {"escolaridade", "areas_atuacao", "areas_formacao"} and row.get("fonte") not in (None, "QCONCURSOS"):
                continue
            if values and not any(norm(v) in norm(row.get(key, "")) for v in values):
                return False
        if self.ufs and norm(row.get("uf")) not in [norm(v) for v in self.ufs]:
            return False
        y = year(row.get("ano"))
        if (self.ano_inicio or self.ano_fim) and y is None:
            return False
        if y and ((self.ano_inicio and y < self.ano_inicio) or (self.ano_fim and y > self.ano_fim)):
            return False
        if self.anos_aplicacao:
            selected_years = {year(value) for value in self.anos_aplicacao}
            if y not in selected_years:
                return False
        if self.foco:
            org = norm(row.get("orgao"))
            if "fafipa" not in norm(row.get("banca")) and not ("curitiba" in org and re.search(r"\bpref(?:eitura)?\b|\bmunicipio\b", org)):
                return False
        return True

    def to_dict(self):
        return asdict(self)
