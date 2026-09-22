from datetime import datetime

import pytest

from db.rule_store import RuleStore

from sql_generation.models import DecisaoMerge, GrupoSinalizado
from sql_generation.montar_decisao import montar_decisao_merge
from tests.fakes import FakeLLMProvider, FakeVerificadorWeb
from verification.models import ResultadoVerificacao
from memory.models import RegraProposta, RespostaIntervencao


def _fake_web_confirmado(nome: str) -> FakeVerificadorWeb:
    """POLIA/POLIA DA CORREIA divergem de verdade (verification.divergencia) — esses
    testes focam noutros aspectos do merge, não no resultado da verificação web em
    si, então injetam um fake sempre-confirmado pra não bater em rede/API real."""
    return FakeVerificadorWeb(
        ResultadoVerificacao(status="confirmado", nome_sugerido=nome, justificativa="ok", fontes=[])
    )


class FakeLLMGenerico:
    """Responde de forma plausível pra qualquer campo textual (name) sem se importar
    com o conteúdo exato — usado quando o teste não foca no resultado da arbitragem."""

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        if schema_name == "decisao_nome":
            return {"modo": "escolher", "part_id_escolhido": self._qualquer_id, "justificativa": "ok"}
        raise AssertionError(f"chamada inesperada ao LLM: schema_name={schema_name!r}")

    def __init__(self, qualquer_id: int):
        self._qualquer_id = qualquer_id


class FakeLLMIntervencao(FakeLLMGenerico):
    def gerar_json(self, system, user, json_schema, schema_name="output"):
        if schema_name == "decisao_similarity_id":
            return {"acao": "permitir_merge", "confianca": "alta", "justificativa": "regra se aplica"}
        return super().gerar_json(system, user, json_schema, schema_name)


def test_caminho_feliz_gera_decisao_merge(fazer_registro, sem_banco):
    # Sem divergência em nenhum campo numérico -> arbitrar_campo nunca bate no banco.
    registros = [
        fazer_registro(1, "POLIA", application="GOL 1.0 1991/2001", created=datetime(2020, 1, 1)),
        fazer_registro(2, "POLIA DA CORREIA", application="GOL 1.0 1991/2001", created=datetime(2021, 1, 1)),
    ]
    llm = FakeLLMGenerico(qualquer_id=2)

    decisao = montar_decisao_merge(
        "83061:CITROEN", registros, llm=llm, rule_store=None, brand_id=1,
        verificar_web=_fake_web_confirmado("POLIA DA CORREIA"),
    )

    assert isinstance(decisao, DecisaoMerge)
    assert decisao.vencedor_id == 1  # completude igual (application preenchida nos dois) -> mais antigo
    assert decisao.perdedor_ids == [2]
    campos_decididos = {dc.campo for dc in decisao.decisoes_campo}
    assert "born_at" in campos_decididos and "name" in campos_decididos


def test_similarity_ids_conflitantes_gera_grupo_sinalizado(fazer_registro):
    registros = [
        fazer_registro(1, "POLIA", similarity_id=10),
        fazer_registro(2, "POLIA DA CORREIA", similarity_id=20),
    ]

    resultado = montar_decisao_merge(
        "83061:CITROEN", registros, llm=FakeLLMProvider({}), rule_store=None, brand_id=1
    )

    assert isinstance(resultado, GrupoSinalizado)
    assert "similarity_id" in resultado.motivo
    assert resultado.membro_ids == [1, 2]


def test_similarity_conflict_callback_permite_merge_e_persiste_regra(fazer_registro, sem_banco, tmp_path):
    registros = [
        fazer_registro(1, "POLIA", similarity_id=10, application="X 1991/2001"),
        fazer_registro(2, "POLIA", similarity_id=20, application="X 1991/2001"),
    ]
    store = RuleStore(tmp_path / "memoria.db")
    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return RespostaIntervencao(
            resposta_humana="Os similarity_id antigos estão errados; são a mesma peça.",
            regra=RegraProposta(
                titulo="Similarity divergente não impede duplicata confirmada",
                condicao="Quando similarity_id diverge mas os registros são a mesma peça",
                resolucao="Permitir o merge após confirmar a identidade por nome e contexto",
            ),
            acao="permitir_merge",
            criado_por="operador-teste",
        )

    resultado = montar_decisao_merge(
        "83061:CITROEN", registros, llm=FakeLLMGenerico(1), rule_store=store, brand_id=1,
        pedir_intervencao=pedir,
    )

    assert isinstance(resultado, DecisaoMerge)
    assert pedidos[0].ponto == "particionamento"
    assert store.listar_intervencoes(campo="particionamento")[0].titulo.startswith("Similarity divergente")


