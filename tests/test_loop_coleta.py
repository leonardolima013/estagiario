# Feature: html-extract-on-web-search, Property 8: A auditoria do loop só ganha eventos `coletar_paginas`
"""Loop de famílias com a coleta de páginas (html-extract-on-web-search).

O módulo tem duas partes:

1. **Bloco comum**, reutilizável pelas propriedades e pelos exemplos de loop desta
   feature: famílias fakes, ``sortear``/``buscar_grupo`` fakes, LLM roteirizado
   por família, verificadores fakes por família (com e sem o protocolo de
   Resultados_Estruturados), ``IntegracaoColeta`` com fábrica e transporte fakes e
   o helper ``rodar_loop``, que roda ``loop.executor.executar_loop`` com o
   ``pipeline.executar_caso`` real, saída em ``tmp_path`` e devolve o JSON de
   auditoria e o SQL do loop já lidos do disco.
2. **Propriedades/exemplos**, uma seção por item, com o cabeçalho
   ``# Feature: html-extract-on-web-search, Property N: <título>``.

Os fakes de grupo, LLM, verificador, intervenção e coleta vêm do bloco comum de
``tests/test_pipeline_coleta_pbt.py`` (só helpers; nenhuma função ``test_*`` é
importada, para o pytest não coletá-las de novo aqui).
"""

from __future__ import annotations

import json
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from coleta_paginas.integracao import NOME_EVENTO_TRACE
from db.rule_store import RuleStore
from loop.executor import executar_loop
from loop.models import (
    FamiliaSorteada,
    IteracaoStatus,
    LoopConfig,
    LoopStatus,
    MotivoParada,
    ResultadoLoop,
)
from pipeline import executar_caso
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tests.test_pipeline_coleta_pbt import (
    DEPENDENCIAS_FK,
    URLS,
    CenarioGrupo,
    EspecMemoria,
    EspecVerificador,
    LLMRoteirizado,
    MontagemColeta,
    PedirIntervencaoFake,
    ambiente_temporario,
    cenarios_grupo,
    especs_memoria,
    especs_verificador,
    fazer_registro,
    montar_coleta,
    semear_regra,
)
from tools.sortear_grupo import NenhumGrupoDuplicadoError
from verification.models import ResultadoVerificacao
from verification.serper_client import ResultadoOrganico

# ===========================================================================
# Bloco comum
# ===========================================================================


@dataclass(frozen=True)
class FamiliaFake:
    """Uma família do loop: o cenário do grupo e o verificador web dessa família."""

    cenario: CenarioGrupo
    verificador: EspecVerificador

    @property
    def chave(self) -> tuple[str, int]:
        return self.cenario.search_ref, self.cenario.brand_id

    def sorteada(self) -> FamiliaSorteada:
        return FamiliaSorteada(self.cenario.search_ref, self.cenario.brand_id, self.cenario.brand)


def sortear_sequencial(familias: tuple[FamiliaFake, ...]):
    """``sortear_fn`` fake: devolve as famílias na ordem, respeitando ``excluir``."""

    def sortear(*, excluir):
        for familia in familias:
            if familia.chave not in excluir:
                return familia.sorteada()
        raise NenhumGrupoDuplicadoError("famílias fakes esgotadas")

    return sortear


def buscar_grupo_familias(familias: tuple[FamiliaFake, ...]):
    """``buscar_grupo`` fake do loop: devolve os registros da família pedida."""
    por_chave = {f.chave: f.cenario for f in familias}

    def buscar(search_ref: str, brand_id: int):
        return list(por_chave[(search_ref, brand_id)].registros)

    return buscar


