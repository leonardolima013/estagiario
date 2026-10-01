"""Testes unitários da seleção de fontes da coleta de páginas (html-extract-save).

Organizado por seção:
- URL: normalização e validação de URL (`coleta_paginas.url`, Req 4).
- Caracterização de verification.serper_decisao: comportamento atual, fixado
  em casos concretos, das regras reutilizadas pela seleção (`_tokens`,
  `_tokens_significativos`, `_padrao_codigo`, `codigo_explicito`,
  `marca_generica`, `marca_no_resultado`; Req 1.1, 2.2, 2.6).
- Seleção: modelo `PecaConsultada`, trava de entrada, classificador, resolução
  do Teto_Aceitos e seletor de fontes (`coleta_paginas.selecao`; Req 1.4, 2,
  3.3, 3.7, 3.9, 4.6–4.8).
"""

from __future__ import annotations

import pytest

from coleta_paginas.url import host_da_url, motivo_recusa_url, normalizar_url
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401


# ---------------------------------------------------------------------------
# URL — motivo_recusa_url (Req 4.4, 4.5, 4.8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "   ",
        "\t\n",
        "ftp://exemplo.com/peca",
        "FTP://exemplo.com/peca",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "/produto/123",  # relativa
        "produto/123",  # relativa sem barra
        "exemplo.com/produto",  # sem esquema
        "//exemplo.com/produto",  # relativa ao esquema
        "http://",  # sem host
        "https:///caminho",  # host vazio
        "http://exem plo.com/x",  # espaço interno no host
        "https://exemplo.com/pe ca",  # espaço interno no caminho
        "https://exemplo.com/x\x00y",  # caractere de controle
        "http://exemplo.com:abc/",  # porta inválida
        "http://exemplo.com:99999/",  # porta fora da faixa
    ],
)
def test_url_invalida(url: str | None) -> None:
    assert motivo_recusa_url(url) == "url_invalida"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/",
        "http://LOCALHOST:8080/x",
        "https://api.localhost/x",
        "http://127.0.0.1/",
        "http://127.255.255.254/",
        "http://0.0.0.0/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://172.31.255.255/",
        "http://192.168.1.10/",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/",
        "http://[::]/",
        "http://[fc00::1]/",
        "http://[fd12:3456::1]/",
        "http://[fe80::1]/",
        # Formas especiais citadas na tarefa 3.5
        "http://2130706433/",  # 127.0.0.1 em decimal
        "http://0x7f.1/",  # 127.0.0.1 em forma legada hexadecimal
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapeado
        "http://[fe80::1%eth0]/",  # link-local com zona
    ],
)
def test_destino_nao_permitido(url: str) -> None:
    assert motivo_recusa_url(url) == "destino_nao_permitido"


@pytest.mark.parametrize(
    "url",
    [
        "https://exemplo.com/produto/123",
        "http://www.loja.com.br/peca?id=1",
        "http://8.8.8.8/",
        "http://172.32.0.1/",  # logo fora de 172.16.0.0/12
        "http://172.15.255.255/",  # logo antes de 172.16.0.0/12
        "http://11.0.0.1/",
        "http://[2001:4860:4860::8888]/",
        "https://localhost.exemplo.com/",  # só o sufixo .localhost é proibido
        "  https://exemplo.com/x  ",  # espaços nas bordas são aparados
    ],
)
def test_url_permitida(url: str) -> None:
    assert motivo_recusa_url(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "ftp://localhost/",  # esquema inválido e host proibido
        "ftp://127.0.0.1/",
        "gopher://10.0.0.1/",
        "http://localhost:abc/",  # porta inválida e host proibido
        "http://local host/",  # espaço interno
    ],
)
def test_url_invalida_prevalece_sobre_destino_nao_permitido(url: str) -> None:
    assert motivo_recusa_url(url) == "url_invalida"


# ---------------------------------------------------------------------------
# URL — normalizar_url (Req 4.1, 4.2, 4.3)
# ---------------------------------------------------------------------------


def test_normalizar_exemplo_completo() -> None:
    assert (
        normalizar_url("HTTP://A.com:80/x//?utm_Source=a&id=1#f")
        == "http://a.com/x?id=1"
    )


