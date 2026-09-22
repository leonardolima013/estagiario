from sql_generation.fk_migration import gerar_updates_fk
from tools.fk_introspection import FkDependency


def test_gera_um_update_por_dependencia():
    dependencias = [
        FkDependency(tabela_dependente="catalog_partactivity", coluna_fk="part_id", nome_constraint="fk1"),
        FkDependency(tabela_dependente="catalog_partowner", coluna_fk="part_id", nome_constraint="fk2"),
    ]

    statements = gerar_updates_fk(dependencias, vencedor_id=100, perdedor_ids=[200])

    assert statements == [
        'UPDATE "catalog_partactivity" SET "part_id" = 100 WHERE "part_id" IN (200);',
        'UPDATE "catalog_partowner" SET "part_id" = 100 WHERE "part_id" IN (200);',
    ]


def test_multiplos_perdedores_no_in():
    dependencias = [FkDependency(tabela_dependente="t", coluna_fk="c", nome_constraint="fk")]

    statements = gerar_updates_fk(dependencias, vencedor_id=1, perdedor_ids=[2, 3, 4])

    assert statements == ['UPDATE "t" SET "c" = 1 WHERE "c" IN (2, 3, 4);']


def test_sem_dependencias_gera_lista_vazia():
    assert gerar_updates_fk([], vencedor_id=1, perdedor_ids=[2]) == []


def test_identificadores_sao_escapados_com_aspas_duplas():
    # Nome de tabela/coluna hipotético com aspas embutidas — nunca deveria vir assim de
    # introspeccao_fk na prática, mas a função não deve confiar cegamente no texto.
    dependencias = [FkDependency(tabela_dependente='ta"bela', coluna_fk="col", nome_constraint="fk")]

    statements = gerar_updates_fk(dependencias, vencedor_id=1, perdedor_ids=[2])

    assert '"ta""bela"' in statements[0]


def test_dedupe_por_tabela_coluna_preservando_ordem():
    # F-06/T-09: constraints distintas na mesma (tabela, coluna) colapsam em 1 UPDATE;
    # colunas distintas na mesma tabela são mantidas; ordem de primeira aparição preservada.
    dependencias = [
        FkDependency(tabela_dependente="t_a", coluna_fk="c1", nome_constraint="fk1"),
        FkDependency(tabela_dependente="t_a", coluna_fk="c1", nome_constraint="fk2"),  # dup exata
        FkDependency(tabela_dependente="t_a", coluna_fk="c2", nome_constraint="fk3"),  # mesma tabela, outra coluna
        FkDependency(tabela_dependente="t_b", coluna_fk="c1", nome_constraint="fk4"),
    ]

    statements = gerar_updates_fk(dependencias, vencedor_id=1, perdedor_ids=[2])

    assert statements == [
        'UPDATE "t_a" SET "c1" = 1 WHERE "c1" IN (2);',
        'UPDATE "t_a" SET "c2" = 1 WHERE "c2" IN (2);',
        'UPDATE "t_b" SET "c1" = 1 WHERE "c1" IN (2);',
    ]
