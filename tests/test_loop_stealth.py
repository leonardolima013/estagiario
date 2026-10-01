"""Loop de famílias com a Chave_Stealth habilitada (stealth-fallback-integration).

Decisão Q2 (Req 1.7): ``loop.executor.executar_loop`` passa a Opcao_Stealth
desabilitada (``fallback_stealth=False``) a ``pipeline.executar_caso`` em todas as
iterações, qualquer que seja a Chave_Stealth. O ``executar_caso`` é um fake que só
registra os ``kwargs``: nenhum navegador, nenhuma rede, nenhum banco.
"""

from __future__ import annotations

import json
from typing import Any

import config
from loop.executor import executar_loop
from loop.models import FamiliaSorteada, LoopConfig, LoopStatus
from partitioning.models import Particao
from pipeline import ResultadoCaso
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
from tools.sortear_grupo import NenhumGrupoDuplicadoError


def _sorter_sequencial(familias: list[FamiliaSorteada]):
    def sortear(*, excluir):
        for familia in familias:
            if familia.chave not in excluir:
                return familia
        raise NenhumGrupoDuplicadoError("esgotado")

    return sortear


def test_loop_forca_fallback_stealth_desligado_com_chave_habilitada(tmp_path, monkeypatch):
    # Depois do isolamento (que faz delenv da Chave_Stealth): liga a chave.
    monkeypatch.setenv(config.NOME_VAR_COLETA_STEALTH_HABILITADA, "1")
    assert config.coleta_stealth_habilitada() == "habilitada"

    familias = [
        FamiliaSorteada("A", 1, "M"),
        FamiliaSorteada("B", 2, "M"),
        FamiliaSorteada("C", 3, "M"),
    ]
    chamadas: list[tuple[str, int, dict[str, Any]]] = []

    def executar_caso_fake(search_ref, brand_id, llm, dependencias_fk, **kwargs):
        chamadas.append((search_ref, brand_id, dict(kwargs)))
        return ResultadoCaso(
            grupo_ref=f"{search_ref}:M",
            particao=Particao(grupo_ref=f"{search_ref}:M", subclusters=[]),
            decisoes=[],
            sql="",
            grupo=[],
        )

    resultado = executar_loop(
        LoopConfig(len(familias), tmp_path / "output"),
        llm=object(),
        dependencias_fk=[],
        sortear_fn=_sorter_sequencial(familias),
        executar_caso_fn=executar_caso_fake,
    )

    assert resultado.status == LoopStatus.CONCLUIDO
    assert [(ref, brand) for ref, brand, _ in chamadas] == [("A", 1), ("B", 2), ("C", 3)]
    for _, _, kwargs in chamadas:
        assert "fallback_stealth" in kwargs
        assert kwargs["fallback_stealth"] is False

    # Saída do loop no tmp_path, com uma iteração por família.
    assert resultado.caminho_json.is_relative_to(tmp_path)
    assert resultado.caminho_sql.is_relative_to(tmp_path)
    documento = json.loads(resultado.caminho_json.read_text(encoding="utf-8"))
    assert list(documento["iteracoes"]) == ["1", "2", "3"]
