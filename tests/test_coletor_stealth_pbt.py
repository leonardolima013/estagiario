"""Testes de propriedade do ``ColetorPaginas`` com o fallback stealth
(spec stealth-fallback-integration).

Bloco comum (reaproveitado pelas Properties 3–13 deste arquivo):

- ``armazem_espiao_temporario``: ``ArmazemEspiao`` (``tests/fakes_stealth.py``)
  num diretório temporário próprio por exemplo, com relógio fixo e o caminho do
  RuleStore no mesmo diretório;
- ``capturar_logs_coleta``: handler próprio no logger ``coleta_paginas`` por
  exemplo (a fixture ``caplog`` é de escopo de função e o Hypothesis a
  reaproveitaria entre exemplos);
- ``snapshot_tabelas``: todas as tabelas do arquivo SQLite, lidas por ``sqlite3``
  puro, sem citar nomes de tabelas;
- ``config_cenario`` e ``INSTANTE``: configuração e relógio fixos.

Nenhum teste abre rede nem navegador: o fallback e a trava são fakes.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import tempfile
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hypothesis import event, given, settings
from hypothesis import strategies as st

from coleta_paginas.coletor import (
    ATRIBUTO_EVENTO,
    CAMPOS_EVENTO_ENTRADA,
    CAMPOS_EVENTO_RESUMO,
    CAMPOS_STEALTH,
    EVENTO_ENTRADA,
    EVENTO_RESUMO,
    EVENTO_STEALTH_INICIO,
    ColetorPaginas,
    ConfigColeta,
)
from coleta_paginas.modelos import PecaConsultada
from db.armazem_paginas import ArmazemPaginas, ErroArmazenamento, TentativaFetch
from tests.fakes_stealth import (
    ALLOWLIST_PADRAO,
    CAMADA_STEALTH_ORACULO,
    CAMADA_URLLIB_ORACULO,
    CONTENT_TYPE_HTML,
    HOSTS,
    MOTIVOS_FALHA_BUSCA,
    STATUS_BLOQUEIO_ORACULO,
    STATUS_REUSO,
    EntradaEsperada,
    FallbackFake,
    FonteCenario,
    TentativaEsperada,
    TransporteRoteiro,
    TravaFake,
    UrllibCorpoVazio,
    UrllibErro,
    UrllibNaoHtml,
    UrllibPagina,
    UrllibStatus,
    CODIGO,
    ArmazemEspiao,
    CenarioStealth,
    cenarios_stealth,
    entrada_esperada_de,
    falhas_busca,
    oraculo_stealth,
    paginas_stealth,
    pares_reputacao,
    peca_cenario,
    preparar_execucao,
    resultados_do_cenario,
    semear_cenario,
    tentativas_novas,
)
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tools.buscador_paginas import FalhaBusca, PaginaBaixada
from tools.buscador_stealth import TRAVA_NAVEGADOR

# ---------------------------------------------------------------------------
# Bloco comum
# ---------------------------------------------------------------------------

INSTANTE = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
TIMEOUT_S = 5.0


def relogio_fixo() -> datetime:
    return INSTANTE


def config_cenario() -> ConfigColeta:
    return ConfigColeta(timeout_s=TIMEOUT_S, teto_aceitos_ambiente=None, janela_reuso_dias=30)


@contextmanager
def armazem_espiao_temporario() -> Iterator[ArmazemEspiao]:
    """``ArmazemEspiao`` novo num diretório temporário, removido ao sair."""
    with tempfile.TemporaryDirectory(prefix="coletor_stealth_pbt_") as diretorio:
        base = Path(diretorio)
        yield ArmazemEspiao(
            base / "paginas.db",
            caminho_rule_store=base / "rule_store.db",
            relogio=relogio_fixo,
        )


def snapshot_tabelas(db_path: Path) -> dict[str, list[tuple]]:
    """Linhas de todas as tabelas (inclusive ``sqlite_sequence``), ordenadas."""
    with closing(sqlite3.connect(db_path)) as conn:
        tabelas = [
            nome
            for (nome,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
        ]
        return {
            nome: conn.execute(f'SELECT * FROM "{nome}" ORDER BY 1').fetchall()
            for nome in tabelas
        }


class _HandlerColeta(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.registros: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.registros.append(record)

    @property
    def eventos(self) -> list[tuple[str, dict[str, Any]]]:
        """(mensagem JSON, dicionário do evento) de cada registro, em ordem."""
        return [
            (r.getMessage(), getattr(r, ATRIBUTO_EVENTO))
            for r in self.registros
            if hasattr(r, ATRIBUTO_EVENTO)
        ]


@contextmanager
def capturar_logs_coleta() -> Iterator[_HandlerColeta]:
    alvo = logging.getLogger("coleta_paginas")
    handler = _HandlerColeta()
    nivel_anterior = alvo.level
    alvo.addHandler(handler)
    alvo.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        alvo.removeHandler(handler)
        alvo.setLevel(nivel_anterior)


# Peça enriquecível do cenário e peça sem marca (exercita a Trava_Entrada).
pecas = st.sampled_from(
    [peca_cenario(), PecaConsultada(codigo_peca=CODIGO, marca_peca=None, nomes_candidatos=())]
)


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------


def _executar_sem_stealth(
    cenario: CenarioStealth,
    peca: PecaConsultada,
    teto: int,
    *,
    injetar: bool,
) -> dict[str, Any]:
    """Roda ``coletar`` num armazém novo e devolve tudo o que é comparado.

    ``injetar=False``: coletor só com os parâmetros anteriores à feature e
    ``coletar`` sem ``stealth``. ``injetar=True``: fallback, allowlist e trava
    fakes injetados e ``coletar(..., stealth=False)``.
    """
    observados: list[dict[str, Any]] = []
    execucao = preparar_execucao(cenario, proximo_observador=lambda e: observados.append(dict(e)))
    with armazem_espiao_temporario() as armazem:
        semear_cenario(armazem, cenario, INSTANTE)
        if injetar:
            coletor = ColetorPaginas(
                armazem,
                transporte=execucao.transporte,
                config=config_cenario(),
                relogio=relogio_fixo,
                fallback_stealth=execucao.fallback,
                allowlist_stealth=cenario.allowlist,
                trava_navegador=execucao.trava,
            )
        else:
            coletor = ColetorPaginas(
                armazem,
                transporte=execucao.transporte,
                config=config_cenario(),
                relogio=relogio_fixo,
            )
        kwargs: dict[str, Any] = {
            "teto_aceitos": teto,
            "cancelado": execucao.cancelamento.cancelado,
            "observador": execucao.cancelamento.observador,
        }
        if injetar:
            kwargs["stealth"] = False
        with capturar_logs_coleta() as logs:
            relatorio = coletor.coletar(peca, resultados_do_cenario(cenario), **kwargs)
        return {
            "relatorio": relatorio,
            "logger": logs.eventos,
            "observador": observados,
            "tabelas": snapshot_tabelas(armazem.db_path),
            "transporte": list(execucao.roteiro_transporte.chamadas),
            "chamadas_reputacao": armazem.chamadas_reputacao,
            "fallback": list(execucao.fallback.chamadas),
            "trava": execucao.trava.chamadas,
        }


# Feature: stealth-fallback-integration, Property 13: Caracterização com o fallback desligado
@settings(max_examples=100, deadline=None)
@given(
    cenario=cenarios_stealth(com_falhas_armazem=True),
    peca=pecas,
    teto=st.integers(min_value=1, max_value=5),
)
def test_property_13_caracterizacao_com_fallback_desligado(
    cenario: CenarioStealth, peca: PecaConsultada, teto: int
) -> None:
    """**Validates: Requirements 2.5, 6.5, 10.1, 10.2, 10.3**"""
    anterior = _executar_sem_stealth(cenario, peca, teto, injetar=False)
    desligado = _executar_sem_stealth(cenario, peca, teto, injetar=True)

    # Relatório, eventos, armazém e transporte idênticos.
    for chave in ("relatorio", "logger", "observador", "tabelas", "transporte"):
        assert desligado[chave] == anterior[chave], chave

    # Nenhuma dependência nova tocada, nenhuma reputação consultada ou registrada.
    for lado in (anterior, desligado):
        assert lado["chamadas_reputacao"] == 0
    assert desligado["fallback"] == []
    assert desligado["trava"] == 0

    # Relatório e eventos sem nenhum Campo_Stealth.
    relatorio = desligado["relatorio"]
    assert relatorio.stealth is False
    assert all(e.camada is None and e.motivo_urllib is None for e in relatorio.entradas)
    eventos_logger = [evento for _msg, evento in desligado["logger"]]
    assert eventos_logger == desligado["observador"]
    for evento in eventos_logger:
        assert evento["evento"] != EVENTO_STEALTH_INICIO
        assert not CAMPOS_STEALTH & evento.keys()
        assert "contagem_camada" not in evento
        if evento["evento"] == EVENTO_ENTRADA:
            assert evento.keys() == CAMPOS_EVENTO_ENTRADA
        else:
            assert evento["evento"] == EVENTO_RESUMO
            assert evento.keys() == CAMPOS_EVENTO_RESUMO
    assert eventos_logger and eventos_logger[-1]["evento"] == EVENTO_RESUMO



# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


def _host_na_allowlist(url: str, allowlist: frozenset[str]) -> bool:
    """Checagem direta (independente do coletor e do oráculo) da pertença do host."""
    host = url.split("://", 1)[1].split("/", 1)[0].casefold()
    return any(host == d.casefold() or host.endswith("." + d.casefold()) for d in allowlist)


def _executar_com_stealth(cenario: CenarioStealth, teto: int) -> dict[str, Any]:
    """Roda ``coletar(stealth=True)`` com fakes injetados e devolve o observado.

    ``relatorio`` é ``None`` e ``excecao`` traz a classe quando ``coletar`` propaga.
    """
    observados: list[dict[str, Any]] = []
    execucao = preparar_execucao(cenario, proximo_observador=lambda e: observados.append(dict(e)))
    with armazem_espiao_temporario() as armazem:
        id_base = semear_cenario(armazem, cenario, INSTANTE)
        coletor = ColetorPaginas(
            armazem,
            transporte=execucao.transporte,
            config=config_cenario(),
            relogio=relogio_fixo,
            fallback_stealth=execucao.fallback,
            allowlist_stealth=cenario.allowlist,
            trava_navegador=execucao.trava,
        )
        relatorio = None
        excecao: type[BaseException] | None = None
        try:
            relatorio = coletor.coletar(
                peca_cenario(),
                resultados_do_cenario(cenario),
                teto_aceitos=teto,
                cancelado=execucao.cancelamento.cancelado,
                observador=execucao.cancelamento.observador,
                stealth=True,
            )
        except Exception as exc:  # noqa: BLE001 - comparada com o oráculo
            excecao = type(exc)
        return {
            "relatorio": relatorio,
            "excecao": excecao,
            "observador": observados,
            "transporte": tuple(execucao.roteiro_transporte.chamadas),
            "fallback": list(execucao.fallback.chamadas),
            "trava": execucao.trava,
            "tentativas": tentativas_novas(armazem, cenario, id_base),
        }


@st.composite
def _casos_despacho(draw: st.DrawFn) -> tuple[CenarioStealth, int]:
    cenario = draw(
        st.one_of(
            cenarios_stealth(com_excecao=True),
            cenarios_stealth(com_excecao=True, com_falhas_armazem=True),
        )
    )
    if draw(st.booleans()):
        # Viés para o caminho do fallback: allowlist padrão e urllib bloqueado
        # (403/429/503) em parte das fontes, sem mexer em host/URL do cenário.
        fontes = tuple(
            replace(f, urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))))
            if draw(st.booleans())
            else f
            for f in cenario.fontes
        )
        cenario = replace(cenario, fontes=fontes, allowlist=ALLOWLIST_PADRAO)
    # Teto >= número de fontes: a seleção mantém todas, na ordem do cenário.
    teto = max(1, len(cenario.fontes)) + draw(st.integers(min_value=0, max_value=2))
    return cenario, teto


# Feature: stealth-fallback-integration, Property 3: Despacho do fallback igual ao oráculo
@settings(max_examples=100, deadline=None)
@given(caso=_casos_despacho())
def test_property_3_despacho_do_fallback_igual_ao_oraculo(
    caso: tuple[CenarioStealth, int],
) -> None:
    """**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.6, 2.7, 2.8, 3.7, 6.1, 6.3, 7.1, 7.2, 7.3, 7.5**"""
    cenario, teto = caso
    esperado = oraculo_stealth(cenario)
    obtido = _executar_com_stealth(cenario, teto)

    # Exceção do fallback atravessa coletar com exatamente a classe prevista.
    assert obtido["excecao"] is esperado.excecao

    # Chamadas ao transporte (nenhuma para urllib excluída) e ao fallback, em ordem.
    assert obtido["transporte"] == esperado.chamadas_transporte
    chamadas = obtido["fallback"]
    assert tuple(c.url for c in chamadas) == esperado.chamadas_fallback
    for chamada in chamadas:
        assert set(chamada.kwargs) == {"allowlist", "timeout"}
        assert chamada.allowlist == cenario.allowlist
        assert chamada.timeout == TIMEOUT_S
        assert _host_na_allowlist(chamada.url, cenario.allowlist)

    # Trava: um acquire por fonte que chegou ao fallback, com o prazo efetivo; livre ao final.
    trava = obtido["trava"]
    assert len(trava.acquires) == esperado.acquires
    assert all(timeout == TIMEOUT_S for _blocking, timeout in trava.acquires)
    assert trava.detida is False
    assert trava.releases == len(chamadas)

    # Evento stealth_inicio: um por chamada, com URL e motivo_urllib do oráculo.
    inicios = tuple(
        (e["url"], e["motivo_urllib"])
        for e in obtido["observador"]
        if e["evento"] == EVENTO_STEALTH_INICIO
    )
    assert inicios == esperado.stealth_inicio

    # Tentativa_Fetch novas, na ordem das buscas.
    assert obtido["tentativas"] == esperado.tentativas

    if esperado.excecao is not None:
        return

    relatorio = obtido["relatorio"]
    assert relatorio is not None and relatorio.stealth is True
    entradas = relatorio.entradas
    # Reaproveitamento mesmo com as duas camadas excluídas está nas entradas projetadas.
    assert tuple(entrada_esperada_de(e) for e in entradas) == esperado.entradas

    # Req 2.8: chamadas <= Teto_Aceitos e = elegíveis − canceladas/trava recusada antes da chamada.
    assert len(chamadas) <= teto
    elegiveis = [e for e in entradas if e.motivo_urllib is not None]
    descontadas = [
        e
        for e in elegiveis
        if e.camada != CAMADA_STEALTH_ORACULO
        and e.motivo in ("cancelado", "navegador_indisponivel")
    ]
    assert len(chamadas) == len(elegiveis) - len(descontadas)



# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

# Todos os domínios que um cenário pode produzir (hosts e variantes de
# ``ResultadoBusca.dominio`` vêm de ``HOSTS``): uma Tentativa_Fetch gravada num
# domínio errado (o host da URL no lugar do Dominio_Fonte, por exemplo) também
# aparece na leitura.
_DOMINIOS_POSSIVEIS = tuple(sorted({h.strip().casefold().rstrip(".") for h in HOSTS}))
_CAMADAS_P6 = (CAMADA_URLLIB_ORACULO, CAMADA_STEALTH_ORACULO)


def _pares_lidos(cenario: CenarioStealth) -> tuple[tuple[str, str], ...]:
    dominios = sorted(set(_DOMINIOS_POSSIVEIS) | {d for d, _c in pares_reputacao(cenario)})
    return tuple((d, c) for d in dominios for c in _CAMADAS_P6)


def _ler_tentativas(
    armazem: ArmazemPaginas, pares: tuple[tuple[str, str], ...]
) -> list[TentativaFetch]:
    """Todas as Tentativa_Fetch dos pares, lidas por ``ultimas_tentativas`` (API da
    base, sem passar pelos contadores do espião) e ordenadas por ``id``."""
    todas: list[TentativaFetch] = []
    for dominio, camada in pares:
        lidas = ArmazemPaginas.ultimas_tentativas(armazem, dominio, camada, 10**6)
        # Contrato da API: mais recentes primeiro, todas do par pedido.
        assert [t.id for t in lidas] == sorted((t.id for t in lidas), reverse=True)
        assert all(t.dominio == dominio and t.camada == camada for t in lidas)
        todas.extend(lidas)
    todas.sort(key=lambda t: t.id)
    return todas


def _tentativa_urllib(dominio: str, item: Any) -> TentativaEsperada | None:
    """Linha esperada para uma busca urllib com o desfecho abstrato ``item``
    (tabela de Tentativa_Fetch do design, escrita de novo aqui)."""
    camada = CAMADA_URLLIB_ORACULO
    if isinstance(item, UrllibPagina):
        return TentativaEsperada(dominio, camada, True, None, item.status)
    if isinstance(item, UrllibStatus):
        if item.status in (403, 429, 503):
            return TentativaEsperada(dominio, camada, False, "status_http", item.status)
        return None
    if isinstance(item, UrllibErro):
        return TentativaEsperada(dominio, camada, False, item.tipo, None)
    return None  # nao_html, corpo_vazio


def _tentativa_stealth(dominio: str, item: Any) -> TentativaEsperada | None:
    """Linha esperada para uma chamada ao fallback com o item do roteiro."""
    camada = CAMADA_STEALTH_ORACULO
    if isinstance(item, PaginaBaixada):
        return TentativaEsperada(dominio, camada, True, None, item.status_http)
    if isinstance(item, FalhaBusca):
        if item.motivo in ("rede", "timeout"):
            return TentativaEsperada(dominio, camada, False, item.motivo, item.status_http)
        if item.motivo == "status_http" and item.status_http in (403, 429, 503):
            return TentativaEsperada(dominio, camada, False, "status_http", item.status_http)
        return None  # ambiente, allowlist, URL, demais motivos
    return None  # exceção: a busca não terminou


def _executar_p6(cenario: CenarioStealth, teto: int) -> dict[str, Any]:
    """``coletar(stealth=True)`` com uma linha do tempo comum ao transporte e ao
    fallback; devolve as buscas em ordem e as Tentativa_Fetch antes e depois.

    Cada item de ``buscas`` é ``("urllib", url)`` (chamada ao transporte) ou
    ``("fallback", url)`` (chamada ao ``FallbackFake``), na ordem real."""
    linha_do_tempo: list[tuple[str, str]] = []
    execucao = preparar_execucao(cenario, linha_do_tempo=linha_do_tempo)
    transporte_base = execucao.transporte

    def transporte(url: str, cabecalhos: Any, timeout: float) -> Any:
        linha_do_tempo.append(("urllib", url))
        return transporte_base(url, cabecalhos, timeout)

    pares = _pares_lidos(cenario)
    with armazem_espiao_temporario() as armazem:
        id_base = semear_cenario(armazem, cenario, INSTANTE)
        antes = _ler_tentativas(armazem, pares)
        coletor = ColetorPaginas(
            armazem,
            transporte=transporte,
            config=config_cenario(),
            relogio=relogio_fixo,
            fallback_stealth=execucao.fallback,
            allowlist_stealth=cenario.allowlist,
            trava_navegador=execucao.trava,
        )
        excecao: type[BaseException] | None = None
        try:
            coletor.coletar(
                peca_cenario(),
                resultados_do_cenario(cenario),
                teto_aceitos=teto,
                cancelado=execucao.cancelamento.cancelado,
                observador=execucao.cancelamento.observador,
                stealth=True,
            )
        except Exception as exc:  # noqa: BLE001 - exceção do fallback, prevista pelo oráculo
            excecao = type(exc)
        depois = _ler_tentativas(armazem, pares)
        return {
            "id_base": id_base,
            "antes": antes,
            "depois": depois,
            "buscas": list(linha_do_tempo),
            "excecao": excecao,
            "pela_api": tentativas_novas(armazem, cenario, id_base),
        }


@st.composite
def _casos_tentativas(draw: st.DrawFn) -> tuple[CenarioStealth, int]:
    """Cenários sem falhas de armazém (cobertas pela Property 7), com exceções do
    fallback e viés para o caminho do fallback (allowlist padrão, 403/429/503)."""
    cenario = draw(cenarios_stealth(com_excecao=True))
    if draw(st.booleans()):
        fontes = tuple(
            replace(f, urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))))
            if draw(st.booleans())
            else f
            for f in cenario.fontes
        )
        cenario = replace(cenario, fontes=fontes, allowlist=ALLOWLIST_PADRAO)
    teto = max(1, len(cenario.fontes)) + draw(st.integers(min_value=0, max_value=2))
    return cenario, teto


# Feature: stealth-fallback-integration, Property 6: Sequência de Tentativa_Fetch
@settings(max_examples=100, deadline=None)
@given(caso=_casos_tentativas())
def test_property_6_sequencia_de_tentativas_fetch(caso: tuple[CenarioStealth, int]) -> None:
    """**Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.5, 5.7**"""
    cenario, teto = caso
    esperado = oraculo_stealth(cenario)
    obtido = _executar_p6(cenario, teto)
    assert obtido["excecao"] is esperado.excecao

    # A reputação pré-semeada continua intacta; só há linhas novas depois dela.
    id_base = obtido["id_base"]
    antes, depois = obtido["antes"], obtido["depois"]
    assert all(t.id <= id_base for t in antes)
    assert [t for t in depois if t.id <= id_base] == antes
    novas = [t for t in depois if t.id > id_base]
    sequencia = tuple(
        TentativaEsperada(t.dominio, t.camada, t.sucesso, t.motivo, t.status_http) for t in novas
    )

    # 1. Igual ao oráculo (e à leitura do helper comum).
    assert sequencia == esperado.tentativas
    assert sequencia == obtido["pela_api"]
    assert all(t.tentado_em == INSTANTE for t in novas)

    # 2. Derivação direta das buscas observadas, na ordem em que aconteceram:
    #    uma linha por busca com PaginaBaixada ou Motivos_Falha_Reputacao,
    #    nenhuma para os demais desfechos (Req 5.1–5.5).
    por_url = {f.url: f for f in cenario.fontes}
    derivada: list[TentativaEsperada] = []
    for camada, url in obtido["buscas"]:
        fonte = por_url[url]
        if camada == "urllib":
            linha = _tentativa_urllib(fonte.dominio_fonte, fonte.urllib)
        else:
            linha = _tentativa_stealth(fonte.dominio_fonte, fonte.item_fallback)
        if linha is not None:
            derivada.append(linha)
    assert sequencia == tuple(derivada)

    # 3. Req 5.7: nº de tentativas = nº de buscas com PaginaBaixada ou Motivos_Falha_Reputacao.
    assert len(novas) == len(derivada)

    # 4. Sem busca, sem tentativa: fontes reaproveitadas, canceladas antes de buscar,
    #    com dominio_excluido ou trava recusada e exclusões de URL ficam de fora.
    urls_buscadas = {url for _camada, url in obtido["buscas"]}
    dominios_buscados = {por_url[url].dominio_fonte for url in urls_buscadas}
    assert {t.dominio for t in novas} <= dominios_buscados
    for fonte in cenario.fontes:
        if fonte.reuso_corpo is not None:
            assert fonte.url not in urls_buscadas
    # Cada camada de uma fonte busca no máximo uma vez, e o urllib vem antes do stealth.
    assert len(obtido["buscas"]) == len(set(obtido["buscas"]))
    for url in urls_buscadas:
        camadas = [c for c, u in obtido["buscas"] if u == url]
        assert camadas in (["urllib"], ["fallback"], ["urllib", "fallback"])



# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

_HOSTS_ALLOWLIST_PADRAO = tuple(
    h for h in HOSTS if _host_na_allowlist(f"https://{h}/", ALLOWLIST_PADRAO)
)
_MOTIVOS_AMBIENTE = ("ambiente_sem_display", "navegador_indisponivel")
_SEM_HASH = "<sem hash>"  # sentinela: entrada que nenhuma regra de derivação explica


def _sha256(corpo: bytes) -> str:
    return hashlib.sha256(corpo).hexdigest()


@st.composite
def _itens_fallback_p4(draw: st.DrawFn, url: str) -> PaginaBaixada | FalhaBusca:
    """Desfechos do fallback com peso nos casos da Property 4: página, motivos de
    ambiente, ``redirecionamento_nao_permitido``, ``status_http`` e os demais."""
    tipo = draw(st.sampled_from(["ambiente", "redirecionamento", "pagina", "status",
                                 "pagina", "qualquer"]))
    if tipo == "pagina":
        return PaginaBaixada(
            status_http=draw(st.sampled_from([200, 203])),
            content_type="text/html; charset=utf-8",
            charset="utf-8",
            url_final=url,
            corpo=b"<html>p4 " + draw(st.binary(max_size=16)),
        )
    if tipo == "ambiente":
        return FalhaBusca(draw(st.sampled_from(_MOTIVOS_AMBIENTE)), url)  # type: ignore[arg-type]
    if tipo == "redirecionamento":
        return FalhaBusca("redirecionamento_nao_permitido", url)
    if tipo == "status":
        return FalhaBusca("status_http", url, status_http=draw(st.sampled_from([403, 404, 500, 503])))
    motivo = draw(st.sampled_from(MOTIVOS_FALHA_BUSCA))
    status = draw(st.sampled_from([403, 429, 404])) if motivo == "status_http" else None
    return FalhaBusca(motivo, url, status_http=status)  # type: ignore[arg-type]


@st.composite
def _casos_entradas(draw: st.DrawFn) -> tuple[CenarioStealth, int]:
    """Cenários sem exceção do fallback (Property 9) e sem falhas de reputação
    (Property 7), com viés para os caminhos da Property 4: fallback acionado por
    403/429/503 em host da allowlist, camada urllib excluída (elegível → stealth
    direto; não elegível → ``dominio_excluido``) e gravação com erro injetado."""
    cenario = draw(cenarios_stealth())
    if draw(st.booleans()):
        fontes = []
        for f in cenario.fontes:
            if draw(st.integers(min_value=0, max_value=3)):
                host = draw(st.sampled_from(_HOSTS_ALLOWLIST_PADRAO + ("outro.com.br",)))
                f = replace(
                    f,
                    host=host,
                    dominio_resultado=None,
                    urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))),
                )
                f = replace(f, fallback=draw(_itens_fallback_p4(f.url)))
            fontes.append(f)
        cenario = replace(cenario, fontes=tuple(fontes), allowlist=ALLOWLIST_PADRAO)
        if draw(st.integers(min_value=0, max_value=3)):
            # Trava sempre obtida e sem cancelamento: o fallback chega a ser chamado.
            cenario = replace(cenario, trava=(), cancelamento=None)
    if cenario.fontes and draw(st.booleans()):
        # Camada urllib excluída (3 falhas consecutivas) para parte dos domínios.
        reputacao = dict(cenario.reputacao)
        for dominio in sorted({f.dominio_fonte for f in cenario.fontes}):
            if draw(st.booleans()):
                reputacao[(dominio, CAMADA_URLLIB_ORACULO)] = (False, False, False)
        cenario = replace(cenario, reputacao=reputacao)
    if cenario.fontes and draw(st.booleans()):
        urls = [f.url for f in cenario.fontes]
        cenario = replace(
            cenario,
            gravacao_falha=frozenset(draw(st.lists(st.sampled_from(urls), max_size=len(urls)))),
        )
    teto = max(1, len(cenario.fontes)) + draw(st.integers(min_value=0, max_value=2))
    return cenario, teto


def _hashes_conteudo(db_path: Path) -> frozenset[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return frozenset(h for (h,) in conn.execute("SELECT hash_conteudo FROM conteudo_pagina"))


def _executar_p4(cenario: CenarioStealth, teto: int) -> dict[str, Any]:
    """``coletar(stealth=True)`` com fakes; devolve relatório, buscas e conteúdos."""
    execucao = preparar_execucao(cenario)
    with armazem_espiao_temporario() as armazem:
        semear_cenario(armazem, cenario, INSTANTE)
        conteudos_antes = _hashes_conteudo(armazem.db_path)
        coletor = ColetorPaginas(
            armazem,
            transporte=execucao.transporte,
            config=config_cenario(),
            relogio=relogio_fixo,
            fallback_stealth=execucao.fallback,
            allowlist_stealth=cenario.allowlist,
            trava_navegador=execucao.trava,
        )
        relatorio = coletor.coletar(
            peca_cenario(),
            resultados_do_cenario(cenario),
            teto_aceitos=teto,
            cancelado=execucao.cancelamento.cancelado,
            observador=execucao.cancelamento.observador,
            stealth=True,
        )
        lidos = {
            e.hash_conteudo: ArmazemPaginas.ler_conteudo(armazem, e.hash_conteudo)
            for e in relatorio.entradas
            if e.desfecho == "armazenado" and e.hash_conteudo is not None
        }
        return {
            "relatorio": relatorio,
            "transporte": frozenset(execucao.roteiro_transporte.chamadas),
            "fallback": frozenset(execucao.fallback.urls),
            "conteudos_antes": conteudos_antes,
            "conteudos_depois": _hashes_conteudo(armazem.db_path),
            "lidos": lidos,
        }


def _falha_urllib(item: Any) -> tuple[str, int | None]:
    """(motivo, status) da ``FalhaBusca`` urllib para o roteiro abstrato ``item``."""
    if isinstance(item, UrllibStatus):
        return "status_http", item.status
    if isinstance(item, UrllibErro):
        return item.tipo, None
    if isinstance(item, UrllibNaoHtml):
        return "nao_html", 200
    if isinstance(item, UrllibCorpoVazio):
        return "corpo_vazio", 200
    raise TypeError(f"roteiro urllib sem falha: {item!r}")


def _derivar_entrada(
    fonte: Any,
    real: Any,
    transporte: frozenset[str],
    fallback: frozenset[str],
    gravacao_falha: frozenset[str],
) -> EntradaEsperada:
    """Entrada esperada derivada direto dos roteiros da fonte e das buscas
    observadas (sem o oráculo). Só usa a entrada real para distinguir os
    desfechos sem busca da camada correspondente (reuso, cancelamento,
    ``dominio_excluido``, trava recusada)."""
    url = fonte.url

    def gravado(status: int, corpo: bytes, camada: str, motivo_urllib: str | None) -> EntradaEsperada:
        if url in gravacao_falha:  # Req 4.3: erro_armazenamento com o status da camada
            return EntradaEsperada(url, "falha", "erro_armazenamento", status, None,
                                   camada, motivo_urllib)
        return EntradaEsperada(url, "armazenado", None, status, _sha256(corpo),  # Req 4.2
                               camada, motivo_urllib)

    if url in fallback:
        # Req 8.1: motivo urllib do fallback, ou "dominio_excluido" sem busca urllib (Req 6.1).
        motivo_urllib = "status_http" if url in transporte else "dominio_excluido"
        item = fonte.item_fallback
        if isinstance(item, FalhaBusca):  # Req 3.3–3.5: motivo e status do fallback
            return EntradaEsperada(url, "falha", item.motivo, item.status_http, None,
                                   CAMADA_STEALTH_ORACULO, motivo_urllib)
        return gravado(item.status_http, item.corpo, CAMADA_STEALTH_ORACULO, motivo_urllib)

    if url in transporte:
        item = fonte.urllib
        if isinstance(item, UrllibPagina):
            return gravado(item.status, item.corpo, CAMADA_URLLIB_ORACULO, None)
        bloqueado = isinstance(item, UrllibStatus) and item.status in STATUS_BLOQUEIO_ORACULO
        if bloqueado and real.motivo in ("cancelado", "navegador_indisponivel"):
            # Interrompida entre a busca urllib e o fallback: camada urllib, sem status.
            return EntradaEsperada(url, "falha", real.motivo, None, None,
                                   CAMADA_URLLIB_ORACULO, "status_http")
        motivo, status = _falha_urllib(item)
        return EntradaEsperada(url, "falha", motivo, status, None, CAMADA_URLLIB_ORACULO, None)

    # Nenhuma busca: camada None.
    if real.desfecho == "reaproveitado" and fonte.reuso_corpo is not None:
        return EntradaEsperada(url, "reaproveitado", None, STATUS_REUSO,
                               _sha256(fonte.reuso_corpo), None, None)
    if real.motivo == "dominio_excluido":  # Req 6.2
        return EntradaEsperada(url, "falha", "dominio_excluido", None, None, None, None)
    if real.motivo == "cancelado" and real.motivo_urllib in (None, "dominio_excluido"):
        return EntradaEsperada(url, "falha", "cancelado", None, None, None, real.motivo_urllib)
    if real.motivo == "navegador_indisponivel":
        # Só no salto direto para o stealth (urllib excluída e fonte elegível).
        return EntradaEsperada(url, "falha", "navegador_indisponivel", None, None, None,
                               "dominio_excluido")
    return EntradaEsperada(url, "?", None, None, _SEM_HASH, None, None)


# Feature: stealth-fallback-integration, Property 4: Entradas do relatório com stealth
@settings(max_examples=100, deadline=None)
@given(caso=_casos_entradas())
def test_property_4_entradas_do_relatorio_com_stealth(caso: tuple[CenarioStealth, int]) -> None:
    """**Validates: Requirements 3.3, 3.4, 3.5, 4.2, 4.3, 6.2, 8.1**"""
    cenario, teto = caso
    esperado = oraculo_stealth(cenario)
    assert esperado.excecao is None  # cenários sem exceção do fallback
    obtido = _executar_p4(cenario, teto)
    relatorio = obtido["relatorio"]
    assert relatorio.stealth is True
    entradas = relatorio.entradas

    # 1. Igual ao oráculo, na ordem das fontes seguida das exclusões de URL.
    assert tuple(entrada_esperada_de(e) for e in entradas) == esperado.entradas
    assert [e.url for e in entradas] == (
        [f.url for f in cenario.fontes] + [url for url, _motivo in cenario.exclusoes]
    )

    # 2. Derivação direta dos roteiros e das buscas observadas, fonte a fonte.
    #    Motivos de ambiente viram falha da fonte e as seguintes continuam
    #    (todas as fontes têm entrada e coletar não levantou; Req 3.4).
    transporte, fallback = obtido["transporte"], obtido["fallback"]
    for fonte, real in zip(cenario.fontes, entradas, strict=False):
        derivada = _derivar_entrada(fonte, real, transporte, fallback, cenario.gravacao_falha)
        assert entrada_esperada_de(real) == derivada, fonte

    # 3. Regras de camada e motivo_urllib (Req 8.1).
    for fonte, real in zip(cenario.fontes, entradas, strict=False):
        if real.camada is None:
            assert fonte.url not in transporte and fonte.url not in fallback
        elif real.camada == CAMADA_URLLIB_ORACULO:
            assert fonte.url in transporte and fonte.url not in fallback
        else:
            assert real.camada == CAMADA_STEALTH_ORACULO and fonte.url in fallback
            assert real.motivo_urllib == (
                "status_http" if fonte.url in transporte else "dominio_excluido"
            )
        if real.motivo == "dominio_excluido":  # Req 6.2: sem rede e sem navegador
            assert (real.desfecho, real.status_http, real.camada, real.motivo_urllib,
                    real.hash_conteudo) == ("falha", None, None, None, None)
    for real in entradas[len(cenario.fontes):]:  # exclusões de URL: sem Campos_Stealth
        assert real.desfecho == "url_invalida"
        assert real.camada is None and real.motivo_urllib is None

    # 4. Conteúdo: só as páginas armazenadas viram Conteudo_Pagina novo; falhas do
    #    fallback (inclusive redirecionamento_nao_permitido) não gravam nada (Req 3.5).
    armazenados = {e.hash_conteudo for e in entradas if e.desfecho == "armazenado"}
    novos = obtido["conteudos_depois"] - obtido["conteudos_antes"]
    assert novos == armazenados - obtido["conteudos_antes"]
    for real in entradas:
        if real.desfecho == "armazenado":
            assert _sha256(obtido["lidos"][real.hash_conteudo]) == real.hash_conteudo



# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

_JANELA_REUSO_S = 30 * 24 * 3600  # janela_reuso_dias=30 de config_cenario()
_TABELAS_P5 = ("registro_coleta", "conteudo_pagina")


def _pagina_p5(url: str, status: int, corpo: bytes) -> PaginaBaixada:
    """A página P: exatamente o que o ``BuscadorPaginas`` devolve para uma
    resposta ``status`` com ``CABECALHOS_HTML`` e ``corpo`` sem redirecionamento."""
    return PaginaBaixada(
        status_http=status,
        content_type=CONTENT_TYPE_HTML,
        charset="utf-8",
        url_final=url,
        corpo=corpo,
    )


@st.composite
def _casos_p5(draw: st.DrawFn) -> tuple[CenarioStealth, CenarioStealth, bool, int]:
    """Par de cenários com as mesmas fontes (hosts da allowlist padrão, todas
    elegíveis ao fallback, sem reuso, reputação, cancelamento, trava recusada,
    exclusões ou falhas de armazém):

    - ``via_fallback``: urllib bloqueado (403/429/503) e fallback devolve P;
    - ``via_urllib``: urllib devolve P (mesmo status, content_type, charset,
      url_final e corpo) e o fallback não tem roteiro.

    Devolve também o Stealth_Efetivo do cenário urllib e o deslocamento (s) da
    segunda ``coletar`` dentro da Janela_Reuso."""
    n = draw(st.integers(min_value=1, max_value=4))
    pelo_fallback: list[FonteCenario] = []
    pelo_urllib: list[FonteCenario] = []
    for indice in range(n):
        host = draw(st.sampled_from(_HOSTS_ALLOWLIST_PADRAO))
        dominio_resultado = draw(st.one_of(st.none(), st.just(f" {host.upper()}. ")))
        status = draw(st.sampled_from([200, 203]))
        corpo = b"<html>p5 " + draw(st.binary(max_size=24))
        base = FonteCenario(
            indice=indice,
            host=host,
            urllib=UrllibPagina(corpo, status),
            dominio_resultado=dominio_resultado,
        )
        pelo_urllib.append(base)
        pelo_fallback.append(
            replace(
                base,
                urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))),
                fallback=_pagina_p5(base.url, status, corpo),
            )
        )
    via_fallback = CenarioStealth(fontes=tuple(pelo_fallback), allowlist=ALLOWLIST_PADRAO)
    via_urllib = CenarioStealth(fontes=tuple(pelo_urllib), allowlist=ALLOWLIST_PADRAO)
    stealth_urllib = draw(st.booleans())
    deslocamento = draw(st.integers(min_value=0, max_value=_JANELA_REUSO_S))
    return via_fallback, via_urllib, stealth_urllib, deslocamento


def _coletor_p5(
    armazem: ArmazemPaginas,
    transporte: Any,
    fallback: FallbackFake,
    trava: TravaFake,
    relogio: Any = relogio_fixo,
) -> ColetorPaginas:
    return ColetorPaginas(
        armazem,
        transporte=transporte,
        config=config_cenario(),
        relogio=relogio,
        fallback_stealth=fallback,
        allowlist_stealth=ALLOWLIST_PADRAO,
        trava_navegador=trava,
    )


# Feature: stealth-fallback-integration, Property 5: Página do fallback gravada como a do urllib e reaproveitável
@settings(max_examples=100, deadline=None)
@given(caso=_casos_p5())
def test_property_5_pagina_do_fallback_gravada_e_reaproveitavel(
    caso: tuple[CenarioStealth, CenarioStealth, bool, int],
) -> None:
    """**Validates: Requirements 4.1, 4.4**"""
    via_fallback, via_urllib, stealth_urllib, deslocamento = caso
    urls = [f.url for f in via_fallback.fontes]
    corpos_por_url = {f.url: f.urllib.corpo for f in via_urllib.fontes}  # type: ignore[union-attr]

    # Cenário "urllib devolve P" (baseline da camada urllib).
    exec_urllib = preparar_execucao(via_urllib)
    with armazem_espiao_temporario() as armazem_urllib:
        relatorio_urllib = _coletor_p5(
            armazem_urllib, exec_urllib.transporte, exec_urllib.fallback, exec_urllib.trava
        ).coletar(
            peca_cenario(),
            resultados_do_cenario(via_urllib),
            teto_aceitos=len(urls),
            stealth=stealth_urllib,
        )
        linhas_urllib = {t: snapshot_tabelas(armazem_urllib.db_path)[t] for t in _TABELAS_P5}
    assert exec_urllib.fallback.chamadas == []
    assert exec_urllib.roteiro_transporte.chamadas == urls

    # Cenário "urllib 403/429/503 + fallback devolve P", depois a segunda coletar.
    exec_fallback = preparar_execucao(via_fallback)
    with armazem_espiao_temporario() as armazem:
        relatorio = _coletor_p5(
            armazem, exec_fallback.transporte, exec_fallback.fallback, exec_fallback.trava
        ).coletar(
            peca_cenario(),
            resultados_do_cenario(via_fallback),
            teto_aceitos=len(urls),
            stealth=True,
        )
        linhas_fallback = {t: snapshot_tabelas(armazem.db_path)[t] for t in _TABELAS_P5}
        # Três 403/429/503 seguidos no mesmo Dominio_Fonte excluem a camada urllib
        # e as fontes seguintes desse domínio saltam direto para o fallback
        # (Req 6.1): as buscas urllib e o motivo_urllib vêm do oráculo.
        oraculo = oraculo_stealth(via_fallback)
        assert tuple(exec_fallback.roteiro_transporte.chamadas) == oraculo.chamadas_transporte
        assert exec_fallback.fallback.urls == urls
        motivo_urllib_esperado = {e.url: e.motivo_urllib for e in oraculo.entradas}

        # Req 4.1: o design só admite diferença em url_final, status_http,
        # content_type e charset, que vêm da página do fallback; como P tem os
        # mesmos valores nos dois cenários, Registro_Coleta e Conteudo_Pagina
        # (todas as colunas) são iguais.
        assert linhas_fallback == linhas_urllib
        assert len(linhas_fallback["registro_coleta"]) == len(urls)

        # Mesmo hash = sha256(corpo); ler_conteudo devolve exatamente o corpo.
        hashes: dict[str, str] = {}
        for e_fb, e_ul in zip(relatorio.entradas, relatorio_urllib.entradas, strict=True):
            assert e_fb.url == e_ul.url
            assert e_fb.desfecho == e_ul.desfecho == "armazenado"
            assert e_fb.hash_conteudo == e_ul.hash_conteudo == _sha256(corpos_por_url[e_fb.url])
            assert e_fb.status_http == e_ul.status_http
            assert e_fb.camada == CAMADA_STEALTH_ORACULO
            assert e_fb.motivo_urllib == motivo_urllib_esperado[e_fb.url]
            assert ArmazemPaginas.ler_conteudo(armazem, e_fb.hash_conteudo) == corpos_por_url[e_fb.url]
            hashes[e_fb.url] = e_fb.hash_conteudo

        # Req 4.4: segunda coletar na Janela_Reuso, mesmo armazém, stealth=True:
        # reaproveitado com o mesmo hash, sem transporte, fallback nem trava.
        transporte_vazio = TransporteRoteiro({})
        fallback_vazio = FallbackFake()
        trava_vazia = TravaFake()
        agora = INSTANTE + timedelta(seconds=deslocamento)
        segundo = _coletor_p5(
            armazem, transporte_vazio, fallback_vazio, trava_vazia, relogio=lambda: agora
        ).coletar(
            peca_cenario(),
            resultados_do_cenario(via_fallback),
            teto_aceitos=len(urls),
            stealth=True,
        )
    assert transporte_vazio.chamadas == []
    assert fallback_vazio.chamadas == []
    assert trava_vazia.chamadas == 0
    assert segundo.stealth is True
    assert [e.url for e in segundo.entradas] == urls
    for entrada in segundo.entradas:
        assert entrada.desfecho == "reaproveitado"
        assert entrada.hash_conteudo == hashes[entrada.url]
        assert entrada.camada is None and entrada.motivo_urllib is None



# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

_ParP7 = tuple[str, str]
_METODOS_P7 = ("dominio_excluido", "ultimas_tentativas", "registrar_tentativa")
_ERROS_P7 = (
    ErroArmazenamento,
    sqlite3.Error,
    sqlite3.DatabaseError,
    sqlite3.OperationalError,
    sqlite3.IntegrityError,
)


class _ArmazemReferencia(ArmazemEspiao):
    """Execução de referência da Property 7: nos mesmos pontos em que o espião
    levantaria erro de armazenamento, a leitura devolve "não excluída"
    (``dominio_excluido`` → ``False``, ``ultimas_tentativas`` → ``()``) e a
    escrita (``registrar_tentativa``) é simplesmente omitida.

    Os contadores por método seguem a mesma regra do ``ArmazemEspiao`` (índice
    0-based por chamada, contado antes de decidir), para que índices de chamada
    apontem para a mesma chamada nas duas execuções."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._omitir: dict[str, tuple[frozenset[_ParP7], frozenset[int]]] = {}
        self.omitidas = 0

    def omitir(
        self, metodo: str, *, pares: frozenset[_ParP7], chamadas: frozenset[int]
    ) -> None:
        normalizados = frozenset((d.strip().casefold().rstrip("."), c) for d, c in pares)
        self._omitir[metodo] = (normalizados, chamadas)

    def _omitida(self, metodo: str, dominio: str, camada: str) -> bool:
        indice = self.chamadas[metodo]
        self.chamadas[metodo] += 1
        pares, indices = self._omitir.get(metodo, (frozenset(), frozenset()))
        par = (dominio.strip().casefold().rstrip("."), camada)
        if par in pares or indice in indices:
            self.omitidas += 1
            return True
        return False

    def registrar_tentativa(self, dominio: str, camada: str, **kwargs: Any) -> None:
        if self._omitida("registrar_tentativa", dominio, camada):
            return
        ArmazemPaginas.registrar_tentativa(self, dominio, camada, **kwargs)

    def ultimas_tentativas(self, dominio: str, camada: str, *args: Any) -> tuple[TentativaFetch, ...]:
        if self._omitida("ultimas_tentativas", dominio, camada):
            return ()
        return ArmazemPaginas.ultimas_tentativas(self, dominio, camada, *args)

    def dominio_excluido(self, dominio: str, camada: str, *args: Any) -> bool:
        if self._omitida("dominio_excluido", dominio, camada):
            return False
        # A base chama self.ultimas_tentativas, como no espião.
        return ArmazemPaginas.dominio_excluido(self, dominio, camada, *args)


