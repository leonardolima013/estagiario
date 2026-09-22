"""Heurísticas de apoio ao particionamento (SPEC.md §4.1).

Funções puras, sem I/O — reduzem custo/ambiguidade da chamada ao modelo,
mas não substituem o julgamento dele (não decidem nada sozinhas).
"""

from __future__ import annotations

import re

_SUFIXO_VARIANTE_RE = re.compile(r"\b(STD|\d+,\d{2})\s*$", re.IGNORECASE)
_KIT_RE = re.compile(r"\bKIT\b", re.IGNORECASE)
_ESPACOS_RE = re.compile(r"\s+")


def sinal_variante_dimensional(nome: str) -> str | None:
    """Detecta sufixo de sobremedida (STD, 0,25, 0,50, 0,75, 1,00...) no fim do nome.

    Retorna o sufixo normalizado (maiúsculo, sem espaços extras) ou None.
    """
    match = _SUFIXO_VARIANTE_RE.search(nome.strip())
    if match is None:
        return None
    return match.group(1).upper()


def sinal_kit_componente(nome_a: str, nome_b: str) -> bool:
    """True se um dos nomes for um 'KIT' cujo nome sem 'KIT' é substring do outro."""
    return _e_kit_do_componente(nome_a, nome_b) or _e_kit_do_componente(nome_b, nome_a)


def _e_kit_do_componente(nome_kit: str, nome_componente: str) -> bool:
    if not _KIT_RE.search(nome_kit):
        return False
    sem_kit = _ESPACOS_RE.sub(" ", _KIT_RE.sub("", nome_kit)).strip()
    if not sem_kit:
        return False
    return sem_kit.lower() in nome_componente.lower()


def sinal_item_distinto(
    valor_a: float | None, valor_b: float | None, threshold: float
) -> bool:
    """True se a divergência relativa entre dois valores (peso/dimensão) excede threshold.

    Sem dado suficiente (algum valor None ou ambos zero), não há sinal — retorna False.
    """
    if valor_a is None or valor_b is None:
        return False
    maior = max(abs(valor_a), abs(valor_b))
    if maior == 0:
        return False
    return abs(valor_a - valor_b) / maior > threshold
