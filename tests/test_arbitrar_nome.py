import pytest

from arbitration.models import DecisaoCampo
from arbitration.nome import ArbitragemInvalidaError, arbitrar_nome
from db.rule_store import RuleStore
from tests.fakes import FakeLLMProvider, FakeVerificadorWeb
from verification.models import FonteWeb, ResultadoVerificacao
from memory.models import RegraProposta, RespostaIntervencao

# Nomes usados nestes testes do juiz interno diferem só em caixa/espaçamento —
# não "divergem" pra fins de verification.divergencia.nomes_normalizados_divergem,
# então continuam passando pelo caminho antigo (julgamento_modelo), não pela
# verificação web (SPEC-verificacao-web-nomenclatura.md). Ver
# test_nomes_divergentes_* abaixo pro caminho novo.


def test_escolhe_nome_existente(fazer_registro):
    registros = [fazer_registro(1, "polia da correia dentada"), fazer_registro(2, "POLIA DA CORREIA DENTADA")]
    fake_llm = FakeLLMProvider(
        {"modo": "escolher", "part_id_escolhido": 2, "justificativa": "melhor capitalização"}
    )

    decisao = arbitrar_nome(registros, llm=fake_llm)

    assert decisao.valor == "POLIA DA CORREIA DENTADA"
    assert decisao.fonte == "julgamento_modelo"
    assert decisao.origem_id == 2


def test_sintetiza_novo_nome(fazer_registro):
    registros = [fazer_registro(1, "polia  da correia"), fazer_registro(2, "POLIA DA CORREIA")]
    fake_llm = FakeLLMProvider(
        {
            "modo": "sintetizar",
            "nome_sintetizado": "POLIA DA CORREIA",
            "justificativa": "nenhum candidato tinha capitalização e espaçamento consistentes",
        }
    )

    decisao = arbitrar_nome(registros, llm=fake_llm)

    assert decisao.valor == "POLIA DA CORREIA"
    assert decisao.origem_id is None


def test_rejeita_part_id_fora_do_subcluster(fazer_registro):
    registros = [fazer_registro(1, "polia da correia"), fazer_registro(2, "POLIA DA CORREIA")]
    fake_llm = FakeLLMProvider({"modo": "escolher", "part_id_escolhido": 999, "justificativa": "x"})

    with pytest.raises(ArbitragemInvalidaError):
        arbitrar_nome(registros, llm=fake_llm)


def test_rejeita_sintetizar_sem_nome(fazer_registro):
    registros = [fazer_registro(1, "polia da correia"), fazer_registro(2, "POLIA DA CORREIA")]
    fake_llm = FakeLLMProvider({"modo": "sintetizar", "justificativa": "x"})

    with pytest.raises(ArbitragemInvalidaError):
        arbitrar_nome(registros, llm=fake_llm)


def test_nomes_divergentes_confirmado_usa_nome_da_web_sem_chamar_llm(fazer_registro):
    # SUPERIOR/INFERIOR é conflito real (nenhum nome é superconjunto do outro) —
    # ver tests/test_divergencia.py pro que conta como divergência de verdade.
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]

    class LLMNuncaChamado:
        def gerar_json(self, *args, **kwargs):
            raise AssertionError("juiz interno não deveria ser chamado quando os nomes divergem")

    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(
            status="confirmado", nome_sugerido="PIVO INFERIOR", justificativa="2 de 3 fontes confirmam",
            fontes=[FonteWeb(url="https://exemplo.com", nome_encontrado="PIVO INFERIOR")],
        )
    )

    decisao = arbitrar_nome(registros, llm=LLMNuncaChamado(), verificar_web=fake_web)

    assert decisao.fonte == "verificacao_web"
    assert decisao.valor == "PIVO INFERIOR"
    assert decisao.origem_id == 2
    assert decisao.escalado_humano is False
    assert fake_web.chamadas == [("83061", "CITROEN", ["PIVO INFERIOR", "PIVO SUPERIOR"])]


