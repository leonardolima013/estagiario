"""Escolha do registro vencedor de um merge (regra definida pelo time de dados,
não documentada na SPEC original): fica o registro mais completo (mais campos
de conteúdo preenchidos); em empate, o mais antigo — ele recebe as informações
que falta via a arbitragem de campo (Fase 2).
"""

from __future__ import annotations

from datetime import datetime

from tools.group_fetch import RegistroCatalogPart

_CAMPOS_DE_COMPLETUDE = (
    "width", "depth", "height", "gross_weight", "net_weight", "ncm", "barcode", "application",
)


def _completude(registro: RegistroCatalogPart) -> int:
    return sum(1 for campo in _CAMPOS_DE_COMPLETUDE if getattr(registro, campo) not in (None, ""))


def escolher_vencedor(registros: list[RegistroCatalogPart]) -> RegistroCatalogPart:
    """Mais completo vence; empate de completude -> o mais antigo (created) vence."""
    if not registros:
        raise ValueError("nenhum registro pra escolher vencedor")

    return min(
        registros,
        key=lambda r: (-_completude(r), r.created or datetime.min),
    )
