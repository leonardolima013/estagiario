"""Sanidade da fixture de isolamento da feature html-extract-on-web-search (Req 12.5, 12.6)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import config
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import (
    PAGINAS_DB_RAIZ,
    AmbienteColeta,
    estado_arquivo,
    isolamento_coleta_autouse,  # noqa: F401
    verificar_inalterado,
)


def test_variaveis_fixadas_e_armazem_no_tmp(
    isolamento_coleta_autouse: AmbienteColeta, tmp_path: Path
):
    amb = isolamento_coleta_autouse
    assert os.environ["ESTAGIARIO_COLETA_HABILITADA"] == "1"
    assert config.web_verification_metodo() == "serper"
    assert config.paginas_db_path() == tmp_path / "paginas.db" == amb.paginas_db_path
    assert config.paginas_db_path() != PAGINAS_DB_RAIZ


def test_teste_pode_sobrescrever_valores(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "playwright")
    assert config.web_verification_metodo() == "playwright"


def test_verificar_inalterado_detecta_criacao_e_alteracao(tmp_path: Path):
    alvo = tmp_path / "protegido.db"
    antes = estado_arquivo(alvo)
    verificar_inalterado(antes, alvo)  # ausente → ausente: ok

    alvo.write_bytes(b"x")
    with pytest.raises(pytest.fail.Exception):
        verificar_inalterado(antes, alvo)

    antes = estado_arquivo(alvo)
    alvo.write_bytes(b"xyz")
    with pytest.raises(pytest.fail.Exception):
        verificar_inalterado(antes, alvo)
