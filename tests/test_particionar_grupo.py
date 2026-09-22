from datetime import datetime

import pytest

from partitioning.particionar import ParticaoInvalidaError, particionar_grupo
from tools.group_fetch import RegistroCatalogPart


def _registro(id_, name, **overrides) -> RegistroCatalogPart:
    campos = dict(
        id=id_,
        search_ref="83061",
        brand_id=1,
        brand="CITROEN",
        name=name,
        width=None,
        depth=None,
        height=None,
        gross_weight=None,
        net_weight=None,
        ncm=None,
        barcode=None,
        application=None,
        born_at=None,
        deprecated_at=None,
        similarity_id=None,
        created=datetime(2020, 1, 1),
    )
    campos.update(overrides)
    return RegistroCatalogPart(**campos)


class FakeLLMProvider:
    def __init__(self, resposta: dict):
        self._resposta = resposta

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        return self._resposta


class FakeLLMProviderSequencial:
    """Retorna uma resposta diferente a cada chamada — simula o modelo errando
    a validação estrutural e acertando numa tentativa seguinte (ou não)."""

    def __init__(self, respostas: list[dict]):
        self._respostas = list(respostas)
        self.chamadas = 0

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        resposta = self._respostas[min(self.chamadas, len(self._respostas) - 1)]
        self.chamadas += 1
        return resposta


_GRUPO = [
    _registro(1, "POLIA"),
    _registro(2, "KIT DA CORREIA SINCRONIZADORA"),
    _registro(3, "POLIA DA CORREIA SINCRONIZADORA"),
]


def test_caminho_feliz_retorna_particao():
    fake = FakeLLMProvider(
        {
            "subclusters": [
                {"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "peça isolada"},
                {"label": "kit_componente", "membro_ids": [2, 3], "justificativa": "kit e seu componente"},
            ]
        }
    )

    particao = particionar_grupo(_GRUPO, llm=fake)

    assert particao.grupo_ref == "83061:CITROEN"
    assert len(particao.subclusters) == 2
    assert {m for s in particao.subclusters for m in s.membro_ids} == {1, 2, 3}


def test_rejeita_id_faltando():
    fake = FakeLLMProvider(
        {"subclusters": [{"label": "distinto_nao_classificado", "membro_ids": [1, 2], "justificativa": "x"}]}
    )
    with pytest.raises(ParticaoInvalidaError, match="ausentes"):
        particionar_grupo(_GRUPO, llm=fake)


def test_rejeita_id_duplicado():
    fake = FakeLLMProvider(
        {
            "subclusters": [
                {"label": "distinto_nao_classificado", "membro_ids": [1, 2], "justificativa": "x"},
                {"label": "kit_componente", "membro_ids": [2, 3], "justificativa": "y"},
            ]
        }
    )
    with pytest.raises(ParticaoInvalidaError, match="duplicados"):
        particionar_grupo(_GRUPO, llm=fake)


def test_rejeita_id_desconhecido():
    fake = FakeLLMProvider(
        {"subclusters": [{"label": "distinto_nao_classificado", "membro_ids": [1, 2, 3, 999], "justificativa": "x"}]}
    )
    with pytest.raises(ParticaoInvalidaError, match="não pertencem"):
        particionar_grupo(_GRUPO, llm=fake)


def test_rejeita_rotulo_invalido():
    fake = FakeLLMProvider(
        {"subclusters": [{"label": "rotulo_inventado", "membro_ids": [1, 2, 3], "justificativa": "x"}]}
    )
    with pytest.raises(ParticaoInvalidaError, match="Rótulo inválido"):
        particionar_grupo(_GRUPO, llm=fake)


def test_rejeita_grupo_vazio():
    with pytest.raises(ValueError):
        particionar_grupo([], llm=FakeLLMProvider({"subclusters": []}))


_PARTICAO_INVALIDA_ID_FALTANDO = {
    "subclusters": [{"label": "distinto_nao_classificado", "membro_ids": [1, 2], "justificativa": "x"}]
}
_PARTICAO_VALIDA = {
    "subclusters": [
        {"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "x"},
        {"label": "kit_componente", "membro_ids": [2, 3], "justificativa": "y"},
    ]
}


def test_tenta_de_novo_apos_resposta_invalida_e_acerta_na_segunda():
    fake = FakeLLMProviderSequencial([_PARTICAO_INVALIDA_ID_FALTANDO, _PARTICAO_VALIDA])

    particao = particionar_grupo(_GRUPO, llm=fake, max_tentativas=3)

    assert fake.chamadas == 2
    assert {m for s in particao.subclusters for m in s.membro_ids} == {1, 2, 3}


def test_desiste_apos_max_tentativas_e_levanta_o_ultimo_erro():
    fake = FakeLLMProviderSequencial([_PARTICAO_INVALIDA_ID_FALTANDO])

    with pytest.raises(ParticaoInvalidaError, match="ausentes"):
        particionar_grupo(_GRUPO, llm=fake, max_tentativas=2)

    assert fake.chamadas == 2