@contextmanager
def _armazem_referencia_temporario() -> Iterator[_ArmazemReferencia]:
    """Igual a ``armazem_espiao_temporario``, com o armazém de referência."""
    with tempfile.TemporaryDirectory(prefix="coletor_stealth_pbt_ref_") as diretorio:
        base = Path(diretorio)
        yield _ArmazemReferencia(
            base / "paginas.db",
            caminho_rule_store=base / "rule_store.db",
            relogio=relogio_fixo,
        )


# Injeção da Property 7: por método, pares (domínio, camada) e índices de chamada
# que falham, e a classe de erro de armazenamento levantada.
_InjecaoP7 = dict[str, tuple[frozenset[_ParP7], frozenset[int], type[BaseException]]]


def _executar_p7(
    cenario: CenarioStealth, teto: int, injecao: _InjecaoP7, *, referencia: bool
) -> dict[str, Any]:
    """``coletar(stealth=True)`` com a reputação falhando (``referencia=False``) ou
    com a execução de referência (``referencia=True``) nos mesmos pontos."""
    observados: list[dict[str, Any]] = []
    execucao = preparar_execucao(cenario, proximo_observador=lambda e: observados.append(dict(e)))
    gerenciador = _armazem_referencia_temporario() if referencia else armazem_espiao_temporario()
    with gerenciador as armazem:
        # As falhas por par do cenário são reinstaladas abaixo, com a injeção.
        semeado = replace(cenario, consultas_falham=frozenset(), registros_falham=frozenset())
        id_base = semear_cenario(armazem, semeado, INSTANTE)
        for metodo in _METODOS_P7:
            pares, chamadas, excecao = injecao[metodo]
            if referencia:
                armazem.omitir(metodo, pares=pares, chamadas=chamadas)  # type: ignore[union-attr]
            else:
                armazem.falhar(metodo, pares=pares, chamadas=chamadas, excecao=excecao)
        coletor = ColetorPaginas(
            armazem,
            transporte=execucao.transporte,
            config=config_cenario(),
            relogio=relogio_fixo,
            fallback_stealth=execucao.fallback,
            allowlist_stealth=cenario.allowlist,
            trava_navegador=execucao.trava,
        )
        relatorio = None
        excecao_coletar: BaseException | None = None
        with capturar_logs_coleta() as logs:
            try:
                relatorio = coletor.coletar(
                    peca_cenario(),
                    resultados_do_cenario(cenario),
                    teto_aceitos=teto,
                    cancelado=execucao.cancelamento.cancelado,
                    observador=execucao.cancelamento.observador,
                    stealth=True,
                )
            except Exception as exc:  # noqa: BLE001 - nenhuma deve atravessar (Req 5.6, 6.4)
                excecao_coletar = exc
        return {
            "relatorio": relatorio,
            "excecao": excecao_coletar,
            "logger": logs.eventos,
            "observador": observados,
            "transporte": tuple(execucao.roteiro_transporte.chamadas),
            "fallback": tuple((c.url, dict(c.kwargs)) for c in execucao.fallback.chamadas),
            "trava": (tuple(execucao.trava.acquires), execucao.trava.releases),
            "tabelas": snapshot_tabelas(armazem.db_path),
            "tentativas": tentativas_novas(armazem, cenario, id_base),
            "chamadas_reputacao": {m: armazem.chamadas[m] for m in _METODOS_P7},
            "omitidas": getattr(armazem, "omitidas", None),
        }


