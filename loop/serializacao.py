"""Serialização e checkpoints públicos do loop.

O journal JSONL interno mantém cada iteração incrementalmente. O arquivo JSON
público é sempre reescrito para um temporário e promovido com `os.replace`,
portanto cancelamentos e erros não deixam um JSON final truncado.
"""

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from loop.models import (
    EventoExecucao,
    FamiliaSorteada,
    RegistroIteracao,
    ResultadoLoop,
)


def para_json(valor: Any) -> Any:
    if isinstance(valor, Enum):
        return valor.value
    if isinstance(valor, (datetime, date)):
        return valor.isoformat()
    if isinstance(valor, Path):
        return str(valor)
    if is_dataclass(valor):
        return {campo.name: para_json(getattr(valor, campo.name)) for campo in fields(valor)}
    if isinstance(valor, dict):
        return {str(k): para_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set)):
        return [para_json(v) for v in valor]
    if valor is None or isinstance(valor, (str, int, float, bool)):
        return valor
    return str(valor)


def familia_para_json(familia: FamiliaSorteada | None) -> dict[str, Any] | None:
    if familia is None:
        return None
    return {
        "codigo": familia.search_ref,
        "marca": familia.brand,
        "brand_id": familia.brand_id,
        "chave": f"{familia.search_ref}:{familia.brand_id}",
    }


def evento_para_json(evento: EventoExecucao) -> dict[str, Any]:
    return {
        "timestamp": evento.timestamp,
        "fase": evento.fase,
        "tool": evento.nome,
        "status": evento.status,
        "duracao_ms": evento.duracao_ms,
        "entrada": para_json(evento.entrada),
        "resultado": para_json(evento.saida or evento.detalhes),
        "justificativa": evento.justificativa,
    }


def resultado_caso_para_json(resultado: Any | None) -> dict[str, Any] | None:
    if resultado is None:
        return None
    familia = None
    if resultado.grupo:
        primeiro = resultado.grupo[0]
        familia = {
            "codigo": primeiro.search_ref,
            "marca": primeiro.brand,
            "brand_id": primeiro.brand_id,
            "chave": f"{primeiro.search_ref}:{primeiro.brand_id}",
        }
    return {
        "familia": familia,
        "nomes": _nomes_para_json(resultado.grupo),
        "pecas": para_json(resultado.grupo),
        "particionamento": para_json(resultado.particao),
        "decisoes": [decisao_para_json(decisao) for decisao in resultado.decisoes],
        "registro_final": [registro_final_para_json(decisao, resultado.grupo) for decisao in resultado.decisoes],
        "sql": {"gerado": bool(resultado.sql), "conteudo": resultado.sql},
    }


def decisao_para_json(decisao: Any) -> dict[str, Any]:
    if hasattr(decisao, "vencedor_id"):
        return {
            "tipo": "merge",
            "grupo_ref": decisao.grupo_ref,
            "id_mantido": decisao.vencedor_id,
            "ids_removidos": para_json(decisao.perdedor_ids),
            "campos": [
                {
                    "campo": campo.campo,
                    "valor": para_json(campo.valor),
                    "fonte": campo.fonte,
                    "confianca": campo.confianca,
                    "origem_id": campo.origem_id,
                    "evidencias": para_json(campo.evidencias),
                    "justificativa": campo.justificativa,
                    "escalado_humano": campo.escalado_humano,
                }
                for campo in decisao.decisoes_campo
            ],
        }
    return {
        "tipo": "sinalizado",
        "grupo_ref": getattr(decisao, "grupo_ref", None),
        "membro_ids": para_json(getattr(decisao, "membro_ids", [])),
        "motivo": getattr(decisao, "motivo", ""),
    }


