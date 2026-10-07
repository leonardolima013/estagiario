"""Juiz de qualidade textual pro campo name (SPEC.md §4.2) — sub-tarefa dedicada,
não reaproveita a cascata de confiabilidade dos campos numéricos.
"""

from __future__ import annotations

import dataclasses
import functools
from dataclasses import dataclass
from typing import Callable

import config
from arbitration.models import DecisaoCampo
from arbitration.pesquisa_web import (
    CONTEXTO_WEB_DESLIGADA,
    JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
    JUSTIFICATIVA_INTERVENCAO_DESLIGADA,
    MOTIVO_PESQUISA_DESLIGADA,
    AtivacaoPesquisa,
    ContextoPesquisaWeb,
    MetodoVerificador,
)
from arbitration.provedor_informacao import arbitrar_por_provedor
from db.rule_store import RuleStore
from llm.provider import LLMProvider
from loop.tracing import TraceSink, sinalizar_inicio
from memory.intervencao import registrar_resposta_intervencao
from memory.models import PedidoIntervencao, RespostaIntervencao
from memory.recuperador import consultar_intervencao
from tools.group_fetch import RegistroCatalogPart
from verification.divergencia import nomes_normalizados_divergem, tokens_normalizados
from verification.models import ResultadoVerificacao
from verification.resultados_estruturados import invocar_verificador
from verification.selector import normalizar_metodo, resolver_verificacao_web

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
    *,
    pesquisa_web_desligada: bool = False,
) -> DecisaoCampo:
    linhas = [f"- id={r.id}: {r.name!r}" for r in registros]
    score_texto = f"score={score:.3f}" if score is not None else "confirmada nesta intervenção"
    # Com a pesquisa desligada pelo operador, o prompt não pode afirmar que houve
    # verificação web (Req 10.11). Sem a flag, o texto é o anterior, byte a byte.
    abertura = (
        "A pesquisa web foi desligada pelo operador nesta execução; uma intervenção "
        "humana anterior ensinou a regra abaixo. "
        if pesquisa_web_desligada
        else "A verificação web não foi suficiente, mas uma intervenção humana anterior "
        "ensinou a regra abaixo. "
    )
    prompt = (
        abertura +
        "Use-a como contexto para decidir este novo caso, "
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


def provedor_nome_autoriza(decisao: DecisaoCampo | None) -> bool:
    """Retorna se uma decisão de provedor pode encerrar a arbitragem de ``name``.

    O atalho é deliberadamente restrito à fonte de confiabilidade alta da marca:
    consenso continua sendo tratado separadamente pelo caminho normal, enquanto
    qualquer resultado de confiabilidade menor precisa passar pela verificação
    web quando o nome diverge.
    """
    return bool(
        decisao is not None
        and decisao.fonte == "regra_confiabilidade"
        and decisao.confianca == "alta"
        and decisao.valor is not None
        and not decisao.escalado_humano
    )


def decisao_nome_da_verificacao_web(
    registros: list[RegistroCatalogPart],
    resultado: ResultadoVerificacao,
) -> DecisaoCampo | None:
    """Converte uma confirmação web em decisão auditável de ``name``.

    ``None`` significa que a resposta não trouxe um nome utilizável — inclusive
    quando o verificador marcou o resultado como confirmado mas retornou apenas
    espaços. O helper é compartilhado pelo caminho normal e pela recuperação de
    registros distintos, evitando chamadas e construções de evidência duplicadas.
    """
    if resultado.status != "confirmado":
        return None
    if not isinstance(resultado.nome_sugerido, str) or not resultado.nome_sugerido.strip():
        return None

    nome_sugerido = resultado.nome_sugerido.strip()
    tokens_sugerido = tokens_normalizados(nome_sugerido)
    origem_id = next(
        (r.id for r in registros if tokens_normalizados(r.name) == tokens_sugerido),
        None,
    )
    justificativa = resultado.justificativa or "Nome confirmado pela verificação web."
    if resultado.fontes:
        justificativa += " (fontes: " + ", ".join(f.url for f in resultado.fontes) + ")"
    return DecisaoCampo(
        campo="name",
        valor=nome_sugerido,
        justificativa=justificativa,
        fonte="verificacao_web",
        origem_id=origem_id,
        confianca="alta",
        evidencias=[
            {"tipo": "web", "url": fonte.url, "nome_encontrado": fonte.nome_encontrado}
            for fonte in resultado.fontes
        ],
    )


def _pesquisa_habilitada(contexto_web: ContextoPesquisaWeb | None) -> bool:
    """`contexto_web=None` equivale ao comportamento anterior: pesquisa ligada."""
    return contexto_web is None or contexto_web.pesquisa_habilitada


def _resolver_verificador(
    verificar_web: Callable | None,
) -> tuple[Callable, MetodoVerificador]:
    """Resolve o Verificador_Web e o método reportado à coleta.

    Injetado → ``(verificar_web, "injetado")``. Senão, o chamável do seletor e o
    método normalizado de ``ESTAGIARIO_WEB_VERIFICATION_METODO``. Método inválido
    levanta ``MetodoVerificacaoInvalidoError`` como antes. Só deve ser chamado
    com a pesquisa web habilitada.
    """
    if verificar_web is not None:
        return verificar_web, "injetado"
    chamavel = resolver_verificacao_web()
    metodo = normalizar_metodo(config.web_verification_metodo())
    return chamavel, metodo  # type: ignore[return-value]  # serper | playwright


def _arbitrar_nome_recuperacao(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None = None,
    brand_id: int | None = None,
    on_aviso: Callable[[str], None] | None = None,
    verificar_web: Callable | None = None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
    arbitrar_por_provedor=arbitrar_por_provedor,
    *,
    contexto_web: ContextoPesquisaWeb | None = None,
) -> DecisaoCampo:
    """Aplica o gate provider→web para a recuperação de partes distintas.

    Ao contrário da arbitragem normal, a recuperação deve consultar a web para
    qualquer resultado que não seja o gate exato de confiabilidade alta, mesmo
    quando os nomes são textualmente convergentes. A cascata de memória,
    intervenção e escalonamento é reutilizada por ``_arbitrar_nome_via_web``.
    """
    if not registros:
        raise ValueError("conjunto de recuperação vazio")
    if len(registros) == 1:
        registro = registros[0]
        return DecisaoCampo(
            campo="name",
            valor=registro.name,
            justificativa="Conjunto de recuperação com um único registro.",
            fonte="sem_conflito",
        )
    if brand_id is None:
        raise ValueError("brand_id é obrigatório para a recuperação por provedor")

    try:
        decisao_provedor = arbitrar_por_provedor(registros, "name", brand_id)
    except Exception as exc:  # noqa: BLE001 — degradação controlada para a web
        if on_aviso:
            on_aviso(f"Tool_Provedor_Informacao inconclusiva por erro em 'name': {exc}")
        decisao_provedor = None
        if trace is not None:
            trace.registrar(
                "tool", "arbitrar_por_provedor", status="erro",
                entrada={"campo": "name", "membro_ids": [r.id for r in registros]},
                saida={"erro": type(exc).__name__},
                justificativa="Provider inconclusivo; recuperação seguirá para a web.",
            )
    else:
        if trace is not None:
            trace.registrar(
                "tool", "arbitrar_por_provedor",
                entrada={"campo": "name", "membro_ids": [r.id for r in registros]},
                saida=(
                    {
                        "valor": decisao_provedor.valor,
                        "fonte": decisao_provedor.fonte,
                        "confianca": decisao_provedor.confianca,
                        "escalado_humano": decisao_provedor.escalado_humano,
                        "origem_id": decisao_provedor.origem_id,
                        "evidencias": decisao_provedor.evidencias,
                    }
                    if decisao_provedor is not None
                    else {"resultado": None}
                ),
                justificativa=(
                    decisao_provedor.justificativa if decisao_provedor is not None
                    else "Provider não retornou decisão."
                ),
            )

    if provedor_nome_autoriza(decisao_provedor):
        return decisao_provedor

    metodo: MetodoVerificador | None = None
    if _pesquisa_habilitada(contexto_web):
        verificar_web, metodo = _resolver_verificador(verificar_web)
    else:
        verificar_web = None
    return _arbitrar_nome_via_web(
        registros, llm, rule_store, on_aviso, verificar_web,
        pedir_intervencao, limiar_intervencao, trace,
        metodo=metodo, contexto_web=contexto_web,
    )


def _arbitrar_nome(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None = None,
    brand_id: int | None = None,
    on_aviso: Callable[[str], None] | None = None,
    verificar_web: Callable | None = None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None = None,
    limiar_intervencao: float = 0.45,
    trace: TraceSink | None = None,
    arbitrar_por_provedor=arbitrar_por_provedor,
    *,
    contexto_web: ContextoPesquisaWeb | None = None,
) -> DecisaoCampo:
    # O seletor é resolvido somente quando a cascata realmente precisar da
    # verificação web; assim consenso, alta confiança e nomes não divergentes não
    # importam/criam uma skill de web desnecessariamente.

    # 1º desempate por confiabilidade de fonte, antes de qualquer outra estratégia
    # de nome (juiz textual, verificação web ou intervenção humana) — Requirement 10.
    # Só quando há brand_id: ele é necessário pra resolver o Provedor_Da_Marca. Com
    # brand_id=None (chamadas legadas), o 1º desempate é pulado e a cascata atual roda
    # como antes (R10.1). Erro em runtime da tool é degradação controlada: tratado como
    # Arbitragem_Inconclusiva, com aviso opcional via on_aviso, e a cascata prossegue.
    if brand_id is not None:
        try:
            decisao_provedor = arbitrar_por_provedor(registros, "name", brand_id)
        except Exception as exc:  # noqa: BLE001 — degradação controlada p/ a cascata de nome
            if on_aviso:
                on_aviso(f"Tool_Provedor_Informacao inconclusiva por erro em 'name': {exc}")
            decisao_provedor = None
        if decisao_provedor is not None:
            # sem_conflito (único / consenso, R10.5) continua sendo final sem web.
            if decisao_provedor.fonte == "sem_conflito":
                return decisao_provedor
            # Somente o gate exato da fabricante/brand evita a web (R10.2).
            if provedor_nome_autoriza(decisao_provedor):
                return decisao_provedor
            # Arbitragem abaixo de alta/inconclusiva cai na cascata existente.

    if nomes_normalizados_divergem([r.name for r in registros]):
        metodo: MetodoVerificador | None = None
        if _pesquisa_habilitada(contexto_web):
            verificar_web, metodo = _resolver_verificador(verificar_web)
        else:
            verificar_web = None
        return _arbitrar_nome_via_web(
            registros, llm, rule_store, on_aviso, verificar_web,
            pedir_intervencao, limiar_intervencao, trace,
            metodo=metodo, contexto_web=contexto_web,
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
    verificar_web: Callable | None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None,
    limiar_intervencao: float,
    trace: TraceSink | None,
    *,
    metodo: MetodoVerificador | None = None,
    contexto_web: ContextoPesquisaWeb | None = None,
) -> DecisaoCampo:
    """Resolve divergência de nomes: web -> coleta -> memória -> humano -> escalonamento.

    Com ``contexto_web=None`` o comportamento é o anterior à coleta: pesquisa
    ligada e nenhuma porta de coleta. A coleta roda depois do aviso de conclusão
    da verificação e antes da decisão/cascata; seu retorno é ignorado, então não
    altera a decisão (Req 5.2, 6.1).
    """
    if not _pesquisa_habilitada(contexto_web):
        return _arbitrar_nome_pesquisa_desligada(
            registros, llm, rule_store, on_aviso, pedir_intervencao,
            limiar_intervencao, trace, contexto_web=contexto_web,
        )
    if verificar_web is None:
        raise TypeError("verificar_web é obrigatório com a pesquisa web habilitada")

    codigo, marca = registros[0].search_ref, registros[0].brand
    nomes_conflitantes = sorted({r.name for r in registros})

    if on_aviso:
        on_aviso(
            f"Nomes divergentes para {codigo} ({marca}): {nomes_conflitantes} — "
            "acionando verificação web antes de decidir."
        )

    sinalizar_inicio(
        trace, "tool", "verificar_nomenclatura_peca",
        {"codigo": codigo, "marca": marca, "nomes_conflitantes": nomes_conflitantes},
    )
    chamada = invocar_verificador(
        verificar_web, codigo, marca, nomes_conflitantes, on_evento=on_aviso
    )
    resultado = chamada.resultado

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

    if contexto_web is not None and contexto_web.coleta is not None:
        ativacao = AtivacaoPesquisa(
            codigo=codigo,
            marca=marca,
            nomes_conflitantes=tuple(nomes_conflitantes),
            metodo=metodo or "injetado",
            situacao=chamada.situacao,
            resultados=(
                chamada.resultados_pesquisa if chamada.situacao == "realizada" else None
            ),
        )
        # A porta nunca levanta exceção; o desfecho é publicado por ela mesma.
        sinalizar_inicio(trace, "tool", "coletar_paginas", {"codigo": codigo, "marca": marca})
        contexto_web.coleta.processar(ativacao, contexto=contexto_web, trace=trace)

    decisao_web = decisao_nome_da_verificacao_web(registros, resultado)
    if decisao_web is not None:
        return decisao_web

    if resultado.status == "confirmado":
        motivo = (
            resultado.justificativa
            or "Verificação web confirmou a consulta, mas não retornou um nome utilizável."
        )
        if resultado.nome_sugerido is None or not str(resultado.nome_sugerido).strip():
            motivo += " Nome sugerido vazio; tratando como inconclusivo."
    else:
        motivo = resultado.justificativa or "Verificação web inconclusiva — sem confirmação clara das fontes."
    texto_contexto_web = "; ".join(
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
        contexto_web=texto_contexto_web,
        membro_ids=[r.id for r in registros],
        candidatos=[(r.id, r.name) for r in registros],
    )

    def _escalonamento_ligada() -> DecisaoCampo:
        return DecisaoCampo(
            campo="name", valor=None, justificativa=motivo,
            fonte="verificacao_web", escalado_humano=True, confianca="baixa",
            evidencias=[
                {"tipo": "web", "url": fonte.url, "nome_encontrado": fonte.nome_encontrado}
                for fonte in resultado.fontes
            ],
        )

    return _cascata_nome(
        registros, llm, rule_store, on_aviso, pedido, pedir_intervencao,
        limiar_intervencao, trace,
        textos=_TextosCascata(
            justificativa_intervencao=_JUSTIFICATIVA_INTERVENCAO_LIGADA,
            escalonamento=_escalonamento_ligada,
        ),
    )


_JUSTIFICATIVA_INTERVENCAO_LIGADA = (
    "Nome decidido por intervenção humana após verificação web inconclusiva."
)


def _arbitrar_nome_pesquisa_desligada(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None,
    on_aviso: Callable[[str], None] | None,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None,
    limiar_intervencao: float,
    trace: TraceSink | None,
    *,
    contexto_web: ContextoPesquisaWeb,
) -> DecisaoCampo:
    """Ativacao_Pesquisa com a pesquisa web desligada pelo operador (Req 10).

    Não resolve nem chama o Verificador_Web — ``ESTAGIARIO_WEB_VERIFICATION_METODO``
    é ignorado. Sequência: aviso único → trace ``desligada`` → porta de coleta
    (``nao_executada/pesquisa_desligada``) → cascata memória/intervenção/escalonamento
    com textos que não afirmam verificação web.
    """
    codigo, marca = registros[0].search_ref, registros[0].brand
    nomes_conflitantes = sorted({r.name for r in registros})

    if on_aviso:
        on_aviso(
            f"Nomes divergentes para {codigo} ({marca}): {nomes_conflitantes} — "
            "pesquisa web desligada pelo operador; seguindo para memória/intervenção."
        )

    if trace is not None:
        trace.registrar(
            "tool", "verificar_nomenclatura_peca",
            status="desligada",
            entrada={"codigo": codigo, "marca": marca, "nomes_conflitantes": nomes_conflitantes},
            saida={"motivo": "pesquisa_desligada"},
            justificativa=MOTIVO_PESQUISA_DESLIGADA,
        )

    if contexto_web.coleta is not None:
        ativacao = AtivacaoPesquisa(
            codigo=codigo,
            marca=marca,
            nomes_conflitantes=tuple(nomes_conflitantes),
            metodo="desligada",
            situacao="desligada",
            resultados=None,
        )
        # A porta nunca levanta exceção; publica nao_executada/pesquisa_desligada.
        contexto_web.coleta.processar(ativacao, contexto=contexto_web, trace=trace)

    pedido = PedidoIntervencao(
        ponto="nome",
        grupo_ref=f"{codigo}:{marca}",
        search_ref=codigo,
        marca=marca,
        nomes_conflitantes=nomes_conflitantes,
        motivo=MOTIVO_PESQUISA_DESLIGADA,
        contexto_web=CONTEXTO_WEB_DESLIGADA,
        membro_ids=[r.id for r in registros],
        candidatos=[(r.id, r.name) for r in registros],
    )

    def _escalonamento_desligada() -> DecisaoCampo:
        return DecisaoCampo(
            campo="name", valor=None,
            justificativa=JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
            fonte="escalado_humano", escalado_humano=True, confianca="baixa",
            evidencias=[{"tipo": "pesquisa_web_desligada"}],
        )

    return _cascata_nome(
        registros, llm, rule_store, on_aviso, pedido, pedir_intervencao,
        limiar_intervencao, trace,
        textos=_TextosCascata(
            justificativa_intervencao=JUSTIFICATIVA_INTERVENCAO_DESLIGADA,
            escalonamento=_escalonamento_desligada,
            pesquisa_web_desligada=True,
        ),
    )


@dataclass(frozen=True)
class _TextosCascata:
    """Textos que variam entre a cascata após verificação web inconclusiva e a
    cascata com a pesquisa web desligada pelo operador (tarefa 6.2).

    - ``justificativa_intervencao``: justificativa da ``DecisaoCampo`` decidida
      pela resposta humana com valor.
    - ``pesquisa_web_desligada``: repassada a ``_decidir_nome_com_regra`` para que
      o prompt não afirme que houve verificação web.
    - ``escalonamento``: fábrica da ``DecisaoCampo`` quando não há regra nem ponte
      humana.
    """

    justificativa_intervencao: str
    escalonamento: Callable[[], DecisaoCampo]
    pesquisa_web_desligada: bool = False


def _cascata_nome(
    registros: list[RegistroCatalogPart],
    llm: LLMProvider,
    rule_store: RuleStore | None,
    on_aviso: Callable[[str], None] | None,
    pedido: PedidoIntervencao,
    pedir_intervencao: Callable[[PedidoIntervencao], RespostaIntervencao] | None,
    limiar_intervencao: float,
    trace: TraceSink | None,
    *,
    textos: _TextosCascata,
) -> DecisaoCampo:
    """Memória de intervenção -> intervenção humana -> escalonamento."""
    # Só repassa a flag quando ligada, preservando a chamada anterior byte a byte.
    kwargs_regra = {"pesquisa_web_desligada": True} if textos.pesquisa_web_desligada else {}

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
                registros, llm, recuperada.regra, score=recuperada.score, **kwargs_regra
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
                justificativa=textos.justificativa_intervencao,
                fonte="intervencao_humana",
                origem_id=resposta.origem_id,
                confianca="alta",
                evidencias=[{"tipo": "intervencao_humana", "regra_confirmada": resposta.regra.titulo}],
            )
        return _decidir_nome_com_regra(registros, llm, resposta.regra, **kwargs_regra)

    return textos.escalonamento()


def nome_em_maiusculas(decisao: DecisaoCampo) -> DecisaoCampo:
    """Todo nome escolhido vai para o catálogo em MAIÚSCULAS, qualquer que seja a
    origem (juiz interno, verificação web, memória/intervenção humana, provedor,
    registro único). Decisão sem valor (escalado_humano) fica intacta — o SQL
    não mexe no name nesse caso."""
    if decisao.campo != "name" or not isinstance(decisao.valor, str):
        return decisao
    valor = decisao.valor.upper()
    return decisao if valor == decisao.valor else dataclasses.replace(decisao, valor=valor)


@functools.wraps(_arbitrar_nome)
def arbitrar_nome(*args, **kwargs) -> DecisaoCampo:
    return nome_em_maiusculas(_arbitrar_nome(*args, **kwargs))


@functools.wraps(_arbitrar_nome_recuperacao)
def arbitrar_nome_recuperacao(*args, **kwargs) -> DecisaoCampo:
    return nome_em_maiusculas(_arbitrar_nome_recuperacao(*args, **kwargs))
