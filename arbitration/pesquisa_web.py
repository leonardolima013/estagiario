"""Contrato entre a arbitragem de nome e a coleta de páginas da pesquisa web.

A arbitragem (`arbitration.nome`) depende apenas deste contrato e não conhece
`coleta_paginas.ColetorPaginas`. A implementação concreta da porta de coleta
fica em `coleta_paginas.integracao.IntegracaoColeta`.

Imports restritos à stdlib e a `verification.serper_client.ResultadoOrganico`;
`loop.tracing.TraceSink` só em `TYPE_CHECKING`. Nenhum import de frontend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from verification.serper_client import ResultadoOrganico

if TYPE_CHECKING:
    from loop.tracing import TraceSink


MetodoVerificador = Literal["serper", "playwright", "injetado", "desligada"]
SituacaoAtivacao = Literal["realizada", "nao_realizada", "sem_estruturados", "desligada"]
EstadoColeta = Literal["habilitada", "desabilitada", "invalida"]

MOTIVO_PESQUISA_DESLIGADA = "Pesquisa web desligada pelo operador."
CONTEXTO_WEB_DESLIGADA = "pesquisa_web=desligada"
JUSTIFICATIVA_INTERVENCAO_DESLIGADA = (
    "Nome decidido por intervenção humana com pesquisa web desligada pelo operador."
)
JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA = (
    "Nome não decidido: pesquisa web desligada pelo operador e nenhuma regra de "
    "memória ou intervenção humana disponível."
)


@dataclass(frozen=True)
class AtivacaoPesquisa:
    """Uma Ativacao_Pesquisa de `_arbitrar_nome_via_web`."""

    codigo: str | None  # search_ref do 1º registro, sem alteração
    marca: str | None  # brand do 1º registro, sem alteração
    nomes_conflitantes: tuple[str, ...]
    metodo: MetodoVerificador
    situacao: SituacaoAtivacao
    resultados: tuple[ResultadoOrganico, ...] | None  # só em "realizada"


class SinalCancelamento(Protocol):
    """Sinal de cancelamento cooperativo (ex.: `threading.Event`)."""

    def is_set(self) -> bool: ...


class PortaColeta(Protocol):
    """Porta de coleta chamada a cada Ativacao_Pesquisa.

    Nunca levanta exceção derivada de `Exception`.
    """

    def processar(
        self,
        ativacao: AtivacaoPesquisa,
        *,
        contexto: ContextoPesquisaWeb,
        trace: TraceSink | None,
    ) -> object: ...


@dataclass(frozen=True)
class ContextoPesquisaWeb:
    """Opções de pesquisa web e coleta de uma execução de `executar_caso`.

    Os padrões equivalem ao comportamento anterior à feature: pesquisa
    habilitada e nenhuma coleta.
    """

    pesquisa_habilitada: bool = True
    coleta_efetiva: EstadoColeta = "desabilitada"
    cancel_event: SinalCancelamento | None = None
    canal_avisos: Callable[[str], None] | None = None  # on_aviso original de executar_caso
    coleta: PortaColeta | None = None
    # Stealth_Efetivo (spec stealth-fallback-integration): só lido pela
    # IntegracaoColeta; a arbitragem não usa este campo.
    stealth_efetivo: EstadoColeta = "desabilitada"
