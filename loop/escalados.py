"""Casos do loop que ficaram para revisão humana, em `output/escalados_humano/`.

Cada iteração com algo para um humano decidir acrescenta uma linha ao journal
`.escalados_<run_id>.jsonl`, logo depois de terminar. No fim da execução sai
`escalados_<run_id>.json` (lista legível, gerada linha a linha a partir do
journal), só quando houve ao menos um caso. Dois tipos de caso:

- ``campos_escalados``: merge gerado, mas um ou mais campos ficaram com
  `escalado_humano` (o SQL deixa o valor atual do vencedor e comenta
  `-- REVISAR MANUALMENTE`). Traz os valores de cada peça para o campo.
- ``grupo_sinalizado``: o merge não foi gerado (`GrupoSinalizado`, comentário
  `-- REVISÃO MANUAL` no SQL), com o motivo.

Os dois levam as peças envolvidas, no formato de `pecas` do JSON do loop. Na
execução não interativa (headless, ou a opção da tela "Rodar loop"), todo caso
que pediria a modal de intervenção cai aqui como escalado.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from loop.models import RegistroIteracao, agora_iso
from loop.serializacao import familia_para_json, para_json
from sql_generation.models import DecisaoMerge, GrupoSinalizado

PASTA_ESCALADOS = "escalados_humano"


def casos_da_iteracao(iteracao: RegistroIteracao, run_id: str) -> list[dict[str, Any]]:
    """Casos para revisão humana de uma iteração (lista vazia quando não há)."""
    resultado = iteracao.resultado_caso
    if resultado is None:
        return []
    por_id = {getattr(registro, "id", None): registro for registro in iteracao.grupo}
    base = {
        "run_id": run_id,
        "iteracao": iteracao.indice,
        "familia": familia_para_json(iteracao.familia),
        "registrado_em": agora_iso(),
    }
    casos: list[dict[str, Any]] = []
    for decisao in resultado.decisoes:
        if isinstance(decisao, GrupoSinalizado):
            membros = list(decisao.membro_ids)
            casos.append({
                **base,
                "tipo": "grupo_sinalizado",
                "grupo_ref": decisao.grupo_ref,
                "membro_ids": membros,
                "motivo": decisao.motivo,
                "pecas": [para_json(por_id[i]) for i in membros if i in por_id],
            })
        elif isinstance(decisao, DecisaoMerge):
            escalados = [dc for dc in decisao.decisoes_campo if dc.escalado_humano]
            if not escalados:
                continue
            membros = [decisao.vencedor_id, *decisao.perdedor_ids]
            casos.append({
                **base,
                "tipo": "campos_escalados",
                "grupo_ref": decisao.grupo_ref,
                "id_mantido": decisao.vencedor_id,
                "ids_removidos": list(decisao.perdedor_ids),
                "campos": [
                    {
                        "campo": dc.campo,
                        "fonte": dc.fonte,
                        "confianca": dc.confianca,
                        "justificativa": dc.justificativa,
                        "evidencias": para_json(dc.evidencias),
                        "valores_por_peca": {
                            str(i): para_json(getattr(por_id[i], dc.campo, None))
                            for i in membros if i in por_id
                        },
                    }
                    for dc in escalados
                ],
                "pecas": [para_json(por_id[i]) for i in membros if i in por_id],
            })
    return casos


class EscritorEscalados:
    """Journal por iteração e JSON legível no fim, só se houver casos."""

    def __init__(self, output_dir: Path, run_id: str) -> None:
        self.pasta = Path(output_dir) / PASTA_ESCALADOS
        self.run_id = run_id
        self.caminho_json = self.pasta / f"escalados_{run_id}.json"
        self._journal = self.pasta / f".escalados_{run_id}.jsonl"
        self.casos = 0

    def registrar(self, iteracao: RegistroIteracao) -> int:
        """Grava os casos da iteração e devolve quantos foram."""
        casos = casos_da_iteracao(iteracao, self.run_id)
        if not casos:
            return 0
        self.pasta.mkdir(parents=True, exist_ok=True)
        with self._journal.open("a", encoding="utf-8") as arquivo:
            for caso in casos:
                arquivo.write(json.dumps(caso, ensure_ascii=False, separators=(",", ":")) + "\n")
            arquivo.flush()
            os.fsync(arquivo.fileno())
        self.casos += len(casos)
        return len(casos)

    def finalizar(self) -> Path | None:
        """Gera `escalados_<run_id>.json` e remove o journal; `None` sem casos."""
        if not self._journal.exists():
            return None
        temporario = self.caminho_json.with_suffix(".json.tmp")
        with self._journal.open("r", encoding="utf-8") as origem, temporario.open("w", encoding="utf-8") as destino:
            destino.write("[\n")
            primeiro = True
            for linha in origem:
                if not linha.strip():
                    continue
                if not primeiro:
                    destino.write(",\n")
                representacao = json.dumps(json.loads(linha), ensure_ascii=False, indent=2)
                destino.write("  " + representacao.replace("\n", "\n  "))
                primeiro = False
            destino.write("\n]\n")
        os.replace(temporario, self.caminho_json)
        self._journal.unlink()
        return self.caminho_json
