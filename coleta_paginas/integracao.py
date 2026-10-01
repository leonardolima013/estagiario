"""Integracao_Coleta: liga a coleta de páginas à pesquisa web da arbitragem de nome
(spec html-extract-on-web-search).

Este módulo implementa a porta ``arbitration.pesquisa_web.PortaColeta``. Para
cada Ativacao_Pesquisa, a integração decide se a coleta roda (precedência do
Req 4.5), converte os Resultados_Estruturados em ``ResultadoBusca``, chama o
Coletor_Paginas e publica um único ``DesfechoIntegracao`` no trace e no
Canal_Avisos.

Aqui ficam os modelos, as funções puras (precedência, conversão, forma do
evento de trace e das mensagens) e ``IntegracaoColeta``. Tudo que sai deste módulo — evento de trace e
mensagens — fica restrito aos Campos_Permitidos (Req 7.5, 8.4): nunca bytes,
trechos ou títulos de página, cabeçalhos HTTP nem mensagens de exceção.

Importar este módulo não faz I/O nem abre SQLite. Nenhum import de frontend.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal

from arbitration.pesquisa_web import (
    AtivacaoPesquisa,
    ContextoPesquisaWeb,
    EstadoColeta,
    MetodoVerificador,
)
from coleta_paginas.coletor import ColetorPaginas, ConfigColeta
from coleta_paginas.modelos import (
    CAMADA_STEALTH,
    CAMADA_URLLIB,
    PecaConsultada,
    RelatorioColeta,
    ResultadoBusca,
)
from coleta_paginas.selecao import trava_entrada
from config import NOME_VAR_COLETA_HABILITADA, NOME_VAR_COLETA_STEALTH_HABILITADA

if TYPE_CHECKING:
    from config import EstadoChaveColeta
    from loop.tracing import TraceSink
    from tools.buscador_paginas import TransporteHTTP
    from tools.buscador_stealth import ColetaStealth, TravaNavegador
    from verification.serper_client import ResultadoOrganico


MotivoNaoExecucao = Literal[
    "pesquisa_desligada",
    "desabilitada",
    "configuracao_invalida",
    "cancelada",
    "sem_resultados_estruturados",
    "pesquisa_nao_realizada",
]
Desfecho = Literal["executada", "nao_enriquecivel", "nao_executada", "erro"]

PREFIXO_AVISO = "Coleta de páginas:"
NOME_EVENTO_TRACE = "coletar_paginas"

# Contagens por desfecho de entrada do Relatorio_Coleta (Req 7.3).
DESFECHOS_ENTRADA = ("armazenado", "reaproveitado", "falha", "url_invalida")
MOTIVO_ENTRADA_CANCELADO = "cancelado"

# Camadas contadas em ``contagem_camada`` do trace com stealth (Req 8.5).
CAMADAS_TRACE = (CAMADA_URLLIB, CAMADA_STEALTH)

# Aviso de Chave_Stealth inválida sem Opcao_Stealth (Req 1.3).
MOTIVO_STEALTH_CONFIG_INVALIDA = "stealth_configuracao_invalida"


@dataclass(frozen=True)
class DesfechoIntegracao:
    """Desfecho_Integracao de uma Ativacao_Pesquisa."""

    desfecho: Desfecho
    motivo: str | None  # marca_sem_referencia | MotivoNaoExecucao | nome da classe | None
    metodo: MetodoVerificador
    codigo_peca: str | None
    marca_peca: str | None
    quantidade_resultados: int  # len(ativacao.resultados or ())
    duracao_ms: float
    relatorio: RelatorioColeta | None = None  # só em executada
    variavel: str | None = None  # só em configuracao_invalida


def resolver_coleta_efetiva(opcao: bool | None, chave: EstadoChaveColeta) -> EstadoColeta:
    """Coleta_Efetiva: a Opcao_Coleta prevalece (Req 9.3); sem opção, vale a
    Chave_Habilitacao (habilitada, desabilitada ou invalida)."""
    if opcao is None:
        return chave
    return "habilitada" if opcao else "desabilitada"


def resolver_stealth_efetivo(opcao: bool | None, chave: EstadoChaveColeta) -> EstadoColeta:
    """Stealth_Efetivo: a Opcao_Stealth prevalece, inclusive sobre Chave_Stealth
    inválida (Req 1.4); sem opção, vale a Chave_Stealth (habilitada, desabilitada
    ou invalida)."""
    if opcao is None:
        return chave
    return "habilitada" if opcao else "desabilitada"


def motivo_nao_execucao(
    ativacao: AtivacaoPesquisa, coleta: EstadoColeta, cancelado: bool
) -> MotivoNaoExecucao | None:
    """Primeira condição de não execução na ordem do Req 4.5, exceto a trava.

    ``None`` indica que a ativação segue para a Trava_Entrada e o coletor.
    """
    if ativacao.situacao == "desligada":
        return "pesquisa_desligada"
    if coleta == "desabilitada":
        return "desabilitada"
    if coleta == "invalida":
        return "configuracao_invalida"
    if cancelado:
        return "cancelada"
    if ativacao.situacao == "sem_estruturados":
        return "sem_resultados_estruturados"
    if ativacao.situacao == "nao_realizada":
        return "pesquisa_nao_realizada"
    return None


def converter_resultados(
    organicos: Sequence[ResultadoOrganico],
) -> tuple[ResultadoBusca, ...]:
    """Converte 1:1, na ordem, sem filtrar nem completar (Req 2.2, 2.4, 2.5).

    ``dominio`` fica ausente para o Coletor_Paginas derivá-lo do host da URL.
    """
    return tuple(
        ResultadoBusca(
            titulo=organico.title,
            snippet=organico.snippet,
            url=organico.link,
            dominio=None,
            posicao=organico.position,
        )
        for organico in organicos
    )


def peca_da_ativacao(ativacao: AtivacaoPesquisa) -> PecaConsultada:
    """Peca_Consultada com código, marca e nomes da ativação, sem transformação (Req 2.3)."""
    return PecaConsultada(
        codigo_peca=ativacao.codigo,
        marca_peca=ativacao.marca,
        nomes_candidatos=tuple(ativacao.nomes_conflitantes),
    )


def _relatorio_executado(desfecho: DesfechoIntegracao) -> RelatorioColeta | None:
    if desfecho.desfecho != "executada":
        return None
    return desfecho.relatorio


def status_trace(desfecho: DesfechoIntegracao) -> str:
    """Status do Evento_Coleta_Trace (Req 7.4).

    ``executada`` → ``cancelado`` se alguma entrada tem motivo ``cancelado``,
    senão ``ok``; os demais desfechos usam o próprio nome.
    """
    if desfecho.desfecho != "executada":
        return desfecho.desfecho
    relatorio = desfecho.relatorio
    if relatorio is not None and any(
        entrada.motivo == MOTIVO_ENTRADA_CANCELADO for entrada in relatorio.entradas
    ):
        return "cancelado"
    return "ok"


def evento_trace(desfecho: DesfechoIntegracao) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(entrada, saida)`` do Evento_Coleta_Trace, só com Campos_Permitidos
    (Req 7.2, 7.3, 7.5). Contagens zeradas e sem ``entradas`` fora de ``executada``.

    Com ``relatorio.stealth`` (spec stealth-fallback-integration, Req 8.2, 8.5),
    cada item de ``entradas`` ganha ``camada`` e ``motivo_urllib`` e ``saida``
    ganha ``contagem_camada`` com as duas camadas, inclusive zeros. Sem stealth,
    o dicionário é idêntico ao de antes da feature (Req 10.2)."""
    entrada: dict[str, Any] = {
        "codigo_peca": desfecho.codigo_peca,
        "marca_peca": desfecho.marca_peca,
        "metodo": desfecho.metodo,
        "quantidade_resultados": desfecho.quantidade_resultados,
    }

    contagem = {nome: 0 for nome in DESFECHOS_ENTRADA}
    saida: dict[str, Any] = {
        "desfecho": desfecho.desfecho,
        "motivo": desfecho.motivo,
        "duracao_ms": desfecho.duracao_ms,
        "contagem": contagem,
    }
    relatorio = _relatorio_executado(desfecho)
    if relatorio is not None:
        for item in relatorio.entradas:
            contagem[item.desfecho] = contagem.get(item.desfecho, 0) + 1
        itens: list[dict[str, Any]] = []
        for item in relatorio.entradas:
            dados: dict[str, Any] = {
                "url": item.url,
                "dominio": item.dominio,
                "confianca": str(item.confianca),
                "desfecho": item.desfecho,
                "motivo": item.motivo,
                "status_http": item.status_http,
                "hash_conteudo": item.hash_conteudo,
            }
            if relatorio.stealth:
                dados["camada"] = item.camada
                dados["motivo_urllib"] = item.motivo_urllib
            itens.append(dados)
        saida["entradas"] = itens
        if relatorio.stealth:
            saida["contagem_camada"] = {
                camada: sum(1 for item in relatorio.entradas if item.camada == camada)
                for camada in CAMADAS_TRACE
            }
    if desfecho.variavel is not None:
        saida["variavel"] = desfecho.variavel
    return entrada, saida


