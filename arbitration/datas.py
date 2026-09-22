"""born_at/deprecated_at NÃO são arbitrados entre candidatos — são derivados
deterministicamente do campo application já unificado (regra do time de dados,
não documentada na SPEC original): born_at é o menor ano mencionado na aplicação,
deprecated_at é o maior.

Isso substitui, pra esses dois campos, a cascata de confiabilidade/modelo de
`arbitration.campo_numerico` — maximiza determinismo (zero custo de LLM no caso
comum) numa base de centenas de milhares de registros. A cascata antiga só entra
como último recurso, se a application unificada não tiver nenhum ano reconhecível
(raro, mas acontece).
"""

from __future__ import annotations

import re

from arbitration.application import arbitrar_application
from arbitration.campo_numerico import arbitrar_campo_numerico
from arbitration.models import DecisaoCampo
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from tools.group_fetch import RegistroCatalogPart

_ANO_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")


def extrair_anos(texto: str | None) -> list[int]:
    """Extrai anos de 4 dígitos (1900-2099) de um texto de application.

    Deliberadamente conservador: não tenta interpretar anos de 2 dígitos
    (ex: '09/13', '01/08') — esses formatos são ambíguos demais (mês? ano
    abreviado? intervalo?) pra extrair com segurança sem virar um parser de
    texto livre — melhor não achar um ano do que achar um errado.
    """
    if not texto:
        return []
    return [int(ano) for ano in _ANO_RE.findall(texto)]


def arbitrar_data_de_application(
    registros: list[RegistroCatalogPart],
    campo: str,
    llm: LLMProvider,
    rule_store: RuleStore | None,
    brand_id: int,
    threshold_divergencia: float = 0.15,
) -> DecisaoCampo:
    if campo not in ("born_at", "deprecated_at"):
        raise ValueError(f"campo não é derivado de application: {campo!r}")

    decisao_application = arbitrar_application(registros, brand_id)
    anos = extrair_anos(decisao_application.valor)

    if anos:
        valor = min(anos) if campo == "born_at" else max(anos)
        return DecisaoCampo(
            campo=campo,
            valor=valor,
            justificativa=(
                f"Derivado do campo application unificado — "
                f"{'menor' if campo == 'born_at' else 'maior'} ano encontrado "
                f"entre {sorted(set(anos))}."
            ),
            fonte="normalizacao",
        )

    # Sem nenhum ano reconhecível na application (raro) — último recurso,
    # cai pra cascata de confiabilidade/modelo como qualquer campo numérico.
    return arbitrar_campo_numerico(registros, campo, llm, rule_store, brand_id, threshold_divergencia)
