"""Testes de propriedade do ``BuscadorPaginas`` (html-extract-save, Req 5).

Os testes usam um transporte fake roteirizado (``TransporteRoteirizado``): cada
chamada consome o próximo passo do roteiro, que pode ser um ``ErroTransporte``
levantado na própria chamada ou uma resposta com status, cabeçalhos, blocos de
corpo (com ``ErroTransporte`` possível no meio da iteração) e um ``fechar`` que
pode falhar. O fake registra URL, cabeçalhos e timeout de cada chamada e quantas
vezes cada resposta foi fechada. Nenhum teste abre rede (guarda ``autouse``).

As estratégias e o fake são reutilizados pelas Properties 14 e 15.
"""

from __future__ import annotations

import gzip
import math
import re
import urllib.parse
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from hypothesis import example, given, settings
from hypothesis import strategies as st

import config
from coleta_paginas.url import motivo_recusa_url
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tools.buscador_paginas import (
    CABECALHOS_FIXOS,
    LIMITE_CORPO_BYTES,
    MAX_REDIRECIONAMENTOS,
    STATUS_REDIRECIONAMENTO,
    BuscadorPaginas,
    ErroTransporte,
    FalhaBusca,
    PaginaBaixada,
    RespostaHTTP,
)

MAX_REQUISICOES = 1 + MAX_REDIRECIONAMENTOS  # 6 GETs por busca (Req 5.5)
_CABECALHOS_FIXOS_ORIGINAIS = dict(CABECALHOS_FIXOS)
_NOMES_PROIBIDOS = frozenset({"cookie", "authorization", "proxy-authorization"})

# ---------------------------------------------------------------------------
# Transporte fake roteirizado
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErroNoPasso:
    """Marca um ``ErroTransporte`` a levantar (na chamada ou no meio do corpo)."""

    tipo: str  # "rede" | "timeout"


@dataclass(frozen=True)
class PassoResposta:
    """Uma resposta roteirizada. ``blocos`` pode conter ``ErroNoPasso``."""

    status: int
    cabecalhos: tuple[tuple[str, str], ...]
    blocos: tuple[bytes | ErroNoPasso, ...]
    erro_ao_fechar: ErroNoPasso | None = None

    def eh_redirecionamento(self) -> bool:
        if self.status not in STATUS_REDIRECIONAMENTO:
            return False
        return any(
            nome.lower() == "location" and valor.strip() for nome, valor in self.cabecalhos
        )


Passo = PassoResposta | ErroNoPasso


@dataclass
class ChamadaRegistrada:
    url: str
    cabecalhos: dict[str, str]
    timeout: float
    passo: Passo
    fechamentos: int = 0
    blocos_lidos: int = 0


@dataclass
class TransporteRoteirizado:
    """Transporte fake: consome o roteiro em ordem e registra cada chamada.

    Com o roteiro esgotado, a chamada levanta ``ErroTransporte("rede")`` — um
    comportamento válido de transporte, também registrado.
    """

    roteiro: tuple[Passo, ...]
    chamadas: list[ChamadaRegistrada] = field(default_factory=list)

    def __call__(
        self, url: str, cabecalhos: Mapping[str, str], timeout: float
    ) -> RespostaHTTP:
        indice = len(self.chamadas)
        passo: Passo = (
            self.roteiro[indice] if indice < len(self.roteiro) else ErroNoPasso("rede")
        )
        chamada = ChamadaRegistrada(
            url=url, cabecalhos=dict(cabecalhos), timeout=timeout, passo=passo
        )
        self.chamadas.append(chamada)
        if isinstance(passo, ErroNoPasso):
            raise ErroTransporte(passo.tipo)  # type: ignore[arg-type]

        def blocos() -> Iterator[bytes]:
            for bloco in passo.blocos:
                if isinstance(bloco, ErroNoPasso):
                    raise ErroTransporte(bloco.tipo)  # type: ignore[arg-type]
                chamada.blocos_lidos += 1
                yield bloco

        def fechar() -> None:
            chamada.fechamentos += 1
            if passo.erro_ao_fechar is not None:
                raise ErroTransporte(passo.erro_ao_fechar.tipo)  # type: ignore[arg-type]

        return RespostaHTTP(
            status=passo.status,
            cabecalhos=dict(passo.cabecalhos),
            blocos=blocos(),
            fechar=fechar,
        )


@dataclass
class RelogioFake:
    """Relógio monotônico que avança ``passo`` segundos a cada leitura."""

    passo: float
    agora: float = 0.0

    def __call__(self) -> float:
        valor = self.agora
        self.agora += self.passo
        return valor


