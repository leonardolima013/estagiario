"""Fallback de coleta stealth — fronteira de I/O com o Chrome real via Patchright.

Integração: o ``ColetorPaginas`` usa ``coletar_via_playwright_stealth`` como
Fallback_Stealth padrão (protocolo ``ColetaStealth``) quando a busca urllib de
uma fonte da allowlist é bloqueada e o fallback está ligado para a chamada.
Quem adquire ``TRAVA_NAVEGADOR`` (um Chrome do fallback por vez no processo) e
grava a página no armazém é o coletor; este módulo não usa a trava e não grava
nada. Só roda local, headed (``headless=False`` fixo, sem parâmetro para mudar), no
Google Chrome do sistema (``channel="chrome"``) com o perfil persistente de
``config.stealth_profile_dir()`` — a mesma configuração validada em
``browserscan/main.py``.

Barreiras antes de abrir o navegador, nesta ordem:

1. ``coleta_paginas.url.motivo_recusa_url`` (URL inválida / destino local ou
   privado) → ``url_invalida`` / ``destino_nao_permitido``;
2. allowlist ``config.coleta_dominios_stealth()`` → ``dominio_nao_permitido_stealth``
   (domínios com proteção deliberada, como autodoc.parts, auto-doc.ie e
   ebay.co.uk, nunca chegam ao navegador);
3. display (``DISPLAY`` ou ``WAYLAND_DISPLAY``) → ``ambiente_sem_display``.

Durante a navegação, ``page.route`` aborta navegações do frame principal para
destinos recusados e qualquer requisição a destino local/privado. Saltos de
redirecionamento HTTP não passam por ``page.route`` (limitação do Playwright):
eles são detectados pelo evento ``request`` e o resultado vira
``redirecionamento_nao_permitido`` sem ler o conteúdo, mas a requisição do salto
já terá sido feita pelo navegador. A URL final (``page.url``) passa de novo pelo
SSRF e pela allowlist.

Desfechos: sempre ``PaginaBaixada`` ou ``FalhaBusca`` para falhas esperadas.
Motivos usados além dos do ``BuscadorPaginas``: ``url_invalida``,
``destino_nao_permitido``, ``dominio_nao_permitido_stealth``,
``ambiente_sem_display`` e ``navegador_indisponivel`` (falha ao iniciar o
Chrome — por exemplo, perfil já em uso por outra instância). Exceções que não
são do Playwright (erros de programação, guarda de rede dos testes) atravessam,
como no ``BuscadorPaginas``; o contexto é fechado em qualquer caso.

``patchright`` é importado de forma preguiçosa dentro de
``abrir_contexto_patchright``: importar este módulo não carrega o Patchright nem
abre navegador.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import config
from coleta_paginas.url import host_da_url, motivo_recusa_url
from tools.buscador_paginas import (
    LIMITE_CORPO_BYTES,
    FalhaBusca,
    PaginaBaixada,
    resolver_timeout,
)

ResultadoColeta = PaginaBaixada | FalhaBusca

CANAL_NAVEGADOR = "chrome"
POLITICA_WEBRTC = "disable_non_proxied_udp"
CHARSET_DOM = "utf-8"  # page.content() é str; serializamos sempre em UTF-8

URL_FUSO_IP = "http://ip-api.com/json/?fields=timezone"
TIMEOUT_CONSULTA_FUSO_S = 5.0
FUSO_PADRAO = "America/Sao_Paulo"
ARQUIVO_CACHE_FUSO = "estagiario_timezone.json"
VALIDADE_CACHE_FUSO = timedelta(hours=24)

_PACOTES_PLAYWRIGHT = frozenset({"patchright", "playwright"})


# ---------------------------------------------------------------------------
# Protocolos (subconjunto da API síncrona do Playwright usado aqui)
# ---------------------------------------------------------------------------


class ContextoNavegador(Protocol):
    """``BrowserContext`` persistente: páginas abertas, cookies e fechamento."""

    @property
    def pages(self) -> Sequence[Any]: ...

    def new_page(self) -> Any: ...

    def cookies(self) -> list[Mapping[str, Any]]: ...

    def close(self) -> None: ...


AbrirContexto = Callable[..., ContextoNavegador]
"""Recebe os kwargs de ``launch_persistent_context`` (``user_data_dir``,
``channel``, ``headless``, ``no_viewport``, ``timezone_id``) e devolve o contexto."""

ObterFuso = Callable[[Path], str]


# ---------------------------------------------------------------------------
# Contrato com o coletor (Trava_Navegador e Fallback_Stealth)
# ---------------------------------------------------------------------------


TRAVA_NAVEGADOR = threading.Lock()
"""Trava_Navegador do processo: no máximo um Chrome do fallback aberto por vez.
Quem adquire é o ColetorPaginas; coletar_via_playwright_stealth não a usa."""


class TravaNavegador(Protocol):
    """Subconjunto de ``threading.Lock`` usado pelo coletor."""

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool: ...

    def release(self) -> None: ...


class ColetaStealth(Protocol):
    """Contrato do Fallback_Stealth visto pelo coletor."""

    def __call__(
        self,
        url: str,
        *,
        allowlist: frozenset[str] | None = None,
        timeout: float | None = None,
    ) -> PaginaBaixada | FalhaBusca: ...


# ---------------------------------------------------------------------------
# Funções puras
# ---------------------------------------------------------------------------


def _normalizar_dominio(dominio: str) -> str:
    return dominio.strip().casefold().rstrip(".")


def dominio_na_allowlist(url: str, allowlist: frozenset[str]) -> bool:
    """True sse o host da URL é um item da allowlist ou subdomínio dele.

    ``www.mercadocar.com.br`` casa ``mercadocar.com.br``;
    ``mercadocar.com.br.evil.com`` e ``evilmercadocar.com.br`` não casam.
    """
    host = _normalizar_dominio(host_da_url(url))
    if not host:
        return False
    for item in allowlist:
        dominio = _normalizar_dominio(item)
        if dominio and (host == dominio or host.endswith("." + dominio)):
            return True
    return False


def _tem_display() -> bool:
    return any(os.environ.get(nome, "").strip() for nome in ("DISPLAY", "WAYLAND_DISPLAY"))


def _classificar_erro(exc: BaseException) -> Literal["timeout", "rede"] | None:
    """``timeout``/``rede`` para exceções do Playwright/Patchright; None para as demais.

    Classifica pelo nome e pacote das classes na MRO para não importar o
    Patchright só para ``isinstance``.
    """
    classes = [
        (classe.__module__.partition(".")[0], classe.__name__)
        for classe in type(exc).__mro__
    ]
    if any(pacote in _PACOTES_PLAYWRIGHT and nome == "TimeoutError" for pacote, nome in classes):
        return "timeout"
    if any(pacote in _PACOTES_PLAYWRIGHT and nome == "Error" for pacote, nome in classes):
        return "rede"
    return None


def _gravar_atomico(caminho: Path, texto: str) -> None:
    """Grava via temporário no mesmo diretório + ``os.replace``; nunca deixa arquivo parcial."""
    fd, tmp = tempfile.mkstemp(prefix=f".{caminho.name}.", suffix=".tmp", dir=caminho.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as arquivo:
            arquivo.write(texto)
        os.replace(tmp, caminho)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


# ---------------------------------------------------------------------------
# Fuso horário do IP (cache em memória + arquivo no perfil)
# ---------------------------------------------------------------------------

_trava_fuso = threading.Lock()
_fuso_memoria: tuple[str, datetime] | None = None


def limpar_cache_fuso() -> None:
    """Esquece o fuso em memória (o arquivo no perfil é preservado)."""
    global _fuso_memoria
    with _trava_fuso:
        _fuso_memoria = None


def _fuso_valido(nome: object) -> bool:
    if not isinstance(nome, str) or not nome.strip():
        return False
    try:
        ZoneInfo(nome)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def _cache_vigente(obtido_em: datetime, agora: datetime) -> bool:
    """Obtido há menos de 24 h (instantes no futuro não valem)."""
    return timedelta(0) <= agora - obtido_em < VALIDADE_CACHE_FUSO


def consultar_fuso_ip_api(timeout: float = TIMEOUT_CONSULTA_FUSO_S) -> str | None:
    """Fuso IANA do IP público via ip-api.com (só o campo ``timezone``); None se falhar.

    O IP nunca é pedido nem registrado.
    """
    try:
        with urllib.request.urlopen(URL_FUSO_IP, timeout=timeout) as resposta:
            dados = json.load(resposta)
    except (OSError, ValueError, HTTPException):
        return None
    fuso = dados.get("timezone") if isinstance(dados, dict) else None
    return fuso if _fuso_valido(fuso) else None


def _ler_cache_fuso(arquivo: Path, agora: datetime) -> tuple[str, datetime] | None:
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
        fuso = dados["timezone"]
        obtido_em = datetime.fromisoformat(dados["obtido_em"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if obtido_em.tzinfo is None or not _fuso_valido(fuso):
        return None
    if not _cache_vigente(obtido_em, agora):
        return None
    return fuso, obtido_em


def obter_fuso_ip(
    profile_dir: Path,
    *,
    consultar: Callable[[], str | None] = consultar_fuso_ip_api,
    agora: Callable[[], datetime] = _agora_utc,
) -> str:
    """Fuso do IP público, com cache de 24 h em memória e em ``profile_dir/ARQUIVO_CACHE_FUSO``.

    Ordem: memória → arquivo do perfil → ``consultar()``. Thread-safe (uma
    consulta por vez). Se a consulta falhar, devolve ``FUSO_PADRAO`` sem
    cachear, para tentar de novo na próxima chamada. Falha ao gravar o arquivo
    é tolerada.
    """
    global _fuso_memoria
    perfil = Path(profile_dir)
    with _trava_fuso:
        instante = agora()
        if _fuso_memoria is not None and _cache_vigente(_fuso_memoria[1], instante):
            return _fuso_memoria[0]
        arquivo = perfil / ARQUIVO_CACHE_FUSO
        do_arquivo = _ler_cache_fuso(arquivo, instante)
        if do_arquivo is not None:
            _fuso_memoria = do_arquivo
            return do_arquivo[0]
        fuso = consultar()
        if not _fuso_valido(fuso):
            return FUSO_PADRAO
        assert fuso is not None
        _fuso_memoria = (fuso, instante)
        try:
            perfil.mkdir(parents=True, exist_ok=True)
            _gravar_atomico(
                arquivo, json.dumps({"timezone": fuso, "obtido_em": instante.isoformat()})
            )
        except OSError:
            pass
        return fuso


# ---------------------------------------------------------------------------
# Política WebRTC no Preferences do perfil
# ---------------------------------------------------------------------------


def aplicar_politica_webrtc(profile_dir: Path) -> bool:
    """Grava ``webrtc.ip_handling_policy = disable_non_proxied_udp`` em
    ``profile_dir/Default/Preferences`` preservando as demais chaves.

    Devolve True se o arquivo ficou com a política. JSON inválido, raiz ou
    ``webrtc`` que não sejam objeto, ou erro de E/S: não reescreve (o arquivo é
    preservado como está) e devolve False — a coleta segue mesmo assim.
    """
    arquivo = Path(profile_dir) / "Default" / "Preferences"
    if arquivo.exists():
        try:
            preferencias = json.loads(arquivo.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(preferencias, dict):
            return False
    else:
        preferencias = {}
    webrtc = preferencias.setdefault("webrtc", {})
    if not isinstance(webrtc, dict):
        return False
    if webrtc.get("ip_handling_policy") == POLITICA_WEBRTC:
        return True
    webrtc["ip_handling_policy"] = POLITICA_WEBRTC
    try:
        arquivo.parent.mkdir(parents=True, exist_ok=True)
        _gravar_atomico(arquivo, json.dumps(preferencias, separators=(",", ":")))
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Abertura padrão do contexto (Patchright)
# ---------------------------------------------------------------------------


class _ContextoPatchright:
    """Contexto persistente + instância do Playwright; ``close`` encerra os dois."""

    def __init__(self, contexto: Any, playwright: Any) -> None:
        self._contexto = contexto
        self._playwright = playwright

    @property
    def pages(self) -> Sequence[Any]:
        return self._contexto.pages

    def new_page(self) -> Any:
        return self._contexto.new_page()

    def cookies(self) -> list[Mapping[str, Any]]:
        return self._contexto.cookies()

    def close(self) -> None:
        try:
            self._contexto.close()
        finally:
            self._playwright.stop()


def abrir_contexto_patchright(**opcoes: Any) -> ContextoNavegador:
    """Inicia o Patchright e abre ``chromium.launch_persistent_context(**opcoes)``."""
    from patchright.sync_api import sync_playwright  # import preguiçoso

    playwright = sync_playwright().start()
    try:
        contexto = playwright.chromium.launch_persistent_context(**opcoes)
    except BaseException:
        try:
            playwright.stop()
        except Exception:
            pass
        raise
    return _ContextoPatchright(contexto, playwright)


# ---------------------------------------------------------------------------
# Navegação
# ---------------------------------------------------------------------------


@dataclass
class _Diagnostico:
    titulo: str | None = None
    cookies: list[dict[str, str]] = field(default_factory=list)
    status_navegacoes: list[dict[str, Any]] = field(default_factory=list)


class _MonitorNavegacao:
    """Handlers de ``route``/``request``/``response`` da página.

    Registra a primeira navegação do frame principal para destino recusado
    (``violacao``) e as respostas de navegação do frame principal, em ordem.
    Nunca levanta a partir dos handlers.
    """

    def __init__(self, pagina: Any, diagnostico: _Diagnostico) -> None:
        self._pagina = pagina
        self._diagnostico = diagnostico
        self.violacao: str | None = None
        self.respostas: list[Any] = []

    def _navegacao_principal(self, requisicao: Any) -> bool:
        try:
            return bool(requisicao.is_navigation_request()) and (
                requisicao.frame == self._pagina.main_frame
            )
        except Exception:
            return False

    def _marcar(self, url: str) -> None:
        if self.violacao is None:
            self.violacao = url

    def rotear(self, rota: Any, requisicao: Any) -> None:
        try:
            url = str(requisicao.url)
            motivo = motivo_recusa_url(url)
            navegacao = self._navegacao_principal(requisicao)
            if navegacao and motivo is not None:
                self._marcar(url)
                rota.abort("blockedbyclient")
            elif motivo == "destino_nao_permitido":
                rota.abort("blockedbyclient")  # sub-recurso local/privado
            else:
                rota.continue_()
        except Exception:
            pass

    def ao_requisitar(self, requisicao: Any) -> None:
        try:
            if self._navegacao_principal(requisicao):
                url = str(requisicao.url)
                if motivo_recusa_url(url) is not None:
                    self._marcar(url)
        except Exception:
            pass

    def ao_responder(self, resposta: Any) -> None:
        try:
            if self._navegacao_principal(resposta.request):
                self.respostas.append(resposta)
                self._diagnostico.status_navegacoes.append(
                    {"url": str(resposta.url), "status": int(resposta.status)}
                )
        except Exception:
            pass


def _content_type(resposta: Any) -> str:
    try:
        cabecalhos = {str(k).lower(): str(v) for k, v in dict(resposta.headers).items()}
    except Exception:
        return ""
    return cabecalhos.get("content-type", "")


def _navegar(
    pagina: Any,
    monitor: _MonitorNavegacao,
    url: str,
    allowlist: frozenset[str],
    prazo_s: float,
    relogio: Callable[[], float],
) -> ResultadoColeta:
    inicio = relogio()
    resposta_goto = pagina.goto(url, wait_until="domcontentloaded", timeout=prazo_s * 1000)
    if monitor.violacao is not None:
        return FalhaBusca("redirecionamento_nao_permitido", monitor.violacao)

    restante = prazo_s - (relogio() - inicio)
    if restante > 0:
        try:
            pagina.wait_for_load_state("networkidle", timeout=restante * 1000)
        except Exception as exc:
            # Sites com polling nunca ficam idle: segue com o conteúdo atual.
            if _classificar_erro(exc) != "timeout":
                raise
    if monitor.violacao is not None:
        return FalhaBusca("redirecionamento_nao_permitido", monitor.violacao)

    url_final = str(pagina.url)
    if motivo_recusa_url(url_final) is not None:
        return FalhaBusca("redirecionamento_nao_permitido", url_final)
    if not dominio_na_allowlist(url_final, allowlist):
        return FalhaBusca("dominio_nao_permitido_stealth", url_final)

    resposta = monitor.respostas[-1] if monitor.respostas else resposta_goto
    if resposta is None:
        return FalhaBusca("rede", url_final)
    status = int(resposta.status)
    content_type = _content_type(resposta)
    if not 200 <= status <= 299:
        return FalhaBusca(
            "status_http", url_final, status_http=status, content_type=content_type or None
        )

    html = pagina.content()
    corpo = html.encode(CHARSET_DOM, errors="replace")
    if len(corpo) > LIMITE_CORPO_BYTES:
        return FalhaBusca(
            "tamanho_excedido", url_final, status_http=status, content_type=content_type or None
        )
    if not html.strip():
        return FalhaBusca(
            "corpo_vazio", url_final, status_http=status, content_type=content_type or None
        )
    return PaginaBaixada(
        status_http=status,
        content_type=content_type,
        charset=CHARSET_DOM,
        url_final=url_final,
        corpo=corpo,
    )


def _coletar_diagnostico(contexto: ContextoNavegador, pagina: Any, diagnostico: _Diagnostico) -> None:
    """Título e cookies (só nome e domínio — nunca valores)."""
    try:
        diagnostico.titulo = str(pagina.title())
    except Exception:
        diagnostico.titulo = None
    try:
        diagnostico.cookies = [
            {"nome": str(c.get("name", "")), "dominio": str(c.get("domain", ""))}
            for c in contexto.cookies()
        ]
    except Exception:
        diagnostico.cookies = []


def _fechar_sem_propagar(contexto: ContextoNavegador) -> None:
    try:
        contexto.close()
    except Exception:
        pass


def _executar(
    url: str,
    *,
    allowlist: frozenset[str] | None,
    timeout: float | None,
    profile_dir: Path | str | None,
    abrir_contexto: AbrirContexto | None,
    obter_fuso: ObterFuso | None,
    relogio_monotonico: Callable[[], float],
    diagnostico: _Diagnostico,
    coletar_diagnostico: bool,
) -> ResultadoColeta:
    # 1. SSRF / URL inválida, sem abrir navegador.
    motivo = motivo_recusa_url(url)
    url_limpa = url.strip() if isinstance(url, str) else ""
    if motivo is not None:
        return FalhaBusca(motivo, url_limpa)

    # 2. Allowlist, sem abrir navegador.
    lista = config.coleta_dominios_stealth() if allowlist is None else frozenset(allowlist)
    if not dominio_na_allowlist(url_limpa, lista):
        return FalhaBusca("dominio_nao_permitido_stealth", url_limpa)

    # 3. Display (nunca headless).
    if not _tem_display():
        return FalhaBusca("ambiente_sem_display", url_limpa)

    prazo_s = resolver_timeout(timeout)
    perfil = Path(profile_dir) if profile_dir is not None else config.stealth_profile_dir()
    try:
        perfil.mkdir(parents=True, exist_ok=True)  # criado se faltar; nunca apagado
    except OSError:
        return FalhaBusca("navegador_indisponivel", url_limpa)

    # 4–5. Fuso do IP e política WebRTC.
    fuso = (obter_fuso or obter_fuso_ip)(perfil)
    aplicar_politica_webrtc(perfil)

    # 6. Chrome real, headed, perfil persistente.
    abrir = abrir_contexto or abrir_contexto_patchright
    try:
        contexto = abrir(
            user_data_dir=str(perfil),
            channel=CANAL_NAVEGADOR,
            headless=False,
            no_viewport=True,
            timezone_id=fuso,
        )
    except Exception as exc:
        if _classificar_erro(exc) is None:
            raise
        return FalhaBusca("navegador_indisponivel", url_limpa)

    monitor: _MonitorNavegacao | None = None
    try:
        pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
        monitor = _MonitorNavegacao(pagina, diagnostico)
        pagina.route("**/*", monitor.rotear)
        pagina.on("request", monitor.ao_requisitar)
        pagina.on("response", monitor.ao_responder)
        try:
            # 7–10. Navegação, espera, status e conteúdo.
            resultado = _navegar(pagina, monitor, url_limpa, lista, prazo_s, relogio_monotonico)
        finally:
            if coletar_diagnostico:
                _coletar_diagnostico(contexto, pagina, diagnostico)
        return resultado
    except Exception as exc:
        # 11. Bloqueio nosso (route.abort) aparece como erro de navegação.
        if monitor is not None and monitor.violacao is not None:
            return FalhaBusca("redirecionamento_nao_permitido", monitor.violacao)
        tipo = _classificar_erro(exc)
        if tipo is None:
            raise
        return FalhaBusca(tipo, url_limpa)
    finally:
        _fechar_sem_propagar(contexto)


def coletar_via_playwright_stealth(
    url: str,
    *,
    allowlist: frozenset[str] | None = None,
    timeout: float | None = None,
    profile_dir: Path | str | None = None,
    abrir_contexto: AbrirContexto | None = None,
    obter_fuso: ObterFuso | None = None,
    relogio_monotonico: Callable[[], float] = time.monotonic,
) -> ResultadoColeta:
    """Coleta uma página pelo Chrome real (Patchright), só para domínios da allowlist.

    - ``allowlist``: padrão ``config.coleta_dominios_stealth()``.
    - ``timeout``: segundos, regra de ``buscador_paginas.resolver_timeout``
      (padrão ``config.coleta_timeout()``); é o prazo total de ``goto`` +
      espera por ``networkidle`` (o tempo de abrir o navegador não conta).
    - ``profile_dir``: padrão ``config.stealth_profile_dir()``; criado se não
      existir, nunca apagado nem recriado.
    - ``abrir_contexto``: injetável (fakes nos testes); padrão
      ``abrir_contexto_patchright``. Recebe sempre ``channel="chrome"``,
      ``headless=False``, ``no_viewport=True`` e o ``timezone_id`` do IP.
    - ``obter_fuso``: injetável; padrão ``obter_fuso_ip`` (cache 24 h).

    Sucesso: ``PaginaBaixada`` com o status da última resposta de navegação do
    frame principal, o Content-Type dela, ``charset="utf-8"`` e o DOM
    serializado (``page.content()``) em UTF-8. Não grava nada no armazém.
    """
    return _executar(
        url,
        allowlist=allowlist,
        timeout=timeout,
        profile_dir=profile_dir,
        abrir_contexto=abrir_contexto,
        obter_fuso=obter_fuso,
        relogio_monotonico=relogio_monotonico,
        diagnostico=_Diagnostico(),
        coletar_diagnostico=False,
    )


def diagnosticar_stealth(
    url: str,
    *,
    allowlist: frozenset[str] | None = None,
    timeout: float | None = None,
    profile_dir: Path | str | None = None,
    abrir_contexto: AbrirContexto | None = None,
    obter_fuso: ObterFuso | None = None,
    relogio_monotonico: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Mesmo fluxo (e mesmas barreiras) de ``coletar_via_playwright_stealth``,
    para teste manual. Devolve um dict com o desfecho resumido (sem o corpo),
    ``titulo``, ``cookies`` (só ``nome`` e ``dominio``, nunca valores) e
    ``status_navegacoes`` (URL e status de cada resposta de navegação do frame
    principal, em ordem)."""
    diagnostico = _Diagnostico()
    resultado = _executar(
        url,
        allowlist=allowlist,
        timeout=timeout,
        profile_dir=profile_dir,
        abrir_contexto=abrir_contexto,
        obter_fuso=obter_fuso,
        relogio_monotonico=relogio_monotonico,
        diagnostico=diagnostico,
        coletar_diagnostico=True,
    )
    saida: dict[str, Any] = {"url": url}
    if isinstance(resultado, PaginaBaixada):
        saida.update(
            desfecho="pagina",
            motivo=None,
            status_http=resultado.status_http,
            content_type=resultado.content_type,
            url_final=resultado.url_final,
            tamanho_bytes=len(resultado.corpo),
        )
    else:
        saida.update(
            desfecho="falha",
            motivo=resultado.motivo,
            status_http=resultado.status_http,
            content_type=resultado.content_type,
            url_final=resultado.url,
            tamanho_bytes=None,
        )
    saida.update(
        titulo=diagnostico.titulo,
        cookies=list(diagnostico.cookies),
        status_navegacoes=list(diagnostico.status_navegacoes),
    )
    return saida
