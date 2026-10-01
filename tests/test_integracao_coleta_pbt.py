"""Testes de propriedade da Integracao_Coleta (spec html-extract-on-web-search).

Organização do módulo:

1. Bloco comum: estratégias Hypothesis reutilizáveis (``ResultadoOrganico``,
   ``AtivacaoPesquisa``, ``ContextoPesquisaWeb``) e fakes (coletor contador,
   fábrica contadora, canal de avisos, trace, sinal de cancelamento).
2. Uma seção por propriedade do design, na ordem numérica.

Todos os testes rodam com a guarda de rede e o isolamento de ambiente da
feature (``ESTAGIARIO_PAGINAS_DB_PATH`` em ``tmp_path``, ``db/paginas.db`` da raiz
verificado no teardown). Nenhum teste abre rede, usa LLM ou depende de gates
``ESTAGIARIO_RUN_*``.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from arbitration.pesquisa_web import AtivacaoPesquisa, ContextoPesquisaWeb
from coleta_paginas.coletor import ColetorPaginas
from coleta_paginas.integracao import (
    PREFIXO_AVISO,
    IntegracaoColeta,
    converter_resultados,
    peca_da_ativacao,
)
from coleta_paginas.modelos import PecaConsultada, RelatorioColeta, ResultadoBusca
from coleta_paginas.selecao import codigo_valido, marca_sem_referencia
from db.armazem_paginas import ArmazemPaginas
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import estado_arquivo, isolamento_coleta_autouse  # noqa: F401
from tools.buscador_paginas import RespostaHTTP
from verification.serper_client import ResultadoOrganico

# ===========================================================================
# Bloco comum: estratégias
# ===========================================================================

# Texto livre com acentos, caixa mista, espaços nas extremidades e símbolos.
# Exclui só surrogates (não codificáveis em UTF-8).
_ALFABETO_LIVRE = st.characters(exclude_categories=("Cs",))
st_texto_livre = st.text(alphabet=_ALFABETO_LIVRE, max_size=40)

# Fragmentos que exercitam acentos, caixa, espaços e marcas do incidente.
_FRAGMENTOS = (
    "CF1000", "cf 1000/3", "  MANN-FILTER ", "Mann Filter", "Filtro de Ar 2°",
    "ÓLEO", "conversão", "OEM", "original oem", "\t", " ", "çãõ", "Ž",
)
st_texto_realista = st.lists(
    st.sampled_from(_FRAGMENTOS) | st_texto_livre, min_size=0, max_size=4
).map("".join)

st_texto = st_texto_livre | st_texto_realista

st_posicao = st.none() | st.integers(min_value=-5, max_value=200)

st_link = st.sampled_from(
    (
        "https://www.exemplo.com.br/cf1000",
        "http://loja.exemplo.com/p?id=1&x=%20",
        "HTTPS://EXEMPLO.COM/Filtro",
        "ftp://nao-http.exemplo/arquivo",
        "",
        "   ",
        "not a url",
    )
) | st_texto_livre


@st.composite
def st_resultado_organico(draw: st.DrawFn) -> ResultadoOrganico:
    """``ResultadoOrganico`` com título, link, snippet e posição arbitrários."""
    return ResultadoOrganico(
        title=draw(st_texto),
        link=draw(st_link),
        snippet=draw(st_texto),
        position=draw(st_posicao),
    )


def st_resultados_organicos(max_size: int = 10) -> st.SearchStrategy[tuple[ResultadoOrganico, ...]]:
    """Tupla (possivelmente vazia) de orgânicos, com duplicatas permitidas."""
    return st.lists(st_resultado_organico(), max_size=max_size).map(tuple)


# Código e marca arbitrários (inclusive ``None``, vazio, só espaços, genéricos).
st_codigo_qualquer = st.none() | st_texto
st_marca_qualquer = st.none() | st.sampled_from(
    ("", "   ", "OEM", "Original OEM", "CONVERSÃO", "MANN-FILTER", "mann filter", " Bosch ")
) | st_texto

# Código válido para a Trava_Entrada e marca com referência (não dispara a trava).
st_codigo_valido = (
    st.sampled_from(("CF1000", "cf 1000/3", " W 712/75 ", "ÓLEO-1", "a"))
    | st_texto
).filter(codigo_valido)
st_marca_com_referencia = (
    st.sampled_from(("MANN-FILTER", "Mann Filter", " Bosch ", "Fram", "Tecfil"))
    | st_texto
).filter(lambda marca: not marca_sem_referencia(marca))

st_nomes_conflitantes = st.lists(st_texto, max_size=5).map(tuple)

st_metodo = st.sampled_from(("serper", "playwright", "injetado", "desligada"))
st_situacao = st.sampled_from(("realizada", "nao_realizada", "sem_estruturados", "desligada"))


@st.composite
def st_ativacao(
    draw: st.DrawFn,
    *,
    codigo: st.SearchStrategy[str | None] = st_codigo_qualquer,
    marca: st.SearchStrategy[str | None] = st_marca_qualquer,
    metodo: st.SearchStrategy[str] = st_metodo,
    situacao: st.SearchStrategy[str] = st_situacao,
) -> AtivacaoPesquisa:
    """``AtivacaoPesquisa`` coerente: ``resultados`` só existem em ``realizada``."""
    situacao_escolhida = draw(situacao)
    resultados = (
        draw(st_resultados_organicos()) if situacao_escolhida == "realizada" else None
    )
    return AtivacaoPesquisa(
        codigo=draw(codigo),
        marca=draw(marca),
        nomes_conflitantes=draw(st_nomes_conflitantes),
        metodo=draw(metodo),
        situacao=situacao_escolhida,
        resultados=resultados,
    )


def st_ativacao_elegivel() -> st.SearchStrategy[AtivacaoPesquisa]:
    """Ativação que passa por toda a precedência do Req 4.5 (com coleta
    habilitada e sem cancelamento): Serper com Pesquisa_Realizada, código
    válido e marca com referência."""
    return st_ativacao(
        codigo=st_codigo_valido,
        marca=st_marca_com_referencia,
        metodo=st.just("serper"),
        situacao=st.just("realizada"),
    )


# ===========================================================================
# Bloco comum: fakes
# ===========================================================================


@dataclass
class ChamadaColetar:
    """Argumentos recebidos por ``ColetorFakeContador.coletar``."""

    peca: PecaConsultada
    resultados: tuple[ResultadoBusca, ...]
    cancelado: Any
    observador: Any


@dataclass
class ColetorFakeContador:
    """Coletor fake: registra cada chamada e devolve um relatório ``executada``
    sem entradas. Sem armazém, sem transporte, sem rede."""

    chamadas: list[ChamadaColetar] = field(default_factory=list)

    def coletar(self, peca, resultados, *, teto_aceitos=None, cancelado=None, observador=None):
        self.chamadas.append(
            ChamadaColetar(
                peca=peca,
                resultados=tuple(resultados),
                cancelado=cancelado,
                observador=observador,
            )
        )
        return RelatorioColeta(
            status="executada",
            motivo=None,
            codigo_peca=peca.codigo_peca,
            marca_peca=peca.marca_peca,
            entradas=(),
        )


@dataclass
class FabricaColetorContador:
    """Fábrica de coletor injetável em ``IntegracaoColeta``; conta construções."""

    coletor: ColetorFakeContador = field(default_factory=ColetorFakeContador)
    construcoes: int = 0

    def __call__(self) -> ColetorFakeContador:
        self.construcoes += 1
        return self.coletor


@dataclass
class CanalFake:
    """Canal_Avisos fake: acumula as mensagens na ordem."""

    mensagens: list[str] = field(default_factory=list)

    def __call__(self, mensagem: str) -> None:
        self.mensagens.append(mensagem)


@dataclass
class EventoTraceFake:
    fase: str
    nome: str
    kwargs: dict[str, Any]


@dataclass
class TraceFake:
    """``TraceSink`` fake: registra ``registrar(fase, nome, **kwargs)``."""

    eventos: list[EventoTraceFake] = field(default_factory=list)

    def registrar(self, fase: str, nome: str, **kwargs: Any) -> None:
        self.eventos.append(EventoTraceFake(fase=fase, nome=nome, kwargs=kwargs))


@dataclass
class SinalFake:
    """Sinal_Cancelamento fake com estado fixo."""

    ativo: bool = False

    def is_set(self) -> bool:
        return self.ativo


def contexto_coleta(
    *,
    coleta_efetiva: str = "habilitada",
    cancel_event: SinalFake | None = None,
    canal_avisos: CanalFake | None = None,
    pesquisa_habilitada: bool = True,
) -> ContextoPesquisaWeb:
    """``ContextoPesquisaWeb`` para os testes da integração."""
    return ContextoPesquisaWeb(
        pesquisa_habilitada=pesquisa_habilitada,
        coleta_efetiva=coleta_efetiva,
        cancel_event=cancel_event,
        canal_avisos=canal_avisos,
    )


def integracao_com_fake() -> tuple[IntegracaoColeta, FabricaColetorContador]:
    """``IntegracaoColeta`` com fábrica fake contadora e relógio monotônico fixo."""
    fabrica = FabricaColetorContador()
    return IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0), fabrica


_SETTINGS_PBT = settings(
    max_examples=100,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _assert_conversao_1_1(
    organicos: tuple[ResultadoOrganico, ...], convertidos: tuple[ResultadoBusca, ...]
) -> None:
    assert isinstance(convertidos, tuple)
    assert len(convertidos) == len(organicos)
    for organico, busca in zip(organicos, convertidos, strict=True):
        assert isinstance(busca, ResultadoBusca)
        assert busca.titulo == organico.title
        assert busca.snippet == organico.snippet
        assert busca.url == organico.link
        assert busca.posicao == organico.position
        assert busca.dominio is None


# ===========================================================================
# Property 2: Conversão 1:1 e peça espelhada
# ===========================================================================


# Feature: html-extract-on-web-search, Property 2: Conversão 1:1 e peça espelhada
@_SETTINGS_PBT
@given(organicos=st_resultados_organicos(max_size=15))
def test_property_2_conversao_um_para_um(organicos):
    """**Validates: Requirements 2.2, 2.4**"""
    convertidos = converter_resultados(organicos)
    _assert_conversao_1_1(organicos, convertidos)
    # Aceita também list e não muta a entrada.
    assert converter_resultados(list(organicos)) == convertidos


# Feature: html-extract-on-web-search, Property 2: Conversão 1:1 e peça espelhada
@_SETTINGS_PBT
@given(ativacao=st_ativacao())
def test_property_2_peca_espelhada(ativacao):
    """**Validates: Requirements 2.3**"""
    peca = peca_da_ativacao(ativacao)
    # Igualdade exata (caractere a caractere) e identidade de tipo, sem strip/casefold.
    assert peca.codigo_peca == ativacao.codigo
    assert type(peca.codigo_peca) is type(ativacao.codigo)
    assert peca.marca_peca == ativacao.marca
    assert type(peca.marca_peca) is type(ativacao.marca)
    assert peca.nomes_candidatos == tuple(ativacao.nomes_conflitantes)


# Feature: html-extract-on-web-search, Property 2: Conversão 1:1 e peça espelhada
@_SETTINGS_PBT
@given(ativacao=st_ativacao_elegivel())
def test_property_2_coletor_recebe_exatamente_os_convertidos(ativacao):
    """Sem filtrar, reordenar nem completar antes da chamada; peça espelhada.

    **Validates: Requirements 2.2, 2.3, 2.4, 2.5**
    """
    integracao, fabrica = integracao_com_fake()
    desfecho = integracao.processar(
        ativacao, contexto=contexto_coleta(coleta_efetiva="habilitada"), trace=None
    )

    assert desfecho.desfecho == "executada"
    assert fabrica.construcoes == 1
    assert len(fabrica.coletor.chamadas) == 1
    chamada = fabrica.coletor.chamadas[0]

    assert chamada.resultados == converter_resultados(ativacao.resultados)
    _assert_conversao_1_1(ativacao.resultados, chamada.resultados)
    assert chamada.peca == peca_da_ativacao(ativacao)
    assert chamada.peca.codigo_peca == ativacao.codigo
    assert chamada.peca.marca_peca == ativacao.marca
    assert chamada.peca.nomes_candidatos == ativacao.nomes_conflitantes



# ===========================================================================
# Property 3: Um desfecho por ativação, com precedência
# ===========================================================================

# Situações possíveis por método, como produzidas por ``_arbitrar_nome_via_web``:
# Serper tem ou não Pesquisa_Realizada; Playwright nunca entrega estruturados;
# o injetado pode ou não implementar o protocolo; "desligada" só na Pesquisa_Desligada.
_SITUACOES_POR_METODO = {
    "serper": ("realizada", "nao_realizada"),
    "playwright": ("sem_estruturados",),
    "injetado": ("realizada", "nao_realizada", "sem_estruturados"),
    "desligada": ("desligada",),
}

# Mistura códigos/marcas que passam e que disparam a trava (ou a validação).
st_codigo_p3 = st_codigo_valido | st_codigo_qualquer
st_marca_p3 = st_marca_com_referencia | st_marca_qualquer


@st.composite
def st_ativacao_coerente(draw: st.DrawFn) -> AtivacaoPesquisa:
    """Ativação com método e situação coerentes entre si."""
    metodo = draw(st_metodo)
    return draw(
        st_ativacao(
            codigo=st_codigo_p3,
            marca=st_marca_p3,
            metodo=st.just(metodo),
            situacao=st.sampled_from(_SITUACOES_POR_METODO[metodo]),
        )
    )


st_coleta_efetiva = st.sampled_from(("habilitada", "desabilitada", "invalida"))
st_sinal = st.sampled_from(("ausente", "inativo", "ativo"))


def _oraculo_precedencia(
    ativacao: AtivacaoPesquisa, coleta: str, cancelado: bool
) -> tuple[str, str | None]:
    """Oráculo do Req 4.5 escrito de forma independente da implementação:
    primeira condição aplicável, na ordem da especificação, até a trava.

    Devolve ``(desfecho, motivo)``. Um código inválido faz a Trava_Entrada
    levantar ``CodigoPecaInvalidoError`` (html-extract-save, Req 1.5), que a
    integração transforma em ``erro`` (Req 5.1)."""
    ordem = (
        (ativacao.situacao == "desligada", ("nao_executada", "pesquisa_desligada")),
        (coleta == "desabilitada", ("nao_executada", "desabilitada")),
        (coleta == "invalida", ("nao_executada", "configuracao_invalida")),
        (cancelado, ("nao_executada", "cancelada")),
        (
            ativacao.situacao == "sem_estruturados",
            ("nao_executada", "sem_resultados_estruturados"),
        ),
        (
            ativacao.situacao == "nao_realizada",
            ("nao_executada", "pesquisa_nao_realizada"),
        ),
        (not codigo_valido(ativacao.codigo), ("erro", "CodigoPecaInvalidoError")),
        (marca_sem_referencia(ativacao.marca), ("nao_enriquecivel", "marca_sem_referencia")),
    )
    for condicao, desfecho in ordem:
        if condicao:
            return desfecho
    return ("executada", None)


_STATUS_TRACE_ESPERADO = {
    "executada": "ok",  # o coletor fake não produz entradas canceladas
    "nao_enriquecivel": "nao_enriquecivel",
    "nao_executada": "nao_executada",
    "erro": "erro",
}


# Feature: html-extract-on-web-search, Property 3: Um desfecho por ativação, com precedência
@_SETTINGS_PBT
@given(
    ativacao=st_ativacao_coerente(),
    coleta=st_coleta_efetiva,
    sinal=st_sinal,
)
def test_property_3_um_desfecho_por_ativacao_com_precedencia(ativacao, coleta, sinal):
    """**Validates: Requirements 3.1, 4.1, 4.2, 4.3, 4.4, 4.5, 6.4, 8.3, 9.4, 9.5, 10.14**"""
    cancel_event = None if sinal == "ausente" else SinalFake(ativo=sinal == "ativo")
    canal = CanalFake()
    trace = TraceFake()
    integracao, fabrica = integracao_com_fake()

    desfecho = integracao.processar(
        ativacao,
        contexto=contexto_coleta(
            coleta_efetiva=coleta, cancel_event=cancel_event, canal_avisos=canal
        ),
        trace=trace,
    )

    esperado, motivo_esperado = _oraculo_precedencia(ativacao, coleta, sinal == "ativo")

    # Desfecho único, igual ao oráculo.
    assert (desfecho.desfecho, desfecho.motivo) == (esperado, motivo_esperado)
    assert desfecho.metodo == ativacao.metodo
    assert desfecho.codigo_peca == ativacao.codigo
    assert desfecho.marca_peca == ativacao.marca
    assert desfecho.quantidade_resultados == len(ativacao.resultados or ())
    if motivo_esperado == "configuracao_invalida":
        assert desfecho.variavel == "ESTAGIARIO_COLETA_HABILITADA"
    else:
        assert desfecho.variavel is None

    # Coletor (e sua fábrica) acionado se e somente se elegível.
    elegivel = esperado == "executada"
    assert len(fabrica.coletor.chamadas) == (1 if elegivel else 0)
    assert fabrica.construcoes == (1 if elegivel else 0)

    # Exatamente um evento de trace ``coletar_paginas`` com o status do desfecho.
    eventos = [e for e in trace.eventos if e.nome == "coletar_paginas"]
    assert len(trace.eventos) == 1
    assert len(eventos) == 1
    assert eventos[0].fase == "tool"
    assert eventos[0].kwargs["status"] == _STATUS_TRACE_ESPERADO[esperado]
    assert eventos[0].kwargs["saida"]["desfecho"] == esperado
    assert eventos[0].kwargs["saida"]["motivo"] == motivo_esperado

    # Canal: uma única mensagem de desfecho fora de ``executada``; o coletor
    # fake não emite eventos, então ``executada`` não gera mensagens.
    com_prefixo = [m for m in canal.mensagens if m.startswith(PREFIXO_AVISO)]
    assert com_prefixo == canal.mensagens
    if elegivel:
        assert canal.mensagens == []
    else:
        assert len(canal.mensagens) == 1
        dados = json.loads(canal.mensagens[0][len(PREFIXO_AVISO):])
        assert dados["desfecho"] == esperado
        assert dados["motivo"] == motivo_esperado
        assert ("variavel" in dados) == (motivo_esperado == "configuracao_invalida")
        if "variavel" in dados:
            assert dados["variavel"] == "ESTAGIARIO_COLETA_HABILITADA"



# ===========================================================================
# Property 4: Não executar não tem efeitos
# ===========================================================================

# Marcas que disparam a Trava_Entrada (genéricas, ausentes, vazias, só espaços).
st_marca_sem_referencia = (
    st.sampled_from((None, "", "   ", "\t", "OEM", "Original OEM", "CONVERSÃO", " oem "))
    | st_texto
).filter(marca_sem_referencia)


def _organicos_com_codigo(codigo: str, marca: str | None, n: int) -> tuple[ResultadoOrganico, ...]:
    """Orgânicos em que título, snippet e URL contêm o código explícito."""
    return tuple(
        ResultadoOrganico(
            title=f"{marca or ''} {codigo} - Filtro",
            link=f"https://loja{i}.exemplo.com.br/p/{quote(codigo, safe='')}",
            snippet=f"Peça {codigo} original",
            position=i + 1,
        )
        for i in range(n)
    )


@st.composite
def st_ativacao_trava(draw: st.DrawFn) -> AtivacaoPesquisa:
    """Ativação elegível em tudo exceto a marca: Serper com Pesquisa_Realizada,
    código válido e marca sem referência, com todos os resultados contendo o
    código explícito (Req 3.4)."""
    codigo = draw(st_codigo_valido)
    marca = draw(st_marca_sem_referencia)
    return AtivacaoPesquisa(
        codigo=codigo,
        marca=marca,
        nomes_conflitantes=draw(st_nomes_conflitantes),
        metodo=draw(st.sampled_from(("serper", "injetado"))),
        situacao="realizada",
        resultados=_organicos_com_codigo(codigo, marca, draw(st.integers(0, 8))),
    )


@st.composite
def st_ativacao_p4(draw: st.DrawFn) -> AtivacaoPesquisa:
    """Ativações coerentes; em ``realizada`` com código em texto, às vezes troca
    os resultados por orgânicos que contêm o código explícito."""
    ativacao = draw(st_ativacao_coerente() | st_ativacao_trava())
    if (
        ativacao.situacao == "realizada"
        and isinstance(ativacao.codigo, str)
        and draw(st.booleans())
    ):
        ativacao = AtivacaoPesquisa(
            codigo=ativacao.codigo,
            marca=ativacao.marca,
            nomes_conflitantes=ativacao.nomes_conflitantes,
            metodo=ativacao.metodo,
            situacao=ativacao.situacao,
            resultados=_organicos_com_codigo(
                ativacao.codigo, ativacao.marca, draw(st.integers(0, 8))
            ),
        )
    return ativacao


@dataclass
class TransporteContador:
    """Transporte_HTTP fake: conta chamadas e devolve uma página com o código."""

    chamadas: list[str] = field(default_factory=list)

    def __call__(self, url, cabecalhos, timeout):
        self.chamadas.append(url)
        return RespostaHTTP(
            status=200,
            cabecalhos={"content-type": "text/html; charset=utf-8"},
            blocos=[b"<html><body>CF1000 pagina</body></html>"],
        )


@dataclass
class FabricaRealContadora:
    """Fábrica que, se chamada, constrói o coletor real sobre ``caminho`` (o que
    criaria/abriria o arquivo do armazém). Conta as construções."""

    caminho: Path
    transporte: TransporteContador
    construcoes: int = 0

    def __call__(self) -> ColetorPaginas:
        self.construcoes += 1
        return ColetorPaginas(ArmazemPaginas(self.caminho), transporte=self.transporte)


def _conteudo_sqlite(caminho: Path) -> dict[str, list[tuple]]:
    """Todas as linhas de todas as tabelas, em leitura somente (``mode=ro``)."""
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    try:
        tabelas = [
            linha[0]
            for linha in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            tabela: sorted(
                (tuple(linha) for linha in conn.execute(f'SELECT * FROM "{tabela}"')),
                key=repr,
            )
            for tabela in tabelas
        }
    finally:
        conn.close()


# Feature: html-extract-on-web-search, Property 4: Não executar não tem efeitos
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    ativacao=st_ativacao_p4(),
    coleta=st_coleta_efetiva,
    sinal=st_sinal,
    armazem_existe=st.booleans(),
    fabrica_padrao=st.booleans(),
)
def test_property_4_nao_executar_nao_tem_efeitos(
    monkeypatch, ativacao, coleta, sinal, armazem_existe, fabrica_padrao
):
    """Para todo desfecho oráculo diferente de ``executada`` (``nao_executada``,
    ``nao_enriquecivel`` e ``erro`` antes do coletor): zero chamadas ao
    transporte, fábrica não chamada, arquivo do armazém não criado e, se já
    existia, inalterado (existência, tamanho, mtime e conteúdo).

    **Validates: Requirements 3.2, 3.4, 4.1, 6.4, 9.5, 10.14**
    """
    cancelado = sinal == "ativo"
    esperado, _ = _oraculo_precedencia(ativacao, coleta, cancelado)
    assume(esperado != "executada")

    cancel_event = None if sinal == "ausente" else SinalFake(ativo=cancelado)

    with tempfile.TemporaryDirectory() as diretorio:
        pasta = Path(diretorio)
        caminho = pasta / "armazem" / "paginas.db"
        # O caminho padrão (sem fábrica injetada) vem de ESTAGIARIO_PAGINAS_DB_PATH.
        monkeypatch.setenv("ESTAGIARIO_PAGINAS_DB_PATH", str(caminho))

        conteudo_antes = None
        if armazem_existe:
            armazem = ArmazemPaginas(caminho)
            armazem.armazenar_conteudo(b"<html>conteudo previo</html>")
            conteudo_antes = _conteudo_sqlite(caminho)
        arquivos_antes = sorted(p.relative_to(pasta) for p in pasta.rglob("*"))
        estado_antes = estado_arquivo(caminho)

        transporte = TransporteContador()
        if fabrica_padrao:
            fabrica = None
            integracao = IntegracaoColeta(
                transporte=transporte, relogio_monotonico=lambda: 0.0
            )
        else:
            fabrica = FabricaRealContadora(caminho=caminho, transporte=transporte)
            integracao = IntegracaoColeta(
                fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0
            )

        desfecho = integracao.processar(
            ativacao,
            contexto=contexto_coleta(
                coleta_efetiva=coleta, cancel_event=cancel_event, canal_avisos=CanalFake()
            ),
            trace=TraceFake(),
        )

        assert desfecho.desfecho == esperado
        assert transporte.chamadas == []
        if fabrica is not None:
            assert fabrica.construcoes == 0

        # Arquivo do armazém: não criado quando ausente; intocado quando existente.
        assert estado_arquivo(caminho) == estado_antes
        assert estado_antes.existe == armazem_existe
        assert sorted(p.relative_to(pasta) for p in pasta.rglob("*")) == arquivos_antes
        if armazem_existe:
            assert _conteudo_sqlite(caminho) == conteudo_antes



# ===========================================================================
# Property 5: Despacho por sequência de ativações
# ===========================================================================


@st.composite
def st_ativacao_elegivel_vazia(draw: st.DrawFn) -> AtivacaoPesquisa:
    """Ativação elegível com Pesquisa_Realizada e lista vazia de orgânicos (Req 1.3)."""
    ativacao = draw(st_ativacao_elegivel())
    return AtivacaoPesquisa(
        codigo=ativacao.codigo,
        marca=ativacao.marca,
        nomes_conflitantes=ativacao.nomes_conflitantes,
        metodo=ativacao.metodo,
        situacao="realizada",
        resultados=(),
    )


# Mistura ativações elegíveis (com e sem resultados) e quaisquer outras coerentes.
st_ativacao_p5 = st_ativacao_elegivel() | st_ativacao_elegivel_vazia() | st_ativacao_coerente()

# A Coleta_Efetiva é fixa numa execução; privilegia "habilitada" para haver elegíveis.
st_coleta_p5 = st.sampled_from(("habilitada", "habilitada", "habilitada", "desabilitada", "invalida"))


# Feature: html-extract-on-web-search, Property 5: Despacho por sequência de ativações
@_SETTINGS_PBT
@given(
    ativacoes=st.lists(st_ativacao_p5, min_size=0, max_size=8),
    coleta=st_coleta_p5,
    sinal=st_sinal,
)
def test_property_5_despacho_por_sequencia_de_ativacoes(ativacoes, coleta, sinal):
    """Numa mesma instância de ``IntegracaoColeta`` (uma execução de
    ``executar_caso``), as chamadas ao coletor são exatamente a subsequência das
    ativações elegíveis, na ordem, cada uma com os resultados convertidos e a
    peça da própria ativação (inclusive a tupla vazia). A fábrica é chamada no
    máximo uma vez, na primeira ativação elegível, e nunca se nenhuma é elegível.
    Cada ativação produz exatamente um desfecho.

    O ``status`` do ``ResultadoVerificacao`` não entra na ``AtivacaoPesquisa``,
    então o despacho independe dele por construção (Req 1.2).

    **Validates: Requirements 1.1, 1.2, 1.3, 1.4, 2.5, 9.8**
    """
    cancelado = sinal == "ativo"
    cancel_event = None if sinal == "ausente" else SinalFake(ativo=cancelado)
    contexto = contexto_coleta(
        coleta_efetiva=coleta, cancel_event=cancel_event, canal_avisos=CanalFake()
    )
    trace = TraceFake()
    integracao, fabrica = integracao_com_fake()

    elegiveis: list[AtivacaoPesquisa] = []
    for indice, ativacao in enumerate(ativacoes):
        desfecho = integracao.processar(ativacao, contexto=contexto, trace=trace)

        esperado, motivo_esperado = _oraculo_precedencia(ativacao, coleta, cancelado)
        assert (desfecho.desfecho, desfecho.motivo) == (esperado, motivo_esperado)
        if esperado == "executada":
            elegiveis.append(ativacao)

        # Fábrica preguiçosa: construída só a partir da primeira elegível, uma vez.
        assert fabrica.construcoes == (1 if elegiveis else 0)
        # Um desfecho (um evento de trace) por ativação, na ordem.
        assert len(trace.eventos) == indice + 1

    chamadas = fabrica.coletor.chamadas
    assert len(chamadas) == len(elegiveis)
    for chamada, ativacao in zip(chamadas, elegiveis, strict=True):
        assert chamada.peca == peca_da_ativacao(ativacao)
        assert chamada.resultados == converter_resultados(ativacao.resultados)
        _assert_conversao_1_1(ativacao.resultados, chamada.resultados)

    assert all(e.nome == "coletar_paginas" for e in trace.eventos)
    assert [e.kwargs["entrada"]["codigo_peca"] for e in trace.eventos] == [
        a.codigo for a in ativacoes
    ]




# ===========================================================================
# Property 6: Falhas da coleta viram `erro` sem propagar
# ===========================================================================

from coleta_paginas import integracao as _modulo_integracao  # noqa: E402
from coleta_paginas.modelos import (  # noqa: E402
    CodigoPecaInvalidoError,
    ConfiguracaoColetaError,
)
from db.armazem_paginas import ConflitoCaminhoArmazemError  # noqa: E402
from tools.buscador_paginas import ErroTransporte  # noqa: E402

# Classes-base derivadas de ``Exception``, todas construíveis com uma mensagem.
_BASES_EXCECAO: tuple[type[Exception], ...] = (
    Exception,
    ValueError,
    TypeError,
    RuntimeError,
    KeyError,
    LookupError,
    OSError,
    ConnectionError,
    TimeoutError,
    sqlite3.OperationalError,
    ConflitoCaminhoArmazemError,
    CodigoPecaInvalidoError,
    ConfiguracaoColetaError,
    ErroTransporte,
)

st_nome_classe = st.from_regex(r"[A-Z][A-Za-z0-9_]{0,20}Error", fullmatch=True)

# Classe de exceção: uma base conhecida ou uma subclasse gerada com nome novo.
st_classe_excecao = st.sampled_from(_BASES_EXCECAO) | st.builds(
    lambda nome, base: type(nome, (base,), {}),
    st_nome_classe,
    st.sampled_from(_BASES_EXCECAO),
)

# Pontos de falha na execução (antes da publicação) → desfecho ``erro``.
PONTOS_EXECUCAO = ("fabrica", "trava", "coletar", "observador")
# Pontos de falha só na publicação → desfecho calculado permanece.
PONTOS_PUBLICACAO = ("canal_publicacao", "trace_registrar")

st_marcador = st.text(alphabet="0123456789abcdef", min_size=8, max_size=16).map(
    lambda sufixo: f"<<MSG-EXC-{sufixo}>>"
)


def _instanciar_excecao(classe: type[Exception], marcador: str) -> Exception:
    """Instância com ``marcador`` na mensagem; ``ConflitoCaminhoArmazemError``
    exige os dois caminhos, que entram na mensagem."""
    if issubclass(classe, ConflitoCaminhoArmazemError):
        return classe(Path(f"/a/{marcador}"), Path(f"/b/{marcador}"))
    if issubclass(classe, ConfiguracaoColetaError):
        return classe("teto_aceitos", marcador, "ambiente")
    return classe(marcador)


@dataclass
class CanalQueFalha:
    """Canal_Avisos que registra a tentativa e levanta ``exc`` em toda chamada."""

    exc: Exception
    tentativas: list[str] = field(default_factory=list)

    def __call__(self, mensagem: str) -> None:
        self.tentativas.append(mensagem)
        raise self.exc


@dataclass
class TraceQueFalha:
    """``TraceSink`` que registra a tentativa e levanta ``exc``."""

    exc: Exception
    tentativas: list[EventoTraceFake] = field(default_factory=list)

    def registrar(self, fase: str, nome: str, **kwargs: Any) -> None:
        self.tentativas.append(EventoTraceFake(fase=fase, nome=nome, kwargs=kwargs))
        raise self.exc


@dataclass
class ColetorQueFalha:
    """Coletor fake cujo ``coletar`` levanta ``exc``."""

    exc: Exception
    chamadas: int = 0

    def coletar(self, peca, resultados, *, teto_aceitos=None, cancelado=None, observador=None):
        self.chamadas += 1
        raise self.exc


@dataclass
class ColetorQueEmite:
    """Coletor fake que emite um Evento_Log_Coleta pelo observador recebido e
    depois devolve um relatório ``executada`` sem entradas."""

    chamadas: int = 0

    def coletar(self, peca, resultados, *, teto_aceitos=None, cancelado=None, observador=None):
        self.chamadas += 1
        if observador is not None:
            observador(
                {
                    "evento": "coleta_paginas.resumo",
                    "status": "executada",
                    "codigo_peca": peca.codigo_peca,
                }
            )
        return RelatorioColeta(
            status="executada",
            motivo=None,
            codigo_peca=peca.codigo_peca,
            marca_peca=peca.marca_peca,
            entradas=(),
        )


def _texto_publicado(canal_msgs: list[str], eventos: list[EventoTraceFake]) -> str:
    """Tudo o que saiu (ou tentou sair) no canal e no trace, como texto."""
    return "\n".join(canal_msgs) + "\n" + "\n".join(repr(e) for e in eventos)


# Feature: html-extract-on-web-search, Property 6: Falhas da coleta viram `erro` sem propagar
@_SETTINGS_PBT
@given(
    ativacao=st_ativacao_elegivel(),
    classe=st_classe_excecao,
    ponto=st.sampled_from(PONTOS_EXECUCAO),
    marcador=st_marcador,
)
def test_property_6_falha_na_execucao_vira_erro(monkeypatch, ativacao, classe, ponto, marcador):
    """Falha na fábrica, na Trava_Entrada, em ``coletar`` ou no observador
    (canal levantando dentro de ``coletar``): ``processar`` não levanta, o
    desfecho é ``erro`` com ``motivo == type(exc).__name__`` e a mensagem da
    exceção não aparece no canal nem no trace.

    **Validates: Requirements 5.1**
    """
    assume(marcador not in repr(ativacao))
    exc = _instanciar_excecao(classe, marcador)
    assert marcador in str(exc)
    assume(marcador not in type(exc).__name__)

    trace = TraceFake()
    canal: CanalFake | CanalQueFalha = CanalFake()

    with monkeypatch.context() as m:
        if ponto == "fabrica":
            def fabrica():
                raise exc

            integracao = IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0)
        elif ponto == "trava":
            def trava_que_falha(peca):
                raise exc

            m.setattr(_modulo_integracao, "trava_entrada", trava_que_falha)
            integracao, _ = integracao_com_fake()
        elif ponto == "coletar":
            coletor = ColetorQueFalha(exc)
            integracao = IntegracaoColeta(
                fabrica_coletor=lambda: coletor, relogio_monotonico=lambda: 0.0
            )
        else:  # observador: o canal levanta dentro de coletar
            coletor_emite = ColetorQueEmite()
            integracao = IntegracaoColeta(
                fabrica_coletor=lambda: coletor_emite, relogio_monotonico=lambda: 0.0
            )
            canal = CanalQueFalha(exc)

        desfecho = integracao.processar(
            ativacao,
            contexto=contexto_coleta(coleta_efetiva="habilitada", canal_avisos=canal),
            trace=trace,
        )

    assert desfecho.desfecho == "erro"
    assert desfecho.motivo == type(exc).__name__
    assert desfecho.relatorio is None
    assert desfecho.codigo_peca == ativacao.codigo
    assert desfecho.marca_peca == ativacao.marca

    if ponto == "observador":
        assert coletor_emite.chamadas == 1
        # Tentativas: o evento encaminhado e depois a mensagem de desfecho.
        mensagens = canal.tentativas
        assert len(mensagens) == 2
        assert mensagens[0].startswith(PREFIXO_AVISO)
    else:
        mensagens = canal.mensagens
        assert len(mensagens) == 1
    dados = json.loads(mensagens[-1][len(PREFIXO_AVISO):])
    assert dados == {"desfecho": "erro", "motivo": type(exc).__name__}

    assert len(trace.eventos) == 1
    evento = trace.eventos[0]
    assert (evento.fase, evento.nome) == ("tool", "coletar_paginas")
    assert evento.kwargs["status"] == "erro"
    assert evento.kwargs["saida"]["desfecho"] == "erro"
    assert evento.kwargs["saida"]["motivo"] == type(exc).__name__
    assert evento.kwargs["justificativa"] is None

    assert marcador not in _texto_publicado(mensagens, trace.eventos)


# Feature: html-extract-on-web-search, Property 6: Falhas da coleta viram `erro` sem propagar
@_SETTINGS_PBT
@given(
    ativacao=st_ativacao_coerente(),
    coleta=st_coleta_efetiva,
    sinal=st_sinal,
    classe=st_classe_excecao,
    ponto=st.sampled_from(PONTOS_PUBLICACAO),
    marcador=st_marcador,
)
def test_property_6_falha_na_publicacao_preserva_desfecho(
    ativacao, coleta, sinal, classe, ponto, marcador
):
    """Falha só na publicação (canal da mensagem de desfecho ou
    ``trace.registrar``): ``processar`` não levanta, devolve o desfecho correto
    (oráculo do Req 4.5) e a outra via de publicação continua funcionando, sem
    a mensagem da exceção.

    **Validates: Requirements 5.1**
    """
    assume(marcador not in repr(ativacao))
    exc = _instanciar_excecao(classe, marcador)
    assert marcador in str(exc)
    assume(marcador not in type(exc).__name__)

    cancelado = sinal == "ativo"
    cancel_event = None if sinal == "ausente" else SinalFake(ativo=cancelado)
    esperado, motivo_esperado = _oraculo_precedencia(ativacao, coleta, cancelado)

    if ponto == "canal_publicacao":
        canal: CanalFake | CanalQueFalha = CanalQueFalha(exc)
        trace: TraceFake | TraceQueFalha = TraceFake()
    else:
        canal = CanalFake()
        trace = TraceQueFalha(exc)

    integracao, fabrica = integracao_com_fake()
    desfecho = integracao.processar(
        ativacao,
        contexto=contexto_coleta(
            coleta_efetiva=coleta, cancel_event=cancel_event, canal_avisos=canal
        ),
        trace=trace,
    )

    assert (desfecho.desfecho, desfecho.motivo) == (esperado, motivo_esperado)
    assert len(fabrica.coletor.chamadas) == (1 if esperado == "executada" else 0)

    mensagens = canal.tentativas if isinstance(canal, CanalQueFalha) else canal.mensagens
    eventos = trace.tentativas if isinstance(trace, TraceQueFalha) else trace.eventos

    # Cada via foi tentada exatamente como sem falhas; uma não bloqueia a outra.
    assert len(mensagens) == (0 if esperado == "executada" else 1)
    if mensagens:
        dados = json.loads(mensagens[0][len(PREFIXO_AVISO):])
        assert (dados["desfecho"], dados["motivo"]) == (esperado, motivo_esperado)
    assert len(eventos) == 1
    assert eventos[0].nome == "coletar_paginas"
    assert eventos[0].kwargs["saida"]["desfecho"] == esperado
    assert eventos[0].kwargs["saida"]["motivo"] == motivo_esperado

    assert marcador not in _texto_publicado(mensagens, eventos)



# ===========================================================================
# Property 10: Encaminhamento isolado dos eventos
# ===========================================================================

import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from coleta_paginas.coletor import (  # noqa: E402
    ATRIBUTO_EVENTO,
    ConfigColeta,
    evento_log,
    evento_resumo,
)
from coleta_paginas.integracao import mensagem_desfecho, mensagem_evento  # noqa: E402

_NOME_LOGGER_COLETA = "coleta_paginas"
_INSTANTE_P10 = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

# Roteiro por requisição do transporte fake (Req 8.1: relatórios variados).
st_resposta_p10 = st.sampled_from(("html", "html", "404", "rede", "timeout"))


@dataclass
class TransporteRoteiro:
    """Transporte_HTTP fake: a i-ésima chamada segue ``roteiro[i]`` (depois do
    fim, HTML 200). ``falha_em`` levanta ``RuntimeError`` (exceção genérica, que
    atravessa o buscador e o coletor) na chamada de índice indicado."""

    roteiro: tuple[str, ...] = ()
    falha_em: int | None = None
    pausa_s: float = 0.0
    chamadas: list[str] = field(default_factory=list)

    def __call__(self, url, cabecalhos, timeout):
        indice = len(self.chamadas)
        self.chamadas.append(url)
        if self.pausa_s:
            time.sleep(self.pausa_s)
        if self.falha_em is not None and indice == self.falha_em:
            raise RuntimeError("<<falha-transporte-p10>>")
        tipo = self.roteiro[indice] if indice < len(self.roteiro) else "html"
        if tipo in ("rede", "timeout"):
            raise ErroTransporte(tipo)
        if tipo == "404":
            return RespostaHTTP(status=404, cabecalhos={"content-type": "text/html"}, blocos=[b""])
        return RespostaHTTP(
            status=200,
            cabecalhos={"content-type": "text/html; charset=utf-8"},
            blocos=[f"<html><body>pagina {indice}</body></html>".encode()],
        )


def _integracao_real(caminho: Path, transporte: TransporteRoteiro) -> IntegracaoColeta:
    """``IntegracaoColeta`` com o ``ColetorPaginas`` real sobre um armazém em
    ``caminho``, configuração explícita (independente do ``.env``) e relógio fixo."""

    def fabrica() -> ColetorPaginas:
        return ColetorPaginas(
            ArmazemPaginas(caminho),
            transporte=transporte,
            config=ConfigColeta(timeout_s=1.0, teto_aceitos_ambiente=None, janela_reuso_dias=30),
            relogio=lambda: _INSTANTE_P10,
        )

    return IntegracaoColeta(fabrica_coletor=fabrica, relogio_monotonico=lambda: 0.0)


@st.composite
def st_ativacao_p10(
    draw: st.DrawFn,
    *,
    codigo: st.SearchStrategy[str] = st_codigo_valido,
    marca: st.SearchStrategy[str] = st_marca_com_referencia,
) -> AtivacaoPesquisa:
    """Ativação elegível com orgânicos que citam o código (buscáveis) misturados a
    orgânicos arbitrários (inclusive URLs inválidas → ``url_invalida``)."""
    codigo_escolhido = draw(codigo)
    marca_escolhida = draw(marca)
    com_codigo = list(
        _organicos_com_codigo(codigo_escolhido, marca_escolhida, draw(st.integers(0, 5)))
    )
    arbitrarios = draw(st.lists(st_resultado_organico(), max_size=4))
    organicos = draw(st.permutations(com_codigo + arbitrarios))
    return AtivacaoPesquisa(
        codigo=codigo_escolhido,
        marca=marca_escolhida,
        nomes_conflitantes=draw(st_nomes_conflitantes),
        metodo="serper",
        situacao="realizada",
        resultados=tuple(organicos),
    )


def _estado_logger(logger: logging.Logger) -> tuple:
    return (
        logger.level,
        tuple(logger.handlers),
        tuple(handler.level for handler in logger.handlers),
        logger.propagate,
        logger.disabled,
        tuple(logger.filters),
    )


def _snapshot_logging() -> dict[str, tuple]:
    """Configuração do logger raiz, do ``coleta_paginas`` e de todos os demais."""
    estado = {
        "<root>": _estado_logger(logging.getLogger()),
        _NOME_LOGGER_COLETA: _estado_logger(logging.getLogger(_NOME_LOGGER_COLETA)),
    }
    for nome, logger in list(logging.root.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger):
            estado[nome] = _estado_logger(logger)
    return estado


def _assert_logging_inalterado(antes: dict[str, tuple], depois: dict[str, tuple]) -> None:
    # Loggers criados durante a chamada (import preguiçoso) não contam como
    # alteração; os que já existiam devem estar idênticos.
    for nome, estado in antes.items():
        assert depois.get(nome) == estado, nome


def _eventos_esperados(relatorio: RelatorioColeta) -> list[dict[str, Any]]:
    """Oráculo independente: uma ``coleta_paginas.entrada`` por entrada do
    relatório, na ordem, e o ``coleta_paginas.resumo`` no fim."""
    return [evento_log(entrada) for entrada in relatorio.entradas] + [evento_resumo(relatorio)]


def _registros_coleta(caplog, thread_id: int | None = None) -> list[logging.LogRecord]:
    return [
        registro
        for registro in caplog.records
        if registro.name == _NOME_LOGGER_COLETA
        and (thread_id is None or registro.thread == thread_id)
    ]


def _assert_mensagens_iguais_aos_registros(
    mensagens: list[str], registros: list[logging.LogRecord]
) -> None:
    """Cada mensagem é ``mensagem_evento`` do mesmo evento do ``LogRecord``, 1:1 e
    na ordem; o JSON após o prefixo é o texto que o logger recebeu."""
    assert len(mensagens) == len(registros)
    for mensagem, registro in zip(mensagens, registros, strict=True):
        assert mensagem == mensagem_evento(getattr(registro, ATRIBUTO_EVENTO))
        assert mensagem == f"{PREFIXO_AVISO} {registro.getMessage()}"


# Feature: html-extract-on-web-search, Property 10: Encaminhamento isolado dos eventos
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    ativacao=st_ativacao_p10(),
    roteiro=st.lists(st_resposta_p10, max_size=8).map(tuple),
    falha_em=st.none() | st.integers(min_value=0, max_value=4),
    info_habilitado=st.booleans(),
)
def test_property_10_encaminhamento_isolado_uma_thread(
    caplog, ativacao, roteiro, falha_em, info_habilitado
):
    """As mensagens com o prefixo de uma ativação são exatamente
    ``mensagem_evento(e)`` de cada evento emitido pelo coletor naquela chamada,
    na ordem, e coincidem com os ``LogRecord`` do logger ``coleta_paginas``
    quando o INFO está habilitado. Nada chega ao canal depois do fim da chamada
    (com sucesso ou exceção). A configuração de logging não muda.

    **Validates: Requirements 8.1, 8.2, 8.5**
    """
    # Com exceção no transporte, o oráculo são os LogRecord: força INFO.
    info_habilitado = info_habilitado or falha_em is not None
    caplog.clear()

    with tempfile.TemporaryDirectory() as diretorio:
        caminho = Path(diretorio) / "paginas.db"
        transporte = TransporteRoteiro(roteiro=roteiro, falha_em=falha_em)
        integracao = _integracao_real(caminho, transporte)
        canal_1 = CanalFake()
        canal_2 = CanalFake()

        nivel = logging.INFO if info_habilitado else logging.WARNING
        with caplog.at_level(nivel, logger=_NOME_LOGGER_COLETA):
            caplog.clear()
            logging_antes = _snapshot_logging()

            desfecho_1 = integracao.processar(
                ativacao, contexto=contexto_coleta(canal_avisos=canal_1), trace=None
            )
            logging_meio = _snapshot_logging()
            registros_1 = _registros_coleta(caplog)
            mensagens_1 = list(canal_1.mensagens)

            # Segunda ativação da mesma peça, mesma instância (coletor reaproveitado,
            # reuso dentro da janela): canal_1 não recebe mais nada.
            caplog.clear()
            desfecho_2 = integracao.processar(
                ativacao, contexto=contexto_coleta(canal_avisos=canal_2), trace=None
            )
            registros_2 = _registros_coleta(caplog)
            logging_depois = _snapshot_logging()

        _assert_logging_inalterado(logging_antes, logging_meio)
        _assert_logging_inalterado(logging_antes, logging_depois)

        # Nenhum evento chega ao canal da 1ª chamada depois do fim dela.
        assert canal_1.mensagens == mensagens_1
        assert all(m.startswith(PREFIXO_AVISO) for m in canal_1.mensagens + canal_2.mensagens)

        for desfecho, mensagens, registros in (
            (desfecho_1, mensagens_1, registros_1),
            (desfecho_2, canal_2.mensagens, registros_2),
        ):
            if desfecho.desfecho == "executada":
                esperados = _eventos_esperados(desfecho.relatorio)
                assert mensagens == [mensagem_evento(e) for e in esperados]
                if info_habilitado:
                    _assert_mensagens_iguais_aos_registros(mensagens, registros)
                else:
                    assert registros == []
            else:
                # Exceção dentro de ``coletar``: os eventos emitidos até a falha,
                # depois só a mensagem única de desfecho ``erro``.
                assert desfecho.desfecho == "erro"
                assert desfecho.motivo == "RuntimeError"
                assert info_habilitado
                _assert_mensagens_iguais_aos_registros(mensagens[:-1], registros)
                assert mensagens[-1] == mensagem_desfecho(desfecho)
                assert "<<falha-transporte-p10>>" not in "\n".join(mensagens)


_CODIGOS_A = ("CF1000", "JE-4699", "W 712/75")
_CODIGOS_B = ("HU711X", "OX123D", "PH5949")


# Feature: html-extract-on-web-search, Property 10: Encaminhamento isolado dos eventos
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    ativacao_a=st_ativacao_p10(
        codigo=st.sampled_from(_CODIGOS_A), marca=st.sampled_from(("MANN-FILTER", "Mahle"))
    ),
    ativacao_b=st_ativacao_p10(
        codigo=st.sampled_from(_CODIGOS_B), marca=st.sampled_from(("Bosch", "Tecfil"))
    ),
    roteiro_a=st.lists(st_resposta_p10, max_size=6).map(tuple),
    roteiro_b=st.lists(st_resposta_p10, max_size=6).map(tuple),
)
def test_property_10_encaminhamento_isolado_duas_threads(
    caplog, ativacao_a, ativacao_b, roteiro_a, roteiro_b
):
    """Duas ``IntegracaoColeta`` (cada uma com seu armazém e seu canal) em threads
    concorrentes: cada canal recebe só os eventos da própria chamada, na ordem,
    iguais aos ``LogRecord`` emitidos na própria thread. Sem cruzamento, e a
    configuração de logging não muda.

    **Validates: Requirements 8.1, 8.2, 8.5**
    """
    caplog.clear()

    with tempfile.TemporaryDirectory() as dir_a, tempfile.TemporaryDirectory() as dir_b:
        casos = {
            "a": (
                ativacao_a,
                _integracao_real(
                    Path(dir_a) / "paginas.db",
                    TransporteRoteiro(roteiro=roteiro_a, pausa_s=0.001),
                ),
                CanalFake(),
            ),
            "b": (
                ativacao_b,
                _integracao_real(
                    Path(dir_b) / "paginas.db",
                    TransporteRoteiro(roteiro=roteiro_b, pausa_s=0.001),
                ),
                CanalFake(),
            ),
        }
        barreira = threading.Barrier(len(casos))
        desfechos: dict[str, Any] = {}
        threads_ids: dict[str, int] = {}
        erros: list[BaseException] = []

        def trabalhador(chave: str) -> None:
            ativacao, integracao, canal = casos[chave]
            try:
                threads_ids[chave] = threading.get_ident()
                barreira.wait(timeout=10)
                desfechos[chave] = integracao.processar(
                    ativacao, contexto=contexto_coleta(canal_avisos=canal), trace=None
                )
            except BaseException as exc:  # noqa: BLE001 — relatado na thread principal
                erros.append(exc)

        with caplog.at_level(logging.INFO, logger=_NOME_LOGGER_COLETA):
            caplog.clear()
            logging_antes = _snapshot_logging()
            threads = [threading.Thread(target=trabalhador, args=(k,)) for k in casos]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            assert not any(thread.is_alive() for thread in threads)
            logging_depois = _snapshot_logging()
            registros = list(caplog.records)

        assert erros == []
        _assert_logging_inalterado(logging_antes, logging_depois)

        for chave, (ativacao, _integracao, canal) in casos.items():
            desfecho = desfechos[chave]
            assert desfecho.desfecho == "executada"

            # Só os eventos da própria chamada, na ordem de emissão.
            esperados = _eventos_esperados(desfecho.relatorio)
            assert canal.mensagens == [mensagem_evento(e) for e in esperados]
            for mensagem in canal.mensagens:
                dados = json.loads(mensagem[len(PREFIXO_AVISO):])
                assert dados["codigo_peca"] == ativacao.codigo
                assert dados["marca_peca"] == ativacao.marca

            # Iguais aos LogRecord emitidos na própria thread.
            da_thread = [
                r
                for r in registros
                if r.name == _NOME_LOGGER_COLETA and r.thread == threads_ids[chave]
            ]
            _assert_mensagens_iguais_aos_registros(canal.mensagens, da_thread)

        # Todos os LogRecord do logger pertencem a uma das duas chamadas.
        total = sum(len(canal.mensagens) for _a, _i, canal in casos.values())
        assert len([r for r in registros if r.name == _NOME_LOGGER_COLETA]) == total




# ===========================================================================
# Property 11: Forma do evento de trace
# ===========================================================================

from collections import Counter  # noqa: E402

from hypothesis import event  # noqa: E402

from coleta_paginas.modelos import Confianca, EntradaRelatorio  # noqa: E402
from loop.models import EventoExecucao  # noqa: E402
from loop.serializacao import evento_para_json  # noqa: E402
from loop.tracing import TraceCollector  # noqa: E402

_DESFECHOS_ENTRADA_P11 = ("armazenado", "reaproveitado", "falha", "url_invalida")
_MOTIVOS_ENTRADA_P11 = (
    None,
    "rede",
    "timeout",
    "status_http",
    "nao_html",
    "erro_armazenamento",
    "cancelado",
    "url_invalida",
    "destino_nao_permitido",
)
_CAMPOS_ENTRADA_TRACE = {"codigo_peca", "marca_peca", "metodo", "quantidade_resultados"}
_CAMPOS_ITEM_TRACE = (
    "url", "dominio", "confianca", "desfecho", "motivo", "status_http", "hash_conteudo",
)

st_hash = st.none() | st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


@st.composite
def st_entrada_relatorio(draw: st.DrawFn, peca: PecaConsultada) -> EntradaRelatorio:
    """Entrada de relatório arbitrária (inclusive motivo ``cancelado``)."""
    return EntradaRelatorio(
        codigo_peca=peca.codigo_peca,
        marca_peca=peca.marca_peca,
        url=draw(st_link),
        dominio=draw(st.sampled_from(("exemplo.com.br", "loja.exemplo.com", "")) | st_texto_livre),
        confianca=draw(st.sampled_from((Confianca.ALTA, Confianca.MEDIA))),
        desfecho=draw(st.sampled_from(_DESFECHOS_ENTRADA_P11)),
        motivo=draw(st.sampled_from(_MOTIVOS_ENTRADA_P11)),
        status_http=draw(st.none() | st.integers(min_value=100, max_value=599)),
        hash_conteudo=draw(st_hash),
    )


@dataclass
class ColetorRoteirizado:
    """Coletor fake que devolve um relatório ``executada`` com as entradas
    geradas por ``gerar_entradas(peca)`` (sem armazém, sem transporte)."""

    gerar_entradas: Any
    chamadas: int = 0

    def coletar(self, peca, resultados, *, teto_aceitos=None, cancelado=None, observador=None):
        self.chamadas += 1
        return RelatorioColeta(
            status="executada",
            motivo=None,
            codigo_peca=peca.codigo_peca,
            marca_peca=peca.marca_peca,
            entradas=tuple(self.gerar_entradas(peca)),
        )


def _relogio_de(valores: list[float]):
    """Relógio monotônico roteirizado (pode até regredir): cicla pelos valores."""
    estado = {"i": 0}

    def relogio() -> float:
        valor = valores[estado["i"] % len(valores)]
        estado["i"] += 1
        return valor

    return relogio


def _status_esperado_p11(desfecho: str, relatorio: RelatorioColeta | None) -> str:
    """Oráculo independente do Req 7.4."""
    if desfecho == "executada":
        assert relatorio is not None
        if any(entrada.motivo == "cancelado" for entrada in relatorio.entradas):
            return "cancelado"
        return "ok"
    return desfecho


def _assert_forma_evento_coleta(
    trace: TraceCollector,
    evento_verificacao: EventoExecucao,
    ativacao: AtivacaoPesquisa,
    desfecho,
    desfecho_esperado: str,
    motivo_esperado: str | None,
) -> None:
    eventos = trace.eventos()
    coleta = [e for e in eventos if e.nome == "coletar_paginas"]
    event(f"status={coleta[0].status if coleta else None}")
    # Exatamente um evento de coleta, depois do evento de verificação da ativação.
    assert len(coleta) == 1
    assert eventos == [evento_verificacao, coleta[0]]
    evento = coleta[0]

    relatorio = desfecho.relatorio if desfecho_esperado == "executada" else None
    assert (desfecho.desfecho, desfecho.motivo) == (desfecho_esperado, motivo_esperado)

    assert evento.fase == "tool"
    assert evento.nome == "coletar_paginas"
    assert evento.status == _status_esperado_p11(desfecho_esperado, relatorio)
    assert evento.justificativa is None
    assert evento.duracao_ms is not None and evento.duracao_ms >= 0
    assert evento.duracao_ms == desfecho.duracao_ms

    # Entrada: exatamente os quatro campos.
    assert set(evento.entrada) == _CAMPOS_ENTRADA_TRACE
    assert evento.entrada["codigo_peca"] == ativacao.codigo
    assert evento.entrada["marca_peca"] == ativacao.marca
    assert evento.entrada["metodo"] == ativacao.metodo
    assert evento.entrada["quantidade_resultados"] == len(ativacao.resultados or ())

    # Saída.
    saida = evento.saida
    chaves = {"desfecho", "motivo", "duracao_ms", "contagem"}
    if desfecho_esperado == "executada":
        chaves.add("entradas")
    if motivo_esperado == "configuracao_invalida":
        chaves.add("variavel")
    assert set(saida) == chaves
    assert saida["desfecho"] == desfecho_esperado
    assert saida["motivo"] == motivo_esperado
    assert saida["duracao_ms"] == evento.duracao_ms
    if motivo_esperado == "configuracao_invalida":
        assert saida["variavel"] == "ESTAGIARIO_COLETA_HABILITADA"

    contagem_esperada = {nome: 0 for nome in _DESFECHOS_ENTRADA_P11}
    if relatorio is not None:
        contagem_esperada.update(Counter(e.desfecho for e in relatorio.entradas))
        itens_esperados = [
            {
                "url": e.url,
                "dominio": e.dominio,
                "confianca": Confianca(e.confianca).value,
                "desfecho": e.desfecho,
                "motivo": e.motivo,
                "status_http": e.status_http,
                "hash_conteudo": e.hash_conteudo,
            }
            for e in relatorio.entradas
        ]
        assert saida["entradas"] == itens_esperados
        assert all(tuple(item) == _CAMPOS_ITEM_TRACE for item in saida["entradas"])
    assert saida["contagem"] == contagem_esperada

    # Serializa pelo caminho da Auditoria_Loop sem erro e sem justificativa.
    serializado = evento_para_json(evento)
    json.dumps(serializado, ensure_ascii=False)
    assert serializado["tool"] == "coletar_paginas"
    assert serializado["fase"] == "tool"
    assert serializado["status"] == evento.status
    assert serializado["justificativa"] is None
    assert serializado["entrada"] == evento.entrada
    assert serializado["resultado"] == saida


def _registrar_verificacao(trace: TraceCollector, ativacao: AtivacaoPesquisa) -> EventoExecucao:
    """Evento ``verificar_nomenclatura_peca`` que ``_arbitrar_nome_via_web``
    registra antes de chamar a integração (inclusive o ``desligada``)."""
    return trace.registrar(
        "tool",
        "verificar_nomenclatura_peca",
        status="desligada" if ativacao.situacao == "desligada" else "ok",
        entrada={"codigo": ativacao.codigo, "marca": ativacao.marca},
        saida={},
    )


# Feature: html-extract-on-web-search, Property 11: Forma do evento de trace
@_SETTINGS_PBT
@given(
    ativacao=st_ativacao_coerente() | st_ativacao_elegivel() | st_ativacao_trava(),
    coleta=st_coleta_p5,
    sinal=st.sampled_from(("ausente", "inativo", "inativo", "ativo")),
    relogio=st.lists(
        st.floats(min_value=0, max_value=1e6, allow_nan=False), min_size=1, max_size=4
    ),
    dados=st.data(),
)
def test_property_11_forma_do_evento_de_trace(ativacao, coleta, sinal, relogio, dados):
    """Com ``TraceCollector`` real e coletor roteirizado (entradas geradas,
    inclusive canceladas): um evento ``("tool", "coletar_paginas")`` por
    ativação, depois do de verificação, com status do Req 7.4, entrada com os
    quatro campos, saída com desfecho, motivo, duração ≥ 0, contagens (zeradas
    fora de ``executada``), ``entradas`` só em ``executada``, ``variavel`` só em
    ``configuracao_invalida`` e sem justificativa; serializável pelo loop.

    **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
    """
    cancelado = sinal == "ativo"
    cancel_event = None if sinal == "ausente" else SinalFake(ativo=cancelado)

    def gerar_entradas(peca: PecaConsultada) -> list[EntradaRelatorio]:
        return dados.draw(st.lists(st_entrada_relatorio(peca), max_size=12))

    coletor = ColetorRoteirizado(gerar_entradas)
    integracao = IntegracaoColeta(
        fabrica_coletor=lambda: coletor, relogio_monotonico=_relogio_de(relogio)
    )
    trace = TraceCollector()
    evento_verificacao = _registrar_verificacao(trace, ativacao)

    desfecho = integracao.processar(
        ativacao,
        contexto=contexto_coleta(coleta_efetiva=coleta, cancel_event=cancel_event),
        trace=trace,
    )

    esperado, motivo_esperado = _oraculo_precedencia(ativacao, coleta, cancelado)
    assert coletor.chamadas == (1 if esperado == "executada" else 0)
    _assert_forma_evento_coleta(
        trace, evento_verificacao, ativacao, desfecho, esperado, motivo_esperado
    )


@dataclass
class SinalApos:
    """Sinal_Cancelamento que fica ativo a partir da chamada ``n`` de ``is_set``
    (a primeira é a checagem da integração; as seguintes, as do coletor)."""

    n: int
    chamadas: int = 0

    def is_set(self) -> bool:
        ativo = self.chamadas >= self.n
        self.chamadas += 1
        return ativo


# Feature: html-extract-on-web-search, Property 11: Forma do evento de trace
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    ativacao=st_ativacao_p10(),
    roteiro=st.lists(st_resposta_p10, max_size=8).map(tuple),
    ativa_apos=st.none() | st.integers(min_value=0, max_value=6),
)
def test_property_11_forma_do_evento_com_coletor_real(ativacao, roteiro, ativa_apos):
    """Mesmo invariante com o ``ColetorPaginas`` real e um sinal que ativa
    durante a coleta: fontes restantes viram ``falha/cancelado`` e o status do
    evento passa a ``cancelado``; contagens e entradas espelham o relatório.

    **Validates: Requirements 7.1, 7.2, 7.3, 7.4**
    """
    with tempfile.TemporaryDirectory() as diretorio:
        transporte = TransporteRoteiro(roteiro=roteiro)
        integracao = _integracao_real(Path(diretorio) / "paginas.db", transporte)
        sinal = None if ativa_apos is None else SinalApos(ativa_apos)
        trace = TraceCollector()
        evento_verificacao = _registrar_verificacao(trace, ativacao)

        desfecho = integracao.processar(
            ativacao, contexto=contexto_coleta(cancel_event=sinal), trace=trace
        )

    if ativa_apos == 0:
        esperado, motivo_esperado = "nao_executada", "cancelada"
    else:
        esperado, motivo_esperado = "executada", None
    _assert_forma_evento_coleta(
        trace, evento_verificacao, ativacao, desfecho, esperado, motivo_esperado
    )

    if esperado == "executada":
        canceladas = [e for e in desfecho.relatorio.entradas if e.motivo == "cancelado"]
        # Com o sinal ativo desde a 1ª fonte, nenhuma fonte selecionada vai à rede.
        if ativa_apos == 1:
            assert transporte.chamadas == []
        for entrada in canceladas:
            assert entrada.desfecho == "falha"
            assert entrada.status_http is None
            assert entrada.hash_conteudo is None
        if canceladas:
            assert trace.eventos()[-1].status == "cancelado"



# ===========================================================================
# Property 12: Só campos permitidos saem da coleta
# ===========================================================================
# Feature: html-extract-on-web-search, Property 12: Só campos permitidos saem da coleta

from hypothesis import example  # noqa: E402

from coleta_paginas.integracao import NOME_EVENTO_TRACE as NOME_EVENTO_TRACE_P12  # noqa: E402
from db.rule_store import RuleStore  # noqa: E402
from pipeline import executar_caso  # noqa: E402
from tests.test_pipeline_coleta_pbt import (  # noqa: E402
    DEPENDENCIAS_FK,
    URLS,
    CenarioGrupo,
    EspecMemoria,
    LLMRoteirizado,
    PedirIntervencaoFake,
    VerificadorComProtocolo,
    buscar_grupo_fake,
    cenarios_grupo,
    especs_memoria,
    fabrica_coletor_real,
    fazer_registro,
    resultados_verificacao,
    semear_regra,
)
from verification.models import ResultadoVerificacao  # noqa: E402

# Chaves permitidas (Campos_Permitidos) no evento de trace e nos JSON do canal.
_CHAVES_SAIDA_P12 = {"desfecho", "motivo", "duracao_ms", "contagem", "entradas", "variavel"}
_CHAVES_MENSAGEM_P12 = {
    "evento", "codigo_peca", "marca_peca", "url", "dominio", "confianca", "desfecho",
    "motivo", "status_http", "hash_conteudo", "status", "contagem", "variavel",
}
_MODOS_TRANSPORTE_P12 = (
    "ok", "ok", "status_erro", "nao_html", "misto", "excecao_transporte", "excecao_fabrica",
)
_CICLO_MISTO_P12 = ("ok", "status_erro", "nao_html", "excecao_transporte")


@dataclass(frozen=True)
class MarcadoresP12:
    """Marcadores únicos por exemplo; nenhum deles pode sair da coleta."""

    sufixo: str

    @property
    def corpo(self) -> str:
        return f"<<CORPO-{self.sufixo}>>"

    @property
    def cabecalho(self) -> str:
        return f"<<CAB-{self.sufixo}>>"

    @property
    def excecao(self) -> str:
        return f"<<EXC-{self.sufixo}>>"

    def titulo(self, i: int) -> str:
        return f"<<TIT-{self.sufixo}-{i}>>"

    def snippet(self, i: int) -> str:
        return f"<<SNP-{self.sufixo}-{i}>>"

    def prefixos(self) -> tuple[str, ...]:
        """Prefixos comuns a todos os marcadores deste exemplo."""
        return tuple(
            f"<<{tipo}-{self.sufixo}" for tipo in ("TIT", "SNP", "CORPO", "CAB", "EXC")
        )


class FalhaMarcadaP12(RuntimeError):
    """Exceção fora do contrato do transporte, com marcador na mensagem."""


@dataclass
class TransporteMarcado:
    """Transporte_HTTP fake cujos corpos, cabeçalhos e exceções levam marcadores.

    - ``ok``: 200 HTML com o código e o marcador de corpo; cabeçalhos extras marcados;
    - ``status_erro``: 500 com corpo e cabeçalhos marcados;
    - ``nao_html``: 200 com Content-Type não HTML marcado;
    - ``excecao_transporte``: levanta ``FalhaMarcadaP12`` com o marcador;
    - ``misto``: cicla pelos anteriores por requisição.
    """

    modo: str
    marcadores: MarcadoresP12
    codigo: str
    chamadas: list[str] = field(default_factory=list)

    def __call__(self, url, cabecalhos, timeout):
        indice = len(self.chamadas)
        self.chamadas.append(url)
        modo = _CICLO_MISTO_P12[indice % 4] if self.modo == "misto" else self.modo
        m = self.marcadores
        extras = {"x-marcador": m.cabecalho, "set-cookie": f"s={m.cabecalho}", "server": m.cabecalho}
        corpo = f"<html><head><title>{m.corpo}</title></head><body>{self.codigo} {m.corpo}</body></html>"
        if modo == "excecao_transporte":
            raise FalhaMarcadaP12(m.excecao)
        if modo == "status_erro":
            return RespostaHTTP(
                status=500,
                cabecalhos={"content-type": "text/html; charset=utf-8", **extras},
                blocos=[corpo.encode()],
            )
        if modo == "nao_html":
            return RespostaHTTP(
                status=200,
                cabecalhos={"content-type": f"application/x-{m.cabecalho}", **extras},
                blocos=[corpo.encode()],
            )
        return RespostaHTTP(
            status=200,
            cabecalhos={"content-type": "text/html; charset=utf-8", **extras},
            blocos=[corpo.encode()],
        )


def _organicos_marcados(
    codigo: str, marcadores: MarcadoresP12, links: list[str], posicoes: list[int | None]
) -> tuple[ResultadoOrganico, ...]:
    """Orgânicos com o código (para a seleção aceitar) e marcadores em título e
    snippet; URLs sem marcador (URL é Campo_Permitido)."""
    return tuple(
        ResultadoOrganico(
            title=f"{codigo} FILTRO {marcadores.titulo(i)}",
            link=link,
            snippet=f"ref {codigo} {marcadores.snippet(i)}",
            position=posicao,
        )
        for i, (link, posicao) in enumerate(zip(links, posicoes, strict=True))
    )


@dataclass
class ExecucaoP12:
    resultado: Any
    erro: str | None
    avisos: list[str]
    eventos: list[EventoExecucao]
    pedidos: list
    chamadas_llm: list[tuple[str, str]]
    chamadas_transporte: list[str]


def _executar_p12(
    cenario: CenarioGrupo,
    resultado_verificacao: ResultadoVerificacao,
    organicos: tuple[ResultadoOrganico, ...],
    memoria: EspecMemoria,
    modo: str,
    marcadores: MarcadoresP12,
    base: Path,
) -> ExecucaoP12:
    """``pipeline.executar_caso`` com verificador fake (protocolo), coletor real
    sobre ``ArmazemPaginas`` em diretório temporário e transporte marcado."""
    diretorio = Path(tempfile.mkdtemp(dir=base))
    transporte = TransporteMarcado(modo, marcadores, cenario.search_ref)
    if modo == "excecao_fabrica":
        def fabrica():
            raise FalhaMarcadaP12(marcadores.excecao)
    else:
        fabrica = fabrica_coletor_real(diretorio, transporte)
    integracao = IntegracaoColeta(fabrica_coletor=fabrica)

    store: RuleStore | None = None
    if memoria.usar_rule_store:
        store = RuleStore(diretorio / "memoria.db")
        if memoria.regra_previa:
            semear_regra(store, cenario)
    pedir = PedirIntervencaoFake(memoria.pedir == "com_valor") if memoria.pedir else None
    llm = LLMRoteirizado(cenario)
    trace = TraceCollector()
    avisos: list[str] = []

    resultado = None
    erro: str | None = None
    try:
        resultado = executar_caso(
            cenario.search_ref,
            cenario.brand_id,
            llm=llm,
            dependencias_fk=DEPENDENCIAS_FK,
            rule_store=store,
            buscar_grupo=buscar_grupo_fake(cenario),
            on_aviso=avisos.append,
            verificar_web=VerificadorComProtocolo(resultado_verificacao, organicos),
            pedir_intervencao=pedir,
            trace=trace,
            pesquisa_web=True,
            coleta_html=True,
            integracao_coleta=integracao,
        )
    except Exception as exc:  # noqa: BLE001 — a mensagem também é verificada
        erro = f"{type(exc).__name__}: {exc}"

    return ExecucaoP12(
        resultado=resultado,
        erro=erro,
        avisos=avisos,
        eventos=trace.eventos(),
        pedidos=list(pedir.pedidos) if pedir else [],
        chamadas_llm=list(llm.chamadas),
        chamadas_transporte=list(transporte.chamadas),
    )


@st.composite
def st_caso_p12(draw: st.DrawFn):
    """``(cenario, resultado_verificacao, organicos, memoria, modo, marcadores, garantir)``."""
    cenario = draw(cenarios_grupo())
    marcadores = MarcadoresP12(
        draw(st.text(alphabet="0123456789abcdef", min_size=8, max_size=12))
    )
    n = draw(st.integers(min_value=0, max_value=4))
    links = draw(st.lists(st.sampled_from(URLS), min_size=n, max_size=n))
    posicoes = draw(
        st.lists(st.none() | st.integers(min_value=1, max_value=10), min_size=n, max_size=n)
    )
    return (
        cenario,
        draw(resultados_verificacao(cenario)),
        _organicos_marcados(cenario.search_ref, marcadores, links, posicoes),
        draw(especs_memoria),
        draw(st.sampled_from(_MODOS_TRANSPORTE_P12)),
        marcadores,
        False,
    )


def _caso_fixo_p12(modo: str):
    """Caso com Ativacao_Pesquisa garantida (nomes divergentes em ``duplicata_real``,
    verificação inconclusiva) e duas fontes aceitáveis; ``garantir=True`` exige
    que a coleta tenha de fato chegado ao ponto do ``modo``."""
    registros = (
        fazer_registro(1, "FILTRO DE AR", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
        fazer_registro(2, "FILTRO CABINE", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
    )
    cenario = CenarioGrupo("CF1000", 1, "MANN-FILTER", registros, (("duplicata_real", (1, 2)),))
    marcadores = MarcadoresP12("f1x0d0e5")
    return (
        cenario,
        ResultadoVerificacao("inconclusivo", None, "sem confirmação clara", []),
        _organicos_marcados("CF1000", marcadores, [URLS[1], URLS[2]], [1, 2]),
        EspecMemoria(usar_rule_store=True, regra_previa=False, pedir="com_valor"),
        modo,
        marcadores,
        True,
    )


def _textos_observaveis_p12(execucao: ExecucaoP12) -> list[tuple[str, str]]:
    """``(origem, texto)`` de tudo o que a Property 12 inspeciona."""
    textos: list[tuple[str, str]] = [("aviso", m) for m in execucao.avisos]
    for evento in execucao.eventos:
        textos.append(
            (
                f"trace:{evento.nome}",
                repr((evento.detalhes, evento.entrada, evento.saida, evento.justificativa)),
            )
        )
    if execucao.resultado is not None:
        textos.append(("resultado_caso", repr(execucao.resultado)))
        textos.append(("sql", execucao.resultado.sql))
        for decisao in execucao.resultado.decisoes:
            for dc in getattr(decisao, "decisoes_campo", ()):
                textos.append(("decisao_campo", repr(dc)))
    if execucao.erro is not None:
        textos.append(("erro", execucao.erro))
    for pedido in execucao.pedidos:
        textos.append(("pedido_intervencao", repr(pedido)))
        textos.append(("pedido.motivo", pedido.motivo or ""))
        textos.append(("pedido.contexto_web", pedido.contexto_web or ""))
    textos.extend((f"prompt:{schema}", prompt) for schema, prompt in execucao.chamadas_llm)
    return textos


# Feature: html-extract-on-web-search, Property 12: Só campos permitidos saem da coleta
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(caso=st_caso_p12())
@example(caso=_caso_fixo_p12("ok"))
@example(caso=_caso_fixo_p12("status_erro"))
@example(caso=_caso_fixo_p12("nao_html"))
@example(caso=_caso_fixo_p12("misto"))
@example(caso=_caso_fixo_p12("excecao_transporte"))
@example(caso=_caso_fixo_p12("excecao_fabrica"))
def test_property_12_so_campos_permitidos_saem_da_coleta(caso, tmp_path, sem_banco):
    """Com marcadores únicos em títulos, snippets, corpos, cabeçalhos HTTP e
    mensagens de exceção, nenhum marcador aparece no Evento_Coleta_Trace, nas
    mensagens do Canal_Avisos, no ``ResultadoCaso`` (decisões, ``DecisaoCampo``,
    SQL), nos ``PedidoIntervencao`` nem nos prompts do LLM fake. As chaves do
    evento de trace e dos JSON ``Coleta de páginas:`` ficam restritas aos
    Campos_Permitidos, e o evento não tem justificativa.

    O verificador fake não repassa títulos nem snippets por ``on_evento`` nem
    pelo ``ResultadoVerificacao``, então a única via que recebe os marcadores é
    a coleta.

    **Validates: Requirements 7.5, 8.4, 12.1, 12.2**
    """
    cenario, resultado_verificacao, organicos, memoria, modo, marcadores, garantir = caso
    execucao = _executar_p12(
        cenario, resultado_verificacao, organicos, memoria, modo, marcadores, tmp_path
    )

    eventos_coleta = [e for e in execucao.eventos if e.nome == NOME_EVENTO_TRACE_P12]
    event(f"modo={modo}")
    event(f"desfechos_coleta={sorted({(e.saida or {}).get('desfecho') for e in eventos_coleta})}")
    event(f"requisicoes_http={'sim' if execucao.chamadas_transporte else 'nao'}")

    # 1. Nenhum marcador em nenhuma saída observável.
    vazamentos = [
        (origem, prefixo)
        for origem, texto in _textos_observaveis_p12(execucao)
        for prefixo in marcadores.prefixos()
        if prefixo in texto
    ]
    assert vazamentos == []

    # 2. Evento_Coleta_Trace restrito aos Campos_Permitidos, sem justificativa.
    for evento in eventos_coleta:
        assert evento.fase == "tool"
        assert evento.justificativa is None
        assert set(evento.entrada) == _CAMPOS_ENTRADA_TRACE
        assert set(evento.saida) <= _CHAVES_SAIDA_P12
        for item in evento.saida.get("entradas", ()):
            assert tuple(item) == _CAMPOS_ITEM_TRACE
        assert set(evento.saida["contagem"]) == set(_DESFECHOS_ENTRADA_P11)

    # 3. Mensagens do canal da coleta: JSON só com Campos_Permitidos.
    mensagens_coleta = [m for m in execucao.avisos if m.startswith(PREFIXO_AVISO)]
    for mensagem in mensagens_coleta:
        dados = json.loads(mensagem[len(PREFIXO_AVISO):])
        assert isinstance(dados, dict)
        assert set(dados) <= _CHAVES_MENSAGEM_P12, set(dados) - _CHAVES_MENSAGEM_P12
        if "contagem" in dados:
            assert set(dados["contagem"]) == set(_DESFECHOS_ENTRADA_P11)

    # Um evento de coleta por Ativacao_Pesquisa (cada uma tem o seu de verificação).
    verificacoes = [e for e in execucao.eventos if e.nome == "verificar_nomenclatura_peca"]
    assert len(eventos_coleta) <= len(verificacoes)

    # Não vacuidade no caso fixo: a coleta chegou ao ponto que carrega os marcadores.
    if garantir:
        assert execucao.erro is None, execucao.erro
        assert len(eventos_coleta) >= 1
        desfechos = {e.saida["desfecho"] for e in eventos_coleta}
        if modo == "excecao_fabrica":
            assert execucao.chamadas_transporte == []
            assert desfechos == {"erro"}
            assert {e.saida["motivo"] for e in eventos_coleta} == {"FalhaMarcadaP12"}
        else:
            assert execucao.chamadas_transporte
            if modo == "excecao_transporte":
                assert desfechos == {"erro"}
            else:
                assert "executada" in desfechos
            if modo == "ok":
                contagem = eventos_coleta[0].saida["contagem"]
                assert contagem["armazenado"] + contagem["reaproveitado"] >= 1
