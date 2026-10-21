"""Introspecção de FKs via information_schema (SPEC.md §4.3, §6.1).

Roda sempre contra a réplica de dev configurada em config.replica_connection_params
— nunca contra produção.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg

from config import replica_connection_params

_QUERY = """
SELECT
    tc.table_name AS tabela_dependente,
    kcu.column_name AS coluna_fk,
    tc.constraint_name AS nome_constraint
FROM information_schema.table_constraints tc
JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
    AND tc.table_schema = kcu.table_schema
JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name
    AND tc.table_schema = ccu.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY'
    AND ccu.table_name = %(tabela)s
    AND ccu.column_name = %(coluna)s
ORDER BY tabela_dependente, coluna_fk;
"""

# Índices únicos (fora a PK) das tabelas dependentes, só com as colunas-chave
# (sem INCLUDE). Índices com expressão (attnum 0) voltam com `tem_expressao`
# e são descartados: não dá para montar a deduplicação por coluna neles.
_QUERY_UNICOS = """
SELECT
    t.relname AS tabela,
    ic.relname AS nome_indice,
    array_agg(a.attname ORDER BY k.ord) AS colunas,
    pg_get_expr(i.indpred, i.indrelid) AS predicado,
    bool_or(k.attnum = 0) AS tem_expressao
FROM pg_index i
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_class ic ON ic.oid = i.indexrelid
CROSS JOIN LATERAL unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord)
LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE i.indisunique
    AND NOT i.indisprimary
    AND k.ord <= i.indnkeyatts
    AND pg_table_is_visible(t.oid)
    AND t.relname = ANY(%(tabelas)s)
GROUP BY t.relname, ic.relname, i.indpred, i.indrelid
ORDER BY t.relname, ic.relname;
"""


@dataclass(frozen=True)
class IndiceUnico:
    """Índice único de uma tabela dependente que inclui uma coluna de FK.

    `predicado` é o WHERE de um índice parcial, como o Postgres o devolve
    (`pg_get_expr`), ou `None` quando o índice cobre a tabela inteira.
    """

    nome: str
    colunas: tuple[str, ...]
    predicado: str | None = None


@dataclass(frozen=True)
class FkDependency:
    tabela_dependente: str
    coluna_fk: str
    nome_constraint: str
    # Índices únicos da tabela dependente que contêm `coluna_fk`. Mover as FKs
    # de um perdedor para o vencedor viola esses índices quando os dois já têm
    # a mesma linha (ex.: catalog_fit (part_id, version_id)); fk_migration usa
    # isto para apagar a cópia do perdedor antes do UPDATE.
    indices_unicos: tuple[IndiceUnico, ...] = ()


def introspeccao_fk(tabela: str, coluna: str) -> list[FkDependency]:
    """Descobre, em tempo de execução, todas as FKs que apontam para `tabela.coluna`,
    com os índices únicos de cada tabela dependente que incluem a coluna da FK."""
    with psycopg.connect(**replica_connection_params()) as conn:
        with conn.cursor() as cur:
            cur.execute(_QUERY, {"tabela": tabela, "coluna": coluna})
            rows = cur.fetchall()
            tabelas = sorted({r[0] for r in rows})
            cur.execute(_QUERY_UNICOS, {"tabelas": tabelas})
            unicos = cur.fetchall()
    return anexar_indices_unicos(
        [FkDependency(tabela_dependente=r[0], coluna_fk=r[1], nome_constraint=r[2]) for r in rows],
        unicos,
    )


def anexar_indices_unicos(
    dependencias: list[FkDependency],
    linhas_indices: list[tuple],
) -> list[FkDependency]:
    """Liga cada FkDependency aos índices únicos da sua tabela que contêm a coluna da FK.

    `linhas_indices` segue o formato de `_QUERY_UNICOS`:
    (tabela, nome_indice, colunas, predicado, tem_expressao). Pura, para teste sem banco.
    """
    por_tabela: dict[str, list[IndiceUnico]] = {}
    for tabela, nome, colunas, predicado, tem_expressao in linhas_indices:
        if tem_expressao:
            continue
        por_tabela.setdefault(tabela, []).append(
            IndiceUnico(nome=nome, colunas=tuple(colunas), predicado=predicado)
        )
    return [
        FkDependency(
            tabela_dependente=dep.tabela_dependente,
            coluna_fk=dep.coluna_fk,
            nome_constraint=dep.nome_constraint,
            indices_unicos=tuple(
                idx for idx in por_tabela.get(dep.tabela_dependente, []) if dep.coluna_fk in idx.colunas
            ),
        )
        for dep in dependencias
    ]
