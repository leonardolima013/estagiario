"""Reputação de domínio por camada de fetch (regra pura).

Um domínio fica excluído de uma camada (``urllib`` ou ``playwright_stealth``)
quando as ``LIMITE_FALHAS_CONSECUTIVAS`` tentativas mais recentes nessa camada
falharam. Um sucesso entre elas zera a contagem. As tentativas vêm de
``db.armazem_paginas.ArmazemPaginas.ultimas_tentativas`` (mais recentes
primeiro); camadas são independentes porque a consulta já filtra por camada.

Com o fallback stealth ligado, o ``ColetorPaginas`` registra uma Tentativa_Fetch
por busca e consulta a reputação antes de buscar. Este módulo também traz as
regras puras desse uso: quando uma falha urllib é elegível ao fallback
(``motivo_elegivel_fallback``) e se um desfecho entra na reputação
(``resultado_tentativa``). Com o fallback desligado, nada disso é chamado.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from tools.buscador_paginas import FalhaBusca, PaginaBaixada

LIMITE_FALHAS_CONSECUTIVAS = 3

STATUS_BLOQUEIO = frozenset({403, 429, 503})
"""Status_Bloqueio: status HTTP que tornam a falha urllib elegível ao fallback."""

MOTIVOS_FALHA_REPUTACAO_SEM_STATUS = frozenset({"rede", "timeout"})
"""Motivos de falha que contam na reputação independentemente do status."""


def motivo_elegivel_fallback(falha: FalhaBusca) -> bool:
    """Motivo_Elegivel_Fallback: motivo ``status_http`` com status em
    ``STATUS_BLOQUEIO``. A elegibilidade da fonte (allowlist e reputação da
    camada stealth) é verificada à parte pelo coletor."""
    return falha.motivo == "status_http" and falha.status_http in STATUS_BLOQUEIO


def resultado_tentativa(desfecho: PaginaBaixada | FalhaBusca) -> bool | None:
    """Indicador de sucesso da Tentativa_Fetch a registrar para `desfecho`.

    ``True`` para ``PaginaBaixada``; ``False`` para Motivos_Falha_Reputacao
    (``status_http`` com status em ``STATUS_BLOQUEIO``, ``rede``, ``timeout``);
    ``None`` quando o desfecho não entra na reputação (demais motivos, ambiente,
    allowlist e URL)."""
    if isinstance(desfecho, PaginaBaixada):
        return True
    if desfecho.motivo in MOTIVOS_FALHA_REPUTACAO_SEM_STATUS:
        return False
    if motivo_elegivel_fallback(desfecho):
        return False
    return None


class _ComSucesso(Protocol):
    """Qualquer tentativa com o indicador ``sucesso`` (ex.: ``TentativaFetch``)."""

    @property
    def sucesso(self) -> bool: ...


def dominio_excluido(
    ultimas: Sequence[_ComSucesso], limite: int = LIMITE_FALHAS_CONSECUTIVAS
) -> bool:
    """True sse há ≥ `limite` tentativas e as `limite` primeiras (as mais
    recentes) são todas falha. `limite` < 1 é erro de uso."""
    if limite < 1:
        raise ValueError("limite precisa ser >= 1")
    if len(ultimas) < limite:
        return False
    return not any(tentativa.sucesso for tentativa in ultimas[:limite])
