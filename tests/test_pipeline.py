from loop.tracing import TraceCollector
from pipeline import ResultadoCaso, executar_caso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tests.fakes import FakeVerificadorWeb
from tools.fk_introspection import FkDependency
from verification.models import ResultadoVerificacao
from memory.models import RegraProposta, RespostaIntervencao

# POLIA/POLIA DA CORREIA (usados nos fixtures abaixo) divergem de verdade
# (verification.divergencia) — estes testes focam noutros aspectos do pipeline,
# não no resultado da verificação web em si, então injetam um fake
# sempre-confirmado pra não bater em rede/API real.
_FAKE_WEB_CONFIRMADO = FakeVerificadorWeb(
    ResultadoVerificacao(status="confirmado", nome_sugerido="POLIA DA CORREIA", justificativa="ok", fontes=[])
)


class FakeLLMParticaoEArbitragem:
    """Fake que responde tanto à chamada de particionamento (Fase 1) quanto às
    chamadas de arbitragem (Fase 2 — só o juiz de nome, já que os demais campos
    são sem-conflito ou derivados de application nos registros de teste)."""

    def __init__(self, ids_grupo: list[int]):
        self._ids_grupo = ids_grupo

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        if schema_name == "particao":
            return {
                "subclusters": [
                    {"label": "duplicata_real", "membro_ids": self._ids_grupo, "justificativa": "mesma peça"}
                ]
            }
        if schema_name == "decisao_nome":
            return {"modo": "escolher", "part_id_escolhido": self._ids_grupo[0], "justificativa": "ok"}
        raise AssertionError(f"chamada inesperada: schema_name={schema_name!r}")


def _buscar_grupo_fake(fazer_registro):
    def _fn(search_ref, brand_id):
        return [
            fazer_registro(1, "POLIA", application="GOL 1.0 1991/2001", search_ref=search_ref, brand_id=brand_id),
            fazer_registro(
                2, "POLIA DA CORREIA", application="GOL 1.0 1991/2001", search_ref=search_ref, brand_id=brand_id
            ),
        ]

    return _fn


def test_executar_caso_gera_resultado_com_sql(fazer_registro, sem_banco):
    llm = FakeLLMParticaoEArbitragem(ids_grupo=[1, 2])

    resultado = executar_caso(
        "83061", 1, llm=llm, dependencias_fk=[], buscar_grupo=_buscar_grupo_fake(fazer_registro),
        verificar_web=_FAKE_WEB_CONFIRMADO,
    )

    assert isinstance(resultado, ResultadoCaso)
    assert len(resultado.particao.subclusters) == 1
    assert len(resultado.decisoes) == 1
    assert isinstance(resultado.decisoes[0], DecisaoMerge)
    assert "DELETE FROM catalog_part" in resultado.sql
    assert [r.id for r in resultado.grupo] == [1, 2]


def test_executar_caso_sem_duplicata_real_retorna_sql_vazio(fazer_registro, sem_banco):
    class FakeLLMSoDistintos:
        def gerar_json(self, system, user, json_schema, schema_name="output"):
            return {
                "subclusters": [
                    {"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "x"},
                    {"label": "distinto_nao_classificado", "membro_ids": [2], "justificativa": "y"},
                ]
            }

    resultado = executar_caso(
        "83061", 1, llm=FakeLLMSoDistintos(), dependencias_fk=[], buscar_grupo=_buscar_grupo_fake(fazer_registro),
        verificar_web=FakeVerificadorWeb(
            ResultadoVerificacao(
                status="inconclusivo",
                nome_sugerido=None,
                justificativa="sem confirmação",
                fontes=[],
            )
        ),
    )

    assert len(resultado.decisoes) == 1
    assert isinstance(resultado.decisoes[0], GrupoSinalizado)
    assert resultado.decisoes[0].membro_ids == [1, 2]
    assert "DELETE" not in resultado.sql
    assert "BEGIN" not in resultado.sql


def test_executar_caso_usa_fk_dependencias_fornecidas(fazer_registro, sem_banco):
    llm = FakeLLMParticaoEArbitragem(ids_grupo=[1, 2])
    dependencias = [FkDependency(tabela_dependente="catalog_partactivity", coluna_fk="part_id", nome_constraint="fk")]

    resultado = executar_caso(
        "83061", 1, llm=llm, dependencias_fk=dependencias, buscar_grupo=_buscar_grupo_fake(fazer_registro),
        verificar_web=_FAKE_WEB_CONFIRMADO,
    )

    assert 'UPDATE "catalog_partactivity"' in resultado.sql


def test_executar_caso_pausa_e_retorna_decisao_da_intervencao(fazer_registro, sem_banco, tmp_path):
    class FakeLLMDivergente(FakeLLMParticaoEArbitragem):
        pass

    def buscar_grupo(search_ref, brand_id):
        return [
            fazer_registro(1, "PIVO SUPERIOR", search_ref=search_ref, brand_id=brand_id, application="X 1991/2001"),
            fazer_registro(2, "PIVO INFERIOR", search_ref=search_ref, brand_id=brand_id, application="X 1991/2001"),
        ]

    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return RespostaIntervencao(
            resposta_humana="A nomenclatura correta para este caso é inferior.",
            regra=RegraProposta(
                titulo="Preferir posição inferior",
                condicao="Quando o conflito de nomenclatura entre superior e inferior não é resolvido pela web",
                resolucao="Escolher o nome inferior confirmado pelo contexto do catálogo",
            ),
            valor="PIVO INFERIOR",
            origem_id=2,
            criado_por="teste",
        )

    resultado = executar_caso(
        "TESTE", 1, llm=FakeLLMDivergente([1, 2]), dependencias_fk=[],
        rule_store=__import__("db.rule_store", fromlist=["RuleStore"]).RuleStore(tmp_path / "memoria.db"),
        buscar_grupo=buscar_grupo, verificar_web=FakeVerificadorWeb(
            ResultadoVerificacao(status="inconclusivo", nome_sugerido=None, justificativa="sem confirmação", fontes=[])
        ), pedir_intervencao=pedir,
    )

    assert len(pedidos) == 1
    assert isinstance(resultado.decisoes[0], DecisaoMerge)
    decisao_nome = next(dc for dc in resultado.decisoes[0].decisoes_campo if dc.campo == "name")
    assert decisao_nome.fonte == "intervencao_humana"
    assert decisao_nome.valor == "PIVO INFERIOR"


def test_executar_caso_emite_trace_resumido(fazer_registro, sem_banco):
    trace = TraceCollector()
    resultado = executar_caso(
        "83061", 1, llm=FakeLLMParticaoEArbitragem([1, 2]), dependencias_fk=[],
        buscar_grupo=_buscar_grupo_fake(fazer_registro), verificar_web=_FAKE_WEB_CONFIRMADO,
        trace=trace,
    )

    nomes = [evento.nome for evento in trace.eventos()]
    assert nomes[:2] == ["buscar_grupo", "particionar_grupo"]
    assert "montar_decisao_merge" in nomes
    assert "arbitrar_campo" in nomes
    assert "gerar_sql" in nomes
    assert all("prompt" not in evento.detalhes for evento in trace.eventos())
    assert resultado.sql
