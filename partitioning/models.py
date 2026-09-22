"""Estruturas de dados do particionamento de grupo (SPEC.md §4.1, §2.3)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RotuloSubcluster = Literal[
    "duplicata_real",
    "kit_componente",
    "variante_dimensional",
    "distinto_nao_classificado",
]

ROTULOS_VALIDOS: frozenset[str] = frozenset(
    ("duplicata_real", "kit_componente", "variante_dimensional", "distinto_nao_classificado")
)


@dataclass(frozen=True)
class Subcluster:
    label: RotuloSubcluster
    membro_ids: list[int]
    justificativa: str


@dataclass(frozen=True)
class Particao:
    grupo_ref: str
    subclusters: list[Subcluster]
    sinais_heuristicos: list[str] = field(default_factory=list)
    regras_aplicaveis: list[str] = field(default_factory=list)

