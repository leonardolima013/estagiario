"""Entrega opcional dos Resultados_Estruturados da pesquisa web
(spec html-extract-on-web-search, Req 1.6, 4.2, 4.3).

O contrato simples do Verificador_Web — `(codigo, marca, nomes, *, on_evento)
-> ResultadoVerificacao` — continua valendo para Playwright e para fakes.
Verificadores que também implementam `VerificadorComResultados` entregam os
`ResultadoOrganico` da mesma pesquisa, sem nova requisição. `invocar_verificador`
normaliza as duas formas numa `ChamadaVerificacao`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol, runtime_checkable

from verification.models import ResultadoVerificacao
from verification.serper_client import ResultadoOrganico

SituacaoPesquisa = Literal["realizada", "nao_realizada", "sem_estruturados"]


@dataclass(frozen=True)
class VerificacaoComResultados:
    """Resultado de `verificar_com_resultados`. `resultados_pesquisa is None`
    indica que não houve Pesquisa_Realizada (Req 4.3)."""

    resultado: ResultadoVerificacao
    resultados_pesquisa: tuple[ResultadoOrganico, ...] | None


@runtime_checkable
class VerificadorComResultados(Protocol):
    """Verificador_Web que, além do contrato simples, entrega os
    Resultados_Estruturados da pesquisa (Req 1.6)."""

    def __call__(
        self,
        codigo: str,
        marca: str,
        nomes_conflitantes: list[str],
        *,
        on_evento: Callable[[str], None] | None = None,
    ) -> ResultadoVerificacao: ...

    def verificar_com_resultados(
        self,
        codigo: str,
        marca: str,
        nomes_conflitantes: list[str],
        *,
        on_evento: Callable[[str], None] | None = None,
    ) -> VerificacaoComResultados: ...


@dataclass(frozen=True)
class ChamadaVerificacao:
    """Saída normalizada de `invocar_verificador`."""

    resultado: ResultadoVerificacao
    situacao: SituacaoPesquisa
    resultados_pesquisa: tuple[ResultadoOrganico, ...] | None


def invocar_verificador(
    verificar_web: Callable,
    codigo: str,
    marca: str,
    nomes_conflitantes: list[str],
    *,
    on_evento: Callable[[str], None] | None,
) -> ChamadaVerificacao:
    """Chama o Verificador_Web uma única vez.

    Se `verificar_web` implementa `VerificadorComResultados`, usa
    `verificar_com_resultados` (situação `realizada` quando há resultados,
    inclusive tupla vazia; `nao_realizada` quando `None`). Caso contrário,
    chama o contrato simples (situação `sem_estruturados`, resultados `None`,
    Req 4.2). Exceções do verificador atravessam sem tratamento.
    """
    if isinstance(verificar_web, VerificadorComResultados):
        chamada = verificar_web.verificar_com_resultados(
            codigo, marca, nomes_conflitantes, on_evento=on_evento
        )
        resultados = chamada.resultados_pesquisa
        if resultados is None:
            return ChamadaVerificacao(chamada.resultado, "nao_realizada", None)
        return ChamadaVerificacao(chamada.resultado, "realizada", tuple(resultados))

    resultado = verificar_web(codigo, marca, nomes_conflitantes, on_evento=on_evento)
    return ChamadaVerificacao(resultado, "sem_estruturados", None)
