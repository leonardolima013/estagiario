"""Reputação de domínio na camada urllib com o fallback stealth desligado.

O loop liga (`executar_loop(reputacao_dominios=True)`, padrão) para não insistir
em sites que bloqueiam (auditoria de memória, N3): 3 falhas urllib seguidas
excluem o domínio, que passa a sair como `falha/dominio_excluido` sem rede. A
tela "Rodar testes" (`executar_caso` direto) continua sem reputação. Sem rede:
transporte e coletor são fakes.
"""

from __future__ import annotations

import pytest

from arbitration.pesquisa_web import AtivacaoPesquisa, ContextoPesquisaWeb
from coleta_paginas.coletor import CAMPOS_EVENTO_ENTRADA
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_coletor_paginas import (  # noqa: F401 — fixtures armazem, relogio
    CABECALHOS_HTML,
    TransporteRoteiro,
    armazem,
    novo_coletor,
    ok,
    peca,
    relogio,
    resultado,
)
from tests.test_integracao_stealth_pbt import CODIGO, MARCA, _organico_p9, integracao_kwargs

URL = "https://www.bloqueia.com.br/peca/JE4699"
DOMINIO = "www.bloqueia.com.br"
BLOQUEIO = (403, CABECALHOS_HTML, b"proibido")


def _coletar(coletor, *, reputacao, observados=None):
    return coletor.coletar(
        peca(), [resultado(URL, 1)], reputacao=reputacao,
        observador=observados.append if observados is not None else None,
    )


def test_tres_bloqueios_excluem_o_dominio_sem_stealth(armazem, relogio):
    transporte = TransporteRoteiro({URL: BLOQUEIO})
    coletor = novo_coletor(armazem, transporte, relogio)

    for _ in range(3):
        entrada, = _coletar(coletor, reputacao=True).entradas
        assert (entrada.desfecho, entrada.motivo, entrada.status_http) == ("falha", "status_http", 403)
    assert len(transporte.chamadas) == 3
    assert armazem.dominio_excluido(DOMINIO, "urllib")

    observados: list = []
    entrada, = _coletar(coletor, reputacao=True, observados=observados).entradas

    assert (entrada.desfecho, entrada.motivo) == ("falha", "dominio_excluido")
    assert len(transporte.chamadas) == 3  # sem rede
    # Os eventos continuam no formato sem stealth (sem camada/motivo_urllib).
    evento_entrada = next(e for e in observados if "desfecho" in e and "url" in e)
    assert set(evento_entrada) == set(CAMPOS_EVENTO_ENTRADA)
    assert entrada.camada is None and entrada.motivo_urllib is None


def test_sucesso_no_meio_zera_a_sequencia(armazem, relogio):
    transporte = TransporteRoteiro({URL: BLOQUEIO})
    coletor = novo_coletor(armazem, transporte, relogio)
    _coletar(coletor, reputacao=True)
    _coletar(coletor, reputacao=True)
    transporte.roteiro[URL] = ok(b"<html>JE4699 Bosch</html>")
    assert _coletar(coletor, reputacao=True).entradas[0].desfecho == "armazenado"

    transporte.roteiro[URL] = BLOQUEIO
    # A página foi armazenada: dentro da janela de reuso, não há nova busca.
    assert _coletar(coletor, reputacao=True).entradas[0].desfecho == "reaproveitado"
    assert not armazem.dominio_excluido(DOMINIO, "urllib")


def test_sem_reputacao_o_fluxo_e_o_anterior(armazem, relogio):
    transporte = TransporteRoteiro({URL: BLOQUEIO})
    coletor = novo_coletor(armazem, transporte, relogio)

    for _ in range(4):
        assert _coletar(coletor, reputacao=False).entradas[0].motivo == "status_http"

    assert len(transporte.chamadas) == 4
    assert armazem.ultimas_tentativas(DOMINIO, "urllib", 10) == ()


# -- integração e pipeline --------------------------------------------------


def _ativacao():
    return AtivacaoPesquisa(
        codigo=CODIGO, marca=MARCA, nomes_conflitantes=("Filtro de óleo", "Filtro óleo"),
        metodo="serper", situacao="realizada", resultados=(_organico_p9(URL, 1),),
    )


@pytest.mark.parametrize(
    ("stealth", "reputacao", "esperado"),
    [
        ("desabilitada", True, {"reputacao": True}),
        ("desabilitada", False, {}),
        ("habilitada", True, {"stealth": True}),  # o stealth já aplica a reputação
    ],
)
def test_integracao_so_repassa_reputacao_quando_ligada(stealth, reputacao, esperado):
    integracao, fabrica = integracao_kwargs()
    contexto = ContextoPesquisaWeb(
        coleta_efetiva="habilitada", stealth_efetivo=stealth, reputacao_dominios=reputacao,
    )

    integracao.processar(_ativacao(), contexto=contexto, trace=None)

    chamada, = fabrica.coletor.chamadas
    extras = {k: v for k, v in chamada.kwargs.items() if k in ("stealth", "reputacao")}
    assert extras == esperado


def test_ligada_no_loop_e_desligada_no_caso_direto():
    import inspect

    import pipeline
    from loop.executor import executar_loop

    assert inspect.signature(executar_loop).parameters["reputacao_dominios"].default is True
    assert inspect.signature(pipeline.executar_caso).parameters["reputacao_dominios"].default is False
    assert ContextoPesquisaWeb().reputacao_dominios is False


def test_executar_caso_repassa_a_opcao_ao_contexto(monkeypatch, fazer_registro):
    import pipeline

    contextos = []
    original = pipeline.ContextoPesquisaWeb

    def espiao(**kwargs):
        contexto = original(**kwargs)
        contextos.append(contexto)
        return contexto

    monkeypatch.setattr(pipeline, "ContextoPesquisaWeb", espiao)
    for kwargs in ({}, {"reputacao_dominios": True}):
        pipeline.executar_caso(
            "A", 1, llm=None, dependencias_fk=[],
            buscar_grupo=lambda s, b: [fazer_registro(1, "X")], **kwargs,
        )

    assert [c.reputacao_dominios for c in contextos] == [False, True]