def test_normalizar_barra_final_equivale_a_sem_barra() -> None:
    assert normalizar_url("https://a.com/") == normalizar_url("https://a.com")
    assert normalizar_url("https://a.com/") == "https://a.com"
    assert normalizar_url("https://a.com//") == "https://a.com"


def test_normalizar_omite_interrogacao_quando_so_ha_utm() -> None:
    assert (
        normalizar_url("https://a.com/p?utm_source=x&UTM_MEDIUM=y&Utm_campaign=z")
        == "https://a.com/p"
    )


def test_normalizar_preserva_query_restante_na_ordem_e_sem_reencodar() -> None:
    url = "https://a.com/p?b=2&utm_x=1&a=%20x&c&utmx=3"
    assert normalizar_url(url) == "https://a.com/p?b=2&a=%20x&c&utmx=3"


def test_normalizar_distingue_produtos_por_query() -> None:
    assert normalizar_url("https://a.com/p?id=123") != normalizar_url(
        "https://a.com/p?id=124"
    )


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("https://A.COM:443/x", "https://a.com/x"),
        ("http://a.com:8080/x", "http://a.com:8080/x"),
        ("https://a.com:80/x", "https://a.com:80/x"),  # 80 não é padrão de https
        ("http://a.com:443/x", "http://a.com:443/x"),  # 443 não é padrão de http
    ],
)
def test_normalizar_porta(url: str, esperado: str) -> None:
    assert normalizar_url(url) == esperado


def test_normalizar_remove_fragmento() -> None:
    assert normalizar_url("https://a.com/p?id=1#secao") == "https://a.com/p?id=1"
    assert normalizar_url("https://a.com/p#") == "https://a.com/p"


def test_normalizar_preserva_caixa_do_caminho() -> None:
    assert normalizar_url("https://A.com/Peca/ABC") == "https://a.com/Peca/ABC"


def test_normalizar_ipv6_mantem_colchetes() -> None:
    assert (
        normalizar_url("http://[2001:DB8::1]:80/x/") == "http://[2001:db8::1]/x"
    )


@pytest.mark.parametrize(
    "url",
    [
        "HTTP://A.com:80/x//?utm_Source=a&id=1#f",
        "https://a.com/",
        "https://a.com/p?utm_source=x",
        "http://[2001:DB8::1]:8080/x/?q=1",
    ],
)
def test_normalizar_idempotente_em_exemplos(url: str) -> None:
    uma_vez = normalizar_url(url)
    assert normalizar_url(uma_vez) == uma_vez


@pytest.mark.parametrize("url", ["ftp://a.com/", "/relativa", "", "http://"])
def test_normalizar_recusa_url_invalida(url: str) -> None:
    with pytest.raises(ValueError):
        normalizar_url(url)


# ---------------------------------------------------------------------------
# URL — host_da_url (Req 4.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "esperado"),
    [
        ("https://WWW.Loja.com:8443/p", "www.loja.com"),
        ("http://a.com", "a.com"),
        ("http://[2001:DB8::1]:80/x", "2001:db8::1"),
        ("  https://Exemplo.com/  ", "exemplo.com"),
        ("/relativa", ""),
    ],
)
def test_host_da_url(url: str, esperado: str) -> None:
    assert host_da_url(url) == esperado



# ===========================================================================
# Caracterização de verification.serper_decisao (Req 1.1, 2.2, 2.6)
#
# `coleta_paginas.selecao` reutiliza estas funções sem modificá-las. Os casos
# abaixo fixam o comportamento ATUAL (inclusive o dos helpers privados
# `_tokens`, `_tokens_significativos` e `_padrao_codigo`) para que qualquer
# mudança futura em `serper_decisao` seja detectada aqui antes de alterar,
# silenciosamente, a trava, a classificação ou o reforço por nome da coleta.
# ===========================================================================

from verification.serper_client import ResultadoOrganico  # noqa: E402
from verification.serper_decisao import (  # noqa: E402
    _padrao_codigo,
    _tokens,
    _tokens_significativos,
    codigo_explicito,
    marca_generica,
    marca_no_resultado,
)


