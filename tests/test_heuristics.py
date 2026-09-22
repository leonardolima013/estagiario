from partitioning.heuristics import (
    sinal_item_distinto,
    sinal_kit_componente,
    sinal_variante_dimensional,
)


def test_sinal_variante_dimensional_std():
    assert sinal_variante_dimensional("KIT DE BRONZINAS CENTRAIS STD") == "STD"


def test_sinal_variante_dimensional_sobremedida():
    assert sinal_variante_dimensional("KIT DE BRONZINAS CENTRAIS 0,25") == "0,25"
    assert sinal_variante_dimensional("KIT DE BRONZINAS CENTRAIS 0,50") == "0,50"


def test_sinal_variante_dimensional_ausente():
    assert sinal_variante_dimensional("POLIA DA CORREIA DENTADA") is None


def test_sinal_kit_componente_positivo():
    assert sinal_kit_componente(
        "KIT DA CORREIA SINCRONIZADORA",
        "POLIA DA CORREIA SINCRONIZADORA",
    )


def test_sinal_kit_componente_e_simetrico():
    assert sinal_kit_componente(
        "POLIA DA CORREIA SINCRONIZADORA",
        "KIT DA CORREIA SINCRONIZADORA",
    )


def test_sinal_kit_componente_negativo():
    assert not sinal_kit_componente("POLIA", "DESVIO-DISTRIBUIÇÃO")


def test_sinal_kit_componente_sem_kit_no_nome():
    assert not sinal_kit_componente("POLIA DA CORREIA DENTADA", "POLIA")


def test_sinal_item_distinto_acima_do_threshold():
    assert sinal_item_distinto(3.2, 2.54, threshold=0.15)


def test_sinal_item_distinto_abaixo_do_threshold():
    assert not sinal_item_distinto(3.20, 3.21, threshold=0.15)


def test_sinal_item_distinto_sem_dado():
    assert not sinal_item_distinto(None, 3.2, threshold=0.15)
    assert not sinal_item_distinto(3.2, None, threshold=0.15)


def test_sinal_item_distinto_ambos_zero():
    assert not sinal_item_distinto(0.0, 0.0, threshold=0.15)
