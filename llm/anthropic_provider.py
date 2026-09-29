"""Implementação de LLMProvider usando a API direta da Anthropic.

Saída estruturada via tool-use forçado (tool_choice apontando pro próprio
schema) — mais confiável que pedir JSON em texto livre e fazer parsing manual.

`cachear_system=True` marca o prefixo fixo (tool schema + system) com
`cache_control` para prompt caching em chamadas repetidas com o mesmo system.
Abaixo do mínimo cacheável do modelo (4096 tokens no Haiku 4.5) o marcador é
ignorado silenciosamente pela API — sem erro, só sem economia.
"""

from __future__ import annotations

import anthropic

from config import anthropic_api_key, llm_model
from llm.provider import LLMProvider


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str | None = None, *, cachear_system: bool = False) -> None:
        self._client = anthropic.Anthropic(api_key=anthropic_api_key())
        self._model = model or llm_model()
        self._cachear_system = cachear_system

    def gerar_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str = "output",
    ) -> dict:
        system_param = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if self._cachear_system
            else system
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system_param,
            messages=[{"role": "user", "content": user}],
            tools=[
                {
                    "name": schema_name,
                    "description": "Retorna o resultado estruturado pedido.",
                    "input_schema": json_schema,
                }
            ],
            tool_choice={"type": "tool", "name": schema_name},
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == schema_name:
                return block.input

        raise RuntimeError(
            f"Resposta do modelo não trouxe um tool_use de '{schema_name}': {response.content!r}"
        )
