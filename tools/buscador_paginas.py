"""Buscador_Paginas — fronteira de I/O HTTP da coleta de páginas (html-extract-save).

Este módulo contém os tipos da busca HTTP e o transporte padrão sobre
``urllib.request`` (biblioteca padrão, sem dependência nova, Req 5.2), no mesmo
padrão de ``verification/serper_client.py``: a requisição é injetável para que os
testes usem um transporte fake, sem rede.

O transporte padrão (``transporte_urllib``):

- envia em toda requisição o conjunto fixo ``CABECALHOS_FIXOS`` (cabeçalhos
  de navegação de um navegador desktop, ``Accept-Encoding: gzip, identity``),
  único ponto de configuração dos cabeçalhos (Req 5.3);
- monta um ``OpenerDirector`` à mão, sem cookies, autenticação, ``file:``,
  ``ftp:`` nem ``data:`` (Req 5.3), e sem seguir redirecionamentos: cada salto
  é validado pelo ``BuscadorPaginas`` antes de ser requisitado (Req 5.5, 5.6);
- devolve ``RespostaHTTP`` também para 3xx/4xx/5xx (``HTTPError``);
- entrega o corpo cru, sem decodificar ``Content-Encoding``;
- converte falhas de rede e timeout em ``ErroTransporte`` (Req 5.8).

Só erros de rede/TLS/HTTP são convertidos. Exceções genéricas (``RuntimeError``,
``Exception``) atravessam de propósito, para que uma conexão acidental bloqueada
pela guarda de rede dos testes continue visível.

``BuscadorPaginas.buscar`` executa uma busca completa (Req 5.1): segue até 5
redirecionamentos validados, verifica status, Content-Type/charset,
Content-Encoding (só ``gzip``/``identity``), Content-Length, lê o corpo em fluxo
com limite de 5 MiB decodificados e prazo total por requisição, e devolve
sempre ``PaginaBaixada`` ou ``FalhaBusca``.

Este módulo não importa ``coleta_paginas``: a validação de destino chega ao
``BuscadorPaginas`` como callable injetado.
"""

from __future__ import annotations

import http.client
import math
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

import config

