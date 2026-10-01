"""Reputação de domínio: tabela `tentativas_fetch_dominio` do Armazem_Paginas e a
regra pura `coleta_paginas.reputacao.dominio_excluido`. SQLite em tmp_path."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coleta_paginas.reputacao import LIMITE_FALHAS_CONSECUTIVAS, dominio_excluido
from db.armazem_paginas import ArmazemPaginas, ErroArmazenamento, TentativaFetch
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401


class RelogioFixo:
    def __init__(self) -> None:
        self.agora = datetime(2025, 3, 1, 12, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        self.agora += timedelta(seconds=1)
        return self.agora


@pytest.fixture
def armazem(tmp_path: Path) -> ArmazemPaginas:
    return ArmazemPaginas(
        tmp_path / "paginas.db",
        caminho_rule_store=tmp_path / "estagiario.db",
        relogio=RelogioFixo(),
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA_ANTIGO = """
CREATE TABLE conteudo_pagina (
    hash_conteudo  TEXT PRIMARY KEY CHECK (length(hash_conteudo) = 64),
    conteudo_gzip  BLOB NOT NULL,
    tamanho_bytes  INTEGER NOT NULL CHECK (tamanho_bytes >= 0),
    criado_em      TEXT NOT NULL
);
INSERT INTO conteudo_pagina VALUES ('%s', x'00', 1, '2025-01-01T00:00:00.000000Z');
""" % ("a" * 64)


def test_schema_migra_arquivo_antigo_sem_perda_e_e_idempotente(tmp_path: Path) -> None:
    caminho = tmp_path / "paginas.db"
    with sqlite3.connect(caminho) as conn:
        conn.executescript(_SCHEMA_ANTIGO)

    rule_store = tmp_path / "estagiario.db"
    armazem = ArmazemPaginas(caminho, caminho_rule_store=rule_store)
    armazem.registrar_tentativa("mercadocar.com.br", "urllib", sucesso=False, motivo="status_http", status_http=403)
    ArmazemPaginas(caminho, caminho_rule_store=rule_store)  # reaplica o schema

    with sqlite3.connect(caminho) as conn:
        assert conn.execute("SELECT COUNT(*) FROM conteudo_pagina").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM tentativas_fetch_dominio").fetchone() == (1,)
        indices = {
            linha[1] for linha in conn.execute("PRAGMA index_list('tentativas_fetch_dominio')")
        }
    assert "idx_tentativas_fetch_dominio" in indices


def test_check_rejeita_camada_invalida(armazem: ArmazemPaginas) -> None:
    with pytest.raises(ErroArmazenamento):
        armazem.registrar_tentativa("mercadocar.com.br", "playwright", sucesso=True)
    with sqlite3.connect(armazem.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tentativas_fetch_dominio (dominio, camada, sucesso, tentado_em) "
                "VALUES ('x.com', 'urllib', 2, 't')"
            )
    assert armazem.ultimas_tentativas("mercadocar.com.br", "playwright") == ()


def test_dominio_vazio_e_erro_de_uso(armazem: ArmazemPaginas) -> None:
    with pytest.raises(ValueError):
        armazem.registrar_tentativa("  . ", "urllib", sucesso=True)


# ---------------------------------------------------------------------------
# Registrar / ler
# ---------------------------------------------------------------------------


def test_registrar_e_ler_mais_recentes_primeiro(armazem: ArmazemPaginas) -> None:
    armazem.registrar_tentativa(" MercadoCar.com.br. ", "urllib", sucesso=True, status_http=200)
    armazem.registrar_tentativa("mercadocar.com.br", "urllib", sucesso=False, motivo="status_http", status_http=403)
    armazem.registrar_tentativa("mercadocar.com.br", "urllib", sucesso=False, motivo="timeout")
    armazem.registrar_tentativa("mercadocar.com.br", "urllib", sucesso=False, motivo="rede")

    ultimas = armazem.ultimas_tentativas("MERCADOCAR.com.br", "urllib")

    assert [(t.sucesso, t.motivo, t.status_http) for t in ultimas] == [
        (False, "rede", None),
        (False, "timeout", None),
        (False, "status_http", 403),
    ]
    assert all(isinstance(t, TentativaFetch) for t in ultimas)
    assert all(t.dominio == "mercadocar.com.br" and t.camada == "urllib" for t in ultimas)
    assert ultimas[0].tentado_em > ultimas[1].tentado_em > ultimas[2].tentado_em
    assert ultimas[0].tentado_em.tzinfo is not None
    assert len(armazem.ultimas_tentativas("mercadocar.com.br", "urllib", limite=10)) == 4
    assert armazem.ultimas_tentativas("mercadocar.com.br", "urllib", limite=0) == ()


# ---------------------------------------------------------------------------
# Regra de exclusão
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class T:
    sucesso: bool


@pytest.mark.parametrize(
    ("historico", "esperado"),
    [
        ([], False),
        ([T(False)], False),
        ([T(False), T(False)], False),
        ([T(False), T(False), T(False)], True),
        ([T(False), T(False), T(False), T(True)], True),  # sucesso antigo não conta
        ([T(False), T(True), T(False), T(False)], False),  # sucesso no meio zera
        ([T(True), T(False), T(False)], False),
    ],
)
def test_regra_pura(historico: list[T], esperado: bool) -> None:
    assert LIMITE_FALHAS_CONSECUTIVAS == 3
    assert dominio_excluido(historico) is esperado


def test_regra_pura_limite_invalido() -> None:
    with pytest.raises(ValueError):
        dominio_excluido([], limite=0)


def _registrar(armazem: ArmazemPaginas, camada: str, *sucessos: bool) -> None:
    for sucesso in sucessos:  # em ordem cronológica
        armazem.registrar_tentativa("mercadocar.com.br", camada, sucesso=sucesso)


@pytest.mark.parametrize("falhas", [0, 1, 2, 3])
def test_armazem_exclui_so_com_tres_falhas(armazem: ArmazemPaginas, falhas: int) -> None:
    _registrar(armazem, "urllib", *([False] * falhas))

    esperado = falhas >= 3
    assert armazem.dominio_excluido("mercadocar.com.br", "urllib") is esperado
    assert dominio_excluido(armazem.ultimas_tentativas("mercadocar.com.br", "urllib")) is esperado


def test_sucesso_no_meio_zera(armazem: ArmazemPaginas) -> None:
    _registrar(armazem, "urllib", False, False, True, False, False)
    assert armazem.dominio_excluido("mercadocar.com.br", "urllib") is False
    _registrar(armazem, "urllib", False)
    assert armazem.dominio_excluido("mercadocar.com.br", "urllib") is True


def test_camadas_independentes(armazem: ArmazemPaginas) -> None:
    _registrar(armazem, "urllib", False, False, False)
    _registrar(armazem, "playwright_stealth", True)

    assert armazem.dominio_excluido("mercadocar.com.br", "urllib") is True
    assert armazem.dominio_excluido("mercadocar.com.br", "playwright_stealth") is False
    assert armazem.dominio_excluido("outro.com.br", "urllib") is False
