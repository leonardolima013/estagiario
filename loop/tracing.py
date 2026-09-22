"""Trace mínimo e seguro do loop.

O collector recebe apenas resumos explícitos dos callers. Ainda assim, aplica
redação/truncamento defensivos para evitar que prompts, credenciais ou payloads
brutos acabem no JSON de auditoria.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from threading import Lock
from time import perf_counter
from typing import Any, Protocol

from loop.models import EventoExecucao, agora_iso

_CHAVES_REDACTED = frozenset({"prompt", "system", "user", "api_key", "password", "token", "secret"})
_MAX_STRING = 500
_MAX_ITEMS = 50


def sanitizar_detalhes(valor: Any, chave: str | None = None) -> Any:
    if chave and chave.casefold() in _CHAVES_REDACTED:
        return "[REDACTED]"
    if isinstance(valor, Enum):
        return valor.value
    if is_dataclass(valor):
        return sanitizar_detalhes(
            {campo.name: getattr(valor, campo.name) for campo in fields(valor)}, chave
        )
    if isinstance(valor, str):
        return valor if len(valor) <= _MAX_STRING else valor[:_MAX_STRING] + "…"
    if isinstance(valor, dict):
        return {
            str(k): sanitizar_detalhes(v, str(k))
            for k, v in list(valor.items())[:_MAX_ITEMS]
        }
    if isinstance(valor, (list, tuple, set)):
        return [sanitizar_detalhes(v) for v in list(valor)[:_MAX_ITEMS]]
    if valor is None or isinstance(valor, (bool, int, float)):
        return valor
    return str(valor)[:_MAX_STRING]


class TraceSink(Protocol):
    def registrar(
        self,
        fase: str,
        nome: str,
        *,
        status: str = "ok",
        duracao_ms: float | None = None,
        detalhes: dict[str, Any] | None = None,
        entrada: dict[str, Any] | None = None,
        saida: dict[str, Any] | None = None,
        justificativa: str | None = None,
    ) -> EventoExecucao:
        ...


class TraceCollector:
    """Collector thread-safe; devolve cópias para não expor estado mutável."""

    def __init__(self) -> None:
        self._eventos: list[EventoExecucao] = []
        self._lock = Lock()

    def registrar(
        self,
        fase: str,
        nome: str,
        *,
        status: str = "ok",
        duracao_ms: float | None = None,
        detalhes: dict[str, Any] | None = None,
        entrada: dict[str, Any] | None = None,
        saida: dict[str, Any] | None = None,
        justificativa: str | None = None,
    ) -> EventoExecucao:
        detalhes_sanitizados = sanitizar_detalhes(detalhes or {})
        entrada_sanitizada = sanitizar_detalhes(entrada or {})
        saida_sanitizada = sanitizar_detalhes(saida if saida is not None else detalhes_sanitizados)
        evento = EventoExecucao(
            timestamp=agora_iso(),
            fase=fase,
            nome=nome,
            status=status,
            duracao_ms=duracao_ms,
            detalhes=detalhes_sanitizados,
            entrada=entrada_sanitizada,
            saida=saida_sanitizada,
            justificativa=sanitizar_detalhes(justificativa) if justificativa else None,
        )
        with self._lock:
            self._eventos.append(evento)
        return evento

    def eventos(self) -> list[EventoExecucao]:
        with self._lock:
            return list(self._eventos)

    def limpar(self) -> None:
        with self._lock:
            self._eventos.clear()


class TracingLLMProvider:
    """Decorador de LLM que registra schema/saída estruturada, nunca o prompt."""

    def __init__(self, delegate: Any, trace: TraceSink) -> None:
        self._delegate = delegate
        self._trace = trace

    def gerar_json(self, system: str, user: str, json_schema: dict, schema_name: str = "output") -> dict:
        inicio = perf_counter()
        entrada = {
            "schema_name": schema_name,
            "schema_fields": list(json_schema.get("properties", {}).keys()),
            "prompt_chars": len(user),
        }
        try:
            resposta = self._delegate.gerar_json(system, user, json_schema, schema_name)
        except Exception as exc:
            self._trace.registrar(
                "llm", schema_name, status="erro",
                duracao_ms=(perf_counter() - inicio) * 1000,
                entrada=entrada,
                saida={"erro": type(exc).__name__},
            )
            raise
        justificativa = resposta.get("justificativa") if isinstance(resposta, dict) else None
        self._trace.registrar(
            "llm", schema_name,
            duracao_ms=(perf_counter() - inicio) * 1000,
            entrada=entrada,
            saida={"resposta_estruturada": resposta},
            justificativa=justificativa if isinstance(justificativa, str) else None,
        )
        return resposta