@st.composite
def _casos_p7(draw: st.DrawFn) -> tuple[CenarioStealth, int, _InjecaoP7]:
    """Cenários com falhas de armazém (por par, do gerador comum) e, em parte dos
    casos, também por índice de chamada; sem exceção do fallback (Property 9)."""
    cenario = draw(cenarios_stealth(com_falhas_armazem=True))
    if draw(st.booleans()):
        # Viés para o caminho do fallback: allowlist padrão e urllib bloqueado.
        fontes = tuple(
            replace(f, urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))))
            if draw(st.booleans())
            else f
            for f in cenario.fontes
        )
        cenario = replace(cenario, fontes=fontes, allowlist=ALLOWLIST_PADRAO)
    if cenario.fontes and draw(st.booleans()):
        # Camadas excluídas para parte dos domínios: a falha de consulta precisa
        # virar "não excluída" para o fluxo mudar de fato.
        reputacao = dict(cenario.reputacao)
        for dominio in sorted({f.dominio_fonte for f in cenario.fontes}):
            for camada in (CAMADA_URLLIB_ORACULO, CAMADA_STEALTH_ORACULO):
                if draw(st.booleans()):
                    reputacao[(dominio, camada)] = (False, False, False)
        cenario = replace(cenario, reputacao=reputacao)

    # Consulta falha em dominio_excluido ou, por dentro dele, em ultimas_tentativas.
    metodo_consulta = draw(st.sampled_from(["dominio_excluido", "ultimas_tentativas"]))
    por_chamada = draw(st.booleans())
    indices = st.frozensets(st.integers(min_value=0, max_value=6), max_size=4)
    todos_pares = frozenset(pares_reputacao(cenario))  # viés: todo par do cenário falha
    injecao: _InjecaoP7 = {}
    for metodo in _METODOS_P7:
        if metodo == "registrar_tentativa":
            pares = draw(st.sampled_from([cenario.registros_falham, todos_pares]))
        elif metodo == metodo_consulta:
            pares = draw(st.sampled_from([cenario.consultas_falham, todos_pares]))
        else:
            pares = frozenset()
        chamadas = draw(indices) if por_chamada else frozenset()
        injecao[metodo] = (pares, chamadas, draw(st.sampled_from(_ERROS_P7)))
    teto = max(1, len(cenario.fontes)) + draw(st.integers(min_value=0, max_value=2))
    return cenario, teto, injecao


