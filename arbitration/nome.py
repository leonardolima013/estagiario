"""Juiz de qualidade textual pro campo name (SPEC.md §4.2) — sub-tarefa dedicada,
não reaproveita a cascata de confiabilidade dos campos numéricos.
"""

from __future__ import annotations

from typing import Callable

from arbitration.models import DecisaoCampo
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.tracing import TraceSink
from memory.intervencao import registrar_resposta_intervencao
from memory.models import PedidoIntervencao, RespostaIntervencao
from memory.recuperador import consultar_intervencao
from tools.group_fetch import RegistroCatalogPart
from verification.divergencia import nomes_normalizados_divergem, tokens_normalizados
from verification.mcp_playwright_agent import verificar_nomenclatura_peca

_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "modo": {"type": "string", "enum": ["escolher", "sintetizar"]},
        "part_id_escolhido": {"type": "integer"},
        "nome_sintetizado": {"type": "string"},
        "justificativa": {"type": "string"},
    },
    "required": ["modo", "justificativa"],
}

_SYSTEM_PROMPT = """\
Você é o Estagiário, um assistente de dados que escolhe ou sintetiza o \
nome mais completo e bem escrito entre nomes candidatos de uma mesma peça \
de autopeças duplicada no catálogo. Prefira escolher um nome já existente \
quando ele já for bom o suficiente; só sintetize um novo nome quando nenhum \
candidato for satisfatório sozinho. Siga as convenções de estilo aprendidas \
abaixo, se houver.
"""


class ArbitragemInvalidaError(ValueError):
    """A resposta do modelo não forma uma decisão de nome válida."""


def _decisao_nome_da_resposta(
    resposta: dict,
    registros: list[RegistroCatalogPart],
    *,
    fonte: str = "julgamento_modelo",
    prefixo_justificativa: str = "",
) -> DecisaoCampo:
    modo = resposta.get("modo")
    justificativa = str(resposta.get("justificativa") or "")
    if prefixo_justificativa:
        justificativa = f"{prefixo_justificativa} {justificativa}".strip()

    if modo == "escolher":
        ids_validos = {r.id for r in registros}
        part_id = resposta.get("part_id_escolhido")
        if part_id not in ids_validos:
            raise ArbitragemInvalidaError(
                f"Modelo escolheu part_id {part_id!r}, fora do subcluster {sorted(ids_validos)}"
            )
        registro_escolhido = next(r for r in registros if r.id == part_id)
        return DecisaoCampo(
            campo="name", valor=registro_escolhido.name, justificativa=justificativa,
            fonte=fonte, origem_id=registro_escolhido.id,
        )

    if modo == "sintetizar":
        nome_sintetizado = resposta.get("nome_sintetizado")
        if not isinstance(nome_sintetizado, str) or not nome_sintetizado.strip():
            raise ArbitragemInvalidaError("modo='sintetizar' sem nome_sintetizado.")
        return DecisaoCampo(
            campo="name", valor=nome_sintetizado.strip(), justificativa=justificativa,
            fonte=fonte,
        )

    raise ArbitragemInvalidaError(f"modo inválido: {modo!r}")


def _decidir_nome_com_regra(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    regra,
    score: float | None = None,
) -> DecisaoCampo:
    linhas = [f"- id={r.id}: {r.name!r}" for r in registros]
    score_texto = f"score={score:.3f}" if score is not None else "confirmada nesta intervenção"
    prompt = (
        "A verificação web não foi suficiente, mas uma intervenção humana anterior "
        "ensinou a regra abaixo. Use-a como contexto para decidir este novo caso, "
        "sem copiar códigos ou marcas do caso antigo.\n\n"
        f"Regra recuperada ({score_texto}):\n"
        f"Título: {regra.titulo or '(sem título)'}\n"
        f"Condição: {regra.condicao}\n"
        f"Resolução: {regra.resolucao}\n\n"
        "Nomes candidatos atuais:\n" + "\n".join(linhas) +
        "\n\nEscolha o melhor nome ou sintetize um novo seguindo a regra."
    )
    resposta = llm.gerar_json(
        system=_SYSTEM_PROMPT,
        user=prompt,
        json_schema=_JSON_SCHEMA,
        schema_name="decisao_nome",
    )
    return _decisao_nome_da_resposta(
        resposta, registros, fonte="intervencao_humana",
        prefixo_justificativa=f"Regra de intervenção recuperada (score {score:.3f})" if score is not None else "Regra de intervenção confirmada",
    )


def arbitrar_nome(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None = None,
    on_aviso: Callable[[str], None] | None = None,
    verificar_web: Callable = verificar_nomenclatura_peca,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
) -> DecisaoCampo:
    if nomes_normalizados_divergem([r.name for r in registros]):
        return _arbitrar_nome_via_web(
            registros, llm, rule_store, on_aviso, verificar_web,
            pedir_intervencao, limiar_intervencao, trace,
        )

    regras = rule_store.consultar("qualidade_nome") if rule_store else []
    contexto_regras = (
        "\n".join(f"- {r.condicao} => {r.resolucao}" for r in regras) if regras else ""
    )

    linhas = [f"- id={r.id}: {r.name!r}" for r in registros]
    prompt = "Nomes candidatos:\n" + "\n".join(linhas)
    if contexto_regras:
        prompt += f"\n\nConvenções de estilo aprendidas em sessões anteriores:\n{contexto_regras}"
    prompt += "\n\nEscolha o melhor nome ou sintetize um novo."

    resposta = llm.gerar_json(
        system=_SYSTEM_PROMPT, user=prompt, json_schema=_JSON_SCHEMA, schema_name="decisao_nome"
    )

    return _decisao_nome_da_resposta(resposta, registros)


