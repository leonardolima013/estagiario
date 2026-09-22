"""Modelos da intervenção humana e da memória aprendida."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from db.rule_store import Regra

PontoIntervencao = Literal["nome", "particionamento"]


@dataclass(frozen=True)
class PedidoIntervencao:
    """Contexto suficiente para o operador responder sem acessar o histórico do LLM."""

    ponto: PontoIntervencao
    grupo_ref: str
    search_ref: str
    marca: str
    nomes_conflitantes: list[str]
    motivo: str
    contexto_web: str | None = None
    membro_ids: list[int] = field(default_factory=list)
    candidatos: list[tuple[int, str]] = field(default_factory=list)

    def texto_busca(self) -> str:
        """Texto sem o identificador específico da peça para recuperar padrões."""
        partes = [*self.nomes_conflitantes, self.motivo]
        if self.contexto_web:
            partes.append(self.contexto_web)
        return " ".join(p for p in partes if p)


@dataclass(frozen=True)
class RegraProposta:
    """Regra semântica sugerida pelo LLM a partir da resposta humana."""

    titulo: str
    condicao: str
    resolucao: str


@dataclass(frozen=True)
class RespostaIntervencao:
    """Resposta já confirmada pelo operador.

    `valor`/`origem_id` resolvem o caso atual; `regra` é a generalização persistida
    para casos futuros. Para decisões de particionamento, `acao` pode ser usada
    como marcador (`permitir_merge`, `manter_sinalizado`, etc.) sem confundir a
    explicação textual da regra.
    """

    resposta_humana: str
    regra: RegraProposta
    valor: object | None = None
    origem_id: int | None = None
    acao: str | None = None
    criado_por: str = "operador"


@dataclass(frozen=True)
class RecuperacaoIntervencao:
    regra: Regra
    score: float
