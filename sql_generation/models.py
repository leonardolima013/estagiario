"""Estruturas de dados da geração de SQL (SPEC.md §4.5, §6.1)."""

from __future__ import annotations

from dataclasses import dataclass, field

from arbitration.models import DecisaoCampo


@dataclass(frozen=True)
class DecisaoMerge:
    grupo_ref: str
    vencedor_id: int
    perdedor_ids: list[int]
    decisoes_campo: list[DecisaoCampo]
    # Snapshot dos valores atuais do registro vencedor, por campo — usado por
    # gerar_sql pra escrever no UPDATE apenas os campos cujo valor decidido DIFERE
    # do valor que o vencedor já tem (evita UPDATE inócuo e sobrescrita acidental —
    # decisão Q-02=a). Default vazio: sem snapshot, gerar_sql escreve todos os
    # campos decididos (comportamento legado, preservado pra DecisaoMerge montada
    # à mão que não fornece o snapshot).
    valores_atuais_vencedor: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GrupoSinalizado:
    """Grupo que não gera SQL automático — precisa de revisão humana antes de mesclar."""

    grupo_ref: str
    motivo: str
    # ids do subcluster que gerou o sinalizado — usado pra TUI conseguir mostrar o
    # sinalizado dentro do painel do subcluster certo (SPEC.md não previa isso;
    # necessário pra correlacionar Subcluster <-> resultado na tela da Fase 4).
    membro_ids: list[int] = field(default_factory=list)
