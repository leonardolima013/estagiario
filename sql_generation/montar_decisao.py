"""Monta a decisão de merge de um subcluster duplicata_real (SPEC.md §7, Fase 3):
escolhe o vencedor, roda a arbitragem de campo (Fase 2) pra cada campo suportado,
e sinaliza pra revisão humana em vez de gerar merge quando os candidatos têm
similarity_id (agrupamento de duplicatas já existente no sistema) conflitantes.
"""

from __future__ import annotations

from typing import Callable

from arbitration.arbitrar import CAMPOS_SUPORTADOS, arbitrar_campo
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.tracing import TraceSink
from memory.intervencao import registrar_resposta_intervencao
from memory.models import PedidoIntervencao, RespostaIntervencao
from memory.recuperador import consultar_intervencao
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from sql_generation.vencedor import escolher_vencedor
from tools.group_fetch import RegistroCatalogPart
from verification.mcp_playwright_agent import verificar_nomenclatura_peca

_JSON_SCHEMA_SIMILARITY = {
    "type": "object",
    "properties": {
        "acao": {"type": "string", "enum": ["permitir_merge", "manter_sinalizado"]},
        "confianca": {"type": "string", "enum": ["alta", "baixa"]},
        "justificativa": {"type": "string"},
    },
    "required": ["acao", "confianca", "justificativa"],
}


def _pedido_similarity(grupo_ref: str, registros_subcluster: list[RegistroCatalogPart], detalhe: str) -> PedidoIntervencao:
    return PedidoIntervencao(
        ponto="particionamento",
        grupo_ref=grupo_ref,
        search_ref=registros_subcluster[0].search_ref,
        marca=registros_subcluster[0].brand,
        nomes_conflitantes=sorted({r.name for r in registros_subcluster}),
        motivo=(
            "similarity_id conflitantes: agrupamentos pré-existentes divergentes. "
            f"{detalhe}"
        ),
        membro_ids=[r.id for r in registros_subcluster],
        candidatos=[(r.id, r.name) for r in registros_subcluster],
    )


def _avaliar_regra_similarity(
    registros_subcluster: list[RegistroCatalogPart],
    llm: LLMProvider,
    regra,
    score: float | None,
) -> tuple[str | None, bool, str]:
    linhas = "\n".join(
        f"- id={r.id}, similarity_id={r.similarity_id}, nome={r.name!r}"
        for r in registros_subcluster
    )
    score_texto = f"{score:.3f}" if score is not None else "confirmada"
    resposta = llm.gerar_json(
        system=(
            "Você decide se uma regra de intervenção permite superar um conflito "
            "de similarity_id. Só permita merge com confiança alta e quando a regra "
            "explicar que os registros são o mesmo produto; caso contrário mantenha "
            "o grupo sinalizado."
        ),
        user=(
            f"Regra recuperada (score {score_texto}):\n"
            f"Título: {regra.titulo or '(sem título)'}\n"
            f"Condição: {regra.condicao}\nResolução: {regra.resolucao}\n\n"
            f"Registros atuais:\n{linhas}\n\n"
            "Escolha permitir_merge ou manter_sinalizado."
        ),
        json_schema=_JSON_SCHEMA_SIMILARITY,
        schema_name="decisao_similarity_id",
    )
    acao = resposta.get("acao")
    confianca = resposta.get("confianca")
    justificativa = str(resposta.get("justificativa") or "")
    if acao not in {"permitir_merge", "manter_sinalizado"}:
        return None, False, justificativa
    return acao, confianca == "alta", justificativa


