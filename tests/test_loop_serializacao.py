from arbitration.models import DecisaoCampo
from partitioning.models import Particao, Subcluster
from pipeline import ResultadoCaso
from sql_generation.models import DecisaoMerge
import json
from datetime import datetime

from loop.models import (
    EventoExecucao,
    FamiliaSorteada,
    IteracaoStatus,
    LoopStatus,
    MotivoParada,
    RegistroIteracao,
    ResultadoLoop,
)
from loop.serializacao import LoopOutputWriter, iteracao_para_json


def _resultado(tmp_path):
    return ResultadoLoop(
        run_id="20260921_120000_000001",
        status=LoopStatus.EM_ANDAMENTO,
        motivo_parada=MotivoParada.ITERACOES_SOLICITADAS_ALCANCADAS,
        iteracoes_solicitadas=2,
        caminho_json=tmp_path / "loop.json",
    )


def _iteracao(indice, status=IteracaoStatus.SUCESSO):
    return RegistroIteracao(
        indice=indice,
        status=status,
        iniciada_em=datetime(2026, 1, 1, 12, indice).isoformat(),
        finalizada_em=datetime(2026, 1, 1, 12, indice, 1).isoformat(),
        familia=FamiliaSorteada(f"REF-{indice}", indice, "MARCA"),
        grupo=[{"id": indice, "name": "PECA"}],
        sql=f"-- iteração {indice}",
    )


def test_checkpoint_json_e_valido_e_contem_pecas(tmp_path):
    resultado = _resultado(tmp_path)
    writer = LoopOutputWriter(tmp_path, resultado.run_id)
    writer.checkpoint(resultado)
    resultado.iteracoes.append(_iteracao(1))
    writer.registrar_iteracao(resultado, resultado.iteracoes[-1])

    texto = writer.caminho_json.read_text(encoding="utf-8")
    conteudo = json.loads(texto)
    assert conteudo["schema"] == "loop_auditoria.v2"
    assert isinstance(conteudo["iteracoes"], dict)
    assert '"iteracoes": {\n' in texto
    assert '\n    "1": {\n' in texto
    assert '\n      "indice": 1' in texto
    assert '\n      "pecas": [' in texto
    assert '\n  "execucao": {\n' in texto
    assert '"status": "em_andamento"' in texto
    assert conteudo["execucao"]["status"] == "em_andamento"
    assert conteudo["iteracoes"]["1"]["pecas"] == [{"id": 1, "name": "PECA"}]
    assert conteudo["execucao"]["arquivos"]["json"].endswith(".json")


def test_checkpoint_de_erro_e_serializavel(tmp_path):
    resultado = _resultado(tmp_path)
    writer = LoopOutputWriter(tmp_path, resultado.run_id)
    iteracao = _iteracao(1, IteracaoStatus.ERRO)
    iteracao.erro = {"tipo": "RuntimeError", "mensagem": "falhou"}
    resultado.iteracoes.append(iteracao)
    writer.registrar_iteracao(resultado, iteracao)

    conteudo = json.loads(writer.caminho_json.read_text(encoding="utf-8"))
    assert conteudo["iteracoes"]["1"]["status"] == "erro"
    assert conteudo["iteracoes"]["1"]["erro"]["tipo"] == "RuntimeError"


def test_finalizacao_remove_journal_e_deixa_json_publico(tmp_path):
    resultado = _resultado(tmp_path)
    writer = LoopOutputWriter(tmp_path, resultado.run_id)
    resultado.iteracoes.append(_iteracao(1))
    writer.registrar_iteracao(resultado, resultado.iteracoes[-1])
    resultado.status = LoopStatus.CONCLUIDO
    resultado.finalizada_em = "fim"
    writer.finalizar(resultado)

    assert writer.caminho_json.exists()
    assert not writer._journal.exists()
    conteudo = json.loads(writer.caminho_json.read_text(encoding="utf-8"))
    assert conteudo["execucao"]["status"] == "concluido"


def test_dossie_expoe_tools_raciocinio_e_registro_final(fazer_registro):
    grupo = [
        fazer_registro(1, "PIVO", width=1.0),
        fazer_registro(2, "PIVO INFERIOR", width=2.0),
    ]
    decisao = DecisaoMerge(
        grupo_ref="X:M",
        vencedor_id=1,
        perdedor_ids=[2],
        decisoes_campo=[
            DecisaoCampo(
                campo="name", valor="PIVO INFERIOR", justificativa="contexto confirmado",
                fonte="julgamento_modelo", origem_id=2, confianca="alta",
                evidencias=[{"tipo": "catalogo", "observacao": "posição"}],
            )
        ],
        valores_atuais_vencedor={"name": "PIVO"},
    )
    resultado = ResultadoCaso(
        grupo_ref="X:M",
        particao=Particao(
            grupo_ref="X:M",
            subclusters=[Subcluster("duplicata_real", [1, 2], "mesma peça")],
            sinais_heuristicos=["sem divergência dimensional"],
            regras_aplicaveis=["regra de nomenclatura"],
        ),
        decisoes=[decisao], sql="BEGIN;\nCOMMIT;", grupo=grupo,
    )
    iteracao = RegistroIteracao(
        indice=1, status=IteracaoStatus.SUCESSO, iniciada_em="a", grupo=grupo,
        resultado_caso=resultado, sql=resultado.sql,
        eventos=[EventoExecucao(
            timestamp="t", fase="tool", nome="arbitrar_campo",
            entrada={"campo": "name"}, saida={"valor": "PIVO INFERIOR"},
            justificativa="contexto confirmado",
        )],
    )

    documento = iteracao_para_json(iteracao)

    evento = documento["tools_utilizadas"][0]
    assert evento["tool"] == "arbitrar_campo"
    assert evento["entrada"] == {"campo": "name"}
    assert evento["resultado"] == {"valor": "PIVO INFERIOR"}
    assert evento["justificativa"] == "contexto confirmado"
    assert documento["particionamento"]["sinais_heuristicos"] == ["sem divergência dimensional"]
    assert documento["raciocinios"][0]["etapa"] == "particionamento"
    assert any(item.get("campo") == "name" for item in documento["raciocinios"])
    final = documento["registro_final"][0]
    assert final["id_mantido"] == 1
    assert final["ids_removidos"] == [2]
    assert final["nome"] == "PIVO INFERIOR"