class LLMFamilias:
    """LLM roteirizado por família; ``selecionar`` escolhe o roteiro da iteração.

    Registra ``(chave, schema_name, user)`` de cada chamada, para comparar prompts
    entre execuções.
    """

    def __init__(self, familias: tuple[FamiliaFake, ...]) -> None:
        self._roteiros = {f.chave: LLMRoteirizado(f.cenario) for f in familias}
        self._atual: tuple[str, int] | None = None
        self.chamadas: list[tuple[tuple[str, int], str, str]] = []

    def selecionar(self, chave: tuple[str, int]) -> None:
        self._atual = chave

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        assert self._atual is not None, "LLM chamado fora de uma iteração"
        self.chamadas.append((self._atual, schema_name, user))
        return self._roteiros[self._atual].gerar_json(system, user, json_schema, schema_name)


@dataclass
class ExecucaoLoop:
    """O que uma execução de ``executar_loop`` deixa observável."""

    resultado: ResultadoLoop
    documento: dict[str, Any]
    sql: str
    avisos: list[str]
    chamadas_llm: list[tuple[tuple[str, int], str, str]]
    kwargs_casos: list[dict[str, Any]] = field(default_factory=list)
    montagem: MontagemColeta | None = None


def rodar_loop(
    familias: tuple[FamiliaFake, ...],
    memoria: EspecMemoria,
    *,
    comportamento_coleta: str,
    base: Path,
    pesquisa_web: bool = True,
    iteracoes: int | None = None,
    cancel_event=None,
) -> ExecucaoLoop:
    """Roda ``executar_loop`` com o ``executar_caso`` real sobre fakes novos.

    Cada chamada usa um diretório próprio em ``base`` (saída do loop, RuleStore e
    Armazem_Paginas). A ``IntegracaoColeta`` de ``montar_coleta`` é compartilhada
    pelas iterações da execução; cada iteração recebe o verificador da própria
    família. ``kwargs_casos`` guarda os ``kwargs`` que o loop repassou ao caso.
    """
    diretorio = Path(tempfile.mkdtemp(dir=base))
    montagem = montar_coleta(comportamento_coleta, diretorio)

    store: RuleStore | None = None
    if memoria.usar_rule_store:
        store = RuleStore(diretorio / "memoria.db")
        if memoria.regra_previa:
            for familia in familias:
                semear_regra(store, familia.cenario)
    pedir = PedirIntervencaoFake(memoria.pedir == "com_valor") if memoria.pedir else None
    llm = LLMFamilias(familias)
    verificadores = {f.chave: f.verificador.criar() for f in familias}
    avisos: list[str] = []
    kwargs_casos: list[dict[str, Any]] = []

    def executar_caso_fn(search_ref, brand_id, llm_iteracao, dependencias_fk, **kwargs):
        kwargs_casos.append(dict(kwargs))
        llm.selecionar((search_ref, brand_id))
        return executar_caso(
            search_ref,
            brand_id,
            llm_iteracao,
            dependencias_fk,
            verificar_web=verificadores[(search_ref, brand_id)],
            pesquisa_web=pesquisa_web,
            # Os kwargs do loop (inclusive cancel_event) prevalecem.
            **{**montagem.kwargs, **kwargs},
        )

    if cancel_event is None:
        # Comportamentos de cancelamento de montar_coleta: o sinal da coleta passa
        # a ser o cancel_event do loop, como em produção (Req 6.6).
        cancel_event = montagem.kwargs.get("cancel_event")

    with ambiente_temporario(montagem.ambiente):
        resultado = executar_loop(
            LoopConfig(iteracoes or len(familias), diretorio / "output"),
            llm,
            DEPENDENCIAS_FK,
            rule_store=store,
            sortear_fn=sortear_sequencial(familias),
            executar_caso_fn=executar_caso_fn,
            buscar_grupo=buscar_grupo_familias(familias),
            on_aviso=avisos.append,
            pedir_intervencao=pedir,
            cancel_event=cancel_event,
        )

    return ExecucaoLoop(
        resultado=resultado,
        documento=json.loads(resultado.caminho_json.read_text(encoding="utf-8")),
        sql=resultado.caminho_sql.read_text(encoding="utf-8"),
        avisos=avisos,
        chamadas_llm=list(llm.chamadas),
        kwargs_casos=kwargs_casos,
        montagem=montagem,
    )