# ---------------------------------------------------------------------------
# _tokens / _tokens_significativos (base de `nome_reforcado`, Req 2.8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("Bucha Do Suporte Do Alternador", ["bucha", "do", "suporte", "do", "alternador"]),
        # Acentos removidos, casefold, hífen/ponto/barra/underscore separam tokens
        ("ÁGUA-Bomba.Óleo/Pivô_2", ["agua", "bomba", "oleo", "pivo", "2"]),
        ("JE-4699 (kit) 2x", ["je", "4699", "kit", "2x"]),
        ("Straße", ["strasse"]),  # casefold de ß
        # Letras de qualquer escrita contam como token
        ("Фильтр масляный", ["фильтр", "масляныи"]),
        ("!!! --- ...", []),
    ],
)
def test_caracterizacao_tokens(texto: str | None, esperado: list[str]) -> None:
    assert _tokens(texto) == esperado


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        (None, set()),
        ("", set()),
        ("Bucha Do Suporte Do Alternador", {"bucha", "suporte", "alternador"}),
        ("Filtro de Óleo para Motor com Junta", {"filtro", "oleo", "motor", "junta"}),
        # Só stopwords: conjunto vazio (um candidato assim nunca reforça)
        ("de do da dos das em com para a o e c p", set()),
        ("DE DO DA", set()),
        # Stopwords são comparadas depois do casefold/acentos; "à" vira "a"
        ("Pivô à Direita", {"pivo", "direita"}),
        ("Kit Kit KIT", {"kit"}),
    ],
)
def test_caracterizacao_tokens_significativos(
    texto: str | None, esperado: set[str]
) -> None:
    assert _tokens_significativos(texto) == esperado


# ---------------------------------------------------------------------------
# _padrao_codigo (base de `codigo_valido`, Req 1.5 / 2.9)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("codigo", [None, "", "   ", "---", "./ -", "\t\n", "_"])
def test_caracterizacao_padrao_codigo_sem_alfanumerico(codigo: str | None) -> None:
    assert _padrao_codigo(codigo) is None  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("codigo", "esperado"),
    [
        ("A", r"(?<![0-9a-z])a(?![0-9a-z])"),
        ("JE-4699", r"(?<![0-9a-z])j[\s\-./]*e[\s\-./]*4[\s\-./]*6[\s\-./]*9[\s\-./]*9(?![0-9a-z])"),
        (" 8.30 ", r"(?<![0-9a-z])8[\s\-./]*3[\s\-./]*0(?![0-9a-z])"),
        ("Ç-1", r"(?<![0-9a-z])c[\s\-./]*1(?![0-9a-z])"),  # acento removido
    ],
)
def test_caracterizacao_padrao_codigo(codigo: str, esperado: str) -> None:
    padrao = _padrao_codigo(codigo)
    assert padrao is not None
    assert padrao.pattern == esperado


# ---------------------------------------------------------------------------
# codigo_explicito (Codigo_Confirmado, Req 2.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texto", "codigo"),
    [
        ("Filtro JE4699 original", "JE4699"),
        # Separadores internos tolerados: espaço, hífen, ponto, barra
        ("Filtro JE 4699 original", "JE4699"),
        ("filtro je-4699", "JE4699"),
        ("Filtro JE.4699", "je 4699"),
        ("Filtro JE/4699", "JE-4699"),
        ("Filtro J E - 4 6 9 9", "JE4699"),
        # Caixa e acento ignorados no texto e no código
        ("Código ÁB12 disponível", "ab12"),
        ("codigo ab12", "ÁB-12"),
        # Fronteira alfanumérica satisfeita por pontuação/espaço/início/fim
        ("83061", "83061"),
        ("(83061)", "83061"),
        ("ref: 83061.", "83061"),
        ("Peça 83061-2", "83061"),  # "-" é fronteira: bate
        # Lookbehind/lookahead só consideram [0-9a-z]: letra de outra escrita
        # não é fronteira alfanumérica
        ("ф83061", "83061"),
    ],
)
def test_caracterizacao_codigo_explicito_bate(texto: str, codigo: str) -> None:
    assert codigo_explicito(texto, codigo) is True


