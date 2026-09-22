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


@dataclass(frozen=True)
class FkDependency:
    tabela_dependente: str
    coluna_fk: str
    nome_constraint: str


def introspeccao_fk(tabela: str, coluna: str) -> list[FkDependency]:
    """Descobre, em tempo de execução, todas as FKs que apontam para `tabela.coluna`."""
    with psycopg.connect(**replica_connection_params()) as conn:
        with conn.cursor() as cur:
            cur.execute(_QUERY, {"tabela": tabela, "coluna": coluna})
            rows = cur.fetchall()
    return [
        FkDependency(tabela_dependente=r[0], coluna_fk=r[1], nome_constraint=r[2])
        for r in rows
    ]
