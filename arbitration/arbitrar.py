"""arbitrar_campo (SPEC.md §6.1, §4.2) — dispatcher único que decide qual estratégia
de arbitragem usar conforme o campo, dentro de um subcluster já rotulado
duplicata_real pela Fase 1 (particionar_grupo).
"""

from __future__ import annotations

from typing import Callable

from arbitration.application import arbitrar_application
from arbitration.campo_numerico import arbitrar_campo_numerico
from arbitration.datas import arbitrar_data_de_application
from arbitration.models import DecisaoCampo
from arbitration.nome import arbitrar_nome
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.tracing import TraceSink
from memory.models import PedidoIntervencao, RespostaIntervencao
from tools.group_fetch import RegistroCatalogPart
from verification.mcp_playwright_agent import verificar_nomenclatura_peca

_CAMPOS_NUMERICOS = frozenset({"width", "depth", "height", "net_weight", "gross_weight", "ncm", "barcode"})
_CAMPOS_DATA_DE_APPLICATION = frozenset({"born_at", "deprecated_at"})

# Todo campo que arbitrar_campo sabe tratar — exportado pra quem precisa iterar
# todos os campos de um merge (Fase 3) sem duplicar essa lista.
CAMPOS_SUPORTADOS = _CAMPOS_NUMERICOS | _CAMPOS_DATA_DE_APPLICATION | {"application", "name"}


def arbitrar_campo(
    registros: list[RegistroCatalogPart],
    campo: str,
    llm: LLMProvider,
    rule_store: RuleStore | None,
    brand_id: int,
    threshold_divergencia: float = 0.15,
    on_aviso: Callable[[str], None] | None = None,
    verificar_web: Callable = verificar_nomenclatura_peca,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
) -> DecisaoCampo:
    if not registros:
        raise ValueError("subcluster vazio")

    if len(registros) == 1:
        return DecisaoCampo(
            campo=campo, valor=getattr(registros[0], campo), justificativa="Subcluster com um único registro.",
            fonte="sem_conflito",
        )

    if campo == "application":
        return arbitrar_application(registros, brand_id)

    if campo == "name":
        return arbitrar_nome(
            registros, llm, rule_store, on_aviso=on_aviso, verificar_web=verificar_web,
            pedir_intervencao=pedir_intervencao, limiar_intervencao=limiar_intervencao,
            trace=trace,
        )

    if campo in _CAMPOS_DATA_DE_APPLICATION:
        return arbitrar_data_de_application(registros, campo, llm, rule_store, brand_id, threshold_divergencia)

    if campo in _CAMPOS_NUMERICOS:
        return arbitrar_campo_numerico(registros, campo, llm, rule_store, brand_id, threshold_divergencia)

    raise ValueError(f"Campo sem estratégia de arbitragem definida: {campo!r}")
