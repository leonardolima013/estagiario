"""Armazem_Paginas: persistência SQLite das páginas coletadas por peça.

Este módulo é o único dono do schema de páginas (`conteudo_pagina`,
`registro_coleta` e a view `vw_paginas_por_peca`), no mesmo papel que
`db/rule_store.py` tem para o RuleStore (Req 7.9). O conteúdo é endereçado pelo
SHA-256 dos bytes exatamente como recebidos e gravado comprimido com gzip
determinístico (Req 7.1–7.5).

Os modelos usam só tipos primitivos para que a camada `db/` não dependa do
domínio `coleta_paginas`. O arquivo é distinto do RuleStore e a abertura é
recusada quando os dois caminhos coincidem (Req 7.10, 7.12).

A API de Tentativa_Fetch (`registrar_tentativa`, `ultimas_tentativas` e
`dominio_excluido`, sobre `tentativas_fetch_dominio`) é usada pelo coletor
somente quando o fallback stealth está ligado: ele registra cada busca urllib ou
stealth e consulta a exclusão por domínio e camada antes de buscar. Com o
fallback desligado, nada no fluxo principal lê nem grava essa tabela. O
comentário "só API; não integrada ao coletor" dentro de `_SCHEMA` é mantido
byte a byte de propósito, porque alterar a string do schema mudaria o artefato
versionado (`VERSAO_SCHEMA` continua 1); vale a descrição desta docstring.
"""

from __future__ import annotations

import codecs
import gzip
import hashlib
import os
import sqlite3
import tempfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import config

