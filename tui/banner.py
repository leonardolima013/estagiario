"""Banner de abertura "O ESTAGIARIO": letras em blocos (pyfiglet) com uma sombra
deslocada 1 linha + 1 coluna atrás — cria profundidade sem gradiente.

Cores espelham tui/theme.tcss ($accent-ai, $border) — Rich não lê variável .tcss
por caractere, mesmo padrão já usado em tui/swatches.py pros swatches de peça.
Se o tema mudar de novo, atualizar aqui também.
"""

from __future__ import annotations

import pyfiglet
from rich.text import Text

_COR_LETRA = "#4FD1FF"  # $accent-ai
_COR_SOMBRA = "#2B1B4A"  # $border

TEXTO_BANNER = "ESTAGIARIO"  # sem acento: fontes figlet só cobrem ASCII básico
# ansi_shadow é bloco sólido e grosso (a referência visual pedida); big/block/doom
# como fallback nessa ordem se ansi_shadow não estiver disponível. mini é o último
# recurso, só por largura (terminal estreito demais pras fontes de bloco acima).
_FONTES_PRINCIPAIS = ("ansi_shadow", "big", "block", "doom")
_FONTE_COMPACTA = "mini"


def _linhas_figlet(texto: str, fonte: str) -> list[str]:
    # width=200 evita o pyfiglet quebrar a palavra em várias linhas sozinho —
    # a decisão de largura/fallback é nossa, feita depois, comparando com o terminal.
    try:
        arte = pyfiglet.figlet_format(texto, font=fonte, width=200)
    except pyfiglet.FontNotFound:
        return []
    linhas = arte.rstrip("\n").split("\n")
    while linhas and not linhas[-1].strip():
        linhas.pop()
    if not linhas:
        return []
    largura = max(len(l) for l in linhas)
    return [l.ljust(largura) for l in linhas]


def gerar_banner(largura_disponivel: int, texto: str = TEXTO_BANNER) -> Text | None:
    """Monta o banner (letra + sombra) que caiba em `largura_disponivel` colunas.

    Tenta as fontes de bloco grosso em ordem de preferência (ansi_shadow primeiro);
    cai pra uma fonte compacta se nenhuma delas couber (o deslocamento da sombra
    soma +1 coluna à largura crua do figlet). Retorna None se nem a compacta
    couber — quem chama mostra só o subtítulo.
    """
    for fonte in (*_FONTES_PRINCIPAIS, _FONTE_COMPACTA):
        linhas = _linhas_figlet(texto, fonte)
        if not linhas:
            continue
        if len(linhas[0]) + 1 <= largura_disponivel:
            return _compor_com_sombra(linhas)
    return None


def _compor_com_sombra(linhas: list[str]) -> Text:
    altura, largura = len(linhas), len(linhas[0])
    canvas: list[list[tuple[str, str | None]]] = [
        [(" ", None) for _ in range(largura + 1)] for _ in range(altura + 1)
    ]

    for r, linha in enumerate(linhas):
        for c, ch in enumerate(linha):
            if ch != " ":
                canvas[r + 1][c + 1] = (ch, _COR_SOMBRA)
    for r, linha in enumerate(linhas):
        for c, ch in enumerate(linha):
            if ch != " ":
                canvas[r][c] = (ch, _COR_LETRA)

    texto = Text()
    for r, linha_canvas in enumerate(canvas):
        for ch, cor in linha_canvas:
            texto.append(ch, style=cor)
        if r != len(canvas) - 1:
            texto.append("\n")
    return texto
