"""Consolidação de estoques no merge — pura, só monta texto SQL (psycopg.sql,
`.as_string(None)`, como fk_migration).

Quando vencedor e perdedor têm estoque (`dealer_stock`) no mesmo fornecedor, a
migração genérica de FK deixaria dois estoques do fornecedor no vencedor — o
backend espera um só (`Stock.get(dealer, part)`; o upload de planilha usa
`.filter(dealer, part).first()`). Regra do time de banco: o registro mais
recente vence, em saldo e preço. Os duplicados repetem o mesmo saldo físico, por
isso não se soma.

Tudo é resolvido na execução do script, não na geração: o script só leva os ids
do merge e lê estoques, saldos e datas dentro da transação, em tabelas
temporárias `ON COMMIT DROP`. Por fornecedor em conflito (estoque de perdedor +
outro estoque do grupo no mesmo fornecedor):

- destino: o estoque do vencedor mais recente no fornecedor; sem estoque do
  vencedor, o do perdedor mais recente (a migração genérica o leva ao vencedor);
- referência: o estoque mais recente do fornecedor entre todos do grupo;
- consolidados: os estoques de perdedores no fornecedor que não são o destino.

O destino recebe uma transação de ajuste até o saldo da referência e copia
preço e `dealer_code` dela; os consolidados são zerados por transação, ficam
`status = 1` e permanecem na peça perdedora (o UPDATE genérico de `dealer_stock`
só move `status = 0`, ver FILTRO_ESTOQUE_MOVIVEL). Recência = maior entre
`modified` e a última transação; empate pelo maior id. Duplicatas que o
vencedor já tinha antes do merge ficam como estão.
"""

from __future__ import annotations

from psycopg import sql

TABELA_ESTOQUE = "dealer_stock"

# Só estoques ativos vão pro vencedor: os consolidados (e os que já estavam
# inativos) ficam no perdedor, sem duplicar (fornecedor, vencedor).
FILTRO_ESTOQUE_MOVIVEL = sql.SQL("{} = 0").format(sql.Identifier("status"))

_SNAPSHOT = """
CREATE TEMP TABLE estagiario_estoque ON COMMIT DROP AS
SELECT s.id, s.dealer_id, s.part_id = {vencedor} AS do_vencedor, s.unit_price, s.dealer_code,
    COALESCE(t.quantidade, 0) AS quantidade,
    GREATEST(s.modified, COALESCE(t.ultima_transacao, s.modified)) AS atividade
FROM dealer_stock s
LEFT JOIN LATERAL (
    SELECT sum(x.quantity) AS quantidade, max(x.created) AS ultima_transacao
    FROM dealer_stocktransaction x WHERE x.stock_id = s.id
) t ON true
WHERE s.part_id IN ({todos}) AND s.status = 0;
"""

_PLANO = """
CREATE TEMP TABLE estagiario_consolidacao ON COMMIT DROP AS
SELECT d.dealer_id, d.id AS destino_id, r.id AS referencia_id
FROM (
    SELECT DISTINCT ON (dealer_id) dealer_id, id FROM estagiario_estoque
    ORDER BY dealer_id, do_vencedor DESC, atividade DESC, id DESC
) d
JOIN (
    SELECT DISTINCT ON (dealer_id) dealer_id, id FROM estagiario_estoque
    ORDER BY dealer_id, atividade DESC, id DESC
) r USING (dealer_id)
WHERE d.dealer_id IN (
    SELECT dealer_id FROM estagiario_estoque
    GROUP BY dealer_id
    HAVING bool_or(NOT do_vencedor) AND count(*) >= 2
);
"""

_CONSOLIDADOS = """
CREATE TEMP TABLE estagiario_consolidado ON COMMIT DROP AS
SELECT e.id, c.destino_id, e.quantidade, e.unit_price
FROM estagiario_estoque e
JOIN estagiario_consolidacao c USING (dealer_id)
WHERE NOT e.do_vencedor AND e.id <> c.destino_id;
"""

_AJUSTE_DESTINO = """
INSERT INTO dealer_stocktransaction (stock_id, responsible_user_id, quantity, created, unit_price)
SELECT c.destino_id, {responsavel}, r.quantidade - d.quantidade, now(), r.unit_price
FROM estagiario_consolidacao c
JOIN estagiario_estoque d ON d.id = c.destino_id
JOIN estagiario_estoque r ON r.id = c.referencia_id
WHERE c.referencia_id <> c.destino_id AND r.quantidade <> d.quantidade;
"""

_DADOS_DESTINO = """
UPDATE dealer_stock s SET unit_price = r.unit_price, dealer_code = r.dealer_code, modified = now()
FROM estagiario_consolidacao c
JOIN estagiario_estoque r ON r.id = c.referencia_id
WHERE s.id = c.destino_id AND c.referencia_id <> c.destino_id;
"""

_ZERAR_CONSOLIDADOS = """
INSERT INTO dealer_stocktransaction (stock_id, responsible_user_id, quantity, created, unit_price)
SELECT k.id, {responsavel}, -k.quantidade, now(), k.unit_price
FROM estagiario_consolidado k
WHERE k.quantidade <> 0;
"""

