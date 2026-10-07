"""Orquestra Fase 1 (particionar_grupo) + Fase 2 (arbitrar_campo, via
montar_decisao_merge) + Fase 3 (gerar_sql) pra um único grupo — usado tanto
pelos scripts de validação manual quanto pela TUI (Fase 4), pra não duplicar
essa sequência em cada lugar que precisa rodar o pipeline completo.

Não é o loop de sessão completo da SPEC.md §6.2 (fila de grupos, clearing de
contexto) — isso é Fase 5. Aqui é só "rodar o pipeline pra um grupo e devolver
o resultado".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Callable

import config
from arbitration.nome import arbitrar_nome_recuperacao
from arbitration.pesquisa_web import ContextoPesquisaWeb, PortaColeta, SinalCancelamento
from arbitration.provedor_informacao import arbitrar_por_provedor
from coleta_paginas.integracao import (
    IntegracaoColeta,
    resolver_coleta_efetiva,
    resolver_stealth_efetivo,
)
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.tracing import TraceSink, sinalizar_grupo, sinalizar_inicio
from memory.models import PedidoIntervencao, RespostaIntervencao
from partitioning.particionar import Particao, particionar_grupo
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from sql_generation.montar_decisao import montar_decisao_merge
from tools.fk_introspection import FkDependency
from tools.group_fetch import RegistroCatalogPart, buscar_grupo as _buscar_grupo_real


@dataclass(frozen=True)
class ResultadoCaso:
    grupo_ref: str
    particao: Particao
    decisoes: list[DecisaoMerge | GrupoSinalizado]
    sql: str
    grupo: list[RegistroCatalogPart] = field(default_factory=list)


def executar_caso(
    search_ref: str,
    brand_id: int,
    llm: LLMProvider,
    dependencias_fk: list[FkDependency],
    rule_store: RuleStore | None = None,
    threshold_divergencia: float = 0.15,
    buscar_grupo=_buscar_grupo_real,
    on_aviso: Callable[[str], None] | None = None,
    verificar_web: Callable | None = None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
    *,
    pesquisa_web: bool = True,
    coleta_html: bool | None = None,
    cancel_event: SinalCancelamento | None = None,
    integracao_coleta: PortaColeta | None = None,
    fallback_stealth: bool | None = None,
) -> ResultadoCaso:
    """Roda o pipeline completo para uma família.

    ``pesquisa_web`` (Opcao_Pesquisa_Web) desliga a verificação web da
    arbitragem de nome; ``coleta_html`` (Opcao_Coleta) força a coleta de páginas
    e, quando ``None``, vale ``ESTAGIARIO_COLETA_HABILITADA``; ``cancel_event``
    é o sinal de cancelamento cooperativo consultado pela coleta;
    ``integracao_coleta`` injeta a porta de coleta (padrão: uma
    ``IntegracaoColeta`` nova por execução, sem I/O na construção).

    ``fallback_stealth`` (Opcao_Stealth) liga ou desliga o fallback stealth da
    coleta e prevalece sobre ``ESTAGIARIO_COLETA_STEALTH_HABILITADA``, inclusive
    quando a chave tem valor inválido; quando ``None``, vale a chave (ausente ou
    vazia → desligado; inválida → desligado com aviso
    ``stealth_configuracao_invalida``). O fallback só é usado quando a coleta é
    de fato chamada e não altera nenhuma decisão do caso.
    """
    # Canal_Avisos da coleta é o on_aviso original, não o wrapper ``avisar``
    # (que também grava observabilidade/aviso no trace — Req 5.4, 8.1).
    contexto_web = ContextoPesquisaWeb(
        pesquisa_habilitada=pesquisa_web,
        coleta_efetiva=resolver_coleta_efetiva(coleta_html, config.coleta_habilitada()),
        cancel_event=cancel_event,
        canal_avisos=on_aviso,
        coleta=integracao_coleta if integracao_coleta is not None else IntegracaoColeta(),
        stealth_efetivo=resolver_stealth_efetivo(
            fallback_stealth, config.coleta_stealth_habilitada()
        ),
    )

    def avisar(mensagem: str) -> None:
        if trace is not None:
            trace.registrar("observabilidade", "aviso", detalhes={"mensagem": mensagem})
        if on_aviso is not None:
            on_aviso(mensagem)

    inicio = perf_counter()
    sinalizar_inicio(trace, "tool", "buscar_grupo", {"search_ref": search_ref, "brand_id": brand_id})
    try:
        grupo = buscar_grupo(search_ref, brand_id)
    except Exception as exc:
        if trace is not None:
            trace.registrar(
                "pipeline", "buscar_grupo", status="erro",
                duracao_ms=(perf_counter() - inicio) * 1000,
                entrada={"search_ref": search_ref, "brand_id": brand_id},
                saida={"erro": type(exc).__name__},
            )
        raise
    if trace is not None:
        trace.registrar(
            "tool", "buscar_grupo",
            duracao_ms=(perf_counter() - inicio) * 1000,
            entrada={"search_ref": search_ref, "brand_id": brand_id},
            saida={"quantidade": len(grupo), "ids": [registro.id for registro in grupo]},
        )

    grupo_ref = f"{search_ref}:{grupo[0].brand}" if grupo else f"{search_ref}:{brand_id}"
    # Só ao vivo (tabela de registros da "Rodar loop"); não entra no trace auditável.
    sinalizar_grupo(trace, grupo_ref, grupo)

    inicio = perf_counter()
    sinalizar_inicio(trace, "tool", "particionar_grupo", {"grupo_ref": grupo_ref, "pecas": len(grupo)})
    try:
        particao = particionar_grupo(
            grupo, llm=llm, rule_store=rule_store, threshold_divergencia=threshold_divergencia
        )
    except Exception as exc:
        if trace is not None:
            trace.registrar(
                "pipeline", "particionar_grupo", status="erro",
                duracao_ms=(perf_counter() - inicio) * 1000,
                entrada={"grupo_ref": grupo_ref, "peca_ids": [registro.id for registro in grupo]},
                saida={"erro": type(exc).__name__},
            )
        raise
    if trace is not None:
        trace.registrar(
            "tool", "particionar_grupo",
            duracao_ms=(perf_counter() - inicio) * 1000,
            entrada={"grupo_ref": grupo_ref, "peca_ids": [registro.id for registro in grupo]},
            saida={
                "subclusters": [
                    {
                        "label": sub.label,
                        "membro_ids": sub.membro_ids,
                        "justificativa": sub.justificativa,
                    }
                    for sub in particao.subclusters
                ],
                "sinais_heuristicos": particao.sinais_heuristicos,
                "regras_aplicaveis": particao.regras_aplicaveis,
            },
            justificativa="; ".join(sub.justificativa for sub in particao.subclusters),
        )

    por_id = {r.id: r for r in grupo}
    subclusters_duplicata = [s for s in particao.subclusters if s.label == "duplicata_real"]
    subclusters_distintos = [
        s for s in particao.subclusters if s.label == "distinto_nao_classificado"
    ]
    decisoes_brutas = []
    for subcluster in subclusters_duplicata:
        inicio = perf_counter()
        sinalizar_inicio(
            trace, "tool", "montar_decisao_merge",
            {"grupo_ref": grupo_ref, "membro_ids": subcluster.membro_ids},
        )
        try:
            decisao = montar_decisao_merge(
                grupo_ref,
                [por_id[i] for i in subcluster.membro_ids],
                llm=llm, rule_store=rule_store, brand_id=brand_id,
                threshold_divergencia=threshold_divergencia,
                on_aviso=avisar, verificar_web=verificar_web,
                pedir_intervencao=pedir_intervencao, limiar_intervencao=limiar_intervencao,
                trace=trace,
                contexto_web=contexto_web,
            )
        except Exception as exc:
            if trace is not None:
                trace.registrar(
                    "pipeline", "montar_decisao_merge", status="erro",
                    duracao_ms=(perf_counter() - inicio) * 1000,
                    entrada={"grupo_ref": grupo_ref, "membro_ids": subcluster.membro_ids},
                    saida={"erro": type(exc).__name__},
                )
            raise
        decisoes_brutas.append(decisao)
        if trace is not None:
            trace.registrar(
                "tool", "montar_decisao_merge",
                duracao_ms=(perf_counter() - inicio) * 1000,
                entrada={"grupo_ref": grupo_ref, "membro_ids": subcluster.membro_ids},
                saida={
                    "tipo": type(decisao).__name__ if decisao is not None else "None",
                    "decisao": decisao,
                },
                justificativa=(getattr(decisao, "motivo", None) if decisao is not None else None),
            )
            if isinstance(decisao, DecisaoMerge):
                for decisao_campo in decisao.decisoes_campo:
                    trace.registrar(
                        "arbitragem", "arbitrar_campo",
                        status="escalado" if decisao_campo.escalado_humano else "ok",
                        entrada={
                            "campo": decisao_campo.campo,
                            "subcluster_ids": subcluster.membro_ids,
                        },
                        saida={
                            "valor": decisao_campo.valor,
                            "fonte": decisao_campo.fonte,
                            "confianca": decisao_campo.confianca,
                            "origem_id": decisao_campo.origem_id,
                            "evidencias": decisao_campo.evidencias,
                        },
                        justificativa=decisao_campo.justificativa,
                    )
                    if decisao_campo.fonte == "verificacao_web":
                        trace.registrar(
                            "tool", "verificar_nomenclatura_peca",
                            saida={
                                "status": "resultado",
                                "evidencias": decisao_campo.evidencias,
                                "valor": decisao_campo.valor,
                            },
                            justificativa=decisao_campo.justificativa,
                        )
                    if decisao_campo.fonte == "intervencao_humana":
                        trace.registrar(
                            "tool", "intervencao_humana",
                            saida={
                                "campo": decisao_campo.campo,
                                "valor": decisao_campo.valor,
                                "evidencias": decisao_campo.evidencias,
                            },
                            justificativa=decisao_campo.justificativa,
                        )
            elif isinstance(decisao, GrupoSinalizado):
                trace.registrar(
                    "decisao", "grupo_sinalizado",
                    status="revisao_manual",
                    saida={"membro_ids": decisao.membro_ids, "motivo": decisao.motivo},
                    justificativa=decisao.motivo,
                )
    distinct_ids: list[int] = []
    distinct_seen: set[int] = set()
    for subcluster in subclusters_distintos:
        for membro_id in subcluster.membro_ids:
            if membro_id not in distinct_seen:
                distinct_seen.add(membro_id)
                distinct_ids.append(membro_id)

    distinct_ids = sorted(distinct_seen)

    if len(distinct_ids) >= 2:
        registros_distintos = [por_id[membro_id] for membro_id in distinct_ids]
        inicio = perf_counter()
        sinalizar_inicio(
            trace, "arbitragem", "recuperar_distintos_name",
            {"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
        )
        try:
            decisao_nome_recuperada = arbitrar_nome_recuperacao(
                registros_distintos,
                llm=llm,
                rule_store=rule_store,
                brand_id=brand_id,
                on_aviso=avisar,
                verificar_web=verificar_web,
                pedir_intervencao=pedir_intervencao,
                limiar_intervencao=limiar_intervencao,
                trace=trace,
                arbitrar_por_provedor=arbitrar_por_provedor,
                contexto_web=contexto_web,
            )
        except Exception as exc:
            if trace is not None:
                trace.registrar(
                    "pipeline", "recuperar_distintos", status="erro",
                    duracao_ms=(perf_counter() - inicio) * 1000,
                    entrada={"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
                    saida={"erro": type(exc).__name__},
                )
            raise

        autorizada = (
            not decisao_nome_recuperada.escalado_humano
            and decisao_nome_recuperada.valor is not None
        )
        if trace is not None:
            trace.registrar(
                "arbitragem", "recuperar_distintos_name",
                status="ok" if autorizada else "escalado",
                duracao_ms=(perf_counter() - inicio) * 1000,
                entrada={"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
                saida={
                    "valor": decisao_nome_recuperada.valor,
                    "fonte": decisao_nome_recuperada.fonte,
                    "confianca": decisao_nome_recuperada.confianca,
                    "escalado_humano": decisao_nome_recuperada.escalado_humano,
                    "origem_id": decisao_nome_recuperada.origem_id,
                    "evidencias": decisao_nome_recuperada.evidencias,
                },
                justificativa=decisao_nome_recuperada.justificativa,
            )

        if autorizada:
            inicio = perf_counter()
            sinalizar_inicio(
                trace, "tool", "montar_decisao_merge",
                {"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
            )
            try:
                decisao = montar_decisao_merge(
                    grupo_ref,
                    registros_distintos,
                    llm=llm,
                    rule_store=rule_store,
                    brand_id=brand_id,
                    threshold_divergencia=threshold_divergencia,
                    on_aviso=avisar,
                    verificar_web=verificar_web,
                    pedir_intervencao=pedir_intervencao,
                    limiar_intervencao=limiar_intervencao,
                    trace=trace,
                    decisoes_campo_precalculadas={"name": decisao_nome_recuperada},
                    contexto_web=contexto_web,
                )
            except Exception as exc:
                if trace is not None:
                    trace.registrar(
                        "pipeline", "montar_decisao_merge", status="erro",
                        duracao_ms=(perf_counter() - inicio) * 1000,
                        entrada={"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
                        saida={"erro": type(exc).__name__},
                    )
                raise
            decisoes_brutas.append(decisao)
            if trace is not None:
                trace.registrar(
                    "tool", "montar_decisao_merge",
                    duracao_ms=(perf_counter() - inicio) * 1000,
                    entrada={"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
                    saida={
                        "tipo": type(decisao).__name__ if decisao is not None else "None",
                        "decisao": decisao,
                    },
                    justificativa=(getattr(decisao, "motivo", None) if decisao is not None else None),
                )
                if isinstance(decisao, DecisaoMerge):
                    for decisao_campo in decisao.decisoes_campo:
                        trace.registrar(
                            "arbitragem", "arbitrar_campo",
                            status="escalado" if decisao_campo.escalado_humano else "ok",
                            entrada={"campo": decisao_campo.campo, "subcluster_ids": distinct_ids},
                            saida={
                                "valor": decisao_campo.valor,
                                "fonte": decisao_campo.fonte,
                                "confianca": decisao_campo.confianca,
                                "origem_id": decisao_campo.origem_id,
                                "evidencias": decisao_campo.evidencias,
                            },
                            justificativa=decisao_campo.justificativa,
                        )
        else:
            motivo = (
                "Recuperação de registros distinto_nao_classificado sem autorização "
                f"para merge (fonte={decisao_nome_recuperada.fonte!r}, "
                f"confianca={decisao_nome_recuperada.confianca!r}). "
                f"{decisao_nome_recuperada.justificativa}"
            )
            decisao = GrupoSinalizado(
                grupo_ref=grupo_ref,
                motivo=motivo,
                membro_ids=distinct_ids,
            )
            decisoes_brutas.append(decisao)
            if trace is not None:
                trace.registrar(
                    "decisao", "grupo_sinalizado",
                    status="revisao_manual",
                    entrada={"grupo_ref": grupo_ref, "membro_ids": distinct_ids},
                    saida={"membro_ids": distinct_ids, "motivo": motivo},
                    justificativa=motivo,
                )

    # Subclusters de um membro não entram como merge; recuperações inconclusivas
    # entram como GrupoSinalizado, que gera apenas comentário de revisão.
    decisoes = [d for d in decisoes_brutas if d is not None]

    inicio = perf_counter()
    sinalizar_inicio(trace, "tool", "gerar_sql", {"grupo_ref": grupo_ref, "decisoes": len(decisoes)})
    sql = gerar_sql(decisoes, dependencias_fk) if decisoes else ""
    if trace is not None:
        trace.registrar(
            "tool", "gerar_sql",
            duracao_ms=(perf_counter() - inicio) * 1000,
            entrada={"grupo_ref": grupo_ref, "decisoes": len(decisoes)},
            saida={"sql": sql, "caracteres": len(sql)},
        )

    return ResultadoCaso(grupo_ref=grupo_ref, particao=particao, decisoes=decisoes, sql=sql, grupo=grupo)
