"""Sorteia um grupo de duplicatas candidatas real, pra testar a ferramenta sem
precisar escolher um search_ref+brand_id manualmente.

Mesma lógica do "Caso 1" de context_files/sortear_pecas_duplicadas.py (grupos de
search_ref+brand_id com mais de 1 registro), mas o sorteio é resolvido dentro do
SQL (ORDER BY random() LIMIT 1) — evita trazer os ~321 mil registros duplicados
pro Python só pra escolher um.
"""

from __future__ import annotations

from dataclasses import dataclass

from tools.db_query import consultar_banco

_QUERY_BASE = """
SELECT search_ref, brand_id, brand
FROM (
    SELECT
        cp.search_ref, cp.brand_id, mb.name AS brand,
        COUNT(*) OVER (PARTITION BY cp.search_ref, cp.brand_id) AS qtd_duplicados
    FROM catalog_part cp
    JOIN manufacturer_brand mb ON mb.id = cp.brand_id
    WHERE cp.search_ref IS NOT NULL AND cp.search_ref <> ''
) t
WHERE qtd_duplicados > 1
"""


class NenhumGrupoDuplicadoError(RuntimeError):
    """Não há outra família duplicada disponível para sortear."""


@dataclass(frozen=True)
class GrupoSorteado:
    search_ref: str
    brand_id: int
    brand: str


def sortear_grupo_aleatorio(
    excluir: set[tuple[str, int]] | None = None,
) -> GrupoSorteado:
    """Sorteia uma família duplicada que não esteja em `excluir`.

    A exclusão é construída como uma lista de tuplas parametrizadas; os valores
    nunca são interpolados no SQL. `NenhumGrupoDuplicadoError` significa apenas
    que o universo elegível acabou e é tratado como parada normal pelo loop.
    """
    excluir = excluir or set()
    query = _QUERY_BASE
    params: dict[str, object] = {}
    if excluir:
        pares = []
        for indice, (search_ref, brand_id) in enumerate(sorted(excluir)):
            chave_search = f"ex_search_{indice}"
            chave_brand = f"ex_brand_{indice}"
            pares.append(f"(%({chave_search})s, %({chave_brand})s)")
            params[chave_search] = search_ref
            params[chave_brand] = brand_id
        query += f" AND (search_ref, brand_id) NOT IN ({', '.join(pares)})\n"
    query += "ORDER BY random()\nLIMIT 1"

    resultado = consultar_banco(query, params=params or None, timeout_seconds=20)
    if not resultado.rows:
        raise NenhumGrupoDuplicadoError("nenhuma família duplicada disponível na réplica configurada")
    search_ref, brand_id, brand = resultado.rows[0]
    return GrupoSorteado(search_ref=search_ref, brand_id=brand_id, brand=brand)
