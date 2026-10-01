"""Teste de fronteira de imports da integração da coleta (html-extract-on-web-search).

Req 12.3: os módulos da Integracao_Coleta, ``pipeline``, ``arbitration`` e
``verification`` não importam, direta ou transitivamente, ``textual`` nem o
pacote ``tui``. ``tui.seletores_execucao`` é puro (sem Textual) e é o único
módulo ``tui.*`` permitido — junto com o pacote pai ``tui``, cujo
``__init__`` é vazio — e só quando ele próprio é importado.

Cada importação roda num subprocesso limpo para não herdar o ``sys.modules``
do pytest, já poluído por outros testes (inclusive os headless da TUI).

Req 12.4: ``tests/test_coleta_fronteiras.py`` continua valendo sem alteração.
Este teste roda na suíte padrão, sem gate de ambiente.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.guarda_rede import guarda_rede_autouse  # noqa: F401

RAIZ = Path(__file__).resolve().parent.parent

MODULOS_CORE = (
    "pipeline",
    "arbitration.nome",
    "arbitration.pesquisa_web",
    "verification.selector",
    "verification.resultados_estruturados",
    "coleta_paginas.integracao",
)
MODULO_SELETORES = "tui.seletores_execucao"
MODULOS_VERIFICADOS = (*MODULOS_CORE, MODULO_SELETORES)

PREFIXOS_PROIBIDOS = ("textual", "tui")
TUI_PERMITIDOS = frozenset({"tui", MODULO_SELETORES})


def _casa_prefixo(modulo: str, prefixo: str) -> bool:
    """Igualdade ou submódulo: ``tui`` casa ``tui`` e ``tui.x``, não ``tuify``."""
    return modulo == prefixo or modulo.startswith(prefixo + ".")


def _modulos_carregados(*modulos: str) -> list[str]:
    """Importa ``modulos`` num interpretador novo e devolve ``sys.modules``."""
    codigo = (
        "import importlib, json, sys\n"
        f"for nome in {list(modulos)!r}:\n"
        "    importlib.import_module(nome)\n"
        "print(json.dumps(sorted(sys.modules)))\n"
    )
    env = {
        chave: valor
        for chave, valor in os.environ.items()
        if not (chave.startswith("ESTAGIARIO_RUN_") or chave == "PYTHONPATH")
    }
    processo = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert processo.returncode == 0, processo.stderr
    return json.loads(processo.stdout.strip().splitlines()[-1])


def _proibidos(carregados: list[str], permitidos: frozenset[str]) -> list[str]:
    return sorted(
        m
        for m in carregados
        if m not in permitidos
        and any(_casa_prefixo(m, p) for p in PREFIXOS_PROIBIDOS)
    )


@pytest.mark.parametrize(
    ("modulo", "prefixo", "esperado"),
    [
        ("textual", "textual", True),
        ("textual.widgets", "textual", True),
        ("textualize", "textual", False),
        ("tui.screens.menu_screen", "tui", True),
        ("tuify", "tui", False),
    ],
)
def test_casa_prefixo(modulo: str, prefixo: str, esperado: bool) -> None:
    assert _casa_prefixo(modulo, prefixo) is esperado


def test_proibidos_permite_so_tui_e_seletores() -> None:
    carregados = ["tui", MODULO_SELETORES, "tui.screens", "textual.app", "pipeline"]

    assert _proibidos(carregados, TUI_PERMITIDOS) == ["textual.app", "tui.screens"]
    assert _proibidos(carregados, frozenset()) == [
        "textual.app",
        "tui",
        "tui.screens",
        MODULO_SELETORES,
    ]


@pytest.mark.parametrize("modulo", MODULOS_VERIFICADOS)
def test_modulo_nao_carrega_frontend(modulo: str) -> None:
    carregados = _modulos_carregados(modulo)

    assert modulo in carregados
    # Módulos do core não podem carregar nada de ``tui``; só a importação de
    # ``tui.seletores_execucao`` admite o próprio módulo e o pacote pai.
    permitidos = TUI_PERMITIDOS if modulo == MODULO_SELETORES else frozenset()
    assert _proibidos(carregados, permitidos) == []


def test_conjunto_nao_carrega_textual_nem_telas() -> None:
    carregados = _modulos_carregados(*MODULOS_VERIFICADOS)

    for modulo in MODULOS_VERIFICADOS:
        assert modulo in carregados
    assert _proibidos(carregados, TUI_PERMITIDOS) == []
