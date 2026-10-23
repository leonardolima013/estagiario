"""Executor agnóstico de UI do loop de famílias duplicadas.

Memória constante por iteração (docs/auditoria-memoria/RELATORIO.md):
- as famílias vêm de uma fonte que entrega uma por vez (`loop.fila.FilaFamilias`
  por padrão, ou um sorteador injetado), nunca de uma varredura por iteração;
- cada iteração é gravada assim que termina (journal JSONL, SQL em append e
  casos para revisão humana) e depois esvaziada (`RegistroIteracao.aliviar`);
- o JSON público e o JSON de escalados só são gerados no fim;
- as consultas ao Postgres da execução reaproveitam uma conexão
  (`tools.db_query.sessao_banco`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
from time import perf_counter
from typing import Any, Callable, Protocol

from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.escalados import EscritorEscalados
from loop.models import (
    AvisoEmitido,
    FamiliaSorteada,
    IteracaoConcluida,
    IteracaoIniciada,
    IteracaoStatus,
    LoopConfig,
    LoopProgresso,
    LoopStatus,
    MotivoParada,
    RegistroIteracao,
    ResultadoLoop,
    agora_iso,
)
from loop.serializacao import LoopOutputWriter, registro_final_para_json
from loop.sql_saida import EscritorSqlLoop
from loop.tracing import TraceCollector, TracingLLMProvider, envolver_intervencao
from memory.models import PedidoIntervencao, RespostaIntervencao
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.db_query import sessao_banco
from tools.fk_introspection import FkDependency
from tools.sortear_grupo import NenhumGrupoDuplicadoError


class FonteFamilias(Protocol):
    """De onde o loop tira as famílias, uma por iteração."""

    def preparar(self) -> None:
        """Chamado uma vez antes da primeira iteração (pode montar a fila)."""

    def proxima(self) -> FamiliaSorteada | None:
        """A próxima família, ou `None` quando acabaram."""

    def concluir(
        self, familia: FamiliaSorteada, status: IteracaoStatus, erro: dict | None = None
    ) -> None:
        """Desfecho da família entregue por `proxima`."""

    def encerrar(self) -> None:
        """Fim da execução (devolve o que ficou em andamento)."""


# Família repetida devolvida por um sorteador injetado: a iteração é pulada.
_REPETIDA = object()


class _FonteSorteio:
    """Adapta um sorteador `sortear_fn(excluir=...)` (testes e usos antigos).

    Guarda as famílias já entregues e as passa como `excluir`, como o loop
    fazia antes da fila.
    """

    nome_evento = "sortear_grupo_aleatorio"

    def __init__(self, sortear_fn: Callable[..., Any]) -> None:
        self._sortear = sortear_fn
        self._vistas: set[tuple[str, int]] = set()

    def preparar(self) -> None:
        return None

    def proxima(self) -> FamiliaSorteada | None | object:
        try:
            familia = self._sortear(excluir=self._vistas)
        except NenhumGrupoDuplicadoError:
            return None
        if not isinstance(familia, FamiliaSorteada):
            familia = FamiliaSorteada(familia.search_ref, familia.brand_id, familia.brand)
        if familia.chave in self._vistas:
            # Defesa contra um sorteador customizado incorreto: não conta nem executa
            # uma família repetida; tenta a próxima iteração sem corromper o conjunto.
            return _REPETIDA
        self._vistas.add(familia.chave)
        return familia

    def entrada_evento(self) -> dict[str, Any]:
        return {"familias_excluidas": len(self._vistas)}

    def concluir(self, familia, status, erro=None) -> None:
        return None

    def encerrar(self) -> None:
        return None


def _run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")


def _erro_dict(exc: Exception) -> dict[str, str]:
    mensagem = str(exc).replace("\x00", "")
    if len(mensagem) > 1000:
        mensagem = mensagem[:1000] + "…"
    return {"tipo": type(exc).__name__, "mensagem": mensagem}


def _emitir(callback, progresso: LoopProgresso) -> None:
    if callback is None:
        return
    try:
        callback(progresso)
    except Exception:
        # A UI não pode transformar uma falha de renderização em falha da iteração.
        pass


def _protegido(callback: Callable[[object], None] | None) -> Callable[[object], None] | None:
    """Versão do callback ao vivo que nunca levanta exceção (mesma regra de `_emitir`)."""
    if callback is None:
        return None

    def _chamar(evento: object) -> None:
        try:
            callback(evento)
        except Exception:  # noqa: BLE001
            pass

    return _chamar


def _contar_decisoes(iteracao: RegistroIteracao) -> tuple[int, int]:
    decisoes = iteracao.resultado_caso.decisoes if iteracao.resultado_caso else []
    merges = sum(isinstance(d, DecisaoMerge) for d in decisoes)
    sinalizados = sum(isinstance(d, GrupoSinalizado) for d in decisoes)
    return merges, sinalizados


def _registros_finais(iteracao: RegistroIteracao) -> tuple[dict[str, Any], ...]:
    """`registro_final` de cada merge da iteração, o mesmo que o JSON grava.

    Só alimenta o painel ao vivo; uma falha aqui vira tupla vazia, nunca erro
    da iteração.
    """
    decisoes = iteracao.resultado_caso.decisoes if iteracao.resultado_caso else []
    try:
        return tuple(
            registro_final_para_json(decisao, iteracao.grupo)
            for decisao in decisoes
            if isinstance(decisao, DecisaoMerge)
        )
    except Exception:  # noqa: BLE001
        return ()


def _fonte_padrao() -> FonteFamilias:
    from loop.fila import FilaFamilias

    return FilaFamilias()


def executar_loop(
    config: LoopConfig,
    llm: LLMProvider,
    dependencias_fk: list[FkDependency],
    *,
    rule_store: RuleStore | None = None,
    sortear_fn=None,
    fonte_familias: FonteFamilias | None = None,
    executar_caso_fn=None,
    buscar_grupo=None,
    on_progresso: Callable[[LoopProgresso], None] | None = None,
    on_aviso: Callable[[str], None] | None = None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    cancel_event: Event | None = None,
    on_evento_ao_vivo: Callable[[object], None] | None = None,
    pesquisa_web: bool = True,
    coleta_html: bool | None = None,
    fallback_stealth: bool = False,
    verificar_web: Callable | None = None,
    reputacao_dominios: bool = True,
) -> ResultadoLoop:
    """Executa até N famílias, sem repetir família e sem aplicar SQL.

    As famílias vêm de `fonte_familias`; sem ela, de `sortear_fn(excluir=...)`
    quando informado; sem nenhum dos dois, da fila local (`loop.fila`), montada
    na primeira execução e consumida da menor família para a maior.

    ``on_evento_ao_vivo`` recebe, na thread do loop, os eventos de
    `loop.models.EventoAoVivo`: início e fim de cada família, cada evento do
    trace no momento do registro, os sinais de etapa e de resposta parcial do
    LLM, os registros da família (`GrupoCarregado`) e uma cópia de cada aviso.
    O fim da família (`IteracaoConcluida`) traz o registro final de cada merge.
    Nada disso muda o JSON nem o SQL do loop, e uma falha do callback é
    ignorada.

    Opções repassadas a cada caso: ``pesquisa_web``, ``coleta_html`` (``None``
    = variável de ambiente), ``fallback_stealth`` (desligado por padrão, como a
    tela "Rodar loop" sempre fez) e ``verificar_web`` (``None`` = método do
    `.env`). ``reputacao_dominios`` (ligado) faz a coleta parar de tentar
    domínios cujas 3 últimas buscas falharam, mesmo sem o stealth. Sem
    ``pedir_intervencao``, a execução é não interativa: o que
    pediria a modal fica escalado e vai para `output/escalados_humano/`.
    """
    if executar_caso_fn is None:
        from pipeline import executar_caso as executar_caso_fn_real
        executar_caso_fn = executar_caso_fn_real
    if fonte_familias is not None:
        fonte = fonte_familias
    elif sortear_fn is not None:
        fonte = _FonteSorteio(sortear_fn)
    else:
        fonte = _fonte_padrao()
    cancel_event = cancel_event or Event()
    ao_vivo = _protegido(on_evento_ao_vivo)
    aviso_caso = on_aviso
    if ao_vivo is not None:
        def aviso_caso(mensagem: str) -> None:
            ao_vivo(AvisoEmitido(timestamp=agora_iso(), mensagem=mensagem))
            if on_aviso is not None:
                on_aviso(mensagem)

    def emitir_ao_vivo(evento: object) -> None:
        if ao_vivo is not None:
            ao_vivo(evento)

    # Só as opções que diferem do padrão do caso: um `executar_caso_fn` que já
    # fixa alguma delas (testes) continua recebendo os mesmos kwargs de antes.
    opcoes_caso: dict[str, Any] = {"fallback_stealth": fallback_stealth}
    if not pesquisa_web:
        opcoes_caso["pesquisa_web"] = False
    if coleta_html is not None:
        opcoes_caso["coleta_html"] = coleta_html
    if verificar_web is not None:
        opcoes_caso["verificar_web"] = verificar_web
    if reputacao_dominios:
        opcoes_caso["reputacao_dominios"] = True

    resultado = ResultadoLoop(
        run_id=_run_id(),
        status=LoopStatus.EM_ANDAMENTO,
        motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS,
        iteracoes_solicitadas=config.iteracoes,
        iniciada_em=agora_iso(),
    )
    writer = LoopOutputWriter(config.output_dir, resultado.run_id)
    escritor_sql = EscritorSqlLoop(writer.caminho_sql, config.iteracoes)
    escritor_escalados = EscritorEscalados(config.output_dir, resultado.run_id)
    resultado.caminho_json = writer.caminho_json
    resultado.caminho_sql = writer.caminho_sql
    contadores = {"concluidas": 0, "erros": 0, "sinalizadas": 0}

    def progresso(fase: str, indice: int, mensagem: str, familia: FamiliaSorteada | None = None, **extra) -> None:
        _emitir(
            on_progresso,
            LoopProgresso(
                total=config.iteracoes, indice_atual=indice, fase=fase, mensagem=mensagem,
                familia=familia, concluidas=contadores["concluidas"], erros=contadores["erros"],
                **extra,
            ),
        )

    def gravar(iteracao: RegistroIteracao) -> None:
        """Grava a iteração em todas as saídas e a esvazia."""
        resultado.iteracoes.append(iteracao)
        writer.registrar_iteracao(resultado, iteracao)
        escritor_sql.registrar(iteracao)
        resultado.casos_escalados += escritor_escalados.registrar(iteracao)
        if iteracao.status == IteracaoStatus.SUCESSO:
            contadores["concluidas"] += 1
        elif iteracao.status == IteracaoStatus.ERRO:
            contadores["erros"] += 1

    def _executar() -> None:
        progresso("preparando", 0, "Preparando a fila de famílias...")
        try:
            fonte.preparar()
        except Exception as exc:  # noqa: BLE001 — sem fila não há o que iterar
            agora = agora_iso()
            iteracao = RegistroIteracao(
                indice=1, status=IteracaoStatus.ERRO, iniciada_em=agora, finalizada_em=agora,
                erro=_erro_dict(exc),
            )
            gravar(iteracao)
            iteracao.aliviar()
            resultado.status = LoopStatus.FINALIZADO_COM_ERROS
            resultado.motivo_parada = MotivoParada.ERRO_FATAL
            return

        indice = 0
        while indice < config.iteracoes:
            if cancel_event.is_set():
                resultado.status = LoopStatus.CANCELADO
                resultado.motivo_parada = MotivoParada.CANCELADO_PELO_USUARIO
                return

            progresso("sorteando", indice + 1, "Buscando a próxima família...")
            try:
                familia = fonte.proxima()
            except Exception as exc:
                indice += 1
                agora = agora_iso()
                iteracao = RegistroIteracao(
                    indice=indice,
                    status=IteracaoStatus.ERRO,
                    iniciada_em=agora,
                    finalizada_em=agora,
                    erro=_erro_dict(exc),
                )
                gravar(iteracao)
                iteracao.aliviar()
                emitir_ao_vivo(
                    IteracaoConcluida(
                        timestamp=agora_iso(), indice=indice, total=config.iteracoes,
                        status=iteracao.status, erro=iteracao.erro,
                    )
                )
                progresso("erro", indice, f"Erro ao obter família: {type(exc).__name__}")
                continue

            if familia is None:
                resultado.status = (
                    LoopStatus.FINALIZADO_COM_ERROS if contadores["erros"] else LoopStatus.PARCIAL
                )
                resultado.motivo_parada = MotivoParada.FAMILIAS_DUPLICADAS_ESGOTADAS
                return
            if familia is _REPETIDA:
                continue

            indice += 1
            inicio_iteracao = perf_counter()
            emitir_ao_vivo(
                IteracaoIniciada(
                    timestamp=agora_iso(), indice=indice, total=config.iteracoes, familia=familia,
                )
            )
            trace = TraceCollector(ouvinte=ao_vivo)
            entrada_evento = getattr(fonte, "entrada_evento", None)
            trace.registrar(
                "loop", getattr(fonte, "nome_evento", "proxima_familia"),
                entrada=entrada_evento() if callable(entrada_evento) else {"origem": "fila"},
                saida={"search_ref": familia.search_ref, "brand_id": familia.brand_id, "brand": familia.brand},
            )
            iteracao = RegistroIteracao(
                indice=indice,
                status=IteracaoStatus.SUCESSO,
                iniciada_em=agora_iso(),
                familia=familia,
            )
            progresso("executando", indice, "Executando pipeline...", familia)
            pedir_intervencao_trace = envolver_intervencao(pedir_intervencao, trace)

            llm_iteracao = TracingLLMProvider(llm, trace)
            terminou = False
            try:
                kwargs = {
                    "rule_store": rule_store,
                    "on_aviso": aviso_caso,
                    "pedir_intervencao": pedir_intervencao_trace,
                    "trace": trace,
                    # Sinal_Cancelamento repassado ao caso (Req 6.6): a coleta de páginas
                    # consulta o mesmo evento que o loop verifica entre iterações.
                    "cancel_event": cancel_event,
                    **opcoes_caso,
                }
                if buscar_grupo is not None:
                    kwargs["buscar_grupo"] = buscar_grupo
                caso = executar_caso_fn(
                    familia.search_ref, familia.brand_id, llm_iteracao, dependencias_fk, **kwargs
                )
                iteracao.grupo = list(caso.grupo)
                iteracao.resultado_caso = caso
                iteracao.sql = caso.sql
                iteracao.eventos = trace.eventos()
                terminou = True
            except Exception as exc:
                terminou = True
                iteracao.status = IteracaoStatus.CANCELADA if cancel_event.is_set() else IteracaoStatus.ERRO
                iteracao.erro = _erro_dict(exc)
                trace.registrar(
                    "loop", "executar_caso", status="cancelado" if cancel_event.is_set() else "erro",
                    detalhes={"tipo": type(exc).__name__},
                )
                iteracao.eventos = trace.eventos()
            finally:
                if not terminou:
                    # Interrompida por algo que não é Exception (Ctrl+C, SystemExit):
                    # nunca grava como sucesso; a família volta para a fila.
                    iteracao.status = IteracaoStatus.CANCELADA
                    iteracao.erro = {"tipo": "Interrompida", "mensagem": "execução interrompida no meio da família"}
                    iteracao.eventos = trace.eventos()
                iteracao.finalizada_em = agora_iso()
                merges, sinalizados = _contar_decisoes(iteracao)
                if sinalizados:
                    # Sinalização é resultado auditável, não erro: a iteração segue sucesso.
                    contadores["sinalizadas"] += 1
                registros_finais = _registros_finais(iteracao) if ao_vivo is not None else ()
                pecas = len(iteracao.grupo)
                gravar(iteracao)
                fonte.concluir(familia, iteracao.status, iteracao.erro)
                iteracao.aliviar()
                if ao_vivo is not None:
                    emitir_ao_vivo(
                        IteracaoConcluida(
                            timestamp=agora_iso(), indice=indice, total=config.iteracoes,
                            status=iteracao.status, familia=familia, pecas=pecas,
                            merges=merges, sinalizados=sinalizados,
                            duracao_ms=(perf_counter() - inicio_iteracao) * 1000,
                            erro=iteracao.erro,
                            registros_finais=registros_finais,
                        )
                    )
                progresso(
                    "checkpoint", indice, "Iteração gravada.", familia,
                    sinalizadas=contadores["sinalizadas"],
                )

            if cancel_event.is_set():
                resultado.status = LoopStatus.CANCELADO
                resultado.motivo_parada = MotivoParada.CANCELADO_PELO_USUARIO
                return

        resultado.status = (
            LoopStatus.FINALIZADO_COM_ERROS if contadores["erros"] else LoopStatus.CONCLUIDO
        )
        resultado.motivo_parada = MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS

    try:
        with sessao_banco():
            _executar()
    finally:
        try:
            fonte.encerrar()
        finally:
            if resultado.status == LoopStatus.EM_ANDAMENTO:
                resultado.status = (
                    LoopStatus.FINALIZADO_COM_ERROS if contadores["erros"] else LoopStatus.PARCIAL
                )
            resultado.finalizada_em = agora_iso()
            writer.finalizar(resultado)
            escritor_sql.finalizar()
            resultado.caminho_escalados = escritor_escalados.finalizar()

    progresso(
        "finalizado", resultado.iteracoes_executadas, f"Loop finalizado: {resultado.status.value}.",
        sinalizadas=contadores["sinalizadas"],
        cancelando=resultado.status == LoopStatus.CANCELADO,
    )
    return resultado
