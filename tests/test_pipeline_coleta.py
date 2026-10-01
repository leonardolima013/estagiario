"""Testes de exemplo do pipeline com a coleta de páginas (html-extract-on-web-search, tarefa 7.8).

Cobrem: ativação pelos dois caminhos (``duplicata_real`` e recuperação de
``distinto_nao_classificado``), N ativações na mesma execução em ordem, coleta
antes de memória e intervenção, ``GrupoSinalizado`` com a justificativa de
pesquisa desligada e os padrões de ``executar_caso``.

Tudo com fakes: ``buscar_grupo``, LLM roteirizado, porta de coleta, verificador
web, RuleStore em ``tmp_path`` e ``pedir_intervencao``. A fixture ``sem_banco``
impede lookups reais de confiabilidade; as fixtures autouse importadas abaixo
bloqueiam a rede e isolam ``db/paginas.db``.
"""

from __future__ import annotations

import inspect

import pytest

from arbitration.pesquisa_web import AtivacaoPesquisa, ContextoPesquisaWeb
from db.rule_store import RuleStore
from loop.tracing import TraceCollector
from memory.models import RegraProposta, RespostaIntervencao
from pipeline import executar_caso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from verification.models import ResultadoVerificacao

_INCONCLUSIVO = ResultadoVerificacao(
    status="inconclusivo", nome_sugerido=None, justificativa="sem confirmação", fontes=[]
)


class FakeLLM:
    """Responde à partição roteirizada e ao juiz de nome (escolhe o 1º candidato)."""

    def __init__(self, subclusters: list[dict]):
        self._subclusters = subclusters
        self.chamadas: list[str] = []

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append(schema_name)
        if schema_name == "particao":
            return {"subclusters": self._subclusters}
        if schema_name == "decisao_nome":
            ids = [int(linha.split("id=")[1].split(":")[0]) for linha in user.splitlines() if "id=" in linha]
            return {"modo": "escolher", "part_id_escolhido": ids[0], "justificativa": "ok"}
        raise AssertionError(f"chamada inesperada: schema_name={schema_name!r}")


class FakeVerificador:
    """Verificador simples (sem Resultados_Estruturados) que registra as chamadas."""

    def __init__(self, resultado: ResultadoVerificacao = _INCONCLUSIVO, log: list | None = None):
        self._resultado = resultado
        self.chamadas: list[tuple] = []
        self._log = log

    def __call__(self, codigo, marca, nomes_conflitantes, *, on_evento=None):
        self.chamadas.append((codigo, marca, list(nomes_conflitantes)))
        if self._log is not None:
            self._log.append(("verificacao", tuple(nomes_conflitantes)))
        return self._resultado


class FakePorta:
    """PortaColeta fake: registra ativação e contexto e marca a posição no trace."""

    def __init__(self, log: list | None = None):
        self.ativacoes: list[AtivacaoPesquisa] = []
        self.contextos: list[ContextoPesquisaWeb] = []
        self._log = log

    def processar(self, ativacao, *, contexto, trace):
        self.ativacoes.append(ativacao)
        self.contextos.append(contexto)
        if self._log is not None:
            self._log.append(("coleta", ativacao.nomes_conflitantes))
        if trace is not None:
            trace.registrar("tool", "porta_coleta_fake", entrada={"codigo": ativacao.codigo})
        return None


def _buscar(registros):
    def _fn(search_ref, brand_id):
        return list(registros)

    return _fn


def _grupo_dois_caminhos(fazer_registro):
    """ids 1–2 divergem (duplicata_real) e 3–4 divergem (recuperação)."""
    app = "X 1991/2001"
    registros = [
        fazer_registro(1, "PIVO SUPERIOR", search_ref="CF1000", application=app),
        fazer_registro(2, "PIVO INFERIOR", search_ref="CF1000", application=app),
        fazer_registro(3, "BUCHA DIANTEIRA", search_ref="CF1000", application=app),
        fazer_registro(4, "BUCHA TRASEIRA", search_ref="CF1000", application=app),
    ]
    subclusters = [
        {"label": "duplicata_real", "membro_ids": [1, 2], "justificativa": "mesma peça"},
        {"label": "distinto_nao_classificado", "membro_ids": [3], "justificativa": "x"},
        {"label": "distinto_nao_classificado", "membro_ids": [4], "justificativa": "y"},
    ]
    return registros, subclusters


def _grupo_so_distintos(fazer_registro):
    app = "X 1991/2001"
    registros = [
        fazer_registro(1, "BUCHA DIANTEIRA", search_ref="CF1000", application=app),
        fazer_registro(2, "BUCHA TRASEIRA", search_ref="CF1000", application=app),
    ]
    subclusters = [
        {"label": "distinto_nao_classificado", "membro_ids": [1], "justificativa": "x"},
        {"label": "distinto_nao_classificado", "membro_ids": [2], "justificativa": "y"},
    ]
    return registros, subclusters


