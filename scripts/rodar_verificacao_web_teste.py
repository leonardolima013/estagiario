"""Roda verificar_nomenclatura_peca de ponta a ponta contra um caso real de
divergência de nome, ou (se nenhum dos 4 casos fixos tiver um subcluster
duplicata_real com nomes divergentes agora) contra o exemplo sintético da
própria SPEC-verificacao-web-nomenclatura.md §1 (PIVO / PIVO SUPERIOR /
PIVO INFERIOR) — validação manual do critério de aceite dessa skill.

Abre um navegador Chromium real e visível (nunca headless) e faz chamadas
reais à API da Anthropic — custa dinheiro e depende de rede.

Uso: venv/bin/python scripts/rodar_verificacao_web_teste.py
Requer ANTHROPIC_API_KEY no .env, .env apontando pra réplica de dev, e Node.js/
npx disponíveis no sistema com os binários de navegador do Playwright já
baixados (rode `npx -y @playwright/mcp@0.0.81` uma vez manualmente antes, se
for a primeira execução — ver CLAUDE.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.anthropic_provider import AnthropicProvider
from partitioning.particionar import particionar_grupo
from tools.group_fetch import RegistroCatalogPart, buscar_grupo, resolver_brand_id
from verification.divergencia import nomes_normalizados_divergem
from verification.mcp_playwright_agent import verificar_nomenclatura_peca
from verification.models import ResultadoVerificacao

_CASOS = [
    ("Caso 1 — CITROEN 83061", "83061", "CITROEN"),
    ("Caso 2 — PEUGEOT 83062", "83062", "PEUGEOT"),
    ("Caso 3 — AFFINIA MB1085", "MB1085", "AFFINIA"),
    ("Caso 4 — DRIVEWAY JE4699", "JE4699", "DRIVEWAY"),
]

_EXEMPLO_SINTETICO = ("N/D (exemplo sintético da SPEC §1)", "N/D", "PIVO", ["PIVO", "PIVO SUPERIOR", "PIVO INFERIOR"])


def _encontrar_caso_divergente() -> tuple[str, str, list[str]] | None:
    llm = AnthropicProvider()
    for titulo, search_ref, brand in _CASOS:
        brand_id = resolver_brand_id(brand)
        grupo = buscar_grupo(search_ref, brand_id)
        particao = particionar_grupo(grupo, llm=llm)
        por_id: dict[int, RegistroCatalogPart] = {r.id: r for r in grupo}

        for subcluster in particao.subclusters:
            if subcluster.label != "duplicata_real":
                continue
            registros = [por_id[mid] for mid in subcluster.membro_ids]
            nomes = [r.name for r in registros]
            if nomes_normalizados_divergem(nomes):
                print(f"Achou divergência real em {titulo}: {sorted(set(nomes))}")
                return search_ref, brand, sorted(set(nomes))

    return None


def _imprimir_resultado(resultado: ResultadoVerificacao) -> None:
    print(f"\nstatus: {resultado.status}")
    print(f"nome_sugerido: {resultado.nome_sugerido!r}")
    print(f"justificativa: {resultado.justificativa}")
    print("fontes:")
    for f in resultado.fontes:
        print(f"  - {f.url}: {f.nome_encontrado!r}")


def main() -> None:
    encontrado = _encontrar_caso_divergente()
    if encontrado:
        codigo, marca, nomes_conflitantes = encontrado
    else:
        print("Nenhum dos 4 casos fixos tem divergência de nome agora — usando exemplo sintético da SPEC.")
        _, codigo, marca, nomes_conflitantes = _EXEMPLO_SINTETICO

    print(f"\nVerificando: codigo={codigo!r} marca={marca!r} nomes={nomes_conflitantes}\n")
    resultado = verificar_nomenclatura_peca(codigo, marca, nomes_conflitantes, on_evento=print)
    _imprimir_resultado(resultado)


if __name__ == "__main__":
    main()
