"""Roda particionar_grupo contra os 3 casos de teste da SPEC.md §8 (mais o Caso 4,
DRIVEWAY JE4699) e imprime o resultado de forma legível, para validação manual
(critério de aceite da Fase 1).

Uso: venv/bin/python scripts/rodar_particionamento_teste.py
Requer ANTHROPIC_API_KEY no .env e .env apontando pra réplica de dev.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.anthropic_provider import AnthropicProvider
from partitioning.particionar import Particao, particionar_grupo
from tools.group_fetch import RegistroCatalogPart, buscar_grupo, resolver_brand_id

_CASOS = [
    ("Caso 1 — CITROEN 83061", "83061", "CITROEN"),
    ("Caso 2 — PEUGEOT 83062", "83062", "PEUGEOT"),
    ("Caso 3 — AFFINIA MB1085", "MB1085", "AFFINIA"),
    ("Caso 4 — DRIVEWAY JE4699", "JE4699", "DRIVEWAY"),
]


def _imprimir(titulo: str, grupo: list[RegistroCatalogPart], particao: Particao) -> None:
    por_id = {r.id: r for r in grupo}
    print(f"\n=== {titulo} ({len(grupo)} registros -> {len(particao.subclusters)} subclusters) ===")
    for i, sub in enumerate(particao.subclusters, start=1):
        print(f"\n  Subcluster {i} [{sub.label}] — {sub.justificativa}")
        for membro_id in sub.membro_ids:
            registro = por_id[membro_id]
            print(f"    - id={registro.id}: {registro.name}")


def main() -> None:
    llm = AnthropicProvider()
    for titulo, search_ref, brand in _CASOS:
        grupo = buscar_grupo(search_ref, resolver_brand_id(brand))
        particao = particionar_grupo(grupo, llm=llm)
        _imprimir(titulo, grupo, particao)


if __name__ == "__main__":
    main()
