"""Roda o pipeline completo (pipeline.executar_caso: particionar_grupo ->
montar_decisao_merge -> gerar_sql) pros 3 casos de teste da SPEC.md §8 (mais o
Caso 4, DRIVEWAY JE4699), e imprime o .sql resultante — NÃO executa nada contra
o banco. Validação manual do critério de aceite da Fase 3.

Uso: venv/bin/python scripts/rodar_geracao_sql_teste.py
Requer ANTHROPIC_API_KEY no .env e .env apontando pra réplica de dev.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.anthropic_provider import AnthropicProvider
from pipeline import executar_caso
from tools.fk_introspection import introspeccao_fk
from tools.group_fetch import resolver_brand_id

_CASOS = [
    ("Caso 1 — CITROEN 83061", "83061", "CITROEN"),
    ("Caso 2 — PEUGEOT 83062", "83062", "PEUGEOT"),
    ("Caso 3 — AFFINIA MB1085", "MB1085", "AFFINIA"),
    ("Caso 4 — DRIVEWAY JE4699", "JE4699", "DRIVEWAY"),
]


def main() -> None:
    llm = AnthropicProvider()
    dependencias_fk = introspeccao_fk("catalog_part", "id")
    print(f"-- {len(dependencias_fk)} dependência(s) de FK encontradas pra catalog_part.id\n")

    for titulo, search_ref, brand in _CASOS:
        brand_id = resolver_brand_id(brand)
        resultado = executar_caso(search_ref, brand_id, llm, dependencias_fk)

        print(f"-- ===== {titulo} =====")
        if not resultado.decisoes:
            print("-- (nenhum subcluster duplicata_real com 2+ membros — nada pra mesclar)\n")
            continue

        print(resultado.sql)


if __name__ == "__main__":
    main()
