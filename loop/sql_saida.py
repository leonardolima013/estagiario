"""Saída SQL acumulada do loop — revisável, nunca executada automaticamente."""

from __future__ import annotations

import os
from pathlib import Path

from loop.models import RegistroIteracao


def _comentario(texto: str) -> str:
    return "\n".join(f"-- {linha}" for linha in str(texto).splitlines())


def sql_da_iteracao(iteracao: RegistroIteracao, total: int) -> str:
    familia = iteracao.familia
    familia_texto = (
        f"search_ref={familia.search_ref}, brand_id={familia.brand_id}, brand={familia.brand}"
        if familia is not None else "família não obtida"
    )
    linhas = [
        f"-- ITERAÇÃO {iteracao.indice}/{total}",
        f"-- Família: {familia_texto}",
        f"-- Resultado: {iteracao.status.value}",
    ]
    if iteracao.erro:
        linhas.append(_comentario(f"Erro: {iteracao.erro.get('tipo')}: {iteracao.erro.get('mensagem')}"))
    if iteracao.sql:
        linhas.append(iteracao.sql.rstrip())
    elif not iteracao.erro:
        linhas.append("-- Nenhum SQL executável gerado para esta iteração.")
    return "\n".join(linhas)


def gerar_sql_loop(iteracoes: list[RegistroIteracao], total: int) -> str:
    if not iteracoes:
        return "-- Nenhuma iteração concluída.\n"
    return "\n\n".join(sql_da_iteracao(iteracao, total) for iteracao in iteracoes) + "\n"


def salvar_sql_atomico(caminho: Path, conteudo: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(conteudo, encoding="utf-8")
    os.replace(temporario, caminho)
