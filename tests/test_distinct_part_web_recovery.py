"""Exploração e caracterização da recuperação de partes distintas.

A primeira propriedade deste módulo é deliberadamente executada no código
unfixed antes da correção: ela deve falhar porque o pipeline descarta todos os
subclusters ``distinto_nao_classificado``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import pipeline
from arbitration.models import DecisaoCampo
from arbitration.nome import arbitrar_nome, arbitrar_nome_recuperacao
from memory.models import RegraProposta, RespostaIntervencao
from pipeline import ResultadoCaso, executar_caso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from sql_generation.montar_decisao import montar_decisao_merge
from tests.fakes import FakeLLMProvider, FakeVerificadorWeb
from tools.group_fetch import RegistroCatalogPart
from verification.models import FonteWeb, ResultadoVerificacao


class _LLMParticao:
    def __init__(self, subclusters: list[dict[str, object]]):
        self._subclusters = subclusters

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        assert schema_name == "particao"
        return {"subclusters": self._subclusters}


def _buscar_grupo(registros):
    def _buscar(search_ref, brand_id):
        return [
            registro.__class__(**{**registro.__dict__, "search_ref": search_ref, "brand_id": brand_id})
            for registro in registros
        ]

    return _buscar


class _ProviderRecorder:
    def __init__(self, decisao: DecisaoCampo):
        self.decisao = decisao
        self.chamadas: list[tuple[tuple[int, ...], str, int]] = []

    def __call__(self, registros, campo, brand_id):
        self.chamadas.append((tuple(registro.id for registro in registros), campo, brand_id))
        return self.decisao


def _decisao_provider_media() -> DecisaoCampo:
    return DecisaoCampo(
        campo="name",
        valor="PIVO DE SUSPENSÃO",
        justificativa="fonte não exata da marca",
        fonte="regra_confiabilidade",
        confianca="media",
        escalado_humano=False,
        origem_id=1,
    )


def _web_confirmada(nome: str = "PIVO DE SUSPENSÃO") -> FakeVerificadorWeb:
    return FakeVerificadorWeb(
        ResultadoVerificacao(
            status="confirmado",
            nome_sugerido=nome,
            justificativa="fontes convergentes",
            fontes=[],
        )
    )


def test_exploracao_distintos_nao_sao_descartados(fazer_registro, sem_banco, monkeypatch):
    """Property 1 / counterexample mínimo da condição de bug.

    No código unfixed esta asserção falha com provider=0, web=0 e nenhuma
    decisão, pois ``pipeline.py`` só percorre ``duplicata_real``.
    """
    registros = [
        fazer_registro(1, "PIVO SUPERIOR"),
        fazer_registro(2, "PIVO INFERIOR"),
    ]
    provider = _ProviderRecorder(_decisao_provider_media())
    web = _web_confirmada()
    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)

    resultado = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(
            [
                {
                    "label": "distinto_nao_classificado",
                    "membro_ids": [1],
                    "justificativa": "posição superior",
                },
                {
                    "label": "distinto_nao_classificado",
                    "membro_ids": [2],
                    "justificativa": "posição inferior",
                },
            ]
        ),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web,
    )

    assert provider.chamadas == [((1, 2), "name", 1)]
    assert web.chamadas == [("83061", "CITROEN", ["PIVO INFERIOR", "PIVO SUPERIOR"])]
    assert len(resultado.decisoes) == 1
    assert isinstance(resultado.decisoes[0], DecisaoMerge)
    assert {resultado.decisoes[0].vencedor_id, *resultado.decisoes[0].perdedor_ids} == {1, 2}


def test_exploracao_agrega_todos_os_subclusters_distintos_e_exclui_rotulos_protegidos(
    fazer_registro, sem_banco, monkeypatch
):
    registros = [
        fazer_registro(1, "PIVO A"),
        fazer_registro(2, "PIVO B"),
        fazer_registro(3, "PIVO C"),
        fazer_registro(4, "PIVO D"),
        fazer_registro(5, "KIT PIVO"),
        fazer_registro(6, "PIVO 24V"),
    ]
    provider = _ProviderRecorder(_decisao_provider_media())
    web = _web_confirmada()
    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)

    resultado = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(
            [
                {"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "a"},
                {"label": "distinto_nao_classificado", "membro_ids": [2, 3], "justificativa": "b"},
                {"label": "distinto_nao_classificado", "membro_ids": [4], "justificativa": "c"},
                {"label": "kit_componente", "membro_ids": [5], "justificativa": "kit"},
                {"label": "variante_dimensional", "membro_ids": [6], "justificativa": "variante"},
            ]
        ),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web,
    )

    assert provider.chamadas == [((1, 2, 3, 4), "name", 1)]
    assert web.chamadas == [("83061", "CITROEN", ["PIVO A", "PIVO B", "PIVO C", "PIVO D"])]
    assert len(resultado.decisoes) == 1
    decisao = resultado.decisoes[0]
    assert isinstance(decisao, DecisaoMerge)
    assert {decisao.vencedor_id, *decisao.perdedor_ids} == {1, 2, 3, 4}
    assert {5, 6}.isdisjoint({decisao.vencedor_id, *decisao.perdedor_ids})


def test_preservacao_singleton_distinto_nao_chama_recuperacao(fazer_registro, sem_banco, monkeypatch):
    registro = fazer_registro(1, "PIVO ISOLADO")
    provider = _ProviderRecorder(_decisao_provider_media())
    web = _web_confirmada()
    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)

    resultado = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(
            [{"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "isolado"}]
        ),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo([registro]),
        verificar_web=web,
    )

    assert isinstance(resultado, ResultadoCaso)
    assert resultado.decisoes == []
    assert resultado.sql == ""
    assert provider.chamadas == []
    assert web.chamadas == []


def test_preservacao_rotulos_protegidos_ficam_sem_merge(fazer_registro, sem_banco, monkeypatch):
    registros = [
        fazer_registro(1, "KIT PIVO"),
        fazer_registro(2, "KIT PIVO COMPONENTE"),
        fazer_registro(3, "PIVO 12V"),
        fazer_registro(4, "PIVO 24V"),
    ]
    provider = _ProviderRecorder(_decisao_provider_media())
    web = _web_confirmada()
    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)

    resultado = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(
            [
                {"label": "kit_componente", "membro_ids": [1, 2], "justificativa": "kit"},
                {"label": "variante_dimensional", "membro_ids": [3, 4], "justificativa": "variante"},
            ]
        ),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web,
    )

    assert resultado.decisoes == []
    assert resultado.sql == ""
    assert provider.chamadas == []
    assert web.chamadas == []


def test_preservacao_web_inconclusiva_nao_produz_sql_destrutivo(
    fazer_registro, sem_banco, monkeypatch
):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    provider = _ProviderRecorder(_decisao_provider_media())
    web = FakeVerificadorWeb(
        ResultadoVerificacao(
            status="inconclusivo",
            nome_sugerido=None,
            justificativa="fontes conflitantes",
            fontes=[],
        )
    )
    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)

    resultado = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(
            [
                {"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "a"},
                {"label": "distinto_nao_classificado", "membro_ids": [2], "justificativa": "b"},
            ]
        ),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web,
    )

    assert len(resultado.decisoes) == 1
    assert isinstance(resultado.decisoes[0], GrupoSinalizado)
    assert resultado.decisoes[0].membro_ids == [1, 2]
    assert "DELETE" not in resultado.sql
    assert "BEGIN" not in resultado.sql



def _subclusters_distintos(*grupos: list[int]) -> list[dict[str, object]]:
    return [
        {"label": "distinto_nao_classificado", "membro_ids": ids, "justificativa": "candidato"}
        for ids in grupos
    ]


def _executar_recuperacao(
    registros,
    subclusters,
    provider,
    web,
    fazer_registro,
    monkeypatch,
):
    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)
    return executar_caso(
        "83061",
        1,
        llm=_LLMParticao(subclusters),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web,
    )


def test_recuperacao_provider_alta_mescla_sem_web(fazer_registro, sem_banco, monkeypatch):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="PIVO INFERIOR",
        justificativa="fabricante exata da marca",
        fonte="regra_confiabilidade",
        confianca="alta",
        escalado_humano=False,
        origem_id=2,
    )
    provider = _ProviderRecorder(decisao_provedor)
    web = _web_confirmada()

    resultado = _executar_recuperacao(
        registros, _subclusters_distintos([1], [2]), provider, web, fazer_registro, monkeypatch
    )

    assert provider.chamadas == [((1, 2), "name", 1)]
    assert web.chamadas == []
    assert len(resultado.decisoes) == 1
    decisao = resultado.decisoes[0]
    assert isinstance(decisao, DecisaoMerge)
    assert next(dc for dc in decisao.decisoes_campo if dc.campo == "name") is decisao_provedor


@pytest.mark.parametrize("confianca", ["media", "baixa", "minima", None])
def test_recuperacao_provider_abaixo_de_alta_chama_web(
    confianca, fazer_registro, sem_banco, monkeypatch
):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="PIVO INFERIOR",
        justificativa="fonte não é fabricante exata",
        fonte="regra_confiabilidade",
        confianca=confianca,
        escalado_humano=False,
        origem_id=2,
    )
    provider = _ProviderRecorder(decisao_provedor)
    web = _web_confirmada("PIVO DE SUSPENSÃO")

    resultado = _executar_recuperacao(
        registros, _subclusters_distintos([1], [2]), provider, web, fazer_registro, monkeypatch
    )

    assert provider.chamadas == [((1, 2), "name", 1)]
    assert len(web.chamadas) == 1
    assert len(resultado.decisoes) == 1
    decisao = resultado.decisoes[0]
    assert isinstance(decisao, DecisaoMerge)
    decisao_nome = next(dc for dc in decisao.decisoes_campo if dc.campo == "name")
    assert decisao_nome.fonte == "verificacao_web"
    assert decisao_nome.valor == "PIVO DE SUSPENSÃO"
    assert decisao_nome.evidencias == []
    assert {decisao.vencedor_id, *decisao.perdedor_ids} == {1, 2}


def test_recuperacao_provider_nulo_e_erro_seguem_para_web(fazer_registro, sem_banco, monkeypatch):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    web = _web_confirmada("PIVO DE SUSPENSÃO")
    chamadas = []

    def provider_nulo(registros, campo, brand_id):
        chamadas.append((tuple(r.id for r in registros), campo, brand_id))
        return None

    resultado_nulo = _executar_recuperacao(
        registros, _subclusters_distintos([1], [2]), provider_nulo, web, fazer_registro, monkeypatch
    )
    assert chamadas == [((1, 2), "name", 1)]
    assert len(web.chamadas) == 1
    assert isinstance(resultado_nulo.decisoes[0], DecisaoMerge)

    web_erro = _web_confirmada("PIVO DE SUSPENSÃO")
    avisos = []

    def provider_erro(registros, campo, brand_id):
        raise RuntimeError("falha transitória simulada")

    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider_erro, raising=False)
    resultado_erro = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(_subclusters_distintos([1], [2])),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web_erro,
        on_aviso=avisos.append,
    )
    assert len(web_erro.chamadas) == 1
    assert isinstance(resultado_erro.decisoes[0], DecisaoMerge)
    assert any("inconclusiva por erro" in aviso for aviso in avisos)


def test_recuperacao_web_preserva_fontes_e_justificativa(fazer_registro, sem_banco, monkeypatch):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    provider = _ProviderRecorder(_decisao_provider_media())
    web = FakeVerificadorWeb(
        ResultadoVerificacao(
            status="confirmado",
            nome_sugerido="PIVO DE SUSPENSÃO",
            justificativa="duas fontes confirmam o nome",
            fontes=[FonteWeb(url="https://catalogo.exemplo/pivo", nome_encontrado="PIVO DE SUSPENSÃO")],
        )
    )

    resultado = _executar_recuperacao(
        registros, _subclusters_distintos([1], [2]), provider, web, fazer_registro, monkeypatch
    )
    decisao = resultado.decisoes[0]
    assert isinstance(decisao, DecisaoMerge)
    decisao_nome = next(dc for dc in decisao.decisoes_campo if dc.campo == "name")
    assert decisao_nome.valor == "PIVO DE SUSPENSÃO"
    assert decisao_nome.fonte == "verificacao_web"
    assert decisao_nome.evidencias == [
        {
            "tipo": "web",
            "url": "https://catalogo.exemplo/pivo",
            "nome_encontrado": "PIVO DE SUSPENSÃO",
        }
    ]
    assert "https://catalogo.exemplo/pivo" in decisao_nome.justificativa
    assert "duas fontes confirmam" in decisao_nome.justificativa


class _LLMNuncaChamado:
    def gerar_json(self, *args, **kwargs):
        raise AssertionError("LLM textual não deveria ser chamado neste caso")


@pytest.mark.parametrize("confianca", ["media", "baixa", "minima", None])
def test_nome_divergente_normal_abaixo_de_alta_chama_web(confianca, fazer_registro):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="PIVO SUPERIOR",
        justificativa="fonte parcial",
        fonte="regra_confiabilidade",
        confianca=confianca,
        origem_id=1,
    )
    web = _web_confirmada("PIVO INFERIOR")

    decisao = arbitrar_nome(
        registros,
        llm=_LLMNuncaChamado(),
        brand_id=1,
        verificar_web=web,
        arbitrar_por_provedor=lambda registros, campo, brand_id: decisao_provedor,
    )

    assert decisao.fonte == "verificacao_web"
    assert decisao.valor == "PIVO INFERIOR"
    assert len(web.chamadas) == 1


def test_nome_divergente_normal_alta_bypassa_web(fazer_registro):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="PIVO SUPERIOR",
        justificativa="fabricante exata",
        fonte="regra_confiabilidade",
        confianca="alta",
        origem_id=1,
    )

    class WebExplode:
        def __call__(self, *args, **kwargs):
            raise AssertionError("web não deveria ser chamada para confiança alta")

    decisao = arbitrar_nome(
        registros,
        llm=_LLMNuncaChamado(),
        brand_id=1,
        verificar_web=WebExplode(),
        arbitrar_por_provedor=lambda registros, campo, brand_id: decisao_provedor,
    )

    assert decisao is decisao_provedor


def test_nome_consenso_preserva_sem_conflito_sem_web(fazer_registro):
    registros = [fazer_registro(1, "PIVO"), fazer_registro(2, "PIVO")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="PIVO",
        justificativa="consenso",
        fonte="sem_conflito",
    )

    class WebExplode:
        def __call__(self, *args, **kwargs):
            raise AssertionError("web não deveria ser chamada para consenso")

    decisao = arbitrar_nome(
        registros,
        llm=_LLMNuncaChamado(),
        brand_id=1,
        verificar_web=WebExplode(),
        arbitrar_por_provedor=lambda registros, campo, brand_id: decisao_provedor,
    )

    assert decisao is decisao_provedor


def test_montar_decisao_usa_name_precalculado_sem_repetir_web(fazer_registro, sem_banco):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    decisao_precalculada = DecisaoCampo(
        campo="name",
        valor="PIVO DE SUSPENSÃO",
        justificativa="confirmado antes do merge",
        fonte="verificacao_web",
        confianca="alta",
        evidencias=[{"tipo": "web", "url": "https://exemplo/pivo"}],
    )

    class WebExplode:
        def __call__(self, *args, **kwargs):
            raise AssertionError("web não deveria ser chamada novamente no merge")

    resultado = montar_decisao_merge(
        "83061:CITROEN",
        registros,
        llm=FakeLLMProvider({}),
        rule_store=None,
        brand_id=1,
        verificar_web=WebExplode(),
        decisoes_campo_precalculadas={"name": decisao_precalculada},
    )

    assert isinstance(resultado, DecisaoMerge)
    assert next(dc for dc in resultado.decisoes_campo if dc.campo == "name") is decisao_precalculada
    assert resultado.valores_atuais_vencedor["name"] == registros[0].name



def _registro_property(part_id: int, name: str) -> RegistroCatalogPart:
    return RegistroCatalogPart(
        id=part_id,
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
        created=datetime(2020, 1, 1) + timedelta(seconds=part_id),
    )


@settings(max_examples=8)
@given(confianca=st.sampled_from(["media", "baixa", "minima", None]))
def test_property_provider_nao_alto_sempre_encaminha_web(confianca):
    """**Validates: Requirements 2.3, 2.8**"""
    registros = [
        _registro_property(1, "PIVO SUPERIOR"),
        _registro_property(2, "PIVO INFERIOR"),
    ]
    provider = _ProviderRecorder(
        DecisaoCampo(
            campo="name",
            valor="PIVO SUPERIOR",
            justificativa="fonte abaixo do gate",
            fonte="regra_confiabilidade",
            confianca=confianca,
            origem_id=1,
        )
    )
    web = _web_confirmada("PIVO DE SUSPENSÃO")

    decisao = arbitrar_nome_recuperacao(
        registros,
        llm=FakeLLMProvider({}),
        brand_id=1,
        verificar_web=web,
        arbitrar_por_provedor=provider,
    )

    assert provider.chamadas == [((1, 2), "name", 1)]
    assert len(web.chamadas) == 1
    assert decisao.fonte == "verificacao_web"
    assert decisao.valor == "PIVO DE SUSPENSÃO"



def test_recuperacao_inconclusiva_pode_ser_resolvida_por_intervencao(
    fazer_registro, sem_banco, monkeypatch
):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    provider = _ProviderRecorder(_decisao_provider_media())
    web = FakeVerificadorWeb(
        ResultadoVerificacao(
            status="inconclusivo",
            nome_sugerido=None,
            justificativa="fontes conflitantes",
            fontes=[],
        )
    )
    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return RespostaIntervencao(
            resposta_humana="O catálogo confirma a posição inferior.",
            regra=RegraProposta(
                titulo="Preferir posição inferior",
                condicao="Quando a web não resolve o conflito de posição",
                resolucao="Escolher a posição inferior confirmada pelo operador",
            ),
            valor="PIVO INFERIOR",
            origem_id=2,
            criado_por="teste",
        )

    monkeypatch.setattr(pipeline, "arbitrar_por_provedor", provider, raising=False)
    resultado = executar_caso(
        "83061",
        1,
        llm=_LLMParticao(_subclusters_distintos([1], [2])),
        dependencias_fk=[],
        buscar_grupo=_buscar_grupo(registros),
        verificar_web=web,
        pedir_intervencao=pedir,
    )

    assert len(pedidos) == 1
    assert isinstance(resultado.decisoes[0], DecisaoMerge)
    decisao_nome = next(dc for dc in resultado.decisoes[0].decisoes_campo if dc.campo == "name")
    assert decisao_nome.fonte == "intervencao_humana"
    assert decisao_nome.valor == "PIVO INFERIOR"
