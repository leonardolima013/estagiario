"""Cabeçalho fixo no topo da tela: banner 'ESTAGIARIO' em blocos + subtítulo.

Ao contrário da primeira tentativa (SplashScreen, removida), isso nunca some —
fica dockado no topo (dock: top) da mesma Screen onde a área de trabalho
(MenuScreen) roda, os dois visíveis o tempo todo, como Claude Code/Gemini CLI.

Nas telas de execução o cabeçalho fica compacto (`compacto = True`): o banner e
o subtítulo dão lugar a uma linha só, e o espaço vai para o painel. Ele continua
montado e visível; `MenuScreen` liga e desliga o modo conforme a tela ativa.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import Static

from tui.banner import gerar_banner

SUBTITULO = "Data validation agent"
TITULO_COMPACTO = f"ESTAGIÁRIO · {SUBTITULO}"


class Cabecalho(Vertical):
    compacto: reactive[bool] = reactive(False)

    def compose(self) -> ComposeResult:
        largura = self.app.size.width or 80
        banner = gerar_banner(largura)
        if banner is not None:
            yield Static(banner, id="cabecalho-banner")
        yield Static(SUBTITULO, id="cabecalho-subtitulo")
        yield Static(TITULO_COMPACTO, id="cabecalho-compacto")

    def watch_compacto(self, compacto: bool) -> None:
        self.set_class(compacto, "-compacto")
