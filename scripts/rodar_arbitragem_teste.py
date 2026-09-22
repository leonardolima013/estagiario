"""Encadeia buscar_grupo -> particionar_grupo -> arbitrar_campo pros 3 casos de
teste da SPEC.md §8 (mais o Caso 4, DRIVEWAY JE4699), e imprime a decisão de
cada campo pra cada subcluster duplicata_real encontrado — validação manual do
critério de aceite da Fase 2.

Uso: venv/bin/python scripts/rodar_arbitragem_teste.py
Requer ANTHROPIC_API_KEY no .env e .env apontando pra réplica de dev.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arbitration.arbitrar import arbitrar_campo
from arbitration.models import DecisaoCampo
from llm.anthropic_provider import AnthropicProvider
from partitioning.particionar import Particao, particionar_grupo
from tools.group_fetch import RegistroCatalogPart, buscar_grupo, resolver_brand_id

_CASOS = [
    ("Caso 1 — CITROEN 83061", "83061", "CITROEN"),
    ("Caso 2 — PEUGEOT 83062", "83062", "PEUGEOT"),
    ("Caso 3 — AFFINIA MB1085", "MB1085", "AFFINIA"),
    ("Caso 4 — DRIVEWAY JE4699", "JE4699", "DRIVEWAY"),
]

_CAMPOS_A_ARBITRAR = [
    "width", "depth", "height", "gross_weight", "net_weight",
    "ncm", "barcode", "born_at", "deprecated_at", "application", "name",
]


def _imprimir_decisao(campo: str, decisao: DecisaoCampo) -> None:
    print(f"      {campo}: {decisao.valor!r}  [{decisao.fonte}] — {decisao.justificativa}")


def main() -> None:
    llm = AnthropicProvider()
    for titulo, search_ref, brand in _CASOS:
        brand_id = resolver_brand_id(brand)
        grupo = buscar_grupo(search_ref, brand_id)
        particao: Particao = particionar_grupo(grupo, llm=llm)
        por_id: dict[int, RegistroCatalogPart] = {r.id: r for r in grupo}

        subclusters_duplicata = [s for s in particao.subclusters if s.label == "duplicata_real"]
        print(f"\n=== {titulo} ({len(subclusters_duplicata)} subcluster(s) duplicata_real) ===")
        if not subclusters_duplicata:
            print("  (nenhum subcluster duplicata_real neste grupo — nada pra arbitrar)")
            continue

        for i, subcluster in enumerate(subclusters_duplicata, start=1):
            registros = [por_id[mid] for mid in subcluster.membro_ids]
            print(f"\n  Subcluster {i}: {[r.name for r in registros]}")
            for campo in _CAMPOS_A_ARBITRAR:
                decisao = arbitrar_campo(registros, campo, llm=llm, rule_store=None, brand_id=brand_id)
                _imprimir_decisao(campo, decisao)


if __name__ == "__main__":
    main()