def test_nomes_divergentes_confirmado_casa_origem_id_por_tokens_normalizados(fazer_registro):
    # F-07/T-10: a web devolve o nome acentuado/reordenado, que não bate byte-a-byte
    # com o candidato — origem_id deve casar por tokens normalizados mesmo assim.
    registros = [
        fazer_registro(1, "PIVO SUPERIOR"),
        fazer_registro(2, "PIVÔ DE SUSPENSÃO INFERIOR"),
    ]
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(
            status="confirmado",
            nome_sugerido="inferior suspensao de pivo",  # sem acento, ordem trocada, minúsculo
            justificativa="fontes convergem",
            fontes=[],
        )
    )

    decisao = arbitrar_nome(registros, llm=FakeLLMProvider({}), verificar_web=fake_web)

    assert decisao.fonte == "verificacao_web"
    assert decisao.origem_id == 2  # casou com "PIVÔ DE SUSPENSÃO INFERIOR" via tokens


def test_nomes_divergentes_confirmado_sem_candidato_equivalente_origem_id_none(fazer_registro):
    # Nome confirmado que não corresponde a nenhum candidato (tokens diferentes) -> None.
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(status="confirmado", nome_sugerido="PIVO CENTRAL TRASEIRO", justificativa="ok", fontes=[])
    )

    decisao = arbitrar_nome(registros, llm=FakeLLMProvider({}), verificar_web=fake_web)

    assert decisao.fonte == "verificacao_web"
    assert decisao.valor == "PIVO CENTRAL TRASEIRO"
    assert decisao.origem_id is None


def test_nomes_divergentes_inconclusivo_escala_para_humano(fazer_registro):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(status="inconclusivo", nome_sugerido=None, justificativa="fontes conflitantes", fontes=[])
    )

    decisao = arbitrar_nome(registros, llm=FakeLLMProvider({}), verificar_web=fake_web)

    assert decisao.fonte == "verificacao_web"
    assert decisao.valor is None
    assert decisao.escalado_humano is True


def test_memoria_de_intervencao_resolve_nome_automaticamente(fazer_registro, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    store.registrar_intervencao(
        titulo="Conflito de posição no nome",
        caso_episodico="caso anterior",
        condicao="Quando nomes divergem por qualificadores de posição",
        resolucao="Escolher o candidato confirmado pelo contexto de posição",
        criado_por="leo",
        sinais_busca="superior inferior conflito posicao",
        campo="nome",
    )
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(status="inconclusivo", nome_sugerido=None, justificativa="fontes conflitantes", fontes=[])
    )
    llm = FakeLLMProvider({"modo": "escolher", "part_id_escolhido": 2, "justificativa": "regra aplicada"})

    decisao = arbitrar_nome(
        registros, llm=llm, rule_store=store, verificar_web=fake_web, limiar_intervencao=0.20
    )

    assert decisao.fonte == "intervencao_humana"
    assert decisao.valor == "PIVO INFERIOR"
    assert decisao.origem_id == 2
    assert decisao.escalado_humano is False


def test_sem_memoria_callback_humano_salva_e_aplica_nome(fazer_registro, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(status="inconclusivo", nome_sugerido=None, justificativa="sem confirmação", fontes=[])
    )
    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return RespostaIntervencao(
            resposta_humana="Neste catálogo, a posição inferior é a correta.",
            regra=RegraProposta(
                titulo="Qualificador inferior confirmado",
                condicao="Quando a busca web não resolve um conflito entre superior e inferior",
                resolucao="Escolher o nome inferior quando o contexto do catálogo indicar essa posição",
            ),
            valor="PIVO INFERIOR",
            origem_id=2,
            criado_por="operador-teste",
        )

    decisao = arbitrar_nome(
        registros, llm=FakeLLMProvider({}), rule_store=store,
        verificar_web=fake_web, pedir_intervencao=pedir,
    )

    assert len(pedidos) == 1
    assert pedidos[0].ponto == "nome"
    assert decisao.fonte == "intervencao_humana"
    assert decisao.valor == "PIVO INFERIOR"
    regras = store.listar_intervencoes(campo="nome")
    assert len(regras) == 1
    assert regras[0].titulo == "Qualificador inferior confirmado"
    assert "Neste catálogo" in (regras[0].caso_episodico or "")


