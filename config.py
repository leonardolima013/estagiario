"""Configuração central: caminho do rule store e credenciais da réplica de dev.

O `.env` deve apontar para uma réplica isolada, nunca para produção — ver SPEC.md §4.4/§6.1.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent


def rule_store_db_path() -> Path:
    """Caminho do arquivo SQLite do rule store.

    Sobrescrevível via ESTAGIARIO_DB_PATH; por padrão vive em db/estagiario.db
    na raiz do projeto.
    """
    override = os.environ.get("ESTAGIARIO_DB_PATH")
    if override:
        return Path(override)
    return PROJECT_ROOT / "db" / "estagiario.db"


def replica_connection_params() -> dict[str, str]:
    """Parâmetros de conexão com a réplica de dev (nunca produção)."""
    return {
        "host": os.environ["DB_HOST"],
        "port": os.environ["DB_PORT"],
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
    }


def anthropic_api_key() -> str:
    """Chave da API Anthropic, usada pelo LLMProvider (SPEC.md §5 — multi-provider)."""
    try:
        return os.environ["ANTHROPIC_API_KEY"]
    except KeyError as exc:
        raise RuntimeError(
            "ANTHROPIC_API_KEY não configurada no .env — necessária para particionar_grupo."
        ) from exc


def llm_model() -> str:
    """Modelo usado pelo LLMProvider. Sobrescrevível via ESTAGIARIO_LLM_MODEL.

    Default é Haiku — fase de testes, prioriza custo baixo sobre qualidade máxima.
    Trocar pra um modelo maior (ex: Sonnet) quando a precisão do particionamento
    virar o gargalo, via ESTAGIARIO_LLM_MODEL no .env, sem mexer em código.
    """
    return os.environ.get("ESTAGIARIO_LLM_MODEL", "claude-haiku-4-5-20251001")


def web_verification_model() -> str:
    """Modelo do sub-agente de verificação web de nomenclatura
    (SPEC-verificacao-web-nomenclatura.md) — knob independente de
    ESTAGIARIO_LLM_MODEL, sobrescrevível via ESTAGIARIO_WEB_VERIFICATION_MODEL.

    Default é Haiku, mesma postura de custo do resto do projeto (uso é de baixo
    volume: só dispara quando há divergência real de `name` num subcluster) —
    decisão confirmada com o usuário, não inferida.
    """
    return os.environ.get("ESTAGIARIO_WEB_VERIFICATION_MODEL", "claude-haiku-4-5-20251001")


def web_verification_max_passos() -> int:
    """Orçamento de passos do sub-agente de verificação web antes de retornar
    inconclusivo (SPEC-verificacao-web-nomenclatura.md §7). Sobrescrevível via
    ESTAGIARIO_WEB_VERIFICATION_MAX_PASSOS.
    """
    return int(os.environ.get("ESTAGIARIO_WEB_VERIFICATION_MAX_PASSOS", "15"))


def web_verification_startup_timeout() -> float:
    """Timeout (segundos) pra subir o servidor MCP do Playwright e completar o
    handshake inicial (initialize + list_tools). Sem isso, uma falha de startup
    (ex: binário do Chromium ausente, download travado) trava a thread da TUI
    indefinidamente sem nenhum feedback — foi exatamente isso que aconteceu na
    primeira execução real desta skill. Sobrescrevível via
    ESTAGIARIO_WEB_VERIFICATION_STARTUP_TIMEOUT.
    """
    return float(os.environ.get("ESTAGIARIO_WEB_VERIFICATION_STARTUP_TIMEOUT", "60"))


def web_verification_tool_timeout() -> float:
    """Timeout (segundos) por chamada individual de tool (ex: uma navegação que
    trava esperando um seletor que nunca aparece). Sobrescrevível via
    ESTAGIARIO_WEB_VERIFICATION_TOOL_TIMEOUT.
    """
    return float(os.environ.get("ESTAGIARIO_WEB_VERIFICATION_TOOL_TIMEOUT", "60"))


def playwright_mcp_command() -> list[str]:
    """Comando pra subir o servidor MCP oficial do Playwright via subprocess
    Node (stdio) — sobrescrevível por inteiro via ESTAGIARIO_PLAYWRIGHT_MCP_COMMAND
    (string, separada por espaço), ou só a versão via ESTAGIARIO_PLAYWRIGHT_MCP_VERSION.

    Requer Node.js/npx disponível no sistema (não é dependência Python) e os
    binários de navegador do Playwright já baixados — ver nota de setup no
    CLAUDE.md antes de rodar esta skill pela primeira vez.
    """
    override = os.environ.get("ESTAGIARIO_PLAYWRIGHT_MCP_COMMAND")
    if override:
        return override.split()
    versao = os.environ.get("ESTAGIARIO_PLAYWRIGHT_MCP_VERSION", "0.0.81")
    return ["npx", "-y", f"@playwright/mcp@{versao}"]
