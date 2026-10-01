"""Testes de propriedade da Integracao_Coleta com o fallback stealth
(spec stealth-fallback-integration).

Organização do módulo:

1. Bloco comum: coletor fake que registra os ``kwargs`` de ``coletar``,
   fábricas, canal, trace, sinal e estratégias de ativação/contexto. As
   estratégias de ativação e o oráculo de precedência vêm de
   ``tests/test_integracao_coleta_pbt.py`` (spec html-extract-on-web-search).
2. Uma seção por propriedade do design, na ordem numérica.

Todos os testes rodam com a guarda de rede e o isolamento de ambiente da
coleta. Nenhum teste abre rede, navegador ou LLM, nem depende de gates
``ESTAGIARIO_RUN_*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from arbitration.pesquisa_web import AtivacaoPesquisa, ContextoPesquisaWeb
from coleta_paginas.integracao import (
    PREFIXO_AVISO,
    IntegracaoColeta,
    mensagem_stealth_invalida,
)
from coleta_paginas.modelos import PecaConsultada, RelatorioColeta, ResultadoBusca
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_integracao_coleta_pbt import (
    CanalFake,
    SinalFake,
    TraceFake,
    _oraculo_precedencia,
    st_ativacao_coerente,
    st_ativacao_elegivel,
    st_coleta_efetiva,
    st_sinal,
)

# ===========================================================================
# Bloco comum: fakes
# ===========================================================================


@dataclass
class ChamadaColetarKwargs:
    """Argumentos recebidos por ``ColetorFakeKwargs.coletar``; ``kwargs`` guarda
    exatamente as chaves nomeadas passadas pela integração."""

    peca: PecaConsultada
    resultados: tuple[ResultadoBusca, ...]
    kwargs: dict[str, Any]


@dataclass
class ColetorFakeKwargs:
    """Coletor fake que registra os ``kwargs`` de cada chamada e devolve um
    relatório com o ``status`` configurado, sem entradas. Sem armazém, sem
    transporte, sem rede."""

    status: str = "executada"
    chamadas: list[ChamadaColetarKwargs] = field(default_factory=list)

    def coletar(self, peca, resultados, **kwargs):
        self.chamadas.append(
            ChamadaColetarKwargs(peca=peca, resultados=tuple(resultados), kwargs=dict(kwargs))
        )
        if self.status == "nao_enriquecivel":
            return RelatorioColeta(
                status="nao_enriquecivel",
                motivo="marca_sem_referencia",
                codigo_peca=peca.codigo_peca,
                marca_peca=peca.marca_peca,
                entradas=(),
            )
        return RelatorioColeta(
            status="executada",
            motivo=None,
            codigo_peca=peca.codigo_peca,
            marca_peca=peca.marca_peca,
            entradas=(),
            stealth=kwargs.get("stealth", False),
        )


class FalhaFabricaFake(RuntimeError):
    """Exceção da fábrica fake (construção do coletor falha)."""


@dataclass
class FabricaColetorKwargs:
    """Fábrica injetável em ``IntegracaoColeta``; conta construções e pode falhar."""

    coletor: ColetorFakeKwargs = field(default_factory=ColetorFakeKwargs)
    falhar: bool = False
    construcoes: int = 0

    def __call__(self) -> ColetorFakeKwargs:
        self.construcoes += 1
        if self.falhar:
            raise FalhaFabricaFake("<<fabrica>>")
        return self.coletor


def contexto_stealth(
    *,
    coleta_efetiva: str = "habilitada",
    stealth_efetivo: str = "desabilitada",
    cancel_event: SinalFake | None = None,
    canal_avisos: CanalFake | None = None,
    pesquisa_habilitada: bool = True,
) -> ContextoPesquisaWeb:
    """``ContextoPesquisaWeb`` com ``stealth_efetivo`` para os testes da integração."""
    return ContextoPesquisaWeb(
        pesquisa_habilitada=pesquisa_habilitada,
        coleta_efetiva=coleta_efetiva,
        cancel_event=cancel_event,
        canal_avisos=canal_avisos,
        stealth_efetivo=stealth_efetivo,
    )


def sinal_de(estado: str) -> SinalFake | None:
    """``ausente`` → ``None``; ``inativo``/``ativo`` → ``SinalFake`` correspondente."""
    return None if estado == "ausente" else SinalFake(ativo=estado == "ativo")


def integracao_kwargs(
    *, status_coletor: str = "executada", fabrica_falha: bool = False
) -> tuple[IntegracaoColeta, FabricaColetorKwargs]:
    """``IntegracaoColeta`` com fábrica fake e relógio monotônico fixo."""
    fabrica = FabricaColetorKwargs(
        coletor=ColetorFakeKwargs(status=status_coletor), falhar=fabrica_falha
    )
    return IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0), fabrica


# ===========================================================================
# Bloco comum: estratégias
# ===========================================================================

st_stealth_efetivo = st.sampled_from(("habilitada", "desabilitada", "invalida"))
st_status_coletor = st.sampled_from(("executada", "executada", "nao_enriquecivel"))

# Mistura ativações quaisquer (coerentes) com elegíveis, para haver chamadas ao coletor.
st_ativacao_stealth: st.SearchStrategy[AtivacaoPesquisa] = (
    st_ativacao_elegivel() | st_ativacao_coerente()
)

_SETTINGS_PBT = settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


# ===========================================================================
# Property 2: A integração só liga o stealth quando chama o coletor
# ===========================================================================


# Feature: stealth-fallback-integration, Property 2: A integração só liga o stealth quando chama o coletor
@_SETTINGS_PBT
@given(
    ativacao=st_ativacao_stealth,
    coleta=st_coleta_efetiva,
    stealth=st_stealth_efetivo,
    sinal=st_sinal,
    status_coletor=st_status_coletor,
    fabrica_falha=st.booleans(),
    com_canal=st.booleans(),
)
def test_property_2_integracao_so_liga_stealth_quando_chama_coletor(
    ativacao, coleta, stealth, sinal, status_coletor, fabrica_falha, com_canal
):
    """O coletor recebe ``stealth=True`` se e somente se é chamado e
    ``stealth_efetivo == "habilitada"``; nas demais chamadas a chave ``stealth``
    não aparece. O aviso ``stealth_configuracao_invalida`` sai exatamente uma
    vez se e somente se o coletor é chamado e ``stealth_efetivo == "invalida"``;
    nunca em ``nao_executada``, ``nao_enriquecivel`` pela Trava_Entrada, erro
    de código inválido ou falha de construção do coletor.

    **Validates: Requirements 1.3, 1.5**
    """
    canal = CanalFake() if com_canal else None
    integracao, fabrica = integracao_kwargs(
        status_coletor=status_coletor, fabrica_falha=fabrica_falha
    )

    desfecho = integracao.processar(
        ativacao,
        contexto=contexto_stealth(
            coleta_efetiva=coleta,
            stealth_efetivo=stealth,
            cancel_event=sinal_de(sinal),
            canal_avisos=canal,
        ),
        trace=TraceFake(),
    )

    # Oráculo independente: o coletor só é alcançado quando a precedência do
    # spec anterior chega a ``executada`` e a fábrica constrói o coletor.
    esperado, _ = _oraculo_precedencia(ativacao, coleta, sinal == "ativo")
    alcanca_fabrica = esperado == "executada"
    chamado = alcanca_fabrica and not fabrica_falha

    assert fabrica.construcoes == (1 if alcanca_fabrica else 0)
    chamadas = fabrica.coletor.chamadas
    assert len(chamadas) == (1 if chamado else 0)

    if chamado:
        kwargs = chamadas[0].kwargs
        if stealth == "habilitada":
            assert kwargs.get("stealth") is True
        else:
            assert "stealth" not in kwargs
        # As demais chaves continuam as do spec anterior.
        assert set(kwargs) - {"stealth"} == {"cancelado", "observador"}
        esperado_final = "executada" if status_coletor == "executada" else "nao_enriquecivel"
        assert desfecho.desfecho == esperado_final
    elif fabrica_falha and alcanca_fabrica:
        assert (desfecho.desfecho, desfecho.motivo) == ("erro", "FalhaFabricaFake")
    else:
        assert desfecho.desfecho == esperado

    if canal is not None:
        aviso = mensagem_stealth_invalida()
        avisos = [m for m in canal.mensagens if m == aviso]
        assert len(avisos) == (1 if chamado and stealth == "invalida" else 0)
        assert all(m.startswith(PREFIXO_AVISO) for m in canal.mensagens)
        # Nenhuma outra mensagem menciona o motivo do aviso stealth.
        outras = [m for m in canal.mensagens if m != aviso]
        assert all("stealth_configuracao_invalida" not in m for m in outras)
        if chamado and stealth == "invalida":
            # O aviso vem antes de qualquer mensagem de desfecho da ativação.
            assert canal.mensagens[0] == aviso


def test_property_2_mensagem_stealth_invalida_formato():
    """Formato exato do aviso: prefixo + JSON com motivo e nome da variável.

    **Validates: Requirements 1.3**
    """
    assert mensagem_stealth_invalida() == (
        f'{PREFIXO_AVISO} {{"motivo": "stealth_configuracao_invalida", '
        f'"variavel": "ESTAGIARIO_COLETA_STEALTH_HABILITADA"}}'
    )



# ===========================================================================
# Property 9: Exceção do fallback vira `erro` sem propagar
# ===========================================================================

import json as _json_p9  # noqa: E402
import tempfile as _tempfile_p9  # noqa: E402
from itertools import count as _count_p9  # noqa: E402
from pathlib import Path as _Path_p9  # noqa: E402

from coleta_paginas.coletor import ColetorPaginas as _ColetorPaginasP9  # noqa: E402
from coleta_paginas.integracao import NOME_EVENTO_TRACE as NOME_EVENTO_TRACE_P9  # noqa: E402
from db.armazem_paginas import ArmazemPaginas as _ArmazemPaginasP9  # noqa: E402
from loop.tracing import TraceCollector as _TraceCollectorP9  # noqa: E402
from tests.fakes_stealth import (  # noqa: E402
    ALLOWLIST_PADRAO,
    FallbackFake,
    TravaFake,
    falhas_busca,
    paginas_stealth,
)
from tests.test_coletor_paginas import (  # noqa: E402
    CODIGO,
    MARCA,
    Relogio,
    TransporteRoteiro,
    config_coleta,
)
from tools.buscador_stealth import TRAVA_NAVEGADOR  # noqa: E402
from verification.serper_client import ResultadoOrganico  # noqa: E402

_MAX_FONTES_P9 = 3  # = Teto_Aceitos configurado; com ≤ 2 falhas stealth antes da
# exceção, a camada stealth nunca chega a 3 falhas consecutivas (fonte sempre elegível).
_CONTADOR_P9 = _count_p9()

_BASES_EXCECAO_P9: tuple[type[Exception], ...] = (
    Exception,
    RuntimeError,
    ValueError,
    KeyError,
    TypeError,
    OSError,
    LookupError,
    ArithmeticError,
    AttributeError,
)

# Nomes gerados de classes: identificadores Python. O marcador usa "§", que
# nunca aparece num nome gerado, então ``motivo == nome`` não o contém.
st_nome_classe_p9 = st.from_regex(r"[A-Z][A-Za-z0-9_]{0,15}", fullmatch=True)


@st.composite
def st_classe_excecao_p9(draw: st.DrawFn) -> type[Exception]:
    base = draw(st.sampled_from(_BASES_EXCECAO_P9))
    if draw(st.booleans()):
        return base
    return type(draw(st_nome_classe_p9), (base,), {})


def _organico_p9(url: str, posicao: int) -> ResultadoOrganico:
    """Título com código e marca → confiança alta na seleção de fontes."""
    return ResultadoOrganico(
        title=f"Filtro de óleo {CODIGO} {MARCA}",
        link=url,
        snippet="Peça automotiva",
        position=posicao,
    )


def _url_p9(indice: int) -> str:
    return f"https://www.mercadocar.com.br/peca/{indice}"


# Feature: stealth-fallback-integration, Property 9: Exceção do fallback vira `erro` sem propagar
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    classe=st_classe_excecao_p9(),
    n_fontes=st.integers(min_value=1, max_value=_MAX_FONTES_P9),
    dados=st.data(),
    status_bloqueio=st.lists(st.sampled_from((403, 429, 503)), min_size=_MAX_FONTES_P9,
                             max_size=_MAX_FONTES_P9),
    trava_real=st.booleans(),
)
def test_property_9_excecao_do_fallback_vira_erro_sem_propagar(
    classe, n_fontes, dados, status_bloqueio, trava_real
):
    """Uma exceção (``Exception`` ou subclasse, inclusive com nome gerado)
    levantada pelo Fallback_Stealth — inclusive no meio de uma sequência de
    fontes — numa ativação com ``stealth_efetivo == "habilitada"`` não
    atravessa ``IntegracaoColeta.processar``: o desfecho é ``erro`` com
    ``motivo == type(exc).__name__``, a mensagem da exceção não aparece no
    canal nem no trace, há um único evento ``coletar_paginas`` e a
    Trava_Navegador fica livre.

    **Validates: Requirements 3.6, 9.2**
    """
    marcador = f"§marcador-p9-{next(_CONTADOR_P9)}§"
    urls = [_url_p9(i) for i in range(n_fontes)]
    # A exceção acontece na fonte ``k``; as anteriores recebem página ou falha.
    k = dados.draw(st.integers(min_value=0, max_value=n_fontes - 1), label="indice_excecao")
    roteiro_fallback: dict[str, Any] = {
        url: dados.draw(paginas_stealth(url) | falhas_busca(url), label=f"fallback_{i}")
        for i, url in enumerate(urls[:k])
    }
    excecao = classe(marcador)
    roteiro_fallback[urls[k]] = excecao
    fallback = FallbackFake(roteiro_fallback)

    transporte = TransporteRoteiro(
        {url: (status_bloqueio[i], {}, b"") for i, url in enumerate(urls)}
    )
    trava = TRAVA_NAVEGADOR if trava_real else TravaFake()
    relogio = Relogio()

    with _tempfile_p9.TemporaryDirectory() as diretorio:
        base = _Path_p9(diretorio)
        construcoes: list[_ColetorPaginasP9] = []

        def fabrica() -> _ColetorPaginasP9:
            armazem = _ArmazemPaginasP9(
                base / "paginas.db",
                caminho_rule_store=base / "rule_store.db",
                relogio=relogio,
            )
            coletor = _ColetorPaginasP9(
                armazem,
                transporte=transporte,
                config=config_coleta(teto=str(_MAX_FONTES_P9)),
                relogio=relogio,
                fallback_stealth=fallback,
                allowlist_stealth=ALLOWLIST_PADRAO,
                trava_navegador=trava,
            )
            construcoes.append(coletor)
            return coletor

        integracao = IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0)
        canal = CanalFake()
        trace = _TraceCollectorP9()
        ativacao = AtivacaoPesquisa(
            codigo=CODIGO,
            marca=MARCA,
            nomes_conflitantes=("Filtro de óleo", "Filtro óleo"),
            metodo="serper",
            situacao="realizada",
            resultados=tuple(_organico_p9(url, i + 1) for i, url in enumerate(urls)),
        )

        # Não levanta (o teste falharia com a exceção gerada).
        desfecho = integracao.processar(
            ativacao,
            contexto=contexto_stealth(stealth_efetivo="habilitada", canal_avisos=canal),
            trace=trace,
        )

    assert len(construcoes) == 1
    assert desfecho.desfecho == "erro"
    assert desfecho.motivo == type(excecao).__name__
    assert desfecho.relatorio is None

    # O fallback foi de fato alcançado em todas as fontes até a exceção.
    assert fallback.urls == urls[: k + 1]

    # Mensagem da exceção nunca sai: nem no canal, nem no trace.
    assert canal.mensagens, "o desfecho erro deve chegar ao canal"
    assert all(m.startswith(PREFIXO_AVISO) for m in canal.mensagens)
    assert all(marcador not in m for m in canal.mensagens)
    assert canal.mensagens[-1] == (
        f"{PREFIXO_AVISO} "
        + _json_p9.dumps(
            {"desfecho": "erro", "motivo": type(excecao).__name__},
            ensure_ascii=False,
            sort_keys=True,
        )
    )

    eventos = [e for e in trace.eventos() if e.nome == NOME_EVENTO_TRACE_P9]
    assert len(eventos) == 1
    (evento,) = eventos
    assert evento.status == "erro"
    assert evento.saida["desfecho"] == "erro"
    assert evento.saida["motivo"] == type(excecao).__name__
    for ev in trace.eventos():
        serializado = _json_p9.dumps(
            {"entrada": ev.entrada, "saida": ev.saida, "detalhes": ev.detalhes,
             "justificativa": ev.justificativa},
            ensure_ascii=False,
            default=str,
        )
        assert marcador not in serializado

    # Trava_Navegador livre ao final.
    if trava_real:
        assert TRAVA_NAVEGADOR.acquire(blocking=False)
        TRAVA_NAVEGADOR.release()
    else:
        assert not trava.detida
        obtidas = len(trava.acquires)
        assert obtidas == k + 1 and trava.releases == obtidas



# ===========================================================================
# Exemplo (tarefa 4.5): Motivo_Ambiente_Stealth mantém o desfecho `executada`
# ===========================================================================

from tests.test_coletor_paginas import ok  # noqa: E402
from tools.buscador_paginas import FalhaBusca as _FalhaBusca45  # noqa: E402
from tools.buscador_paginas import PaginaBaixada as _PaginaBaixada45  # noqa: E402


def test_motivo_ambiente_stealth_mantem_desfecho_executada(tmp_path):
    """``ambiente_sem_display`` devolvido pelo Fallback_Stealth numa fonte da
    allowlist (urllib devolveu 403) vira ``falha`` só dessa fonte: as fontes
    seguintes são processadas (pelo fallback e pela urllib) e o
    ``DesfechoIntegracao`` é ``executada``, sem Tentativa_Fetch stealth para a
    falha de ambiente.

    **Validates: Requirements 3.4**
    """
    urls = [_url_p9(i) for i in range(3)]
    corpo_stealth = b"<html><body>stealth</body></html>"
    corpo_urllib = b"<html><body>urllib</body></html>"

    # Fonte 0: 403 → fallback sem display; fonte 1: 403 → fallback devolve página;
    # fonte 2: urllib devolve página (sem fallback).
    fallback = FallbackFake(
        {
            urls[0]: _FalhaBusca45("ambiente_sem_display", urls[0]),
            urls[1]: _PaginaBaixada45(
                status_http=200,
                content_type="text/html; charset=utf-8",
                charset="utf-8",
                url_final=urls[1],
                corpo=corpo_stealth,
            ),
        }
    )
    transporte = TransporteRoteiro(
        {
            urls[0]: (403, {}, b""),
            urls[1]: (403, {}, b""),
            urls[2]: ok(corpo_urllib),
        }
    )
    trava = TravaFake()
    relogio = Relogio()
    armazens: list[_ArmazemPaginasP9] = []

    def fabrica() -> _ColetorPaginasP9:
        armazem = _ArmazemPaginasP9(
            tmp_path / "paginas.db",
            caminho_rule_store=tmp_path / "rule_store.db",
            relogio=relogio,
        )
        armazens.append(armazem)
        return _ColetorPaginasP9(
            armazem,
            transporte=transporte,
            config=config_coleta(teto="3"),
            relogio=relogio,
            fallback_stealth=fallback,
            allowlist_stealth=ALLOWLIST_PADRAO,
            trava_navegador=trava,
        )

    integracao = IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0)
    canal = CanalFake()
    trace = _TraceCollectorP9()
    ativacao = AtivacaoPesquisa(
        codigo=CODIGO,
        marca=MARCA,
        nomes_conflitantes=("Filtro de óleo", "Filtro óleo"),
        metodo="serper",
        situacao="realizada",
        resultados=tuple(_organico_p9(url, i + 1) for i, url in enumerate(urls)),
    )

    desfecho = integracao.processar(
        ativacao,
        contexto=contexto_stealth(stealth_efetivo="habilitada", canal_avisos=canal),
        trace=trace,
    )

    # Desfecho da execução: executada, com relatório stealth.
    assert desfecho.desfecho == "executada"
    assert desfecho.motivo is None
    assert desfecho.relatorio is not None
    assert desfecho.relatorio.stealth is True

    # Todas as fontes foram processadas: urllib nas três, fallback nas duas bloqueadas.
    assert transporte.chamadas == urls
    assert fallback.urls == urls[:2]
    assert len(trava.acquires) == 2 and trava.releases == 2 and not trava.detida

    por_url = {e.url: e for e in desfecho.relatorio.entradas}
    assert set(por_url) == set(urls)

    sem_display = por_url[urls[0]]
    assert (sem_display.desfecho, sem_display.motivo) == ("falha", "ambiente_sem_display")
    assert sem_display.camada == "playwright_stealth"
    assert sem_display.motivo_urllib == "status_http"
    assert sem_display.hash_conteudo is None

    via_stealth = por_url[urls[1]]
    assert via_stealth.desfecho == "armazenado"
    assert via_stealth.camada == "playwright_stealth"
    assert via_stealth.motivo_urllib == "status_http"
    assert via_stealth.status_http == 200

    via_urllib = por_url[urls[2]]
    assert via_urllib.desfecho == "armazenado"
    assert via_urllib.camada == "urllib"
    assert via_urllib.motivo_urllib is None

    # A falha de ambiente não entra na reputação stealth do domínio; o sucesso
    # stealth da fonte 1 entra (uma única tentativa stealth).
    (armazem,) = armazens
    tentativas_stealth = armazem.ultimas_tentativas(sem_display.dominio, "playwright_stealth")
    assert [(t.sucesso, t.motivo) for t in tentativas_stealth] == [(True, None)]

    # Um único evento de trace, com desfecho executada e a contagem por camada.
    eventos = [e for e in trace.eventos() if e.nome == NOME_EVENTO_TRACE_P9]
    assert len(eventos) == 1
    (evento,) = eventos
    assert evento.saida["desfecho"] == "executada"
    assert evento.saida["contagem_camada"] == {"urllib": 1, "playwright_stealth": 2}

    # O canal termina com o desfecho executada; nenhuma mensagem de erro.
    assert all(m.startswith(PREFIXO_AVISO) for m in canal.mensagens)
    assert not any('"desfecho": "erro"' in m for m in canal.mensagens)



# ===========================================================================
# Property 10: Forma dos eventos com stealth (parte da integração, tarefa 4.6)
# ===========================================================================

import logging as _logging_p10  # noqa: E402
from collections.abc import Iterator as _IteratorP10  # noqa: E402
from contextlib import contextmanager as _contextmanager_p10  # noqa: E402
from dataclasses import replace as _replace_p10  # noqa: E402
from datetime import datetime as _datetime_p10  # noqa: E402
from datetime import timezone as _timezone_p10  # noqa: E402

from coleta_paginas.coletor import ATRIBUTO_EVENTO as _ATRIBUTO_EVENTO_P10  # noqa: E402
from coleta_paginas.coletor import CAMPOS_EVENTO_ENTRADA_STEALTH  # noqa: E402
from coleta_paginas.coletor import CAMPOS_EVENTO_RESUMO_STEALTH  # noqa: E402
from coleta_paginas.coletor import CAMPOS_EVENTO_STEALTH_INICIO  # noqa: E402
from coleta_paginas.coletor import ConfigColeta as _ConfigColetaP10  # noqa: E402
from coleta_paginas.coletor import EVENTO_ENTRADA as _EVENTO_ENTRADA_P10  # noqa: E402
from coleta_paginas.coletor import EVENTO_RESUMO as _EVENTO_RESUMO_P10  # noqa: E402
from coleta_paginas.coletor import EVENTO_STEALTH_INICIO as _EVENTO_STEALTH_INICIO_P10  # noqa: E402
from coleta_paginas.integracao import mensagem_evento as _mensagem_evento_p10  # noqa: E402
from tests.fakes_stealth import (  # noqa: E402
    CenarioStealth,
    cenarios_stealth,
    entrada_esperada_de,
    oraculo_stealth,
    preparar_execucao,
    semear_cenario,
)

_INSTANTE_P10 = _datetime_p10(2026, 3, 1, 12, 0, 0, tzinfo=_timezone_p10.utc)
_TIMEOUT_P10 = 5.0
_MAX_FONTES_P10 = 4  # Teto_Aceitos configurado = máximo de fontes do cenário
_CAMADAS_P10 = ("urllib", "playwright_stealth")
_CAMPOS_ITEM_TRACE_P10 = frozenset(
    {
        "url",
        "dominio",
        "confianca",
        "desfecho",
        "motivo",
        "status_http",
        "hash_conteudo",
        "camada",
        "motivo_urllib",
    }
)


def _relogio_p10() -> _datetime_p10:
    return _INSTANTE_P10


class _CanalLinhaDoTempoP10:
    """Canal_Avisos fake que escreve cada mensagem numa linha do tempo
    compartilhada com o ``FallbackFake`` (``("canal", mensagem)``)."""

    def __init__(self, linha_do_tempo: list[Any]) -> None:
        self.linha_do_tempo = linha_do_tempo
        self.mensagens: list[str] = []

    def __call__(self, mensagem: str) -> None:
        self.mensagens.append(mensagem)
        self.linha_do_tempo.append(("canal", mensagem))


class _HandlerColetaP10(_logging_p10.Handler):
    """Captura o dicionário de cada Evento_Log_Coleta emitido pelo coletor."""

    def __init__(self) -> None:
        super().__init__(level=_logging_p10.DEBUG)
        self.eventos: list[dict[str, Any]] = []

    def emit(self, record: _logging_p10.LogRecord) -> None:
        evento = getattr(record, _ATRIBUTO_EVENTO_P10, None)
        if evento is not None:
            self.eventos.append(dict(evento))


@_contextmanager_p10
def _capturar_eventos_p10() -> _IteratorP10[_HandlerColetaP10]:
    alvo = _logging_p10.getLogger("coleta_paginas")
    handler = _HandlerColetaP10()
    nivel_anterior = alvo.level
    alvo.addHandler(handler)
    alvo.setLevel(_logging_p10.INFO)
    try:
        yield handler
    finally:
        alvo.removeHandler(handler)
        alvo.setLevel(nivel_anterior)


def _sem_dominio_resultado_p10(cenario: CenarioStealth) -> CenarioStealth:
    """A integração converte os Resultados_Estruturados com ``dominio=None``
    (o coletor deriva o domínio do host), então o cenário também."""
    return _replace_p10(
        cenario,
        fontes=tuple(_replace_p10(f, dominio_resultado=None) for f in cenario.fontes),
    )


def _item_trace_esperado_p10(entrada: Any) -> dict[str, Any]:
    return {
        "url": entrada.url,
        "dominio": entrada.dominio,
        "confianca": str(entrada.confianca),
        "desfecho": entrada.desfecho,
        "motivo": entrada.motivo,
        "status_http": entrada.status_http,
        "hash_conteudo": entrada.hash_conteudo,
        "camada": entrada.camada,
        "motivo_urllib": entrada.motivo_urllib,
    }


def _evento_entrada_esperado_p10(entrada: Any) -> dict[str, Any]:
    return {"evento": _EVENTO_ENTRADA_P10, **_item_trace_esperado_p10(entrada),
            "codigo_peca": entrada.codigo_peca, "marca_peca": entrada.marca_peca}


# Feature: stealth-fallback-integration, Property 10: Forma dos eventos com stealth (integração)
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    cenario=cenarios_stealth(max_fontes=_MAX_FONTES_P10, com_cancelamento=False).map(_sem_dominio_resultado_p10),
)
def test_property_10_forma_dos_eventos_com_stealth_integracao(cenario: CenarioStealth):
    """Com ``stealth_efetivo == "habilitada"`` e ``ColetorPaginas`` real com
    fakes (``FallbackFake``, ``TravaFake``, transporte de roteiro):

    - cada Evento_Log_Coleta do coletor (``entrada``, ``resumo`` e
      ``stealth_inicio``) chega ao Canal_Avisos como ``mensagem_evento(e)``, na
      ordem de emissão, e nada mais chega ao canal num desfecho ``executada``;
    - as chaves de cada evento ficam restritas a
      ``CAMPOS_EVENTO_ENTRADA_STEALTH``, ``CAMPOS_EVENTO_RESUMO_STEALTH`` e
      ``CAMPOS_EVENTO_STEALTH_INICIO``;
    - cada chamada ao fallback é precedida imediatamente, na linha do tempo
      comum, pela mensagem ``stealth_inicio`` da mesma fonte, e não há
      ``stealth_inicio`` sem chamada;
    - o Evento_Coleta_Trace (``TraceCollector`` real) traz ``camada`` e
      ``motivo_urllib`` em cada item de ``entradas`` e ``contagem_camada`` com
      as duas camadas (inclusive zeros), igual à do resumo.

    **Validates: Requirements 8.2, 8.3, 8.5**
    """
    esperado = oraculo_stealth(cenario)
    linha_do_tempo: list[Any] = []
    execucao = preparar_execucao(cenario, linha_do_tempo=linha_do_tempo)
    canal = _CanalLinhaDoTempoP10(linha_do_tempo)
    trace = _TraceCollectorP9()
    resultados = [
        _organico_p9(fonte.url, posicao + 1) for posicao, fonte in enumerate(cenario.fontes)
    ]
    base = len(cenario.fontes) + 1
    resultados.extend(
        _organico_p9(url, base + j) for j, (url, _motivo) in enumerate(cenario.exclusoes)
    )
    ativacao = AtivacaoPesquisa(
        codigo=CODIGO,
        marca=MARCA,
        nomes_conflitantes=(),
        metodo="serper",
        situacao="realizada",
        resultados=tuple(resultados),
    )

    with _tempfile_p9.TemporaryDirectory(prefix="integracao_stealth_p10_") as diretorio:
        base_dir = _Path_p9(diretorio)
        armazem = _ArmazemPaginasP9(
            base_dir / "paginas.db",
            caminho_rule_store=base_dir / "rule_store.db",
            relogio=_relogio_p10,
        )
        semear_cenario(armazem, cenario, _INSTANTE_P10)

        def fabrica() -> _ColetorPaginasP9:
            return _ColetorPaginasP9(
                armazem,
                transporte=execucao.transporte,
                config=_ConfigColetaP10(
                    timeout_s=_TIMEOUT_P10,
                    teto_aceitos_ambiente=str(_MAX_FONTES_P10),
                    janela_reuso_dias=30,
                ),
                relogio=_relogio_p10,
                fallback_stealth=execucao.fallback,
                allowlist_stealth=cenario.allowlist,
                trava_navegador=execucao.trava,
            )

        integracao = IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0)
        with _capturar_eventos_p10() as capturados:
            desfecho = integracao.processar(
                ativacao,
                contexto=contexto_stealth(stealth_efetivo="habilitada", canal_avisos=canal),
                trace=trace,
            )

    # Âncora: o coletor real seguiu o oráculo do fluxograma.
    assert desfecho.desfecho == "executada"
    relatorio = desfecho.relatorio
    assert relatorio is not None and relatorio.stealth is True
    assert tuple(entrada_esperada_de(e) for e in relatorio.entradas) == esperado.entradas
    assert tuple(execucao.fallback.urls) == esperado.chamadas_fallback

    # 1. Cada evento do coletor chega ao canal como mensagem_evento(e), na ordem,
    #    e num desfecho executada não há outra mensagem.
    eventos = capturados.eventos
    assert canal.mensagens == [_mensagem_evento_p10(e) for e in eventos]

    # 2. Sequência e forma esperadas, montadas de forma independente do relatório
    #    e do oráculo: [stealth_inicio] + entrada por fonte, exclusões, resumo.
    inicio_por_url = dict(esperado.stealth_inicio)
    sequencia_esperada: list[dict[str, Any]] = []
    for entrada in relatorio.entradas:
        if entrada.url in inicio_por_url:
            sequencia_esperada.append(
                {
                    "evento": _EVENTO_STEALTH_INICIO_P10,
                    "codigo_peca": CODIGO,
                    "marca_peca": MARCA,
                    "url": entrada.url,
                    "dominio": entrada.dominio,
                    "motivo_urllib": inicio_por_url[entrada.url],
                }
            )
        sequencia_esperada.append(_evento_entrada_esperado_p10(entrada))
    contagem_camada = {
        camada: sum(1 for e in relatorio.entradas if e.camada == camada)
        for camada in _CAMADAS_P10
    }
    contagem = {d: 0 for d in ("armazenado", "reaproveitado", "falha", "url_invalida")}
    for entrada in relatorio.entradas:
        contagem[entrada.desfecho] += 1
    sequencia_esperada.append(
        {
            "evento": _EVENTO_RESUMO_P10,
            "status": "executada",
            "motivo": None,
            "codigo_peca": CODIGO,
            "marca_peca": MARCA,
            "contagem": contagem,
            "contagem_camada": contagem_camada,
        }
    )
    assert eventos == sequencia_esperada

    campos_por_evento = {
        _EVENTO_ENTRADA_P10: CAMPOS_EVENTO_ENTRADA_STEALTH,
        _EVENTO_RESUMO_P10: CAMPOS_EVENTO_RESUMO_STEALTH,
        _EVENTO_STEALTH_INICIO_P10: CAMPOS_EVENTO_STEALTH_INICIO,
    }
    for mensagem in canal.mensagens:
        assert mensagem.startswith(PREFIXO_AVISO + " ")
        dados = _json_p9.loads(mensagem[len(PREFIXO_AVISO) + 1 :])
        assert set(dados) == campos_por_evento[dados["evento"]]
    resumo = _json_p9.loads(canal.mensagens[-1][len(PREFIXO_AVISO) + 1 :])
    assert resumo["evento"] == _EVENTO_RESUMO_P10
    assert set(resumo["contagem_camada"]) == set(_CAMADAS_P10)

    # 3. Linha do tempo: cada chamada ao fallback é precedida imediatamente pelo
    #    stealth_inicio da mesma fonte; nenhum stealth_inicio sem chamada.
    inicios = [e for e in eventos if e["evento"] == _EVENTO_STEALTH_INICIO_P10]
    assert [(e["url"], e["motivo_urllib"]) for e in inicios] == list(esperado.stealth_inicio)
    chamadas = [i for i, item in enumerate(linha_do_tempo) if item[0] == "fallback"]
    assert len(chamadas) == len(inicios)
    for indice, inicio in zip(chamadas, inicios, strict=True):
        assert indice > 0
        assert linha_do_tempo[indice - 1] == ("canal", _mensagem_evento_p10(inicio))
        assert linha_do_tempo[indice] == ("fallback", inicio["url"])

    # 4. Evento_Coleta_Trace com os Campos_Stealth e contagem_camada.
    eventos_trace = [e for e in trace.eventos() if e.nome == NOME_EVENTO_TRACE_P9]
    assert len(eventos_trace) == 1
    (evento_trace,) = eventos_trace
    assert evento_trace.status == "ok"  # sem cancelamento no cenário
    saida = evento_trace.saida
    assert set(saida) == {
        "desfecho", "motivo", "duracao_ms", "contagem", "entradas", "contagem_camada"
    }
    assert saida["desfecho"] == "executada"
    assert saida["contagem"] == contagem
    assert saida["contagem_camada"] == contagem_camada == resumo["contagem_camada"]
    assert len(saida["entradas"]) == len(relatorio.entradas)
    for item, entrada in zip(saida["entradas"], relatorio.entradas, strict=True):
        assert set(item) == _CAMPOS_ITEM_TRACE_P10
        assert item == _item_trace_esperado_p10(entrada)



# ===========================================================================
# Property 11: Só campos permitidos saem com stealth (integração e pipeline, tarefa 5.6)
# ===========================================================================
#
# Marcadores únicos (gerados por exemplo) são postos em tudo o que o
# Fallback_Stealth pode trazer ou tocar: corpo HTML, Content-Type e
# ``url_final`` das páginas, Content-Type das ``FalhaBusca``, mensagens das
# exceções e o caminho do perfil (``ESTAGIARIO_STEALTH_PROFILE_DIR``). Os
# corpos urllib e os reaproveitados também levam o marcador. Nenhum deles pode
# aparecer nas saídas públicas: Canal_Avisos, Evento_Log_Coleta, trace,
# ``ResultadoCaso`` (``DecisaoCampo`` e SQL inclusos), ``PedidoIntervencao``,
# prompts do LLM fake e RuleStore.

import uuid as _uuid_p11  # noqa: E402

import pytest as _pytest_p11  # noqa: E402
from hypothesis import event, example  # noqa: E402

import config as _config_p11  # noqa: E402
from coleta_paginas.coletor import ColetorPaginas as _ColetorPaginasP11  # noqa: E402
from db.armazem_paginas import ArmazemPaginas as _ArmazemPaginasP11  # noqa: E402
from db.rule_store import RuleStore as _RuleStoreP11  # noqa: E402
from pipeline import executar_caso as _executar_caso_p11  # noqa: E402
from tests.fakes_stealth import UrllibPagina as _UrllibPaginaP11  # noqa: E402
from tests.fakes_stealth import itens_fallback as _itens_fallback_p11  # noqa: E402
from tests.test_pipeline_coleta_pbt import CABECALHOS_HTML as _CABECALHOS_HTML_P11  # noqa: E402
from tests.test_pipeline_coleta_pbt import DEPENDENCIAS_FK as _DEPENDENCIAS_FK_P11  # noqa: E402
from tests.test_pipeline_coleta_pbt import T0 as _T0_P11  # noqa: E402
from tests.test_pipeline_coleta_pbt import LLMRoteirizado as _LLMRoteirizadoP11  # noqa: E402
from tests.test_pipeline_coleta_pbt import PedirIntervencaoFake as _PedirIntervencaoP11  # noqa: E402
from tests.test_pipeline_coleta_pbt import _conteudo_rule_store as _conteudo_rule_store_p11  # noqa: E402
from tests.test_pipeline_coleta_pbt import buscar_grupo_fake as _buscar_grupo_fake_p11  # noqa: E402
from tests.test_pipeline_coleta_pbt import cenarios_grupo as _cenarios_grupo_p11  # noqa: E402
from tests.test_pipeline_coleta_pbt import NOMES as _NOMES_P11  # noqa: E402
from tests.test_pipeline_coleta_pbt import URLS as _URLS_P11  # noqa: E402
from tests.test_pipeline_coleta_pbt import EspecVerificador as _EspecVerificadorP11  # noqa: E402
from verification.models import ResultadoVerificacao as _ResultadoVerificacaoP11  # noqa: E402
from tests.test_pipeline_coleta_pbt import config_coleta as _config_coleta_pipeline_p11  # noqa: E402
from tests.test_pipeline_coleta_pbt import semear_regra as _semear_regra_p11  # noqa: E402
from tests.test_pipeline_stealth_pbt import ALLOWLIST_P12 as _ALLOWLIST_P11  # noqa: E402
from tests.test_pipeline_stealth_pbt import URLS_MERCADOCAR as _URLS_MERCADOCAR_P11  # noqa: E402
from tests.test_pipeline_stealth_pbt import _caso_p12_fixo as _caso_fixo_p11  # noqa: E402
from tests.test_pipeline_stealth_pbt import _eh_mercadocar as _eh_mercadocar_p11  # noqa: E402
from tests.test_pipeline_stealth_pbt import cenario_nomes_divergentes as _divergentes_p11  # noqa: E402
from tests.test_pipeline_stealth_pbt import especs_memoria as _especs_memoria_p11  # noqa: E402
from tests.test_pipeline_stealth_pbt import (  # noqa: E402
    especs_verificador_stealth as _especs_verificador_p11,
)
from tools.buscador_paginas import RespostaHTTP as _RespostaHTTPP11  # noqa: E402

_CONTADOR_P11 = _count_p9()
_VAR_PERFIL_P11 = "ESTAGIARIO_STEALTH_PROFILE_DIR"
_CAMPOS_POR_EVENTO_P11 = {
    _EVENTO_ENTRADA_P10: CAMPOS_EVENTO_ENTRADA_STEALTH,
    _EVENTO_RESUMO_P10: CAMPOS_EVENTO_RESUMO_STEALTH,
    _EVENTO_STEALTH_INICIO_P10: CAMPOS_EVENTO_STEALTH_INICIO,
}
_CAMPOS_DESFECHO_P11 = frozenset({"desfecho", "motivo", "variavel"})
_CAMPOS_SAIDA_TRACE_P11 = frozenset(
    {"desfecho", "motivo", "duracao_ms", "contagem", "entradas", "contagem_camada", "variavel"}
)
_CAMPOS_ENTRADA_TRACE_P11 = frozenset(
    {"codigo_peca", "marca_peca", "metodo", "quantidade_resultados"}
)

_SETTINGS_P11 = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _token_p11(semente: _uuid_p11.UUID) -> str:
    """Marcador único do exemplo. O prefixo ``mrk`` não é hexadecimal, então o
    marcador nunca aparece por acaso num Hash_Conteudo."""
    return f"mrk{next(_CONTADOR_P11)}x{semente.hex}"


def _corpo_marcado_p11(token: str) -> bytes:
    return f"<html><head><title>{token}</title></head><body>{token}</body></html>".encode()


def _content_type_marcado_p11(token: str) -> str:
    return f"text/html; charset=utf-8; x-marcador={token}"


def _url_final_marcada_p11(url: str, token: str) -> str:
    return f"{url}?destino={token}"


def _mensagem_excecao_p11(token: str) -> str:
    return f"falha do navegador {token}"


def _marcar_item_p11(item: Any, url: str, token: str) -> Any:
    """Põe o marcador em tudo o que o item do fallback carrega."""
    if isinstance(item, _PaginaBaixada45):
        return _replace_p10(
            item,
            corpo=item.corpo + _corpo_marcado_p11(token),
            content_type=_content_type_marcado_p11(token),
            url_final=_url_final_marcada_p11(url, token),
        )
    if isinstance(item, _FalhaBusca45):
        return _replace_p10(item, content_type=_content_type_marcado_p11(token))
    if isinstance(item, type) and issubclass(item, BaseException):
        return item(_mensagem_excecao_p11(token))
    if isinstance(item, BaseException):
        return type(item)(_mensagem_excecao_p11(token))
    return item


@st.composite
def _cenarios_marcados_p11(draw: st.DrawFn) -> tuple[CenarioStealth, str]:
    token = _token_p11(draw(st.uuids()))
    cenario = draw(
        cenarios_stealth(
            max_fontes=_MAX_FONTES_P10, com_cancelamento=False, com_excecao=True
        ).map(_sem_dominio_resultado_p10)
    )
    marca = _corpo_marcado_p11(token)
    fontes = tuple(
        _replace_p10(
            fonte,
            fallback=_marcar_item_p11(fonte.item_fallback, fonte.url, token),
            urllib=(
                _replace_p10(fonte.urllib, corpo=fonte.urllib.corpo + marca)
                if isinstance(fonte.urllib, _UrllibPaginaP11)
                else fonte.urllib
            ),
            reuso_corpo=None if fonte.reuso_corpo is None else fonte.reuso_corpo + marca,
        )
        for fonte in cenario.fontes
    )
    return _replace_p10(cenario, fontes=fontes), token


def _serializar_p11(valor: Any) -> str:
    return _json_p9.dumps(valor, ensure_ascii=False, default=repr, sort_keys=True)


def _eventos_trace_texto_p11(trace: _TraceCollectorP9) -> list[str]:
    return [
        _serializar_p11(
            {
                "fase": ev.fase,
                "nome": ev.nome,
                "status": ev.status,
                "entrada": ev.entrada,
                "saida": ev.saida,
                "detalhes": ev.detalhes,
                "justificativa": ev.justificativa,
            }
        )
        for ev in trace.eventos()
    ]


def _verificar_chaves_evento_p11(dados: dict[str, Any]) -> None:
    """Evento_Log_Coleta: chaves contidas no conjunto do seu tipo; mensagem de
    desfecho (sem ``evento``): só desfecho, motivo e variável."""
    if "evento" in dados:
        assert dados["evento"] in _CAMPOS_POR_EVENTO_P11, dados
        assert set(dados) <= _CAMPOS_POR_EVENTO_P11[dados["evento"]], dados
    else:
        assert set(dados) <= _CAMPOS_DESFECHO_P11, dados


def _verificar_mensagens_coleta_p11(mensagens: list[str]) -> int:
    """Chaves das mensagens ``Coleta de páginas:``; devolve quantas havia."""
    total = 0
    for mensagem in mensagens:
        if not mensagem.startswith(PREFIXO_AVISO + " "):
            continue
        total += 1
        _verificar_chaves_evento_p11(_json_p9.loads(mensagem[len(PREFIXO_AVISO) + 1 :]))
    return total


def _verificar_trace_coleta_p11(trace: _TraceCollectorP9) -> list[Any]:
    """Chaves do Evento_Coleta_Trace restritas aos Campos_Permitidos com stealth."""
    eventos = [e for e in trace.eventos() if e.nome == NOME_EVENTO_TRACE_P9]
    for evento in eventos:
        assert set(evento.entrada) <= _CAMPOS_ENTRADA_TRACE_P11
        assert set(evento.saida) <= _CAMPOS_SAIDA_TRACE_P11
        for item in evento.saida.get("entradas", ()):
            assert set(item) <= _CAMPOS_ITEM_TRACE_P10
        if "contagem_camada" in evento.saida:
            assert set(evento.saida["contagem_camada"]) == set(_CAMADAS_P10)
    return eventos


# Feature: stealth-fallback-integration, Property 11: Só campos permitidos saem com stealth (integração e pipeline)
@_SETTINGS_P11
@given(caso=_cenarios_marcados_p11())
def test_property_11_so_campos_permitidos_saem_com_stealth_integracao(caso):
    """Parte da integração: ``IntegracaoColeta`` com ``ColetorPaginas`` real,
    ``FallbackFake`` (páginas, falhas e exceções marcadas), ``TravaFake`` e
    ``TraceCollector`` real, com o perfil apontando para um caminho marcado.
    O marcador chega ao armazém (âncora), mas não aparece no Canal_Avisos, nos
    Evento_Log_Coleta nem no trace; as chaves dos eventos ficam restritas a
    ``CAMPOS_EVENTO_*_STEALTH`` e ``CAMPOS_EVENTO_STEALTH_INICIO``.

    **Validates: Requirements 4.6, 8.4**
    """
    cenario, token = caso
    esperado = oraculo_stealth(cenario)
    execucao = preparar_execucao(cenario)
    canal = CanalFake()
    trace = _TraceCollectorP9()
    resultados = [
        _organico_p9(fonte.url, posicao + 1) for posicao, fonte in enumerate(cenario.fontes)
    ]
    base = len(cenario.fontes) + 1
    resultados.extend(
        _organico_p9(url, base + j) for j, (url, _motivo) in enumerate(cenario.exclusoes)
    )
    ativacao = AtivacaoPesquisa(
        codigo=CODIGO,
        marca=MARCA,
        nomes_conflitantes=(),
        metodo="serper",
        situacao="realizada",
        resultados=tuple(resultados),
    )

    with (
        _pytest_p11.MonkeyPatch.context() as mp,
        _tempfile_p9.TemporaryDirectory(prefix="integracao_stealth_p11_") as diretorio,
    ):
        base_dir = _Path_p9(diretorio)
        mp.setenv(_VAR_PERFIL_P11, str(base_dir / f"perfil-{token}"))
        assert token in str(_config_p11.stealth_profile_dir())

        armazem = _ArmazemPaginasP11(
            base_dir / "paginas.db",
            caminho_rule_store=base_dir / "rule_store.db",
            relogio=_relogio_p10,
        )
        semear_cenario(armazem, cenario, _INSTANTE_P10)

        def fabrica() -> _ColetorPaginasP11:
            return _ColetorPaginasP11(
                armazem,
                transporte=execucao.transporte,
                config=_ConfigColetaP10(
                    timeout_s=_TIMEOUT_P10,
                    teto_aceitos_ambiente=str(_MAX_FONTES_P10),
                    janela_reuso_dias=30,
                ),
                relogio=_relogio_p10,
                fallback_stealth=execucao.fallback,
                allowlist_stealth=cenario.allowlist,
                trava_navegador=execucao.trava,
            )

        integracao = IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0)
        with _capturar_eventos_p10() as capturados:
            desfecho = integracao.processar(
                ativacao,
                contexto=contexto_stealth(stealth_efetivo="habilitada", canal_avisos=canal),
                trace=trace,
            )

        # Âncora: o coletor real seguiu o oráculo e o marcador chegou ao armazém
        # (corpo, Content-Type e url_final das páginas do fallback).
        assert execucao.fallback.urls == list(esperado.chamadas_fallback)
        if esperado.excecao is not None:
            assert (desfecho.desfecho, desfecho.motivo) == ("erro", esperado.excecao.__name__)
        else:
            assert desfecho.desfecho == "executada"
            relatorio = desfecho.relatorio
            assert relatorio is not None and relatorio.stealth is True
            assert tuple(entrada_esperada_de(e) for e in relatorio.entradas) == esperado.entradas
            registros = {r.url_original: r for r in armazem.registros_da_peca(CODIGO, MARCA)}
            for entrada in relatorio.entradas:
                if entrada.desfecho == "armazenado" and entrada.camada == "playwright_stealth":
                    assert token.encode() in armazem.ler_conteudo(entrada.hash_conteudo)
                    registro = registros[entrada.url]
                    assert token in registro.url_final
                    assert token in registro.content_type

    # Nenhum marcador no Canal_Avisos, nos Evento_Log_Coleta nem no trace.
    assert all(token not in m for m in canal.mensagens)
    assert all(token not in _serializar_p11(e) for e in capturados.eventos)
    assert all(token not in texto for texto in _eventos_trace_texto_p11(trace))

    # Chaves restritas aos Campos_Permitidos com stealth.
    for evento in capturados.eventos:
        _verificar_chaves_evento_p11(evento)
    assert _verificar_mensagens_coleta_p11(canal.mensagens) == len(canal.mensagens)
    assert len(_verificar_trace_coleta_p11(trace)) == 1


# --- Parte do pipeline -------------------------------------------------------


class _LLMRegistraPromptsP11(_LLMRoteirizadoP11):
    """LLM roteirizado que guarda também o system prompt e o schema."""

    def __init__(self, cenario: Any) -> None:
        super().__init__(cenario)
        self.prompts: list[tuple[str, str, str, Any]] = []

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.prompts.append((schema_name, system, user, json_schema))
        return super().gerar_json(system, user, json_schema, schema_name)


class _TransporteMarcadoP11:
    """Transporte_HTTP fake: 403 para a Mercadocar (fallback) e 200 HTML para os
    demais, sempre com o marcador no corpo."""

    def __init__(self, token: str) -> None:
        self._corpo = _corpo_marcado_p11(token)
        self.chamadas: list[str] = []

    def __call__(self, url, cabecalhos, timeout) -> _RespostaHTTPP11:
        self.chamadas.append(url)
        status = 403 if _eh_mercadocar_p11(url) else 200
        return _RespostaHTTPP11(
            status=status, cabecalhos=_CABECALHOS_HTML_P11, blocos=iter([self._corpo])
        )


@dataclass(frozen=True)
class _EspecFallbackP11:
    """Comportamento do Fallback_Stealth habilitado, com itens marcados em ``criar``.

    - ``sucesso``: página marcada para toda URL;
    - ``falhas_por_fonte``: ``roteiro`` URL → página, ``FalhaBusca`` ou exceção;
    - ``trava_recusada``: a Trava_Navegador nunca é obtida;
    - ``excecao``: o fallback levanta ``excecao`` com mensagem marcada.
    """

    tipo: str
    roteiro: tuple[tuple[str, Any], ...] = ()
    excecao: type[Exception] = RuntimeError

    def criar(self, token: str) -> tuple[FallbackFake, TravaFake]:
        def pagina(url: str) -> _PaginaBaixada45:
            return _marcar_item_p11(
                _PaginaBaixada45(
                    status_http=200,
                    content_type="text/html; charset=utf-8",
                    charset="utf-8",
                    url_final=url,
                    corpo=b"",
                ),
                url,
                token,
            )

        if self.tipo == "trava_recusada":
            return FallbackFake(padrao=pagina(_URLS_MERCADOCAR_P11[0])), TravaFake(padrao=False)
        if self.tipo == "excecao":
            return FallbackFake(padrao=self.excecao(_mensagem_excecao_p11(token))), TravaFake()
        if self.tipo == "falhas_por_fonte":
            roteiro = {url: _marcar_item_p11(item, url, token) for url, item in self.roteiro}
            return (
                FallbackFake(roteiro, padrao=_FalhaBusca45("timeout", "x")),
                TravaFake(),
            )
        return (
            FallbackFake(
                {url: pagina(url) for url in _URLS_MERCADOCAR_P11},
                padrao=pagina(_URLS_MERCADOCAR_P11[0]),
            ),
            TravaFake(),
        )


@st.composite
def _especs_fallback_p11(draw: st.DrawFn) -> _EspecFallbackP11:
    tipo = draw(st.sampled_from(["sucesso", "falhas_por_fonte", "trava_recusada", "excecao"]))
    if tipo == "falhas_por_fonte":
        roteiro = tuple(
            (url, draw(_itens_fallback_p11(url, com_excecao=True))) for url in _URLS_MERCADOCAR_P11
        )
        return _EspecFallbackP11(tipo, roteiro=roteiro)
    if tipo == "excecao":
        return _EspecFallbackP11(
            tipo, excecao=draw(st.sampled_from([RuntimeError, KeyError, ValueError, OSError]))
        )
    return _EspecFallbackP11(tipo)


_SEMENTE_FIXA_P11 = _uuid_p11.UUID("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")


@st.composite
def _especs_verificador_alcance_p11(draw: st.DrawFn, cenario: Any):
    """Metade das vezes, um verificador do bloco comum de P12; na outra metade,
    Resultados_Estruturados com fontes da Mercadocar que trazem código e marca
    (aceitas pela seleção e bloqueadas no urllib → fallback)."""
    if draw(st.booleans()):
        return draw(_especs_verificador_p11(cenario))
    urls = draw(st.lists(st.sampled_from(_URLS_MERCADOCAR_P11), min_size=1, max_size=3, unique=True))
    outras = draw(st.lists(st.sampled_from(_URLS_P11[:3]), max_size=1))
    itens = tuple(
        ResultadoOrganico(
            f"{cenario.search_ref} {draw(st.sampled_from(_NOMES_P11))}",
            url,
            f"ref {cenario.search_ref} {cenario.brand}",
            posicao + 1,
        )
        for posicao, url in enumerate(draw(st.permutations(urls + outras)))
    )
    resultado = _ResultadoVerificacaoP11(
        draw(st.sampled_from(["confirmado", "inconclusivo"])),
        draw(st.one_of(st.none(), st.sampled_from([r.name for r in cenario.registros]))),
        "roteiro",
        [],
    )
    return _EspecVerificadorP11("protocolo", resultado=resultado, resultados_pesquisa=itens)


@st.composite
def _casos_p11(draw: st.DrawFn):
    """Como ``casos_p12``, com peso maior no grupo de nomes divergentes (que
    sempre ativa a pesquisa) e em fontes da Mercadocar, para o fallback ser
    alcançado com mais frequência."""
    cenario = draw(
        st.one_of(_cenarios_grupo_p11(), st.just(_divergentes_p11()), st.just(_divergentes_p11()))
    )
    return cenario, draw(_especs_verificador_alcance_p11(cenario)), draw(_especs_memoria_p11)


# Feature: stealth-fallback-integration, Property 11: Só campos permitidos saem com stealth (integração e pipeline)
@_SETTINGS_P11
@given(
    caso=_casos_p11(),
    espec=_especs_fallback_p11(),
    pesquisa_web=st.sampled_from([True, True, True, False]),
    semente=st.uuids(),
    exigir_alcance=st.just(False),
)
@example(caso=_caso_fixo_p11(), espec=_EspecFallbackP11("sucesso"), pesquisa_web=True,
         semente=_SEMENTE_FIXA_P11, exigir_alcance=True)
@example(caso=_caso_fixo_p11(), espec=_EspecFallbackP11("excecao", excecao=KeyError),
         pesquisa_web=True, semente=_SEMENTE_FIXA_P11, exigir_alcance=True)
@example(caso=_caso_fixo_p11(), espec=_EspecFallbackP11("trava_recusada"), pesquisa_web=True,
         semente=_SEMENTE_FIXA_P11, exigir_alcance=True)
@example(
    caso=_caso_fixo_p11(),
    espec=_EspecFallbackP11(
        "falhas_por_fonte",
        roteiro=(
            (_URLS_MERCADOCAR_P11[0], _FalhaBusca45("ambiente_sem_display", _URLS_MERCADOCAR_P11[0])),
            (_URLS_MERCADOCAR_P11[1], _FalhaBusca45("status_http", _URLS_MERCADOCAR_P11[1],
                                                    status_http=403)),
            (_URLS_MERCADOCAR_P11[2], ValueError("z")),
        ),
    ),
    pesquisa_web=True,
    semente=_SEMENTE_FIXA_P11,
    exigir_alcance=True,
)
def test_property_11_so_campos_permitidos_saem_com_stealth_pipeline(
    caso, espec, pesquisa_web, semente, exigir_alcance, tmp_path, sem_banco
):
    """Parte do pipeline: ``executar_caso(fallback_stealth=True, coleta_html=True)``
    com fakes de LLM, banco e Verificador_Web com Resultados_Estruturados,
    RuleStore em diretório temporário, ``IntegracaoColeta`` real e
    ``ColetorPaginas`` real com ``FallbackFake``. Nenhum marcador aparece nos
    avisos (Canal_Avisos), no trace (``TraceCollector`` real), no
    ``ResultadoCaso`` (``DecisaoCampo`` e SQL inclusos), nos
    ``PedidoIntervencao``, nos prompts do LLM fake nem no RuleStore; as chaves
    dos eventos ficam restritas aos Campos_Permitidos com stealth.

    **Validates: Requirements 4.6, 8.4**
    """
    cenario, verificador_espec, memoria = caso
    token = _token_p11(semente)
    fallback, trava = espec.criar(token)
    transporte = _TransporteMarcadoP11(token)
    diretorio = _Path_p9(_tempfile_p9.mkdtemp(dir=tmp_path))
    trace = _TraceCollectorP9()
    avisos: list[str] = []
    llm = _LLMRegistraPromptsP11(cenario)

    def fabrica() -> _ColetorPaginasP11:
        armazem = _ArmazemPaginasP11(
            diretorio / "paginas.db",
            caminho_rule_store=diretorio / "memoria.db",
            relogio=lambda: _T0_P11,
        )
        return _ColetorPaginasP11(
            armazem,
            transporte=transporte,
            config=_config_coleta_pipeline_p11(),
            relogio=lambda: _T0_P11,
            fallback_stealth=fallback,
            allowlist_stealth=_ALLOWLIST_P11,
            trava_navegador=trava,
        )

    store: _RuleStoreP11 | None = None
    if memoria.usar_rule_store:
        store = _RuleStoreP11(diretorio / "memoria.db")
        if memoria.regra_previa:
            _semear_regra_p11(store, cenario)
    pedir = _PedirIntervencaoP11(memoria.pedir == "com_valor") if memoria.pedir else None

    resultado = None
    erro: str | None = None
    with _pytest_p11.MonkeyPatch.context() as mp:
        mp.setenv(_VAR_PERFIL_P11, str(diretorio / f"perfil-{token}"))
        assert token in str(_config_p11.stealth_profile_dir())
        with _capturar_eventos_p10() as capturados:
            try:
                resultado = _executar_caso_p11(
                    cenario.search_ref,
                    cenario.brand_id,
                    llm=llm,
                    dependencias_fk=_DEPENDENCIAS_FK_P11,
                    rule_store=store,
                    buscar_grupo=_buscar_grupo_fake_p11(cenario),
                    on_aviso=avisos.append,
                    verificar_web=verificador_espec.criar(),
                    pedir_intervencao=pedir,
                    trace=trace,
                    pesquisa_web=pesquisa_web,
                    coleta_html=True,
                    integracao_coleta=IntegracaoColeta(fabrica_coletor=fabrica),
                    fallback_stealth=True,
                )
            except Exception as exc:  # noqa: BLE001 — observado pelo teste
                erro = f"{type(exc).__name__}: {exc}"

    # A trava fake fica livre ao final (inclusive com exceção do fallback).
    assert not trava.detida

    # Nenhum marcador sai do core.
    assert all(token not in m for m in avisos)
    assert all(token not in texto for texto in _eventos_trace_texto_p11(trace))
    assert all(token not in _serializar_p11(e) for e in capturados.eventos)
    if resultado is not None:
        assert token not in repr(resultado)  # particao, decisoes (DecisaoCampo), grupo
        assert token not in resultado.sql
    assert erro is None or token not in erro
    pedidos = list(pedir.pedidos) if pedir else []
    assert all(token not in repr(p) for p in pedidos)
    assert all(
        token not in system and token not in user and token not in _serializar_p11(schema)
        for _nome, system, user, schema in llm.prompts
    )
    assert token not in repr(_conteudo_rule_store_p11(store))

    # Chaves restritas aos Campos_Permitidos com stealth.
    for evento in capturados.eventos:
        _verificar_chaves_evento_p11(evento)
    _verificar_mensagens_coleta_p11(avisos)
    eventos_coleta = _verificar_trace_coleta_p11(trace)
    event(f"p11 pipeline: {espec.tipo}, fallback chamado={bool(fallback.chamadas)}")

    # Nos exemplos fixos, o caminho do fallback é de fato alcançado.
    if exigir_alcance:
        assert erro is None and resultado is not None
        if espec.tipo == "trava_recusada":
            assert trava.acquires and fallback.chamadas == []
        else:
            assert fallback.chamadas
            assert all(_eh_mercadocar_p11(c.url) for c in fallback.chamadas)
        if espec.tipo == "sucesso":
            assert any(
                item.get("camada") == "playwright_stealth" and item["desfecho"] == "armazenado"
                for ev in eventos_coleta
                for item in ev.saida.get("entradas", ())
            )
