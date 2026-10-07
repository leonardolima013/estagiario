"""Implementação de LLMProvider usando a API direta da Anthropic.

Saída estruturada via tool-use forçado (tool_choice apontando pro próprio
schema) — mais confiável que pedir JSON em texto livre e fazer parsing manual.

`cachear_system=True` marca o prefixo fixo (tool schema + system) com
`cache_control` para prompt caching em chamadas repetidas com o mesmo system.
Abaixo do mínimo cacheável do modelo (4096 tokens no Haiku 4.5) o marcador é
ignorado silenciosamente pela API — sem erro, só sem economia.

`gerar_json_transmitindo` faz o mesmo pedido em streaming, para o painel de
execução mostrar a justificativa enquanto ela é gerada. Mesmo modelo, mesmos
parâmetros e mesmo retorno de `gerar_json`; o custo é o mesmo.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable

import anthropic
from jiter import from_json

from config import anthropic_api_key, llm_model
from llm.provider import LLMProvider

_log = logging.getLogger(__name__)

_MAX_TOKENS = 4096
_DESCRICAO_TOOL = "Retorna o resultado estruturado pedido."


class AnthropicProvider(LLMProvider):
    def __init__(self, model: str | None = None, *, cachear_system: bool = False) -> None:
        self._client = anthropic.Anthropic(api_key=anthropic_api_key())
        self._model = model or llm_model()
        self._cachear_system = cachear_system
        # Desligado de vez se a API recusar o streaming eager deste tool (ver
        # `gerar_json_transmitindo`); a partir daí só se usa o `create` normal.
        self._streaming_disponivel = True

    def gerar_json(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str = "output",
    ) -> dict:
        response = self._client.messages.create(
            **self._parametros(system, user, json_schema, schema_name)
        )

        for block in response.content:
            if block.type == "tool_use" and block.name == schema_name:
                return block.input

        raise RuntimeError(
            f"Resposta do modelo não trouxe um tool_use de '{schema_name}': {response.content!r}"
        )

    def gerar_json_transmitindo(
        self,
        system: str,
        user: str,
        json_schema: dict,
        schema_name: str = "output",
        ao_atualizar: Callable[[dict], None] | None = None,
    ) -> dict:
        """`gerar_json` em streaming; `ao_atualizar` recebe o JSON parcial a cada trecho.

        O tool vai com `eager_input_streaming`, senão a API só entrega cada chave
        de topo inteira e a justificativa chegaria de uma vez. Nesse modo a API
        não valida o JSON antes de enviá-lo, então o texto final acumulado é
        conferido com `json.loads` (estrito): JSON incompleto vira erro, nunca
        uma resposta parcial aceita em silêncio.
        """
        if ao_atualizar is None or not self._streaming_disponivel:
            return self.gerar_json(system, user, json_schema, schema_name)

        recebido = ""
        try:
            with self._client.messages.stream(
                **self._parametros(system, user, json_schema, schema_name, eager=True)
            ) as stream:
                for evento in stream:
                    if getattr(evento, "type", None) != "input_json":
                        continue
                    recebido += evento.partial_json or ""
                    parcial = _interpretar_parcial(recebido)
                    if parcial is not None:
                        _notificar(ao_atualizar, parcial)
                mensagem = stream.get_final_message()
        except anthropic.BadRequestError as exc:
            # Recusado antes de gerar qualquer token (sem custo): se o motivo é o
            # streaming eager, segue sem streaming daqui em diante.
            if recebido or "eager_input_streaming" not in str(exc):
                raise
            self._streaming_disponivel = False
            _log.warning("API recusou eager_input_streaming; seguindo sem streaming: %s", exc)
            return self.gerar_json(system, user, json_schema, schema_name)

        return _resposta_final(mensagem, schema_name, recebido)

    def _parametros(
        self, system: str, user: str, json_schema: dict, schema_name: str, *, eager: bool = False
    ) -> dict[str, Any]:
        system_param = (
            [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
            if self._cachear_system
            else system
        )
        tool: dict[str, Any] = {
            "name": schema_name,
            "description": _DESCRICAO_TOOL,
            "input_schema": json_schema,
        }
        if eager:
            tool["eager_input_streaming"] = True
        return {
            "model": self._model,
            "max_tokens": _MAX_TOKENS,
            "system": system_param,
            "messages": [{"role": "user", "content": user}],
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": schema_name},
        }


def _interpretar_parcial(texto: str) -> dict | None:
    """JSON parcial do tool_use, incluindo a string que ainda está sendo escrita.

    O `snapshot` do SDK descarta strings abertas (`partial_mode=True`), o que
    esconderia a justificativa até ela terminar; `trailing-strings` mantém o trecho.
    """
    if not texto.strip():
        return None
    try:
        valor = from_json(texto.encode("utf-8"), partial_mode="trailing-strings")
    except ValueError:
        return None
    return valor if isinstance(valor, dict) else None


def _notificar(ao_atualizar: Callable[[dict], None], parcial: dict) -> None:
    try:
        ao_atualizar(parcial)
    except Exception:  # noqa: BLE001 — o observador nunca interrompe a geração
        pass


def _resposta_final(mensagem: Any, schema_name: str, recebido: str) -> dict:
    for block in mensagem.content:
        if block.type == "tool_use" and block.name == schema_name:
            if not recebido.strip():
                return block.input
            try:
                resposta = json.loads(recebido)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"JSON incompleto no tool_use de '{schema_name}' "
                    f"(stop_reason={getattr(mensagem, 'stop_reason', None)!r})"
                ) from exc
            if not isinstance(resposta, dict):
                raise RuntimeError(f"tool_use de '{schema_name}' não trouxe um objeto JSON")
            return resposta

    raise RuntimeError(
        f"Resposta do modelo não trouxe um tool_use de '{schema_name}': {mensagem.content!r}"
    )