def _json(dados: Mapping[str, Any]) -> str:
    return json.dumps(dados, ensure_ascii=False, sort_keys=True)


def mensagem_desfecho(desfecho: DesfechoIntegracao) -> str:
    """Mensagem única ao Canal_Avisos para ``nao_executada``, ``nao_enriquecivel``
    e ``erro`` (Req 8.3): prefixo + JSON com desfecho, motivo e, em
    ``configuracao_invalida``, o nome da variável."""
    dados: dict[str, Any] = {"desfecho": desfecho.desfecho, "motivo": desfecho.motivo}
    if desfecho.variavel is not None:
        dados["variavel"] = desfecho.variavel
    return f"{PREFIXO_AVISO} {_json(dados)}"


def mensagem_evento(evento: Mapping[str, Any]) -> str:
    """Mensagem ao Canal_Avisos para um Evento_Log_Coleta (Req 8.1): prefixo +
    o mesmo JSON que o coletor manda ao logger."""
    return f"{PREFIXO_AVISO} {_json(evento)}"


def mensagem_stealth_invalida() -> str:
    """Aviso ao Canal_Avisos de Chave_Stealth inválida (Req 1.3): prefixo + JSON
    com o motivo ``stealth_configuracao_invalida`` e o nome da variável."""
    return (
        f"{PREFIXO_AVISO} "
        f"{_json({'motivo': MOTIVO_STEALTH_CONFIG_INVALIDA, 'variavel': NOME_VAR_COLETA_STEALTH_HABILITADA})}"
    )



