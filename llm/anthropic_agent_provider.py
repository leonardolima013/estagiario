"""Implementação de AgentLLMProvider (llm/provider.py) usando a API direta da
Anthropic — o único ponto do caminho de verificação web que fala com o SDK da
Anthropic (SPEC.md §5). O loop agentico (`verification/agentic_loop.py`) e o
wiring do Playwright MCP (`verification/mcp_playwright_agent.py`) consomem esta
abstração, não `anthropic.Anthropic` diretamente — o que deixa a Fase 5 plugar
um adaptador OpenRouter atrás da mesma interface sem tocar no loop.

Restrição de custo/contexto (Q-01=a): o orçamento de passos que impede o loop de
crescer indefinidamente vive em `executar_loop_agentico(max_passos=...)`; este
provider só executa uma rodada (`messages.create`) por chamada, então não
adiciona nenhum crescimento de contexto por conta própria.
"""

from __future__ import annotations

import asyncio
from typing import Any

import anthropic

from config import anthropic_api_key, web_verification_model
from llm.provider import AgentLLMProvider

_MAX_TOKENS = 4096


class AnthropicAgentProvider(AgentLLMProvider):
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        model: str | None = None,
        max_tokens: int = _MAX_TOKENS,
    ) -> None:
        self._client = client or anthropic.Anthropic(api_key=anthropic_api_key())
        self._model = model or web_verification_model()
        self._max_tokens = max_tokens

    async def gerar_resposta_com_tools(
        self,
        system: str,
        mensagens: list[dict],
        tools: list[dict],
    ) -> Any:
        # messages.create é bloqueante — roda numa thread pra não travar o event loop.
        return await asyncio.to_thread(
            self._client.messages.create,
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=mensagens,
            tools=tools,
        )