def test_aviso_emitido_antes_e_depois_da_verificacao_web(fazer_registro):
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(status="confirmado", nome_sugerido="PIVO INFERIOR", justificativa="ok", fontes=[])
    )
    avisos = []

    arbitrar_nome(registros, llm=FakeLLMProvider({}), on_aviso=avisos.append, verificar_web=fake_web)

    assert len(avisos) == 2
    assert "83061" in avisos[0] and "PIVO" in avisos[0]
    assert "confirmado" in avisos[1]


def test_nomes_convergentes_nao_aciona_verificacao_web(fazer_registro):
    registros = [fazer_registro(1, "polia da correia"), fazer_registro(2, "POLIA DA CORREIA")]
    fake_web = FakeVerificadorWeb(ResultadoVerificacao(status="confirmado", nome_sugerido="X", justificativa="", fontes=[]))
    fake_llm = FakeLLMProvider({"modo": "escolher", "part_id_escolhido": 2, "justificativa": "ok"})

    arbitrar_nome(registros, llm=fake_llm, verificar_web=fake_web)

    assert fake_web.chamadas == []


def test_nome_mais_detalhado_nao_aciona_verificacao_web(fazer_registro):
    # Regressão: "PLUG ELETRÔNICO ÁGUA" vs "PLUG ELETRÔNICO ÁGUA 24V MTE" não é
    # ambiguidade real — o segundo só tem detalhe a mais, nenhuma aplicação
    # conflitante — deve ir pro juiz interno, não acionar a verificação web.
    registros = [fazer_registro(1, "PLUG ELETRÔNICO ÁGUA"), fazer_registro(2, "PLUG ELETRÔNICO ÁGUA 24V MTE")]
    fake_web = FakeVerificadorWeb(ResultadoVerificacao(status="confirmado", nome_sugerido="X", justificativa="", fontes=[]))
    fake_llm = FakeLLMProvider({"modo": "escolher", "part_id_escolhido": 2, "justificativa": "mais completo"})

    decisao = arbitrar_nome(registros, llm=fake_llm, verificar_web=fake_web)

    assert fake_web.chamadas == []
    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == "PLUG ELETRÔNICO ÁGUA 24V MTE"


def test_injeta_regras_de_qualidade_nome_no_prompt(fazer_registro, tmp_path):
    rule_store = RuleStore(tmp_path / "test.db")
    rule_store.registrar(
        categoria="qualidade_nome",
        condicao="nomes de kit devem começar com 'KIT'",
        resolucao="prefixar com 'KIT ' quando aplicável",
        criado_por="leo",
    )
    registros = [fazer_registro(1, "kit correia dentada"), fazer_registro(2, "KIT CORREIA DENTADA")]

    prompts_recebidos = []

    class FakeLLMComCaptura:
        def gerar_json(self, system, user, json_schema, schema_name="output"):
            prompts_recebidos.append(user)
            return {"modo": "escolher", "part_id_escolhido": 2, "justificativa": "já segue a convenção"}

    arbitrar_nome(registros, llm=FakeLLMComCaptura(), rule_store=rule_store)

    assert "nomes de kit devem começar com 'KIT'" in prompts_recebidos[0]



# ---------------------------------------------------------------------------
# Tarefa 12.4 — 1º desempate por provedor em arbitrar_nome (Requirement 10).
#
# Injeta `arbitrar_por_provedor` como fake e usa brand_id não-nulo. Quando o
# provedor decide (regra_confiabilidade) ou consenso (sem_conflito), a cascata
# de nome NÃO deve rodar — por isso o llm/verificar_web destes testes LEVANTAM
# se forem chamados, provando o curto-circuito (R10.2, R10.5). Quando o provedor
# é inconclusivo ou lança, a cascata existente roda como hoje (R10.3, R10.4).
# ---------------------------------------------------------------------------


