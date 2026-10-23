"""Fila de famílias duplicadas do loop, materializada uma vez num SQLite local.

Substitui o sorteio por varredura completa a cada iteração
(`tools.sortear_grupo`, auditoria de memória C1/A3): a lista de famílias
(`search_ref + brand_id` com mais de uma peça `ACTIVE`) é lida do Postgres uma
única vez, em lotes, e consumida em ordem — menores famílias primeiro, depois
`search_ref` e `brand_id`. O progresso fica gravado por família, então uma
execução interrompida retoma de onde parou e não repete famílias concluídas.

Status de cada família:
- ``pendente``: ainda não processada (ou devolvida por cancelamento);
- ``em_andamento``: entregue a uma execução viva (``pid``/``execucao``);
- ``concluida``: iteração com sucesso;
- ``erro``: iteração com erro; só volta à fila com `reabrir_erros`.

Uma família em andamento cujo processo morreu volta a ``pendente`` no próximo
`preparar`. Só uma execução por vez deve consumir a mesma fila.

A fila é uma foto: uma família que deixou de ser duplicada depois da montagem
chega ao pipeline com menos de duas peças e é encerrada sem chamar o LLM.
`montar` atualiza a foto (mantém concluídas e erros, remove pendentes que
deixaram de ser duplicadas, acrescenta as novas).
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import config
from loop.models import FamiliaSorteada, IteracaoStatus

_QUERY_FAMILIAS = """
SELECT cp.search_ref, cp.brand_id, mb.name AS brand, COUNT(*) AS qtd_pecas
FROM catalog_part cp
JOIN manufacturer_brand mb ON mb.id = cp.brand_id
WHERE cp.search_ref IS NOT NULL AND cp.search_ref <> ''
    AND cp.lifecycle_status = 'ACTIVE'
GROUP BY cp.search_ref, cp.brand_id, mb.name
HAVING COUNT(*) > 1
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fila_familias (
    search_ref    TEXT    NOT NULL,
    brand_id      INTEGER NOT NULL,
    brand         TEXT    NOT NULL,
    qtd_pecas     INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pendente'
                  CHECK (status IN ('pendente', 'em_andamento', 'concluida', 'erro')),
    tentativas    INTEGER NOT NULL DEFAULT 0,
    geracao       INTEGER NOT NULL,
    execucao      TEXT,
    pid           INTEGER,
    ultimo_erro   TEXT,
    atualizado_em TEXT    NOT NULL,
    PRIMARY KEY (search_ref, brand_id)
);
CREATE INDEX IF NOT EXISTS ix_fila_ordem
    ON fila_familias (status, qtd_pecas, search_ref, brand_id);
