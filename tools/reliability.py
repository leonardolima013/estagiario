"""Lookup de confiabilidade por campo (SPEC.md §4.2, passo 1 da cascata de arbitragem).

`catalog_part` não guarda quem preencheu cada campo — isso vive em
`catalog_partactivity` (log de auditoria por (part_id, attribute)). A confiabilidade
em si não é um dado do banco: é uma regra de negócio (repassada pelo time de dados,
não documentada na SPEC original):

1. Provider da mesma manufacturer da marca da peça -> confiabilidade "alta".
2. Providers FRAGA e SUIV (por nome) -> "media", empatados entre si.
3. Qualquer outro provider -> "baixa".
4. Fallback pra owner (registros legados sem provider): mesma comparação de
   manufacturer, só "alta"/"baixa" (sem o nível intermediário FRAGA/SUIV, que é
   específico de provider).
5. Sem provider nem owner rastreável -> confiabilidade desconhecida (None).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tools.db_query import consultar_banco

NivelConfiabilidade = Literal["alta", "media", "baixa"]

_PROVIDERS_NIVEL_MEDIO = frozenset({"FRAGA", "SUIV"})

_QUERY_FONTE_ATUAL = """
SELECT current_provider_id, current_owner_id
FROM catalog_partactivity
WHERE part_id = %(part_id)s AND attribute = %(campo)s
ORDER BY created DESC
LIMIT 1
"""

_QUERY_PROVIDER = """
SELECT name, manufacturer_id
FROM catalog_informationprovider
WHERE id = %(provider_id)s
"""

_QUERY_OWNER_MANUFACTURER = """
SELECT manufacturer_id
FROM catalog_partowner
WHERE id = %(owner_id)s
"""

_QUERY_BRAND_MANUFACTURER = """
SELECT manufacturer_id
FROM manufacturer_brand
WHERE id = %(brand_id)s
"""


@dataclass(frozen=True)
class FonteCampo:
    provider_id: int | None
    owner_id: int | None


@dataclass(frozen=True)
class InfoProvider:
    nome: str
    manufacturer_id: int | None


def buscar_fonte_atual(part_id: int, campo: str) -> FonteCampo:
    """Busca quem é a fonte (provider/owner) do valor atual de `campo` numa peça.

    Sem nenhuma atividade registrada pro campo, retorna FonteCampo(None, None) —
    o campo nunca foi alterado, não há fonte rastreável.
    """
    resultado = consultar_banco(_QUERY_FONTE_ATUAL, params={"part_id": part_id, "campo": campo})
    if not resultado.rows:
        return FonteCampo(provider_id=None, owner_id=None)
    provider_id, owner_id = resultado.rows[0]
    return FonteCampo(provider_id=provider_id, owner_id=owner_id)


def buscar_info_provider(provider_id: int) -> InfoProvider | None:
    resultado = consultar_banco(_QUERY_PROVIDER, params={"provider_id": provider_id})
    if not resultado.rows:
        return None
    nome, manufacturer_id = resultado.rows[0]
    return InfoProvider(nome=nome, manufacturer_id=manufacturer_id)


def buscar_manufacturer_do_owner(owner_id: int) -> int | None:
    resultado = consultar_banco(_QUERY_OWNER_MANUFACTURER, params={"owner_id": owner_id})
    if not resultado.rows:
        return None
    return resultado.rows[0][0]


def buscar_manufacturer_da_marca(brand_id: int) -> int | None:
    resultado = consultar_banco(_QUERY_BRAND_MANUFACTURER, params={"brand_id": brand_id})
    if not resultado.rows:
        return None
    return resultado.rows[0][0]


def nivel_confiabilidade(
    fonte: FonteCampo,
    brand_id: int,
    buscar_info_provider=buscar_info_provider,
    buscar_manufacturer_do_owner=buscar_manufacturer_do_owner,
    buscar_manufacturer_da_marca=buscar_manufacturer_da_marca,
) -> NivelConfiabilidade | None:
    """Aplica a regra de confiabilidade do time de dados pra uma fonte de campo.

    Os lookups são parâmetros injetáveis (default = implementação real via
    consultar_banco) pra permitir testar a regra de negócio com fakes, sem banco.
    """
    if fonte.provider_id is None and fonte.owner_id is None:
        return None

    manufacturer_da_marca = buscar_manufacturer_da_marca(brand_id)

    if fonte.provider_id is not None:
        info = buscar_info_provider(fonte.provider_id)
        if info is not None:
            if manufacturer_da_marca is not None and info.manufacturer_id == manufacturer_da_marca:
                return "alta"
            if info.nome.strip().upper() in _PROVIDERS_NIVEL_MEDIO:
                return "media"
            return "baixa"

    if fonte.owner_id is not None:
        manufacturer_do_owner = buscar_manufacturer_do_owner(fonte.owner_id)
        if manufacturer_do_owner is not None:
            if manufacturer_da_marca is not None and manufacturer_do_owner == manufacturer_da_marca:
                return "alta"
            return "baixa"

    return None
