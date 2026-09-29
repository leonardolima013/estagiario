"""Regressão: o Metodo_Verificacao_Web configurado em
ESTAGIARIO_WEB_VERIFICATION_METODO precisa ser honrado no caminho REAL do
pipeline/arbitragem.

Bug corrigido: pipeline.executar_caso, sql_generation.montar_decisao_merge e
arbitration.arbitrar_campo travavam o default de `verificar_web` na Skill de
Playwright e sempre a repassavam pra baixo. Assim `verificar_web` nunca chegava
None em arbitration.nome.arbitrar_nome — o único ponto que consulta
resolver_verificacao_web() — e o seletor por .env era curto-circuitado. Com
ESTAGIARIO_WEB_VERIFICATION_METODO=serper o pipeline continuava usando Playwright.

O default dessas três camadas agora é None e propaga até arbitrar_nome, que
resolve por configuração. Estes testes cobrem justamente a fronteira
(arbitrar_campo / executar_caso) que carregava o default hardcoded, sem injetar
`verificar_web`, sem subir Playwright/MCP e sem tocar rede/API.
"""

from __future__ import annotations

from datetime import datetime

import pytest

import verification.selector as selector
from arbitration.arbitrar import arbitrar_campo
from pipeline import executar_caso
from tools.group_fetch import RegistroCatalogPart

# Sentinelas: substituem os chamáveis reais de Serper/Playwright para observar
# QUAL skill o seletor escolhe, sem importar Playwright/MCP nem fazer I/O. O
# seletor devolve o retorno da factory correspondente ao método configurado.
_SENTINELA_SERPER = "SKILL_SERPER"
_SENTINELA_PLAYWRIGHT = "SKILL_PLAYWRIGHT"


@pytest.fixture
def spy_verificacao_web(monkeypatch):
    """Captura o chamável de verificação web efetivamente usado no caminho real.

    Faz o seletor devolver uma sentinela por método (evitando import pesado) e
    troca o ponto de divergência de nomes por um verificador que registra qual
    sentinela recebeu — é exatamente o valor que arbitrar_nome resolveu quando
    `verificar_web` chega None.
    """
    monkeypatch.setattr(selector, "_default_serper", lambda: _SENTINELA_SERPER)
    monkeypatch.setattr(selector, "_default_playwright", lambda: _SENTINELA_PLAYWRIGHT)

    capturado: dict[str, object] = {}

    import arbitration.nome as nome_mod

    def fake_via_web(registros, llm, rule_store, on_aviso, verificar_web, *args, **kwargs):
        # `verificar_web` aqui é o que arbitrar_nome resolveu (via selector) ou o
        # que foi injetado. Registramos e devolvemos uma decisão qualquer válida.
        from arbitration.models import DecisaoCampo

        capturado["verificar_web"] = verificar_web
        return DecisaoCampo(
            campo="name", valor=registros[0].name,
            justificativa="stub de regressão", fonte="verificacao_web", origem_id=registros[0].id,
        )

    monkeypatch.setattr(nome_mod, "_arbitrar_nome_via_web", fake_via_web)
    return capturado


def _registro(part_id, name):
    return RegistroCatalogPart(
        id=part_id, search_ref="ABC123", brand_id=1, brand="Bosch", name=name,
        width=None, depth=None, height=None, gross_weight=None, net_weight=None,
        ncm=None, barcode=None, application=None, born_at=None, deprecated_at=None,
        similarity_id=None, created=datetime(2020, 1, 1),
    )


class _LLMNuncaChamado:
    def gerar_json(self, *a, **k):  # pragma: no cover - não deve rodar no caminho web
        raise AssertionError("LLM não deve ser chamado no caminho de verificação web")


# Nomes que divergem de verdade -> forçam o caminho de verificação web em arbitrar_nome.
_NOMES_DIVERGENTES = [_registro(1, "PIVO SUPERIOR"), _registro(2, "PIVO INFERIOR")]


def test_arbitrar_campo_usa_serper_quando_env_serper(spy_verificacao_web, monkeypatch):
    """Com ESTAGIARIO_WEB_VERIFICATION_METODO=serper e SEM injetar verificar_web,
    a fronteira arbitrar_campo resolve para a Skill_Serper — não a de Playwright."""
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "serper")

    decisao = arbitrar_campo(
        _NOMES_DIVERGENTES, "name", _LLMNuncaChamado(), rule_store=None, brand_id=None,
    )

    assert decisao.campo == "name"
    assert spy_verificacao_web["verificar_web"] == _SENTINELA_SERPER
    assert spy_verificacao_web["verificar_web"] != _SENTINELA_PLAYWRIGHT


def test_arbitrar_campo_usa_playwright_quando_env_ausente(spy_verificacao_web, monkeypatch):
    """Com a env var ausente e SEM injetar verificar_web, o default preservado é
    Playwright (resolver_verificacao_web() -> playwright)."""
    monkeypatch.delenv("ESTAGIARIO_WEB_VERIFICATION_METODO", raising=False)

    arbitrar_campo(
        _NOMES_DIVERGENTES, "name", _LLMNuncaChamado(), rule_store=None, brand_id=None,
    )

    assert spy_verificacao_web["verificar_web"] == _SENTINELA_PLAYWRIGHT


def test_arbitrar_campo_usa_playwright_quando_env_vazia(spy_verificacao_web, monkeypatch):
    """Env var vazia normaliza para playwright (R1.1/R1.3)."""
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "   ")

    arbitrar_campo(
        _NOMES_DIVERGENTES, "name", _LLMNuncaChamado(), rule_store=None, brand_id=None,
    )

    assert spy_verificacao_web["verificar_web"] == _SENTINELA_PLAYWRIGHT


def test_executar_caso_propaga_serper_ate_arbitrar_nome(spy_verificacao_web, monkeypatch):
    """Ponta a ponta na fronteira do pipeline: executar_caso -> montar_decisao_merge
    -> arbitrar_campo -> arbitrar_nome, sem injetar verificar_web em nenhum ponto.
    Com serper no .env, a skill resolvida no fim da cadeia é a Serper."""
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "serper")

    class _LLMParticao:
        def gerar_json(self, system, user, json_schema, schema_name="output"):
            if schema_name == "particao":
                return {
                    "subclusters": [
                        {"label": "duplicata_real", "membro_ids": [1, 2], "justificativa": "mesma peça"}
                    ]
                }
            raise AssertionError(f"schema_name inesperado: {schema_name!r}")

    def buscar_grupo(search_ref, brand_id):
        return [_registro(1, "PIVO SUPERIOR"), _registro(2, "PIVO INFERIOR")]

    executar_caso(
        "ABC123", 1, llm=_LLMParticao(), dependencias_fk=[], buscar_grupo=buscar_grupo,
    )

    assert spy_verificacao_web["verificar_web"] == _SENTINELA_SERPER
