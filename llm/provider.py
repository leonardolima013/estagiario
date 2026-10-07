"""Abstração de provider de LLM (SPEC.md §5 — multi-provider).

Interface mínima e genérica — não específica de particionamento — para que
outras etapas que precisam de julgamento de modelo (juiz de nome, arbitragem
contextual, Fase 2) reaproveitem sem redesenho. A Fase 5 formaliza a escolha
de provider por configuração (Anthropic direto vs. OpenRouter); por ora só
existe a implementação Anthropic (`llm/anthropic_provider.py`).
"""

from __future__ import annotations

from typing import Any, Callable, Protocol


class LLMProvider(Protocol):
    def gerar_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str = "output",
    ) -> dict:
        """Pede ao modelo uma resposta estruturada em JSON, validada contra json_schema."""
        ...


class LLMProviderComStreaming(LLMProvider, Protocol):
    """Provider que também sabe transmitir a resposta estruturada enquanto ela é gerada.

    Opcional: só `loop.tracing.TracingLLMProvider` usa este método, e só quando
    alguém acompanha a execução ao vivo. O domínio continua chamando `gerar_json`.
    """

    def gerar_json_transmitindo(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str,
        ao_atualizar: Callable[[dict], None],
    ) -> dict:
        """Mesmo contrato e mesmo retorno de `gerar_json`; `ao_atualizar` recebe o
        JSON parcial interpretado (strings ainda abertas incluídas) a cada trecho
        recebido. Exceções de `ao_atualizar` não interrompem a geração."""
        ...


class AgentLLMProvider(Protocol):
    """Provider para conversas multi-turn com uso de tools (loop agentico) — a
    abstração que o sub-agente de verificação web consome, sem se acoplar ao SDK
    de nenhum provider específico (SPEC.md §5). A Fase 5 adiciona um adaptador
    OpenRouter atrás desta mesma interface; por ora só existe a implementação
    Anthropic (`llm/anthropic_agent_provider.py`).

    Restrição de custo/contexto (decisão do usuário, Q-01=a): o orçamento de
    passos que limita o loop vive em `verification.agentic_loop.executar_loop_agentico`
    (parâmetro `max_passos`) — esta interface não deve crescer sem esse limite
    continuar sendo respeitado pelo chamador.
    """

    async def gerar_resposta_com_tools(
        self,
        system: str,
        mensagens: list[dict],
        tools: list[dict],
    ) -> Any:
        """Gera uma resposta do modelo dada a conversa (`mensagens`) e as `tools`
        disponíveis. O retorno deve expor `.content` como uma lista de blocos, cada
        um com `.type` ('text' | 'tool_use'), e — para blocos tool_use — `.name`,
        `.input` e `.id` (mesma forma consumida por `executar_loop_agentico`)."""
        ...