@pytest.mark.parametrize(
    ("texto", "codigo"),
    [
        # Fronteira alfanumérica: prefixo/sufixo de outro código não bate
        ("Peça 830612", "83061"),
        ("Peça 183061", "83061"),
        ("XJE4699", "JE4699"),
        ("JE4699X", "JE4699"),
        ("é83061", "83061"),  # acento removido vira letra ASCII: sem fronteira
        # Underscore não é separador tolerado
        ("JE_4699", "JE4699"),
        # Texto ou código vazios / sem alfanumérico
        ("", "JE4699"),
        ("JE4699", ""),
        ("JE4699", "---"),
        ("qualquer texto", "   "),
        # Código ausente
        ("Filtro de óleo", "JE4699"),
    ],
)
def test_caracterizacao_codigo_explicito_nao_bate(texto: str, codigo: str) -> None:
    assert codigo_explicito(texto, codigo) is False


# ---------------------------------------------------------------------------
# marca_generica (Marca_Sem_Referencia, Req 1.1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "marca",
    [
        "CONVERSÃO",
        "Conversão",
        "conversao",
        "CONVERSAO",
        "  conversão  ",
        "OEM",
        "oem",
        " Oem ",
        "ORIGINAL OEM",
        "original oem",
        "Original   OEM",  # espaços internos colapsados
        "\toriginal\noem\t",
        "ÓRIGINAL ÔEM",  # acentos removidos
    ],
)
def test_caracterizacao_marca_generica_verdadeira(marca: str) -> None:
    assert marca_generica(marca) is True


@pytest.mark.parametrize(
    "marca",
    [
        None,
        "",  # vazia não é genérica aqui; a trava trata vazia à parte
        "   ",
        "Bosch",
        "OEM Bosch",
        "Original",
        "originaloem",
        "Original-OEM",  # hífen não é normalizado
        "OEM.",
        "conversões",
        "Conversão Kit",
    ],
)
def test_caracterizacao_marca_generica_falsa(marca: str | None) -> None:
    assert marca_generica(marca) is False


# ---------------------------------------------------------------------------
# marca_no_resultado (Marca_Confirmada, Req 2.6)
# ---------------------------------------------------------------------------


def _org(title: str = "", snippet: str = "", link: str = "") -> ResultadoOrganico:
    return ResultadoOrganico(title=title, link=link, snippet=snippet, position=1)


@pytest.mark.parametrize(
    ("organico", "marca"),
    [
        (_org(title="Filtro Bosch JE4699"), "Bosch"),
        (_org(snippet="fabricado pela BOSCH"), "bosch"),
        # O link também é examinado
        (_org(title="Filtro JE4699", link="https://www.bosch.com.br/p/1"), "Bosch"),
        # Caixa e acento ignorados
        (_org(title="Amortecedor CÓFAP"), "Cofap"),
        (_org(title="Amortecedor Cofap"), "CÓFAP"),
        # Espaços internos colapsados na marca e nos campos
        (_org(title="Pistão MAHLE   METAL\nLEVE"), "Mahle  Metal Leve"),
        # Substring sem fronteira de palavra
        (_org(title="Boschmann peças"), "Bosch"),
        # Campos são unidos por espaço: a marca pode atravessar título e snippet
        (_org(title="Pistão Mahle", snippet="Metal Leve original"), "Mahle Metal Leve"),
    ],
)
def test_caracterizacao_marca_no_resultado_bate(
    organico: ResultadoOrganico, marca: str
) -> None:
    assert marca_no_resultado(organico, marca) is True


@pytest.mark.parametrize(
    ("organico", "marca"),
    [
        (_org(title="Filtro JE4699", snippet="original", link="https://loja.com"), "Bosch"),
        # Marca ausente, vazia ou só espaços nunca confirma
        (_org(title="Filtro Bosch"), ""),
        (_org(title="Filtro Bosch"), "   "),
        (_org(title="Filtro Bosch"), None),
        # Hífen não é normalizado
        (_org(title="Pistão Metal Leve"), "Metal-Leve"),
        (_org(title="Pistão Metal-Leve"), "Metal Leve"),
        # Resultado sem campos
        (_org(), "Bosch"),
    ],
)
def test_caracterizacao_marca_no_resultado_nao_bate(
    organico: ResultadoOrganico, marca: str | None
) -> None:
    assert marca_no_resultado(organico, marca) is False  # type: ignore[arg-type]




