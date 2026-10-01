# Feature: html-extract-on-web-search, Property 7: A coleta não altera decisões
"""Propriedades do pipeline com a coleta de páginas (html-extract-on-web-search).

O módulo tem duas partes:

1. **Bloco comum** (fakes, estratégias e o helper ``executar``), compartilhado por
   todas as propriedades de pipeline desta feature (Properties 7, 9, 15, 16 e 17).
2. **Propriedades**, uma por seção, cada uma com o cabeçalho
   ``# Feature: html-extract-on-web-search, Property N: <título>``.

Tudo roda sobre fakes, sem rede e sem banco real:

- ``buscar_grupo`` fake devolve o grupo gerado;
- ``LLMRoteirizado`` responde de forma determinística à partição (``particao``),
  ao juiz de nome (``decisao_nome``), ao julgamento de campo (``decisao_campo``)
  e à regra de ``similarity_id`` (``decisao_similarity_id``), registrando cada
  chamada (schema e prompt);
- verificadores fakes: simples (sem o protocolo), com ``verificar_com_resultados``
  e um que roda a Skill_Serper real sobre ``ClienteSerper`` com transporte fake;
- ``pedir_intervencao`` fake, RuleStore e Armazem_Paginas em diretórios
  temporários por execução;
- ``IntegracaoColeta`` com fábrica e transporte HTTP fakes, ou uma porta fake;
- ``sem_banco`` (``tests/conftest.py``) isola ``tools.reliability`` do Postgres,
  usado por ``arbitrar_por_provedor`` e pela confiabilidade dos campos numéricos.
"""

from __future__ import annotations

import copy
import os
import re
import tempfile
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st

import arbitration.arbitrar as _arbitrar_mod
import arbitration.nome as _nome_mod
import pipeline as _pipeline_mod
from arbitration.models import DecisaoCampo
from arbitration.provedor_informacao import arbitrar_por_provedor as _arbitrar_por_provedor_real
from arbitration.pesquisa_web import (
    CONTEXTO_WEB_DESLIGADA,
    JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
    JUSTIFICATIVA_INTERVENCAO_DESLIGADA,
    MOTIVO_PESQUISA_DESLIGADA,
)
from coleta_paginas.coletor import ColetorPaginas, ConfigColeta
from coleta_paginas.integracao import NOME_EVENTO_TRACE, PREFIXO_AVISO, IntegracaoColeta
from db.armazem_paginas import ArmazemPaginas
from db.rule_store import RuleStore
from loop.models import EventoExecucao
from loop.tracing import TraceCollector
from memory.intervencao import registrar_resposta_intervencao
from memory.models import PedidoIntervencao, RegraProposta, RespostaIntervencao
from memory.recuperador import consultar_intervencao
from pipeline import ResultadoCaso, executar_caso
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tools.buscador_paginas import ErroTransporte, RespostaHTTP
from tools.fk_introspection import FkDependency
from tools.group_fetch import RegistroCatalogPart
from verification.models import FonteWeb, ResultadoVerificacao
from verification.resultados_estruturados import VerificacaoComResultados
from verification.serper_agent import verificar_nomenclatura_peca_serper
from verification.serper_client import ClienteSerper, ResultadoOrganico, SerperRequisicaoError

# ===========================================================================
# Bloco comum
# ===========================================================================

DEPENDENCIAS_FK: list = []
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
CABECALHOS_HTML = {"content-type": "text/html; charset=utf-8"}
# Chave sintética (não é credencial); longa o bastante para não colidir com textos.
API_KEY_SERPER_FAKE = "chave-fake-serper-teste"

CODIGOS = ("CF1000", "JE4699", "83061")
# "OEM" é marca genérica: exercita a Trava_Entrada (nao_enriquecivel).
MARCAS = ("MANN-FILTER", "Bosch", "OEM")
# Pares cujos tokens não são subconjunto um do outro divergem
# (verification.divergencia); "POLIA" ⊂ "POLIA DA CORREIA" não diverge e
# exercita o juiz textual.
NOMES = (
    "FILTRO DE AR",
    "ELEMENTO FILTRANTE AR",
    "FILTRO CABINE",
    "Filtro de Ar",
    "PASTILHA DE FREIO",
    "POLIA",
    "POLIA DA CORREIA",
)
URLS = (
    "https://produto.mercadolivre.com.br/p/1",
    "https://loja.exemplo.com/p",
    "https://b.com.br/x",
    "https://c.com/falha",
    "not-a-url",
)
ROTULOS = ("duplicata_real", "kit_componente", "variante_dimensional", "distinto_nao_classificado")


# --- Grupo -----------------------------------------------------------------


@dataclass(frozen=True)
class CenarioGrupo:
    """Família gerada: registros e a partição que o LLM roteirizado devolve."""

    search_ref: str
    brand_id: int
    brand: str
    registros: tuple[RegistroCatalogPart, ...]
    subclusters: tuple[tuple[str, tuple[int, ...]], ...]

    def resposta_particao(self) -> dict:
        return {
            "subclusters": [
                {"label": label, "membro_ids": list(ids), "justificativa": f"roteiro {label}"}
                for label, ids in self.subclusters
            ]
        }


def fazer_registro(
    id_: int,
    name: str,
    *,
    search_ref: str,
    brand_id: int,
    brand: str,
    application: str | None = None,
    width: float | None = None,
    similarity_id: int | None = None,
) -> RegistroCatalogPart:
    return RegistroCatalogPart(
        id=id_,
        search_ref=search_ref,
        brand_id=brand_id,
        brand=brand,
        name=name,
        width=width,
        depth=None,
        height=None,
        gross_weight=None,
        net_weight=None,
        ncm=None,
        barcode=None,
        application=application,
        born_at=None,
        deprecated_at=None,
        similarity_id=similarity_id,
        created=datetime(2020, 1, 1) + timedelta(seconds=id_),
    )


@st.composite
def cenarios_grupo(draw) -> CenarioGrupo:
    """Grupos de 2 a 4 registros, com nomes que em geral divergem, e uma
    partição válida (cada id em exatamente um subcluster)."""
    search_ref = draw(st.sampled_from(CODIGOS))
    brand = draw(st.sampled_from(MARCAS))
    brand_id = MARCAS.index(brand) + 1
    ids = draw(st.lists(st.integers(min_value=1, max_value=500), min_size=2, max_size=4, unique=True))
    nomes = draw(st.lists(st.sampled_from(NOMES), min_size=len(ids), max_size=len(ids)))
    application_comum = draw(st.sampled_from([None, "GOL 1.0 1991/2001"]))
    registros = tuple(
        fazer_registro(
            id_,
            nome,
            search_ref=search_ref,
            brand_id=brand_id,
            brand=brand,
            application=application_comum,
            width=draw(st.sampled_from([None, 10.0, 12.5])),
            similarity_id=draw(st.sampled_from([None, None, 5, 6])),
        )
        for id_, nome in zip(ids, nomes)
    )

    # Partição: cada id recebe um índice de subcluster; rótulos com peso nos dois
    # que alcançam a pesquisa web (duplicata_real e distinto_nao_classificado).
    indices = draw(st.lists(st.integers(min_value=0, max_value=2), min_size=len(ids), max_size=len(ids)))
    grupos: dict[int, list[int]] = {}
    for id_, indice in zip(ids, indices):
        grupos.setdefault(indice, []).append(id_)
    rotulo = st.sampled_from(
        ["duplicata_real", "duplicata_real", "distinto_nao_classificado", "distinto_nao_classificado",
         "kit_componente", "variante_dimensional"]
    )
    subclusters = tuple((draw(rotulo), tuple(membros)) for _, membros in sorted(grupos.items()))
    return CenarioGrupo(search_ref, brand_id, brand, registros, subclusters)


def buscar_grupo_fake(cenario: CenarioGrupo):
    def _buscar(search_ref: str, brand_id: int) -> list[RegistroCatalogPart]:
        assert (search_ref, brand_id) == (cenario.search_ref, cenario.brand_id)
        return list(cenario.registros)

    return _buscar


# --- LLM roteirizado --------------------------------------------------------

_ID_PROMPT = re.compile(r"id=(\d+)")


