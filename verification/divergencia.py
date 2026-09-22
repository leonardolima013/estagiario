"""Detecção de divergência de nome — gatilho da skill de verificação web
(SPEC-verificacao-web-nomenclatura.md §4). Pura, sem I/O.

Não basta os textos serem diferentes: "PLUG ELETRÔNICO ÁGUA" vs "PLUG ELETRÔNICO
ÁGUA 24V MTE" não diverge de verdade — o segundo só tem detalhe a mais (tensão,
marca), nenhuma palavra conflita com a outra, o juiz de qualidade textual interno
já resolve isso (escolhe o mais completo) sem precisar de verificação web. Já
"PIVO SUPERIOR" vs "PIVO INFERIOR" diverge de verdade — os qualificadores
descrevem posições/aplicações diferentes, o Estagiário não tem como saber sozinho
qual delas (se alguma) está certa. A regra: um par de nomes só diverge quando
nenhum dos dois é um "superconjunto" de palavras do outro — havendo apenas
inclusão (um nome contém todas as palavras do outro, só com texto extra), é
completude, não conflito.
"""

from __future__ import annotations

import unicodedata
from itertools import combinations


def _remover_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _tokens(nome: str) -> set[str]:
    return set(_remover_acentos(nome).casefold().split())


def tokens_normalizados(nome: str) -> frozenset[str]:
    """Conjunto de tokens normalizados (sem acento, casefold, split por espaço) de um
    nome — mesma normalização usada pelo gatilho de divergência. Público pra que a
    arbitragem de nome (arbitration/nome.py) case o nome sugerido pela web a um
    candidato de forma tolerante a acento/caixa/ordem, em vez de igualdade exata."""
    return frozenset(_tokens(nome))


def nomes_normalizados_divergem(nomes: list[str]) -> bool:
    tokens_por_nome = [_tokens(n) for n in nomes]
    for a, b in combinations(tokens_por_nome, 2):
        if not (a <= b or b <= a):
            return True
    return False