# Feature: stealth-fallback-integration, Property 7: Falhas da reputação não mudam o fluxo
@settings(max_examples=100, deadline=None)
@given(caso=_casos_p7())
def test_property_7_falhas_da_reputacao_nao_mudam_o_fluxo(
    caso: tuple[CenarioStealth, int, _InjecaoP7],
) -> None:
    """**Validates: Requirements 5.6, 6.4**"""
    cenario, teto, injecao = caso
    com_falhas = _executar_p7(cenario, teto, injecao, referencia=False)
    referencia = _executar_p7(cenario, teto, injecao, referencia=True)
    atingidos = referencia["omitidas"]
    event(f"pontos de falha atingidos: {'3+' if atingidos >= 3 else atingidos}")

    # Nenhuma exceção de armazenamento atravessa coletar (nem na referência).
    assert com_falhas["excecao"] is None, repr(com_falhas["excecao"])
    assert referencia["excecao"] is None, repr(referencia["excecao"])

    # 1. Igual à execução de referência (leitura que falha = "não excluída",
    #    escrita que falha = omitida): relatório, eventos (logger e observador),
    #    transporte, fallback, trava, armazém e a mesma sequência de chamadas à
    #    reputação.
    for chave in ("relatorio", "logger", "observador", "transporte", "fallback", "trava",
                  "tabelas", "tentativas", "chamadas_reputacao"):
        assert com_falhas[chave] == referencia[chave], chave

    # 2. Sem entrada extra: uma entrada por fonte, seguida das exclusões de URL.
    relatorio = com_falhas["relatorio"]
    assert relatorio is not None and relatorio.stealth is True
    assert [e.url for e in relatorio.entradas] == (
        [f.url for f in cenario.fontes] + [url for url, _motivo in cenario.exclusoes]
    )

    # 3. Sem falhas por índice de chamada, o oráculo modela as falhas por par
    #    (consulta → "não excluída", registro → omitido): igual a ele também.
    if any(chamadas for _pares, chamadas, _exc in injecao.values()):
        return
    consulta = injecao["dominio_excluido"][0] | injecao["ultimas_tentativas"][0]
    esperado = oraculo_stealth(
        replace(cenario, consultas_falham=consulta,
                registros_falham=injecao["registrar_tentativa"][0])
    )
    assert esperado.excecao is None
    assert tuple(entrada_esperada_de(e) for e in relatorio.entradas) == esperado.entradas
    assert com_falhas["transporte"] == esperado.chamadas_transporte
    assert tuple(url for url, _kw in com_falhas["fallback"]) == esperado.chamadas_fallback
    assert len(com_falhas["trava"][0]) == esperado.acquires
    inicios = tuple(
        (e["url"], e["motivo_urllib"])
        for e in com_falhas["observador"]
        if e["evento"] == EVENTO_STEALTH_INICIO
    )
    assert inicios == esperado.stealth_inicio
    assert com_falhas["tentativas"] == esperado.tentativas




# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------
#
# Diferente das demais, esta propriedade usa a ``TRAVA_NAVEGADOR`` real do
# processo (padrão do construtor, nada injetado): é ela que precisa garantir um
# navegador por vez entre threads. O fallback continua fake (nenhum navegador é
# aberto) e é compartilhado entre as threads, medindo a concorrência.

_EXCECOES_P8 = (RuntimeError, ValueError, KeyError)
_DORMIR_P8 = 0.002  # segura cada chamada ao fallback por 2 ms
_PRAZO_JOIN_P8 = 30.0  # folga ampla: o pior caso serializado fica em dezenas de ms
_INDICES_POR_THREAD = 10  # URLs distintas entre threads: https://{host}/peca/{10*t + i}


@st.composite
def _item_fallback_p8(draw: st.DrawFn, url: str) -> PaginaBaixada | FalhaBusca | BaseException:
    """Sucesso, falha ou exceção do fallback, com o mesmo peso. A exceção é uma
    instância nova por fonte (o oráculo compara a classe)."""
    tipo = draw(st.sampled_from(["sucesso", "falha", "excecao"]))
    if tipo == "sucesso":
        return draw(paginas_stealth(url))
    if tipo == "falha":
        return draw(falhas_busca(url))
    return draw(st.sampled_from(_EXCECOES_P8))(f"p8 {url}")