# ===========================================================================
# Seleção — trava, classificador, teto e seletor (Req 1.4, 2, 3, 4.6–4.8)
# ===========================================================================

import inspect  # noqa: E402

from coleta_paginas.modelos import (  # noqa: E402
    CodigoPecaInvalidoError,
    Confianca,
    ConfiguracaoColetaError,
    DecisaoFonte,
    PecaConsultada,
    PrecondicaoTravaError,
    ResultadoBusca,
)
from coleta_paginas.selecao import (  # noqa: E402
    MOTIVO_CODIGO_AUSENTE,
    MOTIVO_CODIGO_E_MARCA,
    MOTIVO_CODIGO_SEM_MARCA,
    TETO_ACEITOS_PADRAO,
    classificar_fontes,
    resolver_teto_aceitos,
    selecionar_fontes,
    trava_entrada,
)


# ---------------------------------------------------------------------------
# PecaConsultada — list → tuple
# ---------------------------------------------------------------------------


def test_peca_consultada_converte_lista_em_tupla() -> None:
    nomes = ["Filtro de Óleo", "Elemento Filtrante"]
    peca = PecaConsultada("JE4699", "Bosch", nomes)  # type: ignore[arg-type]
    assert isinstance(peca.nomes_candidatos, tuple)
    assert peca.nomes_candidatos == ("Filtro de Óleo", "Elemento Filtrante")
    # A lista original não é compartilhada com a peça
    nomes.append("outro")
    assert peca.nomes_candidatos == ("Filtro de Óleo", "Elemento Filtrante")


def test_peca_consultada_padrao_sem_nomes() -> None:
    assert PecaConsultada("JE4699", "Bosch").nomes_candidatos == ()


# ---------------------------------------------------------------------------
# trava_entrada (Req 1.1, 1.4, 1.5)
# ---------------------------------------------------------------------------


def test_trava_entrada_recebe_so_a_peca() -> None:
    parametros = list(inspect.signature(trava_entrada).parameters.values())
    assert len(parametros) == 1
    assert parametros[0].kind in (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
    )
    assert parametros[0].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "marca", [None, "", "   ", "\u00a0\u2003", "CONVERSÃO", " oem ", "Original OEM"]
)
def test_trava_dispara_para_marca_sem_referencia(marca: str | None) -> None:
    assert trava_entrada(PecaConsultada("JE4699", marca, ["Filtro"])) is True


@pytest.mark.parametrize("marca", ["Bosch", "Mahle Metal Leve", "OEM Bosch", "Original"])
def test_trava_nao_dispara_para_marca_real(marca: str) -> None:
    assert trava_entrada(PecaConsultada("JE4699", marca)) is False


@pytest.mark.parametrize("codigo", [None, "", "   ", "---", "./_"])
@pytest.mark.parametrize("marca", ["Bosch", "OEM", None])
def test_trava_codigo_invalido_tem_precedencia(
    codigo: str | None, marca: str | None
) -> None:
    with pytest.raises(CodigoPecaInvalidoError) as exc:
        trava_entrada(PecaConsultada(codigo, marca))
    assert exc.value.codigo == codigo


# ---------------------------------------------------------------------------
# classificar_fontes (Req 2.2–2.5, 2.9, 2.10)
# ---------------------------------------------------------------------------


def _res(
    titulo: str | None = None,
    snippet: str | None = None,
    url: str | None = "https://loja.com/p",
    dominio: str | None = "loja.com",
    posicao: int | None = None,
) -> ResultadoBusca:
    return ResultadoBusca(titulo, snippet, url, dominio, posicao)


