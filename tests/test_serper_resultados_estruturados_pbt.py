# Feature: html-extract-on-web-search, Property 1: A Skill_Serper entrega os resultados sem mudar nada
"""Property 1 (html-extract-on-web-search): `VerificadorSerperPadrao.verificar_com_resultados`
produz o mesmo `ResultadoVerificacao`, as mesmas mensagens `on_evento` e o mesmo
número de chamadas ao transporte que `__call__`, e entrega exatamente os
orgânicos extraídos quando `ClienteSerper.buscar` devolveu uma lista (None nos
demais casos).

Tudo roda sobre fakes: `ClienteSerper` real com transporte fake (sem rede), LLM
fake ou ausente. Nenhuma chave real é usada.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from verification import selector, serper_agent
from verification.resultados_estruturados import VerificacaoComResultados
from verification.selector import VerificadorSerperPadrao
from verification.serper_client import (
    ClienteSerper,
    SerperRequisicaoError,
    extrair_organicos,
)

# Chave sintética mais longa que "k": com "k" o guard de vazamento de
# `_confirmado` (substring da chave na justificativa) rebaixaria toda confirmação
# a inconclusivo, e o ramo `confirmado` ficaria sem cobertura.
_API_KEY_FAKE = "chave-fake-serper-teste"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _TransporteFake:
    """Transporte do ClienteSerper: devolve um corpo JSON fixo ou levanta erro."""

    corpo: dict
    erro: Exception | None = None
    chamadas: int = 0

    def __call__(self, url, dados, cabecalhos, timeout):
        self.chamadas += 1
        if self.erro is not None:
            raise self.erro
        return json.loads(json.dumps(self.corpo))  # cópia profunda


@dataclass
class _LLMFake:
    """LLMProvider fake determinístico: mesma resposta para toda chamada."""

    avaliacao: dict
    traducao: dict
    falhar: bool = False
    chamadas: list = field(default_factory=list)

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append(schema_name)
        if self.falhar:
            raise RuntimeError("falha simulada do LLM")
        if schema_name == "traducao_nome_pt":
            return dict(self.traducao)
        return dict(self.avaliacao)


# ---------------------------------------------------------------------------
# Estratégias
# ---------------------------------------------------------------------------

_palavra = st.sampled_from(
    ["filtro", "ar", "oleo", "pastilha", "freio", "vela", "bomba", "air", "filter", "Luftfilter"]
)
_codigo = st.sampled_from(["", "   ", "CF1000", "JE4699", "83061"])
_marca = st.sampled_from(["", " \t", "MANN-FILTER", "bosch", "OEM", "Conversão", "original oem"])
_candidatos = st.lists(
    st.one_of(st.sampled_from(["", "  "]), st.lists(_palavra, min_size=1, max_size=3).map(" ".join)),
    max_size=3,
)


@st.composite
def _item_organico(draw, codigo: str):
    prefixo = draw(st.sampled_from(["", f"{codigo.strip()} ", "XYZ "]))
    titulo = prefixo + " ".join(draw(st.lists(_palavra, min_size=0, max_size=3)))
    item: dict = {
        "title": titulo,
        "link": draw(
            st.sampled_from(
                ["https://produto.mercadolivre.com.br/x", "https://loja.exemplo.com/p", "", "not-a-url"]
            )
        ),
        "snippet": draw(st.sampled_from(["", "peça original", f"ref {codigo.strip()}"])),
    }
    posicao = draw(st.one_of(st.none(), st.integers(min_value=1, max_value=10), st.just("3")))
    if posicao is not None:
        item["position"] = posicao
    if draw(st.booleans()):
        item.pop("snippet")
    return item


@st.composite
def _cenario(draw):
    codigo = draw(_codigo)
    marca = draw(_marca)
    candidatos = draw(_candidatos)
    chave_presente = draw(st.booleans())
    modo_transporte = draw(st.sampled_from(["lista", "vazia", "sem_organic", "erro"]))
    if modo_transporte == "lista":
        corpo = {"organic": draw(st.lists(_item_organico(codigo), min_size=1, max_size=5))}
    elif modo_transporte == "vazia":
        corpo = {"organic": []}
    else:
        corpo = {}
    erro = SerperRequisicaoError("falha simulada") if modo_transporte == "erro" else None

    modo_llm = draw(st.sampled_from(["fake", "indisponivel", "falha"]))
    avaliacao = {
        "justificativa": "decisão fake",
        "nome_extraido": draw(st.one_of(st.none(), _palavra, st.lists(_palavra, min_size=2, max_size=3).map(" ".join))),
        "candidato_relacionado": draw(
            st.one_of(st.none(), st.sampled_from([c for c in candidatos if c.strip()] or ["inexistente"]))
        ),
        "nome_especifico_coerente": draw(st.booleans()),
    }
    if draw(st.booleans()):
        avaliacao["idioma_origem"] = draw(st.sampled_from(["pt", "en", "de"]))
        avaliacao["nome_pt"] = draw(st.one_of(st.none(), _palavra))
    traducao = {"nome_pt": draw(st.one_of(st.none(), _palavra))}
    return {
        "codigo": codigo,
        "marca": marca,
        "candidatos": candidatos,
        "chave_presente": chave_presente,
        "corpo": corpo,
        "erro": erro,
        "modo_llm": modo_llm,
        "avaliacao": avaliacao,
        "traducao": traducao,
    }


# ---------------------------------------------------------------------------
# Execução de um cenário por um dos dois caminhos
# ---------------------------------------------------------------------------


def _executar(cenario: dict, *, com_resultados: bool):
    """Roda o cenário num ambiente isolado e devolve (retorno, eventos, transporte, llm)."""
    transporte = _TransporteFake(corpo=cenario["corpo"], erro=cenario["erro"])
    llm = _LLMFake(
        avaliacao=cenario["avaliacao"],
        traducao=cenario["traducao"],
        falhar=cenario["modo_llm"] == "falha",
    )

    def fabrica_cliente(*args, **kwargs):
        if cenario["chave_presente"]:
            return ClienteSerper(api_key=_API_KEY_FAKE, timeout=5, transporte=transporte)
        # Sem chave: o ClienteSerper real lê SERPER_API_KEY (removida) e levanta
        # SerperAPIKeyAusenteError antes de qualquer requisição.
        return ClienteSerper(transporte=transporte)

    def llm_padrao():
        if cenario["modo_llm"] == "indisponivel":
            raise RuntimeError("LLM indisponível")
        return llm

    eventos: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("SERPER_API_KEY", raising=False)
        mp.setattr(serper_agent, "ClienteSerper", fabrica_cliente)
        mp.setattr(selector, "_llm_serper_padrao", llm_padrao)
        verificador = VerificadorSerperPadrao()
        args = (cenario["codigo"], cenario["marca"], list(cenario["candidatos"]))
        if com_resultados:
            retorno = verificador.verificar_com_resultados(*args, on_evento=eventos.append)
        else:
            retorno = verificador(*args, on_evento=eventos.append)
    return retorno, eventos, transporte, llm


def _pesquisa_realizada_esperada(cenario: dict) -> bool:
    entrada_ok = (
        cenario["codigo"].strip() != ""
        and cenario["marca"].strip() != ""
        and any(c and c.strip() for c in cenario["candidatos"])
    )
    return entrada_ok and cenario["chave_presente"] and cenario["erro"] is None


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


# Feature: html-extract-on-web-search, Property 1: A Skill_Serper entrega os resultados sem mudar nada
@settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(cenario=_cenario())
def test_property_1_serper_entrega_resultados_sem_mudar_nada(cenario):
    """**Property 1: A Skill_Serper entrega os resultados sem mudar nada**

    **Validates: Requirements 1.6, 2.1, 4.3**
    """
    simples, eventos_simples, transp_simples, llm_simples = _executar(cenario, com_resultados=False)
    estruturado, eventos_estr, transp_estr, llm_estr = _executar(cenario, com_resultados=True)

    assert isinstance(estruturado, VerificacaoComResultados)
    # Mesmo ResultadoVerificacao, campo a campo (dataclasses frozen).
    assert estruturado.resultado == simples
    # Mesmas mensagens on_evento, na mesma ordem.
    assert eventos_estr == eventos_simples
    # Mesmo número de requisições ao transporte Serper, no máximo 1 (Req 2.1).
    assert transp_estr.chamadas == transp_simples.chamadas <= 1
    # Mesmo roteiro de chamadas ao LLM.
    assert llm_estr.chamadas == llm_simples.chamadas

    if _pesquisa_realizada_esperada(cenario):
        assert transp_estr.chamadas == 1
        esperados = tuple(extrair_organicos(cenario["corpo"]))
        assert estruturado.resultados_pesquisa == esperados
    else:
        # Sem Pesquisa_Realizada: entrada em branco, chave ausente ou falha de requisição.
        assert estruturado.resultados_pesquisa is None


def test_exemplo_confirmado_entrega_organicos():
    """Exemplo âncora: o ramo confirmado também entrega os orgânicos, na ordem."""
    cenario = {
        "codigo": "CF1000",
        "marca": "MANN-FILTER",
        "candidatos": ["filtro ar"],
        "chave_presente": True,
        "corpo": {
            "organic": [
                {"title": "MANN CF1000 filtro ar", "link": "https://loja.exemplo.com/p", "snippet": "", "position": 1},
                {"title": "Filtro De Ar Mann Filter Cf1000/3", "link": "https://x.com", "snippet": "", "position": 2},
            ]
        },
        "erro": None,
        "modo_llm": "indisponivel",
        "avaliacao": {},
        "traducao": {},
    }
    simples, eventos_simples, _, _ = _executar(cenario, com_resultados=False)
    estruturado, eventos_estr, transporte, _ = _executar(cenario, com_resultados=True)

    assert simples.status == "confirmado"
    assert estruturado.resultado == simples
    assert eventos_estr == eventos_simples
    assert transporte.chamadas == 1
    assert [r.title for r in estruturado.resultados_pesquisa] == [
        "MANN CF1000 filtro ar",
        "Filtro De Ar Mann Filter Cf1000/3",
    ]
