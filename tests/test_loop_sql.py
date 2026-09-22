from loop.models import FamiliaSorteada, IteracaoStatus, RegistroIteracao
from loop.sql_saida import gerar_sql_loop, salvar_sql_atomico, sql_da_iteracao


def test_sql_inclui_contexto_da_iteracao_e_merge():
    iteracao = RegistroIteracao(
        indice=1,
        status=IteracaoStatus.SUCESSO,
        iniciada_em="a",
        familia=FamiliaSorteada("83061", 1, "CITROEN"),
        sql="BEGIN;\nDELETE FROM catalog_part WHERE id IN (2);\nCOMMIT;",
    )

    sql = sql_da_iteracao(iteracao, 3)

    assert "-- ITERAÇÃO 1/3" in sql
    assert "search_ref=83061" in sql
    assert "brand_id=1" in sql
    assert "BEGIN;" in sql
    assert "DELETE FROM catalog_part" in sql


def test_sql_de_erro_e_somente_comentario():
    iteracao = RegistroIteracao(
        indice=2,
        status=IteracaoStatus.ERRO,
        iniciada_em="a",
        familia=FamiliaSorteada("X", 2, "M"),
        erro={"tipo": "TimeoutError", "mensagem": "banco indisponível"},
    )

    sql = sql_da_iteracao(iteracao, 3)

    assert "-- ITERAÇÃO 2/3" in sql
    assert "TimeoutError" in sql
    assert "BEGIN;" not in sql
    assert "DELETE FROM" not in sql


def test_sql_de_grupo_sinalizado_preserva_apenas_comentarios():
    iteracao = RegistroIteracao(
        indice=1,
        status=IteracaoStatus.SUCESSO,
        iniciada_em="a",
        familia=FamiliaSorteada("X", 2, "M"),
        sql="-- REVISÃO MANUAL: grupo X:M não mesclado automaticamente",
    )

    sql = gerar_sql_loop([iteracao], 1)

    assert "REVISÃO MANUAL" in sql
    assert "BEGIN;" not in sql
    assert "DELETE FROM" not in sql


def test_salvar_sql_atomico_promove_arquivo_final(tmp_path):
    caminho = tmp_path / "loop.sql"
    salvar_sql_atomico(caminho, "-- teste\n")

    assert caminho.read_text(encoding="utf-8") == "-- teste\n"
    assert not (tmp_path / "loop.sql.tmp").exists()