def test_classificar_tabela_codigo_marca() -> None:
    peca = PecaConsultada("JE-4699", "Bosch", ["Filtro de Óleo"])
    resultados = [
        _res(titulo="Filtro de óleo Bosch JE4699"),  # código + marca
        _res(titulo="Filtro JE 4699", snippet="genérico"),  # só código
        _res(titulo="Filtro Bosch", snippet="sem código"),  # só marca
        _res(titulo="Nada", url="https://loja.com/JE4699"),  # código só no link
    ]
    decisoes = classificar_fontes(peca, resultados)

    assert [d.resultado for d in decisoes] == resultados
    assert [d.confianca for d in decisoes] == [
        Confianca.ALTA,
        Confianca.MEDIA,
        Confianca.REJEITADO,
        Confianca.REJEITADO,
    ]
    assert [d.motivo for d in decisoes] == [
        MOTIVO_CODIGO_E_MARCA,
        MOTIVO_CODIGO_SEM_MARCA,
        MOTIVO_CODIGO_AUSENTE,
        MOTIVO_CODIGO_AUSENTE,
    ]
    assert decisoes[0].nome_reforcado is True
    assert decisoes[1].nome_reforcado is False
    assert decisoes[2].marca_confirmada is True
    assert decisoes[2].codigo_confirmado is False


def test_classificar_marca_confirmada_pelo_link() -> None:
    peca = PecaConsultada("JE4699", "Bosch")
    (decisao,) = classificar_fontes(
        peca, [_res(titulo="Filtro JE4699", url="https://www.bosch.com.br/x")]
    )
    assert decisao.marca_confirmada is True
    assert decisao.confianca is Confianca.ALTA


def test_classificar_codigo_prefixo_nao_confirma() -> None:
    peca = PecaConsultada("83061", "Bosch")
    decisoes = classificar_fontes(
        peca,
        [_res(titulo="Peça Bosch 830612"), _res(titulo="Peça Bosch 83061")],
    )
    assert [d.confianca for d in decisoes] == [Confianca.REJEITADO, Confianca.ALTA]


def test_classificar_lista_vazia() -> None:
    assert classificar_fontes(PecaConsultada("JE4699", "Bosch"), []) == ()


@pytest.mark.parametrize("marca", ["OEM", "Conversão", None, "  "])
def test_classificar_recusa_marca_sem_referencia(marca: str | None) -> None:
    with pytest.raises(PrecondicaoTravaError):
        classificar_fontes(
            PecaConsultada("JE4699", marca), [_res(titulo="Filtro JE4699")]
        )


def test_classificar_codigo_invalido_antes_da_trava() -> None:
    with pytest.raises(CodigoPecaInvalidoError):
        classificar_fontes(PecaConsultada("---", "OEM"), [_res(titulo="x")])


# ---------------------------------------------------------------------------
# resolver_teto_aceitos (Req 3.7, 3.8, 3.9)
# ---------------------------------------------------------------------------


def test_teto_padrao_e_3() -> None:
    assert TETO_ACEITOS_PADRAO == 3
    assert resolver_teto_aceitos(None, None) == 3


def test_teto_chamada_prevalece_sobre_ambiente() -> None:
    assert resolver_teto_aceitos(5, "2") == 5
    # Ambiente inválido é ignorado quando a chamada informa o teto
    assert resolver_teto_aceitos(1, "abc") == 1


@pytest.mark.parametrize(("ambiente", "esperado"), [("2", 2), (" 2 ", 2), ("\t10\n", 10)])
def test_teto_do_ambiente(ambiente: str, esperado: int) -> None:
    assert resolver_teto_aceitos(None, ambiente) == esperado


@pytest.mark.parametrize("valor", [True, False, 0, -1, 2.0, 1.5, "2", "3"])
def test_teto_chamada_invalida(valor: object) -> None:
    with pytest.raises(ConfiguracaoColetaError) as exc:
        resolver_teto_aceitos(valor, "4")
    assert exc.value.campo == "teto_aceitos"
    assert exc.value.origem == "chamada"
    assert exc.value.valor == repr(valor)


