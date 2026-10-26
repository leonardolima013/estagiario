import pytest

from arbitration.models import DecisaoCampo
from scripts.gerar_correcao_atividade_merge import gerar_correcao, ler_merges
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge, GrupoSinalizado


def test_le_os_merges_do_script_gerado_pela_pipeline():
    script = gerar_sql(
        [
            DecisaoMerge(
                grupo_ref="A:X", vencedor_id=1, perdedor_ids=[2, 3],
                decisoes_campo=[DecisaoCampo(campo="width", valor=3.2, justificativa="x", fonte="sem_conflito")],
            ),
            GrupoSinalizado(grupo_ref="B:Y", motivo="similarity_id conflitante"),
            DecisaoMerge(grupo_ref="C:Z", vencedor_id=10, perdedor_ids=[11], decisoes_campo=[]),
        ],
        [],
    )

    assert ler_merges(script) == [("A:X", 1, [2, 3]), ("C:Z", 10, [11])]


def test_merge_sem_inativacao_falha():
    with pytest.raises(ValueError, match="merge sem inativação"):
        ler_merges("-- Grupo A:X: merge [2] -> 1\nBEGIN;\nCOMMIT;\n")


def test_peca_repetida_entre_merges_falha():
    script = gerar_sql(
        [
            DecisaoMerge(grupo_ref="A", vencedor_id=1, perdedor_ids=[2], decisoes_campo=[]),
            DecisaoMerge(grupo_ref="B", vencedor_id=3, perdedor_ids=[2], decisoes_campo=[]),
        ],
        [],
    )
    with pytest.raises(ValueError, match="mais de um merge"):
        ler_merges(script)


def test_correcao_escapa_referencias_e_usa_o_autor():
    texto = gerar_correcao([("O'NEIL:X", 1, [2, 3])], 77, "origem.sql", "abc")

    assert "(1, ARRAY[2, 3]::integer[], 'Unificação de duplicatas (IDs mesclados: [2, 3])', 'O''NEIL:X')" in texto
    assert "now(), 77" in texto
    assert texto.rstrip().endswith("COMMIT;")
