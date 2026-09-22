"""Cor por id de peça — fio condutor visual entre as seções da tela de resultado
(tabela de peças, particionamento, arbitragem, registro final). Paleta separada
da Abismo & Neon (que já tem significado semântico fixo — IA/usuário/tool/aviso).

Puro — sem dependência de um app Textual ativo, ao contrário de DataTable/Collapsible.
"""

from __future__ import annotations

from rich.text import Text

PALETA_PECAS: list[str] = [
    "#2AB7CA",  # teal
    "#4C6EF5",  # azul
    "#37B24D",  # verde
    "#F06595",  # rosa
    "#845EF7",  # índigo
    "#15AABF",  # ciano
    "#94D82D",  # lima
    "#E64980",  # magenta
]


def atribuir_cores(ids: list[int]) -> dict[int, str]:
    """Atribui uma cor por id, por ordem de primeira aparição, ciclando a cada 8.

    Ids repetidos reaproveitam a cor já atribuída na primeira ocorrência.
    """
    cores: dict[int, str] = {}
    for id_ in ids:
        if id_ not in cores:
            cores[id_] = PALETA_PECAS[len(cores) % len(PALETA_PECAS)]
    return cores


def swatch(cor_hex: str) -> Text:
    """Um '●' colorido pronto pra célula de DataTable."""
    return Text("●", style=f"bold {cor_hex}")
