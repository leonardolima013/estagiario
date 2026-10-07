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


# ---------------------------------------------------------------------------
# Eventos ao vivo (painel de execução). Nenhum deles entra no JSON de auditoria:
# o que é auditável continua sendo o `EventoExecucao` registrado pelo
# `TraceCollector`. Estes só existem para quem acompanha a execução enquanto
# ela acontece, e são sempre derivados de dados já sanitizados.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EtapaIniciada:
    """Uma etapa começou. Ela termina no próximo `EventoExecucao` de mesmo `nome`."""

    timestamp: str
    fase: str
    nome: str
    detalhes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RespostaParcial:
    """Resposta estruturada de uma chamada LLM ainda sendo gerada.

    `resposta` é o JSON parcial já interpretado e sanitizado como o trace. A
    versão final é o `saida["resposta_estruturada"]` do `EventoExecucao` de fase
    `llm` com o mesmo `nome` (o `schema_name` da chamada).
    """

    timestamp: str
    nome: str
    resposta: dict[str, Any]


@dataclass(frozen=True)
class AvisoEmitido:
    """Espelho de uma mensagem enviada ao `on_aviso` do pipeline."""

    timestamp: str
    mensagem: str


@dataclass(frozen=True)
class GrupoCarregado:
    """Registros da família, assim que `buscar_grupo` os devolve.

    Cada registro vem no formato que o JSON do loop grava em `pecas`
    (`loop.serializacao.para_json`), sem truncar: são dados do catálogo, nunca
    prompt nem segredo.
    """

    timestamp: str
    grupo_ref: str
    registros: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class IteracaoIniciada:
    timestamp: str
    indice: int
    total: int
    familia: FamiliaSorteada


@dataclass(frozen=True)
class IteracaoConcluida:
    timestamp: str
    indice: int
    total: int
    status: IteracaoStatus
    familia: FamiliaSorteada | None = None
    pecas: int = 0
    merges: int = 0
    sinalizados: int = 0
    duracao_ms: float | None = None
    erro: dict[str, str] | None = None
    # Um por merge da família, no formato de `registro_final` do JSON do loop
    # (`loop.serializacao.registro_final_para_json`): o vencedor com os valores
    # depois do merge. Vazio em erro, cancelamento ou família sem merge.
    registros_finais: tuple[dict[str, Any], ...] = ()


EventoAoVivo = (
    EventoExecucao
    | EtapaIniciada
    | RespostaParcial
    | AvisoEmitido
    | GrupoCarregado
    | IteracaoIniciada
    | IteracaoConcluida
    | LoopProgresso
)


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
