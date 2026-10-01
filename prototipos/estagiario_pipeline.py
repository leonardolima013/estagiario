"""Orquestração mínima: busca -> decisão -> fetch -> armazenamento.

Fetch aqui é apenas httpx, sem fallback de browser — isso entra numa etapa
seguinte, quando o Playwright for incorporado para SPAs.
"""
from __future__ import annotations

import httpx

from estagiario_source_selection import PartQuery, SearchResult, normalize_url, select_sources
from estagiario_storage import PageStore

HTTP_TIMEOUT = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; EstagiarioBot/0.1)"


def fetch_and_store(
    store: PageStore,
    part: PartQuery,
    results: list[SearchResult],
    *,
    max_accepted: int = 3,
) -> list[str]:
    """Busca e armazena as páginas aceitas para a peça; devolve os sha256 salvos."""
    decisions = select_sources(results, part, max_accepted=max_accepted)
    saved: list[str] = []

    with httpx.Client(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
        for decision in decisions:
            url_norm = normalize_url(decision.result.url)

            existing = store.already_fetched(url_norm, max_age_days=30)
            if existing is not None:
                saved.append(existing.sha256)
                continue

            try:
                resp = client.get(decision.result.url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue  # falha de rede/bloqueio: pula a fonte, não derruba o job

            sha = store.save_html(
                resp.text,
                url=decision.result.url,
                url_normalized=url_norm,
                domain=decision.result.domain,
                part_search_ref=part.search_ref,
                part_brand=part.brand,
                match_field="sku" if decision.matched_code else "unknown",
                confidence=decision.confidence.value,
                status_code=resp.status_code,
            )
            saved.append(sha)

    return saved
