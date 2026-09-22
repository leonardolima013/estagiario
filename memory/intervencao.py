"""Serviços comuns para validar e persistir intervenções confirmadas."""

from __future__ import annotations

import json

from db.rule_store import Regra, RuleStore
from memory.models import PedidoIntervencao, RespostaIntervencao
from memory.recuperador import sinais_para_busca


class IntervencaoInvalidaError(ValueError):
    """Callback humano retornou uma resposta sem regra confirmada."""


def serializar_caso(caso: PedidoIntervencao) -> str:
    """Serializa o episódio sem perder acentos nem a ordem dos nomes."""
    return json.dumps(
        {
            "ponto": caso.ponto,
            "grupo_ref": caso.grupo_ref,
            "search_ref": caso.search_ref,
            "marca": caso.marca,
            "nomes_conflitantes": caso.nomes_conflitantes,
            "motivo": caso.motivo,
            "contexto_web": caso.contexto_web,
            "membro_ids": caso.membro_ids,
            "candidatos": caso.candidatos,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def registrar_resposta_intervencao(
    rule_store: RuleStore,
    caso: PedidoIntervencao,
    resposta: RespostaIntervencao,
) -> Regra:
    """Persiste a proposta já confirmada pelo operador.

    A regra semântica é indexada por sinais derivados de título/condição/resolução;
    o código e o grupo ficam somente no episódio, para não impedir generalização.
    """
    if not isinstance(resposta.resposta_humana, str) or not resposta.resposta_humana.strip():
        raise IntervencaoInvalidaError("resposta humana vazia")
    regra = resposta.regra
    campos = (regra.titulo, regra.condicao, regra.resolucao)
    if any(not isinstance(campo, str) or not campo.strip() for campo in campos):
        raise IntervencaoInvalidaError("regra confirmada incompleta")

    episodio = serializar_caso(caso) + "\nResposta do operador: " + resposta.resposta_humana.strip()
    return rule_store.registrar_intervencao(
        titulo=regra.titulo.strip(),
        caso_episodico=episodio,
        condicao=regra.condicao.strip(),
        resolucao=regra.resolucao.strip(),
        criado_por=resposta.criado_por or "operador",
        sinais_busca=sinais_para_busca(regra.titulo, regra.condicao, regra.resolucao),
        campo=caso.ponto,
        grupo_exemplo_ref=caso.grupo_ref,
    )
