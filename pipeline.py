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

from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.tracing import TraceSink
from memory.models import PedidoIntervencao, RespostaIntervencao
from partitioning.particionar import Particao, particionar_grupo
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from sql_generation.montar_decisao import montar_decisao_merge
from tools.fk_introspection import FkDependency
from tools.group_fetch import RegistroCatalogPart, buscar_grupo as _buscar_grupo_real
from verification.mcp_playwright_agent import verificar_nomenclatura_peca


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
    verificar_web: Callable = verificar_nomenclatura_peca,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
) -> ResultadoCaso:
    def avisar(mensagem: str) -> None:
        if trace is not None:
            trace.registrar("observabilidade", "aviso", detalhes={"mensagem": mensagem})
        if on_aviso is not None:
            on_aviso(mensagem)

    inicio = perf_counter()
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

    inicio = perf_counter()
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
    decisoes_brutas = []
    for subcluster in subclusters_duplicata:
        inicio = perf_counter()
        try:
            decisao = montar_decisao_merge(
                grupo_ref,
                [por_id[i] for i in subcluster.membro_ids],
                llm=llm, rule_store=rule_store, brand_id=brand_id,
                threshold_divergencia=threshold_divergencia,
                on_aviso=avisar, verificar_web=verificar_web,
                pedir_intervencao=pedir_intervencao, limiar_intervencao=limiar_intervencao,
                trace=trace,
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
    # subcluster de 1 membro -> None (nada pra mesclar); não entra no SQL.
    decisoes = [d for d in decisoes_brutas if d is not None]

    inicio = perf_counter()
    sql = gerar_sql(decisoes, dependencias_fk) if decisoes else ""
    if trace is not None:
        trace.registrar(
            "tool", "gerar_sql",
            duracao_ms=(perf_counter() - inicio) * 1000,
            entrada={"grupo_ref": grupo_ref, "decisoes": len(decisoes)},
            saida={"sql": sql, "caracteres": len(sql)},
        )

    return ResultadoCaso(grupo_ref=grupo_ref, particao=particao, decisoes=decisoes, sql=sql, grupo=grupo)