# Campos voláteis (horários, durações, identificadores de execução e caminhos).
_VOLATEIS_EXECUCAO = ("run_id", "iniciada_em", "finalizada_em", "arquivos")
_VOLATEIS_ITERACAO = ("iniciada_em", "finalizada_em")
_VOLATEIS_EVENTO = ("ordem", "timestamp", "duracao_ms")


def normalizar_evento(evento: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in evento.items() if k not in _VOLATEIS_EVENTO}


def normalizar_documento(documento: dict[str, Any], *, sem_coleta: bool) -> dict[str, Any]:
    """Cópia do JSON de auditoria sem os campos voláteis; com ``sem_coleta``, sem
    os eventos ``coletar_paginas`` de ``tools_utilizadas``."""
    normalizado = {k: v for k, v in documento.items() if k not in ("execucao", "iteracoes")}
    normalizado["execucao"] = {
        k: v for k, v in documento["execucao"].items() if k not in _VOLATEIS_EXECUCAO
    }
    iteracoes = {}
    for chave, iteracao in documento["iteracoes"].items():
        copia = {k: v for k, v in iteracao.items() if k not in _VOLATEIS_ITERACAO}
        copia["tools_utilizadas"] = [
            normalizar_evento(evento)
            for evento in iteracao["tools_utilizadas"]
            if not (sem_coleta and evento["tool"] == NOME_EVENTO_TRACE)
        ]
        iteracoes[chave] = copia
    normalizado["iteracoes"] = iteracoes
    return normalizado


def eventos_coleta(documento: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        evento
        for iteracao in documento["iteracoes"].values()
        for evento in iteracao["tools_utilizadas"]
        if evento["tool"] == NOME_EVENTO_TRACE
    ]


@st.composite
def familias_loop(draw, min_size: int = 1, max_size: int = 3) -> tuple[FamiliaFake, ...]:
    """1 a 3 famílias com chaves ``(search_ref, brand_id)`` distintas."""
    cenarios = draw(
        st.lists(
            cenarios_grupo(),
            min_size=min_size,
            max_size=max_size,
            unique_by=lambda c: (c.search_ref, c.brand_id),
        )
    )
    return tuple(FamiliaFake(c, draw(especs_verificador(c))) for c in cenarios)


def familia_com_coleta_executada(search_ref: str = "CF1000") -> FamiliaFake:
    """Família fixa em que a coleta chega ao Transporte_HTTP (duas fontes aceitas)."""
    registros = (
        fazer_registro(1, "FILTRO DE AR", search_ref=search_ref, brand_id=1, brand="MANN-FILTER"),
        fazer_registro(2, "FILTRO CABINE", search_ref=search_ref, brand_id=1, brand="MANN-FILTER"),
    )
    cenario = CenarioGrupo(search_ref, 1, "MANN-FILTER", registros, (("duplicata_real", (1, 2)),))
    verificador = EspecVerificador(
        "protocolo",
        resultado=ResultadoVerificacao("inconclusivo", None, "sem confirmação clara", []),
        resultados_pesquisa=(
            ResultadoOrganico(f"{search_ref} FILTRO DE AR", URLS[1], f"ref {search_ref} MANN-FILTER", 1),
            ResultadoOrganico(f"{search_ref} FILTRO CABINE", URLS[2], "peça original", 2),
        ),
    )
    return FamiliaFake(cenario, verificador)


SETTINGS_LOOP = settings(
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)


# ===========================================================================
# Property 8: A auditoria do loop só ganha eventos `coletar_paginas`
# ===========================================================================
# Feature: html-extract-on-web-search, Property 8: A auditoria do loop só ganha eventos `coletar_paginas`
# **Validates: Requirements 5.4, 7.6**

# Comportamentos comparados com a referência "desabilitada_opcao". Os de
# cancelamento ficam de fora: no loop, o Sinal_Cancelamento é o próprio
# cancel_event do loop, e ativá-lo encerra o loop de propósito (Req 6.6).
COMPORTAMENTOS_LOOP = (
    "desabilitada_chave",
    "chave_invalida",
    "sucesso",
    "falhas_por_fonte",
    "excecao_transporte",
    "excecao_fabrica",
    "excecao_coletar",
    "fabrica_padrao",
    "porta_fake",
)

