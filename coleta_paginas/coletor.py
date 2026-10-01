"""Coletor_Paginas: orquestra a coleta de páginas de uma peça (spec html-extract-save).

Ordem fixa de uma chamada a ``ColetorPaginas.coletar`` (Req 1.6, 6.1):

1. validação do Codigo_Peca (``CodigoPecaInvalidoError``);
2. Trava_Entrada: marca sem referência devolve um ``RelatorioColeta``
   ``nao_enriquecivel`` sem erro e sem nenhum efeito — sem classificar, sem
   HTTP, sem ler nem gravar no armazém, mesmo com configuração inválida;
3. resolução de Teto_Aceitos e Janela_Reuso (``ConfiguracaoColetaError``);
4. classificação e seleção (funções puras de ``coleta_paginas.selecao``);
5. processamento sequencial das fontes selecionadas: reuso dentro da janela
   (``registrar_reaproveitamento``) ou busca HTTP seguida de ``gravar_coleta``.

Falhas de busca e de armazenamento de uma fonte viram entrada ``falha`` e a
coleta continua (Req 6.6, 6.13). Só erros de armazenamento são capturados:
exceções genéricas (por exemplo, a guarda de rede dos testes) atravessam.

Armazém, transporte, configuração e relógio são injetados (Req 6.9).

Logging estruturado (Req 9.4): cada entrada do relatório gera um evento
``coleta_paginas.entrada`` e toda chamada que devolve relatório (``executada`` ou
``nao_enriquecivel``) termina com um evento ``coleta_paginas.resumo``, ambos em
``logging.getLogger("coleta_paginas")`` no nível INFO. Os eventos são montados
pelas funções puras ``evento_log`` e ``evento_resumo`` e só contêm os campos
permitidos pelo Req 9.4 — nunca bytes ou trechos da página nem cabeçalhos.
Cada evento vai duas vezes no mesmo ``LogRecord``: como mensagem JSON (legível
por qualquer handler) e como dicionário em ``record.coleta_paginas`` (via
``extra``, para handlers estruturados e testes). A biblioteca não configura
handlers; isso fica com a aplicação.

Cancelamento cooperativo e observador (spec html-extract-on-web-search, Req 6.3,
6.5, 8.1, 8.2, 8.5): ``coletar`` aceita ``cancelado`` (consultado antes de cada
fonte selecionada; com ele verdadeiro, a fonte vira ``falha``/``cancelado`` sem
tocar no armazém nem no transporte) e ``observador`` (recebe cada evento logo
depois do logger, sempre, independente do nível do logger). Sem os dois
parâmetros, o comportamento é idêntico ao do spec html-extract-save.

Fallback stealth (spec stealth-fallback-integration): o construtor aceita o
Fallback_Stealth, a Allowlist_Stealth e a Trava_Navegador como dependências
opcionais (padrões de ``tools.buscador_stealth`` e ``config``), só guardando as
referências — construir o coletor não lê ambiente, não abre navegador e não
faz I/O. Com o fallback ligado, ``evento_log`` acrescenta os Campos_Stealth
(``camada`` e ``motivo_urllib``), ``evento_resumo`` acrescenta
``contagem_camada`` e ``evento_stealth_inicio`` anuncia cada chamada ao
navegador. Com o fallback desligado, os eventos são exatamente os anteriores.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from coleta_paginas.modelos import (
    CAMADA_STEALTH,
    CAMADA_URLLIB,
    MOTIVO_CANCELADO,
    MOTIVO_DOMINIO_EXCLUIDO,
    MOTIVO_NAVEGADOR_INDISPONIVEL,
    CamadaFetch,
    ConfiguracaoColetaError,
    DecisaoFonte,
    EntradaRelatorio,
    ExclusaoUrl,
    FonteSelecionada,
    PecaConsultada,
    RelatorioColeta,
    ResultadoBusca,
)
from coleta_paginas.reputacao import motivo_elegivel_fallback, resultado_tentativa
from coleta_paginas.selecao import (
    classificar_fontes,
    resolver_teto_aceitos,
    selecionar_fontes,
    trava_entrada,
    validar_codigo_peca,
)
from coleta_paginas.url import host_da_url, motivo_recusa_url
from db.armazem_paginas import (
    ArmazemPaginas,
    ConteudoNaoEncontradoError,
    ErroArmazenamento,
    NovoRegistroColeta,
    RegistroColeta,
    normalizar_dominio,
)
from tools.buscador_paginas import BuscadorPaginas, FalhaBusca, PaginaBaixada, TransporteHTTP
from tools.buscador_stealth import (
    TRAVA_NAVEGADOR,
    ColetaStealth,
    TravaNavegador,
    coletar_via_playwright_stealth,
    dominio_na_allowlist,
)

JANELA_REUSO_PADRAO_DIAS = 30
JANELA_REUSO_MINIMA_DIAS = 1
JANELA_REUSO_MAXIMA_DIAS = 365
MOTIVO_ERRO_ARMAZENAMENTO = "erro_armazenamento"

_INTEIRO_DECIMAL = re.compile(r"[0-9]+")

logger = logging.getLogger("coleta_paginas")

EVENTO_ENTRADA = "coleta_paginas.entrada"
EVENTO_RESUMO = "coleta_paginas.resumo"
# Atributo do LogRecord que recebe o dicionário do evento (via ``extra``).
ATRIBUTO_EVENTO = "coleta_paginas"
DESFECHOS = ("armazenado", "reaproveitado", "falha", "url_invalida")

# Campos permitidos pelo Req 9.4, mais o nome do evento.
CAMPOS_EVENTO_ENTRADA = frozenset(
    {
        "evento",
        "codigo_peca",
        "marca_peca",
        "url",
        "dominio",
        "confianca",
        "desfecho",
        "motivo",
        "status_http",
        "hash_conteudo",
    }
)
CAMPOS_EVENTO_RESUMO = frozenset(
    {"evento", "status", "motivo", "codigo_peca", "marca_peca", "contagem"}
)

# Fallback stealth (spec stealth-fallback-integration, Req 8.1–8.5).
EVENTO_STEALTH_INICIO = "coleta_paginas.stealth_inicio"
# Campos_Stealth: só aparecem nos eventos com o fallback ligado para a chamada.
CAMPOS_STEALTH = frozenset({"camada", "motivo_urllib"})
CAMPOS_EVENTO_ENTRADA_STEALTH = CAMPOS_EVENTO_ENTRADA | CAMPOS_STEALTH
CAMPOS_EVENTO_RESUMO_STEALTH = CAMPOS_EVENTO_RESUMO | {"contagem_camada"}
CAMPOS_EVENTO_STEALTH_INICIO = frozenset(
    {"evento", "codigo_peca", "marca_peca", "url", "dominio", "motivo_urllib"}
)
CAMADAS = ("urllib", "playwright_stealth")

# Erros de armazenamento tratados por fonte (Req 6.13). Hash de origem sumido
# entre a consulta de reuso e o vínculo também conta como falha de armazenamento.
_ERROS_ARMAZENAMENTO: tuple[type[BaseException], ...] = (
    ErroArmazenamento,
    ConteudoNaoEncontradoError,
    sqlite3.Error,
)


@dataclass(frozen=True)
class ConfigColeta:
    """Configuração da coleta. Teto e janela chegam brutos; a validação acontece
    em ``coletar``, depois da Trava_Entrada (Req 1.6)."""

    timeout_s: float
    teto_aceitos_ambiente: str | None
    janela_reuso_dias: int | str | None

    @classmethod
    def do_ambiente(cls) -> ConfigColeta:
        """Lê ``ESTAGIARIO_COLETA_*`` via ``config.py`` (Req 9.5)."""
        return cls(
            timeout_s=config.coleta_timeout(),
            teto_aceitos_ambiente=config.coleta_teto_aceitos(),
            janela_reuso_dias=config.coleta_janela_reuso_dias(),
        )


def resolver_janela_reuso(valor: int | str | None) -> int:
    """Janela_Reuso efetiva em dias (Req 6.4, 6.12).

    ``None`` → 30. Aceita ``int`` (nunca ``bool``) ou texto que, após
    ``strip()``, casa com ``[0-9]+``, desde que o valor esteja em [1, 365].
    Qualquer outro valor levanta ``ConfiguracaoColetaError(campo=
    "janela_reuso_dias", valor=repr(valor), origem="ambiente")``.
    """
    if valor is None:
        return JANELA_REUSO_PADRAO_DIAS
    dias: int | None = None
    if isinstance(valor, int) and not isinstance(valor, bool):
        dias = valor
    elif isinstance(valor, str):
        texto = valor.strip()
        # Mais de 3 dígitos significativos já está fora do intervalo. Converte
        # só os dígitos significativos: zeros à esquerda em excesso também
        # estourariam o limite de dígitos de int() (ValueError).
        significativos = texto.lstrip("0")
        if _INTEIRO_DECIMAL.fullmatch(texto) and len(significativos) <= 3:
            dias = int(significativos or "0")
    if dias is None or not JANELA_REUSO_MINIMA_DIAS <= dias <= JANELA_REUSO_MAXIMA_DIAS:
        raise ConfiguracaoColetaError("janela_reuso_dias", repr(valor), "ambiente")
    return dias


def _relogio_padrao() -> datetime:
    return datetime.now(timezone.utc)


def _dominio_exclusao(resultado: ResultadoBusca) -> str:
    """Domínio original ou, se em branco, o host da URL original (pode ser vazio)."""
    if resultado.dominio is not None and resultado.dominio.strip():
        return resultado.dominio
    if resultado.url is None:
        return ""
    return host_da_url(resultado.url)


def evento_log(entrada: EntradaRelatorio, *, stealth: bool = False) -> dict[str, Any]:
    """Evento de log de uma entrada do relatório (Req 9.4). Função pura.

    Sem ``stealth``: exatamente ``CAMPOS_EVENTO_ENTRADA``; ``confianca`` vai
    como texto. Com ``stealth``: ``CAMPOS_EVENTO_ENTRADA_STEALTH``, isto é, os
    mesmos campos mais ``camada`` e ``motivo_urllib`` (ambos podem ser None).
    """
    evento: dict[str, Any] = {
        "evento": EVENTO_ENTRADA,
        "codigo_peca": entrada.codigo_peca,
        "marca_peca": entrada.marca_peca,
        "url": entrada.url,
        "dominio": entrada.dominio,
        "confianca": str(entrada.confianca),
        "desfecho": entrada.desfecho,
        "motivo": entrada.motivo,
        "status_http": entrada.status_http,
        "hash_conteudo": entrada.hash_conteudo,
    }
    if stealth:
        evento["camada"] = entrada.camada
        evento["motivo_urllib"] = entrada.motivo_urllib
    return evento


def evento_resumo(relatorio: RelatorioColeta) -> dict[str, Any]:
    """Evento de resumo de uma coleta (Req 9.4). Função pura.

    ``contagem`` traz todos os desfechos, inclusive os com zero entradas. Com
    ``relatorio.stealth``, acrescenta ``contagem_camada``: o número de entradas
    por Camada_Fetch, para as duas camadas, inclusive zeros (Req 8.5).
    """
    contagem = {desfecho: 0 for desfecho in DESFECHOS}
    for entrada in relatorio.entradas:
        contagem[entrada.desfecho] = contagem.get(entrada.desfecho, 0) + 1
    evento: dict[str, Any] = {
        "evento": EVENTO_RESUMO,
        "status": relatorio.status,
        "motivo": relatorio.motivo,
        "codigo_peca": relatorio.codigo_peca,
        "marca_peca": relatorio.marca_peca,
        "contagem": contagem,
    }
    if relatorio.stealth:
        contagem_camada = {camada: 0 for camada in CAMADAS}
        for entrada in relatorio.entradas:
            if entrada.camada is not None:
                contagem_camada[entrada.camada] = contagem_camada.get(entrada.camada, 0) + 1
        evento["contagem_camada"] = contagem_camada
    return evento


def evento_stealth_inicio(
    peca: PecaConsultada, fonte: FonteSelecionada, motivo_urllib: str | None
) -> dict[str, Any]:
    """Evento emitido imediatamente antes de cada chamada ao Fallback_Stealth
    (Req 8.3). Função pura.

    Contém exatamente ``CAMPOS_EVENTO_STEALTH_INICIO``: nunca corpo, título,
    cookies, cabeçalhos, Content-Type, fuso ou caminho do perfil (Req 8.4).
    """
    return {
        "evento": EVENTO_STEALTH_INICIO,
        "codigo_peca": peca.codigo_peca or "",
        "marca_peca": peca.marca_peca,
        "url": fonte.decisao.resultado.url or "",
        "dominio": fonte.dominio,
        "motivo_urllib": motivo_urllib,
    }


Observador = Callable[[Mapping[str, Any]], None]


@dataclass(frozen=True)
class _ExecucaoStealth:
    """Estado de uma chamada a ``coletar`` com o fallback stealth ligado."""

    allowlist: frozenset[str]
    cancelado: Callable[[], bool] | None
    observador: Observador | None


def _emitir(evento: dict[str, Any], observador: Observador | None = None) -> None:
    """Envia o evento ao logger (como sempre) e depois ao observador, se houver.

    O observador é chamado independente do nível do logger; uma exceção dele
    atravessa ``coletar``. Nenhuma configuração de logging é alterada.
    """
    if logger.isEnabledFor(logging.INFO):
        logger.info(
            "%s",
            json.dumps(evento, ensure_ascii=False, sort_keys=True),
            extra={ATRIBUTO_EVENTO: evento},
        )
    if observador is not None:
        observador(evento)


class ColetorPaginas:
    """Encadeia trava, classificação, seleção, busca e armazenamento (Req 6)."""

    def __init__(
        self,
        armazem: ArmazemPaginas,
        *,
        transporte: TransporteHTTP | None = None,
        config: ConfigColeta | None = None,
        relogio: Callable[[], datetime] | None = None,
        fallback_stealth: ColetaStealth | None = None,
        allowlist_stealth: frozenset[str] | None = None,
        trava_navegador: TravaNavegador | None = None,
    ) -> None:
        """``fallback_stealth`` (padrão ``coletar_via_playwright_stealth``),
        ``allowlist_stealth`` (padrão ``None``: ``config.coleta_dominios_stealth()``
        lida a cada coleta com o fallback ligado) e ``trava_navegador`` (padrão
        ``TRAVA_NAVEGADOR``, do processo) só são guardados aqui; nenhum deles é
        chamado nem lido na construção (Req 12.1)."""
        self._armazem = armazem
        self._config = config if config is not None else ConfigColeta.do_ambiente()
        self._relogio = relogio or _relogio_padrao
        self._buscador = BuscadorPaginas(
            validar_destino=motivo_recusa_url,
            transporte=transporte,
            timeout=self._config.timeout_s,
        )
        self._fallback: ColetaStealth = (
            fallback_stealth if fallback_stealth is not None else coletar_via_playwright_stealth
        )
        self._allowlist_stealth = allowlist_stealth
        self._trava: TravaNavegador = (
            trava_navegador if trava_navegador is not None else TRAVA_NAVEGADOR
        )

    @property
    def config(self) -> ConfigColeta:
        return self._config

    def coletar(
        self,
        peca: PecaConsultada,
        resultados: Sequence[ResultadoBusca],
        *,
        teto_aceitos: int | None = None,
        cancelado: Callable[[], bool] | None = None,
        observador: Observador | None = None,
        stealth: bool = False,
    ) -> RelatorioColeta:
        """Coleta as páginas de uma peça e devolve o relatório (Req 1.6–1.8, 6).

        ``cancelado`` é consultado antes de cada fonte selecionada; quando
        verdadeiro, a fonte vira ``falha``/``cancelado`` sem armazém nem HTTP.
        ``observador`` recebe cada evento de log, na ordem de emissão.

        ``stealth`` é o Stealth_Efetivo da chamada (spec
        stealth-fallback-integration). Com ``False`` (padrão), o fluxo é o
        anterior: sem reputação, sem Tentativa_Fetch, sem allowlist, sem trava e
        sem fallback. Com ``True``, cada fonte não reaproveitada passa pela
        reputação e, se bloqueada na camada urllib e elegível, pelo
        Fallback_Stealth; entradas e eventos levam os Campos_Stealth.
        """
        # (a) Código inválido falha antes de tudo (Req 1.5).
        validar_codigo_peca(peca)
        codigo = peca.codigo_peca
        assert codigo is not None  # garantido por validar_codigo_peca

        # (b) Trava_Entrada: sem efeitos e sem erro, antes da configuração (Req 1.7).
        if trava_entrada(peca):
            return self._finalizar(
                RelatorioColeta(
                    status="nao_enriquecivel",
                    motivo="marca_sem_referencia",
                    codigo_peca=codigo,
                    marca_peca=peca.marca_peca,
                    entradas=(),
                    stealth=stealth,
                ),
                observador,
            )

        # (c) Configuração antes de qualquer HTTP ou gravação (Req 3.9, 6.12).
        teto = resolver_teto_aceitos(teto_aceitos, self._config.teto_aceitos_ambiente)
        janela = resolver_janela_reuso(self._config.janela_reuso_dias)

        decisoes = classificar_fontes(peca, resultados)
        selecao = selecionar_fontes(decisoes, teto)

        execucao: _ExecucaoStealth | None = None
        if stealth:
            allowlist = (
                self._allowlist_stealth
                if self._allowlist_stealth is not None
                else config.coleta_dominios_stealth()
            )
            execucao = _ExecucaoStealth(
                allowlist=allowlist, cancelado=cancelado, observador=observador
            )

        entradas: list[EntradaRelatorio] = []
        for fonte in selecao.selecionadas:
            if cancelado is not None and cancelado():
                entrada = self._entrada_falha(peca, fonte, MOTIVO_CANCELADO, None)
            else:
                entrada = self._processar_fonte(peca, fonte, janela, execucao)
            self._registrar_entrada(entradas, entrada, observador, stealth=stealth)
        for exclusao in selecao.excluidas:
            self._registrar_entrada(
                entradas,
                self._entrada_exclusao(peca, exclusao),
                observador,
                stealth=stealth,
            )

        return self._finalizar(
            RelatorioColeta(
                status="executada",
                motivo=None,
                codigo_peca=codigo,
                marca_peca=peca.marca_peca,
                entradas=tuple(entradas),
                stealth=stealth,
            ),
            observador,
        )

    # -- processamento por fonte ------------------------------------------------

    @staticmethod
    def _finalizar(
        relatorio: RelatorioColeta, observador: Observador | None = None
    ) -> RelatorioColeta:
        """Emite o evento de resumo e devolve o relatório sem alterá-lo."""
        _emitir(evento_resumo(relatorio), observador)
        return relatorio

    def _registrar_entrada(
        self,
        entradas: list[EntradaRelatorio],
        entrada: EntradaRelatorio,
        observador: Observador | None = None,
        *,
        stealth: bool = False,
    ) -> None:
        """Ponto único por onde passa cada entrada do relatório; emite o evento."""
        entradas.append(entrada)
        _emitir(evento_log(entrada, stealth=stealth), observador)

    def _processar_fonte(
        self,
        peca: PecaConsultada,
        fonte: FonteSelecionada,
        janela_dias: int,
        execucao: _ExecucaoStealth | None = None,
    ) -> EntradaRelatorio:
        """Reuso dentro da janela ou busca + gravação; nunca levanta erro de
        armazenamento nem de transporte (Req 6.2, 6.3, 6.5, 6.6, 6.11, 6.13).

        Com ``execucao`` (fallback ligado), a busca segue ``_processar_com_stealth``;
        o reuso vem antes da reputação em qualquer caso (Req 6.3)."""
        agora = self._relogio()
        try:
            origem = self._armazem.buscar_reaproveitavel(
                fonte.url_normalizada, coletado_desde=agora - timedelta(days=janela_dias)
            )
        except _ERROS_ARMAZENAMENTO:
            return self._entrada_falha(peca, fonte, MOTIVO_ERRO_ARMAZENAMENTO, None)

        if origem is not None:
            return self._reaproveitar(peca, fonte, origem)

        url_original = fonte.decisao.resultado.url or ""
        if execucao is not None:
            return self._processar_com_stealth(peca, fonte, url_original, agora, execucao)

        desfecho = self._buscador.buscar(url_original)
        if isinstance(desfecho, FalhaBusca):
            return self._entrada_falha(peca, fonte, desfecho.motivo, desfecho.status_http)
        return self._gravar(peca, fonte, desfecho, agora)

    def _processar_com_stealth(
        self,
        peca: PecaConsultada,
        fonte: FonteSelecionada,
        url: str,
        agora: datetime,
        execucao: _ExecucaoStealth,
    ) -> EntradaRelatorio:
        """Busca de uma fonte não reaproveitada com o fallback ligado.

        1. Reputação das duas camadas, antes de qualquer busca (Req 6.3, 6.4);
           a camada stealth só é consultada se o host está na allowlist.
        2. Camada urllib excluída → fallback direto se a fonte é elegível, senão
           ``falha``/``dominio_excluido`` sem rede (Req 6.1, 6.2). Caso
           contrário, busca urllib, Tentativa_Fetch e, se a falha é bloqueio
           (403/429/503) e a fonte é elegível, fallback (Req 2.1–2.4, 2.7).
        3. Cancelamento antes de abrir o navegador (Req 7.1, 7.2).
        4. Trava_Navegador com prazo igual ao timeout da coleta (Req 7.4, 7.5);
           liberada em ``finally``, inclusive com exceção (Req 7.6, 3.6).
        5. Tentativa_Fetch da camada stealth e desfecho (Req 5.3, 3.3–3.5, 4.1–4.3).
        """
        dominio = normalizar_dominio(fonte.dominio)
        excluida_urllib = self._excluida(dominio, CAMADA_URLLIB)
        elegivel = dominio_na_allowlist(url, execucao.allowlist) and not self._excluida(
            dominio, CAMADA_STEALTH
        )

        camada_anterior: CamadaFetch | None
        motivo_urllib: str
        if excluida_urllib:
            if not elegivel:
                return self._entrada_falha(peca, fonte, MOTIVO_DOMINIO_EXCLUIDO, None)
            camada_anterior, motivo_urllib = None, MOTIVO_DOMINIO_EXCLUIDO
        else:
            desfecho = self._buscador.buscar(url)
            self._registrar_tentativa(dominio, CAMADA_URLLIB, desfecho)
            if isinstance(desfecho, PaginaBaixada):
                return self._gravar(peca, fonte, desfecho, agora, camada=CAMADA_URLLIB)
            if not (elegivel and motivo_elegivel_fallback(desfecho)):
                return self._entrada_falha(
                    peca, fonte, desfecho.motivo, desfecho.status_http, camada=CAMADA_URLLIB
                )
            camada_anterior, motivo_urllib = CAMADA_URLLIB, desfecho.motivo

        if execucao.cancelado is not None and execucao.cancelado():
            return self._entrada_falha(
                peca,
                fonte,
                MOTIVO_CANCELADO,
                None,
                camada=camada_anterior,
                motivo_urllib=motivo_urllib,
            )

        timeout = self._buscador.timeout
        if not self._trava.acquire(timeout=timeout):
            return self._entrada_falha(
                peca,
                fonte,
                MOTIVO_NAVEGADOR_INDISPONIVEL,
                None,
                camada=camada_anterior,
                motivo_urllib=motivo_urllib,
            )
        try:
            _emitir(evento_stealth_inicio(peca, fonte, motivo_urllib), execucao.observador)
            resultado = self._fallback(url, allowlist=execucao.allowlist, timeout=timeout)
        finally:
            self._trava.release()

        self._registrar_tentativa(dominio, CAMADA_STEALTH, resultado)
        if isinstance(resultado, FalhaBusca):
            return self._entrada_falha(
                peca,
                fonte,
                resultado.motivo,
                resultado.status_http,
                camada=CAMADA_STEALTH,
                motivo_urllib=motivo_urllib,
            )
        return self._gravar(
            peca, fonte, resultado, agora, camada=CAMADA_STEALTH, motivo_urllib=motivo_urllib
        )

    def _excluida(self, dominio: str, camada: CamadaFetch) -> bool:
        """Camada_Excluida pela reputação; domínio vazio ou erro de armazenamento
        contam como não excluído (Req 6.4)."""
        if not dominio:
            return False
        try:
            return self._armazem.dominio_excluido(dominio, camada)
        except _ERROS_ARMAZENAMENTO:
            return False

    def _registrar_tentativa(
        self, dominio: str, camada: CamadaFetch, desfecho: PaginaBaixada | FalhaBusca
    ) -> None:
        """Registra a Tentativa_Fetch da busca, se o desfecho entra na reputação
        (Req 5.1–5.4). Erro de armazenamento é ignorado (Req 5.6)."""
        sucesso = resultado_tentativa(desfecho)
        if sucesso is None or not dominio:
            return
        motivo = None if sucesso else desfecho.motivo  # type: ignore[union-attr]
        try:
            self._armazem.registrar_tentativa(
                dominio,
                camada,
                sucesso=sucesso,
                motivo=motivo,
                status_http=desfecho.status_http,
            )
        except _ERROS_ARMAZENAMENTO:
            return

    def _gravar(
        self,
        peca: PecaConsultada,
        fonte: FonteSelecionada,
        pagina: PaginaBaixada,
        agora: datetime,
        *,
        camada: CamadaFetch | None = None,
        motivo_urllib: str | None = None,
    ) -> EntradaRelatorio:
        """Grava a página baixada (por qualquer camada) e monta a entrada
        ``armazenado`` ou ``falha``/``erro_armazenamento`` (Req 4.1–4.3)."""
        registro = self._novo_registro(
            peca,
            fonte,
            url_final=pagina.url_final,
            status_http=pagina.status_http,
            content_type=pagina.content_type,
            charset=pagina.charset,
            coletado_em=agora,
        )
        try:
            hash_conteudo = self._armazem.gravar_coleta(pagina.corpo, registro)
        except _ERROS_ARMAZENAMENTO:
            return self._entrada_falha(
                peca,
                fonte,
                MOTIVO_ERRO_ARMAZENAMENTO,
                pagina.status_http,
                camada=camada,
                motivo_urllib=motivo_urllib,
            )
        return self._entrada(
            peca,
            fonte,
            desfecho="armazenado",
            status_http=pagina.status_http,
            hash_conteudo=hash_conteudo,
            camada=camada,
            motivo_urllib=motivo_urllib,
        )

    def _reaproveitar(
        self, peca: PecaConsultada, fonte: FonteSelecionada, origem: RegistroColeta
    ) -> EntradaRelatorio:
        """Vínculo idempotente com a decisão atual e os dados de fetch da origem,
        para que a Janela_Reuso meça sempre a idade do fetch real."""
        registro = self._novo_registro(
            peca,
            fonte,
            url_final=origem.url_final,
            status_http=origem.status_http,
            content_type=origem.content_type,
            charset=origem.charset,
            coletado_em=origem.coletado_em,
        )
        try:
            vinculo, _criado = self._armazem.registrar_reaproveitamento(
                registro, origem.hash_conteudo
            )
        except _ERROS_ARMAZENAMENTO:
            return self._entrada_falha(peca, fonte, MOTIVO_ERRO_ARMAZENAMENTO, None)
        return self._entrada(
            peca,
            fonte,
            desfecho="reaproveitado",
            status_http=vinculo.status_http,
            hash_conteudo=vinculo.hash_conteudo,
        )

    @staticmethod
    def _novo_registro(
        peca: PecaConsultada,
        fonte: FonteSelecionada,
        *,
        url_final: str,
        status_http: int,
        content_type: str,
        charset: str | None,
        coletado_em: datetime,
    ) -> NovoRegistroColeta:
        decisao: DecisaoFonte = fonte.decisao
        return NovoRegistroColeta(
            codigo_peca=peca.codigo_peca or "",
            marca_peca=peca.marca_peca,
            url_original=decisao.resultado.url or "",
            url_normalizada=fonte.url_normalizada,
            url_final=url_final,
            dominio=fonte.dominio,
            confianca=str(decisao.confianca),
            codigo_confirmado=decisao.codigo_confirmado,
            marca_confirmada=decisao.marca_confirmada,
            nome_reforcado=decisao.nome_reforcado,
            motivo_decisao=decisao.motivo,
            status_http=status_http,
            content_type=content_type,
            charset=charset,
            coletado_em=coletado_em,
        )

    # -- montagem das entradas ----------------------------------------------------

    @staticmethod
    def _entrada(
        peca: PecaConsultada,
        fonte: FonteSelecionada,
        *,
        desfecho: str,
        status_http: int | None,
        hash_conteudo: str | None = None,
        motivo: str | None = None,
        camada: CamadaFetch | None = None,
        motivo_urllib: str | None = None,
    ) -> EntradaRelatorio:
        return EntradaRelatorio(
            codigo_peca=peca.codigo_peca or "",
            marca_peca=peca.marca_peca,
            url=fonte.decisao.resultado.url or "",
            dominio=fonte.dominio,
            confianca=fonte.decisao.confianca,
            desfecho=desfecho,  # type: ignore[arg-type]
            motivo=motivo,
            status_http=status_http,
            hash_conteudo=hash_conteudo,
            camada=camada,
            motivo_urllib=motivo_urllib,
        )

    def _entrada_falha(
        self,
        peca: PecaConsultada,
        fonte: FonteSelecionada,
        motivo: str,
        status_http: int | None,
        *,
        camada: CamadaFetch | None = None,
        motivo_urllib: str | None = None,
    ) -> EntradaRelatorio:
        return self._entrada(
            peca,
            fonte,
            desfecho="falha",
            status_http=status_http,
            motivo=motivo,
            camada=camada,
            motivo_urllib=motivo_urllib,
        )

    @staticmethod
    def _entrada_exclusao(peca: PecaConsultada, exclusao: ExclusaoUrl) -> EntradaRelatorio:
        resultado = exclusao.decisao.resultado
        return EntradaRelatorio(
            codigo_peca=peca.codigo_peca or "",
            marca_peca=peca.marca_peca,
            url=resultado.url or "",
            dominio=_dominio_exclusao(resultado),
            confianca=exclusao.decisao.confianca,
            desfecho="url_invalida",
            motivo=exclusao.motivo,
            status_http=None,
            hash_conteudo=None,
        )
