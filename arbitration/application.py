"""Normalização + união do campo application (SPEC.md §4.2) — determinístico,
sem chamada de modelo.
"""

from __future__ import annotations

import re

from arbitration.models import DecisaoCampo
from tools.group_fetch import RegistroCatalogPart
from tools.reliability import ORDEM_CONFIABILIDADE, buscar_fonte_atual, nivel_confiabilidade

_ESPACOS_RE = re.compile(r"\s+")


def _normalizar_linhas(texto: str | None) -> list[str]:
    """strip + normalização de espaços/quebras + dedupe case-insensitive, preservando ordem."""
    if not texto:
        return []
    vistas: set[str] = set()
    resultado: list[str] = []
    for linha in texto.strip().splitlines():
        limpa = _ESPACOS_RE.sub(" ", linha.strip())
        if not limpa:
            continue
        chave = limpa.lower()
        if chave in vistas:
            continue
        vistas.add(chave)
        resultado.append(limpa)
    return resultado


def arbitrar_application(
    registros: list[RegistroCatalogPart],
    brand_id: int,
    buscar_fonte=buscar_fonte_atual,
    calcular_nivel_confiabilidade=nivel_confiabilidade,
) -> DecisaoCampo:
    """União das linhas de application de todos os registros, sem escolher um só.

    Ordena os registros por confiabilidade da fonte (mais confiável primeiro) antes
    de unir, pra que a linha de um registro mais confiável apareça primeiro em caso
    de conteúdo parecido mas não idêntico.
    """

    def ordem(registro: RegistroCatalogPart) -> int:
        fonte = buscar_fonte(registro.id, "application")
        return -ORDEM_CONFIABILIDADE[calcular_nivel_confiabilidade(fonte, brand_id)]

    ordenados = sorted(registros, key=ordem)

    vistas: set[str] = set()
    linhas_finais: list[str] = []
    for registro in ordenados:
        for linha in _normalizar_linhas(registro.application):
            chave = linha.lower()
            if chave in vistas:
                continue
            vistas.add(chave)
            linhas_finais.append(linha)

    valor = "\n".join(linhas_finais) if linhas_finais else None
    return DecisaoCampo(
        campo="application",
        valor=valor,
        justificativa="União normalizada das linhas, ordenada por confiabilidade da fonte.",
        fonte="normalizacao",
    )