def _arbitrar_nome_via_web(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None,
    on_aviso: Callable[[str], None] | None,
    verificar_web: Callable,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None,
    limiar_intervencao: float,
    trace: TraceSink | None,
) -> DecisaoCampo:
    """Resolve divergência de nomes: web -> memória -> humano -> escalonamento."""
    codigo, marca = registros[0].search_ref, registros[0].brand
    nomes_conflitantes = sorted({r.name for r in registros})

    if on_aviso:
        on_aviso(
            f"Nomes divergentes para {codigo} ({marca}): {nomes_conflitantes} — "
            "acionando verificação web antes de decidir."
        )

    resultado = verificar_web(codigo, marca, nomes_conflitantes, on_evento=on_aviso)

    if trace is not None:
        trace.registrar(
            "tool", "verificar_nomenclatura_peca",
            entrada={"codigo": codigo, "marca": marca, "nomes_conflitantes": nomes_conflitantes},
            saida={
                "status": resultado.status,
                "nome_sugerido": resultado.nome_sugerido,
                "fontes": resultado.fontes,
            },
            justificativa=resultado.justificativa,
        )

    if on_aviso:
        on_aviso(
            f"Verificação web concluída: status={resultado.status}, "
            f"nome_sugerido={resultado.nome_sugerido!r}"
        )

    if resultado.status == "confirmado":
        # Casa o nome sugerido a um candidato por tokens normalizados (acento/caixa/ordem
        # tolerante) — igualdade exata quase nunca bate (a web devolve acentuado/reordenado),
        # deixando origem_id=None à toa e quebrando a cor-por-id/rastreabilidade na TUI (F-07).
        tokens_sugerido = tokens_normalizados(resultado.nome_sugerido or "")
        origem_id = next(
            (r.id for r in registros if tokens_normalizados(r.name) == tokens_sugerido),
            None,
        )
        justificativa = resultado.justificativa
        if resultado.fontes:
            justificativa += " (fontes: " + ", ".join(f.url for f in resultado.fontes) + ")"
        return DecisaoCampo(
            campo="name", valor=resultado.nome_sugerido, justificativa=justificativa,
            fonte="verificacao_web", origem_id=origem_id, confianca="alta",
            evidencias=[
                {"tipo": "web", "url": fonte.url, "nome_encontrado": fonte.nome_encontrado}
                for fonte in resultado.fontes
            ],
        )

    motivo = resultado.justificativa or "Verificação web inconclusiva — sem confirmação clara das fontes."
    contexto_web = "; ".join(
        [
            f"status={resultado.status}",
            motivo,
            *(
                f"{fonte.nome_encontrado} ({fonte.url})"
                for fonte in resultado.fontes
            ),
        ]
    )
    pedido = PedidoIntervencao(
        ponto="nome",
        grupo_ref=f"{codigo}:{marca}",
        search_ref=codigo,
        marca=marca,
        nomes_conflitantes=nomes_conflitantes,
        motivo=motivo,
        contexto_web=contexto_web,
        membro_ids=[r.id for r in registros],
        candidatos=[(r.id, r.name) for r in registros],
    )

    # Primeiro tenta uma regra já aprendida. A aplicação é automática, mas o LLM
    # ainda valida qual nome atual satisfaz a regra — a regra é contexto, não um
    # valor literal de outro código/peça.
    if rule_store is not None:
        recuperada = consultar_intervencao(
            pedido, rule_store, campo="nome", limiar=limiar_intervencao
        )
        if trace is not None:
            trace.registrar(
                "memoria", "consultar_memoria",
                entrada={"ponto": "nome", "grupo_ref": pedido.grupo_ref, "texto_busca": pedido.texto_busca()},
                saida={
                    "encontrada": recuperada is not None,
                    "score": recuperada.score if recuperada else None,
                    "regra": recuperada.regra.titulo if recuperada else None,
                },
                justificativa=(recuperada.regra.resolucao if recuperada else "Nenhuma regra acima do limiar."),
            )
        if recuperada is not None:
            if on_aviso:
                on_aviso(
                    f"Memória de intervenção recuperada automaticamente para nomes "
                    f"(score={recuperada.score:.3f}, regra={recuperada.regra.titulo!r})."
                )
            return _decidir_nome_com_regra(
                registros, llm, recuperada.regra, score=recuperada.score
            )

    # Sem memória aplicável, só pausa quando o caller oferece a ponte humana. Em
    # execução headless/testes sem callback, preserva o fallback antigo.
    if pedir_intervencao is not None:
        resposta = pedir_intervencao(pedido)
        if not isinstance(resposta, RespostaIntervencao):
            raise TypeError("pedir_intervencao deve retornar RespostaIntervencao")
        if rule_store is not None:
            regra = registrar_resposta_intervencao(rule_store, pedido, resposta)
            if on_aviso:
                on_aviso(f"Intervenção de nome salva: regra={regra.titulo!r}, id={regra.id}.")

        if resposta.valor is not None and str(resposta.valor).strip():
            return DecisaoCampo(
                campo="name",
                valor=str(resposta.valor).strip(),
                justificativa="Nome decidido por intervenção humana após verificação web inconclusiva.",
                fonte="intervencao_humana",
                origem_id=resposta.origem_id,
                confianca="alta",
                evidencias=[{"tipo": "intervencao_humana", "regra_confirmada": resposta.regra.titulo}],
            )
        return _decidir_nome_com_regra(registros, llm, resposta.regra)

    return DecisaoCampo(
        campo="name", valor=None, justificativa=motivo,
        fonte="verificacao_web", escalado_humano=True, confianca="baixa",
        evidencias=[
            {"tipo": "web", "url": fonte.url, "nome_encontrado": fonte.nome_encontrado}
            for fonte in resultado.fontes
        ],
    )
