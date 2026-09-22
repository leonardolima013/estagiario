"""T-02 (Lote 0 — rede de segurança): caracteriza a realocação de FK e a ordem
transacional dentro de um bloco de merge, ANTES de qualquer mudança (T-09 é o
que passa a deduplicar).

Documenta o achado F-06: hoje `gerar_updates_fk` emite uma UPDATE por
`FkDependency`, sem deduplicar `(tabela_dependente, coluna_fk)` — duas
constraints apontando a mesma coluna geram duas UPDATEs idênticas.

Também fixa a ordem exigida pela SPEC §4.3 dentro do BEGIN;/COMMIT;:
UPDATE do vencedor -> UPDATEs de FK -> DELETE dos perdedores.
"""

from arbitration.models import DecisaoCampo
from sql_generation.fk_migration import gerar_updates_fk
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge
from tools.fk_introspection import FkDependency


def test_par_tabela_coluna_repetido_agora_deduplicado():
    # T-09: duas constraints distintas na MESMA (tabela, coluna) agora geram 1 UPDATE
    # (antes eram 2 — ver histórico deste teste, que caracterizava a duplicação).
    dependencias = [
        FkDependency(tabela_dependente="catalog_partactivity", coluna_fk="part_id", nome_constraint="fk_a"),
        FkDependency(tabela_dependente="catalog_partactivity", coluna_fk="part_id", nome_constraint="fk_b"),
    ]

    statements = gerar_updates_fk(dependencias, vencedor_id=1, perdedor_ids=[2])

    assert len(statements) == 1


def test_caracteriza_ordem_update_vencedor_fk_delete_dentro_da_transacao():
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[DecisaoCampo(campo="width", valor=3.2, justificativa="x", fonte="sem_conflito")],
    )
    dependencias = [FkDependency(tabela_dependente="catalog_partactivity", coluna_fk="part_id", nome_constraint="fk")]

    script = gerar_sql([decisao], dependencias)

    pos_begin = script.index("BEGIN;")
    pos_update_vencedor = script.index("UPDATE catalog_part SET")
    pos_update_fk = script.index('UPDATE "catalog_partactivity"')
    pos_delete = script.index("DELETE FROM catalog_part")
    pos_commit = script.index("COMMIT;")

    assert pos_begin < pos_update_vencedor < pos_update_fk < pos_delete < pos_commit


def test_caracteriza_fk_updates_presentes_mesmo_sem_update_do_vencedor():
    # Todos os campos escalados -> sem UPDATE do vencedor, mas FK + DELETE seguem.
    decisao = DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=None, justificativa="x", fonte="escalado_humano", escalado_humano=True)
        ],
    )
    dependencias = [FkDependency(tabela_dependente="t", coluna_fk="c", nome_constraint="fk")]

    script = gerar_sql([decisao], dependencias)

    assert "UPDATE catalog_part SET" not in script
    assert 'UPDATE "t" SET "c" = 1 WHERE "c" IN (2);' in script
    assert "DELETE FROM catalog_part WHERE id IN (2);" in script
