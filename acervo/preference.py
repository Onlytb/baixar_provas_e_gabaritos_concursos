"""Answer-key preference is evaluated per record, before any user filters."""
from __future__ import annotations

import re
from collections import defaultdict

from .model import norm


def exam_scope(name):
    text = norm(name)
    patterns = {
        "etapa": r"\b(?:etapa|fase)\s*([\divx]+)[ao°]*\b|\b([\divx]+)[ao°]*\s*(?:etapa|fase)\b",
        "turno": r"\b(manha|tarde|noite|matutino|vespertino|noturno)\b",
        "versao": r"\b(?:tipo|versao|caderno|prova)\s*(?:n[.º°o]*\s*)?([a-d]|\d+|azul|amarel[oa]|branc[oa]|rosa|verde)\b",
        "cor": r"\b(azul|amarel[oa]|branc[oa]|rosa|verde)\b",
        "modalidade": r"\b(objetiva|discursiva|pratica)\b",
        "nivel": r"\bnivel\s*(fundamental(?:\s+(?:incompleto|completo))?|medio(?:\s+tecnico)?|superior|tecnico)\b",
        "aplicacao": r"\b(reaplicacao|reaplicada|segunda aplicacao)\b",
        "data": r"(?:aplicada|aplicacao|realizada|prova)\s*(?:em|no dia|do dia|de)?\s*(\d{2}/\d{2}/\d{4})",
    }
    result = []
    aliases = {"matutino": "manha", "vespertino": "tarde", "noturno": "noite",
               "reaplicada": "reaplicacao", "segunda aplicacao": "reaplicacao",
               "i": "1", "ii": "2", "iii": "3", "iv": "4"}
    for key, pattern in patterns.items():
        matches = []
        for match in re.finditer(pattern, text):
            value = next(v for v in match.groups() if v)
            matches.append(aliases.get(value, value))
        result.append((key, tuple(sorted(set(matches)))))
    return tuple(result)


def visible_origin_links(c, record_ids=None):
    """Return origins with their visible record links; never delete provenance.

    An unqualified key applies to its linked cargos. Explicit exam qualifiers
    must agree, including their absence, so ambiguous matches remain visible.
    """
    links = [dict(r) for r in c.execute("""SELECT o.*,dr.registro_id
        FROM origens_documento o JOIN documento_registro dr ON dr.documento_id=o.documento_id""")]
    finals = defaultdict(set)
    for origin in links:
        if origin["tipo"] == "gabarito definitivo":
            finals[origin["registro_id"]].add(exam_scope(origin["nome"]))
    targets = None if record_ids is None else set(record_ids)
    return [o for o in links if (targets is None or o["registro_id"] in targets)
            and not (o["tipo"] == "gabarito preliminar"
                     and exam_scope(o["nome"]) in finals[o["registro_id"]])]