@st.composite
def _roteiros_p8(draw: st.DrawFn) -> tuple[CenarioStealth, ...]:
    """Um cenário por thread (2 a 4), cada um com 1 a 3 fontes da allowlist padrão
    bloqueadas no urllib (403/429/503): toda fonte chega à trava, salvo as que a
    reputação local da thread exclui no caminho."""
    cenarios: list[CenarioStealth] = []
    for t in range(draw(st.integers(min_value=2, max_value=4))):
        fontes: list[FonteCenario] = []
        for i in range(draw(st.integers(min_value=1, max_value=3))):
            fonte = FonteCenario(
                indice=_INDICES_POR_THREAD * t + i,
                host=draw(st.sampled_from(_HOSTS_ALLOWLIST_PADRAO)),
                urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))),
            )
            fontes.append(replace(fonte, fallback=draw(_item_fallback_p8(fonte.url))))
        cenarios.append(CenarioStealth(fontes=tuple(fontes), allowlist=ALLOWLIST_PADRAO))
    return tuple(cenarios)


def _thread_p8(
    cenario: CenarioStealth,
    fallback: FallbackFake,
    barreira: threading.Barrier,
    saida: dict[str, Any],
) -> None:
    """Uma thread: coletor e armazém próprios (diretório temporário próprio), a
    trava padrão (``TRAVA_NAVEGADOR``) e o fallback compartilhado. Guarda em
    ``saida`` o relatório ou a classe da exceção de ``coletar``, os eventos do
    observador e qualquer erro de infraestrutura do próprio teste."""
    observados: list[dict[str, Any]] = []
    saida["observados"] = observados
    try:
        execucao = preparar_execucao(cenario)  # só o transporte é usado
        with armazem_espiao_temporario() as armazem:
            semear_cenario(armazem, cenario, INSTANTE)
            coletor = ColetorPaginas(
                armazem,
                transporte=execucao.transporte,
                config=config_cenario(),  # prazo da trava = TIMEOUT_S, folgado
                relogio=relogio_fixo,
                fallback_stealth=fallback,
                allowlist_stealth=cenario.allowlist,
            )
            barreira.wait()  # todas as threads começam juntas
            try:
                saida["relatorio"] = coletor.coletar(
                    peca_cenario(),
                    resultados_do_cenario(cenario),
                    teto_aceitos=len(cenario.fontes),
                    observador=lambda e: observados.append(dict(e)),
                    stealth=True,
                )
            except Exception as exc:  # noqa: BLE001 - exceção do fallback, comparada ao oráculo
                saida["excecao"] = type(exc)
            saida["transporte"] = tuple(execucao.roteiro_transporte.chamadas)
    except BaseException as exc:  # noqa: BLE001 - erro do próprio teste, relatado no assert
        saida["erro_infra"] = exc


def _recusas_trava(observados: list[dict[str, Any]]) -> list[str]:
    """URLs com ``falha/navegador_indisponivel`` por trava não obtida no prazo
    (camada urllib ou None); o mesmo motivo vindo do fallback tem camada stealth."""
    return [
        e["url"]
        for e in observados
        if e["evento"] == EVENTO_ENTRADA
        and e["motivo"] == "navegador_indisponivel"
        and e.get("camada") != CAMADA_STEALTH_ORACULO
    ]


# Feature: stealth-fallback-integration, Property 8: Um navegador por vez, trava sempre liberada
@settings(max_examples=100, deadline=None)
@given(cenarios=_roteiros_p8())
def test_property_8_um_navegador_por_vez_trava_sempre_liberada(
    cenarios: tuple[CenarioStealth, ...],
) -> None:
    """**Validates: Requirements 7.4, 7.6**"""
    assert not TRAVA_NAVEGADOR.locked(), "TRAVA_NAVEGADOR presa antes do exemplo"

    trava_detida_na_chamada: list[bool] = []
    fallback = FallbackFake(
        {f.url: f.item_fallback for c in cenarios for f in c.fontes},
        # Req 7.4: o fallback só roda enquanto alguém detém a trava do processo.
        durante=lambda _url: trava_detida_na_chamada.append(TRAVA_NAVEGADOR.locked()),
        dormir=_DORMIR_P8,
    )
    barreira = threading.Barrier(len(cenarios), timeout=_PRAZO_JOIN_P8)
    saidas: list[dict[str, Any]] = [{} for _ in cenarios]
    threads = [
        threading.Thread(target=_thread_p8, args=(c, fallback, barreira, s), daemon=True)
        for c, s in zip(cenarios, saidas, strict=True)
    ]
    vivas: list[threading.Thread] = []
    presa_ao_final = False
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=_PRAZO_JOIN_P8)
        vivas = [t for t in threads if t.is_alive()]
        presa_ao_final = TRAVA_NAVEGADOR.locked()
    finally:
        # Proteção da suíte: se todas as threads terminaram e a trava continua
        # presa, ninguém a detém legitimamente (só estas threads a usam aqui),
        # então ela é liberada para não travar os testes seguintes. O assert
        # abaixo falha do mesmo jeito. Com thread viva (possível detentora
        # legítima) nada é liberado.
        if not any(t.is_alive() for t in threads) and TRAVA_NAVEGADOR.locked():
            TRAVA_NAVEGADOR.release()

    assert not vivas, f"{len(vivas)} thread(s) não terminaram em {_PRAZO_JOIN_P8}s"
    for saida in saidas:
        assert "erro_infra" not in saida, repr(saida.get("erro_infra"))

    # Req 7.4: no máximo uma chamada ao fallback em andamento, sempre com a trava detida.
    assert fallback.max_em_andamento <= 1
    assert len(trava_detida_na_chamada) == len(fallback.chamadas)
    assert all(trava_detida_na_chamada)
    assert all(c.timeout == TIMEOUT_S for c in fallback.chamadas)

    # Req 7.6: livre ao final, inclusive depois de exceções do fallback.
    assert not presa_ao_final
    assert TRAVA_NAVEGADOR.acquire(blocking=False) is True
    TRAVA_NAVEGADOR.release()

    excecoes = sucessos = falhas = recusas = 0
    for cenario, saida in zip(cenarios, saidas, strict=True):
        urls_thread = {f.url for f in cenario.fontes}
        chamadas = tuple(u for u in fallback.urls if u in urls_thread)  # ordem da thread
        recusadas = _recusas_trava(saida["observados"])
        excecoes += saida.get("excecao") is not None
        recusas += len(recusadas)
        if recusadas:
            # Trava recusada pelo prazo: falha/navegador_indisponivel e nenhuma
            # chamada ao fallback para essas fontes (Req 7.5); o restante segue.
            assert not set(recusadas) & set(chamadas)
            continue
        # Sem recusa, a thread se comporta exatamente como o oráculo (trava obtida).
        esperado = oraculo_stealth(cenario)
        assert saida.get("excecao") is esperado.excecao
        assert chamadas == esperado.chamadas_fallback
        assert saida["transporte"] == esperado.chamadas_transporte
        if esperado.excecao is None:
            relatorio = saida["relatorio"]
            assert tuple(entrada_esperada_de(e) for e in relatorio.entradas) == esperado.entradas
            for e in relatorio.entradas:
                if e.camada == CAMADA_STEALTH_ORACULO:
                    sucessos += e.desfecho == "armazenado"
                    falhas += e.desfecho == "falha"
    event(f"threads: {len(cenarios)}")
    event(f"exceção do fallback em alguma thread: {excecoes > 0}")
    event(f"sucesso e falha do fallback: {sucessos > 0 and falhas > 0}")
    event(f"trava recusada pelo prazo: {recusas > 0}")




