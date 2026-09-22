from tui.swatches import PALETA_PECAS, atribuir_cores


def test_atribui_por_ordem_de_primeira_aparicao():
    cores = atribuir_cores([10, 20, 30])

    assert cores[10] == PALETA_PECAS[0]
    assert cores[20] == PALETA_PECAS[1]
    assert cores[30] == PALETA_PECAS[2]


def test_ids_repetidos_reaproveitam_a_mesma_cor():
    cores = atribuir_cores([10, 20, 10, 20, 10])

    assert cores[10] == PALETA_PECAS[0]
    assert cores[20] == PALETA_PECAS[1]
    assert len(cores) == 2


def test_cicla_apos_esgotar_a_paleta():
    ids = list(range(len(PALETA_PECAS) + 2))

    cores = atribuir_cores(ids)

    assert cores[len(PALETA_PECAS)] == PALETA_PECAS[0]
    assert cores[len(PALETA_PECAS) + 1] == PALETA_PECAS[1]


def test_lista_vazia_retorna_dict_vazio():
    assert atribuir_cores([]) == {}
