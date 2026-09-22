"""Cabeçalho fixo no topo da tela: banner 'ESTAGIARIO' em blocos + subtítulo.

Ao contrário da primeira tentativa (SplashScreen, removida), isso nunca some —
fica dockado no topo (dock: top) da mesma Screen onde a área de trabalho
(MenuScreen) roda, os dois visíveis o tempo todo, como Claude Code/Gemini CLI.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from tui.banner import gerar_banner

SUBTITULO = "Data validation agent"


class Cabecalho(Vertical):
    def compose(self) -> ComposeResult:
        largura = self.app.size.width or 80
        banner = gerar_banner(largura)
        if banner is not None:
            yield Static(banner, id="cabecalho-banner")
        yield Static(SUBTITULO, id="cabecalho-subtitulo")