LIMITE_CORPO_BYTES = 5 * 1024 * 1024  # 5 MiB (Req 5.7, 5.11)
MAX_REDIRECIONAMENTOS = 5  # até 6 GETs por busca (Req 5.5)
# Cabeçalhos de navegação de um navegador desktop (Req 5.3), centralizados aqui:
# é o único ponto de configuração dos cabeçalhos enviados em toda requisição.
# ``Accept-Encoding`` declara só ``gzip`` e ``identity`` porque é o que
# ``_DecodificadorCorpo`` sabe decodificar (Req 5.14, 5.15). Sem cookies nem
# autenticação.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
CABECALHOS_FIXOS: Mapping[str, str] = MappingProxyType(
    {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, identity",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }
)
STATUS_REDIRECIONAMENTO = frozenset({301, 302, 303, 307, 308})

TAMANHO_BLOCO_LEITURA = 64 * 1024  # 64 KiB
_ESQUEMAS_PERMITIDOS = frozenset({"http", "https"})

TipoErroTransporte = Literal["rede", "timeout"]
MotivoFalhaBusca = Literal[
    "rede",
    "timeout",
    "status_http",
    "nao_html",
    "tamanho_excedido",
    "corpo_vazio",
    "redirecionamento_nao_permitido",
    "redirecionamentos_excedidos",
    "codificacao_nao_suportada",
    "codificacao_invalida",
    # Só do fallback stealth (tools/buscador_stealth.py), nunca do BuscadorPaginas:
    "url_invalida",  # URL recusada por coleta_paginas.url.motivo_recusa_url
    "destino_nao_permitido",  # idem, destino local/privado (SSRF)
    "dominio_nao_permitido_stealth",  # host fora da allowlist do stealth
    "ambiente_sem_display",  # sem DISPLAY/WAYLAND_DISPLAY (nunca headless)
    "navegador_indisponivel",  # falha ao iniciar o Chrome/perfil (ex.: perfil em uso)
]


def _nao_fechar() -> None:
    return None


@dataclass(frozen=True)
class RespostaHTTP:
    """Resposta de uma única requisição GET, sem redirecionamento seguido.

    ``cabecalhos`` tem nomes em minúsculas e o primeiro valor de cada nome.
    ``blocos`` é o corpo cru (sem decodificar ``Content-Encoding``); a iteração
    só levanta ``ErroTransporte``. ``fechar`` libera a conexão e pode ser
    chamado mesmo sem ler o corpo.
    """

    status: int
    cabecalhos: Mapping[str, str]
    blocos: Iterable[bytes]
    fechar: Callable[[], None] = field(default=_nao_fechar)


class ErroTransporte(Exception):
    """Falha de rede ou timeout do transporte. Carrega só o tipo (Req 5.8)."""

    def __init__(self, tipo: TipoErroTransporte) -> None:
        super().__init__(tipo)
        self.tipo: TipoErroTransporte = tipo


class TransporteHTTP(Protocol):
    """Executa um GET e devolve a resposta sem seguir redirecionamentos.

    Levanta somente ``ErroTransporte``, inclusive durante a iteração de
    ``RespostaHTTP.blocos``.
    """

    def __call__(
        self, url: str, cabecalhos: Mapping[str, str], timeout: float
    ) -> RespostaHTTP: ...


@dataclass(frozen=True)
class PaginaBaixada:
    """Desfecho de sucesso da busca (Req 5.7)."""

    status_http: int
    content_type: str
    charset: str | None
    url_final: str
    corpo: bytes  # após Content-Encoding, antes de charset


@dataclass(frozen=True)
class FalhaBusca:
    """Desfecho de falha da busca; nunca carrega corpo nem cabeçalhos."""

    motivo: MotivoFalhaBusca
    url: str
    status_http: int | None = None
    content_type: str | None = None


# ---------------------------------------------------------------------------
# Transporte padrão (urllib)
# ---------------------------------------------------------------------------


class _SemRedirecionamento(urllib.request.HTTPRedirectHandler):
    """Não segue nenhum redirecionamento.

    Devolver ``None`` faz o ``OpenerDirector`` cair no
    ``HTTPDefaultErrorHandler``, que levanta ``HTTPError`` com status,
    cabeçalhos e corpo do 3xx — convertido em ``RespostaHTTP`` pelo transporte.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None

    def _nao_seguir(self, req, fp, code, msg, headers):  # noqa: ANN001
        return None

    http_error_301 = http_error_302 = http_error_303 = _nao_seguir
    http_error_307 = http_error_308 = _nao_seguir


def montar_opener() -> urllib.request.OpenerDirector:
    """``OpenerDirector`` mínimo: proxies do ambiente, HTTP/HTTPS com
    verificação TLS padrão, erros HTTP como ``HTTPError`` e nenhum
    redirecionamento automático. Sem cookies, autenticação, file, ftp ou data."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        _SemRedirecionamento(),
    ):
        opener.add_handler(handler)
    # Sem o User-Agent padrão do urllib: só os cabeçalhos recebidos são enviados.
    opener.addheaders = []
    return opener


def _erro_para_transporte(exc: BaseException) -> ErroTransporte:
    """Mapeia exceções de rede/TLS/HTTP da stdlib para ``ErroTransporte``."""
    if isinstance(exc, TimeoutError):  # inclui socket.timeout
        return ErroTransporte("timeout")
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, TimeoutError):
        return ErroTransporte("timeout")
    return ErroTransporte("rede")


# Exceções convertidas em ``ErroTransporte``. ``ssl.SSLError``, ``URLError`` e
# ``ConnectionError`` são subclasses de ``OSError``; ``HTTPException`` inclui
# ``InvalidURL`` e ``IncompleteRead``; ``UnicodeError`` cobre host que não
# codifica em IDNA. ``RuntimeError``/``Exception`` genéricas não entram.
_ERROS_DE_REDE: tuple[type[BaseException], ...] = (
    OSError,
    http.client.HTTPException,
    UnicodeError,
)


def _cabecalhos_minusculos(mensagem: object) -> dict[str, str]:
    """Nomes em minúsculas, primeiro valor de cada nome."""
    resultado: dict[str, str] = {}
    itens = getattr(mensagem, "items", None)
    if itens is None:
        return resultado
    for nome, valor in itens():
        resultado.setdefault(str(nome).lower(), str(valor))
    return resultado


