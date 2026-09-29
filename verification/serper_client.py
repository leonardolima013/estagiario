"""Cliente_Serper — único ponto de I/O de rede da Skill_Serper.

Faz uma única requisição HTTP POST à API Serper (`google.serper.dev`) usando
exclusivamente a biblioteca padrão do Python (`urllib.request`) — sem nova
dependência de runtime (R9.1). A API_KEY_Serper é lida de `.env` via
`config.serper_api_key()` e nunca é registrada em logs, exceções ou artefatos
(R8.3). Toda a lógica de montagem/extração é pura e testável; a requisição é
injetável por um transporte fake para os testes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

import config

_ENDPOINT = "https://google.serper.dev/search"


@dataclass(frozen=True)
class ResultadoOrganico:
    """Item da lista `organic` da resposta Serper, normalizado. Conteúdo é dado
    não confiável, apenas leitura (R8.1)."""

    title: str
    link: str
    snippet: str
    position: int | None = None


class SerperAPIKeyAusenteError(RuntimeError):
    """API_KEY_Serper ausente/vazia no ambiente — aborta sem requisição
    (R3.5, R9.5). Nunca inclui o valor da chave."""


class SerperRequisicaoError(RuntimeError):
    """Falha de rede, timeout ou status fora de 200–299 (R3.8)."""


def montar_corpo_busca(codigo: str, marca: str) -> dict:
    """Pura: monta o corpo JSON da requisição Serper (R3.1)."""
    return {"q": f"{codigo} {marca}", "gl": "br", "hl": "pt-br"}


def extrair_organicos(resposta_json: dict) -> list[ResultadoOrganico]:
    """Pura: extrai/normaliza a lista `organic` da resposta. Resposta sem
    `organic` ou com lista vazia produz lista vazia (R3.9)."""
    brutos = resposta_json.get("organic") or []
    organicos: list[ResultadoOrganico] = []
    for item in brutos:
        if not isinstance(item, dict):
            continue
        posicao = item.get("position")
        organicos.append(
            ResultadoOrganico(
                title=str(item.get("title") or ""),
                link=str(item.get("link") or ""),
                snippet=str(item.get("snippet") or ""),
                position=posicao if isinstance(posicao, int) else None,
            )
        )
    return organicos


# Transporte HTTP injetável: recebe (url, dados_json, cabecalhos, timeout) e
# devolve o corpo da resposta como dict. O default usa urllib da stdlib; os
# testes passam um fake para evitar rede real.
Transporte = Callable[[str, bytes, dict, float], dict]


def _transporte_urllib(url: str, dados: bytes, cabecalhos: dict, timeout: float) -> dict:
    requisicao = urllib.request.Request(url, data=dados, headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(requisicao, timeout=timeout) as resposta:
            corpo = resposta.read()
    except urllib.error.HTTPError as exc:  # status fora de 2xx
        raise SerperRequisicaoError(
            f"API Serper retornou status {exc.code}."
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # Nunca inclui a chave: exc de rede não carrega o cabeçalho.
        raise SerperRequisicaoError(
            f"Falha de rede/timeout ao consultar a API Serper: {type(exc).__name__}."
        ) from exc
    try:
        return json.loads(corpo.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise SerperRequisicaoError("Resposta da API Serper não é JSON válido.") from exc


class ClienteSerper:
    """I/O HTTP à API Serper. O transporte é injetável para testes; a chave e o
    timeout vêm de `config` por padrão."""

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float | None = None,
        *,
        transporte: Transporte | None = None,
    ):
        # config.serper_api_key() levanta SerperAPIKeyAusenteError se ausente/vazia,
        # ANTES de qualquer requisição (R3.5, R9.5).
        self._api_key = api_key or config.serper_api_key()
        self._timeout = timeout if timeout is not None else config.serper_timeout()
        self._transporte: Transporte = transporte or _transporte_urllib

    def buscar(self, codigo: str, marca: str) -> list[ResultadoOrganico]:
        """Envia o POST a `/search` e devolve os ResultadoOrganico. Levanta
        SerperRequisicaoError em falha de rede/timeout/status != 2xx (R3.8)."""
        corpo = montar_corpo_busca(codigo, marca)
        dados = json.dumps(corpo).encode("utf-8")
        cabecalhos = {
            "X-API-KEY": self._api_key,  # R3.3
            "Content-Type": "application/json",
        }
        resposta_json = self._transporte(_ENDPOINT, dados, cabecalhos, self._timeout)
        return extrair_organicos(resposta_json)
