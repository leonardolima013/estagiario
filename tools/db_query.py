"""Consulta somente-leitura à réplica de dev (SPEC.md §4.4, §6.1).

Validações antes de qualquer conexão:
- Exatamente um statement (rejeita ';' encadeando múltiplos comandos)
- O statement precisa ser um SELECT no topo
- Nenhuma palavra-chave de escrita/DDL em nenhum ponto da query — isso também
  cobre o truque de CTE que escreve ("WITH x AS (DELETE FROM ... RETURNING *) SELECT ...
  FROM x"), que o Postgres aceita como um único statement começando em SELECT.

Na execução: sessão somente-leitura (`readonly=True`), timeout via
`SET LOCAL statement_timeout`, e truncamento de linhas acima de `max_rows`.
"""

from __future__ import annotations

from dataclasses import dataclass

import psycopg
import psycopg.errors
import sqlparse
from psycopg import sql
from sqlparse.tokens import Keyword

from config import replica_connection_params

_FORBIDDEN_KEYWORDS = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT",
    "REVOKE", "CREATE", "MERGE", "CALL", "EXECUTE", "COPY", "VACUUM",
    "REINDEX", "LISTEN", "NOTIFY", "SET", "RESET", "LOCK", "DO", "COMMENT",
    "PREPARE", "DEALLOCATE", "REFRESH",
}


class QueryValidationError(ValueError):
    """Query rejeitada antes de ser executada — não é um SELECT único e seguro."""


class QueryTimeoutError(RuntimeError):
    """Query cancelada por exceder o timeout configurado."""


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncado: bool


def _validar_select_unico(query: str) -> None:
    statements = [
        s for s in sqlparse.parse(query) if s.token_first(skip_cm=True) is not None
    ]
    if len(statements) != 1:
        raise QueryValidationError(
            f"Esperado exatamente 1 statement, encontrado(s) {len(statements)} "
            "— não é permitido encadear múltiplos comandos com ';'."
        )

    statement = statements[0]
    first_token = statement.token_first(skip_cm=True)
    if first_token is None or first_token.normalized.upper() != "SELECT":
        raise QueryValidationError("Apenas statements SELECT são permitidos.")

    for token in statement.flatten():
        if token.ttype is not None and token.ttype in Keyword:
            if token.normalized.upper() in _FORBIDDEN_KEYWORDS:
                raise QueryValidationError(
                    f"Palavra-chave não permitida na query: {token.normalized.upper()}"
                )


def consultar_banco(
    query: str,
    params: dict | None = None,
    timeout_seconds: int = 10,
    max_rows: int = 500,
) -> QueryResult:
    """Executa uma query somente-leitura contra a réplica de dev."""
    _validar_select_unico(query)

    conn = psycopg.connect(**replica_connection_params(), autocommit=False)
    try:
        conn.read_only = True
        with conn.cursor() as cur:
            # SET não aceita parâmetros bind (%s) no protocolo do Postgres — só literais.
            # timeout_seconds vem do parâmetro tipado da função, não de entrada externa;
            # ainda assim usamos sql.Literal (não f-string) para montar o literal com segurança.
            cur.execute(
                sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_seconds * 1000))
            )
            try:
                cur.execute(query, params)
            except psycopg.errors.QueryCanceled as exc:
                raise QueryTimeoutError(
                    f"Query cancelada: excedeu o timeout de {timeout_seconds}s."
                ) from exc
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
    finally:
        conn.rollback()
        conn.close()

    truncado = len(rows) > max_rows
    if truncado:
        rows = rows[:max_rows]
    return QueryResult(columns=columns, rows=rows, truncado=truncado)
