"""Seletor_Verificacao_Web — resolve qual implementação de verificação web
(Skill_Serper ou Skill_Playwright) injetar na arbitragem de nome, a partir do
Metodo_Verificacao_Web configurado no `.env` (R1). As factories são injetáveis
para teste, evitando o import pesado de Playwright/MCP.
"""

from __future__ import annotations

import functools
from typing import Callable

import config

_NOME_VAR = "ESTAGIARIO_WEB_VERIFICATION_METODO"


class MetodoVerificacaoInvalidoError(ValueError):
    """Metodo_Verificacao_Web configurado não é 'serper' nem 'playwright' (R1.5).

    Identifica a variável de ambiente e o valor inválido recebido; o seletor não
    devolve nenhuma implementação de verificação web quando isto é levantado."""


def normalizar_metodo(bruto: str | None) -> str:
    """Pura: normaliza o valor cru da env var, insensível a caixa e ignorando
    espaços nas extremidades. '' ou None -> 'playwright' (R1.1, R1.3, R1.4)."""
    metodo = (bruto or "").strip().casefold()
    return metodo or "playwright"


@functools.lru_cache(maxsize=1)
def _llm_serper_padrao():
    """LLM do SubAgente_Nome_Serper: modelo pequeno (`web_verification_model`,
    Haiku por padrão) com o system fixo marcado para prompt caching. Criado uma
    vez por processo, no primeiro uso — nunca na resolução do seletor."""
    from llm.anthropic_provider import AnthropicProvider

    return AnthropicProvider(model=config.web_verification_model(), cachear_system=True)


def _resolver_llm_serper(on_evento: Callable[[str], None] | None):
    """LLM padrão do sub-agente Serper, resolvido no momento da chamada; sem
    LLM disponível, avisa e devolve None (fallback determinístico)."""
    try:
        return _llm_serper_padrao()
    except Exception as exc:  # noqa: BLE001 — sem LLM, cai no fallback determinístico
        if on_evento is not None:
            on_evento(
                f"Sub-agente Serper indisponível ({type(exc).__name__}); "
                "usando decisão determinística."
            )
        return None


class VerificadorSerperPadrao:
    """Verificador_Web padrão do método Serper.

    `__call__` é o contrato simples `(codigo, marca, nomes, *, on_evento)`, com o
    LLM do sub-agente criado no primeiro uso. `verificar_com_resultados` faz a
    mesma verificação e também entrega os Resultados_Estruturados da pesquisa
    (`VerificadorComResultados`, spec html-extract-on-web-search Req 1.6), sem
    nova requisição. A skill é procurada em `verification.serper_agent` a cada
    chamada, para que substituições no módulo (testes) valham."""

    def __init__(self) -> None:
        from verification import serper_agent

        functools.update_wrapper(self, serper_agent.verificar_nomenclatura_peca_serper)

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        from verification import serper_agent

        llm = _resolver_llm_serper(on_evento)
        return serper_agent.verificar_nomenclatura_peca_serper(
            codigo, marca, nomes_conflitantes, on_evento=on_evento, llm=llm
        )

    def verificar_com_resultados(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        from verification import serper_agent
        from verification.resultados_estruturados import VerificacaoComResultados

        llm = _resolver_llm_serper(on_evento)
        capturados: list = []
        resultado = serper_agent.verificar_nomenclatura_peca_serper(
            codigo,
            marca,
            nomes_conflitantes,
            on_evento=on_evento,
            llm=llm,
            on_resultados=capturados.append,
        )
        return VerificacaoComResultados(resultado, capturados[0] if capturados else None)


def _default_serper() -> Callable:
    return VerificadorSerperPadrao()


def _default_playwright() -> Callable:
    from verification.mcp_playwright_agent import verificar_nomenclatura_peca

    return verificar_nomenclatura_peca


def resolver_verificacao_web(
    metodo: str | None = None,
    *,
    serper_factory: Callable[[], Callable] | None = None,
    playwright_factory: Callable[[], Callable] | None = None,
) -> Callable:
    """Devolve o chamável de verificação web conforme o Metodo_Verificacao_Web.

    Com metodo=None, lê de `config.web_verification_metodo()`. As factories são
    injetáveis para teste (evitam import pesado de Playwright/MCP). O chamável
    devolvido aceita `(codigo, marca, nomes_conflitantes, *, on_evento=None)` e
    retorna `ResultadoVerificacao` — o mesmo contrato dos dois lados (R1.6, R2.1).

    Método inválido levanta MetodoVerificacaoInvalidoError identificando variável
    e valor, sem devolver chamável (R1.5)."""
    bruto = metodo if metodo is not None else config.web_verification_metodo()
    m = normalizar_metodo(bruto)
    if m == "serper":
        return (serper_factory or _default_serper)()
    if m == "playwright":
        return (playwright_factory or _default_playwright)()
    raise MetodoVerificacaoInvalidoError(
        f"{_NOME_VAR}={bruto!r} inválido: use 'serper' ou 'playwright'."
    )
