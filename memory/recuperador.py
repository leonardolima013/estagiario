"""Recuperação lexical de intervenções humanas.

A implementação atual é deliberadamente local/determinística: normaliza tokens e
calcula F1 entre o texto do caso e os sinais da regra. A interface `Recuperador`
permite trocar esse componente por embeddings reais sem alterar os callers.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol, Sequence

from db.rule_store import Regra, RuleStore
from memory.models import PedidoIntervencao, RecuperacaoIntervencao

_STOPWORDS = frozenset(
    "a o as os um uma de da do das dos em no na nos nas por para com sem e ou "
    "que quando como entre este esta isso se deve deve-se usar usar-se "
    "qual qualificar classificar peca pecas nomes nome caso grupo registro "
    "registros valor valores campo fonte fontes web busca intervencao humana "
    "nao sugere confirmou relacao".split()
)
_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


def _remover_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def tokens_busca(texto: str | None) -> frozenset[str]:
    """Normaliza texto para busca: acento, caixa, pontuação e stopwords."""
    if not texto:
        return frozenset()
    normalizado = _remover_acentos(texto).casefold().replace("_", " ")
    # Separar depois de trocar pontuação evita que palavras adjacentes se unam.
    tokens = set(_TOKEN_RE.sub(" ", normalizado).split())
    return frozenset(t for t in tokens if len(t) > 1 and t not in _STOPWORDS)


def _raiz_token(token: str) -> str:
    """Reduz variações simples de gênero/número sem adicionar dependência de NLP."""
    raiz = token[:-1] if len(token) > 4 and token.endswith("s") else token
    if len(raiz) > 4 and raiz.endswith(("a", "o")):
        raiz = raiz[:-1]
    return raiz


def _raizes(tokens: frozenset[str]) -> frozenset[str]:
    return frozenset(_raiz_token(token) for token in tokens)


def sinais_para_busca(*textos: str | None) -> str:
    """Produz um campo estável e legível para indexar a regra semântica."""
    tokens: set[str] = set()
    for texto in textos:
        tokens.update(tokens_busca(texto))
    return " ".join(sorted(tokens))


class Recuperador(Protocol):
    """Contrato para recuperação lexical agora e embeddings no futuro."""

    def recuperar(
        self, consulta: str, regras: Sequence[Regra], limiar: float
    ) -> RecuperacaoIntervencao | None:
        ...


def _texto_regra(regra: Regra) -> str:
    if regra.sinais_busca:
        return " ".join(p for p in (regra.titulo, regra.sinais_busca) if p)
    return " ".join(
        p for p in (regra.titulo, regra.condicao, regra.resolucao) if p
    )


def _f1_sobreposicao(consulta: str, documento: str) -> float:
    tokens_consulta = _raizes(tokens_busca(consulta))
    tokens_documento = _raizes(tokens_busca(documento))
    if not tokens_consulta or not tokens_documento:
        return 0.0
    intersecao = len(tokens_consulta & tokens_documento)
    precisao = intersecao / len(tokens_documento)
    cobertura = intersecao / len(tokens_consulta)
    if precisao + cobertura == 0:
        return 0.0
    return 2 * precisao * cobertura / (precisao + cobertura)


class RecuperadorLexico:
    """Recuperador por F1 de tokens, com limiar de aplicação automática."""

    def __init__(self, limiar_padrao: float = 0.45) -> None:
        if not 0 <= limiar_padrao <= 1:
            raise ValueError("limiar_padrao deve estar entre 0 e 1")
        self.limiar_padrao = limiar_padrao

    def pontuar(self, consulta: str, regra: Regra) -> float:
        return _f1_sobreposicao(consulta, _texto_regra(regra))

    def recuperar(
        self, consulta: str, regras: Sequence[Regra], limiar: float | None = None
    ) -> RecuperacaoIntervencao | None:
        limite = self.limiar_padrao if limiar is None else limiar
        if not 0 <= limite <= 1:
            raise ValueError("limiar deve estar entre 0 e 1")

        melhor: tuple[float, str, int, Regra] | None = None
        for regra in regras:
            score = self.pontuar(consulta, regra)
            # Maior score vence; em empate, regra mais recente e depois maior id.
            # ISO8601 em UTC/SQLite é suficiente para o desempate sem comparar
            # datetime aware com datetime naive de arquivos legados.
            criado_em = regra.criado_em or ""
            chave = (score, criado_em, regra.id, regra)
            if melhor is None or chave[:3] > melhor[:3]:
                melhor = chave

        if melhor is None or melhor[0] < limite:
            return None
        return RecuperacaoIntervencao(regra=melhor[3], score=melhor[0])


def consultar_intervencao(
    caso: PedidoIntervencao | str,
    rule_store: RuleStore,
    *,
    campo: str | None = None,
    limiar: float = 0.45,
    recuperador: Recuperador | None = None,
) -> RecuperacaoIntervencao | None:
    """Recupera uma regra de intervenção acima do limiar, se existir."""
    consulta = caso if isinstance(caso, str) else caso.texto_busca()
    regras = rule_store.listar_intervencoes(campo=campo)
    motor = recuperador or RecuperadorLexico(limiar_padrao=limiar)
    return motor.recuperar(consulta, regras, limiar)