@pytest.mark.parametrize("valor", ["2.0", "0", "-1", "", "   ", "abc", "+2", "1e1", "２"])
def test_teto_ambiente_invalido(valor: str) -> None:
    with pytest.raises(ConfiguracaoColetaError) as exc:
        resolver_teto_aceitos(None, valor)
    assert exc.value.campo == "teto_aceitos"
    assert exc.value.origem == "ambiente"
    assert exc.value.valor == repr(valor)


def test_teto_ambiente_com_digitos_alem_do_limite_de_int_e_rejeitado() -> None:
    # 4301 dígitos passam do limite padrão de int(str); antes vazava ValueError.
    valor = "1" * 4301
    with pytest.raises(ConfiguracaoColetaError) as exc:
        resolver_teto_aceitos(None, valor)
    assert type(exc.value) is ConfiguracaoColetaError
    assert exc.value.campo == "teto_aceitos"
    assert exc.value.origem == "ambiente"
    assert exc.value.valor == repr(valor)


def test_teto_ambiente_grande_conversivel_continua_valido() -> None:
    assert resolver_teto_aceitos(None, "10000000000") == 10_000_000_000
    # Zeros à esquerda não contam como dígitos significativos.
    assert resolver_teto_aceitos(None, "0" * 4301 + "7") == 7


# ---------------------------------------------------------------------------
# selecionar_fontes (Req 3.1–3.6, 3.10–3.12, 4.6–4.8)
# ---------------------------------------------------------------------------


def _dec(
    url: str | None,
    confianca: Confianca = Confianca.ALTA,
    *,
    nome: bool = False,
    posicao: int | None = None,
    dominio: str | None = "loja.com",
) -> DecisaoFonte:
    return DecisaoFonte(
        resultado=ResultadoBusca("t", "s", url, dominio, posicao),
        confianca=confianca,
        codigo_confirmado=confianca is not Confianca.REJEITADO,
        marca_confirmada=confianca is Confianca.ALTA,
        nome_reforcado=nome,
        motivo="m",
    )


def _urls(selecao) -> list[str | None]:  # type: ignore[no-untyped-def]
    return [f.decisao.resultado.url for f in selecao.selecionadas]


def test_selecao_alta_antes_de_media() -> None:
    decisoes = [
        _dec("https://a.com/media", Confianca.MEDIA, nome=True, posicao=1),
        _dec("https://a.com/alta", Confianca.ALTA, posicao=9),
    ]
    assert _urls(selecionar_fontes(decisoes, 3)) == [
        "https://a.com/alta",
        "https://a.com/media",
    ]


def test_selecao_desempate_nome_posicao_ordem() -> None:
    decisoes = [
        _dec("https://a.com/sem-pos-1"),  # sem posição, sem nome
        _dec("https://a.com/pos-5", posicao=5),
        _dec("https://a.com/pos-2", posicao=2),
        _dec("https://a.com/nome-sem-pos", nome=True),
        _dec("https://a.com/sem-pos-2"),
        _dec("https://a.com/nome-pos-7", nome=True, posicao=7),
        _dec("https://a.com/pos-2-bis", posicao=2),
    ]
    assert _urls(selecionar_fontes(decisoes, 10)) == [
        "https://a.com/nome-pos-7",  # nome reforçado, com posição
        "https://a.com/nome-sem-pos",  # nome reforçado, sem posição por último
        "https://a.com/pos-2",  # posição 2, primeiro na entrada
        "https://a.com/pos-2-bis",  # posição 2, segundo na entrada
        "https://a.com/pos-5",
        "https://a.com/sem-pos-1",  # sem posição, ordem de entrada
        "https://a.com/sem-pos-2",
    ]


def test_selecao_teto_corta_depois_de_ordenar() -> None:
    decisoes = [
        _dec("https://a.com/3", Confianca.MEDIA),
        _dec("https://a.com/2", posicao=2),
        _dec("https://a.com/1", posicao=1),
    ]
    selecao = selecionar_fontes(decisoes, 2)
    assert _urls(selecao) == ["https://a.com/1", "https://a.com/2"]


