"""Testes de integração do dispatcher `arbitrar_campo` (arbitration/arbitrar.py)
com o PRIMEIRO desempate por provedor (Tool_Provedor_Informacao).

Testes PUROS — sem banco, sem LLM real. A interação testada é a cascata do
dispatcher em torno de `arbitrar_por_provedor`:

Abordagem escolhida: monkeypatch de `arbitration.arbitrar.arbitrar_por_provedor`.
Motivo — `arbitrar_campo` NÃO aceita kwargs `buscar_fonte` /
`calcular_nivel_confiabilidade` (só `arbitrar_campo_numerico` os aceita), então
não há como injetar as fontes por parâmetro no dispatcher. Substituir a tool no
namespace onde o dispatcher a importou (`arbitration.arbitrar`) é a forma mais
limpa e direta de controlar o RESULTADO da tool (bem-sucedida / inconclusiva /
erro) sem montar todo o encanamento de `tools.reliability`. Isso mantém o foco
no que a Tarefa 6.3 cobre: o branching do dispatcher (R2.2, R2.3, R2.4).

Nos casos B e C a estratégia seguinte (`arbitrar_campo_numerico`) roda de
verdade, com um `FakeLLMProvider` (tests/fakes.py) fornecendo o julgamento do
modelo — sem chamada de API. O fixture `sem_banco` (tests/conftest.py) blinda os
lookups reais de confiabilidade dentro de `arbitrar_campo_numerico`, forçando
fonte desconhecida e, assim, caindo no julgamento do modelo de forma
determinística.

_Requirements: 2.2, 2.3, 2.4_
"""

from __future__ import annotations

import pytest

from arbitration.models import DecisaoCampo
from tests.fakes import FakeLLMProvider

_BRAND_ID = 1
_CAMPO = "width"


class _LLMExplode(FakeLLMProvider):
    """FakeLLMProvider que EXPLODE se `gerar_json` for chamado.

    Usado no Caso A pra provar que, quando a tool decide sozinha, o dispatcher
    retorna sem tocar em nenhuma estratégia seguinte (que usaria o LLM).
    """

    def __init__(self):
        super().__init__({})

    def gerar_json(self, system, user, json_schema, schema_name="output"):  # noqa: D401
        raise AssertionError(
            "LLM não deveria ser chamado quando a Tool_Provedor_Informacao decide sozinha (R2.2)"
        )


def test_tool_decide_sozinha_nao_invoca_proxima_estrategia(fazer_registro, monkeypatch):
    # Caso A (R2.2): a tool retorna Arbitragem_Bem_Sucedida
    # (fonte=regra_confiabilidade, valor definido, escalado_humano=False) ->
    # `arbitrar_campo` usa essa decisão como resultado final, SEM invocar a
    # estratégia seguinte (o LLM explodiria se fosse chamado).
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]

    decisao_tool = DecisaoCampo(
        campo=_CAMPO,
        valor=3.2,
        justificativa="Fonte de confiabilidade 'alta' (provider_id=1) decide sozinha o campo 'width'.",
        fonte="regra_confiabilidade",
        escalado_humano=False,
        origem_id=1,
        confianca="alta",
    )

    def _tool_bem_sucedida(regs, campo, brand_id, *args, **kwargs):
        return decisao_tool

    monkeypatch.setattr("arbitration.arbitrar.arbitrar_por_provedor", _tool_bem_sucedida)

    decisao = _chamar(registros, monkeypatch, llm=_LLMExplode())

    assert decisao is decisao_tool
    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.valor == 3.2
    assert decisao.origem_id == 1
    assert decisao.escalado_humano is False


