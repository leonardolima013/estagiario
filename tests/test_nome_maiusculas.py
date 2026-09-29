"""Todo name escolhido sai em MAIÚSCULAS, em qualquer caminho de decisão —
juiz interno, verificação web, registro único, decisão pré-calculada no merge —
e é isso que chega ao SQL. Fakes, sem rede/API/banco."""

from __future__ import annotations

from datetime import datetime

from arbitration.arbitrar import arbitrar_campo
from arbitration.models import DecisaoCampo
from arbitration.nome import arbitrar_nome, nome_em_maiusculas
from sql_generation.gerar_sql import gerar_sql
from sql_generation.montar_decisao import montar_decisao_merge
from tests.fakes import FakeLLMProvider, FakeVerificadorWeb
from verification.models import ResultadoVerificacao


def test_juiz_interno_sintetizado_em_minusculas_sai_maiusculo(fazer_registro):
    # Nomes compatíveis (um é subconjunto do outro) -> juiz interno, sem web.
    registros = [fazer_registro(1, "polia"), fazer_registro(2, "polia da correia")]
    llm = FakeLLMProvider({"modo": "sintetizar", "nome_sintetizado": "Polia da Correia Dentada", "justificativa": "x"})
    decisao = arbitrar_nome(registros, llm)
    assert decisao.valor == "POLIA DA CORREIA DENTADA"


def test_juiz_interno_escolhendo_registro_minusculo_sai_maiusculo(fazer_registro):
    registros = [fazer_registro(1, "polia"), fazer_registro(2, "polia da correia")]
    decisao = arbitrar_nome(registros, FakeLLMProvider({"modo": "escolher", "part_id_escolhido": 2, "justificativa": "x"}))
    assert decisao.valor == "POLIA DA CORREIA"
    assert decisao.origem_id == 2


def test_verificacao_web_confirmada_sai_maiuscula(fazer_registro):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    web = FakeVerificadorWeb(ResultadoVerificacao(
        status="confirmado", nome_sugerido="Pivô de Suspensão Inferior", justificativa="ok", fontes=[],
    ))
    decisao = arbitrar_nome(registros, FakeLLMProvider({}), verificar_web=web)
    assert decisao.valor == "PIVÔ DE SUSPENSÃO INFERIOR"


def test_escalado_humano_permanece_sem_valor(fazer_registro):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    web = FakeVerificadorWeb(ResultadoVerificacao(
        status="inconclusivo", nome_sugerido=None, justificativa="nada", fontes=[],
    ))
    decisao = arbitrar_nome(registros, FakeLLMProvider({}), verificar_web=web)
    assert decisao.escalado_humano is True
    assert decisao.valor is None


def test_registro_unico_sai_maiusculo(fazer_registro):
    decisao = arbitrar_campo([fazer_registro(1, "polia")], "name", FakeLLMProvider({}), None, brand_id=1)
    assert decisao.valor == "POLIA"


def test_nome_em_maiusculas_so_afeta_name():
    outro = DecisaoCampo(campo="application", valor="gol 1.0", justificativa="x", fonte="normalizacao")
    assert nome_em_maiusculas(outro) is outro


def test_decisao_precalculada_minuscula_chega_maiuscula_ao_sql(fazer_registro, sem_banco):
    registros = [
        fazer_registro(1, "polia", application="GOL 1.0 1991/2001", created=datetime(2020, 1, 1)),
        fazer_registro(2, "polia", application="GOL 1.0 1991/2001", created=datetime(2021, 1, 1)),
    ]
    pre = DecisaoCampo(campo="name", valor="Polia da Correia", justificativa="x", fonte="verificacao_web")
    decisao = montar_decisao_merge(
        "83061:CITROEN", registros, llm=FakeLLMProvider({}), rule_store=None, brand_id=1,
        decisoes_campo_precalculadas={"name": pre},
    )
    nome = next(dc for dc in decisao.decisoes_campo if dc.campo == "name")
    assert nome.valor == "POLIA DA CORREIA"
    script = gerar_sql([decisao], [])
    assert "'POLIA DA CORREIA'" in script
    assert "Polia da Correia" not in script