# Referências "vivas" seguem o estoque que fica; pedidos e orçamentos
# (histórico) continuam apontando pro estoque consolidado.
_TABELAS_REFERENCIA_VIVA = ("checkout_cartitem", "dealer_dealerofferitem", "catalog_partwish")

_MOVER_REFERENCIA = """
UPDATE {tabela} t SET stock_id = k.destino_id
FROM estagiario_consolidado k
WHERE t.stock_id = k.id;
"""

# Índices únicos parciais (dealer, partner, stock, state_uf): uma condição só
# muda de estoque quando o destino ainda não tem uma equivalente, e só uma por
# chave quando dois consolidados levam condições iguais ao mesmo destino.
_MOVER_CONDICAO_COMERCIAL = """
UPDATE dealer_dealeritemcommercialcondition t SET stock_id = k.destino_id
FROM estagiario_consolidado k
WHERE t.stock_id = k.id
    AND NOT EXISTS (
        SELECT 1 FROM dealer_dealeritemcommercialcondition o
        WHERE o.stock_id = k.destino_id AND o.dealer_id = t.dealer_id
            AND o.partner_id IS NOT DISTINCT FROM t.partner_id AND o.state_uf = t.state_uf
    )
    AND t.id = (
        SELECT min(t2.id) FROM dealer_dealeritemcommercialcondition t2
        JOIN estagiario_consolidado k2 ON k2.id = t2.stock_id
        WHERE k2.destino_id = k.destino_id AND t2.dealer_id = t.dealer_id
            AND t2.partner_id IS NOT DISTINCT FROM t.partner_id AND t2.state_uf = t.state_uf
    );
"""

_INATIVAR_CONSOLIDADOS = """
UPDATE dealer_stock s SET status = 1, has_stock = false, modified = now()
FROM estagiario_consolidado k
WHERE s.id = k.id;
"""

_SALDO_ESTOQUE = "COALESCE((SELECT sum(x.quantity) FROM dealer_stocktransaction x WHERE x.stock_id = s.id), 0)"

_HAS_STOCK_DESTINO = f"""
UPDATE dealer_stock s SET has_stock = {_SALDO_ESTOQUE} > 0
WHERE s.id IN (SELECT destino_id FROM estagiario_consolidacao);
"""

# Mesma regra do check_has_stock_part do backend: algum estoque ativo com saldo.
_HAS_STOCK_VENCEDOR = f"""
UPDATE catalog_part p SET has_stock = EXISTS (
    SELECT 1 FROM dealer_stock s
    WHERE s.part_id = p.id AND s.status = 0 AND {_SALDO_ESTOQUE} > 0
)
WHERE p.id = {{vencedor}};
"""

_HAS_STOCK_PERDEDORES = "UPDATE catalog_part SET has_stock = false WHERE id IN ({perdedores});"


def _lista(ids: list[int]) -> sql.Composable:
    return sql.SQL(", ").join(sql.Literal(i) for i in ids)


def _montar(modelo: str, **valores: sql.Composable) -> str:
    return sql.SQL(modelo.strip()).format(**valores).as_string(None)


def gerar_consolidacao_estoque(vencedor_id: int, perdedor_ids: list[int], responsavel_id: int) -> list[str]:
    """Statements que resolvem os conflitos de estoque por fornecedor.

    Vão antes da migração genérica de FK: o destino sem estoque do vencedor
    ainda está no perdedor e é movido por ela.
    """
    vencedor = sql.Literal(vencedor_id)
    responsavel = sql.Literal(responsavel_id)
    statements = [
        f"-- ESTOQUE: conflitos por fornecedor resolvidos na execução; registro mais recente vence; "
        f"ajustes por responsible_user_id={responsavel_id}",
        _montar(_SNAPSHOT, vencedor=vencedor, todos=_lista([vencedor_id, *perdedor_ids])),
        _montar(_PLANO),
        _montar(_CONSOLIDADOS),
        _montar(_AJUSTE_DESTINO, responsavel=responsavel),
        _montar(_DADOS_DESTINO),
        _montar(_ZERAR_CONSOLIDADOS, responsavel=responsavel),
    ]
    statements += [_montar(_MOVER_REFERENCIA, tabela=sql.Identifier(t)) for t in _TABELAS_REFERENCIA_VIVA]
    statements += [
        _montar(_MOVER_CONDICAO_COMERCIAL),
        _montar(_INATIVAR_CONSOLIDADOS),
        _montar(_HAS_STOCK_DESTINO),
    ]
    return statements


def gerar_recalculo_has_stock(vencedor_id: int, perdedor_ids: list[int]) -> list[str]:
    """`catalog_part.has_stock` depois da migração de FK (estoques já no vencedor)."""
    return [
        _montar(_HAS_STOCK_VENCEDOR, vencedor=sql.Literal(vencedor_id)),
        _montar(_HAS_STOCK_PERDEDORES, perdedores=_lista(perdedor_ids)),
    ]
