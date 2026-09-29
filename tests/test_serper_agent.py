"""Testes de exemplo/borda da orquestração da Skill_Serper e do payload do
sub-agente. Usam fakes em memória de ClienteSerper e LLMProvider — sem rede, sem
API Serper real, sem LLM real. A SERPER_API_KEY nunca aparece neste arquivo.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from verification.serper_agent import verificar_nomenclatura_peca_serper
from verification.serper_client import (
    ResultadoOrganico,
    SerperAPIKeyAusenteError,
    SerperRequisicaoError,
)
from verification.serper_decisao import (
    _SCHEMA_AVALIACAO_RESULTADO,
    limpar_nome_final,
    marca_generica,
    montar_payload_resultado,
    relacao_lexica,
    remover_anos,
)


@dataclass
class _ClienteFake:
    organicos: list[ResultadoOrganico] = field(default_factory=list)
    erro: Exception | None = None
    chamado: bool = False
    _api_key: str = "FAKE-API-KEY"

    def buscar(self, codigo, marca):
        self.chamado = True
        if self.erro is not None:
            raise self.erro
        return list(self.organicos)


class _ClienteQueFalhaSeChamado:
    _api_key = "FAKE-API-KEY"

    def buscar(self, codigo, marca):
        raise AssertionError("buscar não deveria ser chamado")


@dataclass
class _LLMFake:
    """Devolve `resposta` sempre, ou `respostas[i]` na i-ésima chamada."""

    resposta: dict | None = None
    respostas: list[dict] = field(default_factory=list)
    chamadas: list[dict] = field(default_factory=list)

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append(
            {"system": system, "user": user, "json_schema": json_schema, "schema_name": schema_name}
        )
        if self.respostas:
            return dict(self.respostas[len(self.chamadas) - 1])
        return dict(self.resposta)


def _aval(nome, relacionado=None, coerente=False):
    return {
        "justificativa": "trecho do título",
        "nome_extraido": nome,
        "candidato_relacionado": relacionado,
        "nome_especifico_coerente": coerente,
    }


def _org(title="", link="", snippet="", position=None):
    return ResultadoOrganico(title=title, link=link, snippet=snippet, position=position)


# ---------------------------------------------------------------------------
# Task 4.2 — payload do sub-agente
# ---------------------------------------------------------------------------


def test_payload_resultado_contem_campos_e_candidatos():
    """O payload leva só o resultado avaliado, todos os candidatos, código e marca."""
    org = _org(title="Filtro X", link="https://a.com", snippet="snippet A", position=1)
    candidatos = ["FILTRO OLEO", "FILTRO DE OLEO MOTOR"]
    system, user = montar_payload_resultado(org, candidatos, "ABC123", "Bosch")

    for trecho in (org.title, org.link, org.snippet, str(org.position), "ABC123", "Bosch"):
        assert trecho in user
    for c in candidatos:
        assert c in user
    # O prompt trata conteúdo como dado, não instrução (R8.2).
    assert "instru" in system.lower()


def test_payload_envia_texto_decodificado_sem_escapes_unicode():
    """Tarefa 1: acentos chegam legíveis ao sub-agente; U+00A0 vira espaço."""
    org = _org(
        title="Bucha Do Suporte Do Alternador\xa0– Conversão",
        snippet="Aplicação: caminhões",
        link="https://a.com",
        position=1,
    )
    _, user = montar_payload_resultado(org, ["BUCHA SUPORTE ALTERNADOR"], "ABC123", "Bosch")
    assert "Conversão" in user and "Aplicação: caminhões" in user
    assert "\\u00" not in user
    assert "\xa0" not in user and "\\xa0" not in user
    assert "Alternador – Conversão" in user


def test_payload_omite_marca_generica():
    org = _org(title="ABC123 filtro", link="https://a.com", position=1)
    for marca in ("CONVERSÃO", "Conversao", " Original  OEM ", "oem"):
        assert marca_generica(marca)
        _, user = montar_payload_resultado(org, ["filtro"], "ABC123", None if marca_generica(marca) else marca)
        assert marca.strip() not in user
        assert "avalie apenas código + nome" in user
    assert not marca_generica("BOSCH")
    assert not marca_generica("OEM PARTS")


def test_limpar_nome_final_remove_codigo_marca_e_anos():
    assert limpar_nome_final(
        "Bucha Do Suporte Do Alternador 2003 A 2023 - 20412345", "20412345", "Bosch"
    ) == "Bucha Do Suporte Do Alternador"
    assert limpar_nome_final("Pivô JE 4699 Inferior DRIVEWAY", "JE4699", "Driveway") == "Pivô Inferior"
    assert limpar_nome_final("Filtro de Óleo Original OEM 2010/2015", "X1", "ORIGINAL OEM") == "Filtro de Óleo"
    assert limpar_nome_final("Polia 2010 até 2014", "P1", "CONVERSÃO") == "Polia"


def test_remover_anos_preserva_formas_ambiguas_e_numeros_nao_ano():
    assert remover_anos("Junta 03/13") == "Junta 03/13"
    assert remover_anos("Anel 1,00 mm") == "Anel 1,00 mm"
    assert remover_anos("Correia 6PK1045") == "Correia 6PK1045"
    assert remover_anos("Correia 2003-2023") == "Correia"


def test_relacao_lexica_ignora_preposicoes():
    candidatos = ["BUCHA SUPORTE ALTERNADOR", "PIVO"]
    assert relacao_lexica("Bucha Do Suporte Do Alternador", candidatos) == "BUCHA SUPORTE ALTERNADOR"
    assert relacao_lexica("Filtro de Ar", candidatos) is None


def test_gerar_json_chamado_com_schema_nao_texto_livre():
    """R4.3: a decisão usa gerar_json com schema, não texto livre."""
    organicos = [_org(title="ABC123 filtro", link="https://a.com", position=1)]
    llm = _LLMFake(resposta=_aval("filtro", relacionado="filtro"))
    verificar_nomenclatura_peca_serper(
        "ABC123", "Bosch", ["filtro"], cliente=_ClienteFake(organicos=organicos), llm=llm
    )
    assert len(llm.chamadas) == 1
    assert llm.chamadas[0]["json_schema"] is _SCHEMA_AVALIACAO_RESULTADO


# ---------------------------------------------------------------------------
# Task 6.9 — bordas da orquestração
# ---------------------------------------------------------------------------


def test_candidatos_vazios_sem_requisicao():
    """R2.7: candidatos vazios -> inconclusivo sem chamar o cliente."""
    cliente = _ClienteQueFalhaSeChamado()
    resultado = verificar_nomenclatura_peca_serper("ABC", "Bosch", [], cliente=cliente)
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_chave_ausente_sem_requisicao(monkeypatch):
    """R3.5, R9.5: chave ausente -> inconclusivo sem requisição, sem vazar valor."""
    # Sem cliente injetado, ClienteSerper() é construído e lê a chave de config.
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    resultado = verificar_nomenclatura_peca_serper("ABC", "Bosch", ["filtro"])
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None
    # A justificativa não contém valor de chave.
    assert "SERPER_API_KEY" not in resultado.justificativa or "ausente" in resultado.justificativa.lower()


def test_resposta_vazia_inconclusivo():
    """R3.9: sem orgânicos -> inconclusivo."""
    resultado = verificar_nomenclatura_peca_serper(
        "ABC", "Bosch", ["filtro"], cliente=_ClienteFake(organicos=[])
    )
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_resultados_organicos_sao_emitidos_em_ordem_antes_de_decisao_inconclusiva():
    """Cada evidência da resposta é observável sem expor prompt ou credencial."""
    organicos = [
        _org(
            title="ABC123 FILTRO A",
            link="https://a.example/item",
            snippet="snippet A\nsegunda linha",
            position=3,
        ),
        _org(
            title="ABC123 FILTRO B",
            link="https://b.example/item",
            snippet="snippet B",
            position=1,
        ),
        _org(
            title="ABC123 FILTRO C",
            link="https://c.example/item",
            snippet="snippet C",
            position=2,
        ),
    ]
    cliente = _ClienteFake(organicos=organicos, _api_key="SYNTHETIC-SERPER-KEY")
    llm = _LLMFake(resposta=_aval(None))
    eventos: list[str] = []

    resultado = verificar_nomenclatura_peca_serper(
        "ABC123",
        "Bosch",
        ["FILTRO A", "FILTRO B", "FILTRO C"],
        cliente=cliente,
        llm=llm,
        on_evento=eventos.append,
    )

    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None
    eventos = [e for e in eventos if e.startswith("Serper resultado orgânico:")]
    assert len(eventos) == len(organicos)
    for ordinal, (evento, organico) in enumerate(zip(eventos, organicos), start=1):
        assert f'"ordinal":{ordinal}' in evento
        assert f'"position":{organico.position}' in evento
        assert json.dumps(organico.title, ensure_ascii=False) in evento
        assert json.dumps(organico.snippet, ensure_ascii=False) in evento
        assert json.dumps(organico.link, ensure_ascii=False) in evento
    assert "\n" not in eventos[0]

    texto_eventos = " ".join(eventos)
    assert "SYNTHETIC-SERPER-KEY" not in texto_eventos
    assert "X-API-KEY" not in texto_eventos
    assert llm.chamadas[0]["user"] not in texto_eventos
    assert llm.chamadas[0]["system"] not in texto_eventos


def test_resposta_sem_organicos_emite_zero_resultados_antes_de_inconclusivo():
    eventos: list[str] = []
    llm = _LLMFake(resposta=_aval("FILTRO", relacionado="FILTRO"))

    resultado = verificar_nomenclatura_peca_serper(
        "ABC", "Bosch", ["FILTRO"], cliente=_ClienteFake(organicos=[]), llm=llm,
        on_evento=eventos.append,
    )

    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None
    assert eventos == ["Serper: 0 resultados orgânicos retornados pela API."]
    assert llm.chamadas == []


def test_falha_cliente_inconclusivo_preserva_estado():
    """R3.8: falha do cliente -> inconclusivo, candidatos preservados."""
    candidatos = ["filtro a", "filtro b"]
    originais = list(candidatos)
    resultado = verificar_nomenclatura_peca_serper(
        "ABC", "Bosch", candidatos, cliente=_ClienteFake(erro=SerperRequisicaoError("rede"))
    )
    assert resultado.status == "inconclusivo"
    assert candidatos == originais


def test_saida_malformada_do_subagente_preserva_estado():
    """R4.6: saída inválida contra o schema -> inconclusivo, sem decisão."""
    organicos = [_org(title="ABC filtro", link="https://a.com", position=1)]
    llm = _LLMFake(resposta={"nome_extraido": 42, "nome_especifico_coerente": "sim"})
    resultado = verificar_nomenclatura_peca_serper(
        "ABC", "Bosch", ["filtro"], cliente=_ClienteFake(organicos=organicos), llm=llm
    )
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_guard_nao_vazamento_da_chave_na_saida():
    """R8.3, R8.4: a chave sintética do cliente nunca aparece na saída."""
    organicos = [
        _org(title="ABC123 filtro", link="https://a.com", position=1),
        _org(title="ABC123 filtro", link="https://b.com", position=2),
    ]
    cliente = _ClienteFake(organicos=organicos, _api_key="CHAVE-SINTETICA-NAO-REAL")
    llm = _LLMFake(resposta=_aval("filtro", relacionado="filtro"))
    resultado = verificar_nomenclatura_peca_serper(
        "ABC123", "Bosch", ["filtro"], cliente=cliente, llm=llm
    )
    texto = resultado.justificativa + " " + " ".join(f.url + f.nome_encontrado for f in resultado.fontes)
    assert "CHAVE-SINTETICA-NAO-REAL" not in texto


def test_confirmacao_bem_sucedida_com_sinais():
    """Caminho feliz: nome confirmado com sinal observável na justificativa."""
    organicos = [
        _org(title="ABC123 FILTRO OLEO", link="https://produto.mercadolivre.com.br/x", position=1),
        _org(title="ABC123 FILTRO OLEO", link="https://b.com", position=2),
    ]
    llm = _LLMFake(resposta=_aval("FILTRO OLEO", relacionado="FILTRO OLEO"))
    resultado = verificar_nomenclatura_peca_serper(
        "ABC123", "Bosch", ["FILTRO OLEO"], cliente=_ClienteFake(organicos=organicos), llm=llm
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "FILTRO OLEO"
    assert "posicao=" in resultado.justificativa
    assert "link_confiavel(posicao=1)" in resultado.justificativa
    assert [f.url for f in resultado.fontes] == ["https://produto.mercadolivre.com.br/x"]


# ---------------------------------------------------------------------------
# Nova lógica de decisão: código explícito, short-circuit, nome novo
# ---------------------------------------------------------------------------


def test_short_circuit_para_no_primeiro_resultado_de_alta_confianca():
    """#1 sem código nunca vai ao LLM; #2 qualifica; #3..#10 não são avaliados."""
    organicos = [_org(title="Bucha genérica", link="https://l0.com", position=1)] + [
        _org(title=f"20412345 Bucha Suporte Alternador {i}", link=f"https://l{i}.com", position=i)
        for i in range(2, 11)
    ]
    llm = _LLMFake(resposta=_aval("Bucha Suporte Alternador", relacionado="BUCHA SUPORTE ALTERNADOR"))
    eventos: list[str] = []
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["BUCHA SUPORTE ALTERNADOR", "BUCHA"],
        cliente=_ClienteFake(organicos=organicos), llm=llm, on_evento=eventos.append,
    )
    assert resultado.status == "confirmado"
    assert len(llm.chamadas) == 1
    assert '"posicao": 2' in llm.chamadas[0]["user"]
    assert resultado.fontes[0].url == "https://l2.com"
    assert any("posicao=1: código ausente" in e for e in eventos)
    assert any("resultados restantes não avaliados" in e for e in eventos)