# ---------------------------------------------------------------------------
# Estratégias reutilizáveis
# ---------------------------------------------------------------------------

URLS_PERMITIDAS = (
    "https://www.exemplo.com.br/peca/JE-4699",
    "http://loja.exemplo.com/produto?id=10",
    "https://catalogo.fabricante.com/itens/83061/",
)
URLS_PROIBIDAS = (
    "http://127.0.0.1/admin",
    "http://localhost:8080/",
    "http://10.0.0.5/x",
    "ftp://arquivos.exemplo.com/a",
    "http://[::1]/",
)
LOCATIONS_RELATIVAS = ("/outra", "pagina2?x=1", "../sobe", "//cdn.exemplo.com/p")

st_tipo_erro = st.sampled_from(("rede", "timeout"))
st_erro = st_tipo_erro.map(ErroNoPasso)

st_url_inicial = st.tuples(
    st.sampled_from(URLS_PERMITIDAS), st.sampled_from(("", " ", "\t", "\n  "))
).map(lambda t: f"{t[1]}{t[0]}{t[1]}")

st_status = st.one_of(
    st.sampled_from(sorted(STATUS_REDIRECIONAMENTO)),
    st.sampled_from((200, 201, 204, 206, 299, 300, 304, 400, 403, 404, 500, 503)),
    st.integers(min_value=100, max_value=599),
)


def _com_caixa_variada(nome: str) -> st.SearchStrategy[str]:
    return st.sampled_from((nome, nome.lower(), nome.upper(), nome.title()))


st_location = st.one_of(
    st.sampled_from(URLS_PERMITIDAS),
    st.sampled_from(URLS_PROIBIDAS),
    st.sampled_from(LOCATIONS_RELATIVAS),
    st.just(""),
    st.just("   "),
)
st_content_type = st.sampled_from(
    (
        "text/html",
        "text/html; charset=utf-8",
        'TEXT/HTML; Charset="ISO-8859-1"',
        "application/xhtml+xml",
        "application/json",
        "text/plain",
        "image/png",
        "",
    )
)
st_content_encoding = st.sampled_from(
    ("", "identity", "gzip", "GZIP ", "br", "deflate", "gzip, br")
)
st_content_length = st.one_of(
    st.integers(min_value=0, max_value=10 * 1024 * 1024).map(str),
    st.sampled_from(("", "abc", "-1", "99999999999")),
)
st_set_cookie = st.sampled_from(
    (
        "sessao=abc123; Path=/; HttpOnly",
        "rastreio=xyz; Domain=.exemplo.com; Secure",
        "Authorization=Bearer segredo",
    )
)

# Cada cabeçalho opcional com nome em caixa variada. Set-Cookie, WWW-Authenticate
# e Authorization na resposta nunca podem voltar como Cookie/Authorization.
_CABECALHOS_OPCIONAIS: tuple[tuple[str, st.SearchStrategy[str]], ...] = (
    ("Location", st_location),
    ("Content-Type", st_content_type),
    ("Content-Encoding", st_content_encoding),
    ("Content-Length", st_content_length),
    ("Set-Cookie", st_set_cookie),
    ("WWW-Authenticate", st.just('Basic realm="x"')),
    ("Authorization", st.just("Basic dXNlcjpzZW5oYQ==")),
    ("X-Extra", st.text(max_size=10)),
)


@st.composite
def st_cabecalhos(draw: st.DrawFn) -> tuple[tuple[str, str], ...]:
    pares: list[tuple[str, str]] = []
    for nome, valor in _CABECALHOS_OPCIONAIS:
        if draw(st.booleans()):
            pares.append((draw(_com_caixa_variada(nome)), draw(valor)))
    return tuple(pares)


_HTML = b"<html><head><title>JE-4699</title></head><body>peca</body></html>"
st_bytes_corpo = st.one_of(
    st.binary(max_size=64),
    st.just(_HTML),
    st.just(gzip.compress(_HTML)),
    st.just(b""),
)
st_blocos = st.lists(st.one_of(st_bytes_corpo, st_erro), max_size=4).map(tuple)