def test_memoria_similarity_aplica_sem_nova_intervencao(fazer_registro, sem_banco, tmp_path):
    from db.rule_store import RuleStore

    store = RuleStore(tmp_path / "memoria.db")
    store.registrar_intervencao(
        titulo="Similarity divergente pode ser falso",
        caso_episodico="caso anterior",
        condicao="Quando similarity_id diverge em registros que são a mesma peça",
        resolucao="Permitir merge quando nomes e contexto confirmarem duplicata",
        criado_por="leo",
        sinais_busca="similarity divergente mesma peça permitir merge",
        campo="particionamento",
    )
    registros = [
        fazer_registro(1, "POLIA", similarity_id=10, application="X 1991/2001"),
        fazer_registro(2, "POLIA", similarity_id=20, application="X 1991/2001"),
    ]
    pedidos = []

    resultado = montar_decisao_merge(
        "83061:CITROEN", registros, llm=FakeLLMIntervencao(1), rule_store=store, brand_id=1,
        limiar_intervencao=0.20, pedir_intervencao=lambda pedido: pedidos.append(pedido),
    )

    assert isinstance(resultado, DecisaoMerge)
    assert pedidos == []


def test_um_similarity_id_nulo_nao_conflita(fazer_registro, sem_banco):
    registros = [
        fazer_registro(1, "POLIA", similarity_id=10, application="X 1991/2001"),
        fazer_registro(2, "POLIA DA CORREIA", similarity_id=None, application="X 1991/2001"),
    ]
    llm = FakeLLMGenerico(qualquer_id=1)

    resultado = montar_decisao_merge(
        "83061:CITROEN", registros, llm=llm, rule_store=None, brand_id=1,
        verificar_web=_fake_web_confirmado("POLIA"),
    )

    assert isinstance(resultado, DecisaoMerge)


def test_subcluster_com_um_membro_retorna_none(fazer_registro):
    # Não é erro — a Fase 1 às vezes rotula um item isolado como duplicata_real; não há
    # nada pra mesclar, então não deve gerar SQL nem interromper o pipeline.
    resultado = montar_decisao_merge(
        "83061:CITROEN", [fazer_registro(1, "POLIA")], llm=FakeLLMProvider({}), rule_store=None, brand_id=1
    )

    assert resultado is None


def test_subcluster_vazio_levanta_erro():
    with pytest.raises(ValueError):
        montar_decisao_merge("83061:CITROEN", [], llm=FakeLLMProvider({}), rule_store=None, brand_id=1)


def test_montar_decisao_preenche_snapshot_do_vencedor(fazer_registro, sem_banco):
    # T-04: DecisaoMerge deve carregar o snapshot dos valores atuais do vencedor,
    # pra gerar_sql conseguir fazer o UPDATE diff-only (Q-02=a).
    registros = [
        fazer_registro(1, "POLIA", width=3.2, application="GOL 1.0 1991/2001", created=datetime(2020, 1, 1)),
        fazer_registro(2, "POLIA DA CORREIA", application="GOL 1.0 1991/2001", created=datetime(2021, 1, 1)),
    ]
    llm = FakeLLMGenerico(qualquer_id=1)

    decisao = montar_decisao_merge(
        "83061:CITROEN", registros, llm=llm, rule_store=None, brand_id=1,
        verificar_web=_fake_web_confirmado("POLIA"),
    )

    assert isinstance(decisao, DecisaoMerge)
    # vencedor é o id=1 (mais completo: width+application). Snapshot deve refletir os
    # valores dele, por campo decidido.
    assert decisao.vencedor_id == 1
    assert decisao.valores_atuais_vencedor["width"] == 3.2
    # campo cujo valor decidido == valor atual do vencedor não deve gerar SET no SQL final.
    from sql_generation.gerar_sql import gerar_sql

    script = gerar_sql([decisao], [])
    # width do vencedor é 3.2 e não diverge -> não reescrito.
    assert '"width"' not in script