class _LLMExplode:
    """LLM que falha o teste se o juiz textual for acionado."""

    def gerar_json(self, *args, **kwargs):
        raise AssertionError("juiz textual não deveria ser chamado — o provedor curto-circuita a cascata")


class _WebExplode:
    """verificar_web que falha o teste se a verificação web for acionada."""

    def __call__(self, *args, **kwargs):
        raise AssertionError("verificação web não deveria ser chamada — o provedor curto-circuita a cascata")


def _provedor_fixo(decisao):
    """Fábrica de fake de arbitrar_por_provedor que sempre devolve `decisao` e
    registra as chamadas recebidas (para checar o campo e o brand_id passados)."""
    chamadas: list[tuple] = []

    def _fake(registros, campo, brand_id):
        chamadas.append((tuple(r.id for r in registros), campo, brand_id))
        return decisao

    _fake.chamadas = chamadas
    return _fake


def test_provedor_decide_nome_sem_chamar_llm_nem_web(fazer_registro):
    # R10.2: provedor devolve Arbitragem_Bem_Sucedida (regra_confiabilidade,
    # valor definido, escalado_humano=False) → decisão final, sem tocar juiz
    # textual nem verificação web.
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="PIVO INFERIOR",
        justificativa="Fonte de confiabilidade 'alta' (provider_id=7) decide sozinha o campo 'name'.",
        fonte="regra_confiabilidade",
        escalado_humano=False,
        origem_id=2,
        confianca="alta",
    )
    fake_provedor = _provedor_fixo(decisao_provedor)

    decisao = arbitrar_nome(
        registros,
        llm=_LLMExplode(),
        verificar_web=_WebExplode(),
        brand_id=1,
        arbitrar_por_provedor=fake_provedor,
    )

    assert decisao is decisao_provedor
    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.valor == "PIVO INFERIOR"
    assert decisao.origem_id == 2
    assert decisao.escalado_humano is False
    # o provedor foi consultado para o campo "name" com o brand_id fornecido
    assert fake_provedor.chamadas == [((1, 2), "name", 1)]


def test_provedor_sem_conflito_e_decisao_final(fazer_registro):
    # R10.5: provedor devolve sem_conflito (registro único / consenso) → decisão
    # final, sem juiz textual nem verificação web.
    registros = [fazer_registro(1, "POLIA DA CORREIA"), fazer_registro(2, "POLIA DA CORREIA")]
    decisao_provedor = DecisaoCampo(
        campo="name",
        valor="POLIA DA CORREIA",
        justificativa="Todos os registros concordam no valor.",
        fonte="sem_conflito",
        escalado_humano=False,
    )
    fake_provedor = _provedor_fixo(decisao_provedor)

    decisao = arbitrar_nome(
        registros,
        llm=_LLMExplode(),
        verificar_web=_WebExplode(),
        brand_id=1,
        arbitrar_por_provedor=fake_provedor,
    )

    assert decisao is decisao_provedor
    assert decisao.fonte == "sem_conflito"
    assert decisao.escalado_humano is False
    assert fake_provedor.chamadas == [((1, 2), "name", 1)]


def _provedor_inconclusivo() -> DecisaoCampo:
    return DecisaoCampo(
        campo="name",
        valor=None,
        justificativa="Arbitragem por confiabilidade inconclusiva: empate no topo.",
        fonte="escalado_humano",
        escalado_humano=True,
        origem_id=None,
    )


