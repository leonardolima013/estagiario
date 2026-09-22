from datetime import datetime

from sql_generation.vencedor import escolher_vencedor


def test_mais_completo_vence(fazer_registro):
    pobre = fazer_registro(1, "X", created=datetime(2020, 1, 1))
    rico = fazer_registro(2, "Y", width=3.2, depth=1.0, application="ALGO", created=datetime(2021, 1, 1))

    assert escolher_vencedor([pobre, rico]).id == 2


def test_empate_de_completude_cai_pro_mais_antigo(fazer_registro):
    a = fazer_registro(1, "X", width=3.2, created=datetime(2021, 1, 1))
    b = fazer_registro(2, "Y", width=1.0, created=datetime(2019, 6, 1))

    assert escolher_vencedor([a, b]).id == 2


def test_completude_ignora_campos_derivados_e_agrupamento(fazer_registro):
    # born_at/deprecated_at (derivados) e similarity_id (agrupamento) não contam pra completude.
    a = fazer_registro(1, "X", born_at=1990, deprecated_at=2020, similarity_id=42, created=datetime(2020, 1, 1))
    b = fazer_registro(2, "Y", created=datetime(2019, 1, 1))

    # Mesma completude (zero campos de conteúdo) -> desempata por idade -> b (mais antigo).
    assert escolher_vencedor([a, b]).id == 2


def test_ordem_estavel_em_empate_total(fazer_registro):
    mesma_data = datetime(2020, 1, 1)
    a = fazer_registro(1, "X", created=mesma_data)
    b = fazer_registro(2, "Y", created=mesma_data)

    assert escolher_vencedor([a, b]).id == 1
    assert escolher_vencedor([b, a]).id == 2
