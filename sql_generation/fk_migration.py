"""Gera os UPDATEs de realocação de FK (SPEC.md §4.3) — reaproveita a lista de
FkDependency já descoberta por tools.fk_introspection.introspeccao_fk (uma vez
por sessão, não por merge). Pura: não abre conexão nenhuma, só monta texto SQL
com escaping seguro via psycopg.sql (Identifier/Literal funcionam sem conexão
viva — `.as_string(None)`).
"""

from __future__ import annotations

from psycopg import sql

from tools.fk_introspection import FkDependency


def gerar_updates_fk(
    dependencias: list[FkDependency],
    vencedor_id: int,
    perdedor_ids: list[int],
) -> list[str]:
    """Uma UPDATE por (tabela, coluna) FK, redirecionando FKs dos perdedores pro vencedor.

    Deduplica por (tabela_dependente, coluna_fk) preservando a ordem de primeira
    aparição (F-06): duas constraints distintas na mesma coluna gerariam UPDATEs
    idênticas — uma só basta.
    """
    statements = []
    vistos: set[tuple[str, str]] = set()
    for dep in dependencias:
        chave = (dep.tabela_dependente, dep.coluna_fk)
        if chave in vistos:
            continue
        vistos.add(chave)
        statement = sql.SQL("UPDATE {tabela} SET {coluna} = {vencedor} WHERE {coluna} IN ({perdedores});").format(
            tabela=sql.Identifier(dep.tabela_dependente),
            coluna=sql.Identifier(dep.coluna_fk),
            vencedor=sql.Literal(vencedor_id),
            perdedores=sql.SQL(", ").join(sql.Literal(pid) for pid in perdedor_ids),
        )
        statements.append(statement.as_string(None))
    return statements