def test_provedor_inconclusivo_cai_na_cascata_juiz_textual(fazer_registro):
    # R10.3: provedor inconclusivo → NÃO retorna a decisão do provedor; cai na
    # cascata existente. Para nomes CONVERGENTES (só diferem em caixa), a cascata
    # vai ao juiz textual e retorna julgamento_modelo — verificar_web não é tocado.
    registros = [fazer_registro(1, "polia da correia"), fazer_registro(2, "POLIA DA CORREIA")]
    fake_provedor = _provedor_fixo(_provedor_inconclusivo())
    fake_llm = FakeLLMProvider(
        {"modo": "escolher", "part_id_escolhido": 2, "justificativa": "melhor capitalização"}
    )
    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(status="confirmado", nome_sugerido="X", justificativa="", fontes=[])
    )

    decisao = arbitrar_nome(
        registros,
        llm=fake_llm,
        verificar_web=fake_web,
        brand_id=1,
        arbitrar_por_provedor=fake_provedor,
    )

    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == "POLIA DA CORREIA"
    assert decisao.origem_id == 2
    assert fake_provedor.chamadas == [((1, 2), "name", 1)]
    assert fake_web.chamadas == []  # nomes convergentes não acionam a web


def test_provedor_inconclusivo_cai_na_cascata_verificacao_web(fazer_registro):
    # R10.3: provedor inconclusivo → cai na cascata. Para nomes DIVERGENTES
    # (SUPERIOR/INFERIOR), a cascata aciona a verificação web como hoje.
    registros = [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]
    fake_provedor = _provedor_fixo(_provedor_inconclusivo())

    class LLMNuncaChamado:
        def gerar_json(self, *args, **kwargs):
            raise AssertionError("juiz textual não deveria ser chamado quando os nomes divergem")

    fake_web = FakeVerificadorWeb(
        ResultadoVerificacao(
            status="confirmado", nome_sugerido="PIVO INFERIOR", justificativa="2 de 3 fontes confirmam",
            fontes=[FonteWeb(url="https://exemplo.com", nome_encontrado="PIVO INFERIOR")],
        )
    )

    decisao = arbitrar_nome(
        registros,
        llm=LLMNuncaChamado(),
        verificar_web=fake_web,
        brand_id=1,
        arbitrar_por_provedor=fake_provedor,
    )

    assert decisao.fonte == "verificacao_web"
    assert decisao.valor == "PIVO INFERIOR"
    assert decisao.origem_id == 2
    assert fake_provedor.chamadas == [((1, 2), "name", 1)]
    assert fake_web.chamadas == [("83061", "CITROEN", ["PIVO INFERIOR", "PIVO SUPERIOR"])]


def test_provedor_erro_cai_na_cascata_com_aviso(fazer_registro):
    # R10.4: provedor LANÇA exceção → tratado como inconclusivo; a cascata roda
    # (aqui, nomes convergentes → juiz textual) e on_aviso recebe a degradação.
    registros = [fazer_registro(1, "polia da correia"), fazer_registro(2, "POLIA DA CORREIA")]

    def provedor_que_explode(registros, campo, brand_id):
        raise RuntimeError("falha simulada de banco")

    fake_llm = FakeLLMProvider(
        {"modo": "escolher", "part_id_escolhido": 2, "justificativa": "melhor capitalização"}
    )
    avisos: list[str] = []

    decisao = arbitrar_nome(
        registros,
        llm=fake_llm,
        brand_id=1,
        arbitrar_por_provedor=provedor_que_explode,
        on_aviso=avisos.append,
    )

    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == "POLIA DA CORREIA"
    # a degradação foi sinalizada via on_aviso, sem interromper a decisão
    assert any("inconclusiva por erro" in a and "falha simulada de banco" in a for a in avisos)


def test_brand_id_none_pula_primeiro_desempate(fazer_registro):
    # R10.1: com brand_id=None, o 1º desempate por provedor é pulado e a cascata
    # roda como no legado — o fake do provedor NÃO deve ser chamado.
    registros = [fazer_registro(1, "polia da correia"), fazer_registro(2, "POLIA DA CORREIA")]

    def provedor_nunca_chamado(registros, campo, brand_id):
        raise AssertionError("com brand_id=None o provedor não deve ser chamado")

    fake_llm = FakeLLMProvider(
        {"modo": "escolher", "part_id_escolhido": 2, "justificativa": "melhor capitalização"}
    )

    decisao = arbitrar_nome(
        registros,
        llm=fake_llm,
        brand_id=None,
        arbitrar_por_provedor=provedor_nunca_chamado,
    )

    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == "POLIA DA CORREIA"