st_passo_resposta = st.builds(
    PassoResposta,
    status=st_status,
    cabecalhos=st_cabecalhos(),
    blocos=st_blocos,
    erro_ao_fechar=st.one_of(st.none(), st_erro),
)
# Redirecionamento permitido explícito, para que as cadeias sejam frequentes.
st_passo_redirecionamento = st.builds(
    PassoResposta,
    status=st.sampled_from(sorted(STATUS_REDIRECIONAMENTO)),
    cabecalhos=st.tuples(
        st.tuples(
            _com_caixa_variada("Location"),
            st.one_of(st.sampled_from(URLS_PERMITIDAS), st.sampled_from(LOCATIONS_RELATIVAS)),
        ),
        st.tuples(_com_caixa_variada("Set-Cookie"), st_set_cookie),
    ),
    blocos=st_blocos,
    erro_ao_fechar=st.one_of(st.none(), st_erro),
)
# Resposta HTML 200 bem-formada (crua ou gzip), para que o sucesso seja frequente.
st_passo_html_ok = st.one_of(
    st.builds(
        PassoResposta,
        status=st.just(200),
        cabecalhos=st.tuples(
            st.tuples(_com_caixa_variada("Content-Type"), st.just("text/html; charset=utf-8")),
            st.tuples(_com_caixa_variada("Set-Cookie"), st_set_cookie),
        ),
        blocos=st.just((_HTML[:20], _HTML[20:])),
        erro_ao_fechar=st.one_of(st.none(), st_erro),
    ),
    st.builds(
        PassoResposta,
        status=st.just(200),
        cabecalhos=st.just(
            (("Content-Type", "text/html"), ("Content-Encoding", "gzip"))
        ),
        blocos=st.just((gzip.compress(_HTML),)),
        erro_ao_fechar=st.none(),
    ),
)
st_passo: st.SearchStrategy[Passo] = st.one_of(
    st_passo_resposta, st_passo_redirecionamento, st_passo_html_ok, st_erro
)
# Roteiros livres e cadeias de 0 a 7 redirecionamentos seguidos de um passo final,
# para exercitar o limite de 6 requisições.
st_roteiro_livre = st.lists(st_passo, min_size=0, max_size=8).map(tuple)
st_roteiro_cadeia = st.tuples(
    st.lists(st_passo_redirecionamento, min_size=0, max_size=MAX_REQUISICOES + 1),
    st_passo,
).map(lambda t: (*t[0], t[1]))
st_roteiro = st.one_of(st_roteiro_livre, st_roteiro_cadeia)

# Timeout explícito: válidos, fora do intervalo, bool, NaN/inf e ausente (None).
st_timeout = st.one_of(
    st.none(),
    st.floats(min_value=1.0, max_value=120.0),
    st.integers(min_value=1, max_value=120),
    st.floats(allow_nan=True, allow_infinity=True),
    st.integers(min_value=-1000, max_value=1000),
    st.booleans(),
)


def timeout_esperado(valor: object) -> float:
    """Oráculo do Req 5.4: numérico finito em [1, 120], senão 15 s; None usa o ambiente."""
    if valor is None:
        return config.coleta_timeout()
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return 15.0
    numero = float(valor)
    if math.isfinite(numero) and 1.0 <= numero <= 120.0:
        return numero
    return 15.0


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------


# Feature: html-extract-save, Property 13: Uma busca, um desfecho, cabeçalhos fixos
@settings(max_examples=100)
@given(
    url=st_url_inicial,
    roteiro=st_roteiro,
    timeout=st_timeout,
    passo_relogio=st.floats(min_value=0.0, max_value=50.0),
)
def test_property_13_uma_busca_um_desfecho_cabecalhos_fixos(
    url: str, roteiro: tuple[Passo, ...], timeout: object, passo_relogio: float
) -> None:
    """**Validates: Requirements 5.1, 5.3, 5.4, 5.8**"""
    transporte = TransporteRoteirizado(roteiro)
    buscador = BuscadorPaginas(
        validar_destino=motivo_recusa_url,
        transporte=transporte,
        timeout=timeout,  # type: ignore[arg-type]
        relogio_monotonico=RelogioFake(passo_relogio),
    )

    # Req 5.1: nunca levanta; exatamente um desfecho.
    desfecho = buscador.buscar(url)
    assert isinstance(desfecho, (PaginaBaixada, FalhaBusca))

    chamadas = transporte.chamadas
    # Pelo menos a requisição inicial, e no máximo 1 + 5 redirecionamentos.
    assert 1 <= len(chamadas) <= MAX_REQUISICOES
    assert chamadas[0].url == url.strip()

    esperado = timeout_esperado(timeout)
    for chamada in chamadas:
        # Req 5.3: exatamente os cabeçalhos fixos, sem estado entre saltos.
        assert chamada.cabecalhos == _CABECALHOS_FIXOS_ORIGINAIS
        assert not {n.lower() for n in chamada.cabecalhos} & _NOMES_PROIBIDOS
        # Req 5.4: o timeout resolvido é passado a toda requisição.
        assert chamada.timeout == esperado
        # Toda resposta obtida é fechada exatamente uma vez.
        if isinstance(chamada.passo, PassoResposta):
            assert chamada.fechamentos == 1

    # Sem retry: toda chamada antes da última foi um redirecionamento seguido.
    for chamada in chamadas[:-1]:
        assert isinstance(chamada.passo, PassoResposta)
        assert chamada.passo.eh_redirecionamento()
        assert chamada.blocos_lidos == 0  # o corpo do 3xx não é lido

    # Req 5.8: erro de transporte na chamada vira falha do mesmo tipo.
    ultimo = chamadas[-1].passo
    if isinstance(ultimo, ErroNoPasso):
        assert desfecho == FalhaBusca(ultimo.tipo, chamadas[-1].url)  # type: ignore[arg-type]

    # Os cabeçalhos fixos do módulo não foram alterados.
    assert dict(CABECALHOS_FIXOS) == _CABECALHOS_FIXOS_ORIGINAIS



# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------

# Destinos proibidos adicionais, inclusive relativos ao esquema, que só viram
# proibidos depois de resolvidos por urljoin.
LOCATIONS_PROIBIDAS = (*URLS_PROIBIDAS, "//127.0.0.1/x", "//localhost/y", "//[::1]:8080/")
st_espacos = st.sampled_from(("", " ", "\t", "  "))
st_location_permitida = st.one_of(
    st.sampled_from(URLS_PERMITIDAS), st.sampled_from(LOCATIONS_RELATIVAS)
)
st_passo_final_html = st.sampled_from(
    (
        PassoResposta(
            status=200,
            cabecalhos=(("Content-Type", "text/html; charset=utf-8"),),
            blocos=(_HTML[:20], _HTML[20:]),
        ),
        PassoResposta(
            status=200,
            cabecalhos=(("content-type", "text/html"), ("Content-Encoding", "gzip")),
            blocos=(gzip.compress(_HTML),),
        ),
    )
)


@dataclass(frozen=True)
class CadeiaRedirecionamentos:
    url_inicial: str
    locations: tuple[str, ...]  # valor bruto do cabeçalho Location de cada salto
    roteiro: tuple[Passo, ...]


@st.composite
def st_cadeia(draw: st.DrawFn) -> CadeiaRedirecionamentos:
    """k saltos 3xx (k em 0..8) com Location absoluta ou relativa, opcionalmente
    um destino proibido num salto aleatório, seguidos de uma resposta HTML 200."""
    k = draw(st.integers(min_value=0, max_value=MAX_REQUISICOES + 2))
    locations = [draw(st_location_permitida) for _ in range(k)]
    if k and draw(st.booleans()):
        indice = draw(st.integers(min_value=0, max_value=k - 1))
        locations[indice] = draw(st.sampled_from(LOCATIONS_PROIBIDAS))
    locations = [f"{draw(st_espacos)}{loc}{draw(st_espacos)}" for loc in locations]

    passos: list[Passo] = []
    for location in locations:
        cabecalhos: list[tuple[str, str]] = [(draw(_com_caixa_variada("Location")), location)]
        if draw(st.booleans()):
            cabecalhos.append((draw(_com_caixa_variada("Set-Cookie")), draw(st_set_cookie)))
        passos.append(
            PassoResposta(
                status=draw(st.sampled_from(sorted(STATUS_REDIRECIONAMENTO))),
                cabecalhos=tuple(cabecalhos),
                blocos=draw(st.lists(st_bytes_corpo, max_size=2).map(tuple)),
            )
        )
    passos.append(draw(st_passo_final_html))
    return CadeiaRedirecionamentos(
        url_inicial=draw(st_url_inicial),
        locations=tuple(locations),
        roteiro=tuple(passos),
    )


def oraculo_cadeia(
    cadeia: CadeiaRedirecionamentos,
) -> tuple[list[str], str, str]:
    """(URLs requisitadas, desfecho esperado, URL do desfecho), via urljoin.

    Desfechos: ``"sucesso"`` (url_final), ``"redirecionamento_nao_permitido"``
    (destino recusado, nunca requisitado) ou ``"redirecionamentos_excedidos"``
    (última URL alcançada, após exatamente 6 requisições).
    """
    atual = cadeia.url_inicial.strip()
    requisitadas = [atual]
    for saltos, location in enumerate(cadeia.locations):
        if saltos == MAX_REDIRECIONAMENTOS:
            return requisitadas, "redirecionamentos_excedidos", atual
        alvo = urllib.parse.urljoin(atual, location.strip())
        if motivo_recusa_url(alvo) is not None:
            return requisitadas, "redirecionamento_nao_permitido", alvo
        requisitadas.append(alvo)
        atual = alvo
    return requisitadas, "sucesso", atual