def test_tool_inconclusiva_segue_para_estrategia_do_campo(fazer_registro, monkeypatch, sem_banco):
    # Caso B (R2.3): a tool retorna Arbitragem_Inconclusiva
    # (escalado_humano=True, valor=None) -> `arbitrar_campo` prossegue para a
    # estratégia do campo (arbitrar_campo_numerico), cuja decisão final vem do
    # julgamento do modelo (FakeLLMProvider).
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]

    decisao_inconclusiva = DecisaoCampo(
        campo=_CAMPO,
        valor=None,
        justificativa="Arbitragem por confiabilidade inconclusiva: empate no topo.",
        fonte="escalado_humano",
        escalado_humano=True,
        origem_id=None,
        confianca=None,
    )

    def _tool_inconclusiva(regs, campo, brand_id, *args, **kwargs):
        return decisao_inconclusiva

    monkeypatch.setattr("arbitration.arbitrar.arbitrar_por_provedor", _tool_inconclusiva)

    fake_llm = FakeLLMProvider(
        {"part_id_escolhido": 1, "justificativa": "peça maior faz mais sentido", "confianca": "alta"}
    )
    decisao = _chamar(registros, monkeypatch, llm=fake_llm)

    # a decisão final NÃO é a inconclusiva da tool: veio da estratégia seguinte.
    assert decisao is not decisao_inconclusiva
    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == 3.2
    assert decisao.origem_id == 1


def test_tool_inconclusiva_baixa_confianca_escala_humano(fazer_registro, monkeypatch, sem_banco):
    # Caso B, variação (R2.3): tool inconclusiva -> estratégia seguinte roda e,
    # com o modelo em baixa confiança, o resultado final é escalado_humano vindo
    # da PRÓPRIA estratégia do campo (não da tool).
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]

    def _tool_inconclusiva(regs, campo, brand_id, *args, **kwargs):
        return DecisaoCampo(
            campo=campo, valor=None, justificativa="inconclusiva",
            fonte="escalado_humano", escalado_humano=True,
        )

    monkeypatch.setattr("arbitration.arbitrar.arbitrar_por_provedor", _tool_inconclusiva)

    fake_llm = FakeLLMProvider(
        {"part_id_escolhido": 1, "justificativa": "não tenho certeza", "confianca": "baixa"}
    )
    decisao = _chamar(registros, monkeypatch, llm=fake_llm)

    assert decisao.fonte == "escalado_humano"
    assert decisao.escalado_humano is True
    assert decisao.valor is None
    assert decisao.origem_id is None


def test_tool_lanca_excecao_emite_aviso_e_segue(fazer_registro, monkeypatch, sem_banco):
    # Caso C (R2.4): a tool LANÇA erro -> `arbitrar_campo` NÃO quebra, trata como
    # inconclusiva, emite aviso via on_aviso e prossegue para a estratégia
    # seguinte (julgamento do modelo).
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]

    def _tool_explode(regs, campo, brand_id, *args, **kwargs):
        raise RuntimeError("falha simulada de banco em buscar_fonte_atual")

    monkeypatch.setattr("arbitration.arbitrar.arbitrar_por_provedor", _tool_explode)

    avisos: list[str] = []
    fake_llm = FakeLLMProvider(
        {"part_id_escolhido": 2, "justificativa": "menor bate com o catálogo", "confianca": "alta"}
    )
    decisao = _chamar(registros, monkeypatch, llm=fake_llm, on_aviso=avisos.append)

    # on_aviso recebeu a mensagem de degradação controlada.
    assert len(avisos) == 1
    assert _CAMPO in avisos[0]
    assert "falha simulada de banco" in avisos[0]

    # a cascata não quebrou e a decisão final veio da estratégia seguinte.
    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == 2.0
    assert decisao.origem_id == 2


# --- helper ---------------------------------------------------------------


def _chamar(registros, monkeypatch, *, llm, on_aviso=None):
    """Chama `arbitrar_campo` para o campo numérico sob teste.

    Import local: o monkeypatch de `arbitration.arbitrar.arbitrar_por_provedor`
    já foi aplicado pelo teste antes desta chamada; importar aqui garante que o
    dispatcher usado seja o do módulo já com o atributo substituído.
    """
    from arbitration.arbitrar import arbitrar_campo

    return arbitrar_campo(
        registros,
        _CAMPO,
        llm=llm,
        rule_store=None,
        brand_id=_BRAND_ID,
        on_aviso=on_aviso,
    )
