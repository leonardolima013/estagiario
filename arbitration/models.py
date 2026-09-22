"""Estruturas de dados da arbitragem de campo (SPEC.md §4.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FonteDecisao = Literal[
    "sem_conflito",
    "regra_confiabilidade",
    "julgamento_modelo",
    "normalizacao",
    "escalado_humano",
    "verificacao_web",
    "intervencao_humana",
]


@dataclass(frozen=True)
class DecisaoCampo:
    campo: str
    valor: object | None
    justificativa: str
    fonte: FonteDecisao
    escalado_humano: bool = False
    # id do registro de onde o valor decidido veio — só faz sentido quando um
    # candidato específico "venceu" (regra_confiabilidade, julgamento_modelo).
    # None pra sem_conflito (consenso, sem vencedor), normalizacao (valor
    # sintetizado/agregado, não vem de um id só) e escalado_humano (sem vencedor ainda).
    origem_id: int | None = None
    confianca: str | None = None
    evidencias: list[dict[str, object]] = field(default_factory=list)

