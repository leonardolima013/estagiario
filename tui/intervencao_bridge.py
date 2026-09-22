"""Ponte Textual -> domínio para intervenção humana bloqueante."""

from __future__ import annotations

from threading import Event

from llm.provider import LLMProvider
from memory.models import PedidoIntervencao, RespostaIntervencao
from tui.screens.intervencao_screen import IntervencaoCanceladaError, IntervencaoScreen


def pedir_intervencao_bloqueante(
    app,
    llm: LLMProvider,
    pedido: PedidoIntervencao,
    *,
    cancel_event: Event | None = None,
) -> RespostaIntervencao:
    """Abre a modal no event loop e espera somente na thread chamadora."""
    evento = Event()
    resultado: dict[str, object] = {}
    cancelamento_enviado = False

    def receber_resposta(resposta: RespostaIntervencao | None) -> None:
        if resposta is None:
            resultado["erro"] = IntervencaoCanceladaError(
                "Intervenção cancelada pelo operador; nenhum merge foi confirmado."
            )
        else:
            resultado["resposta"] = resposta
        evento.set()

    def abrir_modal() -> None:
        app.push_screen(IntervencaoScreen(pedido, llm), callback=receber_resposta)

    def fechar_modal() -> None:
        if isinstance(app.screen, IntervencaoScreen):
            app.screen.dismiss(None)

    app.call_from_thread(abrir_modal)
    while not evento.wait(0.05):
        if cancel_event is not None and cancel_event.is_set() and not cancelamento_enviado:
            cancelamento_enviado = True
            app.call_from_thread(fechar_modal)

    if "erro" in resultado:
        raise resultado["erro"]  # type: ignore[misc]
    resposta = resultado.get("resposta")
    if not isinstance(resposta, RespostaIntervencao):
        raise RuntimeError("modal encerrou sem RespostaIntervencao")
    return resposta