# --- Req 1.5: ativação pelos dois caminhos -------------------------------------


def test_ativacao_pelo_caminho_duplicata_real(fazer_registro, sem_banco):
    registros, subclusters = _grupo_dois_caminhos(fazer_registro)
    subclusters = subclusters[:1]
    porta = FakePorta()
    verificador = FakeVerificador()

    executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros[:2]), verificar_web=verificador,
        integracao_coleta=porta,
    )

    assert len(verificador.chamadas) == 1
    assert len(porta.ativacoes) == 1
    ativacao = porta.ativacoes[0]
    assert ativacao.codigo == "CF1000"
    assert ativacao.marca == "CITROEN"
    assert ativacao.nomes_conflitantes == ("PIVO INFERIOR", "PIVO SUPERIOR")
    assert ativacao.metodo == "injetado"
    assert ativacao.situacao == "sem_estruturados"
    assert ativacao.resultados is None


def test_ativacao_pelo_caminho_recuperacao_distinto(fazer_registro, sem_banco):
    registros, subclusters = _grupo_so_distintos(fazer_registro)
    porta = FakePorta()
    verificador = FakeVerificador()

    resultado = executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros), verificar_web=verificador,
        integracao_coleta=porta,
    )

    assert len(verificador.chamadas) == 1
    assert [a.nomes_conflitantes for a in porta.ativacoes] == [("BUCHA DIANTEIRA", "BUCHA TRASEIRA")]
    assert porta.ativacoes[0].situacao == "sem_estruturados"
    # Recuperação inconclusiva continua virando GrupoSinalizado, como antes.
    assert isinstance(resultado.decisoes[0], GrupoSinalizado)


# --- Req 1.4: N ativações na mesma execução, em ordem --------------------------


def test_n_ativacoes_na_mesma_execucao_em_ordem(fazer_registro, sem_banco):
    registros, subclusters = _grupo_dois_caminhos(fazer_registro)
    log: list = []
    porta = FakePorta(log)
    verificador = FakeVerificador(log=log)

    executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros), verificar_web=verificador,
        integracao_coleta=porta,
    )

    duplicata = ("PIVO INFERIOR", "PIVO SUPERIOR")
    recuperacao = ("BUCHA DIANTEIRA", "BUCHA TRASEIRA")
    assert [a.nomes_conflitantes for a in porta.ativacoes] == [duplicata, recuperacao]
    # Cada coleta segue a verificação da própria ativação.
    assert log == [
        ("verificacao", duplicata), ("coleta", duplicata),
        ("verificacao", recuperacao), ("coleta", recuperacao),
    ]
    # O mesmo contexto (e a mesma porta) serve as duas ativações.
    assert porta.contextos[0] is porta.contextos[1]


# --- Req 6.1: coleta antes de memória e intervenção ----------------------------


def test_coleta_roda_depois_da_verificacao_e_antes_de_memoria_e_intervencao(
    fazer_registro, sem_banco, tmp_path
):
    registros, subclusters = _grupo_dois_caminhos(fazer_registro)
    subclusters = subclusters[:1]
    log: list = []
    porta = FakePorta(log)
    trace = TraceCollector()

    def pedir(pedido):
        log.append(("intervencao", tuple(pedido.nomes_conflitantes)))
        return RespostaIntervencao(
            resposta_humana="O correto é inferior.",
            regra=RegraProposta(
                titulo="Preferir inferior",
                condicao="Conflito superior × inferior sem confirmação",
                resolucao="Escolher o nome inferior",
            ),
            valor="PIVO INFERIOR",
            origem_id=2,
            criado_por="teste",
        )

    resultado = executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        rule_store=RuleStore(tmp_path / "memoria.db"),
        buscar_grupo=_buscar(registros[:2]), verificar_web=FakeVerificador(log=log),
        pedir_intervencao=pedir, trace=trace, integracao_coleta=porta,
    )

    nomes = ("PIVO INFERIOR", "PIVO SUPERIOR")
    assert log == [("verificacao", nomes), ("coleta", nomes), ("intervencao", nomes)]

    eventos = [e.nome for e in trace.eventos()]
    i_verif = eventos.index("verificar_nomenclatura_peca")
    i_coleta = eventos.index("porta_coleta_fake")
    i_memoria = eventos.index("consultar_memoria")
    assert i_verif < i_coleta < i_memoria

    assert isinstance(resultado.decisoes[0], DecisaoMerge)
    decisao_nome = next(
        dc for dc in resultado.decisoes[0].decisoes_campo if dc.campo == "name"
    )
    assert decisao_nome.fonte == "intervencao_humana"
    assert decisao_nome.valor == "PIVO INFERIOR"


