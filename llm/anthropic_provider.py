"""Implementação de LLMProvider usando a API direta da Anthropic.

Saída estruturada via tool-use forçado (tool_choice apontando pro próprio
schema) — mais confiável que pedir JSON em texto livre e fazer parsing manual.
"""

from __future__ import annotations

import anthropic

from config import anthropic_api_key, llm_model
from llm.provider import LLMProvider


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str | None = None) -> None:
        self._client = anthropic.Anthropic(api_key=anthropic_api_key())
        self._model = model or llm_model()

    def gerar_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str = "output",
    ) -> dict:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
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