def test_resultados_sem_codigo_nunca_chegam_ao_llm():
    organicos = [
        _org(title="Bucha Suporte Alternador", snippet="sem código aqui", link="https://a.com", position=1),
        _org(title="Bucha 204123456", link="https://b.com", position=2),  # código não é o mesmo
    ]
    llm = _LLMFake(resposta=_aval("Bucha", coerente=True))
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["BUCHA SUPORTE ALTERNADOR", "BUCHA"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "inconclusivo"
    assert llm.chamadas == []


def test_nome_novo_extraido_da_busca_e_limpo():
    """2.1 + 2.4: nome diferente dos candidatos, sem código/veículo/anos."""
    organicos = [
        _org(
            title="Bucha Do Suporte Do Alternador VOLVO VM220 2003 A 2023 - 20412345",
            link="https://loja.com/p", position=1,
        )
    ]
    llm = _LLMFake(resposta=_aval(
        "Bucha Do Suporte Do Alternador 2003 A 2023 20412345",
        relacionado="BUCHA SUPORTE ALTERNADOR",
    ))
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "CONVERSÃO", ["BUCHA SUPORTE ALTERNADOR", "BUCHA ALTERNADOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "Bucha Do Suporte Do Alternador"
    # Marca genérica: não entra no payload do sub-agente.
    assert "CONVERSÃO" not in llm.chamadas[0]["user"]


def test_codigo_so_no_snippet_basta():
    organicos = [_org(title="Pivô de Suspensão Inferior", snippet="Ref. JE-4699 original", link="https://a.com", position=1)]
    llm = _LLMFake(resposta=_aval("Pivô de Suspensão Inferior", relacionado="PIVO INFERIOR"))
    resultado = verificar_nomenclatura_peca_serper(
        "JE4699", "DRIVEWAY", ["PIVO SUPERIOR", "PIVO INFERIOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "Pivô de Suspensão Inferior"


def test_marca_ausente_no_resultado_nao_impede_alta_confianca():
    """A marca é reforço, não requisito."""
    organicos = [_org(title="JE4699 Pivô Suspensão Inferior", link="https://outra-loja.com", position=1)]
    llm = _LLMFake(resposta=_aval("Pivô Suspensão Inferior", coerente=True))
    resultado = verificar_nomenclatura_peca_serper(
        "JE4699", "DRIVEWAY", ["PIVO SUPERIOR", "PIVO INFERIOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "confirmado"
    assert "marca_reforco" not in resultado.justificativa


def test_nome_inventado_e_rejeitado_e_loop_segue():
    organicos = [
        _org(title="JE4699 Pivô", link="https://a.com", position=1),
        _org(title="JE4699 Pivô de Suspensão Inferior", link="https://b.com", position=2),
    ]
    llm = _LLMFake(respostas=[
        _aval("Pivô Dianteiro Reforçado", coerente=True),  # "Dianteiro/Reforçado" não existem
        _aval("Pivô de Suspensão Inferior", relacionado="PIVO INFERIOR"),
    ])
    resultado = verificar_nomenclatura_peca_serper(
        "JE4699", "DRIVEWAY", ["PIVO SUPERIOR", "PIVO INFERIOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "Pivô de Suspensão Inferior"
    assert len(llm.chamadas) == 2


def test_muitas_variacoes_nao_impedem_vencedor_claro():
    """2.5: vários nomes diferentes na busca; o primeiro que qualifica vence."""
    organicos = [
        _org(title=f"JE4699 Produto {i}", link=f"https://l{i}.com", position=i) for i in range(1, 4)
    ] + [_org(title="JE4699 Pivô Inferior Suspensão", link="https://ok.com", position=4)]
    llm = _LLMFake(respostas=[_aval(None)] * 3 + [_aval("Pivô Inferior Suspensão", relacionado="PIVO INFERIOR")])
    resultado = verificar_nomenclatura_peca_serper(
        "JE4699", "DRIVEWAY", ["PIVO SUPERIOR", "PIVO INFERIOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "confirmado"
    assert resultado.fontes[0].url == "https://ok.com"


def test_nome_generico_sem_relacao_nao_qualifica():
    organicos = [_org(title="JE4699 Produto", link="https://a.com", position=1)]
    llm = _LLMFake(resposta=_aval("Produto", coerente=False))
    resultado = verificar_nomenclatura_peca_serper(
        "JE4699", "DRIVEWAY", ["PIVO SUPERIOR", "PIVO INFERIOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_fallback_sem_llm_um_resultado_basta():
    organicos = [_org(title="JE4699 PIVO INFERIOR DRIVEWAY", link="https://a.com", position=1)]
    resultado = verificar_nomenclatura_peca_serper(
        "JE4699", "DRIVEWAY", ["PIVO SUPERIOR", "PIVO INFERIOR"],
        cliente=_ClienteFake(organicos=organicos),
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "PIVO INFERIOR"


# ---------------------------------------------------------------------------
# Tarefa 3 — nome final sempre em português (pt-BR)
# ---------------------------------------------------------------------------


def _aval_idioma(nome, idioma, nome_pt=None, relacionado=None, coerente=False):
    resposta = _aval(nome, relacionado=relacionado, coerente=coerente)
    resposta.update({"idioma_origem": idioma, "nome_pt": nome_pt})
    return resposta


def test_3_1_evidencia_estrangeira_relacionada_usa_candidato_existente():
    organicos = [_org(title="20412345 Alternator Mounting Bushing Volvo", link="https://en.com", position=1)]
    llm = _LLMFake(resposta=_aval_idioma(
        "Alternator Mounting Bushing", "en",
        nome_pt="Bucha de Montagem do Alternador", relacionado="BUCHA SUPORTE ALTERNADOR",
    ))
    eventos: list[str] = []
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["BUCHA SUPORTE ALTERNADOR", "BUCHA"],
        cliente=_ClienteFake(organicos=organicos), llm=llm, on_evento=eventos.append,
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "BUCHA SUPORTE ALTERNADOR"
    assert "nome_pt_candidato_existente(idioma_evidencia=en)" in resultado.justificativa
    assert len(llm.chamadas) == 1  # sem chamada de tradução
    log = next(e for e in eventos if "[3.1]" in e)
    assert "Alternator Mounting Bushing" in log  # evidência original auditável


def test_3_1_idioma_nao_informado_detectado_pela_heuristica():
    organicos = [_org(title="20412345 Alternator Bushing", link="https://en.com", position=1)]
    llm = _LLMFake(resposta=_aval("Alternator Bushing", relacionado="BUCHA SUPORTE ALTERNADOR"))
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["BUCHA SUPORTE ALTERNADOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.nome_sugerido == "BUCHA SUPORTE ALTERNADOR"


def test_evidencia_em_portugues_mantem_nome_extraido():
    """A regra 2.1 continua valendo para evidência em português."""
    organicos = [_org(title="20412345 Bucha Do Suporte Do Alternador", link="https://a.com", position=1)]
    llm = _LLMFake(resposta=_aval_idioma(
        "Bucha Do Suporte Do Alternador", "pt-BR",
        nome_pt="Bucha Do Suporte Do Alternador", relacionado="BUCHA SUPORTE ALTERNADOR",
    ))
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["BUCHA SUPORTE ALTERNADOR"],
        cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.nome_sugerido == "Bucha Do Suporte Do Alternador"
    assert "nome_pt_" not in resultado.justificativa


def test_3_2_nome_especifico_estrangeiro_e_traduzido():
    organicos = [_org(title="FF5421 Fuel Filter for Cummins 2010-2018", link="https://en.com", position=1)]
    llm = _LLMFake(resposta=_aval_idioma(
        "Fuel Filter", "en", nome_pt="Filtro de Combustível", coerente=True,
    ))
    eventos: list[str] = []
    resultado = verificar_nomenclatura_peca_serper(
        "FF5421", "Fleetguard", ["PIVO"],
        cliente=_ClienteFake(organicos=organicos), llm=llm, on_evento=eventos.append,
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "Filtro de Combustível"
    assert "nome_pt_traduzido(idioma_evidencia=en)" in resultado.justificativa
    assert any("[3.2]" in e and "Fuel Filter" in e for e in eventos)


def test_3_3_salvaguarda_traduz_quando_subagente_nao_traduziu():
    """O sub-agente diz 'pt' e não traduz; a salvaguarda detecta e traduz."""
    organicos = [_org(title="FF5421 Fuel Pump Assembly", link="https://en.com", position=1)]
    llm = _LLMFake(respostas=[
        _aval_idioma("Fuel Pump Assembly", "pt", nome_pt="Fuel Pump Assembly", coerente=True),
        {"nome_pt": "Bomba de Combustível"},
    ])
    eventos: list[str] = []
    resultado = verificar_nomenclatura_peca_serper(
        "FF5421", "Bosch", ["PIVO"],
        cliente=_ClienteFake(organicos=organicos), llm=llm, on_evento=eventos.append,
    )
    assert resultado.nome_sugerido == "Bomba de Combustível"
    assert llm.chamadas[1]["schema_name"] == "traducao_nome_pt"
    assert "nome_pt_salvaguarda" in resultado.justificativa
    assert any("[3.3]" in e and "Fuel Pump Assembly" in e for e in eventos)


def test_3_3_resultado_nao_latino_qualifica_e_e_traduzido():
    """Cirílico passa na checagem de não-invenção e sai traduzido."""
    organicos = [_org(title="20412345 Втулка кронштейна генератора", link="https://ru.com", position=1)]
    llm = _LLMFake(respostas=[
        _aval_idioma("Втулка кронштейна генератора", "ru", nome_pt=None, coerente=True),
        {"nome_pt": "Bucha do Suporte do Alternador"},
    ])
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["PIVO"], cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "Bucha do Suporte do Alternador"


def test_3_3_traducao_invalida_nunca_retorna_nome_estrangeiro():
    organicos = [_org(title="20412345 Втулка генератора", link="https://ru.com", position=1)]
    llm = _LLMFake(respostas=[
        _aval_idioma("Втулка генератора", "ru", coerente=True),
        {"nome_pt": "Втулка генератора"},  # "tradução" que continua em cirílico
    ])
    resultado = verificar_nomenclatura_peca_serper(
        "20412345", "Bosch", ["PIVO"], cliente=_ClienteFake(organicos=organicos), llm=llm,
    )
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_3_3_fallback_sem_llm_nao_retorna_candidato_estrangeiro():
    organicos = [_org(title="FF5421 Fuel Filter", link="https://a.com", position=1)]
    resultado = verificar_nomenclatura_peca_serper(
        "FF5421", "Bosch", ["FUEL FILTER"], cliente=_ClienteFake(organicos=organicos),
    )
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_parece_estrangeiro_nao_dispara_em_estrangeirismos_usuais():
    from verification.serper_decisao import parece_estrangeiro

    for nome in ("PLUG ELETRÔNICO ÁGUA 24V", "AIR BAG", "Kit Embreagem", "Sensor de Rotação", "Coxim do Motor"):
        assert not parece_estrangeiro(nome), nome
    for nome in ("Fuel Filter", "Rodamiento de rueda", "Втулка", "مرشح الوقود", "Muñeca"):
        assert parece_estrangeiro(nome), nome
