"""Testes de propriedade da seleção de fontes e da normalização de URL (html-extract-save)."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from coleta_paginas.url import motivo_recusa_url, normalizar_url
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401

# ---------------------------------------------------------------------------
# Geradores de componentes de URL
# ---------------------------------------------------------------------------

_LETRAS = "abcdefghijklmnopqrstuvwxyz"
_DIGITOS = "0123456789"
_PORTAS_PADRAO = {"http": 80, "https": 443}


@st.composite
def _caixa_aleatoria(draw: st.DrawFn, texto: str) -> str:
    """Mesmo texto ASCII, com maiúsculas/minúsculas sorteadas por caractere."""
    mascara = draw(st.lists(st.booleans(), min_size=len(texto), max_size=len(texto)))
    return "".join(c.upper() if m else c for c, m in zip(texto, mascara))


@st.composite
def _rotulo_host(draw: st.DrawFn) -> str:
    inicio = draw(st.sampled_from(_LETRAS))
    meio = draw(st.text(alphabet=_LETRAS + _DIGITOS + "-", max_size=8))
    fim = draw(st.sampled_from(_LETRAS + _DIGITOS))
    return inicio + meio + fim


@st.composite
def _host(draw: st.DrawFn) -> str:
    """Nome DNS comum (nunca IP literal nem localhost), já com caixa variada."""
    rotulos = draw(st.lists(_rotulo_host(), min_size=1, max_size=3))
    tld = draw(st.text(alphabet=_LETRAS, min_size=2, max_size=6))
    return draw(_caixa_aleatoria(".".join([*rotulos, tld])))


_SEGMENTO = st.text(alphabet=_LETRAS + _LETRAS.upper() + _DIGITOS + "-._~%", max_size=8)


@st.composite
def _caminho(draw: st.DrawFn) -> str:
    """Vazio, ou segmentos com caixa preservada seguidos de 0..3 barras finais."""
    segmentos = draw(st.lists(_SEGMENTO, max_size=4))
    base = "".join("/" + s for s in segmentos)
    barras = draw(st.integers(min_value=0, max_value=3))
    return base + "/" * barras


_TEXTO_QUERY = st.text(alphabet=_LETRAS + _LETRAS.upper() + _DIGITOS + "%-._~+", max_size=6)


@st.composite
def _nome_utm(draw: st.DrawFn) -> str:
    prefixo = draw(_caixa_aleatoria("utm_"))
    return prefixo + draw(_TEXTO_QUERY)


@st.composite
def _nome_comum(draw: st.DrawFn) -> str:
    """Nomes que NÃO começam com `utm_` (inclui controles parecidos)."""
    nome = draw(
        st.one_of(
            st.text(alphabet=_LETRAS + _LETRAS.upper() + _DIGITOS + "%-.~", min_size=1, max_size=6),
            st.sampled_from(["utm", "UTMsource", "autm_x", "utm-source", "ut_m"]),
        )
    )
    return nome


@dataclass(frozen=True)
class _Parametro:
    nome: str
    valor: str | None  # None = pedaço sem "=" (ex.: "flag")
    utm: bool

    @property
    def pedaco(self) -> str:
        return self.nome if self.valor is None else f"{self.nome}={self.valor}"


@st.composite
def _parametro(draw: st.DrawFn) -> _Parametro:
    utm = draw(st.booleans())
    nome = draw(_nome_utm()) if utm else draw(_nome_comum())
    valor = draw(st.one_of(st.none(), _TEXTO_QUERY, _TEXTO_QUERY.map(lambda v: v + "=" + v)))
    return _Parametro(nome=nome, valor=valor, utm=utm)


@dataclass(frozen=True)
class _ComponentesUrl:
    esquema: str
    host: str
    porta: int | None
    caminho: str
    com_interrogacao: bool
    parametros: tuple[_Parametro, ...]
    fragmento: str | None

    def montar(self) -> str:
        porta = f":{self.porta}" if self.porta is not None else ""
        query = "&".join(p.pedaco for p in self.parametros)
        interrogacao = "?" + query if (self.parametros or self.com_interrogacao) else ""
        fragmento = "#" + self.fragmento if self.fragmento is not None else ""
        return f"{self.esquema}://{self.host}{porta}{self.caminho}{interrogacao}{fragmento}"

    def esperado(self) -> str:
        esquema = self.esquema.lower()
        porta = (
            f":{self.porta}"
            if self.porta is not None and self.porta != _PORTAS_PADRAO[esquema]
            else ""
        )
        query = "&".join(p.pedaco for p in self.parametros if not p.utm)
        return f"{esquema}://{self.host.lower()}{porta}{self.caminho.rstrip('/')}" + (
            "?" + query if query else ""
        )


@st.composite
def _componentes_url(draw: st.DrawFn) -> _ComponentesUrl:
    esquema_base = draw(st.sampled_from(["http", "https"]))
    esquema = draw(_caixa_aleatoria(esquema_base))
    porta = draw(
        st.one_of(
            st.none(),
            st.just(_PORTAS_PADRAO[esquema_base]),
            st.sampled_from([80, 443]),
            st.integers(min_value=1, max_value=65535),
        )
    )
    return _ComponentesUrl(
        esquema=esquema,
        host=draw(_host()),
        porta=porta,
        caminho=draw(_caminho()),
        com_interrogacao=draw(st.booleans()),
        parametros=tuple(draw(st.lists(_parametro(), max_size=6))),
        fragmento=draw(
            st.one_of(
                st.none(),
                st.text(alphabet=_LETRAS + _DIGITOS + "/?&=-_", max_size=8),
            )
        ),
    )


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


# Feature: html-extract-save, Property 10: Normalização por componentes
@settings(max_examples=100)
@given(componentes=_componentes_url())
def test_propriedade_normalizacao_por_componentes(componentes: _ComponentesUrl) -> None:
    """**Validates: Requirements 4.1, 4.2**

    Esquema e host em minúsculas, porta só se não for a padrão, caminho sem
    barras finais, parâmetros não-`utm_` na ordem e com os bytes originais
    (sem `?` quando a lista fica vazia) e sem fragmento.
    """
    url = componentes.montar()
    assert motivo_recusa_url(url) is None  # pré-condição de normalizar_url

    assert normalizar_url(url) == componentes.esperado()



# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------

_HEX = "0123456789abcdefABCDEF"


@st.composite
def _escape_percentual(draw: st.DrawFn) -> str:
    """`%XY` válido, ou `%` solto / truncado (bytes crus que não devem ser decodificados)."""
    return draw(
        st.one_of(
            st.builds(lambda a, b: f"%{a}{b}", st.sampled_from(_HEX), st.sampled_from(_HEX)),
            st.sampled_from(["%", "%2", "%zz", "%%"]),
        )
    )


def _texto_com_escapes(alfabeto: str, max_pedacos: int = 4) -> st.SearchStrategy[str]:
    return st.lists(
        st.one_of(st.text(alphabet=alfabeto, max_size=4), _escape_percentual()),
        max_size=max_pedacos,
    ).map("".join)


_ALFABETO_USERINFO = _LETRAS + _LETRAS.upper() + _DIGITOS + "-._~!$&'()*+,;=:"
_ALFABETO_CAMINHO = _LETRAS + _LETRAS.upper() + _DIGITOS + "-._~!$&'()*+,;=:@/"
_ALFABETO_QUERY = _LETRAS + _LETRAS.upper() + _DIGITOS + "-._~!$'()*+,;:@/?="


@st.composite
def _host_adversario(draw: st.DrawFn) -> str:
    """Nome DNS (com ponto final opcional) ou IPv6 entre colchetes (zona opcional), caixa variada."""
    escolha = draw(st.sampled_from(["dns", "dns_ponto", "ipv6", "ipv6_zona"]))
    if escolha.startswith("dns"):
        host = draw(_host())
        return host + "." if escolha == "dns_ponto" else host
    # Sem pré-filtro: IPs das faixas proibidas são descartados pelo filtro do teste,
    # e os reservados permitidos (ex.: 2001:db8::/32, 64:ff9b::/96) ficam como casos extras.
    ip = draw(st.ip_addresses(v=6))
    literal = draw(_caixa_aleatoria(ip.compressed))
    if escolha == "ipv6_zona":
        literal += "%" + draw(_caixa_aleatoria(draw(st.sampled_from(["eth0", "en1", "Lo"]))))
    return f"[{literal}]"


@st.composite
def _porta_adversaria(draw: st.DrawFn) -> str:
    """Sufixo de porta: ausente, vazio (`host:`), padrão, com zeros à esquerda ou arbitrária."""
    return draw(
        st.one_of(
            st.just(""),
            st.just(":"),
            st.sampled_from([":80", ":443", ":080", ":0443", ":00080"]),
            st.integers(min_value=0, max_value=65535).map(lambda p: f":{p}"),
        )
    )


@st.composite
def _pedaco_query_adversario(draw: st.DrawFn) -> str:
    return draw(
        st.one_of(
            st.just(""),  # gera `&&`, `?&`, `&` final
            st.builds(
                lambda prefixo, resto: prefixo + resto,
                _caixa_aleatoria("utm_"),
                _texto_com_escapes(_ALFABETO_QUERY),
            ),
            st.sampled_from(["=", "utm_", "UTM_=x", "utm", "=utm_x", "a=utm_b"]),
            _texto_com_escapes(_ALFABETO_QUERY),
        )
    )


@st.composite
def _url_adversaria(draw: st.DrawFn) -> str:
    esquema = draw(_caixa_aleatoria(draw(st.sampled_from(["http", "https"]))))
    userinfo = draw(
        st.one_of(st.just(""), _texto_com_escapes(_ALFABETO_USERINFO).map(lambda u: u + "@"))
    )
    host = draw(_host_adversario())
    porta = draw(_porta_adversaria())
    caminho = draw(
        st.one_of(
            st.just(""),
            st.sampled_from(["/", "//", "///", "//a//", "/a/./b/../"]),
            _texto_com_escapes(_ALFABETO_CAMINHO).map(lambda c: "/" + c),
        )
    )
    pedacos = draw(st.lists(_pedaco_query_adversario(), max_size=6))
    query = draw(st.one_of(st.just(""), st.just("?"), st.just("?" + "&".join(pedacos))))
    fragmento = draw(
        st.one_of(st.just(""), _texto_com_escapes(_ALFABETO_QUERY + "#").map(lambda f: "#" + f))
    )
    return f"{esquema}://{userinfo}{host}{porta}{caminho}{query}{fragmento}"


_URL_PARA_NORMALIZAR = st.one_of(
    _componentes_url().map(lambda c: c.montar()),
    _url_adversaria(),
)


# Feature: html-extract-save, Property 11: A normalização é idempotente
@settings(max_examples=100)
@given(url=_URL_PARA_NORMALIZAR.filter(lambda u: motivo_recusa_url(u) is None))
def test_propriedade_normalizacao_idempotente(url: str) -> None:
    """**Validates: Requirements 4.3**

    Para toda URL aceita por `motivo_recusa_url`, normalizar o resultado da
    normalização não o altera, e a URL normalizada continua aceita.
    """
    normalizada = normalizar_url(url)

    assert motivo_recusa_url(normalizada) is None
    assert normalizar_url(normalizada) == normalizada



# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------

import ipaddress  # noqa: E402

# Faixas do Req 4.5, reescritas aqui (oráculo independente de `coleta_paginas.url`).
_REDES_PROIBIDAS_V4 = tuple(
    ipaddress.IPv4Network(r)
    for r in ("127.0.0.0/8", "0.0.0.0/32", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16")
)
_REDES_PROIBIDAS_V6 = tuple(
    ipaddress.IPv6Network(r) for r in ("::1/128", "::/128", "fc00::/7", "fe80::/10")
)


def _ip_proibido(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _ip_proibido(ip.ipv4_mapped)
        return any(ip in rede for rede in _REDES_PROIBIDAS_V6)
    return any(ip in rede for rede in _REDES_PROIBIDAS_V4)


def _octeto_legado(valor: int, base: str) -> str:
    if base == "hex":
        return hex(valor)
    if base == "oct":
        return "0" + oct(valor)[2:]
    return str(valor)


@st.composite
def _ipv4_forma_legada(draw: st.DrawFn, ip: ipaddress.IPv4Address) -> str:
    """Formas que `inet_aton` aceita: inteiro decimal/hex, 2 ou 3 partes, octetos em bases mistas."""
    n = int(ip)
    o = ip.packed
    forma = draw(st.sampled_from(["decimal", "hex", "duas_partes", "tres_partes", "bases_mistas"]))
    if forma == "decimal":
        texto = str(n)
    elif forma == "hex":
        texto = hex(n)
    elif forma == "duas_partes":
        bases = draw(st.lists(st.sampled_from(["dec", "hex", "oct"]), min_size=2, max_size=2))
        texto = f"{_octeto_legado(o[0], bases[0])}.{_octeto_legado(n & 0xFFFFFF, bases[1])}"
    elif forma == "tres_partes":
        bases = draw(st.lists(st.sampled_from(["dec", "hex", "oct"]), min_size=3, max_size=3))
        texto = ".".join(
            _octeto_legado(v, b) for v, b in zip((o[0], o[1], n & 0xFFFF), bases)
        )
    else:
        bases = draw(st.lists(st.sampled_from(["dec", "hex", "oct"]), min_size=4, max_size=4))
        texto = ".".join(_octeto_legado(v, b) for v, b in zip(o, bases))
    return draw(_caixa_aleatoria(texto))


@st.composite
def _ipv6_entre_colchetes(draw: st.DrawFn, ip: ipaddress.IPv6Address) -> str:
    texto = draw(st.sampled_from([ip.compressed, ip.exploded]))
    texto = draw(_caixa_aleatoria(texto))
    if draw(st.booleans()):
        texto += "%" + draw(st.sampled_from(["eth0", "en1", "lo", "1"]))
    return f"[{texto}]"


@st.composite
def _ipv4_mapeado(draw: st.DrawFn, ip: ipaddress.IPv4Address) -> str:
    """`::ffff:a.b.c.d`, `::ffff:XXXX:XXXX` ou forma expandida, entre colchetes."""
    alto, baixo = int(ip) >> 16, int(ip) & 0xFFFF
    texto = draw(
        st.sampled_from(
            [
                f"::ffff:{ip}",
                f"::ffff:{alto:x}:{baixo:x}",
                f"0:0:0:0:0:ffff:{ip}",
                f"0000:0000:0000:0000:0000:ffff:{alto:04x}:{baixo:04x}",
            ]
        )
    )
    return f"[{draw(_caixa_aleatoria(texto))}]"


@st.composite
def _host_proibido(draw: st.DrawFn) -> str:
    """Host (já no formato do netloc) que o Req 4.5 manda recusar."""
    tipo = draw(st.sampled_from(["v4", "v4_ponto", "v4_legado", "v6", "v4_mapeado", "localhost"]))
    if tipo == "localhost":
        base = draw(
            st.one_of(
                st.just("localhost"),
                st.just("localhost."),
                st.lists(_rotulo_host(), min_size=1, max_size=3).map(
                    lambda r: ".".join(r) + ".localhost"
                ),
            )
        )
        return draw(_caixa_aleatoria(base))
    if tipo == "v6":
        rede = draw(st.sampled_from(_REDES_PROIBIDAS_V6))
        return draw(_ipv6_entre_colchetes(draw(st.ip_addresses(network=rede))))
    rede4 = draw(st.sampled_from(_REDES_PROIBIDAS_V4))
    ip4 = draw(st.ip_addresses(network=rede4))
    if tipo == "v4":
        return str(ip4)
    if tipo == "v4_ponto":
        return f"{ip4}."
    if tipo == "v4_legado":
        return draw(_ipv4_forma_legada(ip4))
    return draw(_ipv4_mapeado(ip4))


@st.composite
def _host_publico(draw: st.DrawFn) -> str:
    """IP literal fora das faixas proibidas (padrão, legado ou IPv6) ou nome DNS comum."""
    tipo = draw(st.sampled_from(["v4", "v4_legado", "v6", "v4_mapeado", "dns", "dns_parecido"]))
    if tipo == "dns":
        return draw(_host())
    if tipo == "dns_parecido":
        return draw(
            _caixa_aleatoria(
                draw(
                    st.sampled_from(
                        [
                            "localhost.com",
                            "mylocalhost.net",
                            "localhost-br.io",
                            "localhostx",
                            "localhost.example.org",
                            "127.0.0.1.nip.io",
                            "10.0.0.1.example.com",
                            "example.com",
                        ]
                    )
                )
            )
        )
    if tipo == "v6":
        ip6 = draw(st.ip_addresses(v=6).filter(lambda i: not _ip_proibido(i)))
        return draw(_ipv6_entre_colchetes(ip6))
    ip4 = draw(st.ip_addresses(v=4).filter(lambda i: not _ip_proibido(i)))
    if tipo == "v4":
        return str(ip4)
    if tipo == "v4_legado":
        return draw(_ipv4_forma_legada(ip4))
    return draw(_ipv4_mapeado(ip4))


@st.composite
def _url_com_host(draw: st.DrawFn, host: str) -> str:
    """URL http(s) absoluta válida em torno do host, com userinfo, porta, caminho e espaços externos opcionais."""
    esquema = draw(_caixa_aleatoria(draw(st.sampled_from(["http", "https"]))))
    userinfo = draw(st.one_of(st.just(""), _host().map(lambda h: h + "@"), st.just("u:p@")))
    porta = draw(st.one_of(st.just(""), st.integers(min_value=1, max_value=65535).map(lambda p: f":{p}")))
    caminho = draw(_caminho())
    query = draw(st.one_of(st.just(""), st.just("?id=1"), st.just("?utm_source=x")))
    borda = draw(st.sampled_from(["", " ", "\t", "\n ", "\u00a0"]))
    return f"{borda}{esquema}://{userinfo}{host}{porta}{caminho}{query}{borda}"


@st.composite
def _url_invalida(draw: st.DrawFn) -> str | None:
    """URL inválida pelo Req 4.4, quase sempre com host proibido para testar a precedência (Req 4.8)."""
    host = draw(st.one_of(_host_proibido(), _host_publico()))
    esquema_ok = draw(st.sampled_from(["http", "HTTP", "https", "HttpS"]))
    return draw(
        st.one_of(
            st.none(),
            st.sampled_from(["", " ", "   ", "\t\n", "\u00a0\u2003", "\r\n"]),
            # esquema diferente de http/https
            st.sampled_from(["ftp", "file", "ws", "wss", "gopher", "httpx", "javascript", "data"]).map(
                lambda e: f"{e}://{host}/x"
            ),
            # relativas / sem esquema
            st.sampled_from([f"//{host}/x", f"{host}/x", f"/{host}", "/caminho", "?q=1", "#f"]),
            # sem host
            st.sampled_from([f"{esquema_ok}://", f"{esquema_ok}:///x", f"{esquema_ok}://:80/x", f"{esquema_ok}:{host}"]),
            # espaço ou caractere de controle internos
            st.sampled_from([" ", "\t", "\x00", "\x7f", "\n", "\u2003"]).map(
                lambda c: f"{esquema_ok}://{host}/a{c}b"
            ),
            # porta inválida
            st.sampled_from(["99999", "65536", "abc", "-1", "8o"]).map(
                lambda p: f"{esquema_ok}://{host}:{p}/"
            ),
        )
    )


_CASO_DESTINO = st.one_of(
    _host_proibido().flatmap(_url_com_host).map(lambda u: (u, "destino_nao_permitido")),
    _url_invalida().map(lambda u: (u, "url_invalida")),
    _host_publico().flatmap(_url_com_host).map(lambda u: (u, None)),
)


# Feature: html-extract-save, Property 12: Validação de destino
@settings(max_examples=100)
@given(caso=_CASO_DESTINO)
def test_propriedade_validacao_de_destino(caso: tuple[str | None, str | None]) -> None:
    """**Validates: Requirements 4.4, 4.5, 4.8**

    Vazias, relativas, sem host, com esquema não http(s), espaço interno ou
    porta inválida → `url_invalida`, mesmo com host proibido (precedência).
    `localhost`/`*.localhost` e IPs literais das faixas do Req 4.5 (inclusive
    IPv4-mapeados, zona IPv6 e formas IPv4 legadas) → `destino_nao_permitido`.
    IPs públicos e nomes DNS comuns → None.
    """
    url, esperado = caso
    assert motivo_recusa_url(url) == esperado




# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------

import unicodedata  # noqa: E402

from coleta_paginas.modelos import PecaConsultada  # noqa: E402
from coleta_paginas.selecao import trava_entrada  # noqa: E402

# Oráculo independente de `serper_decisao`: lista fechada do time de dados.
_GENERICAS_ORACULO = frozenset({"conversao", "original oem", "oem"})


def _oraculo_marca_sem_referencia(marca: str | None) -> bool:
    """Ausente/em branco, ou genérica após remover acentos, casefold e colapsar espaços."""
    if marca is None or not marca.strip():
        return True
    decomposta = unicodedata.normalize("NFKD", marca)
    sem_acento = "".join(c for c in decomposta if not unicodedata.combining(c))
    return " ".join(sem_acento.casefold().split()) in _GENERICAS_ORACULO


_ESPACOS = ["", " ", "  ", "\t", "\n", "\u00a0", "\u2003", "\u3000", "\u2009"]
_ACENTOS = {
    "a": ["a", "á", "ã", "â", "à"],
    "e": ["e", "é", "ê"],
    "o": ["o", "ó", "õ", "ô"],
    "i": ["i", "í"],
    "c": ["c", "ç"],
    "n": ["n", "ñ"],
}


@st.composite
def _com_acentos(draw: st.DrawFn, texto: str) -> str:
    """Troca letras por variantes acentuadas (pré-compostas ou com diacrítico combinante)."""
    saida = []
    for c in texto:
        variantes = _ACENTOS.get(c.lower())
        if variantes is None:
            saida.append(c)
            continue
        escolhido = draw(st.sampled_from(variantes))
        if draw(st.booleans()):
            escolhido += "\u0301"  # acento agudo combinante
        saida.append(escolhido)
    return "".join(saida)


@st.composite
def _marca_generica_ruidosa(draw: st.DrawFn) -> str:
    base = draw(st.sampled_from(["conversão", "conversao", "oem", "original oem"]))
    palavras = [draw(_caixa_aleatoria(draw(_com_acentos(p)))) for p in base.split()]
    borda = st.sampled_from(_ESPACOS)
    miolo = st.sampled_from([" ", "  ", "\t", "\u00a0", "\u2003", " \u3000 "])
    texto = draw(borda) + palavras[0]
    for palavra in palavras[1:]:
        texto += draw(miolo) + palavra
    return texto + draw(borda)


_MARCA_EM_BRANCO = st.one_of(
    st.none(),
    st.lists(st.sampled_from(_ESPACOS), max_size=4).map("".join),
)

_MARCA_REAL = st.one_of(
    st.sampled_from(
        [
            "BOSCH",
            "Cofap",
            "NAKATA",
            "Mahle",
            "SKF",
            "Fras-le",
            "OEMX",
            "OEM BOSCH",
            "ORIGINAL",
            "Conversão Plus",
            "conversao-oem",
            "original-oem",
            "O E M",
        ]
    ).flatmap(_caixa_aleatoria),
    st.text(max_size=12),  # arbitrária; o oráculo decide
)

_MARCA = st.one_of(_marca_generica_ruidosa(), _MARCA_EM_BRANCO, _MARCA_REAL)

_ALNUM = _LETRAS + _LETRAS.upper() + _DIGITOS
_SEPARADORES = " -./_"


@st.composite
def _codigo_valido(draw: st.DrawFn) -> str:
    """Código com ao menos um alfanumérico: curtos fracos ("70123"), com separadores, com acento."""
    forma = draw(st.sampled_from(["numerico", "separado", "livre", "fixo"]))
    if forma == "numerico":
        return draw(st.text(alphabet=_DIGITOS, min_size=1, max_size=8))
    if forma == "fixo":
        return draw(st.sampled_from(["70123", "83061", "830612", "JE-4699", "a", "7", "ÇA-1", " 12 "]))
    if forma == "separado":
        pedacos = draw(
            st.lists(st.text(alphabet=_ALNUM, min_size=1, max_size=5), min_size=1, max_size=4)
        )
        separadores = [draw(st.sampled_from(_SEPARADORES)) for _ in pedacos[1:]]
        texto = pedacos[0]
        for sep, pedaco in zip(separadores, pedacos[1:]):
            texto += sep + pedaco
        return texto
    nucleo = draw(st.sampled_from(_ALNUM + "éçã"))
    antes = draw(st.text(alphabet=_ALNUM + _SEPARADORES + "#*", max_size=6))
    depois = draw(st.text(alphabet=_ALNUM + _SEPARADORES + "#*", max_size=6))
    return antes + nucleo + depois


_NOMES = st.lists(st.text(max_size=20), max_size=4)


# Feature: html-extract-save, Property 1: A trava depende só da marca
@settings(max_examples=100)
@given(
    marca=_MARCA,
    codigo=_codigo_valido(),
    outro_codigo=_codigo_valido(),
    nomes=_NOMES,
    outros_nomes=_NOMES,
)
def test_propriedade_trava_depende_so_da_marca(
    marca: str | None,
    codigo: str,
    outro_codigo: str,
    nomes: list[str],
    outros_nomes: list[str],
) -> None:
    """**Validates: Requirements 1.1, 1.2, 1.3**

    Com Codigo_Peca válido, `trava_entrada` é verdadeira sse a marca é ausente,
    em branco ou genérica (CONVERSÃO, ORIGINAL OEM, OEM, com ruído de caixa,
    acento e espaços). O resultado não muda ao trocar código ou nomes, é
    determinístico e não altera a peça.
    """
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=nomes)
    copia = PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=list(nomes))
    esperado = _oraculo_marca_sem_referencia(marca)

    primeiro = trava_entrada(peca)
    assert primeiro is esperado
    assert trava_entrada(peca) is primeiro  # determinístico

    variante = PecaConsultada(
        codigo_peca=outro_codigo, marca_peca=marca, nomes_candidatos=outros_nomes
    )
    assert trava_entrada(variante) is primeiro  # independe de código e nomes

    # Sem mutação da peça (Req 1.3).
    assert peca == copia
    assert peca.codigo_peca == codigo
    assert peca.marca_peca is marca
    assert peca.nomes_candidatos == tuple(nomes)




# ---------------------------------------------------------------------------
# Property 4
# ---------------------------------------------------------------------------

from coleta_paginas.modelos import Confianca, ResultadoBusca  # noqa: E402
from coleta_paginas.selecao import (  # noqa: E402
    MOTIVO_CODIGO_AUSENTE,
    MOTIVO_CODIGO_E_MARCA,
    MOTIVO_CODIGO_SEM_MARCA,
    classificar_fontes,
)
from verification import serper_decisao  # noqa: E402
from verification.serper_client import ResultadoOrganico  # noqa: E402

# Tabela código × marca do design (Req 2.3–2.5).
_TABELA_CONFIANCA = {
    (False, False): (Confianca.REJEITADO, MOTIVO_CODIGO_AUSENTE),
    (False, True): (Confianca.REJEITADO, MOTIVO_CODIGO_AUSENTE),
    (True, True): (Confianca.ALTA, MOTIVO_CODIGO_E_MARCA),
    (True, False): (Confianca.MEDIA, MOTIVO_CODIGO_SEM_MARCA),
}

# Marca real: não em branco e não genérica (a peça precisa passar pela trava).
_MARCA_COM_REFERENCIA = _MARCA_REAL.filter(lambda m: not _oraculo_marca_sem_referencia(m))

# Ruído sem alfanumérico: não cria nem destrói fronteiras do código.
_RUIDO = st.sampled_from(["", " ", " - ", " | ", ", ", " / ", "(", ") ", ": ", "\u00a0", " — "])
_PALAVRAS = st.sampled_from(
    ["Amortecedor", "dianteiro", "Pastilha de freio", "kit", "Peça original", "compre já", "OEM"]
)


def _nucleo_codigo(codigo: str) -> list[str]:
    """Caracteres alfanuméricos do código após remover acentos e casefold
    (os mesmos que `serper_decisao._padrao_codigo` exige no texto)."""
    return [c for c in serper_decisao._remover_acentos(codigo).casefold() if c.isalnum()]


@st.composite
def _codigo_embutido(draw: st.DrawFn, codigo: str) -> str:
    """O código com separadores internos (`JE-4699`, `je 4699`, `J.E/46.99`),
    caixa sorteada e acentos acrescentados às letras."""
    nucleo = _nucleo_codigo(codigo)
    texto = nucleo[0]
    for c in nucleo[1:]:
        texto += draw(st.sampled_from(["", "", "-", " ", ".", "/", " - ", "\t"])) + c
    return draw(_caixa_aleatoria(draw(_com_acentos(texto))))


@st.composite
def _codigo_estendido(draw: st.DrawFn, codigo: str) -> str:
    """O código colado a alfanuméricos extras (`830612` para `83061`): nunca é
    uma ocorrência explícita, pela fronteira alfanumérica."""
    nucleo = "".join(_nucleo_codigo(codigo))
    antes = draw(st.text(alphabet=_LETRAS + _DIGITOS, max_size=2))
    depois = draw(st.text(alphabet=_LETRAS + _DIGITOS, max_size=2))
    if not antes and not depois:
        depois = draw(st.sampled_from(_LETRAS + _DIGITOS))
    return draw(_caixa_aleatoria(antes + nucleo + depois))


@st.composite
def _campo_texto(draw: st.DrawFn, codigo: str, marca: str) -> str | None:
    """Título/snippet montado por fragmentos: código embutido, código estendido,
    marca com caixa variada, palavras comuns, texto arbitrário ou nada."""
    if draw(st.integers(min_value=0, max_value=9)) == 0:
        return None
    fragmento = st.one_of(
        _codigo_embutido(codigo),
        _codigo_estendido(codigo),
        _caixa_aleatoria(marca),
        _PALAVRAS,
        st.text(max_size=8),
    )
    pedacos = draw(st.lists(fragmento, max_size=4))
    texto = draw(_RUIDO)
    for pedaco in pedacos:
        texto += pedaco + draw(_RUIDO.filter(bool))
    return texto


@st.composite
def _url_resultado(draw: st.DrawFn, codigo: str, marca: str) -> str | None:
    """URL ausente, arbitrária, ou com o código/marca no host ou no caminho."""
    host = draw(_host())
    trecho = draw(
        st.one_of(
            st.just(""),
            _codigo_embutido(codigo).map(lambda c: c.replace(" ", "-")),
            st.just(marca).map(lambda m: m.replace(" ", "-")),
            _SEGMENTO,
        )
    )
    return draw(
        st.one_of(
            st.none(),
            st.just(f"https://{host}/p/{trecho}"),
            st.just(f"http://{trecho.lower()}.{host}/"),
            st.text(max_size=10),
        )
    )


@st.composite
def _resultado_busca(draw: st.DrawFn, codigo: str, marca: str) -> ResultadoBusca:
    return ResultadoBusca(
        titulo=draw(_campo_texto(codigo, marca)),
        snippet=draw(_campo_texto(codigo, marca)),
        url=draw(_url_resultado(codigo, marca)),
        dominio=draw(st.one_of(st.none(), st.just(""), _host())),
        posicao=draw(st.one_of(st.none(), st.integers(min_value=0, max_value=20))),
    )


@st.composite
def _caso_classificacao(
    draw: st.DrawFn,
) -> tuple[PecaConsultada, list[ResultadoBusca]]:
    codigo = draw(_codigo_valido())
    marca = draw(_MARCA_COM_REFERENCIA)
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=draw(_NOMES))
    resultados = draw(st.lists(_resultado_busca(codigo, marca), max_size=6))
    return peca, resultados


def _oraculo_flags(peca: PecaConsultada, resultado: ResultadoBusca) -> tuple[bool, bool]:
    """Chamadas diretas às regras reutilizadas de `serper_decisao`."""
    texto = f"{resultado.titulo or ''} {resultado.snippet or ''}"
    codigo = serper_decisao.codigo_explicito(texto, peca.codigo_peca)
    marca = serper_decisao.marca_no_resultado(
        ResultadoOrganico(
            title=resultado.titulo or "",
            link=resultado.url or "",
            snippet=resultado.snippet or "",
            position=resultado.posicao,
        ),
        peca.marca_peca,
    )
    return codigo, marca


# Feature: html-extract-save, Property 4: A classificação segue as regras reutilizadas
@settings(max_examples=100)
@given(caso=_caso_classificacao())
def test_propriedade_classificacao_segue_regras_reutilizadas(
    caso: tuple[PecaConsultada, list[ResultadoBusca]],
) -> None:
    """**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.11**

    Uma decisão por resultado, na ordem. `codigo_confirmado` é
    `codigo_explicito(titulo + " " + snippet, codigo)` (o link nunca confirma o
    código), `marca_confirmada` é `marca_no_resultado(...)`, e confiança/motivo
    seguem a tabela código × marca. Determinístico e sem mutação das entradas.
    """
    peca, resultados = caso
    copia_peca = PecaConsultada(
        codigo_peca=peca.codigo_peca,
        marca_peca=peca.marca_peca,
        nomes_candidatos=list(peca.nomes_candidatos),
    )
    copia_resultados = list(resultados)

    decisoes = classificar_fontes(peca, resultados)

    assert isinstance(decisoes, tuple)
    assert len(decisoes) == len(resultados)
    for resultado, decisao in zip(resultados, decisoes):
        assert decisao.resultado == resultado

        codigo, marca = _oraculo_flags(peca, resultado)
        assert decisao.codigo_confirmado is codigo
        assert decisao.marca_confirmada is marca
        assert (decisao.confianca, decisao.motivo) == _TABELA_CONFIANCA[(codigo, marca)]
        assert decisao.motivo.strip()

        # O código só no link nunca confirma (Req 2.2).
        if not codigo:
            assert decisao.codigo_confirmado is False
            assert decisao.confianca is Confianca.REJEITADO

    # Determinístico e sem mutação (Req 2.11).
    assert classificar_fontes(peca, resultados) == decisoes
    assert peca == copia_peca
    assert resultados == copia_resultados


@settings(max_examples=100)
@given(
    codigo=_codigo_valido(),
    marca=_MARCA_COM_REFERENCIA,
    dados=st.data(),
)
def test_propriedade_codigo_estendido_ou_so_no_link_nao_confirma(
    codigo: str, marca: str, dados: st.DataObject
) -> None:
    """**Validates: Requirements 2.2, 2.3**

    Sub-asserção da Property 4: o código colado a alfanuméricos extras
    (`83061` em `830612`) não é confirmado, e o código presente só no link
    também não; nos dois casos a decisão é `rejeitado`.
    """
    estendido = dados.draw(_codigo_estendido(codigo))
    embutido = dados.draw(_codigo_embutido(codigo))
    titulo = dados.draw(_RUIDO) + estendido + dados.draw(_RUIDO)
    snippet = dados.draw(st.one_of(st.none(), _RUIDO))
    resultado = ResultadoBusca(
        titulo=titulo,
        snippet=snippet,
        url=f"https://loja.example.com/{embutido.replace(' ', '-')}",
        dominio="loja.example.com",
        posicao=1,
    )
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca)

    (decisao,) = classificar_fontes(peca, [resultado])

    assert decisao.codigo_confirmado is False
    assert decisao.confianca is Confianca.REJEITADO
    assert decisao.motivo == MOTIVO_CODIGO_AUSENTE

    # Caso fixo do design: "83061" não bate dentro de "830612".
    fixo = ResultadoBusca(
        titulo="Amortecedor 830612 BOSCH",
        snippet="ref 830612",
        url="https://loja.example.com/83061",
        dominio=None,
    )
    (decisao_fixa,) = classificar_fontes(
        PecaConsultada(codigo_peca="83061", marca_peca="BOSCH"), [fixo]
    )
    assert decisao_fixa.codigo_confirmado is False
    assert decisao_fixa.confianca is Confianca.REJEITADO




# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------

from hypothesis import example  # noqa: E402


@dataclass(frozen=True)
class _CasoNomes:
    """Duas peças iguais exceto pelos Nomes_Candidatos, e os mesmos resultados.

    `peca_a` recebe nomes derivados das palavras de título/snippet de alguns
    resultados (tendem a reforçar); `peca_b` recebe nomes arbitrários ou nenhum.
    `indices_derivados` aponta os resultados de onde saíram os nomes de `peca_a`.
    """

    peca_a: PecaConsultada
    peca_b: PecaConsultada
    resultados: tuple[ResultadoBusca, ...]
    indices_derivados: tuple[int, ...]


@st.composite
def _nome_derivado(draw: st.DrawFn, resultado: ResultadoBusca) -> str:
    """Subconjunto não vazio das palavras do resultado, em ordem e caixa sorteadas,
    às vezes com uma stopword que não deve impedir o reforço."""
    palavras = serper_decisao._tokens(resultado.titulo) + serper_decisao._tokens(resultado.snippet)
    if not palavras:
        return draw(_PALAVRAS)
    escolhidas = draw(
        st.lists(st.sampled_from(palavras), min_size=1, max_size=min(3, len(palavras)))
    )
    if draw(st.booleans()):
        escolhidas.insert(draw(st.integers(0, len(escolhidas))), draw(st.sampled_from(["de", "da", "com"])))
    return draw(_caixa_aleatoria(" ".join(escolhidas)))


@st.composite
def _caso_nomes(draw: st.DrawFn) -> _CasoNomes:
    peca, resultados = draw(_caso_classificacao())
    resultados = tuple(resultados)
    indices: tuple[int, ...] = ()
    nomes_a: list[str] = []
    if resultados:
        indices = tuple(
            draw(
                st.lists(
                    st.integers(0, len(resultados) - 1), min_size=1, max_size=3, unique=True
                )
            )
        )
        nomes_a = [draw(_nome_derivado(resultados[i])) for i in indices]
    nomes_a += draw(st.lists(st.text(max_size=12), max_size=2))
    nomes_b = draw(st.one_of(st.just([]), _NOMES))
    if draw(st.booleans()):
        nomes_a, nomes_b = nomes_b, nomes_a  # a ordem das listas não importa
        return _CasoNomes(
            peca_a=PecaConsultada(peca.codigo_peca, peca.marca_peca, nomes_a),
            peca_b=PecaConsultada(peca.codigo_peca, peca.marca_peca, nomes_b),
            resultados=resultados,
            indices_derivados=(),
        )
    return _CasoNomes(
        peca_a=PecaConsultada(peca.codigo_peca, peca.marca_peca, nomes_a),
        peca_b=PecaConsultada(peca.codigo_peca, peca.marca_peca, nomes_b),
        resultados=resultados,
        indices_derivados=indices,
    )


_RESULTADO_FIXO = ResultadoBusca(
    titulo="Amortecedor dianteiro JE-4699 Cofap",
    snippet="Amortecedor dianteiro original",
    url="https://loja.example.com/je-4699",
    dominio="loja.example.com",
    posicao=1,
)
_CASO_FIXO_REFORCO = _CasoNomes(
    peca_a=PecaConsultada("JE4699", "COFAP", ["Amortecedor Dianteiro"]),
    peca_b=PecaConsultada("JE4699", "COFAP", ["Pastilha de freio"]),
    resultados=(
        _RESULTADO_FIXO,
        ResultadoBusca(
            titulo="Amortecedor dianteiro",
            snippet=None,
            url="https://outra.example.com/x",
            dominio=None,
            posicao=2,
        ),
    ),
    indices_derivados=(0,),
)


# Feature: html-extract-save, Property 5: Os nomes não influenciam a confiança
@settings(max_examples=100)
@example(caso=_CASO_FIXO_REFORCO)
@given(caso=_caso_nomes())
def test_propriedade_nomes_nao_influenciam_confianca(caso: _CasoNomes) -> None:
    """**Validates: Requirements 2.8**

    Para a mesma peça e os mesmos resultados, trocar os Nomes_Candidatos não
    muda confiança, motivo, `codigo_confirmado` nem `marca_confirmada` de
    nenhuma decisão; só `nome_reforcado` pode diferir.
    """
    decisoes_a = classificar_fontes(caso.peca_a, caso.resultados)
    decisoes_b = classificar_fontes(caso.peca_b, caso.resultados)

    assert len(decisoes_a) == len(decisoes_b) == len(caso.resultados)
    for resultado, a, b in zip(caso.resultados, decisoes_a, decisoes_b):
        assert a.resultado == b.resultado == resultado
        assert a.confianca == b.confianca
        assert a.motivo == b.motivo
        assert a.codigo_confirmado is b.codigo_confirmado
        assert a.marca_confirmada is b.marca_confirmada

    # Sem nomes, nada é reforçado.
    for peca, decisoes in ((caso.peca_a, decisoes_a), (caso.peca_b, decisoes_b)):
        if not peca.nomes_candidatos:
            assert not any(d.nome_reforcado for d in decisoes)

    # Cobertura: um nome feito só de palavras do resultado o reforça, e com
    # `peca_b` sem nomes o indicador de fato difere, sem afetar a confiança.
    for i in caso.indices_derivados:
        resultado = caso.resultados[i]
        base = set(serper_decisao._tokens(resultado.titulo)) | set(
            serper_decisao._tokens(resultado.snippet)
        )
        if any(
            (s := serper_decisao._tokens_significativos(n)) and s <= base
            for n in caso.peca_a.nomes_candidatos
        ):
            assert decisoes_a[i].nome_reforcado is True
            if not caso.peca_b.nomes_candidatos:
                assert decisoes_b[i].nome_reforcado is False

    if caso is _CASO_FIXO_REFORCO:
        assert [d.nome_reforcado for d in decisoes_a] == [True, True]
        assert [d.nome_reforcado for d in decisoes_b] == [False, False]
        assert decisoes_a[0].confianca is Confianca.ALTA
        assert decisoes_a[1].confianca is Confianca.REJEITADO




# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from coleta_paginas.modelos import (  # noqa: E402
    CodigoPecaInvalidoError,
    PrecondicaoTravaError,
)

# Marca_Sem_Referencia: genérica com ruído, ausente ou em branco (inclui espaços Unicode).
_MARCA_SEM_REFERENCIA = st.one_of(_marca_generica_ruidosa(), _MARCA_EM_BRANCO)

# Codigo_Peca inválido: ausente, vazio, só espaços (inclui Unicode) ou só pontuação.
_PONTUACAO = " -./_#*()[]{}|,;:!?'\"+=&%$@~^`\\<>—–·\u00a0\u2003\t\n"
_CODIGO_INVALIDO = st.one_of(
    st.none(),
    st.lists(st.sampled_from(_ESPACOS), max_size=4).map("".join),
    st.text(alphabet=_PONTUACAO, max_size=10),
    st.sampled_from(["", "-", "---", " / ", "#*", "(—)", "\u0301", " \u0301 "]),
)


@st.composite
def _resultados_para_bloqueada(
    draw: st.DrawFn, codigo: str, marca: str | None
) -> list[ResultadoBusca]:
    """Qualquer lista (inclusive vazia), às vezes com resultados que trazem o código
    explícito e a marca no título/snippet — que confirmariam o código se a peça
    passasse pela trava."""
    marca_txt = marca or ""
    com_codigo = st.builds(
        lambda cod, pos: ResultadoBusca(
            titulo=f"Amortecedor {cod} {marca_txt}",
            snippet=f"ref {cod}",
            url=f"https://loja.example.com/p/{cod.replace(' ', '-')}",
            dominio="loja.example.com",
            posicao=pos,
        ),
        _codigo_embutido(codigo),
        st.one_of(st.none(), st.integers(min_value=0, max_value=20)),
    )
    return draw(
        st.lists(st.one_of(_resultado_busca(codigo, marca_txt), com_codigo), max_size=6)
    )


def _copia_peca(peca: PecaConsultada) -> PecaConsultada:
    return PecaConsultada(
        codigo_peca=peca.codigo_peca,
        marca_peca=peca.marca_peca,
        nomes_candidatos=list(peca.nomes_candidatos),
    )


# Feature: html-extract-save, Property 6: O classificador recusa peça bloqueada
@settings(max_examples=100)
@given(
    codigo=_codigo_valido(),
    marca=_MARCA_SEM_REFERENCIA,
    nomes=_NOMES,
    dados=st.data(),
)
def test_propriedade_classificador_recusa_peca_bloqueada(
    codigo: str, marca: str | None, nomes: list[str], dados: st.DataObject
) -> None:
    """**Validates: Requirements 2.10**

    Com Codigo_Peca válido e Marca_Sem_Referencia, `classificar_fontes` levanta
    `PrecondicaoTravaError` para qualquer lista de resultados (inclusive vazia e
    com o código explícito), sem devolver decisões e sem alterar as entradas.
    """
    resultados = dados.draw(_resultados_para_bloqueada(codigo, marca))
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=nomes)
    copia_peca = _copia_peca(peca)
    copia_resultados = list(resultados)

    # Oráculo independente: a peça de fato não passa pela trava.
    assert _oraculo_marca_sem_referencia(marca)

    devolvido: object = None
    with pytest.raises(PrecondicaoTravaError) as erro:
        devolvido = classificar_fontes(peca, resultados)

    assert type(erro.value) is PrecondicaoTravaError
    assert not isinstance(erro.value, CodigoPecaInvalidoError)
    assert devolvido is None  # nenhuma decisão, nem parcial

    assert peca == copia_peca
    assert resultados == copia_resultados


@settings(max_examples=100)
@given(
    codigo=_CODIGO_INVALIDO,
    codigo_resultados=_codigo_valido(),
    marca=_MARCA_SEM_REFERENCIA,
    dados=st.data(),
)
def test_propriedade_codigo_invalido_precede_trava_no_classificador(
    codigo: str | None,
    codigo_resultados: str,
    marca: str | None,
    dados: st.DataObject,
) -> None:
    """**Validates: Requirements 2.10**

    Sub-asserção da Property 6 (precedência, Req 2.9): com Codigo_Peca inválido
    e Marca_Sem_Referencia, o erro é `CodigoPecaInvalidoError`, não
    `PrecondicaoTravaError`, e nenhuma decisão é devolvida.
    """
    resultados = dados.draw(_resultados_para_bloqueada(codigo_resultados, marca))
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca)
    copia_peca = _copia_peca(peca)
    copia_resultados = list(resultados)

    devolvido: object = None
    with pytest.raises(CodigoPecaInvalidoError) as erro:
        devolvido = classificar_fontes(peca, resultados)

    assert type(erro.value) is CodigoPecaInvalidoError
    assert not isinstance(erro.value, PrecondicaoTravaError)
    assert erro.value.codigo == codigo
    assert devolvido is None

    assert peca == copia_peca
    assert resultados == copia_resultados




# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------

from coleta_paginas.modelos import DecisaoFonte  # noqa: E402
from coleta_paginas.selecao import selecionar_fontes  # noqa: E402

_CONFIANCAS = (Confianca.ALTA, Confianca.MEDIA, Confianca.REJEITADO)
_POSICOES = st.sampled_from([None, 0, 1, 1, 2, 3])  # repetição favorece empates


@dataclass(frozen=True)
class _GrupoUrl:
    """URL canônica de um grupo: toda grafia gerada normaliza para o mesmo valor,
    e grupos distintos (hosts distintos) nunca colidem."""

    esquema: str
    host: str
    caminho: str
    parametros: tuple[str, ...]  # pedaços não-utm, na ordem


@st.composite
def _grupo_url(draw: st.DrawFn, host: str) -> _GrupoUrl:
    return _GrupoUrl(
        esquema=draw(st.sampled_from(["http", "https"])),
        host=host,
        caminho=draw(st.sampled_from(["", "/p", "/p/1", "/Peca/JE-4699"])),
        parametros=draw(st.sampled_from([(), ("id=1",), ("id=1", "q=x"), ("flag",)])),
    )


@st.composite
def _grafia(draw: st.DrawFn, grupo: _GrupoUrl) -> str:
    """Grafia do grupo com caixa, porta padrão, barras finais, `utm_*`
    intercalados, `?` vazio e fragmento sorteados."""
    esquema = draw(_caixa_aleatoria(grupo.esquema))
    host = draw(_caixa_aleatoria(grupo.host))
    porta = draw(st.sampled_from(["", f":{_PORTAS_PADRAO[grupo.esquema]}"]))
    caminho = grupo.caminho + "/" * draw(st.integers(min_value=0, max_value=2))
    pedacos = list(grupo.parametros)
    for _ in range(draw(st.integers(min_value=0, max_value=2))):
        utm = draw(_caixa_aleatoria("utm_")) + draw(st.sampled_from(["source=a", "medium=b", "x"]))
        pedacos.insert(draw(st.integers(min_value=0, max_value=len(pedacos))), utm)
    query = "?" + "&".join(pedacos) if pedacos or draw(st.booleans()) else ""
    fragmento = draw(st.sampled_from(["", "#topo", "#a?b"]))
    return f"{esquema}://{host}{porta}{caminho}{query}{fragmento}"


_URL_RECUSADA = st.one_of(_url_invalida(), _host_proibido().flatmap(_url_com_host))


@dataclass(frozen=True)
class _CasoSelecao:
    decisoes: tuple[DecisaoFonte, ...]
    grupos: tuple[int | None, ...]  # grupo da URL de cada decisão; None = URL recusada
    teto: int


@st.composite
def _caso_selecao(draw: st.DrawFn) -> _CasoSelecao:
    hosts = draw(
        st.lists(_host().map(str.lower), min_size=1, max_size=4, unique=True)
    )
    grupos_url = [draw(_grupo_url(h)) for h in hosts]
    decisoes: list[DecisaoFonte] = []
    grupos: list[int | None] = []
    for _ in range(draw(st.integers(min_value=0, max_value=10))):
        if draw(st.integers(min_value=0, max_value=4)) == 0:
            url, grupo = draw(_URL_RECUSADA), None
        else:
            grupo = draw(st.integers(min_value=0, max_value=len(grupos_url) - 1))
            url = draw(_grafia(grupos_url[grupo]))
        confianca = draw(st.sampled_from(_CONFIANCAS))
        resultado = ResultadoBusca(
            titulo=draw(st.one_of(st.none(), _PALAVRAS)),
            snippet=None,
            url=url,
            dominio=draw(st.one_of(st.none(), st.just(""), st.just("  "), _host())),
            posicao=draw(_POSICOES),
        )
        decisoes.append(
            DecisaoFonte(
                resultado=resultado,
                confianca=confianca,
                codigo_confirmado=confianca is not Confianca.REJEITADO,
                marca_confirmada=confianca is Confianca.ALTA,
                nome_reforcado=draw(st.booleans()),
                motivo=draw(st.sampled_from([MOTIVO_CODIGO_AUSENTE, MOTIVO_CODIGO_E_MARCA, MOTIVO_CODIGO_SEM_MARCA])),
            )
        )
        grupos.append(grupo)
    return _CasoSelecao(
        decisoes=tuple(decisoes),
        grupos=tuple(grupos),
        teto=draw(st.integers(min_value=1, max_value=10)),
    )


def _chave_oraculo(indice: int, decisao: DecisaoFonte) -> tuple[int, int, int, int, int]:
    """Alta antes de media; nome reforçado primeiro; posição crescente com
    ausente por último; ordem de entrada (Req 3.2, 3.3)."""
    posicao = decisao.resultado.posicao
    return (
        0 if decisao.confianca is Confianca.ALTA else 1,
        0 if decisao.nome_reforcado else 1,
        1 if posicao is None else 0,
        0 if posicao is None else posicao,
        indice,
    )


# Feature: html-extract-save, Property 7: A seleção é ordenada, única e só tem aceitos permitidos
@settings(max_examples=100)
@given(caso=_caso_selecao())
def test_propriedade_selecao_ordenada_unica_so_aceitos_permitidos(caso: _CasoSelecao) -> None:
    """**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.12, 4.7**

    Cada selecionada é `alta`/`media` com URL permitida; as URLs_Normalizadas
    são distintas; a ordem segue a chave (confiança, nome reforçado, posição
    com ausente por último, índice de entrada), estritamente crescente; para
    cada URL_Normalizada mantida, a escolhida é a primeira nessa ordem entre os
    aceitos permitidos; rejeitados nunca aparecem em `selecionadas` nem em
    `excluidas`; determinística e sem mutação da entrada.
    """
    decisoes = list(caso.decisoes)
    copia = list(decisoes)
    indice_de = {id(d): i for i, d in enumerate(decisoes)}

    # Pré-condição do gerador: grafias de um grupo são permitidas; recusadas não.
    for decisao, grupo in zip(decisoes, caso.grupos):
        permitida = motivo_recusa_url(decisao.resultado.url) is None
        assert permitida is (grupo is not None)

    selecao = selecionar_fontes(decisoes, caso.teto)

    # Oráculo: aceitos permitidos agrupados por URL canônica, ordenados pela chave.
    aceitos_permitidos = [
        (i, d)
        for i, d in enumerate(decisoes)
        if d.confianca in (Confianca.ALTA, Confianca.MEDIA) and caso.grupos[i] is not None
    ]
    primeiro_do_grupo: dict[int, int] = {}
    for i, d in sorted(aceitos_permitidos, key=lambda par: _chave_oraculo(*par)):
        primeiro_do_grupo.setdefault(caso.grupos[i], i)

    indices = [indice_de[id(f.decisao)] for f in selecao.selecionadas]
    grupos_selecionados = [caso.grupos[i] for i in indices]

    for fonte, i in zip(selecao.selecionadas, indices):
        assert fonte.decisao.confianca in (Confianca.ALTA, Confianca.MEDIA)
        assert motivo_recusa_url(fonte.decisao.resultado.url) is None
        assert caso.grupos[i] is not None
        assert fonte.url_normalizada == normalizar_url(fonte.decisao.resultado.url)
        # A escolhida é a primeira do seu grupo na ordem do design (Req 3.4).
        assert primeiro_do_grupo[caso.grupos[i]] == i

    # Unicidade por URL_Normalizada, tanto pelo grupo canônico quanto pelo valor.
    assert len(set(grupos_selecionados)) == len(grupos_selecionados)
    urls = [f.url_normalizada for f in selecao.selecionadas]
    assert len(set(urls)) == len(urls)

    # Ordem estritamente crescente pela chave.
    chaves = [_chave_oraculo(i, decisoes[i]) for i in indices]
    assert all(a < b for a, b in zip(chaves, chaves[1:]))

    # Rejeitados nunca aparecem em nenhuma das saídas (Req 3.1, 4.7).
    for exclusao in selecao.excluidas:
        assert exclusao.decisao.confianca is not Confianca.REJEITADO
        assert caso.grupos[indice_de[id(exclusao.decisao)]] is None
    for fonte in selecao.selecionadas:
        assert fonte.decisao.confianca is not Confianca.REJEITADO

    # Determinística e sem mutação (Req 3.12).
    assert selecionar_fontes(decisoes, caso.teto) == selecao
    assert decisoes == copia
    assert all(a is b for a, b in zip(decisoes, copia))




# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------

from coleta_paginas.modelos import ExclusaoUrl  # noqa: E402

_DOMINIO_EM_BRANCO = st.sampled_from([None, "", " ", "  ", "\t", "\n", "\u00a0", " \u2003 "])


@dataclass(frozen=True)
class _GrupoComPorta:
    """Grupo de URL da Property 7 com porta não padrão opcional, presente em todas
    as grafias (fica na URL_Normalizada, mas nunca no domínio efetivo)."""

    grupo: _GrupoUrl
    porta: int | None


@st.composite
def _grupo_com_porta(draw: st.DrawFn, host: str) -> _GrupoComPorta:
    grupo = draw(_grupo_url(host))
    padrao = _PORTAS_PADRAO[grupo.esquema]
    porta = draw(
        st.one_of(
            st.none(),
            st.integers(min_value=1, max_value=65535).filter(lambda p: p != padrao),
        )
    )
    return _GrupoComPorta(grupo=grupo, porta=porta)


@st.composite
def _grafia_com_porta(draw: st.DrawFn, gp: _GrupoComPorta) -> str:
    """Grafia de `_grafia`, trocando a porta (padrão ou ausente) pela porta do grupo."""
    base = draw(_grafia(gp.grupo))
    if gp.porta is None:
        return base
    corte = len(gp.grupo.esquema) + 3 + len(gp.grupo.host)
    resto = base[corte:].removeprefix(f":{_PORTAS_PADRAO[gp.grupo.esquema]}")
    return f"{base[:corte]}:{gp.porta}{resto}"


@dataclass(frozen=True)
class _ItemP8:
    decisao: DecisaoFonte
    grupo: int | None  # None = URL recusada
    motivo_esperado: str | None  # motivo que o oráculo espera para a URL


@dataclass(frozen=True)
class _CasoTamanho:
    grupos_url: tuple[_GrupoComPorta, ...]
    itens: tuple[_ItemP8, ...]
    extras: tuple[tuple[int, _ItemP8], ...]  # (posição de inserção, aceito com URL recusada)
    teto: int


@st.composite
def _url_recusada_com_motivo(draw: st.DrawFn) -> tuple[str | None, str]:
    """URL recusada e o motivo esperado; `_url_invalida` quase sempre tem host
    proibido, exercitando a precedência de `url_invalida` (Req 4.8)."""
    return draw(
        st.one_of(
            _url_invalida().map(lambda u: (u, "url_invalida")),
            _host_proibido().flatmap(_url_com_host).map(lambda u: (u, "destino_nao_permitido")),
        )
    )


@st.composite
def _decisao_p8(
    draw: st.DrawFn,
    grupos_url: list[_GrupoComPorta],
    *,
    forcar_recusada_aceita: bool = False,
) -> _ItemP8:
    if forcar_recusada_aceita or draw(st.integers(min_value=0, max_value=3)) == 0:
        (url, motivo), grupo = draw(_url_recusada_com_motivo()), None
    else:
        grupo = draw(st.integers(min_value=0, max_value=len(grupos_url) - 1))
        url, motivo = draw(_grafia_com_porta(grupos_url[grupo])), None
    confianca = (
        draw(st.sampled_from(_CONFIANCAS[:2]))
        if forcar_recusada_aceita
        else draw(st.sampled_from(_CONFIANCAS))
    )
    resultado = ResultadoBusca(
        titulo=draw(st.one_of(st.none(), _PALAVRAS)),
        snippet=None,
        url=url,
        dominio=draw(st.one_of(_DOMINIO_EM_BRANCO, _host(), _host().map(lambda h: f" {h} "))),
        posicao=draw(_POSICOES),
    )
    decisao = DecisaoFonte(
        resultado=resultado,
        confianca=confianca,
        codigo_confirmado=confianca is not Confianca.REJEITADO,
        marca_confirmada=confianca is Confianca.ALTA,
        nome_reforcado=draw(st.booleans()),
        motivo=MOTIVO_CODIGO_AUSENTE if confianca is Confianca.REJEITADO else MOTIVO_CODIGO_SEM_MARCA,
    )
    return _ItemP8(decisao=decisao, grupo=grupo, motivo_esperado=motivo)


@st.composite
def _caso_tamanho(draw: st.DrawFn) -> _CasoTamanho:
    hosts = draw(st.lists(_host().map(str.lower), min_size=1, max_size=4, unique=True))
    grupos_url = [draw(_grupo_com_porta(h)) for h in hosts]
    itens = draw(st.lists(_decisao_p8(grupos_url), max_size=10))
    extras = draw(
        st.lists(
            st.tuples(
                st.integers(min_value=0, max_value=len(itens)),
                _decisao_p8(grupos_url, forcar_recusada_aceita=True),
            ),
            max_size=3,
        )
    )
    return _CasoTamanho(
        grupos_url=tuple(grupos_url),
        itens=tuple(itens),
        extras=tuple(extras),
        teto=draw(st.integers(min_value=1, max_value=12)),
    )


def _host_esperado(gp: _GrupoComPorta) -> str:
    """Oráculo do host da URL_Normalizada: minúsculas, sem porta."""
    return gp.grupo.host.lower()


# Feature: html-extract-save, Property 8: Tamanho, prefixo e exclusões da seleção
@settings(max_examples=100)
@given(caso=_caso_tamanho())
def test_propriedade_tamanho_prefixo_exclusoes_da_selecao(caso: _CasoTamanho) -> None:
    """**Validates: Requirements 3.6, 3.10, 3.11, 4.6, 4.8**

    Com D = número de URLs_Normalizadas distintas entre os aceitos com URL
    permitida: `len(selecionadas) == min(teto, D)`; a seleção com teto a é
    prefixo da seleção com teto b ≥ a; `excluidas` tem exatamente um item por
    aceito com URL recusada, na ordem de entrada, com o motivo de
    `motivo_recusa_url` (`url_invalida` prevalece) e é a mesma para qualquer
    teto; aceitos recusados não ocupam vaga; o domínio efetivo é o original
    quando não está em branco e, senão, o host da URL_Normalizada.
    """
    decisoes = [item.decisao for item in caso.itens]

    # Pré-condições do gerador (oráculo das URLs).
    for item in caso.itens:
        assert motivo_recusa_url(item.decisao.resultado.url) == item.motivo_esperado
        assert (item.grupo is None) is (item.motivo_esperado is not None)

    aceitos = [
        item for item in caso.itens if item.decisao.confianca in (Confianca.ALTA, Confianca.MEDIA)
    ]
    grupos_distintos = {item.grupo for item in aceitos if item.grupo is not None}
    d = len(grupos_distintos)
    # D pelo oráculo de grupos coincide com D por URL_Normalizada.
    assert d == len(
        {normalizar_url(item.decisao.resultado.url) for item in aceitos if item.grupo is not None}
    )

    excluidas_esperadas = tuple(
        ExclusaoUrl(decisao=item.decisao, motivo=item.motivo_esperado)
        for item in aceitos
        if item.grupo is None
    )

    selecao = selecionar_fontes(decisoes, caso.teto)

    # Tamanho (Req 3.10, 3.11).
    assert len(selecao.selecionadas) == min(caso.teto, d)

    # Exclusões: uma por aceito recusado, ordem de entrada, motivo com precedência (Req 3.6, 4.8).
    assert selecao.excluidas == excluidas_esperadas
    for exclusao in selecao.excluidas:
        assert exclusao.motivo == motivo_recusa_url(exclusao.decisao.resultado.url)

    # Prefixo e exclusões independentes do teto (Req 3.11).
    tetos = range(1, max(caso.teto, d) + 3)
    selecoes = {t: selecionar_fontes(decisoes, t) for t in tetos}
    for a in tetos:
        assert len(selecoes[a].selecionadas) == min(a, d)
        assert selecoes[a].excluidas == excluidas_esperadas
        for b in tetos:
            if a <= b:
                assert selecoes[b].selecionadas[: len(selecoes[a].selecionadas)] == selecoes[a].selecionadas
    assert selecoes[caso.teto] == selecao

    # Aceitos com URL recusada não ocupam vaga (Req 3.6): inseri-los não muda a seleção.
    com_extras = list(caso.itens)
    for posicao, extra in sorted(caso.extras, key=lambda par: par[0], reverse=True):
        com_extras.insert(posicao, extra)
    selecao_extras = selecionar_fontes([item.decisao for item in com_extras], caso.teto)
    assert selecao_extras.selecionadas == selecao.selecionadas
    assert selecao_extras.excluidas == tuple(
        ExclusaoUrl(decisao=item.decisao, motivo=item.motivo_esperado)
        for item in com_extras
        if item.decisao.confianca in (Confianca.ALTA, Confianca.MEDIA) and item.grupo is None
    )

    # Domínio efetivo (Req 4.6).
    grupo_de = {id(item.decisao): item.grupo for item in caso.itens}
    for fonte in selecao.selecionadas:
        assert fonte.dominio.strip()
        original = fonte.decisao.resultado.dominio
        if original is not None and original.strip():
            assert fonte.dominio == original
        else:
            gp = caso.grupos_url[grupo_de[id(fonte.decisao)]]
            assert fonte.dominio == _host_esperado(gp)
            assert ":" not in fonte.dominio
            if gp.porta is not None:
                assert f":{gp.porta}" in fonte.url_normalizada




# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------

import sqlite3  # noqa: E402
import tempfile  # noqa: E402
from collections.abc import Iterator, Mapping  # noqa: E402
from contextlib import closing, contextmanager  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

from coleta_paginas.coletor import ColetorPaginas, ConfigColeta  # noqa: E402
from coleta_paginas.modelos import ConfiguracaoColetaError, RelatorioColeta  # noqa: E402
from db.armazem_paginas import ArmazemPaginas, NovoRegistroColeta  # noqa: E402
from tools.buscador_paginas import ErroTransporte, RespostaHTTP  # noqa: E402

_INSTANTE_P2 = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


class _TransporteContadorP2:
    """Transporte fake local: conta as chamadas e nunca devolve página."""

    def __init__(self) -> None:
        self.chamadas: list[str] = []

    def __call__(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> RespostaHTTP:
        self.chamadas.append(url)
        raise ErroTransporte("rede")


@contextmanager
def _armazem_temporario_p2() -> Iterator[ArmazemPaginas]:
    """Armazém em diretório temporário próprio, com o RuleStore no mesmo diretório."""
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        yield ArmazemPaginas(
            base / "paginas.db",
            caminho_rule_store=base / "estagiario.db",
            relogio=lambda: _INSTANTE_P2,
        )


def _snapshot_p2(caminho: Path) -> tuple[object, ...]:
    """Schema e todas as linhas de todas as tabelas, lidos com `sqlite3` puro."""
    with closing(sqlite3.connect(caminho)) as conn:
        objetos = conn.execute(
            "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tabelas = [nome for tipo, nome, _ in objetos if tipo == "table"]
        linhas = tuple(
            (tabela, tuple(conn.execute(f'SELECT * FROM "{tabela}" ORDER BY rowid').fetchall()))
            for tabela in tabelas
        )
        versao = conn.execute("PRAGMA user_version").fetchone()
    return (tuple(objetos), linhas, versao)


def _prepopular_p2(armazem: ArmazemPaginas, conteudos: list[bytes]) -> None:
    """Estado prévio não trivial: conteúdos soltos e um registro de coleta."""
    for conteudo in conteudos:
        armazem.armazenar_conteudo(conteudo)
    armazem.gravar_coleta(
        b"<html>pagina previa</html>",
        NovoRegistroColeta(
            codigo_peca="JE4699",
            marca_peca="COFAP",
            url_original="https://loja.example.com/je-4699",
            url_normalizada="https://loja.example.com/je-4699",
            url_final="https://loja.example.com/je-4699",
            dominio="loja.example.com",
            confianca="alta",
            codigo_confirmado=True,
            marca_confirmada=True,
            nome_reforcado=False,
            motivo_decisao=MOTIVO_CODIGO_E_MARCA,
            status_http=200,
            content_type="text/html; charset=utf-8",
            charset="utf-8",
            coletado_em=_INSTANTE_P2 - timedelta(days=1),
        ),
    )


# Teto/janela válidos e inválidos, de chamada e de ambiente.
_TETO_CHAMADA_P2 = st.one_of(
    st.none(),
    st.integers(min_value=-3, max_value=10),
    st.sampled_from([True, False, 2.0, "3", "abc", 0.5]),
)
_TEXTO_CONFIG_P2 = st.one_of(
    st.none(),
    st.sampled_from(["3", " 7 ", "1", "0", "-1", "2.0", "abc", "", "  ", "999", "366", "30"]),
    st.text(max_size=5),
)
_JANELA_P2 = st.one_of(
    _TEXTO_CONFIG_P2,
    st.integers(min_value=-2, max_value=400),
    st.sampled_from([True, False]),
)


# Feature: html-extract-save, Property 2: Código inválido falha primeiro
@settings(max_examples=100, deadline=None)
@given(
    codigo=_CODIGO_INVALIDO,
    codigo_resultados=_codigo_valido(),
    marca=_MARCA,
    nomes=_NOMES,
    teto_chamada=_TETO_CHAMADA_P2,
    teto_ambiente=_TEXTO_CONFIG_P2,
    janela=_JANELA_P2,
    conteudos=st.lists(st.binary(max_size=64), max_size=3),
    dados=st.data(),
)
def test_propriedade_codigo_invalido_falha_primeiro(
    codigo: str | None,
    codigo_resultados: str,
    marca: str | None,
    nomes: list[str],
    teto_chamada: object,
    teto_ambiente: str | None,
    janela: object,
    conteudos: list[bytes],
    dados: st.DataObject,
) -> None:
    """**Validates: Requirements 1.5, 1.6, 2.9**

    Com Codigo_Peca inválido, qualquer marca (genérica, em branco ou real),
    quaisquer resultados (inclusive com o código válido embutido) e qualquer
    configuração de teto/janela, `trava_entrada`, `classificar_fontes` e
    `ColetorPaginas.coletar` levantam `CodigoPecaInvalidoError` com o código
    recebido — nunca `PrecondicaoTravaError` nem `ConfiguracaoColetaError`,
    nunca um relatório `nao_enriquecivel` — sem chamar o transporte e sem
    alterar o armazém.
    """
    resultados = dados.draw(
        st.lists(_resultado_busca(codigo_resultados, marca or ""), max_size=5)
    )
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=nomes)
    copia_peca = _copia_peca(peca)
    copia_resultados = list(resultados)

    def _verificar_erro(erro: pytest.ExceptionInfo[CodigoPecaInvalidoError]) -> None:
        assert type(erro.value) is CodigoPecaInvalidoError
        assert not isinstance(erro.value, (PrecondicaoTravaError, ConfiguracaoColetaError))
        assert erro.value.codigo == codigo

    # trava_entrada (Req 1.5).
    devolvido_trava: object = None
    with pytest.raises(CodigoPecaInvalidoError) as erro_trava:
        devolvido_trava = trava_entrada(peca)
    _verificar_erro(erro_trava)
    assert devolvido_trava is None

    # classificar_fontes (Req 2.9).
    devolvido_classificacao: object = None
    with pytest.raises(CodigoPecaInvalidoError) as erro_classificacao:
        devolvido_classificacao = classificar_fontes(peca, resultados)
    _verificar_erro(erro_classificacao)
    assert devolvido_classificacao is None

    # ColetorPaginas.coletar (Req 1.6): antes da trava e da configuração.
    with _armazem_temporario_p2() as armazem:
        _prepopular_p2(armazem, conteudos)
        antes = _snapshot_p2(armazem.db_path)
        transporte = _TransporteContadorP2()
        coletor = ColetorPaginas(
            armazem,
            transporte=transporte,
            config=ConfigColeta(
                timeout_s=15.0,
                teto_aceitos_ambiente=teto_ambiente,
                janela_reuso_dias=janela,  # type: ignore[arg-type]
            ),
            relogio=lambda: _INSTANTE_P2,
        )

        relatorio: RelatorioColeta | None = None
        with pytest.raises(CodigoPecaInvalidoError) as erro_coleta:
            relatorio = coletor.coletar(
                peca, resultados, teto_aceitos=teto_chamada  # type: ignore[arg-type]
            )
        _verificar_erro(erro_coleta)
        assert relatorio is None  # nem `nao_enriquecivel`, nem `executada`

        assert transporte.chamadas == []
        assert _snapshot_p2(armazem.db_path) == antes

    assert peca == copia_peca
    assert resultados == copia_resultados




# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------

import math  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
from unittest.mock import patch  # noqa: E402

import config  # noqa: E402
from coleta_paginas.coletor import resolver_janela_reuso  # noqa: E402
from coleta_paginas.selecao import resolver_teto_aceitos  # noqa: E402

_ENV_TIMEOUT_P9 = "ESTAGIARIO_COLETA_TIMEOUT"
_ENV_TETO_P9 = "ESTAGIARIO_COLETA_TETO_ACEITOS"
_ENV_JANELA_P9 = "ESTAGIARIO_COLETA_JANELA_REUSO_DIAS"


@contextmanager
def _ambiente_p9(valores: Mapping[str, str | None]) -> Iterator[None]:
    """Define (ou remove, com `None`) variáveis de ambiente só durante o bloco.

    Não usamos `monkeypatch` aqui: é fixture de escopo de função, e o Hypothesis
    recusa (health check `function_scoped_fixture`) fixtures desse escopo em
    testes `@given`, porque o estado não seria reiniciado entre exemplos.
    `patch.dict(os.environ)` restaura o ambiente inteiro ao sair de cada
    exemplo, inclusive valores que o `.env` tenha carregado na importação.
    """
    with patch.dict(os.environ):
        for nome, valor in valores.items():
            if valor is None:
                os.environ.pop(nome, None)
            else:
                os.environ[nome] = valor
        yield


def _inteiro_decimal_p9(texto: str) -> int:
    """Valor de um texto só com dígitos ASCII, sem o limite de dígitos de `int(str)`."""
    valor = 0
    for c in texto:
        valor = valor * 10 + (ord(c) - ord("0"))
    return valor


def _digitos_ascii_p9(texto: str) -> bool:
    """Oráculo de `[0-9]+`: não vazio, só dígitos ASCII (exclui `٣`, `３`, sinais, `_`)."""
    return bool(texto) and all("0" <= c <= "9" for c in texto)


# Resultado esperado: ("ok", valor) ou ("erro", valor_repr, origem).
_Esperado = tuple[str, object] | tuple[str, str, str]


def _oraculo_teto_p9(chamada: object, ambiente: str | None) -> _Esperado:
    """Req 3.7–3.9: chamada > ambiente > 3.

    Texto do ambiente com mais dígitos significativos (sem zeros à esquerda)
    que o limite de `int(str)` (`sys.get_int_max_str_digits()`, quando não é 0)
    é rejeitado com origem `ambiente` (decisão do usuário, alinhada à janela)."""
    if chamada is not None:
        if type(chamada) is int and chamada >= 1:
            return ("ok", chamada)
        return ("erro", repr(chamada), "chamada")
    if ambiente is not None:
        texto = ambiente.strip()
        limite = sys.get_int_max_str_digits()
        cabe_em_int = not limite or len(texto.lstrip("0")) <= limite
        if _digitos_ascii_p9(texto) and cabe_em_int and _inteiro_decimal_p9(texto) >= 1:
            return ("ok", _inteiro_decimal_p9(texto))
        return ("erro", repr(ambiente), "ambiente")
    return ("ok", 3)


def _oraculo_janela_p9(valor: object) -> _Esperado:
    """Req 6.4, 6.12: None → 30; int (não bool) ou texto `[0-9]+` em [1, 365]."""
    if valor is None:
        return ("ok", 30)
    dias: int | None = None
    if type(valor) is int:
        dias = valor
    elif isinstance(valor, str) and _digitos_ascii_p9(valor.strip()):
        dias = _inteiro_decimal_p9(valor.strip())
    if dias is not None and 1 <= dias <= 365:
        return ("ok", dias)
    return ("erro", repr(valor), "ambiente")


def _oraculo_timeout_p9(bruto: str | None) -> float:
    """Req 5.4: numérico finito em [1, 120] vale; qualquer outro caso → 15.0, sem clamp.

    "Numérico" é o que `float()` do Python aceita (inclui `1e1`, `1_0` e
    dígitos Unicode como `٣`/`３`); é a única leitura razoável de texto numérico.
    """
    if bruto is None:
        return 15.0
    try:
        valor = float(bruto)
    except ValueError:
        return 15.0
    return valor if math.isfinite(valor) and 1.0 <= valor <= 120.0 else 15.0


# Texto de configuração: dígitos Unicode, sinais, espaços (inclui Unicode),
# decimais, notação científica, `_`, números enormes, vazio.
_TETO_ENORME_P9 = "1" * 4301  # além do limite padrão de dígitos de int(str)
_TEXTOS_FIXOS_P9 = [
    "", " ", "\t\n", "0", "00", "1", "3", " 7 ", "007", "+3", "-1", "-0", "2.0", "3.",
    ".5", "1e2", "1_000", "٣", "３", "١٢", "\u00a03\u2003", "\u30003\u3000", "3 4",
    "inf", "-inf", "nan", "abc", "0x10", "30", "365", "366", "0365", "0000030", "999",
    "120", "120.0", "120.0001", "0.999", "1.0", "60", "15", "9" * 30, "1" + "0" * 400,
    _TETO_ENORME_P9,
]
_CARACTERES_NUMERICOS_P9 = "0123456789+-._eE \t\u00a0\u2003٣３٠０"
_TEXTO_NUMERICO_P9 = st.one_of(
    st.sampled_from(_TEXTOS_FIXOS_P9),
    st.text(alphabet=_CARACTERES_NUMERICOS_P9, max_size=8),
    st.builds(
        lambda n, zeros, borda: f"{borda}{'0' * zeros}{n}{borda}",
        st.integers(min_value=-10, max_value=10**30),
        st.integers(min_value=0, max_value=3),
        st.sampled_from(["", " ", "\t", "\u00a0"]),
    ),
    st.floats(allow_nan=True, allow_infinity=True).map(repr),
    # Texto arbitrário representável no ambiente (sem NUL nem surrogates).
    st.text(
        alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\x00"),
        max_size=6,
    ),
)
_TEXTO_CONFIG_P9 = st.one_of(st.none(), _TEXTO_NUMERICO_P9)

_TETO_CHAMADA_P9 = st.one_of(
    st.none(),
    st.integers(min_value=-5, max_value=12),
    st.integers(min_value=-(10**30), max_value=10**30),
    st.booleans(),
    st.sampled_from([2.0, 1.0, 0.5, float("nan"), float("inf"), "3", "٣", " 1 "]),
)
_JANELA_P9 = st.one_of(
    _TEXTO_CONFIG_P9,
    st.integers(min_value=-5, max_value=400),
    st.integers(min_value=-(10**30), max_value=10**30),
    st.booleans(),
    st.sampled_from([30.0, 1.5, float("nan")]),
)


def _resolver_p9(funcao: object, *args: object) -> _Esperado:
    """Chama o resolvedor e traduz o desfecho para o formato do oráculo.

    Só `ConfiguracaoColetaError` é traduzido; qualquer outra exceção sobe e
    falha o teste (o contrato não prevê outra)."""
    try:
        return ("ok", funcao(*args))  # type: ignore[operator]
    except ConfiguracaoColetaError as erro:
        assert type(erro) is ConfiguracaoColetaError
        return ("erro", erro.valor, erro.origem)


# Feature: html-extract-save, Property 9: Resolução de configuração
@settings(max_examples=100, deadline=None)
@example(chamada=None, ambiente=_TETO_ENORME_P9)
@example(chamada=None, ambiente="0" * 4301 + "7")
@example(chamada=None, ambiente="10000000000")
@example(chamada=None, ambiente="٣")
@example(chamada=None, ambiente="３")
@example(chamada=True, ambiente="3")
@example(chamada=5, ambiente="abc")
@example(chamada=None, ambiente=None)
@given(chamada=_TETO_CHAMADA_P9, ambiente=_TEXTO_CONFIG_P9)
def test_propriedade_resolucao_teto_aceitos(chamada: object, ambiente: str | None) -> None:
    """**Validates: Requirements 3.7, 3.8, 3.9**

    Chamada informada decide sozinha (o ambiente é ignorado, válido ou não):
    `int` (não `bool`) ≥ 1, senão erro com origem `chamada`. Sem chamada vale o
    ambiente: texto que após `strip()` é só dígitos ASCII e vale ≥ 1, com no
    máximo `sys.get_int_max_str_digits()` dígitos significativos, senão erro
    com origem `ambiente`. Sem nenhum dos dois, 3. O erro
    é `ConfiguracaoColetaError(campo="teto_aceitos", valor=repr(valor), origem)`.
    """
    esperado = _oraculo_teto_p9(chamada, ambiente)
    obtido = _resolver_p9(resolver_teto_aceitos, chamada, ambiente)
    assert obtido == esperado
    if esperado[0] == "ok":
        assert type(obtido[1]) is int

    if chamada is None and ambiente == _TETO_ENORME_P9:
        assert esperado == ("erro", repr(_TETO_ENORME_P9), "ambiente")

    if esperado[0] == "erro":
        with pytest.raises(ConfiguracaoColetaError) as erro:
            resolver_teto_aceitos(chamada, ambiente)
        assert erro.value.campo == "teto_aceitos"

    # Com chamada informada, trocar o ambiente não muda nada.
    if chamada is not None:
        for outro in (None, "7", "abc", ""):
            assert _resolver_p9(resolver_teto_aceitos, chamada, outro) == esperado


# Feature: html-extract-save, Property 9: Resolução de configuração
@settings(max_examples=100, deadline=None)
@example(valor="0000000000030")
@example(valor="٣٠")
@example(valor=True)
@example(valor=365)
@example(valor=366)
@example(valor="9" * 5000)
@example(valor="0" * 4301 + "30")
@given(valor=_JANELA_P9)
def test_propriedade_resolucao_janela_reuso(valor: object) -> None:
    """**Validates: Requirements 6.4, 6.12**

    `None` → 30. Aceita `int` (não `bool`) ou texto que após `strip()` é só
    dígitos ASCII, com valor em [1, 365]. Qualquer outro valor levanta
    `ConfiguracaoColetaError(campo="janela_reuso_dias", valor=repr(valor))`.
    """
    esperado = _oraculo_janela_p9(valor)
    obtido = _resolver_p9(resolver_janela_reuso, valor)
    assert obtido == esperado
    if esperado[0] == "ok":
        assert type(obtido[1]) is int
    else:
        with pytest.raises(ConfiguracaoColetaError) as erro:
            resolver_janela_reuso(valor)
        assert erro.value.campo == "janela_reuso_dias"


# Feature: html-extract-save, Property 9: Resolução de configuração
@settings(max_examples=100, deadline=None)
@example(timeout="120", teto="3", janela="30")
@example(timeout="0.999", teto=None, janela=None)
@example(timeout="120.0001", teto=None, janela=None)
@example(timeout="inf", teto=None, janela=None)
@example(timeout="nan", teto=None, janela=None)
@example(timeout=None, teto=None, janela=None)
@given(timeout=_TEXTO_CONFIG_P9, teto=_TEXTO_CONFIG_P9, janela=_TEXTO_CONFIG_P9)
def test_propriedade_resolucao_coleta_timeout(
    timeout: str | None, teto: str | None, janela: str | None
) -> None:
    """**Validates: Requirements 5.4**

    Para todo texto (ou ausência) em `ESTAGIARIO_COLETA_TIMEOUT`,
    `coleta_timeout()` devolve o valor se for numérico finito em [1, 120] e 15
    nos demais casos, sem clamp. `ConfigColeta.do_ambiente()` usa esse timeout
    e repassa teto e janela brutos, sem validar.
    """
    with _ambiente_p9({_ENV_TIMEOUT_P9: timeout, _ENV_TETO_P9: teto, _ENV_JANELA_P9: janela}):
        obtido = config.coleta_timeout()
        cfg = ConfigColeta.do_ambiente()

    esperado = _oraculo_timeout_p9(timeout)
    assert type(obtido) is float
    assert obtido == esperado
    assert 1.0 <= obtido <= 120.0

    assert cfg.timeout_s == esperado
    assert cfg.teto_aceitos_ambiente == teto
    assert cfg.janela_reuso_dias == janela


# Feature: html-extract-save, Property 9: Resolução de configuração
@settings(max_examples=100, deadline=None)
@given(
    codigo=_codigo_valido(),
    marca=_MARCA_COM_REFERENCIA,
    nomes=_NOMES,
    teto_chamada=_TETO_CHAMADA_P9,
    teto_ambiente=_TEXTO_CONFIG_P9,
    janela=_JANELA_P9,
    conteudos=st.lists(st.binary(max_size=64), max_size=3),
    dados=st.data(),
)
def test_propriedade_configuracao_invalida_sem_efeitos_no_coletor(
    codigo: str,
    marca: str,
    nomes: list[str],
    teto_chamada: object,
    teto_ambiente: str | None,
    janela: object,
    conteudos: list[bytes],
    dados: st.DataObject,
) -> None:
    """**Validates: Requirements 3.9, 6.12**

    Com peça que passa pela trava (código válido, marca real), teto inválido
    (da chamada ou do ambiente) ou janela inválida fazem `coletar` levantar
    `ConfiguracaoColetaError` com o campo, valor e origem do oráculo — teto
    antes da janela —, sem nenhuma chamada ao transporte e sem alterar o
    armazém. Com configuração válida, `coletar` não levanta esse erro.
    """
    resultados = dados.draw(st.lists(_resultado_busca(codigo, marca), max_size=5))
    peca = PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=nomes)
    assert not _oraculo_marca_sem_referencia(marca)  # pré-condição: passa pela trava

    esperado_teto = _oraculo_teto_p9(teto_chamada, teto_ambiente)
    esperado_janela = _oraculo_janela_p9(janela)
    if esperado_teto[0] == "erro":
        esperado_erro: tuple[str, str, str] | None = ("teto_aceitos", *esperado_teto[1:])  # type: ignore[assignment]
    elif esperado_janela[0] == "erro":
        esperado_erro = ("janela_reuso_dias", *esperado_janela[1:])  # type: ignore[assignment]
    else:
        esperado_erro = None

    with _armazem_temporario_p2() as armazem:
        _prepopular_p2(armazem, conteudos)
        antes = _snapshot_p2(armazem.db_path)
        transporte = _TransporteContadorP2()
        coletor = ColetorPaginas(
            armazem,
            transporte=transporte,
            config=ConfigColeta(
                timeout_s=15.0,
                teto_aceitos_ambiente=teto_ambiente,
                janela_reuso_dias=janela,  # type: ignore[arg-type]
            ),
            relogio=lambda: _INSTANTE_P2,
        )

        if esperado_erro is not None:
            relatorio: RelatorioColeta | None = None
            with pytest.raises(ConfiguracaoColetaError) as erro:
                relatorio = coletor.coletar(
                    peca, resultados, teto_aceitos=teto_chamada  # type: ignore[arg-type]
                )
            assert type(erro.value) is ConfiguracaoColetaError
            assert (erro.value.campo, erro.value.valor, erro.value.origem) == esperado_erro
            assert relatorio is None
            assert transporte.chamadas == []
            assert _snapshot_p2(armazem.db_path) == antes
        else:
            relatorio = coletor.coletar(
                peca, resultados, teto_aceitos=teto_chamada  # type: ignore[arg-type]
            )
            assert relatorio.status == "executada"
            # Sem redirecionamentos (o fake falha na 1ª chamada): no máximo uma
            # requisição por fonte selecionada, e nunca mais que o teto.
            assert len(transporte.chamadas) <= esperado_teto[1]  # type: ignore[operator]