def registro_final_para_json(decisao: Any, grupo: list[Any]) -> dict[str, Any]:
    """`registro_final` de uma decisão: o vencedor com os valores depois do merge
    (campo escalado mantém o valor do vencedor) ou o resumo do sinalizado.

    O loop também manda esse dicionário ao painel ao vivo, em
    `IteracaoConcluida.registros_finais`.
    """
    por_id = {getattr(registro, "id", None): registro for registro in grupo}
    vencedor_id = getattr(decisao, "vencedor_id", None)
    if vencedor_id is None:
        primeiro = grupo[0] if grupo else None
        return {
            "status": "sinalizado",
            "codigo": getattr(primeiro, "search_ref", None),
            "marca": getattr(primeiro, "brand", None),
            "brand_id": getattr(primeiro, "brand_id", None),
            "membro_ids": para_json(getattr(decisao, "membro_ids", [])),
            "motivo": getattr(decisao, "motivo", ""),
        }

    vencedor = por_id.get(vencedor_id)
    valores = para_json(vencedor) if vencedor is not None else {}
    campos_escalados = []
    for decisao_campo in getattr(decisao, "decisoes_campo", []):
        if decisao_campo.escalado_humano:
            campos_escalados.append(decisao_campo.campo)
            continue
        valores[decisao_campo.campo] = para_json(decisao_campo.valor)
    nome = valores.get("name")
    campos = {
        chave: valor
        for chave, valor in valores.items()
        if chave not in {"id", "search_ref", "brand_id", "brand", "name"}
    }
    return {
        "status": "merge",
        "codigo": valores.get("search_ref"),
        "marca": valores.get("brand"),
        "brand_id": valores.get("brand_id"),
        "id_mantido": vencedor_id,
        "ids_removidos": para_json(getattr(decisao, "perdedor_ids", [])),
        "nome": nome,
        "campos": campos,
        "campos_escalados": campos_escalados,
    }


def _raciocinio_observavel(resultado: Any | None, eventos: list[EventoExecucao]) -> list[dict[str, Any]]:
    if resultado is None:
        return []
    observacoes: list[dict[str, Any]] = []
    particao = getattr(resultado, "particao", None)
    for subcluster in getattr(particao, "subclusters", []):
        observacoes.append({
            "etapa": "particionamento",
            "rotulo": subcluster.label,
            "membro_ids": subcluster.membro_ids,
            "justificativa": subcluster.justificativa,
        })
    for decisao in getattr(resultado, "decisoes", []):
        for decisao_campo in getattr(decisao, "decisoes_campo", []):
            observacoes.append({
                "etapa": "arbitragem_campo",
                "campo": decisao_campo.campo,
                "fonte": decisao_campo.fonte,
                "confianca": decisao_campo.confianca,
                "origem_id": decisao_campo.origem_id,
                "justificativa": decisao_campo.justificativa,
                "evidencias": para_json(decisao_campo.evidencias),
            })
    for evento in eventos:
        if evento.justificativa:
            observacoes.append({
                "etapa": evento.fase,
                "tool": evento.nome,
                "justificativa": evento.justificativa,
            })
    return observacoes


def _raciocinio_final(resultado: Any | None, grupo: list[Any]) -> list[dict[str, Any]]:
    if resultado is None:
        return []
    finais = []
    for decisao in getattr(resultado, "decisoes", []):
        registro = registro_final_para_json(decisao, grupo)
        justificativas = [
            dc.justificativa
            for dc in getattr(decisao, "decisoes_campo", [])
            if dc.justificativa
        ]
        finais.append({"justificativas": justificativas, "registro_final": registro})
    return finais


def _nomes_para_json(grupo: list[Any]) -> list[dict[str, Any]]:
    return [
        {"id": getattr(registro, "id", None), "nome": getattr(registro, "name", None)}
        for registro in grupo
    ]


def _raciocinios_para_json(resultado: Any | None, eventos: list[EventoExecucao]) -> list[dict[str, Any]]:
    raciocinios = _raciocinio_observavel(resultado, eventos)
    for indice, raciocinio in enumerate(raciocinios, start=1):
        raciocinio["ordem"] = indice
    return raciocinios


def iteracao_para_json(iteracao: RegistroIteracao) -> dict[str, Any]:
    resultado = iteracao.resultado_caso
    eventos = iteracao.eventos
    decisoes = getattr(resultado, "decisoes", [])
    return {
        "indice": iteracao.indice,
        "status": para_json(iteracao.status),
        "iniciada_em": iteracao.iniciada_em,
        "finalizada_em": iteracao.finalizada_em,
        "familia": familia_para_json(iteracao.familia),
        "codigo": getattr(iteracao.familia, "search_ref", None),
        "marca": getattr(iteracao.familia, "brand", None),
        "nomes": _nomes_para_json(iteracao.grupo),
        "pecas": para_json(iteracao.grupo),
        "particionamento": para_json(getattr(resultado, "particao", None)),
        "tools_utilizadas": [
            {"ordem": indice, **evento_para_json(evento)}
            for indice, evento in enumerate(eventos, start=1)
        ],
        "raciocinios": _raciocinios_para_json(resultado, eventos),
        "decisoes": [decisao_para_json(decisao) for decisao in decisoes],
        "registro_final": [registro_final_para_json(decisao, iteracao.grupo) for decisao in decisoes],
        "raciocinio_final": _raciocinio_final(resultado, iteracao.grupo),
        "sql": {"gerado": bool(iteracao.sql), "conteudo": iteracao.sql},
        "erro": para_json(iteracao.erro),
    }