# ---------------------------------------------------------------------------
# Property 10 (parte do coletor)
# ---------------------------------------------------------------------------
#
# Linha do tempo comum ao observador e ao ``FallbackFake``: cada evento entregue
# ao observador entra como ``("observador", evento)`` e cada chamada ao fallback
# como ``("fallback", url)`` (o fake escreve antes de qualquer outra ação).

from coleta_paginas.coletor import CAMPOS_EVENTO_ENTRADA_STEALTH  # noqa: E402
from coleta_paginas.coletor import CAMPOS_EVENTO_RESUMO_STEALTH  # noqa: E402
from coleta_paginas.coletor import CAMPOS_EVENTO_STEALTH_INICIO  # noqa: E402
from tests.fakes_stealth import MARCA  # noqa: E402
from tests.fakes_stealth import itens_fallback as _itens_fallback_p10  # noqa: E402

_CAMADAS_P10 = (CAMADA_URLLIB_ORACULO, CAMADA_STEALTH_ORACULO)
_DESFECHOS_P10 = ("armazenado", "reaproveitado", "falha", "url_invalida")
_CAMPOS_POR_EVENTO_P10 = {
    EVENTO_ENTRADA: CAMPOS_EVENTO_ENTRADA_STEALTH,
    EVENTO_RESUMO: CAMPOS_EVENTO_RESUMO_STEALTH,
    EVENTO_STEALTH_INICIO: CAMPOS_EVENTO_STEALTH_INICIO,
}


@st.composite
def _casos_p10(draw: st.DrawFn) -> tuple[CenarioStealth, PecaConsultada, int]:
    """Cenários com cancelamento, trava, exclusões e exceções do fallback, com
    viés para o caminho do fallback (allowlist padrão e urllib 403/429/503), e
    a peça do cenário ou uma peça sem marca (Trava_Entrada)."""
    cenario = draw(cenarios_stealth(com_excecao=True))
    if draw(st.booleans()):
        fontes = []
        for f in cenario.fontes:
            if draw(st.integers(min_value=0, max_value=3)):
                f = replace(
                    f,
                    host=draw(st.sampled_from(_HOSTS_ALLOWLIST_PADRAO)),
                    dominio_resultado=None,
                    urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))),
                )
                f = replace(f, fallback=draw(_itens_fallback_p10(f.url, com_excecao=True)))
            fontes.append(f)
        cenario = replace(cenario, fontes=tuple(fontes), allowlist=ALLOWLIST_PADRAO)
        if draw(st.booleans()):
            # Trava sempre obtida e sem cancelamento: o fallback chega a ser chamado.
            cenario = replace(cenario, trava=(), cancelamento=None)
    peca = draw(st.sampled_from([peca_cenario(), peca_cenario(), peca_cenario(),
                                 PecaConsultada(codigo_peca=CODIGO, marca_peca=None,
                                                nomes_candidatos=())]))
    teto = max(1, len(cenario.fontes)) + draw(st.integers(min_value=0, max_value=2))
    return cenario, peca, teto


def _executar_p10(cenario: CenarioStealth, peca: PecaConsultada, teto: int) -> dict[str, Any]:
    """``coletar(stealth=True)`` com a linha do tempo comum e o logger capturado."""
    linha_do_tempo: list[tuple[str, Any]] = []
    execucao = preparar_execucao(
        cenario,
        linha_do_tempo=linha_do_tempo,
        proximo_observador=lambda e: linha_do_tempo.append(("observador", dict(e))),
    )
    with armazem_espiao_temporario() as armazem:
        semear_cenario(armazem, cenario, INSTANTE)
        coletor = ColetorPaginas(
            armazem,
            transporte=execucao.transporte,
            config=config_cenario(),
            relogio=relogio_fixo,
            fallback_stealth=execucao.fallback,
            allowlist_stealth=cenario.allowlist,
            trava_navegador=execucao.trava,
        )
        relatorio = None
        excecao: type[BaseException] | None = None
        with capturar_logs_coleta() as logs:
            try:
                relatorio = coletor.coletar(
                    peca,
                    resultados_do_cenario(cenario),
                    teto_aceitos=teto,
                    cancelado=execucao.cancelamento.cancelado,
                    observador=execucao.cancelamento.observador,
                    stealth=True,
                )
            except Exception as exc:  # noqa: BLE001 - exceção do fallback, prevista pelo oráculo
                excecao = type(exc)
        return {
            "relatorio": relatorio,
            "excecao": excecao,
            "linha_do_tempo": linha_do_tempo,
            "logger": logs.eventos,
            "fallback": list(execucao.fallback.urls),
        }


