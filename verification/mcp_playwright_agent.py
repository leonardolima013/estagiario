"""Ponto de entrada real (DI-facing) da skill de verificação web
(SPEC-verificacao-web-nomenclatura.md) — sobe o servidor MCP oficial do
Playwright (headed, nunca --headless — SPEC §8) via subprocess, dirige o loop
agentico contra a API da Anthropic, e derruba o subprocess ao final.

Sem reaproveitamento de conexão entre chamadas nesta primeira fase — cada
verificação sobe e derruba seu próprio servidor MCP (SPEC §3 já deixa cache/
pool de fora do escopo desta entrega).
"""

from __future__ import annotations

import asyncio
from typing import Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from config import (
    playwright_mcp_command,
    web_verification_max_passos,
    web_verification_startup_timeout,
    web_verification_tool_timeout,
)
from llm.anthropic_agent_provider import AnthropicAgentProvider
from llm.provider import AgentLLMProvider
from verification.agentic_loop import executar_loop_agentico
from verification.mcp_tools import executar_tool_call, listar_tools_anthropic
from verification.models import ResultadoVerificacao
from verification.prompts import montar_system_prompt, montar_user_prompt
from verification.validacao import REPORTAR_RESULTADO_TOOL


class VerificacaoWebTimeoutError(TimeoutError):
    """Subida do servidor MCP do Playwright (spawn + initialize + list_tools) não
    respondeu dentro do timeout configurado — normalmente sinal de que o binário
    do Chromium não está instalado (`npx playwright install chromium`) ou de que
    o comando em playwright_mcp_command() está travando por outro motivo."""


class ModoHeadlessProibidoError(RuntimeError):
    """O comando do Playwright MCP inclui --headless. A fase de testes exige modo
    headed obrigatório (SPEC-verificacao-web-nomenclatura.md §8): o operador precisa
    acompanhar a navegação em tempo real. Headed é o default do @playwright/mcp —
    basta não passar --headless; este guard impede a regressão explicitamente."""


def _garantir_modo_headed(mcp_command: list[str]) -> None:
    if any(arg.strip().lower() == "--headless" for arg in mcp_command):
        raise ModoHeadlessProibidoError(
            "Modo headed é obrigatório nesta fase (SPEC §8) — remova --headless de "
            f"{' '.join(mcp_command)!r} (ou de ESTAGIARIO_PLAYWRIGHT_MCP_COMMAND)."
        )


def verificar_nomenclatura_peca(
    codigo: str,
    marca: str,
    nomes_conflitantes: list[str],
    on_evento: Callable[[str], None] | None = None,
    agent_provider: AgentLLMProvider | None = None,
    max_passos: int | None = None,
    mcp_command: list[str] | None = None,
    startup_timeout: float | None = None,
    tool_timeout: float | None = None,
) -> ResultadoVerificacao:
    provider = agent_provider or AnthropicAgentProvider()
    max_passos = max_passos or web_verification_max_passos()
    mcp_command = mcp_command or playwright_mcp_command()
    startup_timeout = startup_timeout or web_verification_startup_timeout()
    tool_timeout = tool_timeout or web_verification_tool_timeout()

    _garantir_modo_headed(mcp_command)

    return asyncio.run(
        _verificar_async(
            codigo, marca, nomes_conflitantes, provider, max_passos, mcp_command,
            startup_timeout, tool_timeout, on_evento,
        )
    )


async def _verificar_async(
    codigo: str,
    marca: str,
    nomes_conflitantes: list[str],
    provider: AgentLLMProvider,
    max_passos: int,
    mcp_command: list[str],
    startup_timeout: float,
    tool_timeout: float,
    on_evento: Callable[[str], None] | None,
) -> ResultadoVerificacao:
    system = montar_system_prompt(codigo, marca, nomes_conflitantes, max_passos)
    mensagens_iniciais = [{"role": "user", "content": montar_user_prompt(codigo, marca, nomes_conflitantes)}]

    if on_evento:
        on_evento(f"Subindo servidor Playwright MCP: {' '.join(mcp_command)}")
    params = StdioServerParameters(command=mcp_command[0], args=mcp_command[1:])
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=startup_timeout)
                if on_evento:
                    on_evento("Sessão MCP inicializada.")
                tools = await asyncio.wait_for(listar_tools_anthropic(session), timeout=startup_timeout)
                if on_evento:
                    on_evento(f"{len(tools)} tool(s) disponíveis: {', '.join(t['name'] for t in tools)}")
                tools_com_finalize = tools + [REPORTAR_RESULTADO_TOOL]

                async def gerar_resposta(mensagens):
                    return await provider.gerar_resposta_com_tools(
                        system=system, mensagens=mensagens, tools=tools_com_finalize
                    )

                async def chamar_tool(bloco):
                    return await asyncio.wait_for(executar_tool_call(session, bloco), timeout=tool_timeout)

                return await executar_loop_agentico(
                    mensagens_iniciais, gerar_resposta, chamar_tool, max_passos, on_evento
                )
    except TimeoutError as exc:
        raise VerificacaoWebTimeoutError(
            f"Servidor Playwright MCP não respondeu em {startup_timeout}s — verifique se o "
            "Chromium do Playwright está instalado (`npx playwright install chromium`) e se "
            f"{' '.join(mcp_command)} sobe corretamente fora da TUI."
        ) from exc