_CAMPOS_ITERACAO_IDENTICOS = (
    "decisoes", "registro_final", "raciocinios", "raciocinio_final", "sql", "status",
)


@SETTINGS_LOOP
@given(
    familias=familias_loop(),
    memoria=especs_memoria,
    comportamento=st.sampled_from(COMPORTAMENTOS_LOOP),
    pesquisa_web=st.booleans(),
)
@example(
    familias=(familia_com_coleta_executada("CF1000"),),
    memoria=EspecMemoria(usar_rule_store=True, regra_previa=False, pedir="com_valor"),
    comportamento="sucesso",
    pesquisa_web=True,
)
@example(
    familias=(familia_com_coleta_executada("CF1000"), familia_com_coleta_executada("JE4699")),
    memoria=EspecMemoria(usar_rule_store=True, regra_previa=True, pedir=None),
    comportamento="falhas_por_fonte",
    pesquisa_web=True,
)
@example(
    familias=(familia_com_coleta_executada("CF1000"),),
    memoria=EspecMemoria(usar_rule_store=False, regra_previa=False, pedir="sem_valor"),
    comportamento="excecao_transporte",
    pesquisa_web=True,
)
def test_property8_auditoria_do_loop_so_ganha_eventos_coletar_paginas(
    familias, memoria, comportamento, pesquisa_web, tmp_path, sem_banco
):
    referencia = rodar_loop(
        familias, memoria, comportamento_coleta="desabilitada_opcao",
        base=tmp_path, pesquisa_web=pesquisa_web,
    )
    com_coleta = rodar_loop(
        familias, memoria, comportamento_coleta=comportamento,
        base=tmp_path, pesquisa_web=pesquisa_web,
    )

    doc_ref, doc_col = referencia.documento, com_coleta.documento
    assert doc_col["iteracoes"].keys() == doc_ref["iteracoes"].keys()

    # Campos de decisão de cada iteração idênticos, sem normalização (Req 5.4).
    for chave, it_ref in doc_ref["iteracoes"].items():
        it_col = doc_col["iteracoes"][chave]
        for campo in _CAMPOS_ITERACAO_IDENTICOS:
            assert it_col[campo] == it_ref[campo], (chave, campo)

    # SQL do loop idêntico.
    assert com_coleta.sql == referencia.sql

    # Sem os eventos coletar_paginas e sem campos voláteis, o documento inteiro
    # é igual: nenhum outro campo muda e nenhum evento a mais (por exemplo
    # observabilidade/aviso) entra em tools_utilizadas.
    assert normalizar_documento(doc_col, sem_coleta=True) == normalizar_documento(doc_ref, sem_coleta=True)

    # Os eventos a mais são só da coleta: fase tool, sem justificativa (Req 7.5, 7.6).
    for evento in eventos_coleta(doc_col) + eventos_coleta(doc_ref):
        assert evento["fase"] == "tool"
        assert evento["justificativa"] is None

    # Com a porta fake (que não publica), a auditoria não tem nenhum coletar_paginas;
    # com a IntegracaoColeta, a referência desabilitada tem um por Ativacao_Pesquisa.
    if comportamento == "porta_fake":
        assert eventos_coleta(doc_col) == []

    # Nada da coleta chega aos prompts do LLM.
    assert com_coleta.chamadas_llm == referencia.chamadas_llm



# ===========================================================================
# Exemplos do loop (tarefa 7.11): cancel_event repassado, evento coletar_paginas
# em tools_utilizadas e cancelamento durante a coleta.
# _Requirements: 6.5, 6.6, 7.6_
# ===========================================================================

_MEMORIA_ESCALONA = EspecMemoria(usar_rule_store=False, regra_previa=False, pedir=None)

