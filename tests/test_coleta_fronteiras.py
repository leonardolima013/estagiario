"""Testes de fronteira da coleta de páginas (html-extract-save).

Rodam na suíte padrão, sem rede, banco, LLM nem navegador.

1. Imports (Req 9.1, 9.2, 9.3): os módulos da coleta, importados num
   subprocesso limpo, não carregam LLM, SDKs, MCP, UI, PostgreSQL, a consulta à
   réplica, o RuleStore, a memória nem os protótipos. O subprocesso evita o
   ``sys.modules`` do pytest, já poluído por outros testes.
2. Direção de dependências: ``tools.buscador_paginas`` e ``db.armazem_paginas``
   não importam ``coleta_paginas`` (cada um num subprocesso próprio).
3. Dono do schema (Req 7.9): os nomes ``conteudo_pagina``, ``registro_coleta``,
   ``vw_paginas_por_peca`` e ``tentativas_fetch_dominio`` só aparecem em
   ``db/armazem_paginas.py`` entre os ``.py`` do projeto. Ficam fora da
   varredura ``tests/`` (os testes podem montar
   triggers e snapshots), ``venv/``, ``prototipos/`` (referência histórica) e
   diretórios de artefato (ocultos, ``__pycache__``, ``output/``, ``logs/``).
   ``context_files/`` e ``scripts/`` entram: são código do repositório e não
   devem manipular o schema de páginas diretamente.

Req 9.7: estes testes fazem parte da suíte padrão (sem gate de ambiente).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.guarda_rede import guarda_rede_autouse  # noqa: F401

RAIZ = Path(__file__).resolve().parent.parent

MODULOS_COLETA = (
    "coleta_paginas.modelos",
    "coleta_paginas.url",
    "coleta_paginas.selecao",
    "coleta_paginas.coletor",
    "coleta_paginas.reputacao",
    "tools.buscador_paginas",
    "tools.buscador_stealth",
    "db.armazem_paginas",
)

PREFIXOS_PROIBIDOS = (
    "llm",
    "anthropic",
    "mcp",
    "textual",
    "tui",
    "psycopg",
    "tools.db_query",
    "db.rule_store",
    "memory",
    "prototipos",
)

NOMES_SCHEMA = (
    "conteudo_pagina",
    "registro_coleta",
    "vw_paginas_por_peca",
    "tentativas_fetch_dominio",
)
DONO_SCHEMA = Path("db") / "armazem_paginas.py"

DIRETORIOS_EXCLUIDOS = frozenset(
    {"tests", "venv", "prototipos", "__pycache__", "output", "logs", "node_modules"}
)


def _casa_prefixo(modulo: str, prefixo: str) -> bool:
    """Igualdade ou submódulo: ``mcp`` casa ``mcp`` e ``mcp.x``, não ``mcpfoo``."""
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


@pytest.mark.parametrize(
    ("modulo", "prefixo", "esperado"),
    [
        ("mcp", "mcp", True),
        ("mcp.client", "mcp", True),
        ("mcpfoo", "mcp", False),
        ("memory.x", "memory", True),
        ("memoryview_util", "memory", False),
        ("tools.db_query", "tools.db_query", True),
        ("tools.db_query_extra", "tools.db_query", False),
    ],
)
def test_casa_prefixo(modulo: str, prefixo: str, esperado: bool) -> None:
    assert _casa_prefixo(modulo, prefixo) is esperado


def test_modulos_da_coleta_nao_carregam_dependencias_proibidas() -> None:
    carregados = _modulos_carregados(*MODULOS_COLETA)

    for modulo in MODULOS_COLETA:
        assert modulo in carregados
    proibidos = sorted(
        m for m in carregados if any(_casa_prefixo(m, p) for p in PREFIXOS_PROIBIDOS)
    )
    assert proibidos == []


@pytest.mark.parametrize("modulo", ["tools.buscador_paginas", "db.armazem_paginas"])
def test_bordas_de_io_nao_importam_o_dominio_da_coleta(modulo: str) -> None:
    carregados = _modulos_carregados(modulo)

    assert modulo in carregados
    assert [m for m in carregados if _casa_prefixo(m, "coleta_paginas")] == []


def _arquivos_python_varridos() -> list[Path]:
    arquivos: list[Path] = []
    for diretorio, subdiretorios, nomes in os.walk(RAIZ):
        atual = Path(diretorio)
        subdiretorios[:] = sorted(
            d
            for d in subdiretorios
            if not d.startswith(".") and d not in DIRETORIOS_EXCLUIDOS
        )
        arquivos.extend(atual / n for n in sorted(nomes) if n.endswith(".py"))
    return arquivos


def test_so_o_armazem_conhece_o_schema_de_paginas() -> None:
    padrao = re.compile(r"\b(?:" + "|".join(NOMES_SCHEMA) + r")\b")
    arquivos = _arquivos_python_varridos()
    assert RAIZ / DONO_SCHEMA in arquivos

    violacoes = sorted(
        str(arquivo.relative_to(RAIZ))
        for arquivo in arquivos
        if arquivo != RAIZ / DONO_SCHEMA
        and padrao.search(arquivo.read_text(encoding="utf-8", errors="replace"))
    )

    assert violacoes == []
    # Sanidade: a varredura de fato encontra os nomes no dono do schema.
    dono = (RAIZ / DONO_SCHEMA).read_text(encoding="utf-8")
    assert all(re.search(rf"\b{n}\b", dono) for n in NOMES_SCHEMA)
