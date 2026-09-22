import pytest

from memory.destilar_regra import DestilacaoInvalidaError, destilar_regra
from memory.models import PedidoIntervencao


class _FakeLLM:
    def __init__(self, resposta):
        self.resposta = resposta
        self.chamadas = []

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append((system, user, json_schema, schema_name))
        return self.resposta


def _caso():
    return PedidoIntervencao(
        ponto="nome",
        grupo_ref="JE4699:DRIVEWAY",
        search_ref="JE4699",
        marca="DRIVEWAY",
        nomes_conflitantes=["POLIA", "KIT DA CORREIA DE ACESSÓRIOS"],
        motivo="a web não confirmou como os nomes se relacionam",
        contexto_web="fontes conflitantes",
        membro_ids=[1, 2],
    )


def test_destila_resposta_em_regra_generalizavel():
    llm = _FakeLLM(
        {
            "titulo": "Kit contém peças avulsas",
            "condicao": "Quando um grupo mistura um kit e uma peça avulsa que faz parte dele",
            "resolucao": "Classificar o kit como kit_componente e não mesclá-lo com a peça avulsa",
        }
    )

    proposta = destilar_regra(_caso(), "O kit contém a polia; não pode ser mesclado como se fosse a mesma peça.", llm)

    assert proposta.titulo == "Kit contém peças avulsas"
    assert "kit_componente" in proposta.resolucao
    assert llm.chamadas[0][3] == "destilar_regra_intervencao"
    assert "JE4699" in llm.chamadas[0][1]
    assert "Resposta do operador" in llm.chamadas[0][1]


def test_rejeita_resposta_humana_vazia():
    with pytest.raises(DestilacaoInvalidaError, match="vazia"):
        destilar_regra(_caso(), "  ", _FakeLLM({}))


@pytest.mark.parametrize(
    "resposta",
    [
        {},
        {"titulo": "t", "condicao": "c"},
        {"titulo": "", "condicao": "c", "resolucao": "r"},
        {"titulo": "t", "condicao": 3, "resolucao": "r"},
    ],
)
def test_rejeita_proposta_incompleta(resposta):
    with pytest.raises(DestilacaoInvalidaError):
        destilar_regra(_caso(), "resposta válida", _FakeLLM(resposta))