# Feature: html-extract-save, Property 14: Cadeias de redirecionamento
@settings(max_examples=100)
@given(cadeia=st_cadeia())
def test_property_14_cadeias_de_redirecionamento(cadeia: CadeiaRedirecionamentos) -> None:
    """**Validates: Requirements 5.5, 5.6, 5.13**"""
    transporte = TransporteRoteirizado(cadeia.roteiro)
    buscador = BuscadorPaginas(
        validar_destino=motivo_recusa_url,
        transporte=transporte,
        timeout=15.0,
        relogio_monotonico=RelogioFake(0.0),
    )

    desfecho = buscador.buscar(cadeia.url_inicial)
    requisitadas, esperado, url_esperada = oraculo_cadeia(cadeia)

    # O transporte recebe exatamente as URLs resolvidas da cadeia, em ordem.
    assert [c.url for c in transporte.chamadas] == requisitadas
    assert len(requisitadas) <= MAX_REQUISICOES
    # Nenhuma URL requisitada é um destino proibido (Req 5.6).
    assert all(motivo_recusa_url(u) is None for u in requisitadas)
    # O corpo dos 3xx nunca é lido.
    for chamada in transporte.chamadas:
        if isinstance(chamada.passo, PassoResposta) and chamada.passo.eh_redirecionamento():
            assert chamada.blocos_lidos == 0

    if esperado == "sucesso":
        assert isinstance(desfecho, PaginaBaixada)
        assert desfecho.url_final == url_esperada
        assert desfecho.status_http == 200
        assert desfecho.corpo == _HTML
    elif esperado == "redirecionamento_nao_permitido":
        assert isinstance(desfecho, FalhaBusca)
        assert desfecho.motivo == "redirecionamento_nao_permitido"
        assert desfecho.url == url_esperada
        assert url_esperada not in requisitadas
    else:
        assert len(cadeia.locations) > MAX_REDIRECIONAMENTOS
        assert isinstance(desfecho, FalhaBusca)
        assert desfecho.motivo == "redirecionamentos_excedidos"
        assert desfecho.url == url_esperada == requisitadas[-1]
        assert len(transporte.chamadas) == MAX_REQUISICOES



# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------

# Tipos gerados por componentes: o oráculo sabe se o tipo é HTML e qual charset
# foi declarado pela construção, sem reinterpretar o cabeçalho.
_TIPOS_CONTENT_TYPE: tuple[tuple[str, bool], ...] = (
    ("text/html", True),
    ("TEXT/HTML", True),
    ("Text/Html", True),
    ("application/xhtml+xml", True),
    ("Application/XHTML+XML", True),
    ("text/plain", False),
    ("application/json", False),
    ("image/png", False),
    ("text/htmlx", False),
    ("html", False),
    ("", False),
)
_PARAMETROS_EXTRAS = ("q=0.9", "boundary=abc", "level=1")
_VALORES_CHARSET = ("utf-8", "UTF-8", "ISO-8859-1", "windows-1252", "")
_CODIFICACOES = (
    None,  # cabeçalho ausente
    "",
    "identity",
    "IDENTITY",
    "gzip",
    " gzip ",
    "GZIP",
    "br",
    "deflate",
    "gzip, br",
    "x-gzip",
)
_CONTENT_LENGTHS = (
    None,  # cabeçalho ausente
    "0",
    "120",
    str(LIMITE_CORPO_BYTES),
    str(LIMITE_CORPO_BYTES + 1),
    f" {LIMITE_CORPO_BYTES + 1} ",
    "99999999999",
    "abc",
    "-1",
    "+6000000",
    "",
    "٥٥٥٥٥٥٥٥",  # dígitos não ASCII: não é um comprimento declarado
)
_LIXO_APOS_GZIP = (b"lixo", b"\x1f", b"\x00\x00lixo", b"<script>alert(1)</script>")


@dataclass(frozen=True)
class CasoRespostaFinal:
    """Uma única resposta final (sem redirecionamento seguido) e seus componentes."""

    url: str
    status: int
    content_type: str | None  # valor bruto enviado, ou None se ausente
    tipo_html: bool
    charset: str | None  # charset declarado na construção
    content_encoding: str | None
    content_length: str | None
    cabecalhos: tuple[tuple[str, str], ...]
    blocos: tuple[bytes, ...] = field(repr=False)

    @property
    def fio(self) -> bytes:
        return b"".join(self.blocos)

    def __repr__(self) -> str:  # sem despejar MiB de corpo no relatório do Hypothesis
        return (
            f"CasoRespostaFinal(url={self.url!r}, status={self.status}, "
            f"content_type={self.content_type!r}, charset={self.charset!r}, "
            f"content_encoding={self.content_encoding!r}, "
            f"content_length={self.content_length!r}, cabecalhos={self.cabecalhos!r}, "
            f"blocos={len(self.blocos)} blocos / {len(self.fio)} bytes, "
            f"fio[:64]={self.fio[:64]!r})"
        )