def _ler_blocos(leitor: object) -> Iterator[bytes]:
    """Gerador que lê o corpo em blocos de 64 KiB com o mesmo mapeamento de erros."""
    ler = getattr(leitor, "read")
    while True:
        try:
            bloco = ler(TAMANHO_BLOCO_LEITURA)
        except _ERROS_DE_REDE as exc:
            raise _erro_para_transporte(exc) from exc
        if not bloco:
            return
        yield bloco


def _fechador(recurso: object) -> Callable[[], None]:
    def fechar() -> None:
        fechar_recurso = getattr(recurso, "close", None)
        if fechar_recurso is None:
            return
        try:
            fechar_recurso()
        except _ERROS_DE_REDE:
            pass

    return fechar


def transporte_urllib(
    url: str, cabecalhos: Mapping[str, str], timeout: float
) -> RespostaHTTP:
    """Transporte padrão: um GET via ``urllib`` sem redirecionamento automático.

    Levanta somente ``ErroTransporte`` (inclusive durante a iteração dos blocos).
    """
    try:
        esquema = urllib.parse.urlsplit(url).scheme.lower()
    except ValueError as exc:
        raise ErroTransporte("rede") from exc
    if esquema not in _ESQUEMAS_PERMITIDOS:
        # Defesa em profundidade: o opener já não tem handlers de outros esquemas.
        raise ErroTransporte("rede")

    try:
        requisicao = urllib.request.Request(url, headers=dict(cabecalhos), method="GET")
    except ValueError as exc:
        raise ErroTransporte("rede") from exc

    opener = montar_opener()
    try:
        resposta = opener.open(requisicao, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # 3xx (não seguido), 4xx e 5xx: a resposta é devolvida ao chamador.
        return RespostaHTTP(
            status=int(exc.code),
            cabecalhos=_cabecalhos_minusculos(exc.headers),
            blocos=_ler_blocos(exc),
            fechar=_fechador(exc),
        )
    except _ERROS_DE_REDE as exc:
        raise _erro_para_transporte(exc) from exc

    status = getattr(resposta, "status", None)
    if status is None:
        status = resposta.getcode()
    return RespostaHTTP(
        status=int(status),
        cabecalhos=_cabecalhos_minusculos(resposta.headers),
        blocos=_ler_blocos(resposta),
        fechar=_fechador(resposta),
    )



# ---------------------------------------------------------------------------
# BuscadorPaginas
# ---------------------------------------------------------------------------

TIMEOUT_PADRAO_S = 15.0
TIMEOUT_MINIMO_S = 1.0
TIMEOUT_MAXIMO_S = 120.0

_TIPOS_HTML = frozenset({"text/html", "application/xhtml+xml"})
# Limite de bytes crus lidos do fluxo gzip: barra fluxos que consomem entrada
# sem produzir saída (ou produzem muito pouca) antes de atingir LIMITE_CORPO_BYTES.
_LIMITE_CRU_GZIP = 2 * LIMITE_CORPO_BYTES
_WBITS_GZIP = 16 + zlib.MAX_WBITS


def resolver_timeout(valor: object | None) -> float:
    """Timeout efetivo por requisição (Req 5.4).

    ``None`` usa ``config.coleta_timeout()`` (``ESTAGIARIO_COLETA_TIMEOUT``).
    Um valor explícito segue a mesma regra do ambiente, para que a semântica
    do Req 5.4 não dependa da origem: vale se for ``int``/``float`` (não
    ``bool``) finito em [1, 120]; qualquer outro valor (fora do intervalo,
    não numérico, ``bool``, ``NaN``/``inf``, texto) cai no padrão de 15 s,
    sem clamp e sem erro.
    """
    if valor is None:
        return config.coleta_timeout()
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return TIMEOUT_PADRAO_S
    numero = float(valor)
    if not math.isfinite(numero) or not TIMEOUT_MINIMO_S <= numero <= TIMEOUT_MAXIMO_S:
        return TIMEOUT_PADRAO_S
    return numero


def _tipo_e_charset(content_type: str) -> tuple[str, str | None]:
    """Tipo (antes de ``;``, strip, minúsculas) e o parâmetro ``charset``
    declarado (aspas removidas), ou ``None`` quando não há charset. Sem inferência."""
    partes = content_type.split(";")
    tipo = partes[0].strip().lower()
    charset: str | None = None
    for parametro in partes[1:]:
        nome, sep, valor = parametro.partition("=")
        if not sep or nome.strip().lower() != "charset":
            continue
        valor = valor.strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1].strip()
        charset = valor or None
        break
    return tipo, charset


