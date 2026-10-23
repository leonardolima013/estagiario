"""Saídas do loop depois da auditoria de memória (docs/auditoria-memoria/RELATORIO.md).

- iterações gravadas e esvaziadas (nada pesado fica no `ResultadoLoop`);
- SQL em append, igual ao `gerar_sql_loop`;
- JSON público só no fim, recuperável do journal;
- casos para revisão humana em `output/escalados_humano/`;
- fila local como fonte padrão e opções repassadas ao caso.
Sem banco, sem LLM e sem rede.
"""

from __future__ import annotations

import json

from arbitration.models import DecisaoCampo
from loop.escalados import PASTA_ESCALADOS
from loop.executor import executar_loop
from loop.fila import FilaFamilias
from loop.models import FamiliaSorteada, IteracaoStatus, LoopConfig, LoopStatus, MotivoParada
from loop.serializacao import recuperar_json_de_journal
from loop.sql_saida import EscritorSqlLoop, gerar_sql_loop
from partitioning.models import Particao, Subcluster
from pipeline import ResultadoCaso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.sortear_grupo import NenhumGrupoDuplicadoError


def _sorter(familias):
    def sortear(*, excluir):
        for familia in familias:
            if familia.chave not in excluir:
                return familia
        raise NenhumGrupoDuplicadoError("esgotado")

    return sortear


