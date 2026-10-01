"""Testes unitários de `arbitration/nome.py` com `contexto_web`
(spec html-extract-on-web-search, tarefa 6.4).

Cobrem:
- caracterização dos textos do caminho com pesquisa ligada (byte a byte);
- ordem verificação → coleta → cascata, com porta de coleta fake;
- exceção do verificador propaga sem ativação publicada;
- caminho com pesquisa desligada: com e sem regra acima do limiar, com e sem
  `pedir_intervencao`; prompt da regra com a flag; método inválido não levanta.

Requirements: 5.5, 6.1, 10.5, 10.6, 10.7, 10.8, 10.9, 10.11, 10.12, 10.13.
"""

from __future__ import annotations

import pytest

import arbitration.nome as nome_mod
from arbitration.models import DecisaoCampo
from arbitration.nome import (
    _decidir_nome_com_regra,
    arbitrar_nome,
    arbitrar_nome_recuperacao,
)
from arbitration.pesquisa_web import (
    CONTEXTO_WEB_DESLIGADA,
    JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
    JUSTIFICATIVA_INTERVENCAO_DESLIGADA,
    MOTIVO_PESQUISA_DESLIGADA,
    AtivacaoPesquisa,
    ContextoPesquisaWeb,
)
from db.rule_store import RuleStore
from loop.tracing import TraceCollector
from memory.models import RegraProposta, RespostaIntervencao
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from verification.models import ResultadoVerificacao
from verification.resultados_estruturados import VerificacaoComResultados
from verification.selector import MetodoVerificacaoInvalidoError
from verification.serper_client import ResultadoOrganico