class _TamanhoExcedido(Exception):
    """Sinal interno: corpo cru ou decodificado acima do limite."""


class _DecodificadorCorpo:
    """Acumula o corpo decodificado, com os limites cru e decodificado.

    ``identity``: os bytes crus são o corpo; mais de ``LIMITE_CORPO_BYTES`` é
    excesso. ``gzip``: descompressão incremental com ``max_length``, de modo
    que nunca se produz mais que ``LIMITE_CORPO_BYTES + 1`` bytes decodificados
    (bombas gzip param no limite). Membros múltiplos são tratados reabrindo o
    decompressor sobre ``unused_data``; zeros de preenchimento depois de um
    membro completo são ignorados, como faz o módulo ``gzip``.

    Levanta ``_TamanhoExcedido`` ou ``zlib.error``.
    """

    def __init__(self, gzip: bool) -> None:
        self._gzip = gzip
        self._saida = bytearray()
        self._crus = 0
        self._decompressor: zlib._Decompress | None = None
        self._membros_concluidos = 0

    def alimentar(self, bloco: bytes) -> None:
        if not bloco:
            return
        self._crus += len(bloco)
        if not self._gzip:
            restante = LIMITE_CORPO_BYTES + 1 - len(self._saida)
            self._saida += bloco[:restante]
            if len(self._saida) > LIMITE_CORPO_BYTES:
                raise _TamanhoExcedido
            return
        if self._crus > _LIMITE_CRU_GZIP:
            raise _TamanhoExcedido
        dados = bytes(bloco)
        while dados:
            if self._decompressor is None:
                if self._membros_concluidos:
                    dados = dados.lstrip(b"\x00")
                    if not dados:
                        return
                self._decompressor = zlib.decompressobj(wbits=_WBITS_GZIP)
            restante = LIMITE_CORPO_BYTES + 1 - len(self._saida)  # sempre >= 1
            self._saida += self._decompressor.decompress(dados, restante)
            if len(self._saida) > LIMITE_CORPO_BYTES:
                raise _TamanhoExcedido
            if self._decompressor.eof:
                dados = self._decompressor.unused_data
                self._decompressor = None
                self._membros_concluidos += 1
            else:
                # Com saída abaixo do limite, a entrada foi toda consumida.
                dados = self._decompressor.unconsumed_tail

    def finalizar(self) -> bytes:
        """Corpo decodificado. Um membro gzip iniciado e não terminado (sem
        ``eof``) é fluxo truncado: ``zlib.error``."""
        if self._gzip and self._decompressor is not None:
            raise zlib.error("fluxo gzip terminou sem eof")
        return bytes(self._saida)


def _fechar_sem_propagar(resposta: RespostaHTTP) -> None:
    try:
        resposta.fechar()
    except ErroTransporte:
        pass