CREATE TABLE IF NOT EXISTS fila_metadados (
    chave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""

STATUS_FILA = ("pendente", "em_andamento", "concluida", "erro")

LerFamilias = Callable[[], Iterable[Iterable[tuple]]]
"""Devolve lotes de linhas ``(search_ref, brand_id, brand, qtd_pecas)``."""


def ler_familias_do_banco() -> Iterator[list[tuple]]:
    """Lotes da agregação de famílias duplicadas, com cursor no servidor."""
    from tools.db_query import consultar_banco_em_lotes

    return consultar_banco_em_lotes(_QUERY_FAMILIAS, timeout_seconds=600)


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _processo_vivo(pid: int | None) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # existe, de outro usuário
    return True


class FilaFamilias:
    """Fonte de famílias do loop apoiada numa fila SQLite local.

    Construir não faz I/O; `preparar` cria o arquivo, recupera famílias de
    execuções mortas e monta a fila na primeira vez.
    """

    def __init__(
        self,
        caminho: Path | str | None = None,
        *,
        ler_familias: LerFamilias = ler_familias_do_banco,
        relogio: Callable[[], str] = _agora,
    ) -> None:
        self.caminho = Path(caminho) if caminho is not None else config.fila_db_path()
        self._ler_familias = ler_familias
        self._relogio = relogio
        self._execucao = uuid.uuid4().hex
        self._pid = os.getpid()
        self._pronta = False

    # -- infraestrutura ----------------------------------------------------

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.caminho, isolation_level=None, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def _transacao(self) -> Iterator[sqlite3.Connection]:
        with closing(self._conectar()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def _garantir_schema(self) -> None:
        if self._pronta:
            return
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._conectar()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA}\nCOMMIT;")
        self._pronta = True

    def _metadado(self, conn: sqlite3.Connection, chave: str) -> str | None:
        linha = conn.execute("SELECT valor FROM fila_metadados WHERE chave = ?", (chave,)).fetchone()
        return linha[0] if linha else None

    # -- ciclo de vida -----------------------------------------------------

    @property
    def montada(self) -> bool:
        self._garantir_schema()
        with closing(self._conectar()) as conn:
            return self._metadado(conn, "montada_em") is not None

    def preparar(self) -> None:
        """Cria o arquivo, devolve famílias de execuções mortas e monta na 1ª vez."""
        self._garantir_schema()
        self.recuperar_interrompidas()
        if not self.montada:
            self.montar()

    def montar(self) -> int:
        """Lê a lista de famílias do Postgres e atualiza a fila. Devolve o total lido.

        Mantém o status de famílias já conhecidas; pendentes que deixaram de ser
        duplicadas saem; famílias novas entram como pendentes.
        """
        self._garantir_schema()
        agora = self._relogio()
        with self._transacao() as conn:
            geracao = int(self._metadado(conn, "geracao") or 0) + 1
            total = 0
            for lote in self._ler_familias():
                linhas = [
                    (str(search_ref), int(brand_id), str(brand), int(qtd), geracao, agora)
                    for search_ref, brand_id, brand, qtd in lote
                ]
                total += len(linhas)
                conn.executemany(
                    """
                    INSERT INTO fila_familias
                        (search_ref, brand_id, brand, qtd_pecas, status, geracao, atualizado_em)
                    VALUES (?, ?, ?, ?, 'pendente', ?, ?)
                    ON CONFLICT (search_ref, brand_id) DO UPDATE SET
                        brand = excluded.brand,
                        qtd_pecas = excluded.qtd_pecas,
                        geracao = excluded.geracao
                    """,
                    linhas,
                )
            conn.execute(
                "DELETE FROM fila_familias WHERE status = 'pendente' AND geracao < ?", (geracao,)
            )
            for chave, valor in (("geracao", str(geracao)), ("montada_em", agora), ("total_lido", str(total))):
                conn.execute(
                    "INSERT INTO fila_metadados (chave, valor) VALUES (?, ?) "
                    "ON CONFLICT (chave) DO UPDATE SET valor = excluded.valor",
                    (chave, valor),
                )
        return total

    def recuperar_interrompidas(self) -> int:
        """Devolve à fila famílias em andamento de execuções que não existem mais."""
        self._garantir_schema()
        with self._transacao() as conn:
            linhas = conn.execute(
                "SELECT search_ref, brand_id, pid, execucao FROM fila_familias WHERE status = 'em_andamento'"
            ).fetchall()
            mortas = [
                (search_ref, brand_id)
                for search_ref, brand_id, pid, execucao in linhas
                if not _processo_vivo(pid) or (pid == self._pid and execucao != self._execucao)
            ]
            conn.executemany(
                "UPDATE fila_familias SET status = 'pendente', execucao = NULL, pid = NULL, "
                "atualizado_em = ? WHERE search_ref = ? AND brand_id = ?",
                [(self._relogio(), s, b) for s, b in mortas],
            )
        return len(mortas)

    def reabrir_erros(self) -> int:
        """Devolve à fila as famílias que terminaram em erro."""
        self._garantir_schema()
        with self._transacao() as conn:
            cur = conn.execute(
                "UPDATE fila_familias SET status = 'pendente', atualizado_em = ? WHERE status = 'erro'",
                (self._relogio(),),
            )
            return cur.rowcount

    def encerrar(self) -> None:
        """Devolve à fila o que esta execução pegou e não concluiu."""
        if not self._pronta:
            return
        with self._transacao() as conn:
            conn.execute(
                "UPDATE fila_familias SET status = 'pendente', execucao = NULL, pid = NULL, "
                "atualizado_em = ? WHERE status = 'em_andamento' AND execucao = ?",
                (self._relogio(), self._execucao),
            )

    # -- consumo -----------------------------------------------------------

    def proxima(self) -> FamiliaSorteada | None:
        """A próxima família pendente (menor primeiro), já marcada em andamento.

        `None` quando não há mais pendentes.
        """
        self._garantir_schema()
        with self._transacao() as conn:
            linha = conn.execute(
                """
                SELECT search_ref, brand_id, brand FROM fila_familias
                WHERE status = 'pendente'
                ORDER BY qtd_pecas, search_ref, brand_id
                LIMIT 1
                """
            ).fetchone()
            if linha is None:
                return None
            search_ref, brand_id, brand = linha
            conn.execute(
                """
                UPDATE fila_familias
                SET status = 'em_andamento', execucao = ?, pid = ?,
                    tentativas = tentativas + 1, atualizado_em = ?
                WHERE search_ref = ? AND brand_id = ?
                """,
                (self._execucao, self._pid, self._relogio(), search_ref, brand_id),
            )
        return FamiliaSorteada(search_ref=search_ref, brand_id=brand_id, brand=brand)

    def concluir(
        self, familia: FamiliaSorteada, status: IteracaoStatus, erro: dict | None = None
    ) -> None:
        """Grava o desfecho da família: sucesso → concluída, erro → erro,
        cancelada → volta a pendente."""
        novo = {
            IteracaoStatus.SUCESSO: "concluida",
            IteracaoStatus.ERRO: "erro",
            IteracaoStatus.CANCELADA: "pendente",
        }[status]
        ultimo_erro = f"{erro.get('tipo')}: {erro.get('mensagem')}"[:500] if erro else None
        with self._transacao() as conn:
            conn.execute(
                """
                UPDATE fila_familias
                SET status = ?, ultimo_erro = COALESCE(?, ultimo_erro),
                    execucao = NULL, pid = NULL, atualizado_em = ?
                WHERE search_ref = ? AND brand_id = ?
                """,
                (novo, ultimo_erro, self._relogio(), familia.search_ref, familia.brand_id),
            )

    # -- inspeção ----------------------------------------------------------

    def contagem(self) -> dict[str, int]:
        """Famílias por status (todos os status aparecem, inclusive zerados)."""
        self._garantir_schema()
        with closing(self._conectar()) as conn:
            linhas = conn.execute(
                "SELECT status, COUNT(*) FROM fila_familias GROUP BY status"
            ).fetchall()
        contagem = dict.fromkeys(STATUS_FILA, 0)
        contagem.update({status: n for status, n in linhas})
        return contagem

    def metadados(self) -> dict[str, str]:
        self._garantir_schema()
        with closing(self._conectar()) as conn:
            return dict(conn.execute("SELECT chave, valor FROM fila_metadados").fetchall())