def _evento_entrada_esperado_p10(entrada: Any) -> dict[str, Any]:
    """Evento ``entrada`` montado à mão a partir da ``EntradaRelatorio``."""
    return {
        "evento": EVENTO_ENTRADA,
        "codigo_peca": entrada.codigo_peca,
        "marca_peca": entrada.marca_peca,
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


def _projetar_evento_entrada_p10(evento: dict[str, Any]) -> EntradaEsperada:
    return EntradaEsperada(
        url=evento["url"],
        desfecho=evento["desfecho"],
        motivo=evento["motivo"],
        status_http=evento["status_http"],
        hash_conteudo=evento["hash_conteudo"],
        camada=evento["camada"],
        motivo_urllib=evento["motivo_urllib"],
    )


# Feature: stealth-fallback-integration, Property 10: Forma dos eventos com stealth (coletor)
@settings(max_examples=100, deadline=None)
@given(caso=_casos_p10())
def test_property_10_forma_dos_eventos_com_stealth_coletor(
    caso: tuple[CenarioStealth, PecaConsultada, int],
) -> None:
    """**Validates: Requirements 8.2, 8.3, 8.5**"""
    cenario, peca, teto = caso
    obtido = _executar_p10(cenario, peca, teto)
    linha_do_tempo = obtido["linha_do_tempo"]
    eventos = [item for tipo, item in linha_do_tempo if tipo == "observador"]

    # Logger e observador recebem os mesmos eventos, na mesma ordem.
    assert [evento for _msg, evento in obtido["logger"]] == eventos

    # Cada evento tem exatamente os campos do seu tipo (com stealth).
    for evento in eventos:
        assert evento["evento"] in _CAMPOS_POR_EVENTO_P10, evento
        assert evento.keys() == _CAMPOS_POR_EVENTO_P10[evento["evento"]], evento

    if peca.marca_peca is None:
        # Trava_Entrada: só o resumo (com as duas camadas zeradas), sem fallback.
        assert obtido["excecao"] is None
        assert obtido["fallback"] == []
        assert eventos == [
            {
                "evento": EVENTO_RESUMO,
                "status": "nao_enriquecivel",
                "motivo": "marca_sem_referencia",
                "codigo_peca": CODIGO,
                "marca_peca": None,
                "contagem": {d: 0 for d in _DESFECHOS_P10},
                "contagem_camada": {c: 0 for c in _CAMADAS_P10},
            }
        ]
        return

    esperado = oraculo_stealth(cenario)
    assert obtido["excecao"] is esperado.excecao
    assert tuple(obtido["fallback"]) == esperado.chamadas_fallback

    # Eventos entrada: Campos_Stealth iguais aos da entrada (oráculo e relatório).
    eventos_entrada = [e for e in eventos if e["evento"] == EVENTO_ENTRADA]
    assert tuple(_projetar_evento_entrada_p10(e) for e in eventos_entrada) == esperado.entradas

    # stealth_inicio: um por chamada, com URL e motivo_urllib do oráculo.
    inicios = [e for e in eventos if e["evento"] == EVENTO_STEALTH_INICIO]
    assert [(e["url"], e["motivo_urllib"]) for e in inicios] == list(esperado.stealth_inicio)
    por_url = {f.url: f for f in cenario.fontes}
    for inicio in inicios:
        assert inicio["codigo_peca"] == CODIGO
        assert inicio["marca_peca"] == MARCA
        fonte = por_url[inicio["url"]]
        assert inicio["dominio"].strip().casefold().rstrip(".") == fonte.dominio_fonte

    # Linha do tempo: cada chamada ao fallback é precedida imediatamente pelo
    # stealth_inicio da mesma fonte; não há stealth_inicio sem chamada.
    chamadas = [i for i, (tipo, _item) in enumerate(linha_do_tempo) if tipo == "fallback"]
    event(f"chamadas ao fallback: {min(len(chamadas), 3)}{'+' if len(chamadas) >= 3 else ''}")
    event(f"exceção do fallback: {esperado.excecao is not None}")
    assert len(chamadas) == len(inicios)
    for indice, inicio in zip(chamadas, inicios, strict=True):
        assert indice > 0
        assert linha_do_tempo[indice - 1] == ("observador", inicio)
        assert linha_do_tempo[indice] == ("fallback", inicio["url"])
    for indice, (tipo, item) in enumerate(linha_do_tempo):
        if tipo == "observador" and item["evento"] == EVENTO_STEALTH_INICIO:
            assert indice + 1 < len(linha_do_tempo)
            assert linha_do_tempo[indice + 1] == ("fallback", item["url"])

    if esperado.excecao is not None:
        # A exceção atravessa coletar logo depois da chamada: sem entrada da
        # fonte e sem resumo; o último evento é o stealth_inicio dela.
        assert eventos and eventos[-1]["evento"] == EVENTO_STEALTH_INICIO
        assert linha_do_tempo[-1] == ("fallback", eventos[-1]["url"])
        assert all(e["evento"] != EVENTO_RESUMO for e in eventos)
        return

    relatorio = obtido["relatorio"]
    assert relatorio is not None and relatorio.stealth is True

    # Sequência completa montada à mão: [stealth_inicio] + entrada por fonte,
    # exclusões de URL e o resumo com contagem_camada (duas camadas, zeros inclusos).
    inicio_por_url = {e["url"]: e for e in inicios}
    sequencia: list[dict[str, Any]] = []
    for entrada in relatorio.entradas:
        if entrada.url in inicio_por_url:
            sequencia.append(
                {
                    "evento": EVENTO_STEALTH_INICIO,
                    "codigo_peca": CODIGO,
                    "marca_peca": MARCA,
                    "url": entrada.url,
                    "dominio": entrada.dominio,
                    "motivo_urllib": entrada.motivo_urllib,
                }
            )
        sequencia.append(_evento_entrada_esperado_p10(entrada))
    contagem = {d: 0 for d in _DESFECHOS_P10}
    for entrada in relatorio.entradas:
        contagem[entrada.desfecho] += 1
    contagem_camada = {
        camada: sum(1 for e in relatorio.entradas if e.camada == camada) for camada in _CAMADAS_P10
    }
    sequencia.append(
        {
            "evento": EVENTO_RESUMO,
            "status": "executada",
            "motivo": None,
            "codigo_peca": CODIGO,
            "marca_peca": MARCA,
            "contagem": contagem,
            "contagem_camada": contagem_camada,
        }
    )
    assert eventos == sequencia

    # contagem_camada consistente com as entradas do oráculo também.
    resumo = eventos[-1]
    assert set(resumo["contagem_camada"]) == set(_CAMADAS_P10)
    for camada in _CAMADAS_P10:
        assert resumo["contagem_camada"][camada] == sum(
            1 for e in esperado.entradas if e.camada == camada
        )



# ---------------------------------------------------------------------------
# Property 11 (parte do coletor)
# ---------------------------------------------------------------------------
#
# Marcadores únicos (gerados por exemplo) são postos em tudo o que as buscas
# trazem ou tocam: corpos urllib, do fallback e reaproveitados; Content-Type e
# ``url_final`` das páginas do fallback; Content-Type das ``FalhaBusca`` do
# fallback; mensagens das exceções do fallback; caminho do perfil
# (``ESTAGIARIO_STEALTH_PROFILE_DIR``). Nenhum marcador pode aparecer nos
# eventos do logger (mensagem JSON e dicionário) nem do observador. Os helpers
# replicam localmente os de ``tests/test_integracao_stealth_pbt.py``
# (``_token_p11``, ``_marcar_item_p11``).

import json as _json_p11  # noqa: E402
import uuid as _uuid_p11  # noqa: E402
from itertools import count as _count_p11  # noqa: E402

import pytest as _pytest_p11  # noqa: E402
from hypothesis import example  # noqa: E402

import config as _config_p11  # noqa: E402
from tests.fakes_stealth import itens_fallback as _itens_fallback_p11  # noqa: E402

_CONTADOR_P11 = _count_p11()
_VAR_PERFIL_P11 = "ESTAGIARIO_STEALTH_PROFILE_DIR"
_CAMPOS_POR_EVENTO_P11 = {
    EVENTO_ENTRADA: CAMPOS_EVENTO_ENTRADA_STEALTH,
    EVENTO_RESUMO: CAMPOS_EVENTO_RESUMO_STEALTH,
    EVENTO_STEALTH_INICIO: CAMPOS_EVENTO_STEALTH_INICIO,
}


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
    if isinstance(item, PaginaBaixada):
        return replace(
            item,
            corpo=item.corpo + _corpo_marcado_p11(token),
            content_type=_content_type_marcado_p11(token),
            url_final=_url_final_marcada_p11(url, token),
        )
    if isinstance(item, FalhaBusca):
        return replace(item, content_type=_content_type_marcado_p11(token))
    if isinstance(item, type) and issubclass(item, BaseException):
        return item(_mensagem_excecao_p11(token))
    if isinstance(item, BaseException):
        return type(item)(_mensagem_excecao_p11(token))
    return item


def _marcar_fonte_p11(fonte: FonteCenario, token: str) -> FonteCenario:
    marca = _corpo_marcado_p11(token)
    return replace(
        fonte,
        fallback=_marcar_item_p11(fonte.item_fallback, fonte.url, token),
        urllib=(
            replace(fonte.urllib, corpo=fonte.urllib.corpo + marca)
            if isinstance(fonte.urllib, UrllibPagina)
            else fonte.urllib
        ),
        reuso_corpo=None if fonte.reuso_corpo is None else fonte.reuso_corpo + marca,
    )


@st.composite
def _casos_p11(draw: st.DrawFn) -> tuple[CenarioStealth, str, int]:
    """Cenários com cancelamento, trava, exclusões, reuso e exceções do fallback,
    com viés para o caminho do fallback (como na Property 10), marcados depois de
    gerados (o oráculo vê os itens já marcados)."""
    token = _token_p11(draw(st.uuids()))
    cenario = draw(cenarios_stealth(com_excecao=True))
    if draw(st.integers(min_value=0, max_value=3)):
        fontes = []
        for f in cenario.fontes:
            if draw(st.integers(min_value=0, max_value=3)):
                f = replace(
                    f,
                    host=draw(st.sampled_from(_HOSTS_ALLOWLIST_PADRAO)),
                    dominio_resultado=None,
                    urllib=UrllibStatus(draw(st.sampled_from(sorted(STATUS_BLOQUEIO_ORACULO)))),
                )
                f = replace(f, fallback=draw(_itens_fallback_p11(f.url, com_excecao=True)))
            fontes.append(f)
        cenario = replace(cenario, fontes=tuple(fontes), allowlist=ALLOWLIST_PADRAO)
        if draw(st.booleans()):
            # Trava sempre obtida e sem cancelamento: o fallback chega a ser chamado.
            cenario = replace(cenario, trava=(), cancelamento=None)
    cenario = replace(cenario, fontes=tuple(_marcar_fonte_p11(f, token) for f in cenario.fontes))
    teto = max(1, len(cenario.fontes)) + draw(st.integers(min_value=0, max_value=2))
    return cenario, token, teto


def _serializar_p11(valor: Any) -> str:
    return _json_p11.dumps(valor, ensure_ascii=False, default=repr, sort_keys=True)


_SEMENTE_FIXA_P11 = _uuid_p11.UUID("0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0")


def _caso_fixo_p11(ultimo_fallback: Any) -> tuple[CenarioStealth, str, int]:
    """Caso que alcança o fallback com certeza: página do fallback, falha do
    fallback, urllib com página e reuso, e ``ultimo_fallback`` na última fonte."""
    token = _token_p11(_SEMENTE_FIXA_P11)
    fontes = (
        FonteCenario(0, "www.mercadocar.com.br", UrllibStatus(403),
                     fallback=PaginaBaixada(200, CONTENT_TYPE_HTML, "utf-8",
                                            "https://www.mercadocar.com.br/peca/0", b"<html>ok")),
        FonteCenario(1, "mercadocar.com.br", UrllibStatus(429),
                     fallback=FalhaBusca("status_http", "https://mercadocar.com.br/peca/1",
                                         status_http=403)),
        FonteCenario(2, "outro.com.br", UrllibPagina(b"<html>urllib")),
        FonteCenario(3, "outro.com.br", UrllibStatus(403), reuso_corpo=b"<html>reuso"),
        FonteCenario(4, "loja.mercadocar.com.br", UrllibStatus(503), fallback=ultimo_fallback),
    )
    cenario = CenarioStealth(
        fontes=tuple(_marcar_fonte_p11(f, token) for f in fontes), allowlist=ALLOWLIST_PADRAO
    )
    return cenario, token, len(fontes)


# Feature: stealth-fallback-integration, Property 11: Só campos permitidos saem com stealth (coletor)
@settings(max_examples=100, deadline=None)
@given(caso=_casos_p11())
@example(caso=_caso_fixo_p11(FalhaBusca("ambiente_sem_display", "https://loja.mercadocar.com.br/peca/4")))
@example(caso=_caso_fixo_p11(KeyError("x")))
def test_property_11_so_campos_permitidos_saem_com_stealth_coletor(
    caso: tuple[CenarioStealth, str, int],
) -> None:
    """Parte do coletor: ``coletar(stealth=True)`` com ``FallbackFake``
    (páginas, falhas e exceções marcadas), transporte com corpos marcados,
    reuso marcado e o perfil apontando para um caminho marcado. O marcador chega
    ao armazém (âncora), mas não aparece nos eventos do logger nem do
    observador; as chaves ficam restritas a ``CAMPOS_EVENTO_*_STEALTH`` e
    ``CAMPOS_EVENTO_STEALTH_INICIO``.

    **Validates: Requirements 4.6, 8.4**
    """
    cenario, token, teto = caso
    esperado = oraculo_stealth(cenario)
    observados: list[dict[str, Any]] = []
    execucao = preparar_execucao(cenario, proximo_observador=lambda e: observados.append(dict(e)))

    with _pytest_p11.MonkeyPatch.context() as mp, armazem_espiao_temporario() as armazem:
        mp.setenv(_VAR_PERFIL_P11, str(armazem.db_path.parent / f"perfil-{token}"))
        assert token in str(_config_p11.stealth_profile_dir())

        semear_cenario(armazem, cenario, INSTANTE)
        coletor = ColetorPaginas(
            armazem,
            transporte=execucao.transporte,
            config=config_cenario(),
            relogio=relogio_fixo,
            fallback_stealth=execucao.fallback,
            allowlist_stealth=cenario.allowlist,
            trava_navegador=execucao.trava,
        )
        relatorio = None
        excecao: BaseException | None = None
        with capturar_logs_coleta() as logs:
            try:
                relatorio = coletor.coletar(
                    peca_cenario(),
                    resultados_do_cenario(cenario),
                    teto_aceitos=teto,
                    cancelado=execucao.cancelamento.cancelado,
                    observador=execucao.cancelamento.observador,
                    stealth=True,
                )
            except Exception as exc:  # noqa: BLE001 - exceção do fallback, prevista pelo oráculo
                excecao = exc

        # Âncora: o coletor real seguiu o oráculo e o marcador chegou ao armazém.
        assert execucao.fallback.urls == list(esperado.chamadas_fallback)
        assert (type(excecao) if excecao is not None else None) is esperado.excecao
        registros = armazem.registros_da_peca(CODIGO, peca_cenario().marca_peca)
        if excecao is not None:
            assert token in str(excecao)
            for registro in registros:
                assert token.encode() in armazem.ler_conteudo(registro.hash_conteudo)
        else:
            assert relatorio is not None and relatorio.stealth is True
            assert tuple(entrada_esperada_de(e) for e in relatorio.entradas) == esperado.entradas
            por_url = {r.url_original: r for r in registros}
            for entrada in relatorio.entradas:
                if entrada.desfecho in ("armazenado", "reaproveitado"):
                    assert token.encode() in armazem.ler_conteudo(entrada.hash_conteudo)
                if entrada.desfecho == "armazenado" and entrada.camada == CAMADA_STEALTH_ORACULO:
                    registro = por_url[entrada.url]
                    assert token in registro.url_final
                    assert token in registro.content_type
        event(f"p11 coletor: fallback chamado={bool(execucao.fallback.chamadas)}, "
              f"exceção={excecao is not None}")

    # Nenhum marcador nos eventos do logger (mensagem JSON e dicionário), em
    # nenhum registro do logger ``coleta_paginas``, nem no observador.
    assert logs.registros, "o coletor deveria ter emitido eventos"
    for registro_log in logs.registros:
        assert token not in registro_log.getMessage()
    for mensagem, evento in logs.eventos:
        assert token not in mensagem
        assert token not in _serializar_p11(evento)
        assert _json_p11.loads(mensagem) == evento
    for evento in observados:
        assert token not in _serializar_p11(evento)
    assert [evento for _msg, evento in logs.eventos] == observados

    # Chaves restritas aos Campos_Permitidos com stealth.
    for evento in observados:
        assert evento.get("evento") in _CAMPOS_POR_EVENTO_P11, evento
        assert set(evento) <= _CAMPOS_POR_EVENTO_P11[evento["evento"]], evento