NOMES = ["PIVO INFERIOR", "PIVO SUPERIOR"]  # ordenados, como `_arbitrar_nome_via_web`
FRASES_PROIBIDAS_DESLIGADA = (
    "acionando verificação web",
    "Verificação web concluída",
    "A verificação web não foi suficiente",
    "após verificação web inconclusiva",
)
INCONCLUSIVO = ResultadoVerificacao(
    status="inconclusivo", nome_sugerido=None, justificativa="fontes conflitantes", fontes=[]
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class Linha:
    """Registro ordenado de acontecimentos compartilhado pelos fakes."""

    def __init__(self) -> None:
        self.eventos: list[str] = []


class PortaFake:
    """PortaColeta fake: registra as ativações e a ordem da chamada."""

    def __init__(self, linha: Linha | None = None) -> None:
        self.linha = linha
        self.chamadas: list[tuple[AtivacaoPesquisa, ContextoPesquisaWeb, object]] = []

    def processar(self, ativacao, *, contexto, trace):
        if self.linha is not None:
            self.linha.eventos.append("coleta")
        self.chamadas.append((ativacao, contexto, trace))
        return object()


class VerificadorSimples:
    """Verificador_Web sem o protocolo de Resultados_Estruturados."""

    def __init__(self, resultado: ResultadoVerificacao, linha: Linha | None = None) -> None:
        self.resultado = resultado
        self.linha = linha
        self.chamadas: list[tuple] = []

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        if self.linha is not None:
            self.linha.eventos.append("verificacao")
        self.chamadas.append((codigo, marca, list(nomes_conflitantes)))
        return self.resultado


class VerificadorComProtocolo(VerificadorSimples):
    """Verificador_Web que implementa `verificar_com_resultados`."""

    def __init__(self, resultado, resultados_pesquisa, linha: Linha | None = None) -> None:
        super().__init__(resultado, linha)
        self.resultados_pesquisa = resultados_pesquisa

    def verificar_com_resultados(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        resultado = self(codigo, marca, nomes_conflitantes, on_evento=on_evento)
        return VerificacaoComResultados(resultado, self.resultados_pesquisa)


class VerificadorProibido:
    def __init__(self) -> None:
        self.chamadas = 0

    def __call__(self, *args, **kwargs):
        self.chamadas += 1
        raise AssertionError("o Verificador_Web não deveria ser chamado com a pesquisa desligada")


class LLMRoteirizado:
    """LLM fake que registra os prompts e devolve uma resposta fixa."""

    def __init__(self, resposta: dict | None = None, linha: Linha | None = None) -> None:
        self.resposta = resposta or {"modo": "escolher", "part_id_escolhido": 2, "justificativa": "regra"}
        self.linha = linha
        self.prompts: list[str] = []

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        if self.linha is not None:
            self.linha.eventos.append("llm")
        self.prompts.append(user)
        return self.resposta


class LLMProibido:
    def gerar_json(self, *args, **kwargs):
        raise AssertionError("LLM não deveria ser chamado")


def _resposta_humana(valor="PIVO INFERIOR", origem_id=2) -> RespostaIntervencao:
    return RespostaIntervencao(
        resposta_humana="Neste catálogo, a posição inferior é a correta.",
        regra=RegraProposta(
            titulo="Qualificador inferior confirmado",
            condicao="Quando há conflito entre superior e inferior",
            resolucao="Escolher o nome inferior quando o contexto indicar",
        ),
        valor=valor,
        origem_id=origem_id,
        criado_por="operador-teste",
    )


def _gravar_regra_desligada(store: RuleStore) -> None:
    """Regra real cujos sinais coincidem com o texto de busca do pedido desligado."""
    store.registrar_intervencao(
        titulo="Posição inferior prevalece",
        caso_episodico="caso anterior",
        condicao="Quando nomes divergem entre superior e inferior",
        resolucao="Escolher o candidato inferior",
        criado_por="leo",
        sinais_busca="pivo superior inferior pesquisa desligada operador",
        campo="nome",
    )


def _gravar_regra_ligada(store: RuleStore) -> None:
    store.registrar_intervencao(
        titulo="Conflito de posição no nome",
        caso_episodico="caso anterior",
        condicao="Quando nomes divergem por qualificadores de posição",
        resolucao="Escolher o candidato confirmado pelo contexto de posição",
        criado_por="leo",
        sinais_busca="superior inferior conflito posicao",
        campo="nome",
    )


@pytest.fixture
def registros(fazer_registro):
    return [fazer_registro(1, "PIVO SUPERIOR"), fazer_registro(2, "PIVO INFERIOR")]


def _desligado(porta=None, avisos=None) -> ContextoPesquisaWeb:
    return ContextoPesquisaWeb(
        pesquisa_habilitada=False,
        coleta=porta,
        canal_avisos=avisos.append if avisos is not None else None,
    )


# ---------------------------------------------------------------------------
# Caminho com pesquisa ligada: caracterização dos textos (Req 5.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("contexto", [None, "com_porta"])
def test_textos_do_caminho_ligado_sao_os_de_antes(registros, contexto):
    porta = PortaFake()
    contexto_web = None if contexto is None else ContextoPesquisaWeb(coleta=porta)
    avisos: list[str] = []

    decisao = arbitrar_nome(
        registros, llm=LLMProibido(), on_aviso=avisos.append,
        verificar_web=VerificadorSimples(INCONCLUSIVO), contexto_web=contexto_web,
    )

    assert avisos == [
        f"Nomes divergentes para 83061 (CITROEN): {NOMES} — acionando verificação web antes de decidir.",
        "Verificação web concluída: status=inconclusivo, nome_sugerido=None",
    ]
    assert decisao == DecisaoCampo(
        campo="name", valor=None, justificativa="fontes conflitantes",
        fonte="verificacao_web", escalado_humano=True, confianca="baixa", evidencias=[],
    )


def test_justificativa_de_intervencao_do_caminho_ligado_inalterada(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return _resposta_humana()

    decisao = arbitrar_nome(
        registros, llm=LLMProibido(), rule_store=store,
        verificar_web=VerificadorSimples(INCONCLUSIVO), pedir_intervencao=pedir,
        contexto_web=ContextoPesquisaWeb(coleta=PortaFake()),
    )

    assert decisao.justificativa == (
        "Nome decidido por intervenção humana após verificação web inconclusiva."
    )
    assert decisao.fonte == "intervencao_humana"
    assert pedidos[0].motivo == "fontes conflitantes"
    assert pedidos[0].contexto_web == "status=inconclusivo; fontes conflitantes"


def test_prompt_da_regra_no_caminho_ligado_inalterado(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    _gravar_regra_ligada(store)
    llm = LLMRoteirizado()

    decisao = arbitrar_nome(
        registros, llm=llm, rule_store=store, verificar_web=VerificadorSimples(INCONCLUSIVO),
        limiar_intervencao=0.20, contexto_web=ContextoPesquisaWeb(coleta=PortaFake()),
    )

    assert decisao.fonte == "intervencao_humana"
    assert llm.prompts[0].startswith(
        "A verificação web não foi suficiente, mas uma intervenção humana anterior "
        "ensinou a regra abaixo. Use-a como contexto"
    )


# ---------------------------------------------------------------------------
# Ordem verificação → coleta → cascata (Req 6.1)
# ---------------------------------------------------------------------------


def test_ordem_verificacao_coleta_cascata(registros, tmp_path):
    linha = Linha()
    porta = PortaFake(linha)
    trace = TraceCollector()
    organicos = (ResultadoOrganico(title="T", link="https://a.example/x", snippet="s", position=1),)
    verificador = VerificadorComProtocolo(INCONCLUSIVO, organicos, linha)

    def aviso(msg):
        linha.eventos.append("aviso:" + msg.split(":")[0])

    def pedir(pedido):
        linha.eventos.append("intervencao")
        return _resposta_humana()

    arbitrar_nome(
        registros, llm=LLMProibido(), rule_store=RuleStore(tmp_path / "m.db"),
        on_aviso=aviso, verificar_web=verificador, pedir_intervencao=pedir, trace=trace,
        contexto_web=ContextoPesquisaWeb(coleta=porta),
    )

    ordem = [e for e in linha.eventos if not e.startswith("aviso:Intervenção")]
    assert ordem == [
        "aviso:Nomes divergentes para 83061 (CITROEN)",
        "verificacao",
        "aviso:Verificação web concluída",
        "coleta",
        "intervencao",
    ]
    # O evento de verificação é registrado antes da consulta à memória.
    nomes_trace = [e.nome for e in trace.eventos()]
    assert nomes_trace.index("verificar_nomenclatura_peca") < nomes_trace.index("consultar_memoria")

    (ativacao, contexto, trace_recebido), = porta.chamadas
    assert ativacao == AtivacaoPesquisa(
        codigo="83061", marca="CITROEN", nomes_conflitantes=tuple(NOMES),
        metodo="injetado", situacao="realizada", resultados=organicos,
    )
    assert trace_recebido is trace
    assert contexto.coleta is porta


@pytest.mark.parametrize(
    ("verificador", "situacao", "resultados"),
    [
        (VerificadorSimples(INCONCLUSIVO), "sem_estruturados", None),
        (VerificadorComProtocolo(INCONCLUSIVO, None), "nao_realizada", None),
        (VerificadorComProtocolo(INCONCLUSIVO, ()), "realizada", ()),
    ],
)
def test_situacao_da_ativacao_conforme_o_verificador(registros, verificador, situacao, resultados):
    porta = PortaFake()

    arbitrar_nome(
        registros, llm=LLMProibido(), verificar_web=verificador,
        contexto_web=ContextoPesquisaWeb(coleta=porta),
    )

    (ativacao, _, _), = porta.chamadas
    assert ativacao.situacao == situacao
    assert ativacao.resultados == resultados


def test_coleta_nao_altera_decisao_confirmada(registros):
    confirmado = ResultadoVerificacao(
        status="confirmado", nome_sugerido="PIVO INFERIOR", justificativa="ok", fontes=[]
    )
    sem_contexto = arbitrar_nome(registros, llm=LLMProibido(), verificar_web=VerificadorSimples(confirmado))
    porta = PortaFake()
    com_porta = arbitrar_nome(
        registros, llm=LLMProibido(), verificar_web=VerificadorSimples(confirmado),
        contexto_web=ContextoPesquisaWeb(coleta=porta),
    )

    assert com_porta == sem_contexto
    assert len(porta.chamadas) == 1


def test_metodo_do_seletor_e_repassado_a_ativacao(registros, monkeypatch):
    verificador = VerificadorSimples(INCONCLUSIVO)
    monkeypatch.setattr(nome_mod, "resolver_verificacao_web", lambda: verificador)
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "  SERPER ")
    porta = PortaFake()

    arbitrar_nome(registros, llm=LLMProibido(), contexto_web=ContextoPesquisaWeb(coleta=porta))

    assert porta.chamadas[0][0].metodo == "serper"
    assert len(verificador.chamadas) == 1


def test_excecao_do_verificador_propaga_sem_ativacao(registros):
    porta = PortaFake()

    class Falha(RuntimeError):
        pass

    def verificador(*args, **kwargs):
        raise Falha("boom")

    with pytest.raises(Falha):
        arbitrar_nome(
            registros, llm=LLMProibido(), verificar_web=verificador,
            contexto_web=ContextoPesquisaWeb(coleta=porta),
        )

    assert porta.chamadas == []


def test_metodo_invalido_com_pesquisa_ligada_levanta_como_antes(registros, monkeypatch):
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "bing")
    with pytest.raises(MetodoVerificacaoInvalidoError):
        arbitrar_nome(registros, llm=LLMProibido(), contexto_web=ContextoPesquisaWeb(coleta=PortaFake()))


# ---------------------------------------------------------------------------
# Caminho com pesquisa desligada (Req 10)
# ---------------------------------------------------------------------------


def test_desligada_metodo_invalido_nao_levanta_nem_resolve_seletor(registros, monkeypatch):
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "valor-invalido")
    chamadas_seletor = []

    def seletor(*args, **kwargs):
        chamadas_seletor.append(1)
        raise AssertionError("resolver_verificacao_web não deveria ser chamado")

    monkeypatch.setattr(nome_mod, "resolver_verificacao_web", seletor)
    verificador = VerificadorProibido()

    decisao = arbitrar_nome(
        registros, llm=LLMProibido(), verificar_web=verificador, contexto_web=_desligado(PortaFake()),
    )

    assert decisao.escalado_humano is True
    assert chamadas_seletor == []
    assert verificador.chamadas == 0


def test_desligada_aviso_unico_trace_e_ativacao(registros):
    avisos: list[str] = []
    trace = TraceCollector()
    porta = PortaFake()
    contexto = _desligado(porta)

    arbitrar_nome(
        registros, llm=LLMProibido(), on_aviso=avisos.append, trace=trace, contexto_web=contexto,
    )

    assert avisos == [
        f"Nomes divergentes para 83061 (CITROEN): {NOMES} — pesquisa web desligada pelo "
        "operador; seguindo para memória/intervenção."
    ]
    eventos = [e for e in trace.eventos() if e.nome == "verificar_nomenclatura_peca"]
    assert len(eventos) == 1
    evento = eventos[0]
    assert evento.fase == "tool"
    assert evento.status == "desligada"
    assert evento.entrada == {"codigo": "83061", "marca": "CITROEN", "nomes_conflitantes": NOMES}
    assert evento.saida == {"motivo": "pesquisa_desligada"}
    assert evento.justificativa == MOTIVO_PESQUISA_DESLIGADA

    (ativacao, contexto_recebido, trace_recebido), = porta.chamadas
    assert ativacao == AtivacaoPesquisa(
        codigo="83061", marca="CITROEN", nomes_conflitantes=tuple(NOMES),
        metodo="desligada", situacao="desligada", resultados=None,
    )
    assert contexto_recebido is contexto
    assert trace_recebido is trace


def test_desligada_sem_regra_sem_ponte_escala(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    avisos: list[str] = []

    decisao = arbitrar_nome(
        registros, llm=LLMProibido(), rule_store=store, on_aviso=avisos.append,
        contexto_web=_desligado(PortaFake()),
    )

    assert decisao == DecisaoCampo(
        campo="name", valor=None, justificativa=JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA,
        fonte="escalado_humano", escalado_humano=True, confianca="baixa",
        evidencias=[{"tipo": "pesquisa_web_desligada"}],
    )
    assert "pesquisa web desligada pelo operador" in decisao.justificativa
    assert store.listar_intervencoes(campo="nome") == []
    for texto in [*avisos, decisao.justificativa]:
        assert not any(f in texto for f in FRASES_PROIBIDAS_DESLIGADA)


def test_desligada_sem_regra_com_ponte_pede_e_grava(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return _resposta_humana()

    decisao = arbitrar_nome(
        registros, llm=LLMProibido(), rule_store=store, pedir_intervencao=pedir,
        contexto_web=_desligado(PortaFake()),
    )

    (pedido,) = pedidos
    assert pedido.ponto == "nome"
    assert pedido.grupo_ref == "83061:CITROEN"
    assert pedido.search_ref == "83061"
    assert pedido.marca == "CITROEN"
    assert pedido.nomes_conflitantes == NOMES
    assert pedido.motivo == MOTIVO_PESQUISA_DESLIGADA
    assert pedido.contexto_web == CONTEXTO_WEB_DESLIGADA
    assert pedido.membro_ids == [1, 2]
    assert pedido.candidatos == [(1, "PIVO SUPERIOR"), (2, "PIVO INFERIOR")]

    assert decisao.fonte == "intervencao_humana"
    assert decisao.valor == "PIVO INFERIOR"
    assert decisao.origem_id == 2
    assert decisao.justificativa == JUSTIFICATIVA_INTERVENCAO_DESLIGADA
    regras = store.listar_intervencoes(campo="nome")
    assert [r.titulo for r in regras] == ["Qualificador inferior confirmado"]
    assert CONTEXTO_WEB_DESLIGADA in (regras[0].caso_episodico or "")


def test_desligada_ponte_sem_valor_usa_prompt_sem_afirmar_web(registros, tmp_path):
    llm = LLMRoteirizado()

    decisao = arbitrar_nome(
        registros, llm=llm, rule_store=RuleStore(tmp_path / "m.db"),
        pedir_intervencao=lambda pedido: _resposta_humana(valor=None, origem_id=None),
        contexto_web=_desligado(PortaFake()),
    )

    assert decisao.fonte == "intervencao_humana"
    assert decisao.valor == "PIVO INFERIOR"
    assert llm.prompts[0].startswith(
        "A pesquisa web foi desligada pelo operador nesta execução; uma intervenção "
        "humana anterior ensinou a regra abaixo. "
    )
    assert not any(f in llm.prompts[0] for f in FRASES_PROIBIDAS_DESLIGADA)


def test_desligada_com_regra_acima_do_limiar_aplica_regra(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    _gravar_regra_desligada(store)
    llm = LLMRoteirizado()
    avisos: list[str] = []
    trace = TraceCollector()

    def pedir(pedido):
        raise AssertionError("com regra acima do limiar não deve pedir intervenção")

    decisao = arbitrar_nome(
        registros, llm=llm, rule_store=store, on_aviso=avisos.append, trace=trace,
        pedir_intervencao=pedir, limiar_intervencao=0.45, contexto_web=_desligado(PortaFake()),
    )

    assert decisao.fonte == "intervencao_humana"
    assert decisao.valor == "PIVO INFERIOR"
    assert decisao.justificativa.startswith("Regra de intervenção recuperada (score ")
    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert prompt.startswith("A pesquisa web foi desligada pelo operador nesta execução;")
    assert "Posição inferior prevalece" in prompt
    memoria = [e for e in trace.eventos() if e.nome == "consultar_memoria"]
    assert memoria and memoria[0].saida["encontrada"] is True
    for texto in [*avisos, prompt, decisao.justificativa]:
        assert not any(f in texto for f in FRASES_PROIBIDAS_DESLIGADA)


def test_desligada_regra_abaixo_do_limiar_segue_para_ponte(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    _gravar_regra_desligada(store)
    pedidos = []

    def pedir(pedido):
        pedidos.append(pedido)
        return _resposta_humana()

    decisao = arbitrar_nome(
        registros, llm=LLMProibido(), rule_store=store, pedir_intervencao=pedir,
        limiar_intervencao=1.0, contexto_web=_desligado(PortaFake()),
    )

    assert len(pedidos) == 1
    assert decisao.justificativa == JUSTIFICATIVA_INTERVENCAO_DESLIGADA


def test_desligada_sem_rule_store_sem_ponte_escala(registros):
    decisao = arbitrar_nome(registros, llm=LLMProibido(), contexto_web=_desligado())

    assert decisao.fonte == "escalado_humano"
    assert decisao.evidencias == [{"tipo": "pesquisa_web_desligada"}]


def test_desligada_na_recuperacao_nao_chama_web(registros, monkeypatch):
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "invalido")
    monkeypatch.setattr(
        nome_mod, "resolver_verificacao_web",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("não deveria resolver")),
    )
    verificador = VerificadorProibido()
    porta = PortaFake()
    avisos: list[str] = []

    def provedor_inconclusivo(regs, campo, brand_id):
        return None

    decisao = arbitrar_nome_recuperacao(
        registros, llm=LLMProibido(), brand_id=1, on_aviso=avisos.append,
        verificar_web=verificador, arbitrar_por_provedor=provedor_inconclusivo,
        contexto_web=_desligado(porta),
    )

    assert verificador.chamadas == 0
    assert decisao.fonte == "escalado_humano"
    assert decisao.justificativa == JUSTIFICATIVA_ESCALONAMENTO_DESLIGADA
    assert porta.chamadas[0][0].situacao == "desligada"
    assert len(avisos) == 1 and "pesquisa web desligada pelo operador" in avisos[0]


# ---------------------------------------------------------------------------
# `_decidir_nome_com_regra` com a flag (Req 10.11)
# ---------------------------------------------------------------------------


def test_decidir_nome_com_regra_flag_troca_so_a_abertura(registros, tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    _gravar_regra_ligada(store)
    (regra,) = store.listar_intervencoes(campo="nome")

    llm_ligada, llm_desligada = LLMRoteirizado(), LLMRoteirizado()
    d_ligada = _decidir_nome_com_regra(registros, llm_ligada, regra, score=0.5)
    d_desligada = _decidir_nome_com_regra(
        registros, llm_desligada, regra, score=0.5, pesquisa_web_desligada=True
    )

    abertura_ligada = (
        "A verificação web não foi suficiente, mas uma intervenção humana anterior "
        "ensinou a regra abaixo. "
    )
    abertura_desligada = (
        "A pesquisa web foi desligada pelo operador nesta execução; uma intervenção "
        "humana anterior ensinou a regra abaixo. "
    )
    (p_ligada,), (p_desligada,) = llm_ligada.prompts, llm_desligada.prompts
    assert p_ligada.startswith(abertura_ligada)
    assert p_desligada.startswith(abertura_desligada)
    assert p_ligada[len(abertura_ligada):] == p_desligada[len(abertura_desligada):]
    assert not any(f in p_desligada for f in FRASES_PROIBIDAS_DESLIGADA)
    assert d_ligada == d_desligada
