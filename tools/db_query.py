"""Consulta somente-leitura à réplica de dev (SPEC.md §4.4, §6.1).

Validações antes de qualquer conexão:
- Exatamente um statement (rejeita ';' encadeando múltiplos comandos)
- O statement precisa ser um SELECT no topo
- Nenhuma palavra-chave de escrita/DDL em nenhum ponto da query — isso também
  cobre o truque de CTE que escreve ("WITH x AS (DELETE FROM ... RETURNING *) SELECT ...
  FROM x"), que o Postgres aceita como um único statement começando em SELECT.

Na execução: sessão somente-leitura (`readonly=True`), timeout via
`SET LOCAL statement_timeout`, e truncamento de linhas acima de `max_rows`.

Conexão: fora de uma `sessao_banco()`, cada consulta abre e fecha a própria
conexão. Dentro dela, as consultas da mesma thread reaproveitam uma única
conexão, aberta só na primeira consulta; cada consulta continua sendo uma
transação curta (SET LOCAL + SELECT + rollback), então nenhuma transação fica
aberta entre consultas. O loop abre uma sessão por execução (auditoria de
memória, docs/auditoria-memoria/RELATORIO.md, A2).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, TypeVar

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


T = TypeVar("T")

# Teto do cache de consultas de referência de uma sessão (marca → manufacturer,
# provider, owner). Bem acima do que um loop pequeno usa; o LRU só evita que uma
# execução longa cresça sem limite.
MAX_CACHE_SESSAO = 4096


def _abrir_conexao() -> psycopg.Connection:
    conn = psycopg.connect(**replica_connection_params(), autocommit=False)
    conn.read_only = True
    return conn


class _SessaoBanco:
    """Uma conexão somente-leitura reaproveitada e um cache LRU de referência.

    A conexão só é aberta na primeira consulta e pertence à thread que abriu a
    sessão; outra thread que herde o contexto usa conexões próprias, porque uma
    conexão psycopg não pode ser usada por duas threads ao mesmo tempo.
    """

    def __init__(self) -> None:
        self._thread = threading.get_ident()
        self._conn: psycopg.Connection | None = None
        self._cache: OrderedDict[Any, Any] = OrderedDict()
        self.conexoes_abertas = 0

    @property
    def da_thread_atual(self) -> bool:
        return threading.get_ident() == self._thread

    def conexao(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed or self._conn.broken:
            self.descartar()
            self._conn = _abrir_conexao()
            self.conexoes_abertas += 1
        return self._conn

    def descartar(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 — conexão já quebrada
                pass

    def memo(self, chave: Any, calcular: Callable[[], T]) -> T:
        if chave in self._cache:
            self._cache.move_to_end(chave)
            return self._cache[chave]
        valor = calcular()
        self._cache[chave] = valor
        if len(self._cache) > MAX_CACHE_SESSAO:
            self._cache.popitem(last=False)
        return valor


_sessao_atual: ContextVar[_SessaoBanco | None] = ContextVar("estagiario_sessao_banco", default=None)


@contextmanager
def sessao_banco() -> Iterator[_SessaoBanco]:
    """Reaproveita uma conexão (e um cache de referência) nas consultas do bloco.

    Sessões aninhadas reaproveitam a de fora. Nenhuma conexão é aberta se o
    bloco não consultar o banco.
    """
    externa = _sessao_atual.get()
    if externa is not None and externa.da_thread_atual:
        yield externa
        return
    sessao = _SessaoBanco()
    token = _sessao_atual.set(sessao)
    try:
        yield sessao
    finally:
        _sessao_atual.reset(token)
        sessao.descartar()


def _sessao_da_thread() -> _SessaoBanco | None:
    sessao = _sessao_atual.get()
    if sessao is not None and sessao.da_thread_atual:
        return sessao
    return None


def memo_sessao(chave: Any, calcular: Callable[[], T]) -> T:
    """`calcular()` em cache durante a sessão atual; sem sessão, sempre calcula.

    Só para dados de referência que não mudam durante um loop (marca →
    manufacturer, provider, owner). Sem sessão não há cache, então testes que
    trocam `consultar_banco` nunca enxergam valores de outro teste.
    """
    sessao = _sessao_da_thread()
    if sessao is None:
        return calcular()
    return sessao.memo(chave, calcular)


def _definir_timeout(cur: psycopg.Cursor, timeout_seconds: int) -> None:
    # SET não aceita parâmetros bind (%s) no protocolo do Postgres — só literais.
    # timeout_seconds vem do parâmetro tipado da função, não de entrada externa;
    # ainda assim usamos sql.Literal (não f-string) para montar o literal com segurança.
    cur.execute(
        sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout_seconds * 1000))
    )


def _executar(conn: psycopg.Connection, query: str, params, timeout_seconds: int, max_rows: int):
    try:
        with conn.cursor() as cur:
            _definir_timeout(cur, timeout_seconds)
            try:
                cur.execute(query, params)
            except psycopg.errors.QueryCanceled as exc:
                raise QueryTimeoutError(
                    f"Query cancelada: excedeu o timeout de {timeout_seconds}s."
                ) from exc
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows = cur.fetchmany(max_rows + 1)
    finally:
        if not conn.closed and not conn.broken:
            conn.rollback()
    return columns, rows


def consultar_banco(
    query: str,
    params: dict | None = None,
    timeout_seconds: int = 10,
    max_rows: int = 500,
) -> QueryResult:
    """Executa uma query somente-leitura contra a réplica de dev."""
    _validar_select_unico(query)

    sessao = _sessao_da_thread()
    if sessao is None:
        conn = _abrir_conexao()
        try:
            columns, rows = _executar(conn, query, params, timeout_seconds, max_rows)
        finally:
            conn.close()
    else:
        conn = sessao.conexao()
        try:
            columns, rows = _executar(conn, query, params, timeout_seconds, max_rows)
        except psycopg.OperationalError:
            # Conexão perdida: a próxima consulta da sessão abre outra.
            sessao.descartar()
            raise

    truncado = len(rows) > max_rows
    if truncado:
        rows = rows[:max_rows]
    return QueryResult(columns=columns, rows=rows, truncado=truncado)


def consultar_banco_em_lotes(
    query: str,
    params: dict | None = None,
    *,
    timeout_seconds: int = 300,
    tamanho_lote: int = 5000,
) -> Iterator[list[tuple]]:
    """Itera o resultado em lotes, com cursor no servidor, sem teto de linhas.

    Para leituras grandes feitas uma vez (montagem da fila de famílias), sem
    trazer o resultado inteiro para a memória. Mesma validação e mesma sessão
    somente-leitura de `consultar_banco`; usa uma conexão própria, fechada ao
    fim da iteração. O timeout vale para cada FETCH; o primeiro carrega a
    agregação inteira.
    """
    _validar_select_unico(query)
    conn = _abrir_conexao()
    try:
        with conn.cursor() as cur:
            _definir_timeout(cur, timeout_seconds)
        with conn.cursor(name="estagiario_lotes") as cur:
            try:
                cur.execute(query, params)
                while True:
                    lote = cur.fetchmany(tamanho_lote)
                    if not lote:
                        break
                    yield lote
            except psycopg.errors.QueryCanceled as exc:
                raise QueryTimeoutError(
                    f"Query cancelada: excedeu o timeout de {timeout_seconds}s."
                ) from exc
    finally:
        if not conn.closed and not conn.broken:
            conn.rollback()
        conn.close()