class IntegracaoColeta:
    """Implementação de ``PortaColeta``: um ``DesfechoIntegracao`` por
    Ativacao_Pesquisa, sem nunca propagar exceção (Req 4.4, 5.1).

    O coletor (e com ele o Armazem_Paginas) é construído de forma preguiçosa na
    primeira ativação que chega à chamada ``coletar`` e reaproveitado nas
    seguintes desta instância (Req 9.8). Uma construção que falha não fica em
    cache. Construir a instância não faz I/O.

    Padrão: ``ColetorPaginas(ArmazemPaginas(), transporte=..., config=...,
    relogio=...)``, com a configuração ``ESTAGIARIO_COLETA_*`` e
    ``ESTAGIARIO_PAGINAS_DB_PATH`` do spec html-extract-save (Req 9.6).

    Fallback stealth (spec stealth-fallback-integration): ``fallback_stealth``,
    ``allowlist_stealth`` e ``trava_navegador`` são repassados à fábrica padrão
    do coletor; ``None`` mantém os padrões do ``ColetorPaginas``. O coletor só
    recebe ``stealth=True`` quando ``contexto.stealth_efetivo == "habilitada"``
    (Req 1.5); com ``"invalida"``, a ativação que chama o coletor publica uma
    vez o aviso ``stealth_configuracao_invalida`` (Req 1.3).
    """

    def __init__(
        self,
        *,
        fabrica_coletor: Callable[[], ColetorPaginas] | None = None,
        transporte: TransporteHTTP | None = None,
        config_coleta: ConfigColeta | None = None,
        relogio: Callable[[], datetime] | None = None,
        relogio_monotonico: Callable[[], float] = time.perf_counter,
        fallback_stealth: ColetaStealth | None = None,
        allowlist_stealth: frozenset[str] | None = None,
        trava_navegador: TravaNavegador | None = None,
    ) -> None:
        self._fabrica_coletor = fabrica_coletor or self._fabrica_padrao
        self._transporte = transporte
        self._config_coleta = config_coleta
        self._relogio = relogio
        self._relogio_monotonico = relogio_monotonico
        self._fallback_stealth = fallback_stealth
        self._allowlist_stealth = allowlist_stealth
        self._trava_navegador = trava_navegador
        self._coletor_cache: ColetorPaginas | None = None
        self._lock = Lock()

    def _fabrica_padrao(self) -> ColetorPaginas:
        # Import preguiçoso: abrir o armazém é I/O e só acontece aqui.
        from db.armazem_paginas import ArmazemPaginas

        return ColetorPaginas(
            ArmazemPaginas(),
            transporte=self._transporte,
            config=self._config_coleta,
            relogio=self._relogio,
            fallback_stealth=self._fallback_stealth,
            allowlist_stealth=self._allowlist_stealth,
            trava_navegador=self._trava_navegador,
        )

    def _coletor(self) -> ColetorPaginas:
        """Coletor único da instância; falha de construção não fica em cache."""
        with self._lock:
            if self._coletor_cache is None:
                self._coletor_cache = self._fabrica_coletor()
            return self._coletor_cache

    def processar(
        self,
        ativacao: AtivacaoPesquisa,
        *,
        contexto: ContextoPesquisaWeb,
        trace: TraceSink | None,
    ) -> DesfechoIntegracao:
        """Decide, executa e publica a coleta de uma Ativacao_Pesquisa."""
        inicio = self._relogio_monotonico()
        quantidade = len(ativacao.resultados or ())

        def _desfecho(
            desfecho: Desfecho,
            motivo: str | None,
            *,
            relatorio: RelatorioColeta | None = None,
            variavel: str | None = None,
        ) -> DesfechoIntegracao:
            duracao = max(0.0, (self._relogio_monotonico() - inicio) * 1000)
            return DesfechoIntegracao(
                desfecho=desfecho,
                motivo=motivo,
                metodo=ativacao.metodo,
                codigo_peca=ativacao.codigo,
                marca_peca=ativacao.marca,
                quantidade_resultados=quantidade,
                duracao_ms=duracao,
                relatorio=relatorio,
                variavel=variavel,
            )

        try:
            resultado = self._executar(ativacao, contexto, _desfecho)
        except Exception as exc:  # noqa: BLE001 — Req 5.1: nunca propagar
            # Só o nome da classe; a mensagem da exceção nunca sai (Req 5.1, 7.5).
            resultado = _desfecho("erro", type(exc).__name__)

        self._publicar(resultado, contexto, trace)
        return resultado

    def _executar(
        self,
        ativacao: AtivacaoPesquisa,
        contexto: ContextoPesquisaWeb,
        fazer: Callable[..., DesfechoIntegracao],
    ) -> DesfechoIntegracao:
        sinal = contexto.cancel_event
        cancelado = sinal is not None and sinal.is_set()

        # 1. Precedência do Req 4.5, exceto a trava.
        motivo = motivo_nao_execucao(ativacao, contexto.coleta_efetiva, cancelado)
        if motivo is not None:
            variavel = (
                NOME_VAR_COLETA_HABILITADA if motivo == "configuracao_invalida" else None
            )
            return fazer("nao_executada", motivo, variavel=variavel)

        # 2. Trava_Entrada antes de construir o coletor/abrir o armazém (Req 3.2).
        peca = peca_da_ativacao(ativacao)
        if trava_entrada(peca):
            return fazer("nao_enriquecivel", "marca_sem_referencia")

        # 3. Coletor preguiçoso e chamada única (Req 1.1, 9.8).
        coletor = self._coletor()
        canal = contexto.canal_avisos

        def _observador(evento: Mapping[str, Any]) -> None:
            # Direto ao on_aviso original, sem o wrapper ``avisar`` (Req 8.1).
            if canal is not None:
                canal(mensagem_evento(evento))

        # Aviso de Chave_Stealth inválida só quando o coletor é chamado (Req 1.3, 1.5).
        if contexto.stealth_efetivo == "invalida" and canal is not None:
            canal(mensagem_stealth_invalida())

        kwargs: dict[str, Any] = {
            "cancelado": sinal.is_set if sinal is not None else None,
            "observador": _observador,
        }
        # A chave ``stealth`` só vai ao coletor quando habilitada, para manter os
        # coletores fake sem esse parâmetro funcionando (Req 1.5, 10.1).
        if contexto.stealth_efetivo == "habilitada":
            kwargs["stealth"] = True

        relatorio = coletor.coletar(
            peca,
            converter_resultados(ativacao.resultados or ()),
            **kwargs,
        )
        if relatorio.status == "nao_enriquecivel":
            return fazer("nao_enriquecivel", relatorio.motivo)
        return fazer("executada", None, relatorio=relatorio)

    @staticmethod
    def _publicar(
        desfecho: DesfechoIntegracao,
        contexto: ContextoPesquisaWeb,
        trace: TraceSink | None,
    ) -> None:
        """Mensagem única (fora de ``executada``) e evento de trace, cada um com
        proteção própria; falhas são engolidas (Req 5.1, 7.1, 7.7, 8.3, 8.6)."""
        canal = contexto.canal_avisos
        if desfecho.desfecho != "executada" and canal is not None:
            try:
                canal(mensagem_desfecho(desfecho))
            except Exception:  # noqa: BLE001
                pass
        if trace is not None:
            try:
                entrada, saida = evento_trace(desfecho)
                trace.registrar(
                    "tool",
                    NOME_EVENTO_TRACE,
                    status=status_trace(desfecho),
                    duracao_ms=desfecho.duracao_ms,
                    entrada=entrada,
                    saida=saida,
                    justificativa=None,
                )
            except Exception:  # noqa: BLE001
                pass
