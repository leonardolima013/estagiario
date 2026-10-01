"""Executor agnóstico de UI do loop de famílias duplicadas."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Event
from typing import Callable

from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.models import (
    FamiliaSorteada,
    IteracaoStatus,
    LoopConfig,
    LoopProgresso,
    LoopStatus,
    MotivoParada,
    RegistroIteracao,
    ResultadoLoop,
    agora_iso,
)
from loop.serializacao import LoopOutputWriter
from loop.sql_saida import gerar_sql_loop, salvar_sql_atomico
from loop.tracing import TraceCollector, TracingLLMProvider
from memory.models import PedidoIntervencao, RespostaIntervencao
from sql_generation.models import GrupoSinalizado
from tools.fk_introspection import FkDependency
from tools.sortear_grupo import NenhumGrupoDuplicadoError, sortear_grupo_aleatorio


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


def executar_loop(
    config: LoopConfig,
    llm: LLMProvider,
    dependencias_fk: list[FkDependency],
    *,
    rule_store: RuleStore | None = None,
    sortear_fn=sortear_grupo_aleatorio,
    executar_caso_fn=None,
    buscar_grupo=None,
    on_progresso: Callable[[LoopProgresso], None] | None = None,
    on_aviso: Callable[[str], None] | None = None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    cancel_event: Event | None = None,
) -> ResultadoLoop:
    """Executa até N tentativas, sem repetir família e sem aplicar SQL."""
    if executar_caso_fn is None:
        from pipeline import executar_caso as executar_caso_fn_real
        executar_caso_fn = executar_caso_fn_real
    cancel_event = cancel_event or Event()
    resultado = ResultadoLoop(
        run_id=_run_id(),
        status=LoopStatus.EM_ANDAMENTO,
        motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS,
        iteracoes_solicitadas=config.iteracoes,
        iniciada_em=agora_iso(),
    )
    writer = LoopOutputWriter(config.output_dir, resultado.run_id)
    resultado.caminho_json = writer.caminho_json
    resultado.caminho_sql = writer.caminho_sql
    writer.checkpoint(resultado)

    vistas: set[tuple[str, int]] = set()
    indice = 0
    while indice < config.iteracoes:
        if cancel_event.is_set():
            resultado.status = LoopStatus.CANCELADO
            resultado.motivo_parada = MotivoParada.CANCELADO_PELO_USUARIO
            break

        _emitir(
            on_progresso,
            LoopProgresso(
                total=config.iteracoes, indice_atual=indice + 1, fase="sorteando",
                mensagem="Sorteando família duplicada...",
                concluidas=resultado.iteracoes_concluidas,
                erros=resultado.iteracoes_com_erro,
            ),
        )
        try:
            familia = sortear_fn(excluir=vistas)
        except NenhumGrupoDuplicadoError:
            resultado.status = (
                LoopStatus.FINALIZADO_COM_ERROS
                if resultado.iteracoes_com_erro else LoopStatus.PARCIAL
            )
            resultado.motivo_parada = MotivoParada.FAMILIAS_DUPLICADAS_ESGOTADAS
            break
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
            resultado.iteracoes.append(iteracao)
            writer.registrar_iteracao(resultado, iteracao)
            salvar_sql_atomico(writer.caminho_sql, gerar_sql_loop(resultado.iteracoes, config.iteracoes))
            _emitir(
                on_progresso,
                LoopProgresso(
                    total=config.iteracoes, indice_atual=indice, fase="erro",
                    mensagem=f"Erro ao sortear família: {type(exc).__name__}",
                    concluidas=resultado.iteracoes_concluidas,
                    erros=resultado.iteracoes_com_erro,
                ),
            )
            continue

        if not isinstance(familia, FamiliaSorteada):
            familia = FamiliaSorteada(familia.search_ref, familia.brand_id, familia.brand)
        if familia.chave in vistas:
            # Defesa contra um sorteador customizado incorreto: não conta nem executa
            # uma família repetida; tenta a próxima iteração sem corromper o conjunto.
            continue
        vistas.add(familia.chave)
        indice += 1
        trace = TraceCollector()
        trace.registrar(
            "loop", "sortear_grupo_aleatorio",
            entrada={"familias_excluidas": len(vistas)},
            saida={"search_ref": familia.search_ref, "brand_id": familia.brand_id, "brand": familia.brand},
        )
        iniciada = agora_iso()
        iteracao = RegistroIteracao(
            indice=indice,
            status=IteracaoStatus.SUCESSO,
            iniciada_em=iniciada,
            familia=familia,
        )
        _emitir(
            on_progresso,
            LoopProgresso(
                total=config.iteracoes, indice_atual=indice, fase="executando",
                familia=familia, mensagem="Executando pipeline...",
                concluidas=resultado.iteracoes_concluidas,
                erros=resultado.iteracoes_com_erro,
            ),
        )
        def pedir_intervencao_trace(pedido):
            if pedir_intervencao is None:
                return None
            resposta = pedir_intervencao(pedido)
            trace.registrar(
                "intervencao", "intervencao_humana",
                entrada={
                    "ponto": pedido.ponto,
                    "grupo_ref": pedido.grupo_ref,
                    "nomes_conflitantes": pedido.nomes_conflitantes,
                    "motivo": pedido.motivo,
                },
                saida={
                    "resposta_humana": resposta.resposta_humana,
                    "regra": resposta.regra,
                    "valor": resposta.valor,
                    "origem_id": resposta.origem_id,
                    "acao": resposta.acao,
                },
                justificativa=resposta.resposta_humana,
            )
            return resposta

        llm_iteracao = TracingLLMProvider(llm, trace)
        try:
            kwargs = {
                "rule_store": rule_store,
                "on_aviso": on_aviso,
                "pedir_intervencao": pedir_intervencao_trace if pedir_intervencao is not None else None,
                "trace": trace,
                # Sinal_Cancelamento repassado ao caso (Req 6.6): a coleta de páginas
                # consulta o mesmo evento que o loop verifica entre iterações.
                "cancel_event": cancel_event,
                # Decisão Q2 (Req 1.7): o loop nunca liga o fallback stealth,
                # mesmo com a Chave_Stealth habilitada.
                "fallback_stealth": False,
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
            if any(isinstance(decisao, GrupoSinalizado) for decisao in caso.decisoes):
                # Mantém sucesso da iteração: sinalização é resultado auditável, não erro.
                pass
        except Exception as exc:
            iteracao.status = IteracaoStatus.CANCELADA if cancel_event.is_set() else IteracaoStatus.ERRO
            iteracao.erro = _erro_dict(exc)
            trace.registrar(
                "loop", "executar_caso", status="cancelado" if cancel_event.is_set() else "erro",
                detalhes={"tipo": type(exc).__name__},
            )
            iteracao.eventos = trace.eventos()
        finally:
            iteracao.finalizada_em = agora_iso()
            resultado.iteracoes.append(iteracao)
            writer.registrar_iteracao(resultado, iteracao)
            salvar_sql_atomico(writer.caminho_sql, gerar_sql_loop(resultado.iteracoes, config.iteracoes))
            _emitir(
                on_progresso,
                LoopProgresso(
                    total=config.iteracoes, indice_atual=indice, fase="checkpoint",
                    familia=familia,
                    mensagem="Checkpoint salvo.",
                    concluidas=resultado.iteracoes_concluidas,
                    erros=resultado.iteracoes_com_erro,
                    sinalizadas=sum(
                        any(isinstance(d, GrupoSinalizado) for d in (i.resultado_caso.decisoes if i.resultado_caso else []))
                        for i in resultado.iteracoes
                    ),
                ),
            )

        if cancel_event.is_set():
            resultado.status = LoopStatus.CANCELADO
            resultado.motivo_parada = MotivoParada.CANCELADO_PELO_USUARIO
            break

    else:
        resultado.status = (
            LoopStatus.FINALIZADO_COM_ERROS
            if resultado.iteracoes_com_erro else LoopStatus.CONCLUIDO
        )
        resultado.motivo_parada = MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS

    if resultado.status == LoopStatus.EM_ANDAMENTO:
        resultado.status = (
            LoopStatus.FINALIZADO_COM_ERROS
            if resultado.iteracoes_com_erro else LoopStatus.PARCIAL
        )
    resultado.finalizada_em = agora_iso()
    writer.finalizar(resultado)
    salvar_sql_atomico(writer.caminho_sql, gerar_sql_loop(resultado.iteracoes, config.iteracoes))
    _emitir(
        on_progresso,
        LoopProgresso(
            total=config.iteracoes, indice_atual=resultado.iteracoes_executadas,
            fase="finalizado", mensagem=f"Loop finalizado: {resultado.status.value}.",
            concluidas=resultado.iteracoes_concluidas,
            erros=resultado.iteracoes_com_erro,
            cancelando=resultado.status == LoopStatus.CANCELADO,
        ),
    )
    return resultado