# Chaves que loop.serializacao.evento_para_json (mais "ordem") grava por evento.
_CHAVES_EVENTO_SERIALIZADO = {
    "ordem", "timestamp", "fase", "tool", "status", "duracao_ms",
    "entrada", "resultado", "justificativa",
}
_CHAVES_ENTRADA_COLETA = {"codigo_peca", "marca_peca", "metodo", "quantidade_resultados"}
_CHAVES_SAIDA_COLETA = {"desfecho", "motivo", "duracao_ms", "contagem", "entradas", "variavel"}
_CHAVES_ENTRADA_RELATORIO = {
    "url", "dominio", "confianca", "desfecho", "motivo", "status_http", "hash_conteudo",
}
_CHAVES_CONTAGEM = {"armazenado", "reaproveitado", "falha", "url_invalida"}


@pytest.fixture
def conexoes_postgres(monkeypatch):
    """Registra (e recusa) qualquer tentativa de abrir conexão PostgreSQL."""
    import psycopg

    tentativas: list[tuple] = []

    def _recusar(*args, **kwargs):
        tentativas.append((args, kwargs))
        raise AssertionError("conexão PostgreSQL não permitida neste teste")

    monkeypatch.setattr(psycopg, "connect", _recusar)
    return tentativas


def _eventos_iteracao(documento: dict[str, Any], chave: str) -> list[dict[str, Any]]:
    return documento["iteracoes"][chave]["tools_utilizadas"]


def _evento_coleta_unico(documento: dict[str, Any], chave: str) -> dict[str, Any]:
    eventos = [e for e in _eventos_iteracao(documento, chave) if e["tool"] == NOME_EVENTO_TRACE]
    assert len(eventos) == 1, eventos
    return eventos[0]


def _assert_campos_permitidos(evento: dict[str, Any]) -> None:
    assert set(evento) == _CHAVES_EVENTO_SERIALIZADO
    assert evento["fase"] == "tool"
    assert evento["justificativa"] is None
    assert set(evento["entrada"]) == _CHAVES_ENTRADA_COLETA
    saida = evento["resultado"]
    assert set(saida) <= _CHAVES_SAIDA_COLETA
    assert set(saida["contagem"]) == _CHAVES_CONTAGEM
    for item in saida.get("entradas", []):
        assert set(item) == _CHAVES_ENTRADA_RELATORIO
    # Nenhum byte do corpo da página nem cabeçalho HTTP no evento serializado.
    serializado = json.dumps(evento, ensure_ascii=False)
    assert "<html>" not in serializado
    assert "content-type" not in serializado.casefold()


def test_loop_repassa_o_mesmo_cancel_event_a_cada_executar_caso(tmp_path, sem_banco):
    familias = (familia_com_coleta_executada("CF1000"), familia_com_coleta_executada("JE4699"))
    sinal = threading.Event()

    execucao = rodar_loop(
        familias, _MEMORIA_ESCALONA, comportamento_coleta="sucesso",
        base=tmp_path, cancel_event=sinal,
    )

    assert len(execucao.kwargs_casos) == 2
    for kwargs in execucao.kwargs_casos:
        assert kwargs["cancel_event"] is sinal
    assert execucao.resultado.status == LoopStatus.CONCLUIDO
    assert not sinal.is_set()


