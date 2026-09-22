from arbitration.models import DecisaoCampo
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.fk_introspection import FkDependency

_DEPENDENCIAS = [FkDependency(tabela_dependente="catalog_partactivity", coluna_fk="part_id", nome_constraint="fk1")]


def test_merge_completo_segue_a_ordem_esperada():
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=3.2, justificativa="x", fonte="sem_conflito"),
            DecisaoCampo(campo="name", valor="POLIA DA CORREIA", justificativa="y", fonte="julgamento_modelo"),
        ],
    )

    script = gerar_sql([decisao], _DEPENDENCIAS)

    posicao_begin = script.index("BEGIN;")
    posicao_update_vencedor = script.index('UPDATE catalog_part SET "width" = 3.2, "name"')
    posicao_update_fk = script.index('UPDATE "catalog_partactivity"')
    posicao_delete = script.index("DELETE FROM catalog_part")
    posicao_commit = script.index("COMMIT;")

    assert posicao_begin < posicao_update_vencedor < posicao_update_fk < posicao_delete < posicao_commit
    assert "WHERE id = 1;" in script
    assert "IN (2);" in script  # tanto no update de FK quanto no delete


def test_campo_escalado_vira_comentario_e_nao_entra_no_update():
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=None, justificativa="ambíguo", fonte="escalado_humano", escalado_humano=True),
        ],
    )

    script = gerar_sql([decisao], _DEPENDENCIAS)

    assert "-- REVISAR MANUALMENTE: campo width ambíguo — ambíguo" in script
    assert "UPDATE catalog_part SET" not in script  # único campo era escalado -> nada pra atualizar no vencedor


def test_todos_os_campos_escalados_nao_gera_update_do_vencedor():
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=None, justificativa="x", fonte="escalado_humano", escalado_humano=True),
        ],
    )

    script = gerar_sql([decisao], [])

    assert "UPDATE catalog_part SET" not in script
    assert "DELETE FROM catalog_part WHERE id IN (2);" in script


def test_campo_escalado_via_verificacao_web_tambem_vira_comentario():
    # Regressão: verificação web inconclusiva marca escalado_humano=True com
    # fonte="verificacao_web" (não a string "escalado_humano") — gerar_sql precisa
    # checar o booleano, não o fonte, senão gera um UPDATE que zera o campo (bug
    # real encontrado rodando o Caso 4 DRIVEWAY JE4699 de ponta a ponta).
    decisao = DecisaoMerge(
        grupo_ref="JE4699:DRIVEWAY",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(
                campo="name", valor=None, justificativa="orçamento esgotado",
                fonte="verificacao_web", escalado_humano=True,
            ),
        ],
    )

    script = gerar_sql([decisao], _DEPENDENCIAS)

    assert "-- REVISAR MANUALMENTE: campo name ambíguo — orçamento esgotado" in script
    assert "UPDATE catalog_part SET" not in script
    assert '"name" = NULL' not in script


def test_grupo_sinalizado_vira_so_comentario_sem_sql_executavel():
    grupo = GrupoSinalizado(grupo_ref="MB1085:AFFINIA", motivo="similarity_id conflitante")

    script = gerar_sql([grupo], [])

    assert script.strip() == "-- REVISÃO MANUAL: grupo MB1085:AFFINIA não mesclado automaticamente — similarity_id conflitante"
    assert "BEGIN" not in script
    assert "DELETE" not in script


def test_multiplas_decisoes_geram_blocos_separados():
    merge = DecisaoMerge(grupo_ref="A", vencedor_id=1, perdedor_ids=[2], decisoes_campo=[])
    sinalizado = GrupoSinalizado(grupo_ref="B", motivo="x")

    script = gerar_sql([merge, sinalizado], [])

    assert script.count("BEGIN;") == 1
    assert "REVISÃO MANUAL: grupo B" in script
    assert script.index("COMMIT;") < script.index("REVISÃO MANUAL: grupo B")
