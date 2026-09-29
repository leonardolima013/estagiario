"""Teste de integração da injeção da Skill_Serper via seletor na arbitragem de
nome (arbitration/nome.py). Usa fakes de LLM/cliente — sem rede, sem API real.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from arbitration.nome import arbitrar_nome
from tools.group_fetch import RegistroCatalogPart
from verification.selector import resolver_verificacao_web
from verification.serper_agent import verificar_nomenclatura_peca_serper
from verification.serper_client import ResultadoOrganico


@dataclass
class _ClienteFake:
    organicos: list[ResultadoOrganico] = field(default_factory=list)
    _api_key: str = "FAKE-API-KEY"

    def buscar(self, codigo, marca):
        return list(self.organicos)


@dataclass
class _LLMFake:
    resposta: dict

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        return dict(self.resposta)


def _registro(part_id, name):
    return RegistroCatalogPart(
        id=part_id, search_ref="ABC123", brand_id=1, brand="Bosch", name=name,
        width=None, depth=None, height=None, gross_weight=None, net_weight=None,
        ncm=None, barcode=None, application=None, born_at=None, deprecated_at=None,
        similarity_id=None, created=datetime(2020, 1, 1),
    )


def test_selector_serper_resolve_para_skill_serper():
    """R1.6, R2.1: com método serper, o seletor devolve a Skill_Serper (envolta
    para injetar o LLM do sub-agente no primeiro uso)."""
    chamavel = resolver_verificacao_web("serper")
    assert chamavel.__wrapped__ is verificar_nomenclatura_peca_serper


def test_arbitragem_escala_humano_quando_serper_inconclusivo():
    """R7.1, R7.2: nomes divergentes + Serper inconclusivo -> escalado_humano,
    sem alterar o name original; nenhum rule_store/intervenção disponível."""
    # Nomes que divergem de verdade (nenhum é superconjunto do outro).
    registros = [_registro(1, "PIVO SUPERIOR"), _registro(2, "PIVO INFERIOR")]

    # Cliente devolve orgânicos que não sustentam nenhum candidato -> inconclusivo.
    cliente = _ClienteFake(organicos=[ResultadoOrganico(title="algo diferente", link="https://x.com", snippet="", position=1)])
    llm_web = _LLMFake({
        "justificativa": "sem nome", "nome_extraido": None,
        "candidato_relacionado": None, "nome_especifico_coerente": False,
    })

    def verificar_web(codigo, marca, nomes_conflitantes, *, on_evento=None):
        return verificar_nomenclatura_peca_serper(
            codigo, marca, nomes_conflitantes, on_evento=on_evento, llm=llm_web, cliente=cliente
        )

    # llm da arbitragem não deve ser necessário para o caminho de escalonamento.
    class _LLMArb:
        def gerar_json(self, *a, **k):  # pragma: no cover
            raise AssertionError("não deve ser chamado no escalonamento")

    decisao = arbitrar_nome(
        registros, _LLMArb(), verificar_web=verificar_web
    )

    assert decisao.escalado_humano is True
    assert decisao.valor is None
    # O name original de cada registro permanece intacto.
    assert {r.id: r.name for r in registros} == {1: "PIVO SUPERIOR", 2: "PIVO INFERIOR"}


def test_arbitragem_aplica_nome_novo_confirmado_pela_serper():
    """Nome extraído da busca (diferente dos candidatos) vira a decisão de name."""
    registros = [_registro(1, "PIVO SUPERIOR"), _registro(2, "PIVO INFERIOR")]
    cliente = _ClienteFake(organicos=[
        ResultadoOrganico(title="ABC123 Pivô de Suspensão Inferior Gol 2010 a 2015", link="https://a.com", snippet="", position=1)
    ])
    llm_web = _LLMFake({
        "justificativa": "título", "nome_extraido": "Pivô de Suspensão Inferior",
        "candidato_relacionado": "PIVO INFERIOR", "nome_especifico_coerente": True,
    })

    def verificar_web(codigo, marca, nomes_conflitantes, *, on_evento=None):
        return verificar_nomenclatura_peca_serper(
            codigo, marca, nomes_conflitantes, on_evento=on_evento, llm=llm_web, cliente=cliente
        )

    decisao = arbitrar_nome(registros, _LLMFake({}), verificar_web=verificar_web)

    assert decisao.fonte == "verificacao_web"
    assert decisao.valor == "PIVÔ DE SUSPENSÃO INFERIOR"
    assert decisao.origem_id is None
    assert decisao.escalado_humano is False
