"""Contratos do loop autônomo de famílias duplicadas."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


def agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class LoopStatus(StrEnum):
    EM_ANDAMENTO = "em_andamento"
    CONCLUIDO = "concluido"
    FINALIZADO_COM_ERROS = "finalizado_com_erros"
    CANCELADO = "cancelado"
    PARCIAL = "parcial"


class IteracaoStatus(StrEnum):
    SUCESSO = "sucesso"
    ERRO = "erro"
    CANCELADA = "cancelada"


class MotivoParada(StrEnum):
    ITERACOES_SOLICITADAS_ALCANCADAS = "iteracoes_solicitadas_alcancadas"
    FAMILIAS_DUPLICADAS_ESGOTADAS = "familias_duplicadas_esgotadas"
    CANCELADO_PELO_USUARIO = "cancelado_pelo_usuario"
    ERRO_FATAL = "erro_fatal"


@dataclass(frozen=True)
class LoopConfig:
    iteracoes: int
    output_dir: Path

    def __post_init__(self) -> None:
        if isinstance(self.iteracoes, bool) or not isinstance(self.iteracoes, int):
            raise ValueError("iteracoes deve ser um inteiro positivo")
        if self.iteracoes <= 0:
            raise ValueError("iteracoes deve ser um inteiro positivo")


@dataclass(frozen=True)
class FamiliaSorteada:
    search_ref: str
    brand_id: int
    brand: str

    @property
    def chave(self) -> tuple[str, int]:
        return self.search_ref, self.brand_id


@dataclass(frozen=True)
class EventoExecucao:
    timestamp: str
    fase: str
    nome: str
    status: str = "ok"
    duracao_ms: float | None = None
    detalhes: dict[str, Any] = field(default_factory=dict)
    entrada: dict[str, Any] = field(default_factory=dict)
    saida: dict[str, Any] = field(default_factory=dict)
    justificativa: str | None = None


@dataclass(frozen=True)
class LoopProgresso:
    total: int
    indice_atual: int
    fase: str
    mensagem: str
    familia: FamiliaSorteada | None = None
    concluidas: int = 0
    erros: int = 0
    sinalizadas: int = 0
    cancelando: bool = False


@dataclass
class RegistroIteracao:
    indice: int
    status: IteracaoStatus
    iniciada_em: str
    finalizada_em: str | None = None
    familia: FamiliaSorteada | None = None
    grupo: list[Any] = field(default_factory=list)
    resultado_caso: Any | None = None
    eventos: list[EventoExecucao] = field(default_factory=list)
    sql: str = ""
    erro: dict[str, str] | None = None


@dataclass
class ResultadoLoop:
    run_id: str
    status: LoopStatus
    motivo_parada: MotivoParada
    iteracoes_solicitadas: int
    iteracoes: list[RegistroIteracao] = field(default_factory=list)
    iniciada_em: str = field(default_factory=agora_iso)
    finalizada_em: str | None = None
    caminho_json: Path | None = None
    caminho_sql: Path | None = None

    @property
    def iteracoes_executadas(self) -> int:
        return len(self.iteracoes)

    @property
    def iteracoes_concluidas(self) -> int:
        return sum(i.status == IteracaoStatus.SUCESSO for i in self.iteracoes)

    @property
    def iteracoes_com_erro(self) -> int:
        return sum(i.status == IteracaoStatus.ERRO for i in self.iteracoes)

    @property
    def iteracoes_canceladas(self) -> int:
        return sum(i.status == IteracaoStatus.CANCELADA for i in self.iteracoes)