def _cabecalhos_do_caso(
    content_type: str | None,
    content_encoding: str | None,
    content_length: str | None,
    location_vazia: str | None = None,
) -> tuple[tuple[str, str], ...]:
    pares: list[tuple[str, str]] = []
    if content_type is not None:
        pares.append(("Content-Type", content_type))
    if content_encoding is not None:
        pares.append(("Content-Encoding", content_encoding))
    if content_length is not None:
        pares.append(("Content-Length", content_length))
    if location_vazia is not None:
        pares.append(("Location", location_vazia))
    return tuple(pares)


@st.composite
def st_content_type_componentes(draw: st.DrawFn) -> tuple[str, bool, str | None]:
    """(valor bruto, tipo é HTML, charset declarado ou None)."""
    tipo, eh_html = draw(st.sampled_from(_TIPOS_CONTENT_TYPE + _TIPOS_HTML_PONDERADOS))
    espaco = st.sampled_from(("", " ", "  "))
    parametros: list[str] = []
    if draw(st.booleans()):
        parametros.append(draw(st.sampled_from(_PARAMETROS_EXTRAS)))
    charset: str | None = None
    if draw(st.booleans()):
        nome = draw(st.sampled_from(("charset", "Charset", "CHARSET")))
        valor = draw(st.sampled_from(_VALORES_CHARSET))
        aspas = draw(st.sampled_from(("", '"', "'")))
        texto = (
            f"{draw(espaco)}{nome}{draw(espaco)}={draw(espaco)}"
            f"{aspas}{valor}{aspas}{draw(espaco)}"
        )
        parametros.insert(draw(st.integers(0, len(parametros))), texto)
        charset = valor or None
    bruto = f"{draw(espaco)}{tipo}{draw(espaco)}" + "".join(f";{p}" for p in parametros)
    return bruto, eh_html, charset


@st.composite
def st_fio_corpo(draw: st.DrawFn) -> bytes:
    """Bytes do corpo na rede: cru, gzip (um ou vários membros, com zeros de
    preenchimento), gzip truncado, corrompido ou seguido de lixo. Payloads
    pequenos: a corrupção nunca produz mais que 5 MiB decodificados."""
    payload = draw(st.one_of(st.binary(max_size=200), st.just(_HTML), st.just(b"")))
    forma = draw(
        st.sampled_from(
            ("cru", "gzip", "multimembro", "gzip_zeros", "truncado", "corrompido", "gzip_lixo")
        )
    )
    comprimido = gzip.compress(payload, mtime=0)
    if forma == "cru":
        return payload
    if forma == "gzip":
        return comprimido
    if forma == "multimembro":
        corte = draw(st.integers(0, len(payload)))
        return gzip.compress(payload[:corte], mtime=0) + gzip.compress(payload[corte:], mtime=0)
    if forma == "gzip_zeros":
        return comprimido + b"\x00" * draw(st.integers(1, 8))
    if forma == "truncado":
        return comprimido[: draw(st.integers(0, len(comprimido) - 1))]
    if forma == "corrompido":
        # Só depois do cabeçalho fixo de 10 bytes, onde zlib e o módulo gzip
        # validam igual (bits reservados do FLG são tratados de modo diferente).
        posicao = draw(st.integers(10, len(comprimido) - 1))
        mascara = draw(st.integers(1, 255))
        alterado = bytearray(comprimido)
        alterado[posicao] ^= mascara
        return bytes(alterado)
    return comprimido + draw(st.sampled_from(_LIXO_APOS_GZIP))


@st.composite
def st_blocos_de(draw: st.DrawFn, fio: bytes) -> tuple[bytes, ...]:
    """Divide o fio em blocos por cortes aleatórios (blocos vazios possíveis)."""
    cortes = sorted(draw(st.lists(st.integers(0, len(fio)), max_size=4)))
    limites = [0, *cortes, len(fio)]
    return tuple(fio[a:b] for a, b in zip(limites, limites[1:]))


# Pesos por repetição: 2xx e tipos HTML predominam, para que os ramos depois
# das verificações de cabeçalho (decodificação, tamanho, vazio, sucesso) sejam
# frequentes em 100 exemplos.
st_status_final = st.one_of(
    st.just(200),
    st.just(200),
    st.sampled_from((200, 201, 203, 204, 206, 299)),
    st.sampled_from((200, 201, 203, 204, 206, 299)),
    st.sampled_from((300, 301, 302, 303, 304, 307, 308, 400, 404, 500, 503)),
    st.integers(min_value=100, max_value=599),
)
_TIPOS_HTML_PONDERADOS = tuple(t for t in _TIPOS_CONTENT_TYPE if t[1]) * 2


