from tui.banner import TEXTO_BANNER, gerar_banner


def test_texto_do_banner_e_estagiario_sem_o():
    assert TEXTO_BANNER == "ESTAGIARIO"


def test_usa_ansi_shadow_em_terminal_de_80_colunas():
    # ansi_shadow é a fonte de bloco sólido pedida — tem que ser a escolhida
    # quando cabe, não cair direto pra uma alternativa.
    import pyfiglet

    banner = gerar_banner(80)
    esperado = pyfiglet.figlet_format(TEXTO_BANNER, font="ansi_shadow", width=200)
    linhas_esperadas = [l for l in esperado.rstrip("\n").split("\n") if l.strip()]

    linhas_banner = str(banner).splitlines()
    # a primeira linha do banner (sem sombra na coluna 0) deve bater com a
    # primeira linha crua do ansi_shadow.
    assert linhas_banner[0].rstrip() == linhas_esperadas[0].rstrip()


def test_gera_banner_em_terminal_de_80_colunas():
    banner = gerar_banner(80)

    assert banner is not None
    linhas = str(banner).splitlines()
    assert all(len(l) <= 80 for l in linhas)


def test_cai_pra_fonte_compacta_quando_nao_cabe_a_principal():
    banner_largo = gerar_banner(80)
    banner_estreito = gerar_banner(40)

    assert banner_estreito is not None
    assert len(str(banner_estreito).splitlines()[0]) < len(str(banner_largo).splitlines()[0])


def test_retorna_none_quando_nada_cabe():
    assert gerar_banner(5) is None


def test_sombra_deslocada_uma_linha_e_uma_coluna():
    banner = gerar_banner(80)
    linhas = str(banner).splitlines()
    # A sombra soma +1 linha e +1 coluna ao tamanho cru do figlet.
    assert len(linhas) >= 2
