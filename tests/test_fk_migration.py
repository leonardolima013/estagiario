from sql_generation.fk_migration import gerar_updates_fk
from tools.fk_introspection import FkDependency, IndiceUnico, anexar_indices_unicos


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


def test_tabela_com_indice_unico_ganha_delete_de_duplicatas_antes_do_update():
    # Execução real do log-200: catalog_fit (part_id, version_id) violou o índice
    # quando vencedor e perdedor já serviam à mesma versão.
    indice = IndiceUnico(nome="catalog_fit_uniq", colunas=("part_id", "version_id"))
    dependencias = [
        FkDependency("catalog_fit", "part_id", "fk", indices_unicos=(indice,)),
        FkDependency("catalog_partactivity", "part_id", "fk2"),
    ]

    statements = gerar_updates_fk(dependencias, vencedor_id=1, perdedor_ids=[2, 3])

    assert statements == [
        'DELETE FROM "catalog_fit" WHERE ctid IN (SELECT ctid FROM (SELECT ctid, row_number() OVER '
        '(PARTITION BY CASE WHEN "part_id" IN (2, 3) THEN 1 ELSE "part_id" END, "version_id" '
        'ORDER BY CASE WHEN "part_id" IN (2, 3) THEN 1 ELSE 0 END, ctid) AS rn FROM "catalog_fit" '
        'WHERE ("part_id" IN (1, 2, 3)) AND "part_id" IS NOT NULL AND "version_id" IS NOT NULL) '
        "AS dup WHERE dup.rn > 1);",
        'UPDATE "catalog_fit" SET "part_id" = 1 WHERE "part_id" IN (2, 3);',
        'UPDATE "catalog_partactivity" SET "part_id" = 1 WHERE "part_id" IN (2, 3);',
    ]


def test_indice_com_duas_fks_gera_um_delete_so_antes_do_primeiro_update():
    # catalog_partrelationship: part_id e related_part_id no mesmo índice parcial.
    indice = IndiceUnico(
        nome="rel_sem_provider",
        colunas=("part_id", "related_part_id", "tipo_id"),
        predicado="(information_provider_id IS NULL)",
    )
    dependencias = [
        FkDependency("rel", "part_id", "fk1", indices_unicos=(indice,)),
        FkDependency("rel", "related_part_id", "fk2", indices_unicos=(indice,)),
    ]

    statements = gerar_updates_fk(dependencias, vencedor_id=1, perdedor_ids=[2])

    assert [s.split(" ")[0] for s in statements] == ["DELETE", "UPDATE", "UPDATE"]
    delete = statements[0]
    assert 'CASE WHEN "related_part_id" IN (2) THEN 1 ELSE "related_part_id" END' in delete
    assert '("part_id" IN (1, 2) OR "related_part_id" IN (1, 2))' in delete
    assert "((information_provider_id IS NULL))" in delete


def test_anexar_indices_unicos_filtra_por_coluna_e_ignora_expressao():
    dependencias = [
        FkDependency("rel", "part_id", "fk1"),
        FkDependency("rel", "related_part_id", "fk2"),
        FkDependency("act", "part_id", "fk3"),
    ]
    linhas = [
        ("rel", "so_part", ["part_id", "tipo"], None, False),
        ("rel", "ambas", ["part_id", "related_part_id"], "(x IS NULL)", False),
        ("act", "expr", ["part_id", None], None, True),
    ]

    resultado = anexar_indices_unicos(dependencias, linhas)

    assert [i.nome for i in resultado[0].indices_unicos] == ["so_part", "ambas"]
    assert [i.nome for i in resultado[1].indices_unicos] == ["ambas"]
    assert resultado[1].indices_unicos[0].predicado == "(x IS NULL)"
    assert resultado[2].indices_unicos == ()


def test_filtro_extra_entra_so_no_update_da_tabela_indicada():
    from psycopg import sql

    dependencias = [
        FkDependency(tabela_dependente="dealer_stock", coluna_fk="part_id", nome_constraint="fk1"),
        FkDependency(tabela_dependente="catalog_partowner", coluna_fk="part_id", nome_constraint="fk2"),
    ]

    statements = gerar_updates_fk(
        dependencias, vencedor_id=1, perdedor_ids=[2],
        filtros_extras={"dealer_stock": sql.SQL('"status" = 0')},
    )

    assert statements == [
        'UPDATE "dealer_stock" SET "part_id" = 1 WHERE "part_id" IN (2) AND "status" = 0;',
        'UPDATE "catalog_partowner" SET "part_id" = 1 WHERE "part_id" IN (2);',
    ]