def montar_decisao_merge(
    grupo_ref: str,
    registros_subcluster: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None,
    brand_id: int,
    threshold_divergencia: float = 0.15,
    on_aviso: Callable[[str], None] | None = None,
    verificar_web: Callable = verificar_nomenclatura_peca,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
) -> DecisaoMerge | GrupoSinalizado | None:
    """Retorna None quando não há nada pra mesclar (subcluster de 1 membro) — não é
    um erro: a Fase 1 às vezes rotula um item isolado como duplicata_real mesmo sem
    ter ninguém pra duplicar com ele. Um subcluster vazio, por outro lado, indica bug
    upstream (o schema da Fase 1 exige pelo menos 1 membro) e levanta erro.
    """
    if not registros_subcluster:
        raise ValueError(f"subcluster duplicata_real vazio: {grupo_ref!r}")

    if len(registros_subcluster) == 1:
        return None

    similarity_ids = {r.similarity_id for r in registros_subcluster if r.similarity_id is not None}
    if len(similarity_ids) > 1:
        detalhe = ", ".join(f"id={r.id}:similarity_id={r.similarity_id}" for r in registros_subcluster)
        motivo_base = (
            f"candidatos têm similarity_id conflitantes ({sorted(similarity_ids)}) — "
            f"agrupamentos pré-existentes divergentes, merge automático não é seguro ({detalhe})."
        )
        pedido = _pedido_similarity(grupo_ref, registros_subcluster, detalhe)
        motivo_extra = ""
        regra_decidiu_manter = False

        if rule_store is not None:
            recuperada = consultar_intervencao(
                pedido, rule_store, campo="particionamento", limiar=limiar_intervencao
            )
            if trace is not None:
                trace.registrar(
                    "memoria", "consultar_memoria",
                    entrada={"ponto": "particionamento", "grupo_ref": pedido.grupo_ref, "texto_busca": pedido.texto_busca()},
                    saida={
                        "encontrada": recuperada is not None,
                        "score": recuperada.score if recuperada else None,
                        "regra": recuperada.regra.titulo if recuperada else None,
                    },
                    justificativa=(recuperada.regra.resolucao if recuperada else "Nenhuma regra acima do limiar."),
                )
            if recuperada is not None:
                acao, confianca_alta, justificativa = _avaliar_regra_similarity(
                    registros_subcluster, llm, recuperada.regra, recuperada.score
                )
                motivo_extra = (
                    f" Regra recuperada (score={recuperada.score:.3f}): {justificativa}"
                )
                if on_aviso:
                    on_aviso(
                        f"Memória de particionamento recuperada (score={recuperada.score:.3f}, "
                        f"regra={recuperada.regra.titulo!r}, ação={acao!r}, "
                        f"confiança_alta={confianca_alta})."
                    )
                if confianca_alta and acao == "manter_sinalizado":
                    regra_decidiu_manter = True
                elif confianca_alta and acao == "permitir_merge":
                    similarity_ids = set()

        if similarity_ids and not regra_decidiu_manter and pedir_intervencao is not None:
            resposta = pedir_intervencao(pedido)
            if not isinstance(resposta, RespostaIntervencao):
                raise TypeError("pedir_intervencao deve retornar RespostaIntervencao")
            if rule_store is not None:
                regra = registrar_resposta_intervencao(rule_store, pedido, resposta)
                motivo_extra += f" Intervenção salva: regra={regra.titulo!r}, id={regra.id}."
            if resposta.acao == "permitir_merge":
                similarity_ids = set()
                motivo_extra += " Operador permitiu o merge."
            else:
                motivo_extra += " Operador manteve o grupo sinalizado."

        if similarity_ids:
            return GrupoSinalizado(
                grupo_ref=grupo_ref,
                motivo=motivo_base + motivo_extra,
                membro_ids=[r.id for r in registros_subcluster],
            )

    vencedor = escolher_vencedor(registros_subcluster)
    perdedor_ids = [r.id for r in registros_subcluster if r.id != vencedor.id]

    decisoes_campo = [
        arbitrar_campo(
            registros_subcluster, campo, llm, rule_store, brand_id, threshold_divergencia,
            on_aviso=on_aviso, verificar_web=verificar_web,
            pedir_intervencao=pedir_intervencao, limiar_intervencao=limiar_intervencao,
            trace=trace,
        )
        for campo in sorted(CAMPOS_SUPORTADOS)
    ]

    # Snapshot dos valores atuais do vencedor pros campos decididos — gerar_sql usa
    # pra emitir no UPDATE só o que de fato muda (Q-02=a).
    valores_atuais_vencedor = {dc.campo: getattr(vencedor, dc.campo, None) for dc in decisoes_campo}

    return DecisaoMerge(
        grupo_ref=grupo_ref,
        vencedor_id=vencedor.id,
        perdedor_ids=perdedor_ids,
        decisoes_campo=decisoes_campo,
        valores_atuais_vencedor=valores_atuais_vencedor,
    )
