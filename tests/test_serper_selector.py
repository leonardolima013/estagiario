"""Testes de exemplo/borda do Seletor_Verificacao_Web (verification/selector.py).

Usam factories injetadas — não sobem Playwright/MCP nem fazem I/O real.
"""

from __future__ import annotations

import inspect

import pytest

from verification.selector import (
    MetodoVerificacaoInvalidoError,
    normalizar_metodo,
    resolver_verificacao_web,
)


def test_normalizar_metodo_default_e_vazio():
    assert normalizar_metodo(None) == "playwright"
    assert normalizar_metodo("") == "playwright"
    assert normalizar_metodo("   ") == "playwright"
    assert normalizar_metodo("  SERPER ") == "serper"
    assert normalizar_metodo("PlayWright") == "playwright"


def test_factories_injetadas_escolhidas_por_metodo():
    """R1.2, R1.3: cada método resolve para a factory correspondente."""
    serper = resolver_verificacao_web(
        "serper", serper_factory=lambda: "SERPER", playwright_factory=lambda: "PW"
    )
    playwright = resolver_verificacao_web(
        "playwright", serper_factory=lambda: "SERPER", playwright_factory=lambda: "PW"
    )
    assert serper == "SERPER"
    assert playwright == "PW"


def test_metodo_invalido_levanta_erro_com_variavel_e_valor():
    """R1.5: método inválido identifica variável e valor, sem devolver chamável."""
    with pytest.raises(MetodoVerificacaoInvalidoError) as exc:
        resolver_verificacao_web(
            "bing", serper_factory=lambda: "SERPER", playwright_factory=lambda: "PW"
        )
    assert "ESTAGIARIO_WEB_VERIFICATION_METODO" in str(exc.value)
    assert "bing" in str(exc.value)


def test_assinatura_do_chamavel_da_skill_serper():
    """R1.6, R2.1: o chamável real da Skill_Serper aceita (codigo, marca,
    nomes_conflitantes, *, on_evento=...)."""
    from verification.serper_agent import verificar_nomenclatura_peca_serper

    chamavel = resolver_verificacao_web(
        "serper", serper_factory=lambda: verificar_nomenclatura_peca_serper
    )
    sig = inspect.signature(chamavel)
    params = list(sig.parameters)
    assert params[:3] == ["codigo", "marca", "nomes_conflitantes"]
    assert "on_evento" in sig.parameters
    assert sig.parameters["on_evento"].kind == inspect.Parameter.KEYWORD_ONLY


def test_default_serper_injeta_llm_lazy_uma_vez(monkeypatch):
    """O default serper injeta o sub-agente LLM (modelo pequeno + cache do system),
    criado só no primeiro uso e reaproveitado entre chamadas."""
    import verification.selector as selector
    import verification.serper_agent as serper_agent

    criados: list[dict] = []

    class _ProviderFake:
        def __init__(self, model=None, *, cachear_system=False):
            criados.append({"model": model, "cachear_system": cachear_system})

    monkeypatch.setattr("llm.anthropic_provider.AnthropicProvider", _ProviderFake)
    monkeypatch.setattr(selector.config, "web_verification_model", lambda: "modelo-pequeno")
    selector._llm_serper_padrao.cache_clear()
    recebidos: list[object] = []

    def _skill_fake(codigo, marca, nomes, *, on_evento=None, llm=None, cliente=None):
        recebidos.append(llm)
        return "ok"

    monkeypatch.setattr(serper_agent, "verificar_nomenclatura_peca_serper", _skill_fake)
    try:
        chamavel = resolver_verificacao_web("serper")
        assert criados == []  # nada construído na resolução
        assert chamavel("C1", "M", ["A", "B"]) == "ok"
        assert chamavel("C2", "M", ["A", "B"]) == "ok"
        assert criados == [{"model": "modelo-pequeno", "cachear_system": True}]
        assert all(isinstance(llm, _ProviderFake) for llm in recebidos)
        assert recebidos[0] is recebidos[1]
    finally:
        selector._llm_serper_padrao.cache_clear()


def test_default_serper_sem_llm_disponivel_cai_no_deterministico(monkeypatch):
    import verification.selector as selector
    import verification.serper_agent as serper_agent

    def _explode():
        raise RuntimeError("sem chave")

    monkeypatch.setattr(selector, "_llm_serper_padrao", _explode)
    recebidos: list[object] = []
    monkeypatch.setattr(
        serper_agent, "verificar_nomenclatura_peca_serper",
        lambda *a, llm=None, **k: recebidos.append(llm) or "ok",
    )
    eventos: list[str] = []
    assert resolver_verificacao_web("serper")("C", "M", ["A"], on_evento=eventos.append) == "ok"
    assert recebidos == [None]
    assert any("indisponível" in e for e in eventos)



def test_default_serper_implementa_protocolo_com_resultados():
    """html-extract-on-web-search Req 1.6: o verificador Serper padrão entrega
    Resultados_Estruturados; preserva __wrapped__, nome e assinatura."""
    import verification.selector as selector
    from verification.resultados_estruturados import VerificadorComResultados
    from verification.serper_agent import verificar_nomenclatura_peca_serper

    chamavel = selector._default_serper()
    assert isinstance(chamavel, VerificadorComResultados)
    assert chamavel.__wrapped__ is verificar_nomenclatura_peca_serper
    assert chamavel.__name__ == verificar_nomenclatura_peca_serper.__name__
    assert chamavel.__doc__ == verificar_nomenclatura_peca_serper.__doc__


def test_verificar_com_resultados_captura_organicos_e_call_nao_passa_on_resultados(monkeypatch):
    """Req 1.6, 4.3: `verificar_com_resultados` repassa `on_resultados` e devolve os
    orgânicos capturados (ou None sem Pesquisa_Realizada); `__call__` não o repassa."""
    import verification.selector as selector
    import verification.serper_agent as serper_agent
    from verification.serper_client import ResultadoOrganico

    monkeypatch.setattr(selector, "_llm_serper_padrao", lambda: "LLM")
    organicos = (ResultadoOrganico(title="t", link="https://a.com", snippet="s", position=1),)
    chamadas: list[dict] = []

    def _skill_fake(codigo, marca, nomes, *, on_evento=None, llm=None, **kwargs):
        chamadas.append({"llm": llm, **kwargs})
        if codigo == "COM" and "on_resultados" in kwargs:
            kwargs["on_resultados"](organicos)
        return f"resultado-{codigo}"

    monkeypatch.setattr(serper_agent, "verificar_nomenclatura_peca_serper", _skill_fake)
    chamavel = resolver_verificacao_web("serper")

    com = chamavel.verificar_com_resultados("COM", "M", ["A"])
    assert com.resultado == "resultado-COM"
    assert com.resultados_pesquisa == organicos

    sem = chamavel.verificar_com_resultados("SEM", "M", ["A"])
    assert sem.resultado == "resultado-SEM"
    assert sem.resultados_pesquisa is None

    assert chamavel("COM", "M", ["A"]) == "resultado-COM"
    assert "on_resultados" not in chamadas[-1]
    assert all(c["llm"] == "LLM" for c in chamadas)
