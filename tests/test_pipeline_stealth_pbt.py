# Feature: stealth-fallback-integration, Property 1: Chave_Stealth e Stealth_Efetivo
"""Propriedades do pipeline com o fallback stealth (stealth-fallback-integration).

O módulo tem duas partes:

1. **Bloco comum** (porta de coleta registradora, cenário fixo, estratégias da
   Chave_Stealth e o helper ``executar_stealth``), compartilhado pelas
   propriedades de pipeline desta feature (Property 1, parte do pipeline, e
   Property 12).
2. **Propriedades**, uma por seção, cada uma com o cabeçalho
   ``# Feature: stealth-fallback-integration, Property N: <título>``.

Os fakes de pipeline (grupo, LLM roteirizado, verificador) vêm do bloco comum de
``tests/test_pipeline_coleta_pbt.py``; nenhuma função ``test_*`` é importada.
Tudo roda sem rede, sem banco real (``sem_banco``) e sem navegador.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

from arbitration.pesquisa_web import ContextoPesquisaWeb
from coleta_paginas.coletor import ColetorPaginas
from coleta_paginas.integracao import PREFIXO_AVISO, IntegracaoColeta
from db.armazem_paginas import ArmazemPaginas
from db.rule_store import RuleStore
from pipeline import ResultadoCaso, executar_caso
from tests.fakes_stealth import FallbackFake, TravaFake, itens_fallback
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_pipeline_coleta_pbt import CABECALHOS_HTML as CABECALHOS_HTML_PIPELINE
from tests.test_pipeline_coleta_pbt import (
    DEPENDENCIAS_FK,
    NOMES,
    T0,
    URLS,
    CenarioGrupo,
    EspecMemoria,
    EspecVerificador,
    LLMRoteirizado,
    PedirIntervencaoFake,
    VerificadorSimples,
    _conteudo_rule_store,
    buscar_grupo_fake,
    cenarios_grupo,
    config_coleta,
    especs_memoria,
    especs_verificador,
    fazer_registro,
    semear_regra,
)
from tools.buscador_paginas import FalhaBusca, PaginaBaixada, RespostaHTTP
from verification.models import ResultadoVerificacao
from verification.serper_client import ResultadoOrganico

# ===========================================================================
# Bloco comum
# ===========================================================================

NOME_VAR_STEALTH = "ESTAGIARIO_COLETA_STEALTH_HABILITADA"

# Conjuntos do oráculo, escritos aqui de forma independente de ``config``.
_HABILITADA = ("1", "true", "sim", "on")
_DESABILITADA = ("0", "false", "nao", "não", "off")

SETTINGS_PIPELINE_STEALTH = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


class PortaColetaRegistradora:
    """Porta de coleta fake: registra ``(ativacao, contexto)`` e não publica nada."""

    def __init__(self) -> None:
        self.ativacoes: list = []
        self.contextos: list[ContextoPesquisaWeb] = []

    def processar(self, ativacao, *, contexto, trace):
        self.ativacoes.append(ativacao)
        self.contextos.append(contexto)
        return None


def cenario_nomes_divergentes() -> CenarioGrupo:
    """Duplicata_real com nomes divergentes: garante ao menos uma Ativacao_Pesquisa
    (com a pesquisa ligada ou desligada, a arbitragem de nome chama a porta)."""
    registros = (
        fazer_registro(1, "FILTRO DE AR", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
        fazer_registro(2, "FILTRO CABINE", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
    )
    return CenarioGrupo("CF1000", 1, "MANN-FILTER", registros, (("duplicata_real", (1, 2)),))


def resultado_inconclusivo() -> ResultadoVerificacao:
    return ResultadoVerificacao("inconclusivo", None, "sem confirmação clara", [])


@dataclass
class ExecucaoStealth:
    """O que uma execução de ``executar_caso`` deixa observável para o stealth."""

    resultado: ResultadoCaso | None
    erro: tuple[str, str] | None
    avisos: list[str]
    porta: Any
    chamadas_llm: list[tuple[str, str]] = field(default_factory=list)


def executar_stealth(
    cenario: CenarioGrupo,
    *,
    fallback_stealth: bool | None,
    pesquisa_web: bool = True,
    coleta_html: bool | None = True,
    integracao_coleta: Any = None,
    verificador: Any = None,
    **extras: Any,
) -> ExecucaoStealth:
    """Roda ``pipeline.executar_caso`` com fakes novos.

    ``integracao_coleta=None`` usa uma ``PortaColetaRegistradora`` nova;
    ``extras`` são repassados a ``executar_caso`` (rule_store, pedir_intervencao…).
    """
    porta = integracao_coleta if integracao_coleta is not None else PortaColetaRegistradora()
    llm = LLMRoteirizado(cenario)
    verificar = verificador if verificador is not None else VerificadorSimples(resultado_inconclusivo())
    avisos: list[str] = []
    resultado: ResultadoCaso | None = None
    erro: tuple[str, str] | None = None
    try:
        resultado = executar_caso(
            cenario.search_ref,
            cenario.brand_id,
            llm=llm,
            dependencias_fk=DEPENDENCIAS_FK,
            buscar_grupo=buscar_grupo_fake(cenario),
            on_aviso=avisos.append,
            verificar_web=verificar,
            pesquisa_web=pesquisa_web,
            coleta_html=coleta_html,
            integracao_coleta=porta,
            fallback_stealth=fallback_stealth,
            **extras,
        )
    except Exception as exc:  # noqa: BLE001 — observado pelo teste
        erro = (type(exc).__name__, str(exc))
    return ExecucaoStealth(resultado, erro, avisos, porta, list(llm.chamadas))


# --- Estratégias da Chave_Stealth -------------------------------------------

_ESPACOS = st.text(alphabet=" \t", max_size=3)


@st.composite
def _com_ruido(draw, base: str) -> str:
    """``base`` com caixa aleatória por caractere e espaços nas bordas."""
    maiusculas = draw(st.lists(st.booleans(), min_size=len(base), max_size=len(base)))
    texto = "".join(c.upper() if m else c for c, m in zip(base, maiusculas))
    return draw(_ESPACOS) + texto + draw(_ESPACOS)


def _normalizar(valor: str) -> str:
    return valor.strip().casefold()


_texto_arbitrario = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    max_size=12,
).filter(lambda v: _normalizar(v) not in {"", *_HABILITADA, *_DESABILITADA})

# ``None`` = variável ausente.
valores_chave_stealth = st.one_of(
    st.none(),
    st.just(""),
    _ESPACOS,
    st.sampled_from(_HABILITADA).flatmap(_com_ruido),
    st.sampled_from(_DESABILITADA).flatmap(_com_ruido),
    _texto_arbitrario,
)


def oraculo_chave(valor: str | None) -> str:
    """Chave_Stealth esperada: ausente/vazia → desabilitada (Req 1.2)."""
    if valor is None:
        return "desabilitada"
    normalizado = _normalizar(valor)
    if normalizado in _HABILITADA:
        return "habilitada"
    if normalizado == "" or normalizado in _DESABILITADA:
        return "desabilitada"
    return "invalida"


def oraculo_stealth_efetivo(opcao: bool | None, valor: str | None) -> str:
    """A Opcao_Stealth prevalece, inclusive sobre chave inválida (Req 1.4)."""
    if opcao is None:
        return oraculo_chave(valor)
    return "habilitada" if opcao else "desabilitada"


# ===========================================================================
# Property 1: Chave_Stealth e Stealth_Efetivo (parte do pipeline)
# ===========================================================================
# Feature: stealth-fallback-integration, Property 1: Chave_Stealth e Stealth_Efetivo
# **Validates: Requirements 1.4, 1.6**


@SETTINGS_PIPELINE_STEALTH
@given(
    fallback_stealth=st.sampled_from([None, True, False]),
    valor_chave=valores_chave_stealth,
    pesquisa_web=st.booleans(),
)
def test_property1_stealth_efetivo_no_pipeline(fallback_stealth, valor_chave, pesquisa_web, sem_banco):
    esperado = oraculo_stealth_efetivo(fallback_stealth, valor_chave)
    with pytest.MonkeyPatch.context() as mp:
        if valor_chave is None:
            mp.delenv(NOME_VAR_STEALTH, raising=False)
        else:
            mp.setenv(NOME_VAR_STEALTH, valor_chave)
        execucao = executar_stealth(
            cenario_nomes_divergentes(), fallback_stealth=fallback_stealth, pesquisa_web=pesquisa_web
        )

    assert execucao.erro is None
    porta = execucao.porta
    assert len(porta.contextos) >= 1, "o cenário deve produzir ao menos uma Ativacao_Pesquisa"
    for contexto in porta.contextos:
        assert isinstance(contexto, ContextoPesquisaWeb)
        assert contexto.stealth_efetivo == esperado



# ===========================================================================
# Property 12: O fallback não altera decisões
# ===========================================================================
# Feature: stealth-fallback-integration, Property 12: O fallback não altera decisões
# **Validates: Requirements 9.1, 9.3**

ALLOWLIST_P12 = frozenset({"mercadocar.com.br"})
# URLs da Allowlist_Stealth (host igual e subdomínios): o transporte fake
# responde 403 (Status_Bloqueio), o que torna a fonte elegível ao fallback.
URLS_MERCADOCAR = (
    "https://www.mercadocar.com.br/produto/filtro-1",
    "https://loja.mercadocar.com.br/p/2",
    "https://mercadocar.com.br/peca/3",
)
_HOSTS_MERCADOCAR = ("mercadocar.com.br", "www.mercadocar.com.br", "loja.mercadocar.com.br")


def _eh_mercadocar(url: str) -> bool:
    host = url.split("://", 1)[-1].split("/", 1)[0].casefold()
    return host in _HOSTS_MERCADOCAR


class TransporteBloqueioMercadocar:
    """Transporte_HTTP fake: 403 para a Mercadocar, 200 HTML para os demais."""

    def __init__(self) -> None:
        self.chamadas: list[str] = []

    def __call__(self, url, cabecalhos, timeout) -> RespostaHTTP:
        self.chamadas.append(url)
        if _eh_mercadocar(url):
            return RespostaHTTP(status=403, cabecalhos=CABECALHOS_HTML_PIPELINE, blocos=iter([b"bloqueado"]))
        return RespostaHTTP(status=200, cabecalhos=CABECALHOS_HTML_PIPELINE, blocos=iter([b"<html>ok</html>"]))


def pagina_stealth(url: str) -> PaginaBaixada:
    return PaginaBaixada(
        status_http=200,
        content_type="text/html; charset=utf-8",
        charset="utf-8",
        url_final=url,
        corpo=b"<html>stealth " + url.encode() + b"</html>",
    )


@dataclass(frozen=True)
class EspecStealth:
    """Comportamento do stealth numa execução comparada.

    - ``desabilitado``: ``fallback_stealth=False`` (coletor real, sem fallback);
    - ``sucesso``: o fallback devolve uma página para toda URL;
    - ``falhas_por_fonte``: ``roteiro`` URL → página ou ``FalhaBusca``;
    - ``trava_recusada``: a Trava_Navegador nunca é obtida;
    - ``excecao``: o fallback levanta ``excecao`` (vira ``erro`` na integração).
    """

    tipo: str
    roteiro: tuple[tuple[str, Any], ...] = ()
    excecao: type[Exception] = RuntimeError

    def criar(self) -> tuple[FallbackFake, TravaFake]:
        if self.tipo == "trava_recusada":
            return FallbackFake(padrao=FalhaBusca("rede", "x")), TravaFake(padrao=False)
        if self.tipo == "excecao":
            return FallbackFake(padrao=self.excecao("mensagem secreta do fallback")), TravaFake()
        if self.tipo == "falhas_por_fonte":
            return FallbackFake(dict(self.roteiro), padrao=FalhaBusca("timeout", "x")), TravaFake()
        # sucesso (e desabilitado, que nunca deve chamar o fallback)
        return FallbackFake({u: pagina_stealth(u) for u in URLS_MERCADOCAR},
                            padrao=pagina_stealth(URLS_MERCADOCAR[0])), TravaFake()


@st.composite
def especs_stealth(draw) -> EspecStealth:
    tipo = draw(st.sampled_from(["desabilitado", "sucesso", "falhas_por_fonte", "trava_recusada", "excecao"]))
    if tipo == "falhas_por_fonte":
        roteiro = tuple((url, draw(itens_fallback(url))) for url in URLS_MERCADOCAR)
        return EspecStealth(tipo, roteiro=roteiro)
    if tipo == "excecao":
        return EspecStealth(tipo, excecao=draw(st.sampled_from([RuntimeError, KeyError, ValueError, OSError])))
    return EspecStealth(tipo)


def organicos_mercadocar(codigo: str, marca: str) -> st.SearchStrategy[tuple[ResultadoOrganico, ...]]:
    """Resultados_Estruturados com ao menos um resultado da Mercadocar com o
    código no título; os demais podem ser de outros domínios."""
    titulo = st.sampled_from(NOMES).map(lambda nome: f"{codigo} {nome}")
    mercadocar = st.builds(
        ResultadoOrganico,
        title=titulo,
        link=st.sampled_from(URLS_MERCADOCAR),
        snippet=st.sampled_from([f"ref {codigo} {marca}", f"ref {codigo} {marca}", "peça original", ""]),
        position=st.one_of(st.none(), st.integers(min_value=1, max_value=10)),
    )
    outro = st.builds(
        ResultadoOrganico,
        title=st.sampled_from([f"{codigo} ", "", "XYZ "]).flatmap(
            lambda prefixo: st.sampled_from(NOMES).map(lambda nome: prefixo + nome)
        ),
        link=st.sampled_from(URLS),
        snippet=st.sampled_from(["", "peça original", f"ref {codigo} MANN-FILTER"]),
        position=st.one_of(st.none(), st.integers(min_value=1, max_value=10)),
    )
    return st.tuples(
        st.lists(mercadocar, min_size=1, max_size=3), st.lists(outro, max_size=2)
    ).flatmap(lambda partes: st.permutations(partes[0] + partes[1]).map(tuple))


@st.composite
def especs_verificador_stealth(draw, cenario) -> EspecVerificador:
    """Verificadores com Resultados_Estruturados que incluem a Mercadocar (peso
    maior), ou qualquer verificador do bloco comum do spec anterior."""
    tipo = draw(st.sampled_from(["protocolo", "protocolo", "serper", "base"]))
    if tipo == "base":
        return draw(especs_verificador(cenario))
    resultado = ResultadoVerificacao(
        draw(st.sampled_from(["confirmado", "inconclusivo"])),
        draw(st.one_of(st.none(), st.sampled_from([r.name for r in cenario.registros]))),
        "roteiro",
        [],
    )
    itens = draw(organicos_mercadocar(cenario.search_ref, cenario.brand))
    if tipo == "protocolo":
        return EspecVerificador("protocolo", resultado=resultado, resultados_pesquisa=itens)
    corpo = {
        "organic": [
            {"title": o.title, "link": o.link, "snippet": o.snippet,
             **({"position": o.position} if o.position is not None else {})}
            for o in itens
        ]
    }
    return EspecVerificador("serper", corpo_serper=corpo, erro_serper=False)


@st.composite
def casos_p12(draw):
    # Peso extra no grupo de nomes divergentes, que sempre ativa a pesquisa.
    cenario = draw(st.one_of(cenarios_grupo(), st.just(cenario_nomes_divergentes())))
    return cenario, draw(especs_verificador_stealth(cenario)), draw(especs_memoria)


@dataclass
class ExecucaoP12:
    execucao: ExecucaoStealth
    pedidos: list
    regras: list
    chamadas_verificador: int
    fallback: FallbackFake
    trava: TravaFake
    transporte: TransporteBloqueioMercadocar


def executar_p12(cenario, verificador: EspecVerificador, memoria: EspecMemoria,
                 stealth: EspecStealth, *, base: Path, pesquisa_web: bool,
                 fallback_stealth: bool) -> ExecucaoP12:
    """Uma execução com ``IntegracaoColeta`` real e ``ColetorPaginas`` sobre um
    ``ArmazemPaginas`` próprio, num diretório temporário novo."""
    diretorio = Path(tempfile.mkdtemp(dir=base))
    fallback, trava = stealth.criar()
    transporte = TransporteBloqueioMercadocar()

    def fabrica() -> ColetorPaginas:
        armazem = ArmazemPaginas(
            diretorio / "paginas.db", caminho_rule_store=diretorio / "memoria.db", relogio=lambda: T0
        )
        return ColetorPaginas(
            armazem,
            transporte=transporte,
            config=config_coleta(),
            relogio=lambda: T0,
            fallback_stealth=fallback,
            allowlist_stealth=ALLOWLIST_P12,
            trava_navegador=trava,
        )

    store: RuleStore | None = None
    if memoria.usar_rule_store:
        store = RuleStore(diretorio / "memoria.db")
        if memoria.regra_previa:
            semear_regra(store, cenario)
    pedir = PedirIntervencaoFake(memoria.pedir == "com_valor") if memoria.pedir else None
    verificar = verificador.criar()

    execucao = executar_stealth(
        cenario,
        fallback_stealth=fallback_stealth,
        pesquisa_web=pesquisa_web,
        coleta_html=True,
        integracao_coleta=IntegracaoColeta(fabrica_coletor=fabrica),
        verificador=verificar,
        rule_store=store,
        pedir_intervencao=pedir,
    )
    return ExecucaoP12(
        execucao=execucao,
        pedidos=list(pedir.pedidos) if pedir else [],
        regras=_conteudo_rule_store(store),
        chamadas_verificador=verificar.chamadas,
        fallback=fallback,
        trava=trava,
        transporte=transporte,
    )


def _caso_p12_fixo():
    """Duplicata_real com nomes divergentes e Resultados_Estruturados com a
    Mercadocar (403 no urllib → fallback) e uma fonte comum (200)."""
    cenario = cenario_nomes_divergentes()
    resultados = (
        ResultadoOrganico("CF1000 FILTRO DE AR", URLS_MERCADOCAR[0], "ref CF1000 MANN-FILTER", 1),
        ResultadoOrganico("CF1000 FILTRO CABINE", URLS[1], "ref CF1000 MANN-FILTER", 2),
        ResultadoOrganico("CF1000 FILTRO CABINE", URLS_MERCADOCAR[1], "peça original", 3),
    )
    verificador = EspecVerificador(
        "protocolo",
        resultado=ResultadoVerificacao("inconclusivo", None, "sem confirmação clara", []),
        resultados_pesquisa=resultados,
    )
    return cenario, verificador, EspecMemoria(usar_rule_store=True, regra_previa=False, pedir="com_valor")


_FALHAS_FIXAS = EspecStealth(
    "falhas_por_fonte",
    roteiro=(
        (URLS_MERCADOCAR[0], FalhaBusca("ambiente_sem_display", URLS_MERCADOCAR[0])),
        (URLS_MERCADOCAR[1], pagina_stealth(URLS_MERCADOCAR[1])),
        (URLS_MERCADOCAR[2], FalhaBusca("status_http", URLS_MERCADOCAR[2], status_http=403)),
    ),
)


@SETTINGS_PIPELINE_STEALTH
@given(
    caso=casos_p12(),
    stealth=especs_stealth(),
    pesquisa_web=st.booleans(),
    exigir_alcance=st.just(False),
)
@example(caso=_caso_p12_fixo(), stealth=EspecStealth("sucesso"), pesquisa_web=True, exigir_alcance=True)
@example(caso=_caso_p12_fixo(), stealth=_FALHAS_FIXAS, pesquisa_web=True, exigir_alcance=True)
@example(caso=_caso_p12_fixo(), stealth=EspecStealth("excecao", excecao=KeyError), pesquisa_web=True,
         exigir_alcance=True)
@example(caso=_caso_p12_fixo(), stealth=EspecStealth("trava_recusada"), pesquisa_web=True, exigir_alcance=True)
@example(caso=_caso_p12_fixo(), stealth=EspecStealth("desabilitado"), pesquisa_web=True, exigir_alcance=True)
def test_property12_fallback_nao_altera_decisoes(caso, stealth, pesquisa_web, exigir_alcance, tmp_path, sem_banco):
    cenario, verificador, memoria = caso

    referencia = executar_p12(
        cenario, verificador, memoria, EspecStealth("desabilitado"),
        base=tmp_path, pesquisa_web=pesquisa_web, fallback_stealth=False,
    )
    com_stealth = executar_p12(
        cenario, verificador, memoria, stealth,
        base=tmp_path, pesquisa_web=pesquisa_web, fallback_stealth=stealth.tipo != "desabilitado",
    )

    # A referência nunca toca no fallback nem na trava (Req 10.1).
    assert referencia.fallback.chamadas == []
    assert referencia.trava.acquires == []

    # ResultadoCaso: particionamento, decisões (DecisaoCampo inclusas) e SQL (Req 9.1).
    assert com_stealth.execucao.erro == referencia.execucao.erro
    assert com_stealth.execucao.resultado == referencia.execucao.resultado

    # Mesmas chamadas ao Verificador_Web, pedidos de intervenção e RuleStore (Req 9.3).
    assert com_stealth.chamadas_verificador == referencia.chamadas_verificador
    assert com_stealth.pedidos == referencia.pedidos
    assert com_stealth.regras == referencia.regras

    # Prompts ao LLM idênticos: nada do fallback chega ao modelo (Req 9.1).
    assert com_stealth.execucao.chamadas_llm == referencia.execucao.chamadas_llm

    # As mensagens fora da coleta são as mesmas; o stealth só acrescenta
    # linhas "Coleta de páginas:".
    def _sem_coleta(avisos: list[str]) -> list[str]:
        return [m for m in avisos if not m.startswith(PREFIXO_AVISO)]

    assert _sem_coleta(com_stealth.execucao.avisos) == _sem_coleta(referencia.execucao.avisos)

    # A trava fake fica livre ao final (inclusive com exceção do fallback).
    assert not com_stealth.trava.detida
    event(f"stealth={stealth.tipo}, fallback chamado={bool(com_stealth.fallback.chamadas)}")

    if stealth.tipo == "desabilitado":
        assert com_stealth.fallback.chamadas == []
        assert com_stealth.trava.acquires == []

    # Nos exemplos fixos, o caminho do fallback é de fato alcançado.
    if exigir_alcance:
        if stealth.tipo in ("sucesso", "falhas_por_fonte", "excecao"):
            assert com_stealth.fallback.chamadas, "o fallback deveria ter sido chamado"
            assert all(_eh_mercadocar(c.url) for c in com_stealth.fallback.chamadas)
        elif stealth.tipo == "trava_recusada":
            assert com_stealth.trava.acquires, "a trava deveria ter sido pedida"
            assert com_stealth.fallback.chamadas == []
        if stealth.tipo == "falhas_por_fonte":
            assert len(com_stealth.fallback.chamadas) >= 2