class BuscadorPaginas:
    """Busca uma página por HTTP simples e valida a resposta (Req 5).

    Cada ``buscar`` executa exatamente uma busca: uma requisição GET à URL e,
    no máximo, 5 GETs de redirecionamento validados um a um pelo
    ``validar_destino`` injetado (tipicamente
    ``coleta_paginas.url.motivo_recusa_url``; este módulo não importa
    ``coleta_paginas``). Devolve sempre ``PaginaBaixada`` ou ``FalhaBusca``,
    sem retry. ``ErroTransporte`` e erros de descompressão nunca chegam ao
    chamador; exceções genéricas (por exemplo, a guarda de rede dos testes)
    não são capturadas.
    """

    def __init__(
        self,
        *,
        validar_destino: Callable[[str], str | None],
        transporte: TransporteHTTP | None = None,
        timeout: float | None = None,
        relogio_monotonico: Callable[[], float] = time.monotonic,
    ) -> None:
        self._validar_destino = validar_destino
        self._transporte: TransporteHTTP = (
            transporte if transporte is not None else transporte_urllib
        )
        self._timeout = resolver_timeout(timeout)
        self._relogio = relogio_monotonico

    @property
    def timeout(self) -> float:
        """Timeout efetivo aplicado a cada requisição (Req 5.4)."""
        return self._timeout

    def buscar(self, url: str) -> PaginaBaixada | FalhaBusca:
        """Exatamente uma busca, um desfecho, nenhuma exceção de transporte (Req 5.1)."""
        url_atual = url.strip()
        saltos = 0
        while True:
            inicio = self._relogio()
            try:
                resposta = self._transporte(url_atual, CABECALHOS_FIXOS, self._timeout)
            except ErroTransporte as exc:
                return FalhaBusca(exc.tipo, url_atual)
            try:
                desfecho = self._avaliar(resposta, url_atual, saltos, inicio)
            finally:
                _fechar_sem_propagar(resposta)
            if isinstance(desfecho, str):
                # Redirecionamento permitido: próximo salto, sem ler o corpo.
                saltos += 1
                url_atual = desfecho
                continue
            return desfecho

    def _avaliar(
        self, resposta: RespostaHTTP, url_atual: str, saltos: int, inicio: float
    ) -> PaginaBaixada | FalhaBusca | str:
        """Desfecho da resposta, ou a URL do próximo salto (``str``)."""
        cabecalhos: dict[str, str] = {}
        for nome, valor in resposta.cabecalhos.items():
            cabecalhos.setdefault(str(nome).lower(), str(valor))
        status = int(resposta.status)

        # 1. Redirecionamento (Req 5.5, 5.6, 5.13).
        location = cabecalhos.get("location")
        if status in STATUS_REDIRECIONAMENTO and location is not None and location.strip():
            if saltos >= MAX_REDIRECIONAMENTOS:
                return FalhaBusca("redirecionamentos_excedidos", url_atual, status_http=status)
            try:
                alvo = urllib.parse.urljoin(url_atual, location.strip())
            except ValueError:
                return FalhaBusca(
                    "redirecionamento_nao_permitido", location.strip(), status_http=status
                )
            if self._validar_destino(alvo) is not None:
                return FalhaBusca("redirecionamento_nao_permitido", alvo, status_http=status)
            return alvo

        # 2. Status (Req 5.9), inclusive 3xx sem Location.
        if not 200 <= status <= 299:
            return FalhaBusca("status_http", url_atual, status_http=status)

        # 3. Content-Type e charset (Req 5.7, 5.10).
        content_type = cabecalhos.get("content-type")
        if content_type is None:
            return FalhaBusca("nao_html", url_atual, status_http=status)
        tipo, charset = _tipo_e_charset(content_type)
        if tipo not in _TIPOS_HTML:
            return FalhaBusca(
                "nao_html", url_atual, status_http=status, content_type=content_type
            )

        # 4. Content-Encoding (Req 5.14, 5.15).
        codificacao = cabecalhos.get("content-encoding", "").strip().lower()
        if codificacao in ("", "identity"):
            usa_gzip = False
        elif codificacao == "gzip":
            usa_gzip = True
        else:
            return FalhaBusca(
                "codificacao_nao_suportada",
                url_atual,
                status_http=status,
                content_type=content_type,
            )

        # 5. Content-Length declarado (Req 5.11), sem ler o corpo.
        declarado = cabecalhos.get("content-length", "").strip()
        if declarado.isascii() and declarado.isdigit() and int(declarado) > LIMITE_CORPO_BYTES:
            return FalhaBusca(
                "tamanho_excedido", url_atual, status_http=status, content_type=content_type
            )

        # 6. Leitura em fluxo com limites e prazo total da requisição (Req 5.4, 5.11).
        decodificador = _DecodificadorCorpo(gzip=usa_gzip)
        try:
            for bloco in resposta.blocos:
                if self._relogio() - inicio > self._timeout:
                    return FalhaBusca("timeout", url_atual, status_http=status)
                decodificador.alimentar(bloco)
            corpo = decodificador.finalizar()
        except ErroTransporte as exc:
            return FalhaBusca(exc.tipo, url_atual, status_http=status)
        except _TamanhoExcedido:
            return FalhaBusca(
                "tamanho_excedido", url_atual, status_http=status, content_type=content_type
            )
        except zlib.error:
            return FalhaBusca(
                "codificacao_invalida", url_atual, status_http=status, content_type=content_type
            )

        # 7. Corpo vazio (Req 5.12).
        if not corpo:
            return FalhaBusca(
                "corpo_vazio", url_atual, status_http=status, content_type=content_type
            )

        # 8. Sucesso (Req 5.7).
        return PaginaBaixada(
            status_http=status,
            content_type=content_type,
            charset=charset,
            url_final=url_atual,
            corpo=corpo,
        )
