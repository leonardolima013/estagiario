"""Configuração do fallback stealth: allowlist e diretório do perfil."""

from __future__ import annotations

from pathlib import Path

import pytest

import config

VAR = "ESTAGIARIO_COLETA_DOMINIOS_STEALTH"


def test_allowlist_ausente_usa_padrao(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VAR, raising=False)
    assert config.coleta_dominios_stealth() == frozenset({"mercadocar.com.br"})


@pytest.mark.parametrize("valor", ["", "   ", " , ,, "])
def test_allowlist_vazia_desliga(monkeypatch: pytest.MonkeyPatch, valor: str) -> None:
    monkeypatch.setenv(VAR, valor)
    assert config.coleta_dominios_stealth() == frozenset()


def test_allowlist_normaliza_espacos_caixa_e_ponto_final(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VAR, " MercadoCar.com.br. ,  Loja.Exemplo.COM , ,mercadocar.com.br")
    assert config.coleta_dominios_stealth() == frozenset({"mercadocar.com.br", "loja.exemplo.com"})


def test_profile_dir_padrao_e_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ESTAGIARIO_STEALTH_PROFILE_DIR", raising=False)
    assert config.stealth_profile_dir() == config.PROJECT_ROOT / "browserscan" / "chrome-profile"
    monkeypatch.setenv("ESTAGIARIO_STEALTH_PROFILE_DIR", str(tmp_path / "perfil"))
    assert config.stealth_profile_dir() == tmp_path / "perfil"
