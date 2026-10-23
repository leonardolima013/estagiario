from sql_generation.estoque import (
    FILTRO_ESTOQUE_MOVIVEL,
    gerar_consolidacao_estoque,
    gerar_recalculo_has_stock,
)


def _sql(statements):
    return "\n".join(statements)


def test_consolidacao_segue_a_ordem_snapshot_plano_ajustes_inativacao():
    script = _sql(gerar_consolidacao_estoque(vencedor_id=1, perdedor_ids=[2, 3], responsavel_id=8))

    posicoes = [
        script.index("CREATE TEMP TABLE estagiario_estoque ON COMMIT DROP"),
        script.index("CREATE TEMP TABLE estagiario_consolidacao ON COMMIT DROP"),
        script.index("CREATE TEMP TABLE estagiario_consolidado ON COMMIT DROP"),
        script.index("SELECT c.destino_id, 8, r.quantidade - d.quantidade"),
        script.index("UPDATE dealer_stock s SET unit_price = r.unit_price, dealer_code = r.dealer_code"),
        script.index("SELECT k.id, 8, -k.quantidade"),
        script.index('UPDATE "checkout_cartitem" t SET stock_id = k.destino_id'),
        script.index("UPDATE dealer_dealeritemcommercialcondition t SET stock_id = k.destino_id"),
        script.index("UPDATE dealer_stock s SET status = 1, has_stock = false"),
        script.index("UPDATE dealer_stock s SET has_stock ="),
    ]
    assert posicoes == sorted(posicoes)


def test_snapshot_usa_os_ids_do_merge_e_so_estoques_ativos():
    script = _sql(gerar_consolidacao_estoque(vencedor_id=10, perdedor_ids=[20, 30], responsavel_id=8))

    assert "s.part_id = 10 AS do_vencedor" in script
    assert "WHERE s.part_id IN (10, 20, 30) AND s.status = 0;" in script


def test_recencia_e_a_ultima_atividade_com_desempate_pelo_maior_id():
    script = _sql(gerar_consolidacao_estoque(vencedor_id=1, perdedor_ids=[2], responsavel_id=8))

    assert "GREATEST(s.modified, COALESCE(t.ultima_transacao, s.modified)) AS atividade" in script
    # destino prefere o estoque do vencedor; referência é o mais recente de todos
    assert "ORDER BY dealer_id, do_vencedor DESC, atividade DESC, id DESC" in script
    assert "ORDER BY dealer_id, atividade DESC, id DESC" in script
    # conflito: algum estoque de perdedor + outro estoque do grupo no fornecedor
    assert "HAVING bool_or(NOT do_vencedor) AND count(*) >= 2" in script


def test_responsavel_vem_do_parametro():
    script = _sql(gerar_consolidacao_estoque(vencedor_id=1, perdedor_ids=[2], responsavel_id=42))

    assert "responsible_user_id=42" in script
    assert script.count("SELECT c.destino_id, 42,") == 1
    assert script.count("SELECT k.id, 42,") == 1


def test_historico_de_pedidos_e_orcamentos_nao_muda_de_estoque():
    script = _sql(gerar_consolidacao_estoque(vencedor_id=1, perdedor_ids=[2], responsavel_id=8))

    for tabela in ("checkout_cartitem", "dealer_dealerofferitem", "catalog_partwish"):
        assert f'UPDATE "{tabela}" t SET stock_id = k.destino_id' in script
    assert "checkout_orderitem" not in script
    assert "checkout_budgetitem" not in script


def test_condicao_comercial_so_muda_sem_colidir_no_indice_unico():
    script = _sql(gerar_consolidacao_estoque(vencedor_id=1, perdedor_ids=[2], responsavel_id=8))

    assert "o.partner_id IS NOT DISTINCT FROM t.partner_id AND o.state_uf = t.state_uf" in script
    assert "SELECT min(t2.id) FROM dealer_dealeritemcommercialcondition t2" in script


def test_recalculo_has_stock_do_vencedor_e_dos_perdedores():
    statements = gerar_recalculo_has_stock(vencedor_id=1, perdedor_ids=[2, 3])

    assert "WHERE s.part_id = p.id AND s.status = 0" in statements[0]
    assert statements[0].endswith("WHERE p.id = 1;")
    assert statements[1] == "UPDATE catalog_part SET has_stock = false WHERE id IN (2, 3);"


def test_filtro_de_estoque_movivel():
    assert FILTRO_ESTOQUE_MOVIVEL.as_string(None) == '"status" = 0'
