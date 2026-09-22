"""Contrato de dados da verificação web de nomenclatura
(SPEC-verificacao-web-nomenclatura.md §6)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class FonteWeb:
    url: str
    nome_encontrado: str


@dataclass(frozen=True)
class ResultadoVerificacao:
    status: Literal["confirmado", "inconclusivo"]
    nome_sugerido: str | None
    justificativa: str
    fontes: list[FonteWeb] = field(default_factory=list)