def test_selecao_deduplica_por_url_normalizada_mantendo_a_melhor() -> None:
    decisoes = [
        _dec("https://A.com/p/?utm_source=x", Confianca.MEDIA),
        _dec("https://a.com/p#frag", Confianca.ALTA, posicao=3),
        _dec("HTTPS://a.com:443/p", Confianca.ALTA, posicao=1),
        _dec("https://a.com/q", Confianca.MEDIA),
    ]
    selecao = selecionar_fontes(decisoes, 3)
    assert _urls(selecao) == ["HTTPS://a.com:443/p", "https://a.com/q"]
    assert [f.url_normalizada for f in selecao.selecionadas] == [
        "https://a.com/p",
        "https://a.com/q",
    ]


def test_selecao_duplicata_nao_ocupa_vaga() -> None:
    decisoes = [
        _dec("https://a.com/p", posicao=1),
        _dec("https://a.com/p/", posicao=2),
        _dec("https://a.com/q", posicao=3),
    ]
    assert _urls(selecionar_fontes(decisoes, 2)) == [
        "https://a.com/p",
        "https://a.com/q",
    ]


def test_selecao_exclusoes_nao_ocupam_vaga_e_seguem_ordem_de_entrada() -> None:
    decisoes = [
        _dec("http://localhost/x", posicao=1),
        _dec("https://a.com/1", posicao=5),
        _dec("ftp://a.com/x", Confianca.MEDIA, posicao=2),
        _dec(None, posicao=3),
        _dec("https://a.com/2", Confianca.MEDIA),
        _dec("http://10.0.0.1/", Confianca.MEDIA),
    ]
    selecao = selecionar_fontes(decisoes, 2)
    assert _urls(selecao) == ["https://a.com/1", "https://a.com/2"]
    assert [(e.decisao, e.motivo) for e in selecao.excluidas] == [
        (decisoes[0], "destino_nao_permitido"),
        (decisoes[2], "url_invalida"),
        (decisoes[3], "url_invalida"),
        (decisoes[5], "destino_nao_permitido"),
    ]


def test_selecao_exclusoes_independem_do_teto() -> None:
    decisoes = [
        _dec("https://a.com/1"),
        _dec("https://a.com/2"),
        _dec("http://127.0.0.1/"),
    ]
    for teto in (1, 2, 5):
        selecao = selecionar_fontes(decisoes, teto)
        assert [e.decisao for e in selecao.excluidas] == [decisoes[2]]


def test_selecao_rejeitados_nunca_selecionados_nem_excluidos() -> None:
    decisoes = [
        _dec("https://a.com/r", Confianca.REJEITADO, nome=True, posicao=1),
        _dec("http://localhost/r", Confianca.REJEITADO),
        _dec(None, Confianca.REJEITADO),
        _dec("https://a.com/ok", Confianca.MEDIA),
    ]
    selecao = selecionar_fontes(decisoes, 5)
    assert _urls(selecao) == ["https://a.com/ok"]
    assert selecao.excluidas == ()


def test_selecao_vazia() -> None:
    selecao = selecionar_fontes([], 3)
    assert selecao.selecionadas == ()
    assert selecao.excluidas == ()


@pytest.mark.parametrize("dominio", [None, "", "   "])
def test_selecao_dominio_em_branco_usa_host(dominio: str | None) -> None:
    (fonte,) = selecionar_fontes(
        [_dec("https://WWW.Loja.com:8443/p", dominio=dominio)], 3
    ).selecionadas
    assert fonte.dominio == "www.loja.com"


def test_selecao_preserva_dominio_informado() -> None:
    (fonte,) = selecionar_fontes(
        [_dec("https://www.loja.com/p", dominio="Loja Oficial")], 3
    ).selecionadas
    assert fonte.dominio == "Loja Oficial"


def test_selecao_nao_altera_entrada_e_e_deterministica() -> None:
    decisoes = [
        _dec("https://a.com/2", Confianca.MEDIA, posicao=2),
        _dec("https://a.com/1", posicao=1),
        _dec("http://localhost/", posicao=3),
    ]
    copia = list(decisoes)
    primeira = selecionar_fontes(decisoes, 3)
    segunda = selecionar_fontes(decisoes, 3)
    assert decisoes == copia
    assert primeira == segunda