def test_loop_grava_evento_coletar_paginas_em_tools_utilizadas(tmp_path, sem_banco, conexoes_postgres):
    familia = familia_com_coleta_executada("CF1000")

    execucao = rodar_loop(
        (familia,), _MEMORIA_ESCALONA, comportamento_coleta="sucesso", base=tmp_path,
    )

    documento = execucao.documento
    assert documento["execucao"]["status"] == "concluido"
    evento = _evento_coleta_unico(documento, "1")
    _assert_campos_permitidos(evento)

    # Serializado pelo mesmo caminho dos demais eventos: "ordem" contígua e
    # posterior ao verificar_nomenclatura_peca da mesma ativação (Req 7.1, 7.6).
    eventos = _eventos_iteracao(documento, "1")
    assert [e["ordem"] for e in eventos] == list(range(1, len(eventos) + 1))
    indice_verificacao = next(
        i for i, e in enumerate(eventos) if e["tool"] == "verificar_nomenclatura_peca"
    )
    assert eventos.index(evento) > indice_verificacao

    assert evento["status"] == "ok"
    assert evento["entrada"] == {
        "codigo_peca": "CF1000",
        "marca_peca": "MANN-FILTER",
        "metodo": "injetado",
        "quantidade_resultados": 2,
    }
    saida = evento["resultado"]
    assert saida["desfecho"] == "executada"
    assert saida["motivo"] is None
    assert saida["duracao_ms"] >= 0
    entradas = saida["entradas"]
    assert {item["url"] for item in entradas} == {URLS[1], URLS[2]}
    assert all(item["desfecho"] == "armazenado" for item in entradas)
    assert all(item["status_http"] == 200 and item["hash_conteudo"] for item in entradas)
    assert saida["contagem"] == {"armazenado": 2, "reaproveitado": 0, "falha": 0, "url_invalida": 0}
    assert sorted(execucao.montagem.transporte.chamadas) == sorted([URLS[1], URLS[2]])

    # A coleta não entra em raciocinios (evento sem justificativa, Req 7.5).
    raciocinios = documento["iteracoes"]["1"]["raciocinios"]
    assert all(r.get("tool") != NOME_EVENTO_TRACE for r in raciocinios)
    assert conexoes_postgres == []


def test_cancelamento_durante_a_coleta_gera_falha_cancelado_e_encerra_o_loop(
    tmp_path, sem_banco, conexoes_postgres
):
    familias = (familia_com_coleta_executada("CF1000"), familia_com_coleta_executada("JE4699"))

    execucao = rodar_loop(
        familias, _MEMORIA_ESCALONA, comportamento_coleta="cancelado_durante", base=tmp_path,
    )

    # Sem cancel_event explícito, o sinal da coleta é o cancel_event do loop.
    sinal = execucao.montagem.kwargs["cancel_event"]
    assert sinal.is_set()
    assert len(execucao.kwargs_casos) == 1
    assert execucao.kwargs_casos[0]["cancel_event"] is sinal

    # Loop encerrado como cancelado pelo executor depois da 1ª iteração (Req 6.6).
    resultado = execucao.resultado
    assert resultado.status == LoopStatus.CANCELADO
    assert resultado.motivo_parada == MotivoParada.CANCELADO_PELO_USUARIO
    documento = execucao.documento
    assert documento["execucao"]["status"] == "cancelado"
    assert documento["execucao"]["motivo_parada"] == "cancelado_pelo_usuario"
    assert list(documento["iteracoes"]) == ["1"]
    # executar_caso terminou sem exceção: a iteração fica como sucesso no executor.
    assert documento["iteracoes"]["1"]["status"] == IteracaoStatus.SUCESSO.value

    # A 1ª fonte foi processada; a restante virou falha/cancelado sem HTTP (Req 6.5).
    assert len(execucao.montagem.transporte.chamadas) == 1
    evento = _evento_coleta_unico(documento, "1")
    _assert_campos_permitidos(evento)
    assert evento["status"] == "cancelado"
    saida = evento["resultado"]
    assert saida["desfecho"] == "executada"
    entradas = saida["entradas"]
    assert len(entradas) == 2
    processada, cancelada = entradas
    assert processada["url"] == execucao.montagem.transporte.chamadas[0]
    assert processada["desfecho"] == "armazenado"
    assert processada["status_http"] == 200
    assert cancelada["desfecho"] == "falha"
    assert cancelada["motivo"] == "cancelado"
    assert cancelada["status_http"] is None
    assert cancelada["hash_conteudo"] is None
    assert saida["contagem"] == {"armazenado": 1, "reaproveitado": 0, "falha": 1, "url_invalida": 0}

    # Nenhum SQL executado: nenhuma conexão PostgreSQL; o SQL do loop é só texto.
    assert conexoes_postgres == []
    assert resultado.caminho_sql.exists()
