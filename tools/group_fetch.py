"""Fetch de um grupo de duplicatas candidatas (search_ref + brand_id) em catalog_part.

Reaproveita tools.db_query.consultar_banco em vez de abrir uma conexão própria,
para herdar a mesma validação/segurança (somente-leitura, réplica de dev).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from tools.db_query import consultar_banco

_QUERY = """
SELECT
    cp.id, cp.search_ref, cp.brand_id, mb.name AS brand, cp.name,
    cp.width, cp.depth, cp.height, cp.gross_weight, cp.net_weight,
    cp.ncm, cp.barcode,
    cp.application, cp.born_at, cp.deprecated_at, cp.similarity_id, cp.created
FROM catalog_part cp
JOIN manufacturer_brand mb ON mb.id = cp.brand_id
WHERE cp.search_ref = %(search_ref)s AND cp.brand_id = %(brand_id)s
ORDER BY cp.id
"""


@dataclass(frozen=True)
class RegistroCatalogPart:
    id: int
    search_ref: str
    brand_id: int
    brand: str
    name: str
    width: float | None
    depth: float | None
    height: float | None
    gross_weight: float | None
    net_weight: float | None
    ncm: str | None
    barcode: str | None
    application: str | None
    # born_at/deprecated_at são ANOS inteiros derivados de `application`
    # (arbitration/datas.py deriva min/max ano), não datas/strings — daí `int | None`.
    # TODO(data-team): confirmar o tipo real da coluna em catalog_part antes de
    # fechar (leitura de schema é do usuário; o ambiente bloqueia leitura direta
    # contra hubbi_prod). Se a coluna for date/timestamp, a derivação e este hint
    # precisam mudar juntos.
    born_at: int | None
    deprecated_at: int | None
    similarity_id: int | None
    created: datetime | None


class GrupoTruncadoError(RuntimeError):
    """O grupo excede o teto de linhas de consultar_banco (max_rows) e voltou
    truncado — particionar/mesclar um subconjunto silencioso do grupo levaria a
    um merge parcial (ids "faltando" que nunca aparecem). Melhor falhar explícito
    (F-08): grupos grandes precisam de política própria (relevante à compaction da
    Fase 5), não de um subconjunto arbitrário."""


def buscar_grupo(search_ref: str, brand_id: int) -> list[RegistroCatalogPart]:
    """Busca todos os registros de catalog_part para um search_ref + brand_id."""
    resultado = consultar_banco(_QUERY, params={"search_ref": search_ref, "brand_id": brand_id})
    if resultado.truncado:
        raise GrupoTruncadoError(
            f"Grupo {search_ref!r}/{brand_id} excedeu o limite de linhas de consultar_banco — "
            "resultado truncado. Não é seguro particionar um subconjunto do grupo; "
            "trate grupos grandes explicitamente antes de prosseguir."
        )
    return [RegistroCatalogPart(**dict(zip(resultado.columns, row))) for row in resultado.rows]


def resolver_brand_id(nome_marca: str) -> int:
    """Resolve o id de uma marca pelo nome — utilitário para os casos de teste da SPEC.md §8,
    que identificam grupos por nome de marca (ex: 'CITROEN'), não por brand_id."""
    resultado = consultar_banco(
        "SELECT id FROM manufacturer_brand WHERE name = %(nome)s",
        params={"nome": nome_marca},
    )
    if not resultado.rows:
        raise ValueError(f"Marca não encontrada: {nome_marca!r}")
    return resultado.rows[0][0]
