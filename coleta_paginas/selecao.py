"""Lógica pura de identidade da coleta de páginas (spec html-extract-save).

Trava de entrada por marca sem referência (Req 1). As regras de identidade não
são redefinidas aqui: reutilizam, somente para leitura, as funções de
`verification.serper_decisao` (`marca_generica` e a mesma condição de código
válido de `_padrao_codigo`/`codigo_explicito`).

Todas as funções são puras: sem I/O, sem LLM e sem mutação das entradas.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Sequence

from coleta_paginas.modelos import (
    CodigoPecaInvalidoError,
    Confianca,
    ConfiguracaoColetaError,
    DecisaoFonte,
    ExclusaoUrl,
    FonteSelecionada,
    PecaConsultada,
    PrecondicaoTravaError,
    ResultadoBusca,
    SelecaoFontes,
)
from coleta_paginas.url import host_da_url, motivo_recusa_url, normalizar_url
from verification import serper_decisao
from verification.serper_client import ResultadoOrganico

# Motivos da Decisao_Fonte, pela tabela código × marca (Req 2.3–2.5).
MOTIVO_CODIGO_AUSENTE = "codigo_nao_encontrado: código não encontrado no título nem no snippet"
MOTIVO_CODIGO_E_MARCA = "codigo_e_marca_confirmados"
MOTIVO_CODIGO_SEM_MARCA = "codigo_confirmado_marca_ausente: marca não encontrada no resultado"


def codigo_valido(codigo: str | None) -> bool:
    """True sse o código tem ao menos um caractere alfanumérico após remover
    acentos e aplicar casefold.

    É exatamente a condição em que `serper_decisao._padrao_codigo` devolve um
    padrão, de modo que todo código válido aqui é pesquisável por
    `codigo_explicito` (Req 1.5, 2.9)."""
    if not isinstance(codigo, str):
        return False
    return serper_decisao._padrao_codigo(codigo) is not None


def validar_codigo_peca(peca: PecaConsultada) -> None:
    """Levanta `CodigoPecaInvalidoError` quando o Codigo_Peca é ausente, vazio ou
    composto só de espaços/caracteres não alfanuméricos (Req 1.5, 2.9)."""
    if not codigo_valido(peca.codigo_peca):
        raise CodigoPecaInvalidoError(peca.codigo_peca)


def marca_sem_referencia(marca: str | None) -> bool:
    """True quando a marca é ausente, vazia, só espaços ou genérica segundo
    `serper_decisao.marca_generica` (CONVERSÃO, ORIGINAL OEM, OEM).

    Os casos ausente/vazio/espaços são verificados explicitamente porque
    `marca_generica(None)` devolve falso (Req 1.1)."""
    if marca is None or not marca.strip():
        return True
    return serper_decisao.marca_generica(marca)


def trava_entrada(peca: PecaConsultada) -> bool:
    """Trava de entrada pública (Req 1.4): recebe só a Peca_Consultada e devolve
    True quando o enriquecimento web NÃO deve rodar.

    Valida o Codigo_Peca antes de tudo, inclusive quando a marca é sem
    referência (Req 1.5). A decisão depende exclusivamente da Marca_Peca, sem
    avaliar se o código é distintivo nem os Nomes_Candidatos (Req 1.2). Sem I/O
    e sem mutação da peça (Req 1.3)."""
    validar_codigo_peca(peca)
    return marca_sem_referencia(peca.marca_peca)



def _nome_reforcado(peca: PecaConsultada, resultado: ResultadoBusca) -> bool:
    """Nome_Reforcado: todas as palavras significativas de algum Nome_Candidato
    aparecem no título ou no snippet, com a normalização de tokens de
    `serper_decisao` (mesma de `escolher_deterministico`). Nome sem palavra
    significativa nunca reforça."""
    base = set(serper_decisao._tokens(resultado.titulo)) | set(
        serper_decisao._tokens(resultado.snippet)
    )
    for nome in peca.nomes_candidatos:
        significativos = serper_decisao._tokens_significativos(nome)
        if significativos and significativos <= base:
            return True
    return False


def _classificar(peca: PecaConsultada, resultado: ResultadoBusca) -> DecisaoFonte:
    codigo = peca.codigo_peca or ""
    marca = peca.marca_peca or ""
    # Só título e snippet; o link não entra na confirmação do código (Req 2.2).
    texto = f"{resultado.titulo or ''} {resultado.snippet or ''}"
    codigo_confirmado = serper_decisao.codigo_explicito(texto, codigo)
    # Função existente sem modificação; ela também examina o link (Req 2.6).
    marca_confirmada = serper_decisao.marca_no_resultado(
        ResultadoOrganico(
            title=resultado.titulo or "",
            link=resultado.url or "",
            snippet=resultado.snippet or "",
            position=resultado.posicao,
        ),
        marca,
    )
    nome_reforcado = _nome_reforcado(peca, resultado)

    # Confiança e motivo dependem só de (código, marca) (Req 2.3–2.5, 2.8).
    if not codigo_confirmado:
        confianca, motivo = Confianca.REJEITADO, MOTIVO_CODIGO_AUSENTE
    elif marca_confirmada:
        confianca, motivo = Confianca.ALTA, MOTIVO_CODIGO_E_MARCA
    else:
        confianca, motivo = Confianca.MEDIA, MOTIVO_CODIGO_SEM_MARCA

    return DecisaoFonte(
        resultado=resultado,
        confianca=confianca,
        codigo_confirmado=codigo_confirmado,
        marca_confirmada=marca_confirmada,
        nome_reforcado=nome_reforcado,
        motivo=motivo,
    )


def classificar_fontes(
    peca: PecaConsultada, resultados: Sequence[ResultadoBusca]
) -> tuple[DecisaoFonte, ...]:
    """Classificador_Fontes (Req 2): uma Decisao_Fonte por Resultado_Busca, na
    ordem de entrada.

    Ordem das verificações, antes de avaliar qualquer resultado:
    1. Codigo_Peca inválido → `CodigoPecaInvalidoError` (Req 2.9);
    2. Marca_Sem_Referencia → `PrecondicaoTravaError` (Req 2.10).

    Puro: sem I/O, sem LLM e sem mutação da peça ou dos resultados (Req 2.11)."""
    validar_codigo_peca(peca)
    if marca_sem_referencia(peca.marca_peca):
        raise PrecondicaoTravaError(
            "Peca_Consultada não passa pela Trava_Entrada: marca sem referência"
        )
    return tuple(_classificar(peca, resultado) for resultado in resultados)



TETO_ACEITOS_PADRAO = 3
_INTEIRO_DECIMAL = re.compile(r"[0-9]+")


def resolver_teto_aceitos(chamada: object | None, ambiente: str | None) -> int:
    """Teto_Aceitos efetivo: chamada > ambiente > 3 (Req 3.7, 3.8).

    - Chamada: aceita somente `int` (nunca `bool`) >= 1.
    - Ambiente: texto que, após `strip()`, casa com ``[0-9]+`` e vale >= 1.

    O ambiente só é consultado quando a chamada é `None`. Texto do ambiente
    com mais dígitos significativos (sem zeros à esquerda) do que o limite de
    conversão de `int()` (`sys.get_int_max_str_digits()`, quando não é 0)
    também é inválido. Valor efetivo inválido levanta
    `ConfiguracaoColetaError(campo="teto_aceitos", valor=repr(valor),
    origem=...)` (Req 3.9)."""
    if chamada is not None:
        if isinstance(chamada, int) and not isinstance(chamada, bool) and chamada >= 1:
            return chamada
        raise ConfiguracaoColetaError("teto_aceitos", repr(chamada), "chamada")
    if ambiente is not None:
        texto = ambiente.strip() if isinstance(ambiente, str) else None
        if texto is not None and _INTEIRO_DECIMAL.fullmatch(texto):
            significativos = texto.lstrip("0")
            limite = sys.get_int_max_str_digits()
            if not limite or len(significativos) <= limite:
                # Converter sem os zeros à esquerda: o limite de int() conta
                # todos os dígitos do texto, inclusive esses zeros.
                valor = int(significativos or "0")
                if valor >= 1:
                    return valor
        raise ConfiguracaoColetaError("teto_aceitos", repr(ambiente), "ambiente")
    return TETO_ACEITOS_PADRAO


def _chave_ordenacao(item: tuple[int, DecisaoFonte]) -> tuple[int, int, bool, int, int]:
    """Chave do design (Req 3.2, 3.3): alta antes de media; nome reforçado
    primeiro; posição crescente com ausentes por último; ordem de entrada."""
    indice, decisao = item
    posicao = decisao.resultado.posicao
    return (
        0 if decisao.confianca is Confianca.ALTA else 1,
        0 if decisao.nome_reforcado else 1,
        posicao is None,
        posicao or 0,
        indice,
    )


def selecionar_fontes(
    decisoes: Sequence[DecisaoFonte], teto_aceitos: int
) -> SelecaoFontes:
    """Seletor_Fontes (Req 3, 4.6–4.8), na ordem do Req 3.5:

    1. aceitos (`alta`/`media`) com seu índice de entrada; rejeitados saem
       sem passar pela validação de URL (Req 3.1, 4.7);
    2. `motivo_recusa_url` em cada aceito: recusados viram `ExclusaoUrl`, na
       ordem de entrada, e não ocupam vaga (Req 3.6, 4.4, 4.5, 4.8);
    3. ordenação estável pela chave do design (Req 3.2, 3.3);
    4. deduplicação por URL_Normalizada, mantendo a primeira (Req 3.4);
    5. corte no teto já resolvido (Req 3.10, 3.11).

    O domínio efetivo é o original ou, se vier em branco, o host da
    URL_Normalizada (Req 4.6). Puro e determinístico (Req 3.12)."""
    permitidos: list[tuple[int, DecisaoFonte]] = []
    excluidas: list[ExclusaoUrl] = []
    for indice, decisao in enumerate(decisoes):
        if decisao.confianca not in (Confianca.ALTA, Confianca.MEDIA):
            continue
        motivo = motivo_recusa_url(decisao.resultado.url)
        if motivo is not None:
            excluidas.append(ExclusaoUrl(decisao=decisao, motivo=motivo))
        else:
            permitidos.append((indice, decisao))

    selecionadas: list[FonteSelecionada] = []
    vistas: set[str] = set()
    for _, decisao in sorted(permitidos, key=_chave_ordenacao):
        if len(selecionadas) >= teto_aceitos:
            break
        # motivo_recusa_url(url) is None garante que url é str válida.
        url_normalizada = normalizar_url(decisao.resultado.url or "")
        if url_normalizada in vistas:
            continue
        vistas.add(url_normalizada)
        dominio = decisao.resultado.dominio
        if dominio is None or not dominio.strip():
            dominio = host_da_url(url_normalizada)
        selecionadas.append(
            FonteSelecionada(
                decisao=decisao, url_normalizada=url_normalizada, dominio=dominio
            )
        )

    return SelecaoFontes(selecionadas=tuple(selecionadas), excluidas=tuple(excluidas))
