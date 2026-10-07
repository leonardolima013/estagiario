"""Trace mínimo e seguro do loop.

O collector recebe apenas resumos explícitos dos callers. Ainda assim, aplica
redação/truncamento defensivos para evitar que prompts, credenciais ou payloads
brutos acabem no JSON de auditoria.

Um `ouvinte` opcional recebe cada evento no momento em que é registrado, mais
os sinais que só fazem sentido ao vivo (`EtapaIniciada`, `RespostaParcial`,
`GrupoCarregado`).
É assim que o painel de execução acompanha uma família enquanto ela roda. Os
sinais ao vivo nunca entram em `eventos()`, então o JSON de auditoria é o mesmo
com ou sem ouvinte.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from threading import Lock
from time import perf_counter
from typing import TYPE_CHECKING, Any, Callable, Iterable, Protocol

from loop.models import EtapaIniciada, EventoExecucao, GrupoCarregado, RespostaParcial, agora_iso
from loop.serializacao import para_json

if TYPE_CHECKING:
    from memory.models import PedidoIntervencao, RespostaIntervencao

_CHAVES_REDACTED = frozenset({"prompt", "system", "user", "api_key", "password", "token", "secret"})
_MAX_STRING = 500
_MAX_ITEMS = 50

OuvinteTrace = Callable[[object], None]


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

    def __init__(self, ouvinte: OuvinteTrace | None = None) -> None:
        self._eventos: list[EventoExecucao] = []
        self._lock = Lock()
        self._ouvinte = ouvinte

    @property
    def tem_ouvinte(self) -> bool:
        return self._ouvinte is not None

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
        self._notificar(evento)
        return evento

    def sinalizar_inicio(
        self, fase: str, nome: str, detalhes: dict[str, Any] | None = None
    ) -> None:
        """Avisa o ouvinte que a etapa `nome` começou. Não entra em `eventos()`."""
        if self._ouvinte is None:
            return
        self._notificar(
            EtapaIniciada(
                timestamp=agora_iso(), fase=fase, nome=nome,
                detalhes=sanitizar_detalhes(detalhes or {}),
            )
        )

    def sinalizar_parcial(self, nome: str, resposta: Any) -> None:
        """Repassa ao ouvinte a resposta parcial da chamada LLM `nome`, sanitizada."""
        if self._ouvinte is None or not isinstance(resposta, dict):
            return
        self._notificar(
            RespostaParcial(timestamp=agora_iso(), nome=nome, resposta=sanitizar_detalhes(resposta))
        )

    def sinalizar_grupo(self, grupo_ref: str, registros: Iterable[Any]) -> None:
        """Repassa ao ouvinte os registros da família. Não entra em `eventos()`.

        Os registros vão como o JSON do loop os grava em `pecas` (`para_json`),
        sem o truncamento de `sanitizar_detalhes`: a tabela do painel mostra o
        mesmo que a auditoria.
        """
        if self._ouvinte is None:
            return
        self._notificar(
            GrupoCarregado(
                timestamp=agora_iso(), grupo_ref=grupo_ref,
                registros=tuple(para_json(registro) for registro in registros),
            )
        )

    def _notificar(self, evento: object) -> None:
        ouvinte = self._ouvinte
        if ouvinte is None:
            return
        try:
            ouvinte(evento)
        except Exception:  # noqa: BLE001
            # Mesma regra de `loop.executor._emitir`: a UI não pode transformar uma
            # falha de renderização em falha da etapa.
            pass

    def eventos(self) -> list[EventoExecucao]:
        with self._lock:
            return list(self._eventos)

    def limpar(self) -> None:
        with self._lock:
            self._eventos.clear()


def sinalizar_inicio(
    trace: Any, fase: str, nome: str, detalhes: dict[str, Any] | None = None
) -> None:
    """Sinaliza o início de uma etapa quando o trace sabe fazer isso.

    `None` e traces que só implementam `registrar` (os fakes dos testes) são
    ignorados, e nenhuma falha do sinal chega ao pipeline.
    """
    metodo = getattr(trace, "sinalizar_inicio", None)
    if metodo is None:
        return
    try:
        metodo(fase, nome, detalhes)
    except Exception:  # noqa: BLE001 — sinal ao vivo nunca derruba a etapa
        pass


def sinalizar_grupo(trace: Any, grupo_ref: str, registros: Iterable[Any]) -> None:
    """Publica os registros da família quando o trace sabe fazer isso.

    Mesma tolerância de `sinalizar_inicio`: `None`, fakes sem o método e falhas
    do sinal não chegam ao pipeline.
    """
    metodo = getattr(trace, "sinalizar_grupo", None)
    if metodo is None:
        return
    try:
        metodo(grupo_ref, registros)
    except Exception:  # noqa: BLE001 — sinal ao vivo nunca derruba a etapa
        pass


def envolver_intervencao(
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None,
    trace: TraceSink,
) -> Callable[[PedidoIntervencao], RespostaIntervencao] | None:
    """Callback de intervenção humana com registro no trace.

    Registra `intervencao/intervencao_humana` depois da resposta, exatamente como
    o loop sempre fez, e sinaliza o início para o painel mostrar que a execução
    está esperando o operador.
    """
    if pedir_intervencao is None:
        return None

    def _pedir(pedido: PedidoIntervencao) -> RespostaIntervencao:
        sinalizar_inicio(
            trace, "intervencao", "intervencao_humana",
            {
                "ponto": pedido.ponto,
                "grupo_ref": pedido.grupo_ref,
                "nomes_conflitantes": pedido.nomes_conflitantes,
                "motivo": pedido.motivo,
            },
        )
        resposta = pedir_intervencao(pedido)
        trace.registrar(
            "intervencao", "intervencao_humana",
            entrada={
                "ponto": pedido.ponto,
                "grupo_ref": pedido.grupo_ref,
                "nomes_conflitantes": pedido.nomes_conflitantes,
                "motivo": pedido.motivo,
            },
            saida={
                "resposta_humana": resposta.resposta_humana,
                "regra": resposta.regra,
                "valor": resposta.valor,
                "origem_id": resposta.origem_id,
                "acao": resposta.acao,
            },
            justificativa=resposta.resposta_humana,
        )
        return resposta

    return _pedir


class TracingLLMProvider:
    """Decorador de LLM que registra schema/saída estruturada, nunca o prompt.

    Com um trace ao vivo (`tem_ouvinte`) e um delegate que implementa
    `gerar_json_transmitindo` (`llm.provider.LLMProviderComStreaming`), a
    resposta é transmitida enquanto é gerada e cada versão parcial vai ao
    ouvinte. O domínio continua chamando só `gerar_json`.
    """

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
        sinalizar_inicio(self._trace, "llm", schema_name, entrada)
        try:
            resposta = self._chamar(system, user, json_schema, schema_name)
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

    def _chamar(self, system: str, user: str, json_schema: dict, schema_name: str) -> dict:
        transmitir = getattr(self._delegate, "gerar_json_transmitindo", None)
        sinalizar_parcial = getattr(self._trace, "sinalizar_parcial", None)
        if (
            callable(transmitir)
            and callable(sinalizar_parcial)
            and getattr(self._trace, "tem_ouvinte", False) is True
        ):
            return transmitir(
                system, user, json_schema, schema_name,
                ao_atualizar=lambda parcial: sinalizar_parcial(schema_name, parcial),
            )
        return self._delegate.gerar_json(system, user, json_schema, schema_name)