@st.composite
def st_caso_resposta_final(draw: st.DrawFn) -> CasoRespostaFinal:
    status = draw(st_status_final)
    content_type: str | None = None
    tipo_html = False
    charset: str | None = None
    if draw(st.sampled_from((True, True, True, False))):
        content_type, tipo_html, charset = draw(st_content_type_componentes())
    content_encoding = draw(st.sampled_from(_CODIFICACOES))
    content_length = draw(st.sampled_from(_CONTENT_LENGTHS))
    # Location em branco não é redirecionamento: 3xx assim é status_http (Req 5.9).
    location_vazia = draw(st.sampled_from((None, None, "", "   ")))
    fio = draw(st_fio_corpo())
    blocos = draw(st_blocos_de(fio))

    pares = list(_cabecalhos_do_caso(content_type, content_encoding, content_length, location_vazia))
    pares = [(draw(_com_caixa_variada(nome)), valor) for nome, valor in pares]
    if draw(st.booleans()):
        pares.append((draw(_com_caixa_variada("Set-Cookie")), draw(st_set_cookie)))
    pares = draw(st.permutations(pares))
    return CasoRespostaFinal(
        url=draw(st_url_inicial),
        status=status,
        content_type=content_type,
        tipo_html=tipo_html,
        charset=charset,
        content_encoding=content_encoding,
        content_length=content_length,
        cabecalhos=tuple(pares),
        blocos=blocos,
    )


def _caso_fixo(
    fio: bytes,
    *,
    content_encoding: str | None = None,
    content_length: str | None = None,
    tamanho_bloco: int = 64 * 1024,
) -> CasoRespostaFinal:
    """Caso HTML 200 com corpo grande, dividido em blocos de tamanho fixo."""
    content_type = "text/html; charset=utf-8"
    blocos = tuple(fio[i : i + tamanho_bloco] for i in range(0, len(fio), tamanho_bloco))
    return CasoRespostaFinal(
        url=URLS_PERMITIDAS[0],
        status=200,
        content_type=content_type,
        tipo_html=True,
        charset="utf-8",
        content_encoding=content_encoding,
        content_length=content_length,
        cabecalhos=_cabecalhos_do_caso(content_type, content_encoding, content_length),
        blocos=blocos,
    )


