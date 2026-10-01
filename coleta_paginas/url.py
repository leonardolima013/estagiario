"""Normalização e validação de URLs da coleta de páginas (Req 4).

Módulo puro: só biblioteca padrão, sem I/O e sem rede. ``socket.inet_aton``
é usado apenas como parser de formas IPv4 legadas (``2130706433``, ``0x7f.1``),
sem resolução de nomes.
"""

from __future__ import annotations

import ipaddress
import socket
import unicodedata
from typing import Literal
from urllib.parse import SplitResult, urlsplit

MotivoExclusao = Literal["url_invalida", "destino_nao_permitido"]

ESQUEMAS_PERMITIDOS = frozenset({"http", "https"})
PORTAS_PADRAO = {"http": 80, "https": 443}

# Faixas proibidas, exatamente as do Req 4.5 (não usa `is_private`, que é mais amplo).
REDES_PROIBIDAS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("0.0.0.0/32"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)


def _tem_espaco_ou_controle(texto: str) -> bool:
    return any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in texto)


def _dividir(url: str) -> SplitResult | None:
    """`urlsplit` + validação de esquema, host e porta; None se a URL for inválida."""
    texto = url.strip()
    if not texto or _tem_espaco_ou_controle(texto):
        return None
    try:
        partes = urlsplit(texto)
        if partes.scheme.lower() not in ESQUEMAS_PERMITIDOS:
            return None
        if not partes.hostname:
            return None
        partes.port  # levanta ValueError se a porta for inválida
    except ValueError:
        return None
    return partes


def _ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Interpreta o host como IP literal, inclusive formas IPv4 legadas; None se não for IP."""
    if ":" in host:
        try:
            ip = ipaddress.IPv6Address(host.partition("%")[0])
        except ValueError:
            return None
        return ip.ipv4_mapped or ip
    try:
        return ipaddress.IPv4Address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ValueError):
        return None


def _destino_proibido(host: str) -> bool:
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    ip = _ip_literal(host)
    return ip is not None and any(ip in rede for rede in REDES_PROIBIDAS)


def motivo_recusa_url(url: str | None) -> MotivoExclusao | None:
    """None se a URL pode ser buscada; caso contrário, o motivo (Req 4.4, 4.5).

    `url_invalida` tem precedência sobre `destino_nao_permitido` (Req 4.8).
    """
    if url is None:
        return "url_invalida"
    partes = _dividir(url)
    if partes is None:
        return "url_invalida"
    if _destino_proibido(partes.hostname or ""):
        return "destino_nao_permitido"
    return None


def _query_sem_utm(query: str) -> str:
    pedacos = [
        pedaco
        for pedaco in query.split("&")
        if pedaco.partition("=")[0][:4].lower() != "utm_"
    ]
    return "&".join(pedacos)


def normalizar_url(url: str) -> str:
    """URL_Normalizada (Req 4.1, 4.2), idempotente (Req 4.3).

    Pré-condição: ``motivo_recusa_url(url) is None``. Esquema e host em
    minúsculas, porta padrão removida, barras finais do caminho removidas,
    parâmetros ``utm_*`` removidos (query dividida crua por ``&``, sem
    decodificar), ``?`` omitido quando nada sobra e fragmento descartado.
    """
    partes = _dividir(url)
    if partes is None:
        raise ValueError("normalizar_url exige uma URL válida (motivo_recusa_url(url) is None)")

    esquema = partes.scheme.lower()
    userinfo, arroba, hostinfo = partes.netloc.rpartition("@")
    host = (partes.hostname or "").lower()
    if hostinfo.startswith("["):
        host = f"[{host}]"
    porta = partes.port
    sufixo_porta = f":{porta}" if porta is not None and porta != PORTAS_PADRAO[esquema] else ""
    netloc = f"{userinfo}{arroba}{host}{sufixo_porta}"

    caminho = partes.path.rstrip("/")
    query = _query_sem_utm(partes.query)
    return f"{esquema}://{netloc}{caminho}{'?' + query if query else ''}"


def host_da_url(url: str) -> str:
    """Host em minúsculas, sem porta e sem colchetes IPv6 (Req 4.6); vazio se não houver."""
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""
