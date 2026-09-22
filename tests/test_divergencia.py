from verification.divergencia import nomes_normalizados_divergem


def test_nomes_identicos_nao_divergem():
    assert nomes_normalizados_divergem(["POLIA", "POLIA"]) is False


def test_diferenca_de_caixa_nao_diverge():
    assert nomes_normalizados_divergem(["polia da correia", "POLIA DA CORREIA"]) is False


def test_diferenca_de_espacamento_nao_diverge():
    assert nomes_normalizados_divergem(["POLIA  DA CORREIA", "POLIA DA CORREIA "]) is False


def test_diferenca_de_acento_nao_diverge():
    assert nomes_normalizados_divergem(["PIVO DE SUSPENSAO", "PIVÔ DE SUSPENSÃO"]) is False


def test_nome_mais_detalhado_e_completude_nao_conflito():
    # "24V MTE" é detalhe extra, nenhuma palavra do nome curto conflita com o longo —
    # o juiz de qualidade textual interno resolve isso, não precisa de verificação web.
    assert nomes_normalizados_divergem(["PLUG ELETRÔNICO ÁGUA", "PLUG ELETRÔNICO ÁGUA 24V MTE"]) is False


def test_nome_generico_e_variante_mais_especifica_nao_diverge():
    assert nomes_normalizados_divergem(["POLIA", "POLIA DA CORREIA DENTADA"]) is False


def test_qualificadores_conflitantes_divergem():
    # SUPERIOR e INFERIOR descrevem posições/aplicações diferentes — nenhum nome é
    # superconjunto de palavras do outro, isso é conflito real, não completude.
    assert nomes_normalizados_divergem(["PIVO SUPERIOR", "PIVO INFERIOR"]) is True


def test_grupo_com_um_par_conflitante_diverge_mesmo_com_outros_pares_compativeis():
    assert nomes_normalizados_divergem(
        ["PIVO", "PIVO SUPERIOR", "PIVÔ DE SUSPENSÃO", "PIVÔ DE SUSPENSÃO INFERIOR DIREITO / ESQUERDO"]
    ) is True


def test_categorias_de_produto_diferentes_divergem():
    assert nomes_normalizados_divergem(["FILTRO DE ÓLEO", "FILTRO DE COMBUSTÍVEL"]) is True


def test_nome_unico_nao_diverge():
    assert nomes_normalizados_divergem(["POLIA"]) is False


def test_lista_vazia_nao_diverge():
    assert nomes_normalizados_divergem([]) is False
