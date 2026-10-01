"""Decisão de quais páginas de busca são, com boa confiança, sobre a peça pesquisada.

Reaproveita os critérios já definidos na skill de resolução de nomes: o código é o
sinal principal, a marca é só reforço secundário (nunca obrigatória) e é ignorada
quando genérica. Aqui a decisão é sobre ACEITAR A PÁGINA para fetch + armazenamento,
não sobre extrair ou escolher um valor — por isso confiança MEDIUM também é aceita.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlsplit, urlunsplit

# Mesma lista fechada já usada na skill de nomes.
GENERIC_BRANDS = {"CONVERSAO", "ORIGINAL OEM", "OEM"}

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

_VARIANT_MARKERS = {
    "kit": ["kit", "jogo", "conjunto"],
    "oversize": ["oversize", "std", "0.25", "0.50", "0.75", "1.00"],
}


def normalize_code(text: str) -> str:
    """Remove espaços, hífens, pontos etc. e deixa em maiúsculas, para comparar códigos."""
    return _NON_ALNUM.sub("", text.upper())


def normalize_url(url: str) -> str:
    """Normaliza a URL para dedupe: remove query string, fragmento e barra final."""
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme, parts.netloc.lower(), path, "", ""))


def _detect_variant_marker(text: str) -> str | None:
    lowered = text.lower()
    for marker, keywords in _VARIANT_MARKERS.items():
        if any(kw in lowered for kw in keywords):
            return marker
    return None


class Confidence(str, Enum):
    HIGH = "high"       # código bate + (marca bate OU marca genérica/ausente)
    MEDIUM = "medium"   # código bate, mas marca diverge e não é genérica
    REJECTED = "rejected"


@dataclass
class PartQuery:
    search_ref: str  # código/SKU pesquisado
    brand: str  # marca no banco (pode ser genérica)
    name_candidates: list[str] = field(default_factory=list)  # nomes já vistos no grupo


@dataclass
class SearchResult:
    url: str
    title: str
    snippet: str
    domain: str
    position: int


@dataclass
class SourceDecision:
    result: SearchResult
    confidence: Confidence
    matched_code: bool
    matched_brand: bool
    matched_name: bool
    variant_marker: str | None  # "kit" | "oversize" | None
    reason: str


def classify_source(result: SearchResult, part: PartQuery) -> SourceDecision:
    haystack = f"{result.title} {result.snippet}"
    haystack_norm = normalize_code(haystack)
    code_norm = normalize_code(part.search_ref)

    matched_code = bool(code_norm) and code_norm in haystack_norm

    if not matched_code:
        return SourceDecision(
            result=result, confidence=Confidence.REJECTED,
            matched_code=False, matched_brand=False, matched_name=False,
            variant_marker=None, reason="código não encontrado em título/snippet",
        )

    brand_is_generic = part.brand.strip().upper() in GENERIC_BRANDS
    matched_brand = brand_is_generic or (part.brand.strip().upper() in haystack.upper())

    matched_name = any(
        name.strip().upper() in haystack.upper()
        for name in part.name_candidates if name.strip()
    )

    variant_marker = _detect_variant_marker(haystack)

    if matched_brand:
        confidence = Confidence.HIGH
        reason = "código confirmado; marca bate ou é genérica"
    else:
        confidence = Confidence.MEDIUM
        reason = "código confirmado; marca do título diverge da marca no banco"

    return SourceDecision(
        result=result, confidence=confidence,
        matched_code=matched_code, matched_brand=matched_brand, matched_name=matched_name,
        variant_marker=variant_marker, reason=reason,
    )


def select_sources(
    results: list[SearchResult],
    part: PartQuery,
    *,
    min_confidence: Confidence = Confidence.MEDIUM,
    max_accepted: int | None = None,
) -> list[SourceDecision]:
    """Classifica todos os resultados e devolve só os aceitos, na ordem de confiança."""
    order = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.REJECTED: 2}
    accepted = [
        d for d in (classify_source(r, part) for r in results)
        if order[d.confidence] <= order[min_confidence]
    ]
    accepted.sort(key=lambda d: order[d.confidence])
    if max_accepted is not None:
        accepted = accepted[:max_accepted]
    return accepted
