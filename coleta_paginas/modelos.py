"""Modelos de domínio da coleta de páginas por peça (spec html-extract-save).

Todas as dataclasses são imutáveis (`frozen=True`) e usam `tuple` para
sequências, de modo que trava, classificador e seletor possam ser funções puras
sem risco de mutação das entradas (Req 1.3, 2.11, 3.12).

O relatório e suas entradas carregam só os campos permitidos pelo Req 9.4:
nunca bytes do conteúdo nem cabeçalhos HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Confianca(StrEnum):
    """Nível de identidade de um Resultado_Busca em relação à peça (Req 2.3–2.5)."""

    ALTA = "alta"
    MEDIA = "media"
    REJEITADO = "rejeitado"


@dataclass(frozen=True)
class PecaConsultada:
    """Identidade da peça sob análise.

    `nomes_candidatos` é sempre guardado como `tuple`; uma `list` (ou outro
    iterável) recebida é convertida em `__post_init__`.
    """

    codigo_peca: str | None
    marca_peca: str | None
    nomes_candidatos: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.nomes_candidatos, tuple):
            object.__setattr__(self, "nomes_candidatos", tuple(self.nomes_candidatos))


@dataclass(frozen=True)
class ResultadoBusca:
    """Item de resultado de busca. Todo o conteúdo é dado não confiável."""

    titulo: str | None
    snippet: str | None
    url: str | None
    dominio: str | None
    posicao: int | None = None


@dataclass(frozen=True)
class DecisaoFonte:
    """Saída do Classificador_Fontes para um Resultado_Busca (Req 2.7)."""

    resultado: ResultadoBusca
    confianca: Confianca
    codigo_confirmado: bool
    marca_confirmada: bool
    nome_reforcado: bool
    motivo: str


@dataclass(frozen=True)
class FonteSelecionada:
    """Aceito escolhido pelo Seletor_Fontes para fetch."""

    decisao: DecisaoFonte
    url_normalizada: str
    dominio: str  # domínio efetivo (Req 4.6)


@dataclass(frozen=True)
class ExclusaoUrl:
    """Aceito excluído da seleção por URL inválida ou destino proibido (Req 4.4, 4.5)."""

    decisao: DecisaoFonte
    motivo: Literal["url_invalida", "destino_nao_permitido"]


@dataclass(frozen=True)
class SelecaoFontes:
    """Resultado do Seletor_Fontes."""

    selecionadas: tuple[FonteSelecionada, ...]
    excluidas: tuple[ExclusaoUrl, ...]  # ordem de entrada


Desfecho = Literal["armazenado", "reaproveitado", "falha", "url_invalida"]

MotivoFalha = Literal[
    "rede",
    "timeout",
    "status_http",
    "nao_html",
    "tamanho_excedido",
    "corpo_vazio",
    "redirecionamento_nao_permitido",
    "redirecionamentos_excedidos",
    "codificacao_nao_suportada",
    "codificacao_invalida",
    "erro_armazenamento",
    "cancelado",
    # Acréscimos do spec stealth-fallback-integration (Req 8.1), na ordem do design:
    # motivos que o Fallback_Stealth pode devolver e os do próprio coletor.
    "url_invalida",
    "destino_nao_permitido",
    "dominio_nao_permitido_stealth",
    "ambiente_sem_display",
    "navegador_indisponivel",
    "dominio_excluido",
]

# Fonte selecionada não processada porque o Sinal_Cancelamento ficou ativo
# durante a coleta (spec html-extract-on-web-search, Req 6.5).
MOTIVO_CANCELADO: MotivoFalha = "cancelado"

# Camada urllib excluída pela reputação e fonte não elegível ao Fallback_Stealth
# (spec stealth-fallback-integration, Req 6.2).
MOTIVO_DOMINIO_EXCLUIDO: MotivoFalha = "dominio_excluido"

# Trava_Navegador não obtida dentro do prazo, ou perfil do navegador em uso
# (spec stealth-fallback-integration, Req 7.5).
MOTIVO_NAVEGADOR_INDISPONIVEL: MotivoFalha = "navegador_indisponivel"

# Camada_Fetch que buscou a página de uma fonte (spec stealth-fallback-integration).
CamadaFetch = Literal["urllib", "playwright_stealth"]
CAMADA_URLLIB: CamadaFetch = "urllib"
CAMADA_STEALTH: CamadaFetch = "playwright_stealth"


@dataclass(frozen=True)
class EntradaRelatorio:
    """Desfecho de um Resultado_Busca selecionado ou excluído (Req 6.8, 9.4)."""

    codigo_peca: str
    marca_peca: str | None
    url: str  # URL original do resultado
    dominio: str
    confianca: Confianca  # alta | media
    desfecho: Desfecho
    motivo: str | None  # só em falha / url_invalida
    status_http: int | None
    hash_conteudo: str | None  # só em armazenado / reaproveitado
    # Campos_Stealth (spec stealth-fallback-integration, Req 8.1). Com o fallback
    # desligado ficam nos padrões; a igualdade é a mesma de antes da feature.
    camada: CamadaFetch | None = None  # última Camada_Fetch que buscou, ou None sem busca
    motivo_urllib: str | None = None  # motivo urllib que levou ao fallback


@dataclass(frozen=True)
class RelatorioColeta:
    """Saída do Coletor_Paginas (Req 1.7, 6.8)."""

    status: Literal["executada", "nao_enriquecivel"]
    motivo: Literal["marca_sem_referencia"] | None
    codigo_peca: str
    marca_peca: str | None
    entradas: tuple[EntradaRelatorio, ...]  # selecionadas (ordem da seleção) + excluídas
    stealth: bool = False  # Stealth_Efetivo da chamada (spec stealth-fallback-integration)


class CodigoPecaInvalidoError(ValueError):
    """Codigo_Peca ausente, vazio ou sem caractere alfanumérico (Req 1.5, 2.9)."""

    def __init__(self, codigo: object) -> None:
        self.codigo = codigo
        super().__init__(f"Codigo_Peca inválido: {codigo!r}")


class PrecondicaoTravaError(ValueError):
    """Classificador chamado com peça que não passa pela Trava_Entrada (Req 2.10)."""


class ConfiguracaoColetaError(ValueError):
    """Teto_Aceitos ou Janela_Reuso inválidos, com valor e origem (Req 3.9, 6.12)."""

    def __init__(
        self,
        campo: str,
        valor: str,
        origem: Literal["chamada", "ambiente"],
    ) -> None:
        self.campo = campo
        self.valor = valor
        self.origem = origem
        super().__init__(f"configuração inválida de {campo} ({origem}): {valor}")