def _caso(decisoes_por_ref=None, sql_por_ref=None, grupo_por_ref=None, chamadas=None):
    def executar(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        if chamadas is not None:
            chamadas.append((search_ref, kwargs))
        ref = f"{search_ref}:M"
        return ResultadoCaso(
            grupo_ref=ref,
            particao=Particao(grupo_ref=ref, subclusters=[Subcluster("duplicata_real", [1, 2], "x")]),
            decisoes=(decisoes_por_ref or {}).get(search_ref, []),
            sql=(sql_por_ref or {}).get(search_ref, ""),
            grupo=(grupo_por_ref or {}).get(search_ref, []),
        )

    return executar


def test_iteracoes_sao_esvaziadas_depois_de_gravadas(tmp_path, fazer_registro):
    grupo = [fazer_registro(1, "PIVO"), fazer_registro(2, "PIVO SUPERIOR")]
    familias = [FamiliaSorteada("A", 1, "M"), FamiliaSorteada("B", 2, "M")]

    resultado = executar_loop(
        LoopConfig(2, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter(familias),
        executar_caso_fn=_caso(sql_por_ref={"A": "BEGIN;\nCOMMIT;"}, grupo_por_ref={"A": grupo}),
    )

    for iteracao in resultado.iteracoes:
        assert iteracao.grupo == [] and iteracao.resultado_caso is None
        assert iteracao.eventos == [] and iteracao.sql == ""
    assert [i.familia.chave for i in resultado.iteracoes] == [("A", 1), ("B", 2)]
    # O que foi esvaziado está nas saídas.
    documento = json.loads(resultado.caminho_json.read_text(encoding="utf-8"))
    assert [p["id"] for p in documento["iteracoes"]["1"]["pecas"]] == [1, 2]
    assert "BEGIN;\nCOMMIT;" in resultado.caminho_sql.read_text(encoding="utf-8")


def test_sql_em_append_igual_ao_gerado_de_uma_vez(tmp_path):
    familias = [FamiliaSorteada(ref, i, "M") for i, ref in enumerate("ABC", start=1)]
    sqls = {"A": "BEGIN;\nUPDATE x;\nCOMMIT;", "C": "-- REVISÃO MANUAL: y"}

    resultado = executar_loop(
        LoopConfig(3, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter(familias), executar_caso_fn=_caso(sql_por_ref=sqls),
    )

    texto = resultado.caminho_sql.read_text(encoding="utf-8")
    assert texto.count("-- ITERAÇÃO ") == 3
    assert texto.endswith("\n") and "\n\n-- ITERAÇÃO 2/3" in texto


def test_escritor_sql_vazio_e_bloco_a_bloco(tmp_path):
    from loop.models import RegistroIteracao

    vazio = EscritorSqlLoop(tmp_path / "vazio.sql", 2)
    vazio.finalizar()
    assert (tmp_path / "vazio.sql").read_text(encoding="utf-8") == gerar_sql_loop([], 2)

    iteracoes = [
        RegistroIteracao(i, IteracaoStatus.SUCESSO, "a", familia=FamiliaSorteada(f"R{i}", i, "M"), sql=f"-- sql {i}")
        for i in (1, 2)
    ]
    escritor = EscritorSqlLoop(tmp_path / "loop.sql", 2)
    for iteracao in iteracoes:
        escritor.registrar(iteracao)
    escritor.finalizar()
    assert (tmp_path / "loop.sql").read_text(encoding="utf-8") == gerar_sql_loop(iteracoes, 2)


def test_json_publico_so_aparece_no_fim(tmp_path):
    vistos = []

    def progresso(evento):
        if evento.fase == "checkpoint":
            vistos.append(sorted(p.name for p in tmp_path.glob("loop_*.json")))

    resultado = executar_loop(
        LoopConfig(2, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter([FamiliaSorteada("A", 1, "M"), FamiliaSorteada("B", 2, "M")]),
        executar_caso_fn=_caso(), on_progresso=progresso,
    )

    assert vistos == [[], []]
    assert resultado.caminho_json.exists()
    assert not list(tmp_path.glob(".loop_*.jsonl"))


def test_recuperar_json_de_journal_orfao(tmp_path):
    def caso_que_mata(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        if search_ref == "B":
            raise KeyboardInterrupt  # simula o processo morrendo no meio
        return _caso()(search_ref, brand_id, llm, dependencias_fk, **kwargs)

    try:
        executar_loop(
            LoopConfig(3, tmp_path), llm=object(), dependencias_fk=[],
            sortear_fn=_sorter([FamiliaSorteada(r, i, "M") for i, r in enumerate("ABC", 1)]),
            executar_caso_fn=caso_que_mata,
        )
    except KeyboardInterrupt:
        pass
    else:  # pragma: no cover
        raise AssertionError("o caso deveria ter interrompido o loop")

    # O `finally` do executor ainda gera o JSON; para simular a morte sem `finally`,
    # recria o journal a partir dele e apaga o JSON.
    documento = json.loads(next(tmp_path.glob("loop_*.json")).read_text(encoding="utf-8"))
    run_id = documento["execucao"]["run_id"]
    journal = tmp_path / f".loop_{run_id}.jsonl"
    journal.write_text(
        "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in documento["iteracoes"].values()),
        encoding="utf-8",
    )
    next(tmp_path.glob("loop_*.json")).unlink()

    # A família interrompida não pode sair como sucesso.
    assert documento["iteracoes"]["2"]["status"] == IteracaoStatus.CANCELADA.value
    assert documento["iteracoes"]["2"]["erro"]["tipo"] == "Interrompida"

    caminho = recuperar_json_de_journal(journal)

    recuperado = json.loads(caminho.read_text(encoding="utf-8"))
    assert recuperado["execucao"]["recuperado_de_journal"] is True
    assert recuperado["execucao"]["status"] == LoopStatus.PARCIAL.value
    assert recuperado["execucao"]["iteracoes_concluidas"] == 1
    assert recuperado["execucao"]["iteracoes_canceladas"] == 1
    assert recuperado["iteracoes"] == documento["iteracoes"]
    assert journal.exists()


def test_escalados_vao_para_a_pasta_propria(tmp_path, fazer_registro):
    grupo_a = [fazer_registro(1, "PIVO", width=10.0), fazer_registro(2, "PIVO INF", width=12.0)]
    grupo_b = [fazer_registro(3, "KIT"), fazer_registro(4, "KIT X")]
    merge = DecisaoMerge(
        grupo_ref="A:M", vencedor_id=1, perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(campo="width", valor=None, justificativa="sem sinal confiável",
                         fonte="escalado_humano", escalado_humano=True),
            DecisaoCampo(campo="name", valor="PIVO", justificativa="ok", fonte="sem_conflito"),
        ],
        valores_atuais_vencedor={"width": 10.0, "name": "PIVO"},
    )
    sinalizado = GrupoSinalizado(grupo_ref="B:M", motivo="similarity_id conflitante", membro_ids=[3, 4])

    resultado = executar_loop(
        LoopConfig(3, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter([FamiliaSorteada(r, i, "M") for i, r in enumerate("ABC", 1)]),
        executar_caso_fn=_caso(
            decisoes_por_ref={"A": [merge], "B": [sinalizado]},
            grupo_por_ref={"A": grupo_a, "B": grupo_b},
        ),
    )

    assert resultado.casos_escalados == 2
    assert resultado.caminho_escalados == tmp_path / PASTA_ESCALADOS / f"escalados_{resultado.run_id}.json"
    casos = json.loads(resultado.caminho_escalados.read_text(encoding="utf-8"))
    assert [c["tipo"] for c in casos] == ["campos_escalados", "grupo_sinalizado"]
    campo = casos[0]["campos"][0]
    assert campo["campo"] == "width"
    assert campo["valores_por_peca"] == {"1": 10.0, "2": 12.0}
    assert casos[0]["familia"]["codigo"] == "A" and casos[0]["iteracao"] == 1
    assert casos[1]["motivo"] == "similarity_id conflitante"
    assert [p["id"] for p in casos[1]["pecas"]] == [3, 4]
    assert not list((tmp_path / PASTA_ESCALADOS).glob(".escalados_*.jsonl"))


def test_sem_escalados_nao_cria_arquivo(tmp_path):
    resultado = executar_loop(
        LoopConfig(1, tmp_path), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter([FamiliaSorteada("A", 1, "M")]), executar_caso_fn=_caso(),
    )
    assert resultado.caminho_escalados is None
    assert resultado.casos_escalados == 0


def test_fila_como_fonte_marca_desfechos(tmp_path):
    fila = FilaFamilias(
        tmp_path / "fila.db",
        ler_familias=lambda: [[("A", 1, "M", 2), ("B", 2, "M", 3), ("C", 3, "M", 4)]],
    )

    def caso(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        if search_ref == "B":
            raise RuntimeError("falhou")
        return _caso()(search_ref, brand_id, llm, dependencias_fk, **kwargs)

    primeira = executar_loop(
        LoopConfig(2, tmp_path / "out"), llm=object(), dependencias_fk=[],
        fonte_familias=fila, executar_caso_fn=caso,
    )
    assert [i.familia.chave for i in primeira.iteracoes] == [("A", 1), ("B", 2)]
    assert fila.contagem() == {"pendente": 1, "em_andamento": 0, "concluida": 1, "erro": 1}

    # Uma nova execução retoma de onde parou e esgota a fila.
    segunda = executar_loop(
        LoopConfig(5, tmp_path / "out"), llm=object(), dependencias_fk=[],
        fonte_familias=fila, executar_caso_fn=caso,
    )
    assert [i.familia.chave for i in segunda.iteracoes] == [("C", 3)]
    assert segunda.motivo_parada == MotivoParada.FAMILIAS_DUPLICADAS_ESGOTADAS
    documento = json.loads(segunda.caminho_json.read_text(encoding="utf-8"))
    eventos = documento["iteracoes"]["1"]["tools_utilizadas"]
    assert eventos[0]["tool"] == "proxima_familia"


def test_interrupcao_no_meio_da_familia_devolve_a_familia_para_a_fila(tmp_path):
    fila = FilaFamilias(tmp_path / "fila.db", ler_familias=lambda: [[("A", 1, "M", 2), ("B", 2, "M", 2)]])

    def caso(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        raise KeyboardInterrupt

    try:
        executar_loop(
            LoopConfig(2, tmp_path / "out"), llm=object(), dependencias_fk=[],
            fonte_familias=fila, executar_caso_fn=caso,
        )
    except KeyboardInterrupt:
        pass

    assert fila.contagem() == {"pendente": 2, "em_andamento": 0, "concluida": 0, "erro": 0}


def test_cancelamento_devolve_a_familia_para_a_fila(tmp_path):
    from threading import Event

    fila = FilaFamilias(tmp_path / "fila.db", ler_familias=lambda: [[("A", 1, "M", 2), ("B", 2, "M", 2)]])
    cancelar = Event()

    def caso(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        cancelar.set()
        raise RuntimeError("modal cancelada")

    resultado = executar_loop(
        LoopConfig(2, tmp_path / "out"), llm=object(), dependencias_fk=[],
        fonte_familias=fila, executar_caso_fn=caso, cancel_event=cancelar,
    )

    assert resultado.status == LoopStatus.CANCELADO
    assert fila.contagem()["pendente"] == 2


def test_falha_ao_preparar_a_fila_e_erro_fatal(tmp_path):
    def ler_quebrado():
        raise RuntimeError("réplica fora do ar")

    resultado = executar_loop(
        LoopConfig(3, tmp_path / "out"), llm=object(), dependencias_fk=[],
        fonte_familias=FilaFamilias(tmp_path / "fila.db", ler_familias=ler_quebrado),
        executar_caso_fn=_caso(),
    )

    assert resultado.motivo_parada == MotivoParada.ERRO_FATAL
    assert resultado.status == LoopStatus.FINALIZADO_COM_ERROS
    assert len(resultado.iteracoes) == 1
    assert resultado.iteracoes[0].erro["tipo"] == "RuntimeError"


def test_opcoes_do_caso_so_vao_quando_diferem_do_padrao(tmp_path):
    padrao: list = []
    executar_loop(
        LoopConfig(1, tmp_path / "a"), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter([FamiliaSorteada("A", 1, "M")]), executar_caso_fn=_caso(chamadas=padrao),
    )
    (_, kwargs), = padrao
    assert kwargs["fallback_stealth"] is False
    assert not {"pesquisa_web", "coleta_html", "verificar_web"} & set(kwargs)

    ligadas: list = []
    verificador = object()
    executar_loop(
        LoopConfig(1, tmp_path / "b"), llm=object(), dependencias_fk=[],
        sortear_fn=_sorter([FamiliaSorteada("A", 1, "M")]), executar_caso_fn=_caso(chamadas=ligadas),
        pesquisa_web=False, coleta_html=True, fallback_stealth=True, verificar_web=verificador,
    )
    (_, kwargs), = ligadas
    assert kwargs["fallback_stealth"] is True
    assert kwargs["pesquisa_web"] is False
    assert kwargs["coleta_html"] is True
    assert kwargs["verificar_web"] is verificador
