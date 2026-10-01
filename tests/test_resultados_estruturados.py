"""Testes unitários de `invocar_verificador` e caracterização da marca genérica
na Skill_Serper (spec html-extract-on-web-search, tarefa 3.4).

Req 1.3: pesquisa realizada com lista vazia entrega tupla vazia (`realizada`).
Req 4.2: verificador sem o protocolo vira `sem_estruturados`, sem resultados.
Req 3.3: com marca genérica, a Skill_Serper continua pesquisando e decidindo
exatamente como antes; a Trava_Entrada só afeta a coleta.

Sem rede (guarda de rede), sem LLM real e sem API Serper real: `ClienteSerper`
usa um transporte fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from verification import selector, serper_agent
from verification.models import FonteWeb, ResultadoVerificacao
from verification.resultados_estruturados import (
    ChamadaVerificacao,
    VerificacaoComResultados,
    VerificadorComResultados,
    invocar_verificador,
)
from verification.serper_client import ClienteSerper, ResultadoOrganico
from verification.serper_decisao import marca_generica

_RESULTADO = ResultadoVerificacao(
    status="inconclusivo", nome_sugerido=None, justificativa="fake", fontes=[]
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _VerificadorSimples:
    """Contrato simples (sem `verificar_com_resultados`), como Playwright/fakes."""

    resultado: ResultadoVerificacao = _RESULTADO
    erro: Exception | None = None
    chamadas: list[tuple] = field(default_factory=list)

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        self.chamadas.append((codigo, marca, list(nomes_conflitantes), on_evento))
        if on_evento is not None:
            on_evento("simples: mensagem")
        if self.erro is not None:
            raise self.erro
        return self.resultado


@dataclass
class _VerificadorProtocolo:
    """Implementa `VerificadorComResultados`; `__call__` nunca deve ser usado."""

    resultados: tuple[ResultadoOrganico, ...] | list | None = ()
    resultado: ResultadoVerificacao = _RESULTADO
    erro: Exception | None = None
    chamadas: list[tuple] = field(default_factory=list)

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        raise AssertionError("contrato simples não deve ser usado com o protocolo")

    def verificar_com_resultados(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        self.chamadas.append((codigo, marca, list(nomes_conflitantes), on_evento))
        if self.erro is not None:
            raise self.erro
        return VerificacaoComResultados(self.resultado, self.resultados)


class _ErroVerificador(RuntimeError):
    pass


@dataclass
class _TransporteSerperFake:
    """Transporte do `ClienteSerper`: registra cada requisição e devolve `resposta`."""

    resposta: dict
    requisicoes: list[dict] = field(default_factory=list)

    def __call__(self, url, dados, cabecalhos, timeout):
        self.requisicoes.append({"url": url, "corpo": json.loads(dados.decode("utf-8"))})
        return json.loads(json.dumps(self.resposta))


@dataclass
class _LLMFake:
    resposta: dict
    chamadas: list[dict] = field(default_factory=list)

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append({"system": system, "user": user, "schema_name": schema_name})
        return dict(self.resposta)


def _org(title, snippet, link, position):
    return {"title": title, "snippet": snippet, "link": link, "position": position}


# ---------------------------------------------------------------------------
# invocar_verificador
# ---------------------------------------------------------------------------


def test_verificador_simples_vira_sem_estruturados():
    verificador = _VerificadorSimples()
    eventos: list[str] = []

    chamada = invocar_verificador(
        verificador, "CF1000", "MANN-FILTER", ["FILTRO AR", "FILTRO DE AR"], on_evento=eventos.append
    )

    assert not isinstance(verificador, VerificadorComResultados)
    assert chamada == ChamadaVerificacao(_RESULTADO, "sem_estruturados", None)
    assert chamada.resultado is verificador.resultado
    assert verificador.chamadas == [
        ("CF1000", "MANN-FILTER", ["FILTRO AR", "FILTRO DE AR"], eventos.append)
    ]
    assert eventos == ["simples: mensagem"]


def test_funcao_simples_vira_sem_estruturados():
    chamadas = []

    def verificar(codigo, marca, nomes, *, on_evento=None):
        chamadas.append((codigo, marca, nomes))
        return _RESULTADO

    chamada = invocar_verificador(verificar, "X1", "BOSCH", ["A"], on_evento=None)

    assert chamada.situacao == "sem_estruturados"
    assert chamada.resultados_pesquisa is None
    assert chamadas == [("X1", "BOSCH", ["A"])]


def test_protocolo_com_lista_vazia_vira_realizada_com_tupla_vazia():
    verificador = _VerificadorProtocolo(resultados=())

    chamada = invocar_verificador(verificador, "CF1000", "MANN", ["FILTRO"], on_evento=None)

    assert isinstance(verificador, VerificadorComResultados)
    assert chamada.situacao == "realizada"
    assert chamada.resultados_pesquisa == ()
    assert isinstance(chamada.resultados_pesquisa, tuple)
    assert chamada.resultado is _RESULTADO
    assert len(verificador.chamadas) == 1


def test_protocolo_com_resultados_preserva_ordem_em_tupla():
    orgs = [
        ResultadoOrganico(title="b", link="https://b", snippet="", position=2),
        ResultadoOrganico(title="a", link="https://a", snippet="", position=1),
    ]
    verificador = _VerificadorProtocolo(resultados=orgs)

    chamada = invocar_verificador(verificador, "C", "M", ["N"], on_evento=None)

    assert chamada.situacao == "realizada"
    assert chamada.resultados_pesquisa == tuple(orgs)


def test_protocolo_sem_pesquisa_vira_nao_realizada():
    chamada = invocar_verificador(
        _VerificadorProtocolo(resultados=None), "C", "M", ["N"], on_evento=None
    )

    assert chamada == ChamadaVerificacao(_RESULTADO, "nao_realizada", None)


@pytest.mark.parametrize(
    "verificador",
    [
        _VerificadorSimples(erro=_ErroVerificador("falhou")),
        _VerificadorProtocolo(erro=_ErroVerificador("falhou")),
    ],
    ids=["simples", "protocolo"],
)
def test_excecao_do_verificador_atravessa(verificador):
    with pytest.raises(_ErroVerificador, match="falhou"):
        invocar_verificador(verificador, "C", "M", ["N"], on_evento=None)
    assert len(verificador.chamadas) == 1


def test_verificador_serper_padrao_com_lista_vazia_vira_realizada(monkeypatch):
    """Caminho real: `VerificadorSerperPadrao` + `ClienteSerper` com transporte fake."""
    transporte = _TransporteSerperFake({"organic": []})
    monkeypatch.setattr(
        serper_agent, "ClienteSerper", lambda: ClienteSerper(api_key="k", transporte=transporte)
    )
    monkeypatch.setattr(selector, "_resolver_llm_serper", lambda on_evento: None)
    eventos: list[str] = []

    chamada = invocar_verificador(
        selector.VerificadorSerperPadrao(), "CF1000", "MANN", ["FILTRO AR"], on_evento=eventos.append
    )

    assert chamada.situacao == "realizada"
    assert chamada.resultados_pesquisa == ()
    assert chamada.resultado.status == "inconclusivo"
    assert len(transporte.requisicoes) == 1
    assert eventos == ["Serper: 0 resultados orgânicos retornados pela API."]


# ---------------------------------------------------------------------------
# Caracterização: marca genérica mantém pesquisa e decisão Serper (Req 3.3)
# ---------------------------------------------------------------------------

_CODIGO = "CF1000"
_CANDIDATOS = ["FILTRO AR", "FILTRO DE AR MOTOR"]
_RESPOSTA_SERPER = {
    "organic": [
        _org("Kit Embreagem Completo", "sem código aqui", "https://x.example/kit", 1),
        _org("Filtro de Ar CF1000", "Filtro de Ar CF1000 para motor", "https://y.example/cf1000", 2),
    ]
}
_AVALIACAO = {
    "justificativa": "título traz Filtro de Ar CF1000",
    "idioma_origem": "pt",
    "nome_extraido": "Filtro de Ar",
    "nome_pt": "Filtro de Ar",
    "candidato_relacionado": "FILTRO AR",
    "nome_especifico_coerente": True,
}


def _rodar_serper(marca: str, *, com_on_resultados: bool):
    transporte = _TransporteSerperFake(_RESPOSTA_SERPER)
    llm = _LLMFake(_AVALIACAO)
    eventos: list[str] = []
    capturados: list[tuple[ResultadoOrganico, ...]] = []
    kwargs = {"on_resultados": capturados.append} if com_on_resultados else {}
    resultado = serper_agent.verificar_nomenclatura_peca_serper(
        _CODIGO,
        marca,
        list(_CANDIDATOS),
        on_evento=eventos.append,
        llm=llm,
        cliente=ClienteSerper(api_key="k", transporte=transporte),
        **kwargs,
    )
    return resultado, eventos, transporte, llm, capturados


@pytest.mark.parametrize("marca", ["CONVERSÃO", "OEM", "ORIGINAL OEM"])
def test_marca_generica_mantem_pesquisa_e_decisao_serper(marca):
    assert marca_generica(marca)

    base, eventos_base, transp_base, llm_base, _ = _rodar_serper(marca, com_on_resultados=False)
    novo, eventos_novo, transp_novo, llm_novo, capturados = _rodar_serper(
        marca, com_on_resultados=True
    )

    # A requisição Serper acontece (uma vez), com a marca na consulta como antes.
    assert len(transp_base.requisicoes) == len(transp_novo.requisicoes) == 1
    assert transp_novo.requisicoes == transp_base.requisicoes
    assert transp_novo.requisicoes[0]["corpo"]["q"] == f"{_CODIGO} {marca}"

    # Decisão, mensagens e prompts idênticos com e sem on_resultados.
    assert novo == base
    assert eventos_novo == eventos_base
    assert llm_novo.chamadas == llm_base.chamadas
    assert f"Serper: marca genérica {marca!r} ignorada; decisão por código + nome." in eventos_base

    # A decisão continua a de antes: confirmado pelo resultado com código explícito.
    assert base == ResultadoVerificacao(
        status="confirmado",
        nome_sugerido="Filtro de Ar",
        justificativa=base.justificativa,
        fontes=[FonteWeb(url="https://y.example/cf1000", nome_encontrado="Filtro de Ar")],
    )
    assert "codigo_explicito(posicao=2)" in base.justificativa
    assert "marca_reforco" not in base.justificativa
    # Marca genérica fica fora do payload do sub-agente, como antes.
    assert len(llm_base.chamadas) == 1
    assert "Marca: não informada" in llm_base.chamadas[0]["user"]

    # Os Resultados_Pesquisa são entregues uma vez, na ordem da API.
    assert capturados == [
        tuple(
            ResultadoOrganico(
                title=o["title"], link=o["link"], snippet=o["snippet"], position=o["position"]
            )
            for o in _RESPOSTA_SERPER["organic"]
        )
    ]


@pytest.mark.parametrize("marca", ["CONVERSÃO", "OEM", "ORIGINAL OEM"])
def test_marca_generica_via_verificador_padrao_entrega_resultados(monkeypatch, marca):
    """Pelo seletor, a marca genérica ainda vira `realizada` com os resultados da
    pesquisa; a trava de entrada é aplicada só depois, pela coleta."""
    transporte = _TransporteSerperFake(_RESPOSTA_SERPER)
    llm = _LLMFake(_AVALIACAO)
    monkeypatch.setattr(
        serper_agent, "ClienteSerper", lambda: ClienteSerper(api_key="k", transporte=transporte)
    )
    monkeypatch.setattr(selector, "_resolver_llm_serper", lambda on_evento: llm)
    verificador = selector.VerificadorSerperPadrao()

    eventos_simples: list[str] = []
    simples = verificador(_CODIGO, marca, list(_CANDIDATOS), on_evento=eventos_simples.append)
    eventos: list[str] = []
    chamada = invocar_verificador(
        verificador, _CODIGO, marca, list(_CANDIDATOS), on_evento=eventos.append
    )

    assert chamada.situacao == "realizada"
    assert len(chamada.resultados_pesquisa) == 2
    assert chamada.resultado == simples
    assert chamada.resultado.status == "confirmado"
    assert eventos == eventos_simples
    assert len(transporte.requisicoes) == 2  # uma por chamada, nenhuma extra