FORMATO_INSTANTE = "%Y-%m-%dT%H:%M:%S.%fZ"
VERSAO_SCHEMA = 1

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS conteudo_pagina (
    hash_conteudo  TEXT PRIMARY KEY CHECK (length(hash_conteudo) = 64),
    conteudo_gzip  BLOB NOT NULL,
    tamanho_bytes  INTEGER NOT NULL CHECK (tamanho_bytes >= 0),
    criado_em      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registro_coleta (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    codigo_peca        TEXT NOT NULL,
    marca_peca         TEXT,
    url_original       TEXT NOT NULL,
    url_normalizada    TEXT NOT NULL,
    url_final          TEXT NOT NULL,
    dominio            TEXT NOT NULL,
    confianca          TEXT NOT NULL CHECK (confianca IN ('alta', 'media')),
    codigo_confirmado  INTEGER NOT NULL CHECK (codigo_confirmado IN (0, 1)),
    marca_confirmada   INTEGER NOT NULL CHECK (marca_confirmada IN (0, 1)),
    nome_reforcado     INTEGER NOT NULL CHECK (nome_reforcado IN (0, 1)),
    motivo_decisao     TEXT NOT NULL,
    status_http        INTEGER NOT NULL,
    content_type       TEXT NOT NULL,
    charset            TEXT,
    reaproveitado      INTEGER NOT NULL CHECK (reaproveitado IN (0, 1)),
    hash_conteudo      TEXT NOT NULL REFERENCES conteudo_pagina(hash_conteudo),
    coletado_em        TEXT NOT NULL,
    registrado_em      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_registro_coleta_peca
    ON registro_coleta (codigo_peca, marca_peca, coletado_em, id);
CREATE INDEX IF NOT EXISTS idx_registro_coleta_url
    ON registro_coleta (url_normalizada, coletado_em, id);
CREATE INDEX IF NOT EXISTS idx_registro_coleta_hash
    ON registro_coleta (hash_conteudo, coletado_em, id);

CREATE VIEW IF NOT EXISTS vw_paginas_por_peca AS
SELECT r.id, r.codigo_peca, r.marca_peca, r.url_original, r.dominio, r.confianca,
       r.coletado_em, r.hash_conteudo, c.tamanho_bytes
FROM registro_coleta r
JOIN conteudo_pagina c ON c.hash_conteudo = r.hash_conteudo;

-- Reputação de domínio por camada de fetch (só API; não integrada ao coletor).
CREATE TABLE IF NOT EXISTS tentativas_fetch_dominio (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    dominio      TEXT NOT NULL,
    camada       TEXT NOT NULL CHECK (camada IN ('urllib', 'playwright_stealth')),
    sucesso      INTEGER NOT NULL CHECK (sucesso IN (0, 1)),
    motivo       TEXT NULL,
    status_http  INTEGER NULL,
    tentado_em   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tentativas_fetch_dominio
    ON tentativas_fetch_dominio (dominio, camada, id);

PRAGMA user_version = {VERSAO_SCHEMA};
"""


# ---------------------------------------------------------------------------
# Modelos (tipos primitivos, sem dependência do domínio)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NovoRegistroColeta:
    """Dados de um Registro_Coleta a gravar (decisão da fonte + metadados do fetch)."""

    codigo_peca: str
    marca_peca: str | None
    url_original: str
    url_normalizada: str
    url_final: str
    dominio: str
    confianca: str
    codigo_confirmado: bool
    marca_confirmada: bool
    nome_reforcado: bool
    motivo_decisao: str
    status_http: int
    content_type: str
    charset: str | None
    coletado_em: datetime


@dataclass(frozen=True)
class RegistroColeta:
    """Registro_Coleta persistido: `NovoRegistroColeta` + id, hash, reuso e registro."""

    id: int
    codigo_peca: str
    marca_peca: str | None
    url_original: str
    url_normalizada: str
    url_final: str
    dominio: str
    confianca: str
    codigo_confirmado: bool
    marca_confirmada: bool
    nome_reforcado: bool
    motivo_decisao: str
    status_http: int
    content_type: str
    charset: str | None
    reaproveitado: bool
    hash_conteudo: str
    coletado_em: datetime
    registrado_em: datetime


CAMADAS_FETCH = frozenset({"urllib", "playwright_stealth"})
LIMITE_FALHAS_CONSECUTIVAS_PADRAO = 3


@dataclass(frozen=True)
class TentativaFetch:
    """Uma tentativa de fetch de um domínio por uma camada (`tentativas_fetch_dominio`)."""

    id: int
    dominio: str
    camada: str
    sucesso: bool
    motivo: str | None
    status_http: int | None
    tentado_em: datetime


def normalizar_dominio(dominio: str) -> str:
    """Domínio como gravado na reputação: ``strip`` + casefold, sem ponto final."""
    return dominio.strip().casefold().rstrip(".")


@dataclass(frozen=True)
class ResumoPeca:
    """Linha da listagem de peças com ao menos um Registro_Coleta (Req 8.4)."""

    codigo_peca: str
    marca_peca: str | None
    quantidade_registros: int
    ultima_coleta: datetime


# ---------------------------------------------------------------------------
# Erros
# ---------------------------------------------------------------------------


class ErroArmazenamento(RuntimeError):
    """Falha de gravação ou de integridade no Armazem_Paginas (Req 7.7)."""


class ConteudoNaoEncontradoError(LookupError):
    """Hash_Conteudo solicitado não existe no Armazem_Paginas (Req 7.8, 8.9)."""

    def __init__(self, hash_conteudo: str) -> None:
        super().__init__(f"conteúdo não encontrado: {hash_conteudo}")
        self.hash_conteudo = hash_conteudo


class DestinoExistenteError(FileExistsError):
    """O arquivo de destino da exportação já existe (Req 8.7)."""


class ConflitoCaminhoArmazemError(ValueError):
    """O arquivo do Armazem_Paginas coincide com o do RuleStore (Req 7.12)."""

    def __init__(self, caminho_armazem: Path, caminho_rule_store: Path) -> None:
        super().__init__(
            "configuração inválida: o arquivo do Armazem_Paginas "
            f"({caminho_armazem}) é o mesmo do RuleStore ({caminho_rule_store})"
        )
        self.caminho_armazem = caminho_armazem
        self.caminho_rule_store = caminho_rule_store


# ---------------------------------------------------------------------------
# Instantes
# ---------------------------------------------------------------------------


def _instante_para_texto(instante: datetime) -> str:
    """ISO-8601 UTC em formato fixo; ordem lexicográfica = ordem cronológica."""
    if instante.tzinfo is None or instante.utcoffset() is None:
        raise ValueError("instante precisa ser um datetime com fuso (aware), em UTC")
    return instante.astimezone(timezone.utc).strftime(FORMATO_INSTANTE)


def _texto_para_instante(texto: str) -> datetime:
    return datetime.strptime(texto, FORMATO_INSTANTE).replace(tzinfo=timezone.utc)


def _relogio_padrao() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Armazém
# ---------------------------------------------------------------------------


def _caminhos_conflitam(armazem: Path, rule_store: Path) -> bool:
    if armazem.resolve() == rule_store.resolve():
        return True
    if armazem.exists() and rule_store.exists():
        # Cobre hard links, que `resolve()` não identifica.
        return os.path.samefile(armazem, rule_store)
    return False


class ArmazemPaginas:
    """Armazém de páginas por peça sobre um arquivo SQLite local."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        caminho_rule_store: Path | None = None,
        relogio: Callable[[], datetime] | None = None,
    ) -> None:
        """Checa o conflito com o RuleStore ANTES de conectar (Req 7.12), cria o
        diretório e aplica o schema idempotente (Req 7.11)."""
        caminho = Path(db_path) if db_path is not None else config.paginas_db_path()
        rule_store = (
            Path(caminho_rule_store)
            if caminho_rule_store is not None
            else config.rule_store_db_path()
        )
        if _caminhos_conflitam(caminho, rule_store):
            raise ConflitoCaminhoArmazemError(caminho, rule_store)

        self._db_path = caminho
        self._relogio = relogio or _relogio_padrao
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._aplicar_schema()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # -- infraestrutura ----------------------------------------------------

    def _conectar(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    @contextmanager
    def _transacao(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            # Alguns erros (ex.: SQLITE_FULL, RAISE(ROLLBACK)) já desfazem a transação.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _aplicar_schema(self) -> None:
        with closing(self._conectar()) as conn:
            # journal_mode não pode mudar dentro de transação e é persistente no arquivo.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(f"BEGIN IMMEDIATE;\n{_SCHEMA}\nCOMMIT;")

    def _agora(self) -> str:
        return _instante_para_texto(self._relogio())

    # -- conteúdo ------------------------------------------------------------

    def armazenar_conteudo(self, conteudo: bytes) -> str:
        """Grava `conteudo` endereçado por SHA-256, se ainda não existir, e devolve o hash.

        O hash é calculado sobre os bytes exatamente como recebidos (Req 7.1). Um hash
        já presente preserva blob, tamanho e `criado_em` originais (Req 7.2, 7.4).
        """
        dados = _como_bytes(conteudo)
        hash_conteudo = hashlib.sha256(dados).hexdigest()
        criado_em = self._agora()
        comprimido = gzip.compress(dados, compresslevel=6, mtime=0)
        try:
            with closing(self._conectar()) as conn, self._transacao(conn):
                _inserir_conteudo(conn, hash_conteudo, comprimido, len(dados), criado_em)
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao gravar conteúdo {hash_conteudo}: {type(exc).__name__}"
            ) from exc
        return hash_conteudo

    def ler_conteudo(self, hash_conteudo: str) -> bytes:
        """Bytes originais do conteúdo (Req 7.3); erro se o hash não existe (Req 7.8)."""
        with closing(self._conectar()) as conn:
            linha = conn.execute(
                "SELECT conteudo_gzip FROM conteudo_pagina WHERE hash_conteudo = ?",
                (hash_conteudo,),
            ).fetchone()
        if linha is None:
            raise ConteudoNaoEncontradoError(hash_conteudo)
        try:
            return gzip.decompress(linha["conteudo_gzip"])
        except (OSError, EOFError, zlib.error) as exc:
            raise ErroArmazenamento(
                f"conteúdo {hash_conteudo} corrompido no armazém"
            ) from exc

    # -- registros de coleta ---------------------------------------------------

    def gravar_coleta(self, conteudo: bytes, registro: NovoRegistroColeta) -> str:
        """Grava o conteúdo (se novo) e o Registro_Coleta numa única transação (Req 7.6).

        O registro é gravado com `reaproveitado=0` e `registrado_em` = agora. Qualquer
        `sqlite3.Error` desfaz a transação inteira — nem conteúdo novo nem registro
        novo ficam visíveis — e vira `ErroArmazenamento` (Req 7.7). Devolve o hash.
        """
        dados = _como_bytes(conteudo)
        hash_conteudo = hashlib.sha256(dados).hexdigest()
        comprimido = gzip.compress(dados, compresslevel=6, mtime=0)
        # Conversões de instante fora da transação: datetime ingênuo é erro de uso
        # (ValueError), não falha de armazenamento.
        coletado_em = _instante_para_texto(registro.coletado_em)
        agora = self._agora()
        try:
            with closing(self._conectar()) as conn, self._transacao(conn):
                _inserir_conteudo(conn, hash_conteudo, comprimido, len(dados), agora)
                _inserir_registro(
                    conn,
                    registro,
                    hash_conteudo=hash_conteudo,
                    reaproveitado=False,
                    coletado_em=coletado_em,
                    registrado_em=agora,
                )
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao gravar coleta do conteúdo {hash_conteudo}: {type(exc).__name__}"
            ) from exc
        return hash_conteudo

    def buscar_reaproveitavel(
        self, url_normalizada: str, *, coletado_desde: datetime
    ) -> RegistroColeta | None:
        """Registro mais recente da URL_Normalizada com `coletado_em >= coletado_desde`.

        "Mais recente" = maior `coletado_em` e, em empate, maior `id` (Req 6.2). O
        chamador calcula `coletado_desde = agora - Janela_Reuso`. Como registros
        reaproveitados herdam o `coletado_em` da origem, a janela mede sempre a idade
        do fetch real. Devolve `None` quando não há registro na janela.
        """
        desde = _instante_para_texto(coletado_desde)
        try:
            with closing(self._conectar()) as conn:
                linha = conn.execute(
                    """
                    SELECT * FROM registro_coleta
                    WHERE url_normalizada = ? AND coletado_em >= ?
                    ORDER BY coletado_em DESC, id DESC
                    LIMIT 1
                    """,
                    (url_normalizada, desde),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao consultar registros reaproveitáveis: {type(exc).__name__}"
            ) from exc
        return _linha_para_registro(linha) if linha is not None else None

    def registrar_reaproveitamento(
        self, registro: NovoRegistroColeta, hash_conteudo: str
    ) -> tuple[RegistroColeta, bool]:
        """Vincula a peça de `registro` a um conteúdo já armazenado (Req 6.3, 6.11).

        Contrato: o chamador monta `registro` com a Decisao_Fonte ATUAL (peça, URL
        original, URL_Normalizada, domínio, confiança, indicadores, motivo) e com os
        dados de fetch COPIADOS do Registro_Coleta de origem devolvido por
        `buscar_reaproveitavel` (`coletado_em`, `status_http`, `content_type`,
        `charset`, `url_final`). O armazém grava esses valores como recebidos, com
        `reaproveitado=1` e `registrado_em` = agora; assim a Janela_Reuso não se
        renova em cadeia.

        Na mesma transação `BEGIN IMMEDIATE`: se já existe registro com o mesmo
        (código, marca — `IS`, ausente só casa com ausente —, URL_Normalizada, hash),
        devolve `(existente mais recente, False)` sem inserir; senão insere e devolve
        `(novo, True)`.

        Erros: hash inexistente → `ConteudoNaoEncontradoError` (nada é gravado);
        qualquer `sqlite3.Error` → rollback e `ErroArmazenamento`.
        """
        coletado_em = _instante_para_texto(registro.coletado_em)
        agora = self._agora()
        try:
            with closing(self._conectar()) as conn, self._transacao(conn):
                existe_conteudo = conn.execute(
                    "SELECT 1 FROM conteudo_pagina WHERE hash_conteudo = ?",
                    (hash_conteudo,),
                ).fetchone()
                if existe_conteudo is None:
                    raise ConteudoNaoEncontradoError(hash_conteudo)
                existente = conn.execute(
                    """
                    SELECT * FROM registro_coleta
                    WHERE codigo_peca = ? AND marca_peca IS ?
                      AND url_normalizada = ? AND hash_conteudo = ?
                    ORDER BY coletado_em DESC, id DESC
                    LIMIT 1
                    """,
                    (
                        registro.codigo_peca,
                        registro.marca_peca,
                        registro.url_normalizada,
                        hash_conteudo,
                    ),
                ).fetchone()
                if existente is not None:
                    return _linha_para_registro(existente), False
                novo_id = _inserir_registro(
                    conn,
                    registro,
                    hash_conteudo=hash_conteudo,
                    reaproveitado=True,
                    coletado_em=coletado_em,
                    registrado_em=agora,
                )
                linha = conn.execute(
                    "SELECT * FROM registro_coleta WHERE id = ?", (novo_id,)
                ).fetchone()
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao registrar reaproveitamento do conteúdo {hash_conteudo}: "
                f"{type(exc).__name__}"
            ) from exc
        return _linha_para_registro(linha), True

    # -- reputação de domínio -------------------------------------------------

    def registrar_tentativa(
        self,
        dominio: str,
        camada: str,
        *,
        sucesso: bool,
        motivo: str | None = None,
        status_http: int | None = None,
    ) -> None:
        """Grava uma tentativa de fetch em `tentativas_fetch_dominio`.

        `dominio` é normalizado (``strip`` + casefold, sem ponto final); vazio é
        erro de uso (`ValueError`). `camada` não é validada em Python: o `CHECK`
        do schema recusa valores fora de ``CAMADAS_FETCH`` e a falha vira
        `ErroArmazenamento`, como qualquer `sqlite3.Error`. `tentado_em` = agora.
        """
        normalizado = normalizar_dominio(dominio)
        if not normalizado:
            raise ValueError("dominio vazio")
        agora = self._agora()
        try:
            with closing(self._conectar()) as conn, self._transacao(conn):
                conn.execute(
                    """
                    INSERT INTO tentativas_fetch_dominio
                        (dominio, camada, sucesso, motivo, status_http, tentado_em)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (normalizado, camada, int(bool(sucesso)), motivo, status_http, agora),
                )
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao registrar tentativa de fetch: {type(exc).__name__}"
            ) from exc

    def ultimas_tentativas(
        self, dominio: str, camada: str, limite: int = LIMITE_FALHAS_CONSECUTIVAS_PADRAO
    ) -> tuple[TentativaFetch, ...]:
        """Até `limite` tentativas do (domínio, camada), mais recentes primeiro (maior `id`)."""
        if limite <= 0:
            return ()
        try:
            with closing(self._conectar()) as conn:
                linhas = conn.execute(
                    """
                    SELECT * FROM tentativas_fetch_dominio
                    WHERE dominio = ? AND camada = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (normalizar_dominio(dominio), camada, limite),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao consultar tentativas de fetch: {type(exc).__name__}"
            ) from exc
        return tuple(
            TentativaFetch(
                id=linha["id"],
                dominio=linha["dominio"],
                camada=linha["camada"],
                sucesso=bool(linha["sucesso"]),
                motivo=linha["motivo"],
                status_http=linha["status_http"],
                tentado_em=_texto_para_instante(linha["tentado_em"]),
            )
            for linha in linhas
        )

    def dominio_excluido(
        self, dominio: str, camada: str, limite: int = LIMITE_FALHAS_CONSECUTIVAS_PADRAO
    ) -> bool:
        """True sse as `limite` tentativas mais recentes do (domínio, camada) falharam.

        Mesma regra de `coleta_paginas.reputacao.dominio_excluido`, reimplementada
        aqui porque `db/` não importa o domínio `coleta_paginas` (direção de
        dependências verificada em `tests/test_coleta_fronteiras.py`).
        """
        if limite < 1:
            raise ValueError("limite precisa ser >= 1")
        ultimas = self.ultimas_tentativas(dominio, camada, limite)
        return len(ultimas) >= limite and not any(t.sucesso for t in ultimas)

    # -- consulta e inspeção -------------------------------------------------

    def registros_da_peca(
        self, codigo_peca: str, marca_peca: str | None
    ) -> list[RegistroColeta]:
        """Registros_Coleta da peça, com igualdade exata de código e marca (Req 8.2).

        A comparação é caractere a caractere (colação binária do SQLite), sem
        normalizar caixa, espaços ou acentos; marca `None` casa só com marca ausente
        (`IS`). Ordem: confiança (`alta` antes de `media`, por `CASE` explícito),
        depois `coletado_em` crescente e, em empate, `id` crescente (ordem de
        gravação). Sem correspondência, devolve lista vazia (Req 8.3).
        """
        try:
            with closing(self._conectar()) as conn:
                linhas = conn.execute(
                    """
                    SELECT * FROM registro_coleta
                    WHERE codigo_peca = ? AND marca_peca IS ?
                    ORDER BY CASE confianca WHEN 'alta' THEN 0 ELSE 1 END,
                             coletado_em, id
                    """,
                    (codigo_peca, marca_peca),
                ).fetchall()
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao consultar registros da peça: {type(exc).__name__}"
            ) from exc
        return [_linha_para_registro(linha) for linha in linhas]

    def listar_pecas(self) -> list[ResumoPeca]:
        """Uma linha por (código, marca) com ao menos um Registro_Coleta (Req 8.4).

        Marca ausente forma um grupo próprio, distinto de qualquer marca informada
        (o `GROUP BY` do SQLite agrupa `NULL` com `NULL`). Ordem determinística:
        `codigo_peca` e depois `marca_peca`, ambos pela colação binária do SQLite,
        com marca ausente antes das informadas. Vazio quando não há registros.
        """
        try:
            with closing(self._conectar()) as conn:
                linhas = conn.execute(
                    """
                    SELECT codigo_peca, marca_peca,
                           COUNT(*) AS quantidade_registros,
                           MAX(coletado_em) AS ultima_coleta
                    FROM registro_coleta
                    GROUP BY codigo_peca, marca_peca
                    ORDER BY codigo_peca, marca_peca IS NOT NULL, marca_peca
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao listar peças: {type(exc).__name__}"
            ) from exc
        return [
            ResumoPeca(
                codigo_peca=linha["codigo_peca"],
                marca_peca=linha["marca_peca"],
                quantidade_registros=linha["quantidade_registros"],
                ultima_coleta=_texto_para_instante(linha["ultima_coleta"]),
            )
            for linha in linhas
        ]

    def ler_texto(self, hash_conteudo: str) -> str:
        """Conteúdo decodificado como texto (Req 8.8); hash inexistente → erro (Req 8.9).

        Usa o charset do Registro_Coleta mais recente do hash (maior `coletado_em`
        e, em empate, maior `id`), com `errors="replace"`. Cai em UTF-8 com
        `errors="replace"` quando: não há registro para o hash (conteúdo gravado só
        por `armazenar_conteudo`); o charset é ausente; o codec é desconhecido; o
        codec existe mas não é codificação de texto (`base64`, `rot13`, `zlib`...,
        para os quais `bytes.decode` levanta `LookupError`); ou o codec não aceita
        o tratamento `replace` (ex.: `idna`, que levanta `UnicodeError`).
        """
        dados = self.ler_conteudo(hash_conteudo)
        try:
            with closing(self._conectar()) as conn:
                linha = conn.execute(
                    """
                    SELECT charset FROM registro_coleta
                    WHERE hash_conteudo = ?
                    ORDER BY coletado_em DESC, id DESC
                    LIMIT 1
                    """,
                    (hash_conteudo,),
                ).fetchone()
        except sqlite3.Error as exc:
            raise ErroArmazenamento(
                f"falha ao consultar charset do conteúdo {hash_conteudo}: "
                f"{type(exc).__name__}"
            ) from exc
        charset = linha["charset"] if linha is not None else None
        return _decodificar(dados, charset)

    def exportar_conteudo(self, hash_conteudo: str, destino: str | Path) -> None:
        """Grava em `destino` os bytes originais do conteúdo (Req 8.6, 8.7, 8.9).

        Ordem: lê o conteúdo (hash inexistente → `ConteudoNaoEncontradoError`, sem
        tocar o destino); confere que o SHA-256 recalculado é o hash pedido (senão
        `ErroArmazenamento`); recusa destino existente (`DestinoExistenteError`,
        inclusive symlink quebrado). Os bytes vão para um temporário no mesmo
        diretório, com `fsync`, e são publicados por `os.link`, que falha
        atomicamente se o destino passou a existir e nunca sobrescreve. Se o
        sistema de arquivos não suporta hard link, o fallback é `open(destino,
        "xb")`, removendo o destino criado em caso de falha. O temporário é sempre
        removido; o destino nunca fica parcial. Falhas de E/S → `ErroArmazenamento`.
        """
        dados = self.ler_conteudo(hash_conteudo)
        if hashlib.sha256(dados).hexdigest() != hash_conteudo:
            raise ErroArmazenamento(
                f"falha de integridade: o conteúdo {hash_conteudo} não confere com o hash"
            )
        caminho = Path(destino)
        if os.path.lexists(caminho):
            raise DestinoExistenteError(f"destino já existe: {caminho}")

        tmp: str | None = None
        try:
            try:
                fd, tmp = tempfile.mkstemp(
                    prefix=f".{caminho.name}.", suffix=".tmp", dir=caminho.parent
                )
                with os.fdopen(fd, "wb") as arquivo:
                    arquivo.write(dados)
                    arquivo.flush()
                    os.fsync(arquivo.fileno())
            except OSError as exc:
                raise ErroArmazenamento(
                    f"falha ao gravar temporário da exportação: {type(exc).__name__}"
                ) from exc
            try:
                os.link(tmp, caminho)
            except FileExistsError as exc:
                raise DestinoExistenteError(f"destino já existe: {caminho}") from exc
            except OSError:
                # Sistema de arquivos sem hard link: criação exclusiva direta.
                _gravar_exclusivo(caminho, dados)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass


def _decodificar(dados: bytes, charset: str | None) -> str:
    """Decodifica com `charset` (replace) ou cai em UTF-8 (replace); ver `ler_texto`."""
    if charset:
        try:
            if codecs.lookup(charset)._is_text_encoding:
                return dados.decode(charset, errors="replace")
        except (LookupError, ValueError):
            # LookupError: codec desconhecido ou não textual; ValueError inclui
            # UnicodeError (handler `replace` não suportado) e nomes com NUL.
            pass
    return dados.decode("utf-8", errors="replace")


def _gravar_exclusivo(caminho: Path, dados: bytes) -> None:
    """Cria `caminho` exclusivamente e grava `dados`; remove o arquivo se falhar."""
    try:
        arquivo = open(caminho, "xb")
    except FileExistsError as exc:
        raise DestinoExistenteError(f"destino já existe: {caminho}") from exc
    except OSError as exc:
        raise ErroArmazenamento(
            f"falha ao criar destino da exportação: {type(exc).__name__}"
        ) from exc
    try:
        with arquivo:
            arquivo.write(dados)
            arquivo.flush()
            os.fsync(arquivo.fileno())
    except BaseException as exc:
        try:
            os.unlink(caminho)
        except FileNotFoundError:
            pass
        if isinstance(exc, OSError):
            raise ErroArmazenamento(
                f"falha ao gravar destino da exportação: {type(exc).__name__}"
            ) from exc
        raise


def _inserir_registro(
    conn: sqlite3.Connection,
    registro: NovoRegistroColeta,
    *,
    hash_conteudo: str,
    reaproveitado: bool,
    coletado_em: str,
    registrado_em: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO registro_coleta (
            codigo_peca, marca_peca, url_original, url_normalizada, url_final, dominio,
            confianca, codigo_confirmado, marca_confirmada, nome_reforcado,
            motivo_decisao, status_http, content_type, charset, reaproveitado,
            hash_conteudo, coletado_em, registrado_em
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            registro.codigo_peca,
            registro.marca_peca,
            registro.url_original,
            registro.url_normalizada,
            registro.url_final,
            registro.dominio,
            str(registro.confianca),
            int(registro.codigo_confirmado),
            int(registro.marca_confirmada),
            int(registro.nome_reforcado),
            registro.motivo_decisao,
            registro.status_http,
            registro.content_type,
            registro.charset,
            int(reaproveitado),
            hash_conteudo,
            coletado_em,
            registrado_em,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _linha_para_registro(linha: sqlite3.Row) -> RegistroColeta:
    return RegistroColeta(
        id=linha["id"],
        codigo_peca=linha["codigo_peca"],
        marca_peca=linha["marca_peca"],
        url_original=linha["url_original"],
        url_normalizada=linha["url_normalizada"],
        url_final=linha["url_final"],
        dominio=linha["dominio"],
        confianca=linha["confianca"],
        codigo_confirmado=bool(linha["codigo_confirmado"]),
        marca_confirmada=bool(linha["marca_confirmada"]),
        nome_reforcado=bool(linha["nome_reforcado"]),
        motivo_decisao=linha["motivo_decisao"],
        status_http=linha["status_http"],
        content_type=linha["content_type"],
        charset=linha["charset"],
        reaproveitado=bool(linha["reaproveitado"]),
        hash_conteudo=linha["hash_conteudo"],
        coletado_em=_texto_para_instante(linha["coletado_em"]),
        registrado_em=_texto_para_instante(linha["registrado_em"]),
    )


def _como_bytes(conteudo: bytes) -> bytes:
    if isinstance(conteudo, bytes):
        return conteudo
    if isinstance(conteudo, (bytearray, memoryview)):
        return bytes(conteudo)
    raise TypeError(f"conteúdo precisa ser bytes, recebido {type(conteudo).__name__}")


def _inserir_conteudo(
    conn: sqlite3.Connection,
    hash_conteudo: str,
    comprimido: bytes,
    tamanho_bytes: int,
    criado_em: str,
) -> None:
    conn.execute(
        """
        INSERT INTO conteudo_pagina (hash_conteudo, conteudo_gzip, tamanho_bytes, criado_em)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(hash_conteudo) DO NOTHING
        """,
        (hash_conteudo, comprimido, tamanho_bytes, criado_em),
    )