class LLMRoteirizado:
    """LLMProvider fake determinístico; registra ``(schema_name, user)``.

    - ``particao``: a partição do cenário;
    - ``decisao_nome``: escolhe o menor ``id=`` citado no prompt;
    - ``decisao_campo``: escolhe o menor ``id=`` citado, com confiança alta;
    - ``decisao_similarity_id``: mantém o grupo sinalizado.
    Qualquer outro schema derruba o teste.
    """

    def __init__(self, cenario: CenarioGrupo) -> None:
        self._cenario = cenario
        self.chamadas: list[tuple[str, str]] = []

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append((schema_name, user))
        if schema_name == "particao":
            return self._cenario.resposta_particao()
        ids = sorted({int(m) for m in _ID_PROMPT.findall(user)})
        if schema_name == "decisao_nome":
            return {"modo": "escolher", "part_id_escolhido": ids[0], "justificativa": "roteiro: menor id"}
        if schema_name == "decisao_campo":
            return {"part_id_escolhido": ids[0], "confianca": "alta", "justificativa": "roteiro: menor id"}
        if schema_name == "decisao_similarity_id":
            return {"acao": "manter_sinalizado", "confianca": "alta", "justificativa": "roteiro"}
        raise AssertionError(f"chamada inesperada ao LLM: schema_name={schema_name!r}")


# --- Verificadores web fakes -----------------------------------------------


class VerificadorSimples:
    """Verificador_Web sem o protocolo de Resultados_Estruturados."""

    def __init__(self, resultado: ResultadoVerificacao) -> None:
        self._resultado = resultado
        self.chamadas = 0

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        self.chamadas += 1
        if on_evento is not None:
            on_evento(f"verificador fake: {codigo} {marca} {list(nomes_conflitantes)}")
        return copy.deepcopy(self._resultado)


