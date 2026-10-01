"""Smoke de fronteira e de schema da feature stealth-fallback-integration.

Roda na suíte padrão (Req 12.3), sem rede, banco, LLM nem navegador.

1. Import preguiçoso do Patchright e core sem UI (Req 9.4): importar
   ``pipeline``, ``coleta_paginas.coletor``, ``coleta_paginas.integracao`` e
   ``tui.seletores_execucao`` num subprocesso limpo não carrega nenhum módulo
   ``patchright*``, ``textual*`` nem ``tui.screens*``. O subprocesso evita o
   ``sys.modules`` do pytest, já poluído por outros testes. O ambiente do
   subprocesso é controlado: sem ``ESTAGIARIO_RUN_*`` nem ``PYTHONPATH``, e com
   ``dotenv.load_dotenv`` neutralizado antes do primeiro import, para que o
   ``.env`` local não reinjete gates de integração.
2. Schema inalterado (Req 4.5, decisão Q8): ``VERSAO_SCHEMA == 1`` e
   ``PRAGMA table_info`` da tabela de registros de coleta igual a um
   instantâneo literal, num Armazem_Paginas criado em ``tmp_path``.

``tests/test_coleta_fronteiras.py`` continua valendo sem alteração.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from db.armazem_paginas import VERSAO_SCHEMA, ArmazemPaginas
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401

RAIZ = Path(__file__).resolve().parent.parent

MODULOS_IMPORTADOS = (
    "pipeline",
    "coleta_paginas.coletor",
    "coleta_paginas.integracao",
    "tui.seletores_execucao",
)

PREFIXOS_PROIBIDOS = ("patchright", "textual", "tui.screens")

# Instantâneo literal de ``PRAGMA table_info(registro_coleta)``:
# (cid, name, type, notnull, dflt_value, pk).
TABLE_INFO_REGISTRO_COLETA = [
    (0, "id", "INTEGER", 0, None, 1),
    (1, "codigo_peca", "TEXT", 1, None, 0),
    (2, "marca_peca", "TEXT", 0, None, 0),
    (3, "url_original", "TEXT", 1, None, 0),
    (4, "url_normalizada", "TEXT", 1, None, 0),
    (5, "url_final", "TEXT", 1, None, 0),
    (6, "dominio", "TEXT", 1, None, 0),
    (7, "confianca", "TEXT", 1, None, 0),
    (8, "codigo_confirmado", "INTEGER", 1, None, 0),
    (9, "marca_confirmada", "INTEGER", 1, None, 0),
    (10, "nome_reforcado", "INTEGER", 1, None, 0),
    (11, "motivo_decisao", "TEXT", 1, None, 0),
    (12, "status_http", "INTEGER", 1, None, 0),
    (13, "content_type", "TEXT", 1, None, 0),
    (14, "charset", "TEXT", 0, None, 0),
    (15, "reaproveitado", "INTEGER", 1, None, 0),
    (16, "hash_conteudo", "TEXT", 1, None, 0),
    (17, "coletado_em", "TEXT", 1, None, 0),
    (18, "registrado_em", "TEXT", 1, None, 0),
]


def _casa_prefixo(modulo: str, prefixo: str) -> bool:
    """Igualdade ou submódulo: ``textual`` casa ``textual.app``, não ``textualx``."""
    return modulo == prefixo or modulo.startswith(prefixo + ".")


def _ambiente_controlado() -> dict[str, str]:
    """``os.environ`` sem gates ``ESTAGIARIO_RUN_*`` e sem ``PYTHONPATH``."""
    return {
        chave: valor
        for chave, valor in os.environ.items()
        if not (chave.startswith("ESTAGIARIO_RUN_") or chave == "PYTHONPATH")
    }


def _importar_em_subprocesso(*modulos: str) -> dict[str, list[str]]:
    """Importa ``modulos`` num interpretador novo; devolve módulos e gates vistos."""
    codigo = (
        "import dotenv\n"
        "dotenv.load_dotenv = lambda *a, **k: False\n"
        "import importlib, json, os, sys\n"
        f"for nome in {list(modulos)!r}:\n"
        "    importlib.import_module(nome)\n"
        "gates = sorted(k for k in os.environ if k.startswith('ESTAGIARIO_RUN_'))\n"
        "print(json.dumps({'modulos': sorted(sys.modules), 'gates': gates}))\n"
    )
    processo = subprocess.run(
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=_ambiente_controlado(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert processo.returncode == 0, processo.stderr
    return json.loads(processo.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("modulo", "prefixo", "esperado"),
    [
        ("patchright", "patchright", True),
        ("patchright.sync_api", "patchright", True),
        ("textual.app", "textual", True),
        ("textualx", "textual", False),
        ("tui.screens", "tui.screens", True),
        ("tui.screens.executar_caso_screen", "tui.screens", True),
        ("tui.seletores_execucao", "tui.screens", False),
    ],
)
def test_casa_prefixo(modulo: str, prefixo: str, esperado: bool) -> None:
    assert _casa_prefixo(modulo, prefixo) is esperado


def test_ambiente_controlado_sem_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ESTAGIARIO_RUN_STEALTH_TESTS", "1")
    monkeypatch.setenv("PYTHONPATH", "/inexistente")

    env = _ambiente_controlado()

    assert not any(k.startswith("ESTAGIARIO_RUN_") for k in env)
    assert "PYTHONPATH" not in env


def test_core_nao_carrega_patchright_textual_nem_telas() -> None:
    resultado = _importar_em_subprocesso(*MODULOS_IMPORTADOS)
    carregados = resultado["modulos"]

    for modulo in MODULOS_IMPORTADOS:
        assert modulo in carregados
    # O fallback stealth faz parte do grafo de imports do coletor; o Patchright não.
    assert "tools.buscador_stealth" in carregados
    proibidos = sorted(
        m for m in carregados if any(_casa_prefixo(m, p) for p in PREFIXOS_PROIBIDOS)
    )
    assert proibidos == []
    assert resultado["gates"] == []


def test_schema_do_registro_de_coleta_inalterado(tmp_path: Path) -> None:
    caminho = tmp_path / "armazem" / "paginas.db"
    ArmazemPaginas(caminho, caminho_rule_store=tmp_path / "estagiario.db")

    with closing(sqlite3.connect(caminho)) as conn:
        colunas = [
            tuple(linha) for linha in conn.execute("PRAGMA table_info(registro_coleta)")
        ]
        (versao,) = conn.execute("PRAGMA user_version").fetchone()

    assert VERSAO_SCHEMA == 1
    assert versao == VERSAO_SCHEMA
    assert colunas == TABLE_INFO_REGISTRO_COLETA