# --- Req 10.10: GrupoSinalizado com a justificativa de desligada ---------------


def test_recuperacao_desligada_produz_grupo_sinalizado_com_justificativa(
    fazer_registro, sem_banco, monkeypatch
):
    # Método inválido no ambiente não pode levantar com a pesquisa desligada.
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "metodo-inexistente")
    registros, subclusters = _grupo_so_distintos(fazer_registro)
    porta = FakePorta()
    verificador = FakeVerificador()

    resultado = executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros), verificar_web=verificador,
        integracao_coleta=porta, pesquisa_web=False,
    )

    assert verificador.chamadas == []
    assert len(resultado.decisoes) == 1
    sinalizado = resultado.decisoes[0]
    assert isinstance(sinalizado, GrupoSinalizado)
    assert sinalizado.membro_ids == [1, 2]
    assert "pesquisa web desligada pelo operador" in sinalizado.motivo
    assert "fonte='escalado_humano'" in sinalizado.motivo
    assert "DELETE" not in resultado.sql

    assert len(porta.ativacoes) == 1
    assert porta.ativacoes[0].situacao == "desligada"
    assert porta.ativacoes[0].metodo == "desligada"
    assert porta.contextos[0].pesquisa_habilitada is False


# --- Req 10.1: padrões de executar_caso ----------------------------------------


def test_assinatura_tem_os_padroes_documentados():
    params = inspect.signature(executar_caso).parameters
    assert params["pesquisa_web"].default is True
    assert params["coleta_html"].default is None
    assert params["cancel_event"].default is None
    assert params["integracao_coleta"].default is None
    for nome in ("pesquisa_web", "coleta_html", "cancel_event", "integracao_coleta"):
        assert params[nome].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    ("valor_env", "esperado"),
    [
        (None, "habilitada"),
        ("", "habilitada"),
        ("1", "habilitada"),
        (" Off ", "desabilitada"),
        ("não", "desabilitada"),
        ("talvez", "invalida"),
    ],
)
def test_padroes_pesquisa_ligada_e_coleta_pela_chave(
    fazer_registro, sem_banco, monkeypatch, valor_env, esperado
):
    if valor_env is None:
        monkeypatch.delenv("ESTAGIARIO_COLETA_HABILITADA", raising=False)
    else:
        monkeypatch.setenv("ESTAGIARIO_COLETA_HABILITADA", valor_env)
    registros, subclusters = _grupo_so_distintos(fazer_registro)
    porta = FakePorta()
    verificador = FakeVerificador()

    executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros), verificar_web=verificador,
        integracao_coleta=porta,
    )

    assert len(verificador.chamadas) == 1  # pesquisa ligada por padrão
    contexto = porta.contextos[0]
    assert contexto.pesquisa_habilitada is True
    assert contexto.coleta_efetiva == esperado
    assert contexto.cancel_event is None


@pytest.mark.parametrize(
    ("valor_env", "motivo"),
    [("0", "desabilitada"), ("talvez", "configuracao_invalida"), ("1", "sem_resultados_estruturados")],
)
def test_integracao_padrao_publica_desfecho_pela_chave(
    fazer_registro, sem_banco, monkeypatch, isolamento_coleta_autouse, valor_env, motivo
):
    monkeypatch.setenv("ESTAGIARIO_COLETA_HABILITADA", valor_env)
    registros, subclusters = _grupo_so_distintos(fazer_registro)
    trace = TraceCollector()
    avisos: list[str] = []

    executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros), verificar_web=FakeVerificador(),
        on_aviso=avisos.append, trace=trace,
    )

    coletas = [e for e in trace.eventos() if e.nome == "coletar_paginas"]
    assert len(coletas) == 1
    assert coletas[0].fase == "tool"
    assert coletas[0].status == "nao_executada"
    assert coletas[0].saida["desfecho"] == "nao_executada"
    assert coletas[0].saida["motivo"] == motivo
    assert coletas[0].entrada["metodo"] == "injetado"
    assert sum(1 for a in avisos if a.startswith("Coleta de páginas:")) == 1
    # Sem chegar ao coletor, o armazém temporário nem é criado.
    assert not isolamento_coleta_autouse.paginas_db_path.exists()


def test_opcao_coleta_prevalece_sobre_chave(fazer_registro, sem_banco, monkeypatch):
    monkeypatch.setenv("ESTAGIARIO_COLETA_HABILITADA", "talvez")
    registros, subclusters = _grupo_so_distintos(fazer_registro)
    porta = FakePorta()

    executar_caso(
        "CF1000", 1, llm=FakeLLM(subclusters), dependencias_fk=[],
        buscar_grupo=_buscar(registros), verificar_web=FakeVerificador(),
        integracao_coleta=porta, coleta_html=False,
    )

    assert porta.contextos[0].coleta_efetiva == "desabilitada"