def _corpo_de(tamanho: int) -> bytes:
    return (_HTML * (tamanho // len(_HTML) + 1))[:tamanho]


_MIB = 1024 * 1024
_EXEMPLOS_LIMITE = (
    # Limite de 5 MiB decodificados, cru e gzip: exatamente o limite passa, +1 excede.
    _caso_fixo(_corpo_de(LIMITE_CORPO_BYTES)),
    _caso_fixo(_corpo_de(LIMITE_CORPO_BYTES + 1)),
    _caso_fixo(gzip.compress(_corpo_de(LIMITE_CORPO_BYTES), mtime=0), content_encoding="gzip"),
    _caso_fixo(
        gzip.compress(_corpo_de(LIMITE_CORPO_BYTES + 1), mtime=0), content_encoding="gzip"
    ),
    # Bomba gzip: ~16 KiB na rede, 16 MiB decodificados, em blocos de 1 KiB.
    _caso_fixo(
        gzip.compress(b"\x00" * (16 * _MIB), mtime=0),
        content_encoding="gzip",
        tamanho_bloco=1024,
    ),
    # Dois membros de 3 MiB: cada um cabe, a soma excede.
    _caso_fixo(
        gzip.compress(_corpo_de(3 * _MIB), mtime=0) * 2,
        content_encoding="gzip",
        tamanho_bloco=4096,
    ),
    # Content-Length declarado: acima do limite recusa sem ler; no limite, passa.
    _caso_fixo(_HTML, content_length=str(LIMITE_CORPO_BYTES + 1)),
    _caso_fixo(_HTML, content_length=str(LIMITE_CORPO_BYTES)),
)


def oraculo_resposta_final(
    caso: CasoRespostaFinal,
) -> tuple[PaginaBaixada | FalhaBusca, bool]:
    """(desfecho esperado, se o corpo é lido), na ordem status → Content-Type →
    Content-Encoding → Content-Length → decodificação/tamanho → vazio → sucesso.

    O gzip é decodificado por ``gzip.decompress`` (módulo ``gzip``), independente
    do decompressor incremental da implementação. Isso é equivalente porque a
    corrupção gerada só ocorre em payloads pequenos: nenhum fluxo inválido chega
    a 5 MiB decodificados antes do erro.
    """
    url = caso.url.strip()
    status = caso.status
    if not 200 <= status <= 299:
        return FalhaBusca("status_http", url, status_http=status), False
    ct = caso.content_type
    if ct is None:
        return FalhaBusca("nao_html", url, status_http=status), False
    if not caso.tipo_html:
        return FalhaBusca("nao_html", url, status_http=status, content_type=ct), False
    codificacao = (caso.content_encoding or "").strip().lower()
    if codificacao not in ("", "identity", "gzip"):
        return (
            FalhaBusca("codificacao_nao_suportada", url, status_http=status, content_type=ct),
            False,
        )
    declarado = (caso.content_length or "").strip()
    if re.fullmatch(r"[0-9]+", declarado) and int(declarado) > LIMITE_CORPO_BYTES:
        return FalhaBusca("tamanho_excedido", url, status_http=status, content_type=ct), False

    if codificacao == "gzip":
        try:
            corpo = gzip.decompress(caso.fio)
        except (OSError, EOFError, zlib.error):
            return (
                FalhaBusca("codificacao_invalida", url, status_http=status, content_type=ct),
                True,
            )
    else:
        corpo = caso.fio
    if len(corpo) > LIMITE_CORPO_BYTES:
        return FalhaBusca("tamanho_excedido", url, status_http=status, content_type=ct), True
    if not corpo:
        return FalhaBusca("corpo_vazio", url, status_http=status, content_type=ct), True
    return (
        PaginaBaixada(
            status_http=status,
            content_type=ct,
            charset=caso.charset,
            url_final=url,
            corpo=corpo,
        ),
        True,
    )


def _decodificado_ate(fio: bytes) -> int:
    """Bytes decodificados de um prefixo gzip (tolerante a truncamento)."""
    total = 0
    dados = fio
    while dados:
        decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        total += len(decompressor.decompress(dados))
        if not decompressor.eof:
            break
        dados = decompressor.unused_data.lstrip(b"\x00")
    return total


# Feature: html-extract-save, Property 15: Classificação da resposta final
@settings(max_examples=100, deadline=None)
@given(caso=st_caso_resposta_final())
@example(caso=_EXEMPLOS_LIMITE[0])
@example(caso=_EXEMPLOS_LIMITE[1])
@example(caso=_EXEMPLOS_LIMITE[2])
@example(caso=_EXEMPLOS_LIMITE[3])
@example(caso=_EXEMPLOS_LIMITE[4])
@example(caso=_EXEMPLOS_LIMITE[5])
@example(caso=_EXEMPLOS_LIMITE[6])
@example(caso=_EXEMPLOS_LIMITE[7])
def test_property_15_classificacao_da_resposta_final(caso: CasoRespostaFinal) -> None:
    """**Validates: Requirements 5.7, 5.9, 5.10, 5.11, 5.12, 5.14, 5.15**"""
    transporte = TransporteRoteirizado(
        (PassoResposta(status=caso.status, cabecalhos=caso.cabecalhos, blocos=caso.blocos),)
    )
    buscador = BuscadorPaginas(
        validar_destino=motivo_recusa_url,
        transporte=transporte,
        timeout=15.0,
        relogio_monotonico=RelogioFake(0.0),
    )

    desfecho = buscador.buscar(caso.url)
    esperado, le_corpo = oraculo_resposta_final(caso)

    # Uma única requisição (resposta final, sem redirecionamento), fechada uma vez.
    assert len(transporte.chamadas) == 1
    chamada = transporte.chamadas[0]
    assert chamada.fechamentos == 1

    # Motivo, URL, status e Content-Type; no sucesso, também charset e corpo.
    assert desfecho == esperado

    # As verificações de cabeçalho acontecem antes de qualquer leitura do corpo.
    if not le_corpo:
        assert chamada.blocos_lidos == 0

    # A leitura para no bloco que ultrapassa o limite (nunca lê além do necessário).
    if le_corpo and isinstance(esperado, FalhaBusca) and esperado.motivo == "tamanho_excedido":
        gzip_ativo = (caso.content_encoding or "").strip().lower() == "gzip"
        lidos = caso.blocos[: chamada.blocos_lidos - 1]
        antes_do_ultimo = _decodificado_ate(b"".join(lidos)) if gzip_ativo else sum(
            map(len, lidos)
        )
        assert antes_do_ultimo <= LIMITE_CORPO_BYTES
        assert chamada.blocos_lidos >= 1
