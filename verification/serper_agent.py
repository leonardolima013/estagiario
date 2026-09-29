"""Skill_Serper — orquestração da verificação web de nomenclatura via API Serper.

Entry point compatível com o Contrato_Verificacao (mesma assinatura e retorno
`ResultadoVerificacao` da Skill_Playwright). Faz validação de entrada, chama o
Cliente_Serper (I/O) e percorre os resultados por posição: resultados sem o
código explícito são descartados sem LLM; cada um dos demais é avaliado sozinho
pelo SubAgente_Nome_Serper (LLMProvider.gerar_json) e o primeiro de alta
confiança encerra a busca (short-circuit). Toda falha de runtime degrada para
`inconclusivo` sem efeito colateral, preservando o estado da peça.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable

from llm.provider import LLMProvider
from loop.tracing import sanitizar_detalhes
from verification.models import FonteWeb, ResultadoVerificacao
from verification.serper_client import (
    ClienteSerper,
    ResultadoOrganico,
    SerperAPIKeyAusenteError,
    SerperRequisicaoError,
)
from verification.serper_decisao import (
    _SCHEMA_AVALIACAO_RESULTADO,
    _SCHEMA_TRADUCAO_NOME,
    TERMOS_PROIBIDOS_VAZAMENTO,
    AvaliacaoInvalidaError,
    AvaliacaoResultado,
    candidato_correspondente,
    codigo_explicito,
    escolher_deterministico,
    limpar_nome_final,
    link_confiavel,
    marca_generica,
    marca_no_resultado,
    montar_payload_resultado,
    montar_payload_traducao,
    ordenar_por_posicao,
    parece_estrangeiro,
    relacao_lexica,
    resultado_vazio,
    tem_escrita_nao_latina,
    tokens_sustentados,
    validar_avaliacao,
)


def _emitir(on_evento: Callable[[str], None] | None, msg: str) -> None:
    if on_evento is not None:
        on_evento(msg)


def _inconclusivo(justificativa: str, fontes: list[FonteWeb] | None = None) -> ResultadoVerificacao:
    return ResultadoVerificacao(
        status="inconclusivo",
        nome_sugerido=None,
        justificativa=justificativa,
        fontes=fontes or [],
    )


def _sanitizar_evidencia(valor: object) -> object:
    """Limita e torna segura a representação de conteúdo retornado pela busca."""
    return sanitizar_detalhes(valor)


def _emitir_resultados_organicos(
    on_evento: Callable[[str], None] | None,
    organicos: list[ResultadoOrganico],
) -> None:
    """Emite evidências Serper sem tratar conteúdo externo como instrução."""
    if not organicos:
        _emitir(on_evento, "Serper: 0 resultados orgânicos retornados pela API.")
        return

    for ordinal, organico in enumerate(organicos, start=1):
        dados = {
            "ordinal": ordinal,
            "position": _sanitizar_evidencia(organico.position),
            "title": _sanitizar_evidencia(organico.title),
            "snippet": _sanitizar_evidencia(organico.snippet),
            "link": _sanitizar_evidencia(organico.link),
        }
        # ensure_ascii=False: acentos legíveis no log; controles (\n) seguem escapados.
        mensagem = "Serper resultado orgânico: " + json.dumps(
            dados,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _emitir(on_evento, mensagem)


def _em_branco(valor: str | None) -> bool:
    return valor is None or not str(valor).strip()


def _contem_termo_proibido(texto: str, api_key: str | None) -> bool:
    baixo = texto.casefold()
    if api_key and api_key.casefold() in baixo:
        return True
    return any(termo in baixo for termo in TERMOS_PROIBIDOS_VAZAMENTO)


def verificar_nomenclatura_peca_serper(
    codigo: str,
    marca: str,
    nomes_conflitantes: list[str],
    *,
    on_evento: Callable[[str], None] | None = None,
    llm: LLMProvider | None = None,
    cliente: ClienteSerper | None = None,
) -> ResultadoVerificacao:
    """Verifica a nomenclatura da peça via API Serper + sub-agente LLM.

    Contrato idêntico ao da Skill_Playwright. `llm` e `cliente` são injetáveis
    para testes puros com fakes (sem rede, sem API real, sem LLM real).
    """
    # 1. Validação de entrada (R2.7, R3.2) — antes de qualquer requisição.
    if _em_branco(codigo):
        return _inconclusivo("Código da peça ausente ou em branco.")
    if _em_branco(marca):
        return _inconclusivo("Marca da peça ausente ou em branco.")
    candidatos = [n for n in (nomes_conflitantes or []) if n and n.strip()]
    if not candidatos:
        return _inconclusivo("Nenhum nome candidato informado.")

    codigo = codigo.strip()
    marca = marca.strip()

    # 2. Guard de API key + cliente (R9.5) — sem requisição se a chave falta.
    try:
        cliente = cliente or ClienteSerper()
    except SerperAPIKeyAusenteError:
        # Erro de configuração degradado para inconclusivo, sem vazar o valor.
        return _inconclusivo("Configuração da chave de API Serper ausente.")

    # 3. Requisição à API (R3.6); falha/vazio -> inconclusivo (R3.8, R3.9).
    try:
        organicos = cliente.buscar(codigo, marca)
    except SerperRequisicaoError as exc:
        _emitir(on_evento, f"Verificação Serper falhou: {exc}")
        return _inconclusivo(f"Falha ao consultar a API Serper: {exc}")
    except SerperAPIKeyAusenteError:
        return _inconclusivo("Configuração da chave de API Serper ausente.")

    # Registra a resposta completa antes de qualquer decisão do sub-agente. A
    # mensagem usa representação JSON segura: evidencia é truncada pelo mesmo
    # sanitizador do trace e controles (como quebras de linha) são escapados.
    _emitir_resultados_organicos(on_evento, organicos)
    if not organicos:
        return _inconclusivo("A API Serper não retornou resultados orgânicos.")

    # 4. Decisão resultado a resultado, por posição, com short-circuit.
    marca_ref = None if marca_generica(marca) else marca
    if marca_ref is None:
        _emitir(on_evento, f"Serper: marca genérica {marca!r} ignorada; decisão por código + nome.")
    api_key = getattr(cliente, "_api_key", None)

    if llm is None:
        # Fallback determinístico puro (sem LLM): primeiro resultado com código
        # explícito que contém todas as palavras de um candidato.
        escolha = escolher_deterministico(organicos, candidatos, codigo)
        if escolha is None:
            return _inconclusivo(_MOTIVO_SEM_ALTA_CONFIANCA)
        nome, org = escolha
        nome = limpar_nome_final(nome, codigo, marca)
        if resultado_vazio(nome):
            return _inconclusivo("Nome sugerido ficou vazio após remover código e marca.")
        # Salvaguarda de idioma (3.3): sem LLM não há como traduzir.
        nome, sinal = _garantir_portugues(nome, None, codigo, marca, on_evento, _rotulo_posicao(org))
        if nome is None:
            return _inconclusivo(_MOTIVO_IDIOMA)
        return _confirmado(nome, org, "relacionado_a_candidato", marca_ref, api_key, [sinal] if sinal else [])

    for org in ordenar_por_posicao(organicos):
        posicao = _rotulo_posicao(org)
        if not codigo_explicito(f"{org.title} {org.snippet}", codigo):
            _emitir(on_evento, f"Serper posicao={posicao}: código ausente no título/snippet — ignorado.")
            continue

        system, user = montar_payload_resultado(org, candidatos, codigo, marca_ref)
        try:
            bruto = llm.gerar_json(
                system=system,
                user=user,
                json_schema=_SCHEMA_AVALIACAO_RESULTADO,
                schema_name="avaliacao_resultado_serper",
            )
        except Exception as exc:  # noqa: BLE001 — degradação controlada
            _emitir(on_evento, f"Sub-agente Serper falhou: {type(exc).__name__}")
            return _inconclusivo("Falha ao decidir o nome via sub-agente.")

        try:
            avaliacao = validar_avaliacao(bruto)
        except AvaliacaoInvalidaError as exc:
            _emitir(on_evento, f"Serper posicao={posicao}: saída do sub-agente inválida ({exc}) — próximo.")
            continue

        qualificacao, motivo = _avaliar_alta_confianca(avaliacao, org, candidatos, codigo, marca)
        if qualificacao is None:
            _emitir(on_evento, f"Serper posicao={posicao}: não qualificou — {motivo}.")
            continue

        # A confiança já está decidida; daqui em diante só o IDIOMA do nome
        # final é tratado (3.1, 3.2, 3.3) — nunca a decisão em si.
        nome, sinais_idioma = _nome_final_em_portugues(
            avaliacao, qualificacao, org, codigo, marca, llm, on_evento
        )
        if nome is None:
            _emitir(
                on_evento,
                f"Serper posicao={posicao}: alta confiança, mas sem nome garantido em "
                "português — próximo.",
            )
            continue

        _emitir(
            on_evento,
            f"Serper posicao={posicao}: alta confiança ({qualificacao.criterio}) — "
            "resultados restantes não avaliados.",
        )
        return _confirmado(nome, org, qualificacao.criterio, marca_ref, api_key, sinais_idioma)

    return _inconclusivo(_MOTIVO_SEM_ALTA_CONFIANCA)


_MOTIVO_SEM_ALTA_CONFIANCA = (
    "Nenhum resultado Serper com código explícito e nome relacionado aos "
    "candidatos ou específico de peça."
)
_MOTIVO_IDIOMA = "Nome sugerido não está em português e não pôde ser traduzido."


@dataclass(frozen=True)
class _Qualificacao:
    """Resultado de alta confiança: nome (idioma original, limpo), critério da
    regra 2.2 e o candidato do banco relacionado (variante a), se houver."""

    nome: str
    criterio: str
    candidato: str | None


def _rotulo_posicao(org: ResultadoOrganico) -> str:
    return str(org.position) if org.position is not None else "?"


def _avaliar_alta_confianca(
    avaliacao: AvaliacaoResultado,
    org: ResultadoOrganico,
    candidatos: list[str],
    codigo: str,
    marca: str,
) -> tuple[_Qualificacao | None, str]:
    """Regra de alta confiança (o código explícito já foi verificado antes):
    nome utilizável, sem palavras inventadas, e relacionado a um candidato OU
    específico/coerente por si só. A marca nunca é requisito. Devolve
    (qualificação, "") ou (None, motivo da rejeição)."""
    if _em_branco(avaliacao.nome_extraido):
        return None, "sem nome de peça utilizável"
    nome = limpar_nome_final(avaliacao.nome_extraido, codigo, marca)
    if resultado_vazio(nome):
        return None, "nome vazio após remover código, marca e anos"
    if not tokens_sustentados(nome, org, candidatos):
        return None, "nome com termos ausentes do resultado e dos candidatos"
    candidato = candidato_correspondente(
        avaliacao.candidato_relacionado, candidatos
    ) or relacao_lexica(nome, candidatos)
    if candidato:
        return _Qualificacao(nome, "relacionado_a_candidato", candidato), ""
    if avaliacao.nome_especifico_coerente:
        return _Qualificacao(nome, "nome_especifico", None), ""
    return None, "nome sem relação com os candidatos e não específico"


def _citar(texto: str | None) -> str:
    """Cita texto externo no log de forma segura (truncado, controles escapados)."""
    return json.dumps(_sanitizar_evidencia(texto or ""), ensure_ascii=False)


def _nome_final_em_portugues(
    avaliacao: AvaliacaoResultado,
    qualificacao: _Qualificacao,
    org: ResultadoOrganico,
    codigo: str,
    marca: str,
    llm: LLMProvider,
    on_evento: Callable[[str], None] | None,
) -> tuple[str | None, list[str]]:
    """Garante o nome final em pt-BR depois da decisão de confiança.

    - Evidência em português: o nome extraído segue como está (regra 2.1).
    - Evidência estrangeira + variante (a): o nome final é o candidato do banco,
      já em português (3.1); o resultado estrangeiro só valida a confiança.
    - Evidência estrangeira + variante (b): usa a tradução pt-BR do sub-agente (3.2).
    - Em todos os casos, a salvaguarda de idioma roda por último (3.3).

    Devolve (nome, sinais para a justificativa) ou (None, []) se não houver como
    garantir português."""
    posicao = _rotulo_posicao(org)
    idioma = avaliacao.idioma_origem
    estrangeiro = (idioma is not None and idioma != "pt") or parece_estrangeiro(qualificacao.nome)
    rotulo_idioma = idioma if idioma and idioma != "pt" else "?"
    sinais: list[str] = []
    nome = qualificacao.nome

    if estrangeiro:
        candidato_pt = (
            limpar_nome_final(qualificacao.candidato, codigo, marca)
            if qualificacao.candidato
            else ""
        )
        if qualificacao.criterio == "relacionado_a_candidato" and not resultado_vazio(candidato_pt):
            nome = candidato_pt
            sinais.append(f"nome_pt_candidato_existente(idioma_evidencia={rotulo_idioma})")
            _emitir(
                on_evento,
                f"Serper posicao={posicao}: [3.1] evidência em idioma estrangeiro "
                f"({rotulo_idioma}); nome final = candidato existente {_citar(nome)}. "
                f"Evidência original: nome={_citar(qualificacao.nome)}, "
                f"título={_citar(org.title)}.",
            )
        else:
            traduzido = (
                limpar_nome_final(avaliacao.nome_pt, codigo, marca) if avaliacao.nome_pt else ""
            )
            if not resultado_vazio(traduzido):
                nome = traduzido
                sinais.append(f"nome_pt_traduzido(idioma_evidencia={rotulo_idioma})")
                _emitir(
                    on_evento,
                    f"Serper posicao={posicao}: [3.2] nome traduzido para pt-BR "
                    f"{_citar(nome)}. Texto original ({rotulo_idioma}): "
                    f"nome={_citar(qualificacao.nome)}, título={_citar(org.title)}.",
                )
            # Sem tradução do sub-agente, a salvaguarda abaixo traduz.

    nome, sinal = _garantir_portugues(nome, llm, codigo, marca, on_evento, posicao)
    if nome is None:
        return None, []
    if sinal:
        sinais.append(sinal)
    return nome, sinais


def _garantir_portugues(
    nome: str,
    llm: LLMProvider | None,
    codigo: str,
    marca: str,
    on_evento: Callable[[str], None] | None,
    posicao: str,
) -> tuple[str | None, str | None]:
    """Salvaguarda final (3.3), independente do caminho: se o nome não parece
    português, traduz com o LLM antes de retornar. Sem LLM, com falha na
    tradução, ou se ainda restar escrita não latina, devolve None — um nome em
    outro idioma nunca é retornado. Após a tradução só a escrita é reconferida
    (não o léxico), para não rejeitar estrangeirismos usuais em pt-BR."""
    if not parece_estrangeiro(nome):
        return nome, None
    if llm is None:
        _emitir(
            on_evento,
            f"Serper posicao={posicao}: [3.3] nome {_citar(nome)} não parece português "
            "e não há sub-agente para traduzir — descartado.",
        )
        return None, None
    system, user = montar_payload_traducao(nome)
    try:
        bruto = llm.gerar_json(
            system=system,
            user=user,
            json_schema=_SCHEMA_TRADUCAO_NOME,
            schema_name="traducao_nome_pt",
        )
    except Exception as exc:  # noqa: BLE001 — degradação controlada
        _emitir(on_evento, f"Serper posicao={posicao}: [3.3] tradução falhou: {type(exc).__name__}.")
        return None, None
    bruto_pt = bruto.get("nome_pt") if isinstance(bruto, dict) else None
    traduzido = limpar_nome_final(bruto_pt, codigo, marca) if isinstance(bruto_pt, str) else ""
    if resultado_vazio(traduzido) or tem_escrita_nao_latina(traduzido):
        _emitir(
            on_evento,
            f"Serper posicao={posicao}: [3.3] tradução de {_citar(nome)} inválida "
            f"({_citar(traduzido)}) — descartado.",
        )
        return None, None
    _emitir(
        on_evento,
        f"Serper posicao={posicao}: [3.3] validação de idioma: {_citar(nome)} não "
        f"parecia português; traduzido para {_citar(traduzido)}.",
    )
    return traduzido, "nome_pt_salvaguarda"


def _confirmado(
    nome: str,
    org: ResultadoOrganico,
    criterio: str,
    marca_ref: str | None,
    api_key: str | None,
    sinais_idioma: list[str] | None = None,
) -> ResultadoVerificacao:
    """Monta o resultado confirmado a partir do único resultado vencedor. A
    justificativa é construída aqui só com sinais observáveis — nunca com texto
    do modelo — e passa pelo guard de não-vazamento (R8.4)."""
    posicao = _rotulo_posicao(org)
    sinais = [f"codigo_explicito(posicao={posicao})", f"{criterio}(posicao={posicao})"]
    if marca_ref and marca_no_resultado(org, marca_ref):
        sinais.append(f"marca_reforco(posicao={posicao})")
    if link_confiavel(org.link, marca_ref or ""):
        sinais.append(f"link_confiavel(posicao={posicao})")
    sinais.extend(sinais_idioma or [])
    justificativa = (
        "Nome confirmado por resultado Serper de alta confiança. "
        f"Sinais: {', '.join(sinais)}."
    )
    if _contem_termo_proibido(justificativa, api_key):
        # Guard defensivo: nunca deixa segredo/raciocínio vazar.
        return _inconclusivo("Verificação inconclusiva.")
    return ResultadoVerificacao(
        status="confirmado",
        nome_sugerido=nome,
        justificativa=justificativa,
        fontes=[FonteWeb(url=org.link, nome_encontrado=nome)],
    )