class VerificadorComProtocolo(VerificadorSimples):
    """Implementa ``VerificadorComResultados``; ``resultados_pesquisa=None`` simula
    uma ativação sem Pesquisa_Realizada."""

    def __init__(
        self,
        resultado: ResultadoVerificacao,
        resultados_pesquisa: tuple[ResultadoOrganico, ...] | None,
    ) -> None:
        super().__init__(resultado)
        self._resultados_pesquisa = resultados_pesquisa

    def verificar_com_resultados(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        resultado = self(codigo, marca, nomes_conflitantes, on_evento=on_evento)
        return VerificacaoComResultados(resultado, self._resultados_pesquisa)


@dataclass
class TransporteSerperFake:
    """Transporte do ``ClienteSerper``: corpo JSON fixo ou erro; conta as chamadas."""

    corpo: dict
    erro: bool = False
    chamadas: int = 0

    def __call__(self, url, dados, cabecalhos, timeout):
        self.chamadas += 1
        if self.erro:
            raise SerperRequisicaoError("falha simulada")
        return copy.deepcopy(self.corpo)


class VerificadorSerperFake:
    """Skill_Serper real (fallback determinístico, ``llm=None``) sobre
    ``ClienteSerper`` com transporte fake — sem rede."""

    def __init__(self, corpo: dict, erro: bool) -> None:
        self.transporte = TransporteSerperFake(corpo, erro)
        self.chamadas = 0

    def _cliente(self) -> ClienteSerper:
        return ClienteSerper(api_key=API_KEY_SERPER_FAKE, timeout=1.0, transporte=self.transporte)

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        self.chamadas += 1
        return verificar_nomenclatura_peca_serper(
            codigo, marca, nomes_conflitantes, on_evento=on_evento, llm=None, cliente=self._cliente()
        )

    def verificar_com_resultados(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        self.chamadas += 1
        capturados: list[tuple[ResultadoOrganico, ...]] = []
        resultado = verificar_nomenclatura_peca_serper(
            codigo, marca, nomes_conflitantes, on_evento=on_evento, llm=None,
            cliente=self._cliente(), on_resultados=capturados.append,
        )
        return VerificacaoComResultados(resultado, capturados[0] if capturados else None)


@dataclass(frozen=True)
class EspecVerificador:
    """Descrição imutável de um verificador; ``criar`` devolve uma instância nova
    (contadores zerados) para cada execução comparada."""

    tipo: str  # simples | protocolo | serper
    resultado: ResultadoVerificacao | None = None
    resultados_pesquisa: tuple[ResultadoOrganico, ...] | None = None
    corpo_serper: dict = field(default_factory=dict)
    erro_serper: bool = False

    def criar(self):
        if self.tipo == "simples":
            return VerificadorSimples(self.resultado)
        if self.tipo == "protocolo":
            return VerificadorComProtocolo(self.resultado, self.resultados_pesquisa)
        return VerificadorSerperFake(self.corpo_serper, self.erro_serper)


def organicos(codigo: str) -> st.SearchStrategy[tuple[ResultadoOrganico, ...]]:
    item = st.builds(
        ResultadoOrganico,
        # Código explícito na maioria dos títulos, para a seleção aceitar fontes.
        title=st.sampled_from([f"{codigo} ", f"{codigo} ", "", "XYZ "]).flatmap(
            lambda prefixo: st.sampled_from(NOMES).map(lambda nome: prefixo + nome)
        ),
        link=st.sampled_from(URLS + URLS[:3]),
        snippet=st.sampled_from(["", "peça original", f"ref {codigo} MANN-FILTER"]),
        position=st.one_of(st.none(), st.integers(min_value=1, max_value=10)),
    )
    # Vazia é rara mas possível (Pesquisa_Realizada sem orgânicos).
    return st.one_of(st.just(()), st.lists(item, min_size=1, max_size=4).map(tuple), st.lists(item, min_size=1, max_size=4).map(tuple))


@st.composite
def resultados_verificacao(draw, cenario: CenarioGrupo) -> ResultadoVerificacao:
    nomes = [r.name for r in cenario.registros]
    return ResultadoVerificacao(
        status=draw(st.sampled_from(["confirmado", "inconclusivo"])),
        nome_sugerido=draw(st.one_of(st.none(), st.just("  "), st.sampled_from(nomes))),
        justificativa=draw(st.sampled_from(["", "fontes concordam", "sem confirmação clara"])),
        fontes=draw(
            st.lists(
                st.builds(FonteWeb, url=st.sampled_from(URLS[:3]), nome_encontrado=st.sampled_from(nomes)),
                max_size=2,
            )
        ),
    )


@st.composite
def especs_verificador(draw, cenario: CenarioGrupo) -> EspecVerificador:
    # Peso maior nos verificadores que entregam Resultados_Estruturados.
    tipo = draw(st.sampled_from(["simples", "protocolo", "protocolo", "serper", "serper"]))
    if tipo == "simples":
        return EspecVerificador("simples", resultado=draw(resultados_verificacao(cenario)))
    if tipo == "protocolo":
        return EspecVerificador(
            "protocolo",
            resultado=draw(resultados_verificacao(cenario)),
            resultados_pesquisa=draw(
                st.one_of(st.none(), organicos(cenario.search_ref), organicos(cenario.search_ref))
            ),
        )
    itens = draw(organicos(cenario.search_ref))
    corpo = {
        "organic": [
            {"title": o.title, "link": o.link, "snippet": o.snippet,
             **({"position": o.position} if o.position is not None else {})}
            for o in itens
        ]
    }
    return EspecVerificador("serper", corpo_serper=corpo, erro_serper=draw(st.sampled_from([False, False, True])))


# --- Intervenção humana e memória ------------------------------------------

REGRA_OPERADOR = RegraProposta(
    titulo="Preferir nome mais descritivo",
    condicao="Quando nomes de filtro ou polia divergem na nomenclatura do catálogo",
    resolucao="Escolher o nome com mais termos descritivos da peça",
)


class PedirIntervencaoFake:
    """Ponte humana fake: responde com o primeiro candidato ou sem valor."""

    def __init__(self, com_valor: bool) -> None:
        self._com_valor = com_valor
        self.pedidos: list[PedidoIntervencao] = []

    def __call__(self, pedido: PedidoIntervencao) -> RespostaIntervencao:
        self.pedidos.append(pedido)
        origem_id, valor = pedido.candidatos[0] if self._com_valor else (None, None)
        return RespostaIntervencao(
            resposta_humana="O operador confirmou a nomenclatura mais descritiva.",
            regra=REGRA_OPERADOR,
            valor=valor,
            origem_id=origem_id,
            criado_por="teste",
        )


def semear_regra(store: RuleStore, cenario: CenarioGrupo) -> None:
    """Grava uma regra de intervenção de nome parecida com o caso (pode ou não
    passar do limiar lexical — os dois lados da comparação a recebem igual)."""
    pedido = PedidoIntervencao(
        ponto="nome",
        grupo_ref=f"{cenario.search_ref}:{cenario.brand}",
        search_ref=cenario.search_ref,
        marca=cenario.brand,
        nomes_conflitantes=sorted({r.name for r in cenario.registros}),
        motivo="Semente do teste.",
    )
    registrar_resposta_intervencao(
        store,
        pedido,
        RespostaIntervencao(resposta_humana="Semente.", regra=REGRA_OPERADOR, criado_por="teste"),
    )


@dataclass(frozen=True)
class EspecMemoria:
    usar_rule_store: bool
    regra_previa: bool
    pedir: str | None  # None | "com_valor" | "sem_valor"


especs_memoria = st.builds(
    EspecMemoria,
    usar_rule_store=st.booleans(),
    regra_previa=st.booleans(),
    pedir=st.sampled_from([None, "com_valor", "sem_valor"]),
)


# --- Coleta: transporte HTTP, fábricas e porta fakes -----------------------


class FalhaInesperadaTransporte(RuntimeError):
    """Exceção fora do contrato do Transporte_HTTP (só ``ErroTransporte`` é esperado)."""


class TransporteColetaFake:
    """Transporte_HTTP fake da coleta; ``modo`` define a resposta para toda URL.

    - ``ok``: 200 com HTML;
    - ``falhas``: alterna 500, ``ErroTransporte("timeout")`` e 200 por URL;
    - ``excecao``: levanta ``FalhaInesperadaTransporte``;
    - ``cancela``: ativa ``sinal`` na primeira chamada e responde 200.
    """

    def __init__(self, modo: str, sinal: threading.Event | None = None) -> None:
        self.modo = modo
        self.sinal = sinal
        self.chamadas: list[str] = []

    def __call__(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> RespostaHTTP:
        self.chamadas.append(url)
        if self.modo == "excecao":
            raise FalhaInesperadaTransporte("falha inesperada do transporte")
        if self.modo == "cancela" and self.sinal is not None:
            self.sinal.set()
        if self.modo == "falhas":
            escolha = len(url) % 3
            if escolha == 0:
                return RespostaHTTP(status=500, cabecalhos=CABECALHOS_HTML, blocos=iter([b"erro"]))
            if escolha == 1:
                raise ErroTransporte("timeout")
        return RespostaHTTP(status=200, cabecalhos=CABECALHOS_HTML, blocos=iter([b"<html>ok</html>"]))


def config_coleta() -> ConfigColeta:
    return ConfigColeta(timeout_s=1.0, teto_aceitos_ambiente=None, janela_reuso_dias=30)


def fabrica_coletor_real(diretorio: Path, transporte: TransporteColetaFake):
    """Fábrica de ``ColetorPaginas`` real sobre ``ArmazemPaginas`` em ``diretorio``."""

    def _fabrica() -> ColetorPaginas:
        armazem = ArmazemPaginas(
            diretorio / "paginas.db",
            caminho_rule_store=diretorio / "memoria.db",
            relogio=lambda: T0,
        )
        return ColetorPaginas(armazem, transporte=transporte, config=config_coleta(), relogio=lambda: T0)

    return _fabrica


class FalhaFabricaColetor(RuntimeError):
    pass


class FalhaColetar(ValueError):
    pass


class ColetorQueFalha:
    def coletar(self, *args, **kwargs):
        raise FalhaColetar("falha dentro de coletar")


class PortaColetaFake:
    """Porta de coleta fake: registra as ativações e não publica nada."""

    def __init__(self) -> None:
        self.ativacoes: list = []

    def processar(self, ativacao, *, contexto, trace):
        self.ativacoes.append(ativacao)
        return None


# Comportamentos de coleta comparados com a referência ``desabilitada_opcao``.
COMPORTAMENTOS_COLETA = (
    "desabilitada_opcao",   # coleta_html=False
    "desabilitada_chave",   # coleta_html=None, ESTAGIARIO_COLETA_HABILITADA=0
    "chave_invalida",       # coleta_html=None, valor não reconhecido
    "sucesso",              # coletor real, transporte 200
    "falhas_por_fonte",     # coletor real, 500/timeout/200
    "excecao_transporte",   # exceção fora do contrato → erro
    "excecao_fabrica",      # construção do coletor falha → erro
    "excecao_coletar",      # coletar levanta → erro
    "cancelado_antes",      # sinal já ativo → nao_executada/cancelada
    "cancelado_durante",    # sinal ativado na 1ª requisição → falha/cancelado
    "fabrica_padrao",       # IntegracaoColeta com a fábrica padrão (ArmazemPaginas do ambiente)
    "porta_fake",           # porta injetada que só registra
)


@dataclass
class MontagemColeta:
    kwargs: dict[str, Any]
    ambiente: dict[str, str]
    transporte: TransporteColetaFake | None = None
    porta: PortaColetaFake | None = None


def montar_coleta(comportamento: str, diretorio: Path) -> MontagemColeta:
    """Parâmetros de ``executar_caso`` e variáveis de ambiente de um comportamento."""
    if comportamento == "porta_fake":
        porta = PortaColetaFake()
        return MontagemColeta({"coleta_html": True, "integracao_coleta": porta}, {}, porta=porta)

    sinal: threading.Event | None = None
    modo = {"falhas_por_fonte": "falhas", "excecao_transporte": "excecao"}.get(comportamento, "ok")
    if comportamento == "cancelado_antes":
        sinal = threading.Event()
        sinal.set()
    elif comportamento == "cancelado_durante":
        sinal = threading.Event()
        modo = "cancela"
    transporte = TransporteColetaFake(modo, sinal)

    ambiente: dict[str, str] = {}
    if comportamento == "excecao_fabrica":
        def fabrica():
            raise FalhaFabricaColetor("falha ao construir o coletor")
        integracao = IntegracaoColeta(fabrica_coletor=fabrica)
    elif comportamento == "excecao_coletar":
        integracao = IntegracaoColeta(fabrica_coletor=ColetorQueFalha)
    elif comportamento == "fabrica_padrao":
        ambiente["ESTAGIARIO_PAGINAS_DB_PATH"] = str(diretorio / "paginas_padrao.db")
        integracao = IntegracaoColeta(transporte=transporte, config_coleta=config_coleta(), relogio=lambda: T0)
    else:
        integracao = IntegracaoColeta(fabrica_coletor=fabrica_coletor_real(diretorio, transporte))

    if comportamento == "desabilitada_opcao":
        coleta_html: bool | None = False
    elif comportamento == "desabilitada_chave":
        coleta_html, ambiente["ESTAGIARIO_COLETA_HABILITADA"] = None, "0"
    elif comportamento == "chave_invalida":
        coleta_html, ambiente["ESTAGIARIO_COLETA_HABILITADA"] = None, "talvez"
    else:
        coleta_html = True

    kwargs: dict[str, Any] = {"coleta_html": coleta_html, "integracao_coleta": integracao}
    if sinal is not None:
        kwargs["cancel_event"] = sinal
    return MontagemColeta(kwargs, ambiente, transporte=transporte)


@contextmanager
def ambiente_temporario(valores: Mapping[str, str]) -> Iterator[None]:
    """Sobrescreve variáveis de ambiente só durante a execução comparada."""
    anteriores = {nome: os.environ.get(nome) for nome in valores}
    os.environ.update(valores)
    try:
        yield
    finally:
        for nome, valor in anteriores.items():
            if valor is None:
                os.environ.pop(nome, None)
            else:
                os.environ[nome] = valor


# --- Execução comparável ----------------------------------------------------


@dataclass
class Execucao:
    """Tudo o que uma execução de ``executar_caso`` deixa observável."""

    resultado: ResultadoCaso | None
    erro: tuple[str, str] | None
    avisos: list[str]
    eventos: list[EventoExecucao]
    pedidos: list[PedidoIntervencao]
    chamadas_llm: list[tuple[str, str]]
    chamadas_verificador: int
    chamadas_serper: int | None
    regras: list[tuple]
    chamadas_transporte_coleta: list[str] = field(default_factory=list)
    ativacoes_porta: list = field(default_factory=list)

    def avisos_sem_coleta(self) -> list[str]:
        return [m for m in self.avisos if not m.startswith(PREFIXO_AVISO)]

    def trace_sem_coleta(self) -> list[tuple]:
        """Eventos sem ``coletar_paginas`` e sem campos voláteis (timestamp, duração)."""
        return [
            (e.fase, e.nome, e.status, e.detalhes, e.entrada, e.saida, e.justificativa)
            for e in self.eventos
            if e.nome != NOME_EVENTO_TRACE
        ]


def _conteudo_rule_store(store: RuleStore | None) -> list[tuple]:
    if store is None:
        return []
    return [
        (r.id, r.categoria, r.campo, r.condicao, r.resolucao, r.grupo_exemplo_ref,
         r.criado_por, r.ativo, r.titulo, r.caso_episodico, r.sinais_busca)
        for r in store.listar_todas()
    ]


def executar(
    cenario: CenarioGrupo,
    verificador: EspecVerificador,
    memoria: EspecMemoria,
    *,
    comportamento_coleta: str,
    base: Path,
    pesquisa_web: bool = True,
    injetar_verificador: bool = True,
) -> Execucao:
    """Roda ``pipeline.executar_caso`` com fakes novos num diretório próprio.

    ``injetar_verificador=False`` passa ``verificar_web=None`` (o seletor seria
    resolvido pelo ambiente); o fake é criado mesmo assim, com contador zerado.
    """
    diretorio = Path(tempfile.mkdtemp(dir=base))
    montagem = montar_coleta(comportamento_coleta, diretorio)

    store: RuleStore | None = None
    if memoria.usar_rule_store:
        store = RuleStore(diretorio / "memoria.db")
        if memoria.regra_previa:
            semear_regra(store, cenario)
    pedir = PedirIntervencaoFake(memoria.pedir == "com_valor") if memoria.pedir else None
    llm = LLMRoteirizado(cenario)
    verificar = verificador.criar()
    trace = TraceCollector()
    avisos: list[str] = []

    resultado: ResultadoCaso | None = None
    erro: tuple[str, str] | None = None
    with ambiente_temporario(montagem.ambiente):
        try:
            resultado = executar_caso(
                cenario.search_ref,
                cenario.brand_id,
                llm=llm,
                dependencias_fk=DEPENDENCIAS_FK,
                rule_store=store,
                buscar_grupo=buscar_grupo_fake(cenario),
                on_aviso=avisos.append,
                verificar_web=verificar if injetar_verificador else None,
                pedir_intervencao=pedir,
                trace=trace,
                pesquisa_web=pesquisa_web,
                **montagem.kwargs,
            )
        except Exception as exc:  # noqa: BLE001 — comparado entre as execuções
            erro = (type(exc).__name__, str(exc))

    return Execucao(
        resultado=resultado,
        erro=erro,
        avisos=avisos,
        eventos=trace.eventos(),
        pedidos=list(pedir.pedidos) if pedir else [],
        chamadas_llm=list(llm.chamadas),
        chamadas_verificador=verificar.chamadas,
        chamadas_serper=(
            verificar.transporte.chamadas if isinstance(verificar, VerificadorSerperFake) else None
        ),
        regras=_conteudo_rule_store(store),
        chamadas_transporte_coleta=list(montagem.transporte.chamadas) if montagem.transporte else [],
        ativacoes_porta=list(montagem.porta.ativacoes) if montagem.porta else [],
    )


@st.composite
def casos_pipeline(draw):
    """``(cenario, verificador, memoria)`` para uma execução do pipeline."""
    cenario = draw(cenarios_grupo())
    return cenario, draw(especs_verificador(cenario)), draw(especs_memoria)


SETTINGS_PIPELINE = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


def _caso_com_coleta_executada():
    """Caso fixo em que a coleta chega ao Transporte_HTTP (duas fontes aceitas),
    usado como ``@example`` para garantir cobertura dos caminhos com HTTP."""
    registros = (
        fazer_registro(1, "FILTRO DE AR", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
        fazer_registro(2, "FILTRO CABINE", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
    )
    cenario = CenarioGrupo("CF1000", 1, "MANN-FILTER", registros, (("duplicata_real", (1, 2)),))
    resultados = (
        ResultadoOrganico("CF1000 FILTRO DE AR", URLS[1], "ref CF1000 MANN-FILTER", 1),
        ResultadoOrganico("CF1000 FILTRO CABINE", URLS[2], "peça original", 2),
    )
    verificador = EspecVerificador(
        "protocolo",
        resultado=ResultadoVerificacao("inconclusivo", None, "sem confirmação clara", []),
        resultados_pesquisa=resultados,
    )
    return cenario, verificador, EspecMemoria(usar_rule_store=True, regra_previa=False, pedir="com_valor")


# ===========================================================================
# Property 7: A coleta não altera decisões
# ===========================================================================
# Feature: html-extract-on-web-search, Property 7: A coleta não altera decisões
# **Validates: Requirements 2.1, 5.2, 5.3, 5.6, 12.2**


@SETTINGS_PIPELINE
@given(
    caso=casos_pipeline(),
    comportamento=st.sampled_from(COMPORTAMENTOS_COLETA),
    pesquisa_web=st.booleans(),
)
@example(caso=_caso_com_coleta_executada(), comportamento="sucesso", pesquisa_web=True)
@example(caso=_caso_com_coleta_executada(), comportamento="falhas_por_fonte", pesquisa_web=True)
@example(caso=_caso_com_coleta_executada(), comportamento="cancelado_durante", pesquisa_web=True)
@example(caso=_caso_com_coleta_executada(), comportamento="excecao_transporte", pesquisa_web=True)
def test_property7_coleta_nao_altera_decisoes(caso, comportamento, pesquisa_web, tmp_path, sem_banco):
    cenario, verificador, memoria = caso

    referencia = executar(
        cenario, verificador, memoria,
        comportamento_coleta="desabilitada_opcao", base=tmp_path, pesquisa_web=pesquisa_web,
    )
    com_coleta = executar(
        cenario, verificador, memoria,
        comportamento_coleta=comportamento, base=tmp_path, pesquisa_web=pesquisa_web,
    )

    # ResultadoCaso: particionamento, decisões (DecisaoCampo inclusas) e SQL (Req 5.3).
    assert com_coleta.erro == referencia.erro
    assert com_coleta.resultado == referencia.resultado

    # Mesmas chamadas ao Verificador_Web e ao transporte Serper (Req 2.1, 5.2).
    assert com_coleta.chamadas_verificador == referencia.chamadas_verificador
    assert com_coleta.chamadas_serper == referencia.chamadas_serper

    # Memória, pedidos de intervenção e RuleStore intocados pela coleta (Req 5.6).
    assert com_coleta.pedidos == referencia.pedidos
    assert com_coleta.regras == referencia.regras

    # Prompts ao LLM idênticos: nada da coleta chega ao modelo (Req 12.2).
    assert com_coleta.chamadas_llm == referencia.chamadas_llm

    # Mensagens e trace existentes preservados; a coleta só acrescenta linhas
    # "Coleta de páginas:" e eventos coletar_paginas.
    assert com_coleta.avisos_sem_coleta() == referencia.avisos_sem_coleta()
    assert com_coleta.trace_sem_coleta() == referencia.trace_sem_coleta()

    # Com a pesquisa desligada, nenhuma requisição HTTP a páginas acontece.
    if not pesquisa_web:
        assert com_coleta.chamadas_transporte_coleta == []



# ===========================================================================
# Property 9: As mensagens existentes são preservadas
# ===========================================================================
# Feature: html-extract-on-web-search, Property 9: As mensagens existentes são preservadas
# **Validates: Requirements 5.5**

# Mensagens que encerram a parte "web" de uma Ativacao_Pesquisa: a coleta só
# pode publicar depois de uma delas (Req 6.1), sem se intercalar às anteriores.
_FIM_VERIFICACAO = "Verificação web concluída:"
_FIM_DESLIGADA = "pesquisa web desligada pelo operador; seguindo para memória/intervenção."


def _fecha_ativacao(mensagem: str) -> bool:
    return mensagem.startswith(_FIM_VERIFICACAO) or mensagem.endswith(_FIM_DESLIGADA)


@SETTINGS_PIPELINE
@given(
    caso=casos_pipeline(),
    comportamento=st.sampled_from(COMPORTAMENTOS_COLETA),
    pesquisa_web=st.booleans(),
)
@example(caso=_caso_com_coleta_executada(), comportamento="sucesso", pesquisa_web=True)
@example(caso=_caso_com_coleta_executada(), comportamento="falhas_por_fonte", pesquisa_web=True)
@example(caso=_caso_com_coleta_executada(), comportamento="cancelado_durante", pesquisa_web=True)
@example(caso=_caso_com_coleta_executada(), comportamento="desabilitada_opcao", pesquisa_web=False)
def test_property9_mensagens_existentes_preservadas(caso, comportamento, pesquisa_web, tmp_path, sem_banco):
    cenario, verificador, memoria = caso

    # Referência sem nenhuma mensagem da coleta: porta injetada que não publica.
    sem_feature = executar(
        cenario, verificador, memoria,
        comportamento_coleta="porta_fake", base=tmp_path, pesquisa_web=pesquisa_web,
    )
    # Referência com a coleta desabilitada (Property 9, design).
    desabilitada = executar(
        cenario, verificador, memoria,
        comportamento_coleta="desabilitada_opcao", base=tmp_path, pesquisa_web=pesquisa_web,
    )
    com_coleta = executar(
        cenario, verificador, memoria,
        comportamento_coleta=comportamento, base=tmp_path, pesquisa_web=pesquisa_web,
    )

    assert not any(m.startswith(PREFIXO_AVISO) for m in sem_feature.avisos)

    # Subsequência sem "Coleta de páginas:" idêntica em conteúdo e ordem.
    assert com_coleta.avisos_sem_coleta() == desabilitada.avisos_sem_coleta()
    assert com_coleta.avisos_sem_coleta() == sem_feature.avisos

    # As mensagens da coleta só aparecem depois do fim da parte web de uma
    # ativação (e de outras mensagens da coleta), nunca no meio das existentes.
    ultima_existente: str | None = None
    blocos_coleta = 0
    anterior_coleta = False
    for mensagem in com_coleta.avisos:
        if mensagem.startswith(PREFIXO_AVISO):
            assert ultima_existente is not None and _fecha_ativacao(ultima_existente), (
                mensagem, ultima_existente,
            )
            if not anterior_coleta:
                blocos_coleta += 1
            anterior_coleta = True
        else:
            ultima_existente = mensagem
            anterior_coleta = False

    # No máximo um bloco de mensagens da coleta por Ativacao_Pesquisa.
    ativacoes = sum(1 for m in sem_feature.avisos if _fecha_ativacao(m))
    assert blocos_coleta <= ativacoes
    # Com a coleta desabilitada, cada ativação publica exatamente uma mensagem (Req 8.3).
    assert sum(1 for m in desabilitada.avisos if m.startswith(PREFIXO_AVISO)) == ativacoes



# ===========================================================================
# Property 15: Pesquisa desligada não chama a web e aplica a cascata
# ===========================================================================
# Feature: html-extract-on-web-search, Property 15: Pesquisa desligada não chama a web e aplica a cascata
# **Validates: Requirements 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 10.12, 10.13**

# Valores que o seletor rejeitaria com MetodoVerificacaoInvalidoError.
METODOS_INVALIDOS = ("bing", "invalido", "  xyz  ", "SERPERR", "playwright;serper")
_ABERTURA_PROMPT_DESLIGADA = "A pesquisa web foi desligada pelo operador nesta execução"
_SUFIXO_AVISO_DESLIGADA = (
    "pesquisa web desligada pelo operador; seguindo para memória/intervenção."
)


@dataclass
class AtivacaoObservada:
    """Uma chamada a ``_arbitrar_nome_pesquisa_desligada`` e o estado ao redor dela."""

    registros: list[RegistroCatalogPart]
    limiar: float
    pedido_esperado: PedidoIntervencao
    recuperada: Any  # IntervencaoRecuperada | None, calculada antes da chamada
    tem_store: bool
    pedir: PedirIntervencaoFake | None
    pedidos_antes: int
    pedidos_depois: int
    regras_antes: set[int]
    regras_depois: dict[int, str | None]  # id → titulo
    llm_chamadas: list[tuple[str, str]]  # chamadas feitas durante a ativação
    decisao: DecisaoCampo


def _pedido_desligada(registros: list[RegistroCatalogPart]) -> PedidoIntervencao:
    codigo, marca = registros[0].search_ref, registros[0].brand
    return PedidoIntervencao(
        ponto="nome",
        grupo_ref=f"{codigo}:{marca}",
        search_ref=codigo,
        marca=marca,
        nomes_conflitantes=sorted({r.name for r in registros}),
        motivo=MOTIVO_PESQUISA_DESLIGADA,
        contexto_web=CONTEXTO_WEB_DESLIGADA,
        membro_ids=[r.id for r in registros],
        candidatos=[(r.id, r.name) for r in registros],
    )


def _decisao_via_regra(registros: list[RegistroCatalogPart], prefixo: str) -> DecisaoCampo:
    """O que ``_decidir_nome_com_regra`` produz com o ``LLMRoteirizado`` (menor id)."""
    escolhido = min(registros, key=lambda r: r.id)
    return DecisaoCampo(
        campo="name",
        valor=escolhido.name,
        justificativa=f"{prefixo} roteiro: menor id",
        fonte="intervencao_humana",
        origem_id=escolhido.id,
    )


def _espiar_desligada(observadas: list[AtivacaoObservada]):
    original = _nome_mod._arbitrar_nome_pesquisa_desligada

    def _espiao(registros, llm, rule_store, on_aviso, pedir_intervencao,
                limiar_intervencao, trace, *, contexto_web):
        pedido = _pedido_desligada(registros)
        # consultar_intervencao é só leitura: o oráculo da memória é calculado
        # sobre o estado do RuleStore imediatamente antes da ativação.
        recuperada = (
            consultar_intervencao(pedido, rule_store, campo="nome", limiar=limiar_intervencao)
            if rule_store is not None else None
        )
        pedidos_antes = len(pedir_intervencao.pedidos) if pedir_intervencao is not None else 0
        regras_antes = {r.id for r in rule_store.listar_todas()} if rule_store is not None else set()
        llm_antes = len(llm.chamadas)
        decisao = original(
            registros, llm, rule_store, on_aviso, pedir_intervencao,
            limiar_intervencao, trace, contexto_web=contexto_web,
        )
        observadas.append(AtivacaoObservada(
            registros=list(registros),
            limiar=limiar_intervencao,
            pedido_esperado=pedido,
            recuperada=recuperada,
            tem_store=rule_store is not None,
            pedir=pedir_intervencao,
            pedidos_antes=pedidos_antes,
            pedidos_depois=len(pedir_intervencao.pedidos) if pedir_intervencao is not None else 0,
            regras_antes=regras_antes,
            regras_depois=(
                {r.id: r.titulo for r in rule_store.listar_todas()} if rule_store is not None else {}
            ),
            llm_chamadas=list(llm.chamadas[llm_antes:]),
            decisao=decisao,
        ))
        return decisao

    return _espiao


def _verificar_cascata(obs: AtivacaoObservada) -> None:
    """Oráculo da Cascata_Nome_Sem_Web para uma ativação (Req 10.6–10.9)."""
    novas_regras = {i: t for i, t in obs.regras_depois.items() if i not in obs.regras_antes}

    if obs.recuperada is not None:
        # Regra acima do limiar: decide por _decidir_nome_com_regra, sem pedir (10.7).
        assert obs.pedidos_depois == obs.pedidos_antes
        assert novas_regras == {}
        assert obs.decisao == _decisao_via_regra(
            obs.registros, f"Regra de intervenção recuperada (score {obs.recuperada.score:.3f})"
        )
        assert len(obs.llm_chamadas) == 1
        schema, prompt = obs.llm_chamadas[0]
        assert schema == "decisao_nome" and prompt.startswith(_ABERTURA_PROMPT_DESLIGADA)
        return

    if obs.pedir is not None:
        # Sem regra: pede a intervenção com o pedido de desligada (10.6, 10.8).
        assert obs.pedidos_depois == obs.pedidos_antes + 1
        assert obs.pedir.pedidos[obs.pedidos_antes] == obs.pedido_esperado
        if obs.tem_store:
            # Resposta gravada no RuleStore por registrar_resposta_intervencao.
            assert list(novas_regras.values()) == [REGRA_OPERADOR.titulo]
        if obs.pedir._com_valor:
            origem_id, valor = obs.pedido_esperado.candidatos[0]
            assert obs.decisao == DecisaoCampo(
                campo="name",
                valor=valor.strip(),
                justificativa=JUSTIFICATIVA_INTERVENCAO_DESLIGADA,
                fonte="intervencao_humana",
                origem_id=origem_id,
                confianca="alta",
                evidencias=[{"tipo": "intervencao_humana", "regra_confirmada": REGRA_OPERADOR.titulo}],
            )
            assert obs.llm_chamadas == []
        else:
            assert obs.decisao == _decisao_via_regra(obs.registros, "Regra de intervenção confirmada")
            assert len(obs.llm_chamadas) == 1
            schema, prompt = obs.llm_chamadas[0]
            assert schema == "decisao_nome" and prompt.startswith(_ABERTURA_PROMPT_DESLIGADA)
        return

    # Sem regra nem ponte humana: escalonamento exato (10.9).
    assert novas_regras == {}
    assert obs.llm_chamadas == []
    assert obs.decisao == DecisaoCampo(
        campo="name",
        valor=None,
        justificativa=JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
        fonte="escalado_humano",
        escalado_humano=True,
        confianca="baixa",
        evidencias=[{"tipo": "pesquisa_web_desligada"}],
    )


def _caso_duplicata_divergente(pedir: str | None, regra_previa: bool):
    registros = (
        fazer_registro(1, "FILTRO DE AR", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
        fazer_registro(2, "FILTRO CABINE", search_ref="CF1000", brand_id=1, brand="MANN-FILTER"),
    )
    cenario = CenarioGrupo("CF1000", 1, "MANN-FILTER", registros, (("duplicata_real", (1, 2)),))
    verificador = EspecVerificador(
        "simples", resultado=ResultadoVerificacao("confirmado", "FILTRO DE AR", "ok", [])
    )
    return cenario, verificador, EspecMemoria(usar_rule_store=True, regra_previa=regra_previa, pedir=pedir)


def _caso_recuperacao_distintos():
    registros = (
        fazer_registro(3, "PASTILHA DE FREIO", search_ref="JE4699", brand_id=2, brand="Bosch"),
        fazer_registro(4, "FILTRO CABINE", search_ref="JE4699", brand_id=2, brand="Bosch"),
    )
    cenario = CenarioGrupo(
        "JE4699", 2, "Bosch", registros,
        (("distinto_nao_classificado", (3,)), ("distinto_nao_classificado", (4,))),
    )
    verificador = EspecVerificador(
        "simples", resultado=ResultadoVerificacao("confirmado", "FILTRO CABINE", "ok", [])
    )
    return cenario, verificador, EspecMemoria(usar_rule_store=False, regra_previa=False, pedir=None)


@SETTINGS_PIPELINE
@given(
    caso=casos_pipeline(),
    metodo_invalido=st.sampled_from(METODOS_INVALIDOS),
    injetar=st.booleans(),
)
@example(caso=_caso_duplicata_divergente(None, False), metodo_invalido="bing", injetar=True)
@example(caso=_caso_duplicata_divergente("com_valor", False), metodo_invalido="bing", injetar=False)
@example(caso=_caso_duplicata_divergente("sem_valor", False), metodo_invalido="bing", injetar=True)
@example(caso=_caso_duplicata_divergente(None, True), metodo_invalido="bing", injetar=True)
@example(caso=_caso_recuperacao_distintos(), metodo_invalido="invalido", injetar=False)
def test_property15_pesquisa_desligada_nao_chama_web_e_aplica_cascata(
    caso, metodo_invalido, injetar, tmp_path, sem_banco, monkeypatch
):
    cenario, verificador, memoria = caso
    chamadas_seletor: list[tuple] = []
    observadas: list[AtivacaoObservada] = []

    def _resolver_contador(*args, **kwargs):
        chamadas_seletor.append((args, kwargs))
        return verificador.criar()

    with monkeypatch.context() as mp, ambiente_temporario(
        {"ESTAGIARIO_WEB_VERIFICATION_METODO": metodo_invalido}
    ):
        mp.setattr(_nome_mod, "resolver_verificacao_web", _resolver_contador)
        mp.setattr(_nome_mod, "_arbitrar_nome_pesquisa_desligada", _espiar_desligada(observadas))
        execucao = executar(
            cenario, verificador, memoria,
            comportamento_coleta="porta_fake", base=tmp_path,
            pesquisa_web=False, injetar_verificador=injetar,
        )

    # Nenhuma exceção (inclusive MetodoVerificacaoInvalidoError) — Req 10.5.
    event(f"ativacoes_desligada={len(observadas)}")
    assert execucao.erro is None, execucao.erro
    # Zero chamadas ao Verificador_Web e ao seletor (Req 10.3, 10.4).
    assert chamadas_seletor == []
    assert execucao.chamadas_verificador == 0
    assert execucao.chamadas_serper in (None, 0)

    # Aviso único de desligada por ativação, sem as mensagens da web (Req 10.12).
    avisos_desligada = [m for m in execucao.avisos if m.endswith(_SUFIXO_AVISO_DESLIGADA)]
    assert len(avisos_desligada) == len(observadas)
    assert not any("acionando verificação web" in m for m in execucao.avisos)
    assert not any(m.startswith("Verificação web concluída") for m in execucao.avisos)

    # Trace: um evento "desligada" por ativação e nenhum de resultado (Req 10.13).
    eventos_verif = [e for e in execucao.eventos if e.nome == "verificar_nomenclatura_peca"]
    assert len(eventos_verif) == len(observadas)
    assert all(e.status == "desligada" for e in eventos_verif)

    # A porta de coleta vê cada ativação como desligada (Req 10.14 via 10.2).
    assert len(execucao.ativacoes_porta) == len(observadas)

    for obs, aviso, evento, ativacao in zip(
        observadas, avisos_desligada, eventos_verif, execucao.ativacoes_porta
    ):
        pedido = obs.pedido_esperado
        assert aviso == (
            f"Nomes divergentes para {pedido.search_ref} ({pedido.marca}): "
            f"{pedido.nomes_conflitantes} — {_SUFIXO_AVISO_DESLIGADA}"
        )
        assert evento.fase == "tool"
        assert evento.entrada == {
            "codigo": pedido.search_ref,
            "marca": pedido.marca,
            "nomes_conflitantes": pedido.nomes_conflitantes,
        }
        assert evento.saida == {"motivo": "pesquisa_desligada"}
        assert (ativacao.metodo, ativacao.situacao, ativacao.resultados) == ("desligada", "desligada", None)
        assert (ativacao.codigo, ativacao.marca) == (pedido.search_ref, pedido.marca)
        assert list(ativacao.nomes_conflitantes) == pedido.nomes_conflitantes
        # Mesmo limiar_intervencao do pipeline (Req 10.7).
        assert obs.limiar == 0.45
        _verificar_cascata(obs)

    # Todos os pedidos de nome recebidos pela ponte humana são de desligada
    # (Req 10.6); os de similarity_id (ponto "particionamento") não mudam.
    pedidos_nome = [p for p in execucao.pedidos if p.ponto == "nome"]
    assert pedidos_nome == [
        obs.pedido_esperado for obs in observadas
        if obs.recuperada is None and obs.pedir is not None
    ]
    for pedido in pedidos_nome:
        assert pedido.motivo == MOTIVO_PESQUISA_DESLIGADA
        assert pedido.contexto_web == CONTEXTO_WEB_DESLIGADA




# ===========================================================================
# Property 16: Textos da pesquisa desligada não afirmam verificação web
# ===========================================================================
# Feature: html-extract-on-web-search, Property 16: Textos da pesquisa desligada não afirmam verificação web
# **Validates: Requirements 10.11**

# Frases do design (Property 16) e demais afirmações de verificação web que o
# caminho ligado produz (aviso, prompt da regra, justificativas e motivos).
FRASES_VERIFICACAO_WEB = (
    "acionando verificação web",
    "Verificação web concluída",
    "A verificação web não foi suficiente",
    "após verificação web inconclusiva",
    "Verificação web inconclusiva",
    "Verificação web confirmou",
    "confirmado pela verificação web",
)
# Rede de segurança para variações de caixa/flexão das mesmas afirmações.
_AFIRMA_VERIFICACAO_WEB = re.compile(
    r"(acionando|após|apos|pela)\s+(a\s+)?verifica[çc][ãa]o\s+web"
    r"|verifica[çc][ãa]o\s+web\s+(conclu[íi]da|confirmou|inconclusiva|n[ãa]o\s+foi\s+suficiente)",
    re.IGNORECASE,
)


def _afirma_verificacao_web(texto: str) -> bool:
    dobrado = texto.casefold()
    return any(frase.casefold() in dobrado for frase in FRASES_VERIFICACAO_WEB) or bool(
        _AFIRMA_VERIFICACAO_WEB.search(texto)
    )


def _textos_req_10_11(execucao: Execucao) -> list[tuple[str, str]]:
    """``(origem, texto)`` de tudo o que o Req 10.11 cobre numa execução."""
    textos: list[tuple[str, str]] = [("aviso", m) for m in execucao.avisos]

    # Justificativas de name e motivos de GrupoSinalizado no ResultadoCaso.
    if execucao.resultado is not None:
        for decisao in execucao.resultado.decisoes:
            motivo = getattr(decisao, "motivo", None)
            if isinstance(motivo, str):
                textos.append(("grupo_sinalizado", motivo))
            for dc in getattr(decisao, "decisoes_campo", ()):
                if dc.campo == "name":
                    textos.append(("decisao_name", dc.justificativa or ""))

    # A decisão de name da recuperação só aparece inteira no trace.
    for evento in execucao.eventos:
        if evento.nome in {
            "verificar_nomenclatura_peca", "recuperar_distintos_name",
            "intervencao_humana", "grupo_sinalizado",
        } or (evento.nome == "arbitrar_campo" and (evento.entrada or {}).get("campo") == "name"):
            textos.append((f"trace:{evento.nome}", evento.justificativa or ""))
            saida = evento.saida or {}
            if isinstance(saida, Mapping) and isinstance(saida.get("motivo"), str):
                textos.append((f"trace:{evento.nome}.motivo", saida["motivo"]))

    for pedido in execucao.pedidos:
        if pedido.ponto == "nome":
            textos.append(("pedido.motivo", pedido.motivo or ""))
            textos.append(("pedido.contexto_web", pedido.contexto_web or ""))

    # Prompts do juiz de nome (regra recuperada/confirmada e juiz textual).
    textos.extend(
        ("prompt_decisao_nome", prompt)
        for schema, prompt in execucao.chamadas_llm
        if schema == "decisao_nome"
    )
    return textos


def test_property16_detector_reconhece_textos_do_caminho_ligado():
    """O detector não é vácuo: marca os textos atuais do caminho com a web ligada
    e não marca os textos de desligada."""
    ligados = (
        "Nomes divergentes para CF1000 (MANN-FILTER): ['A', 'B'] — acionando verificação web antes de decidir.",
        "Verificação web concluída: status=inconclusivo, nome_sugerido=None",
        "A verificação web não foi suficiente, mas uma intervenção humana anterior ensinou a regra abaixo.",
        _nome_mod._JUSTIFICATIVA_INTERVENCAO_LIGADA,
        "Verificação web inconclusiva — sem confirmação clara das fontes.",
        "Nome confirmado pela verificação web.",
    )
    assert all(_afirma_verificacao_web(t) for t in ligados)
    desligados = (
        MOTIVO_PESQUISA_DESLIGADA,
        CONTEXTO_WEB_DESLIGADA,
        JUSTIFICATIVA_INTERVENCAO_DESLIGADA,
        JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
        _ABERTURA_PROMPT_DESLIGADA,
        f"Nomes divergentes para CF1000 (MANN-FILTER): ['A'] — {_SUFIXO_AVISO_DESLIGADA}",
    )
    assert not any(_afirma_verificacao_web(t) for t in desligados)


@SETTINGS_PIPELINE
@given(caso=casos_pipeline(), comportamento=st.sampled_from(COMPORTAMENTOS_COLETA))
@example(caso=_caso_duplicata_divergente(None, False), comportamento="desabilitada_opcao")
@example(caso=_caso_duplicata_divergente("com_valor", False), comportamento="sucesso")
@example(caso=_caso_duplicata_divergente("sem_valor", False), comportamento="porta_fake")
@example(caso=_caso_duplicata_divergente(None, True), comportamento="sucesso")
@example(caso=_caso_recuperacao_distintos(), comportamento="desabilitada_opcao")
def test_property16_textos_desligada_nao_afirmam_verificacao_web(
    caso, comportamento, tmp_path, sem_banco
):
    cenario, verificador, memoria = caso
    execucao = executar(
        cenario, verificador, memoria,
        comportamento_coleta=comportamento, base=tmp_path, pesquisa_web=False,
    )
    assert execucao.erro is None, execucao.erro
    # Os textos de verificadores fakes nunca entram: o verificador não é chamado.
    assert execucao.chamadas_verificador == 0

    textos = _textos_req_10_11(execucao)
    ativacoes = sum(1 for m in execucao.avisos if m.endswith(_SUFIXO_AVISO_DESLIGADA))
    event(f"ativacoes_desligada={ativacoes}")

    violacoes = [(origem, texto) for origem, texto in textos if _afirma_verificacao_web(texto)]
    assert violacoes == []




# ===========================================================================
# Property 17: Etapas fora da web não mudam com a pesquisa desligada
# ===========================================================================
# Feature: html-extract-on-web-search, Property 17: Etapas fora da web não mudam com a pesquisa desligada
# **Validates: Requirements 10.15**
#
# Compara, para o mesmo caso, ``pesquisa_web=True`` e ``pesquisa_web=False``.
# O que pode mudar de propósito (Req 10) é só a DecisaoCampo de ``name`` das
# ativações que passam por ``_arbitrar_nome_via_web``. Consequências admitidas:
#
# - na recuperação de ``distinto_nao_classificado``, a decisão de nome decide
#   entre merge e GrupoSinalizado; quando os dois lados divergem nessa escolha,
#   o merge recuperado (e seus campos, eventos e chamadas ao provedor) só existe
#   de um lado e fica fora da comparação; quando os dois fazem merge, os campos
#   diferentes de ``name`` são comparados; quando os dois sinalizam, compara-se
#   ``membro_ids`` (o motivo inclui a justificativa de nome);
# - intervenções de nome gravam regras no RuleStore em quantidades diferentes,
#   então o ``id`` autoincremento citado em "Intervenção salva: regra=..., id=N."
#   de um GrupoSinalizado de similarity_id é normalizado. As regras de nome
#   (``campo="nome"``) não entram na consulta de particionamento nem na de
#   campos, então o restante do motivo tem de ser idêntico.
#
# SQL: o ``ResultadoCaso.sql`` real mistura o ``name`` decidido no UPDATE do
# vencedor, então não dá para recortar "o bloco de name" com segurança. A
# comparação usa ``gerar_sql`` sobre as decisões normalizadas (sem a
# DecisaoCampo de ``name``) com dependências FK não vazias, o que exercita a
# realocação de FKs, o UPDATE diff-only dos demais campos e o DELETE dos
# perdedores. Quando nenhuma ativação passa pela web, o ResultadoCaso inteiro
# (inclusive o SQL real) tem de ser idêntico.

FK_PROPERTY17 = [
    FkDependency("catalog_part_image", "part_id", "fk_catalog_part_image_part"),
    FkDependency("catalog_part_stock", "catalog_part_id", "fk_catalog_part_stock_part"),
]
_ID_REGRA_SALVA = re.compile(r"(Intervenção salva: regra=.*?, id=)\d+")
_EVENTOS_FORA_DA_WEB = frozenset({"buscar_grupo", "particionar_grupo", "arbitrar_por_provedor"})
COMPORTAMENTOS_PROPERTY17 = ("desabilitada_opcao", "porta_fake", "sucesso")


@dataclass
class ExecucaoEspionada:
    execucao: Execucao
    via_web: list[frozenset[int]]           # ids de cada chamada a _arbitrar_nome_via_web
    provedor: list[tuple[str, frozenset[int], int, Any]]  # (campo, ids, brand_id, desfecho)


def _normalizar_motivo(motivo: str) -> str:
    return _ID_REGRA_SALVA.sub(r"\1<id>", motivo)


def _executar_espionado(
    cenario, verificador, memoria, *, pesquisa_web: bool, comportamento: str, base: Path, monkeypatch,
) -> ExecucaoEspionada:
    via_web: list[frozenset[int]] = []
    provedor: list[tuple[str, frozenset[int], int, Any]] = []

    def _provedor_espiao(registros, campo, brand_id):
        ids = frozenset(r.id for r in registros)
        try:
            decisao = _arbitrar_por_provedor_real(registros, campo, brand_id)
        except Exception as exc:
            provedor.append((campo, ids, brand_id, ("erro", type(exc).__name__)))
            raise
        provedor.append((campo, ids, brand_id, decisao))
        return decisao

    via_web_original = _nome_mod._arbitrar_nome_via_web
    nome_original = _nome_mod._arbitrar_nome

    def _via_web_espiao(registros, *args, **kwargs):
        via_web.append(frozenset(r.id for r in registros))
        return via_web_original(registros, *args, **kwargs)

    def _nome_espiao(*args, **kwargs):
        # arbitrar_campo não repassa arbitrar_por_provedor; o padrão de
        # _arbitrar_nome foi ligado na definição, então é injetado aqui.
        kwargs.setdefault("arbitrar_por_provedor", _provedor_espiao)
        return nome_original(*args, **kwargs)

    with monkeypatch.context() as mp:
        mp.setattr(_nome_mod, "_arbitrar_nome_via_web", _via_web_espiao)
        mp.setattr(_nome_mod, "_arbitrar_nome", _nome_espiao)
        mp.setattr(_arbitrar_mod, "arbitrar_por_provedor", _provedor_espiao)
        mp.setattr(_pipeline_mod, "arbitrar_por_provedor", _provedor_espiao)
        execucao = executar(
            cenario, verificador, memoria,
            comportamento_coleta=comportamento, base=base, pesquisa_web=pesquisa_web,
        )
    return ExecucaoEspionada(execucao, via_web, provedor)


def _ids_decisao(decisao: DecisaoMerge | GrupoSinalizado) -> frozenset[int]:
    if isinstance(decisao, DecisaoMerge):
        return frozenset([decisao.vencedor_id, *decisao.perdedor_ids])
    return frozenset(decisao.membro_ids)


def _sem_name(decisao: DecisaoMerge) -> DecisaoMerge:
    return replace(decisao, decisoes_campo=[dc for dc in decisao.decisoes_campo if dc.campo != "name"])


def _distintos(resultado: ResultadoCaso) -> frozenset[int]:
    return frozenset(
        i for s in resultado.particao.subclusters if s.label == "distinto_nao_classificado"
        for i in s.membro_ids
    )


def _duplicatas_comparaveis(resultado: ResultadoCaso, via_web: set[frozenset[int]]) -> list:
    """Decisões dos subclusters duplicata_real, com ``name`` removido só onde a
    ativação passou pela web e o id de regra normalizado nos sinalizados."""
    distintos = _distintos(resultado)
    saida = []
    for decisao in resultado.decisoes:
        ids = _ids_decisao(decisao)
        if len(distintos) >= 2 and ids == distintos:
            continue  # decisão da recuperação: tratada à parte
        if isinstance(decisao, DecisaoMerge):
            saida.append(_sem_name(decisao) if ids in via_web else decisao)
        else:
            saida.append(replace(decisao, motivo=_normalizar_motivo(decisao.motivo)))
    return saida


def _recuperacao(resultado: ResultadoCaso) -> DecisaoMerge | GrupoSinalizado | None:
    distintos = _distintos(resultado)
    if len(distintos) < 2:
        return None
    return next((d for d in resultado.decisoes if _ids_decisao(d) == distintos), None)


def _trace_fora_da_web(execucao: Execucao, distintos: frozenset[int], incluir_recuperada: bool) -> list[tuple]:
    """Eventos das etapas que não dependem da pesquisa web, sem campos voláteis."""
    saida = []
    for e in execucao.eventos:
        entrada = e.entrada if isinstance(e.entrada, Mapping) else {}
        chave = (e.fase, e.nome, e.status, e.detalhes, e.entrada, e.saida, e.justificativa)
        if e.nome in _EVENTOS_FORA_DA_WEB:
            saida.append(chave)
        elif e.nome == "consultar_memoria" and entrada.get("ponto") == "particionamento":
            saida.append(chave)
        elif e.nome == "arbitrar_campo" and entrada.get("campo") != "name":
            ids = frozenset(entrada.get("subcluster_ids") or ())
            if ids != distintos or incluir_recuperada:
                saida.append(chave)
        elif e.nome == "grupo_sinalizado":
            membros = frozenset(((e.saida or {}) if isinstance(e.saida, Mapping) else {}).get("membro_ids") or ())
            if membros != distintos:
                saida_norm = dict(e.saida)
                saida_norm["motivo"] = _normalizar_motivo(saida_norm.get("motivo") or "")
                saida.append((e.fase, e.nome, e.status, e.detalhes, e.entrada, saida_norm,
                              _normalizar_motivo(e.justificativa or "")))
    return saida


def _caso_misto_similarity():
    """Nome divergente num duplicata_real, conflito de similarity_id noutro e
    dois distintos: exercita a normalização do id de regra e a recuperação."""
    kw = {"search_ref": "CF1000", "brand_id": 1, "brand": "MANN-FILTER"}
    registros = (
        fazer_registro(1, "FILTRO DE AR", width=10.0, **kw),
        fazer_registro(2, "FILTRO CABINE", width=12.5, **kw),
        fazer_registro(3, "POLIA", similarity_id=5, **kw),
        fazer_registro(4, "POLIA DA CORREIA", similarity_id=6, **kw),
        fazer_registro(7, "PASTILHA DE FREIO", **kw),
        fazer_registro(8, "ELEMENTO FILTRANTE AR", **kw),
    )
    cenario = CenarioGrupo(
        "CF1000", 1, "MANN-FILTER", registros,
        (("duplicata_real", (1, 2)), ("duplicata_real", (3, 4)),
         ("distinto_nao_classificado", (7,)), ("distinto_nao_classificado", (8,))),
    )
    verificador = EspecVerificador(
        "simples", resultado=ResultadoVerificacao("confirmado", "FILTRO DE AR", "ok", [])
    )
    return cenario, verificador, EspecMemoria(usar_rule_store=True, regra_previa=False, pedir="com_valor")


@SETTINGS_PIPELINE
@given(caso=casos_pipeline(), comportamento=st.sampled_from(COMPORTAMENTOS_PROPERTY17))
@example(caso=_caso_misto_similarity(), comportamento="desabilitada_opcao")
@example(caso=_caso_duplicata_divergente("com_valor", False), comportamento="porta_fake")
@example(caso=_caso_duplicata_divergente(None, True), comportamento="sucesso")
@example(caso=_caso_recuperacao_distintos(), comportamento="desabilitada_opcao")
@example(caso=_caso_com_coleta_executada(), comportamento="sucesso")
def test_property17_etapas_fora_da_web_nao_mudam(caso, comportamento, tmp_path, sem_banco, monkeypatch):
    cenario, verificador, memoria = caso
    ligada = _executar_espionado(
        cenario, verificador, memoria, pesquisa_web=True,
        comportamento=comportamento, base=tmp_path, monkeypatch=monkeypatch,
    )
    desligada = _executar_espionado(
        cenario, verificador, memoria, pesquisa_web=False,
        comportamento=comportamento, base=tmp_path, monkeypatch=monkeypatch,
    )
    assert ligada.execucao.erro is None, ligada.execucao.erro
    assert desligada.execucao.erro is None, desligada.execucao.erro
    r_lig, r_des = ligada.execucao.resultado, desligada.execucao.resultado

    # Particionamento inteiro (subclusters, sinais e regras aplicáveis) e grupo.
    assert r_des.particao == r_lig.particao
    assert r_des.grupo == r_lig.grupo
    assert r_des.grupo_ref == r_lig.grupo_ref

    # As ativações que chegam a _arbitrar_nome_via_web dependem só do provedor e
    # da divergência de nomes, não da pesquisa web.
    assert desligada.via_web == ligada.via_web
    via_web = set(ligada.via_web)
    event(f"ativacoes_web={len(via_web)}")

    # Sem nenhuma ativação web, nada pode mudar — inclusive o SQL real.
    if not via_web:
        assert r_des == r_lig

    # Subclusters duplicata_real: vencedor, perdedores, valores atuais do
    # vencedor, DecisaoCampo de todos os campos exceto name (e de name quando não
    # passou pela web) e GrupoSinalizado de similarity_id.
    dup_lig = _duplicatas_comparaveis(r_lig, via_web)
    dup_des = _duplicatas_comparaveis(r_des, via_web)
    assert dup_des == dup_lig

    # Recuperação de distinto_nao_classificado.
    distintos = _distintos(r_lig)
    rec_lig, rec_des = _recuperacao(r_lig), _recuperacao(r_des)
    rec_comparaveis: list[tuple[Any, Any]] = []
    incluir_recuperada = False
    if len(distintos) >= 2:
        if distintos not in via_web:
            # Provedor autorizou: nome sem web, decisão inteira igual.
            assert rec_des == rec_lig
            rec_comparaveis.append((rec_lig, rec_des))
            incluir_recuperada = True
        elif isinstance(rec_lig, DecisaoMerge) and isinstance(rec_des, DecisaoMerge):
            assert _sem_name(rec_des) == _sem_name(rec_lig)
            rec_comparaveis.append((_sem_name(rec_lig), _sem_name(rec_des)))
            incluir_recuperada = True
        elif isinstance(rec_lig, GrupoSinalizado) and isinstance(rec_des, GrupoSinalizado):
            assert rec_des.membro_ids == rec_lig.membro_ids
        else:
            event("recuperacao_merge_vs_sinalizado")

    # SQL fora do bloco de name: gerar_sql sobre as decisões normalizadas, com
    # dependências FK reais de exemplo (realocação de FKs, UPDATE diff-only, DELETE).
    decisoes_sql_lig = dup_lig + [a for a, _ in rec_comparaveis if isinstance(a, DecisaoMerge)]
    decisoes_sql_des = dup_des + [b for _, b in rec_comparaveis if isinstance(b, DecisaoMerge)]
    assert gerar_sql(decisoes_sql_des, FK_PROPERTY17) == gerar_sql(decisoes_sql_lig, FK_PROPERTY17)

    # Chamadas a arbitrar_por_provedor (name e demais campos), em ordem e com o
    # mesmo desfecho; as do merge recuperado só contam quando ele existe dos dois lados.
    def _provedor_comparavel(chamadas):
        return [
            c for c in chamadas
            if incluir_recuperada or not (len(distintos) >= 2 and c[1] == distintos and c[0] != "name")
        ]
    assert _provedor_comparavel(desligada.provedor) == _provedor_comparavel(ligada.provedor)

    # Pedidos de intervenção de similarity_id (ponto "particionamento") iguais.
    assert [p for p in desligada.execucao.pedidos if p.ponto == "particionamento"] == [
        p for p in ligada.execucao.pedidos if p.ponto == "particionamento"
    ]

    # Eventos de trace das etapas que não dependem da web.
    assert _trace_fora_da_web(desligada.execucao, distintos, incluir_recuperada) == _trace_fora_da_web(
        ligada.execucao, distintos, incluir_recuperada
    )