class LoopOutputWriter:
    """Escritor incremental dos dois artefatos públicos do loop."""

    def __init__(self, output_dir: Path, run_id: str) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.caminho_json = self.output_dir / f"loop_{run_id}.json"
        self.caminho_sql = self.output_dir / f"loop_{run_id}.sql"
        self._journal = self.output_dir / f".loop_{run_id}.jsonl"
        self._iteracoes_escritas = 0
        self._journal.touch()

    def registrar_iteracao(self, resultado: ResultadoLoop, iteracao: RegistroIteracao) -> None:
        linha = json.dumps(iteracao_para_json(iteracao), ensure_ascii=False, separators=(",", ":"))
        with self._journal.open("a", encoding="utf-8") as arquivo:
            arquivo.write(linha + "\n")
        self._iteracoes_escritas += 1
        self.checkpoint(resultado)

    def checkpoint(self, resultado: ResultadoLoop) -> None:
        temporario = self.caminho_json.with_suffix(".json.tmp")
        metadata = {
            "schema": "loop_auditoria.v2",
            "execucao": {
                "run_id": resultado.run_id,
                "status": para_json(resultado.status),
                "motivo_parada": para_json(resultado.motivo_parada),
                "iteracoes_solicitadas": resultado.iteracoes_solicitadas,
                "iteracoes_executadas": resultado.iteracoes_executadas,
                "iteracoes_concluidas": resultado.iteracoes_concluidas,
                "iteracoes_com_erro": resultado.iteracoes_com_erro,
                "iteracoes_canceladas": resultado.iteracoes_canceladas,
                "iniciada_em": resultado.iniciada_em,
                "finalizada_em": resultado.finalizada_em,
                "arquivos": {
                    "json": str(self.caminho_json),
                    "sql": str(self.caminho_sql),
                },
            },
        }
        with temporario.open("w", encoding="utf-8") as arquivo:
            arquivo.write("{\n")
            items = list(metadata.items())
            for chave, valor in items:
                self._escrever_chave_formatada(arquivo, chave, valor, indentacao=2)
                arquivo.write(",\n")
            arquivo.write('  "iteracoes": {\n')
            self._copiar_journal_para_json(arquivo)
            arquivo.write("\n  }\n}\n")
        os.replace(temporario, self.caminho_json)

    @staticmethod
    def _escrever_chave_formatada(arquivo, chave: str, valor: Any, indentacao: int) -> None:
        representacao = json.dumps(valor, ensure_ascii=False, indent=2)
        linhas = representacao.splitlines()
        arquivo.write(" " * indentacao)
        json.dump(chave, arquivo, ensure_ascii=False)
        arquivo.write(": " + linhas[0])
        for linha in linhas[1:]:
            arquivo.write("\n" + " " * indentacao + linha)

    def _copiar_journal_para_json(self, destino) -> None:
        with self._journal.open("r", encoding="utf-8") as origem:
            atual = origem.readline()
            primeiro = True
            while atual:
                proxima = origem.readline()
                registro = json.loads(atual)
                chave = str(registro["indice"])
                if not primeiro:
                    destino.write(",\n")
                representacao = json.dumps(registro, ensure_ascii=False, indent=2).splitlines()
                destino.write("    ")
                json.dump(chave, destino, ensure_ascii=False)
                destino.write(": " + representacao[0])
                for linha in representacao[1:]:
                    destino.write("\n    " + linha)
                primeiro = False
                atual = proxima

    def finalizar(self, resultado: ResultadoLoop) -> tuple[Path, Path]:
        resultado.caminho_json = self.caminho_json
        resultado.caminho_sql = self.caminho_sql
        self.checkpoint(resultado)
        try:
            self._journal.unlink()
        except FileNotFoundError:
            pass
        return self.caminho_json, self.caminho_sql
