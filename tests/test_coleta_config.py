"""Testes unitários das funções de configuração da coleta de páginas
(spec html-extract-save, Req 5.4, 7.10, 9.5).

Todas as variáveis são controladas por ``monkeypatch``: os testes não dependem
dos valores do ``.env`` do desenvolvedor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import config
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401


# --- paginas_db_path (Req 7.10) ---


def test_paginas_db_path_padrao_fica_em_db_paginas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESTAGIARIO_PAGINAS_DB_PATH", raising=False)

    caminho = config.paginas_db_path()

    assert caminho == config.PROJECT_ROOT / "db" / "paginas.db"


def test_paginas_db_path_padrao_distinto_do_rule_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESTAGIARIO_PAGINAS_DB_PATH", raising=False)
    monkeypatch.delenv("ESTAGIARIO_DB_PATH", raising=False)

    assert config.paginas_db_path() != config.rule_store_db_path()


def test_paginas_db_path_override_por_variavel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destino = tmp_path / "outro" / "paginas.sqlite"
    monkeypatch.setenv("ESTAGIARIO_PAGINAS_DB_PATH", str(destino))

    caminho = config.paginas_db_path()

    assert isinstance(caminho, Path)
    assert caminho == destino


def test_paginas_db_path_override_vazio_usa_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTAGIARIO_PAGINAS_DB_PATH", "")

    assert config.paginas_db_path() == config.PROJECT_ROOT / "db" / "paginas.db"


# --- coleta_timeout (Req 5.4) ---


def test_coleta_timeout_ausente_devolve_15(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESTAGIARIO_COLETA_TIMEOUT", raising=False)

    assert config.coleta_timeout() == 15.0


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [("1", 1.0), ("120", 120.0), ("30", 30.0), ("2.5", 2.5), (" 45 ", 45.0), ("1e1", 10.0)],
)
def test_coleta_timeout_valores_validos_sao_mantidos(
    monkeypatch: pytest.MonkeyPatch, bruto: str, esperado: float
) -> None:
    monkeypatch.setenv("ESTAGIARIO_COLETA_TIMEOUT", bruto)

    assert config.coleta_timeout() == esperado


@pytest.mark.parametrize(
    "bruto",
    ["", "abc", "10s", "nan", "NaN", "inf", "-inf", "0", "0.999", "-5", "120.0001", "121", "1000"],
)
def test_coleta_timeout_invalido_devolve_15_sem_clamp(
    monkeypatch: pytest.MonkeyPatch, bruto: str
) -> None:
    # Fora do intervalo não é aproximado ao limite (diferente de serper_timeout).
    monkeypatch.setenv("ESTAGIARIO_COLETA_TIMEOUT", bruto)

    assert config.coleta_timeout() == 15.0


# --- getters brutos (Req 9.5): sem validação em config.py ---


@pytest.mark.parametrize(
    ("funcao", "variavel"),
    [
        (config.coleta_teto_aceitos, "ESTAGIARIO_COLETA_TETO_ACEITOS"),
        (config.coleta_janela_reuso_dias, "ESTAGIARIO_COLETA_JANELA_REUSO_DIAS"),
    ],
)
def test_getter_bruto_ausente_devolve_none(
    monkeypatch: pytest.MonkeyPatch, funcao, variavel: str
) -> None:
    monkeypatch.delenv(variavel, raising=False)

    assert funcao() is None


@pytest.mark.parametrize(
    ("funcao", "variavel"),
    [
        (config.coleta_teto_aceitos, "ESTAGIARIO_COLETA_TETO_ACEITOS"),
        (config.coleta_janela_reuso_dias, "ESTAGIARIO_COLETA_JANELA_REUSO_DIAS"),
    ],
)
@pytest.mark.parametrize("bruto", ["5", " 7 ", "", "abc", "2.0", "-1"])
def test_getter_bruto_presente_devolve_valor_sem_validar(
    monkeypatch: pytest.MonkeyPatch, funcao, variavel: str, bruto: str
) -> None:
    monkeypatch.setenv(variavel, bruto)

    assert funcao() == bruto
