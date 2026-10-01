"""Teste de propriedade do cancelamento cooperativo do ``ColetorPaginas``.

Spec html-extract-on-web-search, Property 13 (Req 6.3, 6.5, 6.7). Reaproveita
os fakes e as estratégias de ``tests/test_coletor_paginas_pbt.py`` (transporte
contador com roteiro, armazém em diretório temporário por exemplo, snapshot cru
das tabelas e relógio UTC fixo). Nenhum teste abre rede nem toca ``db/``.
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from coleta_paginas.coletor import ColetorPaginas, ConfigColeta
from coleta_paginas.modelos import MOTIVO_CANCELADO, PecaConsultada, ResultadoBusca
from coleta_paginas.selecao import (
    classificar_fontes,
    marca_sem_referencia,
    resolver_teto_aceitos,
    selecionar_fontes,
)
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_coletor_paginas_pbt import (
    URLS_RESULTADO,
    RelogioUTCFake,
    TransporteContador,
    _responder_roteiro,
    armazem_temporario,
    snapshot_armazem,
    st_desfecho_transporte,
    st_janela_valida,
    st_peca,
    st_resultados,
    st_teto_ambiente_valido,
)


@dataclass
class SinalCancelamentoFake:
    """``cancelado()`` fica verdadeiro a partir da (k+1)-ésima consulta.

    ``k=None`` nunca cancela. Como o coletor consulta o sinal uma vez antes de
    cada fonte selecionada, as k primeiras fontes são processadas normalmente.
    """

    k: int | None
    consultas: int = 0

    def __call__(self) -> bool:
        indice = self.consultas
        self.consultas += 1
        return self.k is not None and indice >= self.k


@st.composite
def _caso_cancelamento(draw: st.DrawFn):
    peca = draw(st_peca())
    resultados = draw(st_resultados(peca.codigo_peca or "", peca.marca_peca, max_size=7))
    teto_chamada = draw(st.none() | st.integers(min_value=1, max_value=10))
    config_coleta = ConfigColeta(
        timeout_s=15.0,
        teto_aceitos_ambiente=draw(st_teto_ambiente_valido),
        janela_reuso_dias=draw(st_janela_valida),
    )
    roteiro = draw(
        st.fixed_dictionaries(
            {url.strip(): st_desfecho_transporte for url in URLS_RESULTADO if url.strip()}
        )
    )
    teto = resolver_teto_aceitos(teto_chamada, config_coleta.teto_aceitos_ambiente)
    n = len(selecionar_fontes(classificar_fontes(peca, resultados), teto).selecionadas)
    k = draw(st.none() | st.integers(min_value=0, max_value=n))
    return peca, resultados, teto_chamada, config_coleta, roteiro, k


def _executar(peca, resultados, teto_chamada, config_coleta, roteiro, **kwargs):
    """Roda ``coletar`` num armazém novo; devolve relatório, URLs do transporte e snapshot."""
    with armazem_temporario(RelogioUTCFake()) as armazem:
        transporte = TransporteContador(responder=_responder_roteiro(roteiro))
        coletor = ColetorPaginas(
            armazem, transporte=transporte, config=config_coleta, relogio=RelogioUTCFake()
        )
        relatorio = coletor.coletar(peca, resultados, teto_aceitos=teto_chamada, **kwargs)
        snapshot = snapshot_armazem(armazem.db_path)
    return relatorio, transporte.urls, snapshot


# Feature: html-extract-on-web-search, Property 13: Cancelamento cooperativo
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(caso=_caso_cancelamento())
@example(
    caso=(
        PecaConsultada("JE-4699", "Bosch", ("amortecedor dianteiro",)),
        [
            ResultadoBusca("JE-4699 Bosch", None, URLS_RESULTADO[0], "exemplo", 1),
            ResultadoBusca("JE-4699", None, URLS_RESULTADO[1], None, 2),
            ResultadoBusca("JE-4699", None, URLS_RESULTADO[2], None, 3),
            ResultadoBusca("JE-4699 Bosch", None, URLS_RESULTADO[4], "x", 4),  # proibida
        ],
        None,
        ConfigColeta(15.0, None, None),
        {URLS_RESULTADO[1]: ("404", b"")},
        1,
    )
)
def test_property_13_cancelamento_cooperativo(caso) -> None:
    """**Validates: Requirements 6.3, 6.5, 6.7**"""
    peca, resultados, teto_chamada, config_coleta, roteiro, k = caso
    assert not marca_sem_referencia(peca.marca_peca)  # pré-condição do gerador

    teto = resolver_teto_aceitos(teto_chamada, config_coleta.teto_aceitos_ambiente)
    selecao = selecionar_fontes(classificar_fontes(peca, resultados), teto)
    selecionadas = selecao.selecionadas
    n = len(selecionadas)
    processadas = n if k is None else k

    # Referência: a mesma coleta sem o parâmetro ``cancelado`` (html-extract-save).
    rel_ref, urls_ref, snap_ref = _executar(
        peca, resultados, teto_chamada, config_coleta, roteiro
    )

    sinal = SinalCancelamentoFake(k)
    rel, urls, snap = _executar(
        peca, resultados, teto_chamada, config_coleta, roteiro, cancelado=sinal
    )

    # O sinal é consultado exatamente uma vez antes de cada fonte selecionada (Req 6.3).
    assert sinal.consultas == n

    # Devolve relatório executado, sem exceção (Req 6.5).
    assert rel.status == "executada"
    assert rel.motivo is None
    assert (rel.codigo_peca, rel.marca_peca) == (rel_ref.codigo_peca, rel_ref.marca_peca)
    assert len(rel.entradas) == len(rel_ref.entradas)

    # As k primeiras fontes são processadas como sem cancelamento.
    assert rel.entradas[:processadas] == rel_ref.entradas[:processadas]

    # As demais viram falha/cancelado, sem status HTTP nem hash.
    for fonte, entrada in zip(selecionadas[processadas:], rel.entradas[processadas:n]):
        assert entrada.desfecho == "falha"
        assert entrada.motivo == MOTIVO_CANCELADO
        assert entrada.status_http is None
        assert entrada.hash_conteudo is None
        assert entrada.url == (fonte.decisao.resultado.url or "")
        assert entrada.dominio == fonte.dominio
        assert entrada.confianca == fonte.decisao.confianca

    # Exclusões url_invalida continuam relatadas, iguais à referência.
    assert rel.entradas[n:] == rel_ref.entradas[n:]
    assert all(e.desfecho == "url_invalida" for e in rel.entradas[n:])

    # Transporte só para as k primeiras fontes, na ordem (nenhuma chamada para as canceladas).
    urls_esperadas = [
        (fonte.decisao.resultado.url or "").strip() for fonte in selecionadas[:processadas]
    ]
    assert urls == urls_esperadas
    assert urls == urls_ref[:processadas]

    # Nenhuma gravação para as canceladas: só os armazenados das k primeiras.
    armazenados = [e for e in rel.entradas[:processadas] if e.desfecho == "armazenado"]
    registros = snap["registro_coleta"]
    assert len(registros) == len(armazenados)  # type: ignore[arg-type]
    hashes = {linha[0] for linha in snap["conteudo_pagina"]}  # type: ignore[union-attr]
    assert hashes == {e.hash_conteudo for e in armazenados}

    if k is None:
        # Nunca cancelado: idêntico à coleta sem o parâmetro (Req 6.7).
        assert rel == rel_ref
        assert snap == snap_ref

    # ``cancelado=None`` explícito é idêntico a omitir o parâmetro (Req 6.7).
    rel_none, urls_none, snap_none = _executar(
        peca, resultados, teto_chamada, config_coleta, roteiro, cancelado=None
    )
    assert rel_none == rel_ref
    assert urls_none == urls_ref
    assert snap_none == snap_ref
