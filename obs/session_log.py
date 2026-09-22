"""Logging estruturado leve por sessão (T-12, base de observabilidade pra Fase 5).

Objetivo: ter um canal único e persistente pros avisos/eventos que hoje só
passam por `on_aviso`/`on_evento` (prints na TUI, efêmeros). NÃO muda a semântica
de nada — é só um canal a mais: um `SessionLogger` é um callable compatível com a
assinatura de `on_aviso: Callable[[str], None]`, então pode ser injetado nos
mesmos pontos (pipeline, verificação web) sem alterar o fluxo.

Escreve uma linha timestampada por evento em `<PROJECT_ROOT>/logs/sessao_<ts>.log`.
`_LOGS_DIR` é módulo-level (mesmo padrão de tui/screens/executar_caso_screen.py)
pra que testes façam `monkeypatch.setattr(session_log, "_LOGS_DIR", tmp_path)` em
vez de escrever na árvore real do projeto.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
_LOGS_DIR = PROJECT_ROOT / "logs"


class SessionLogger:
    """Logger por sessão. Uso:

        logger = SessionLogger("verificacao_web")
        logger("Subindo servidor MCP...")   # callable -> compatível com on_aviso
        executar_caso(..., on_aviso=logger)
    """

    def __init__(self, prefixo: str = "sessao", *, logs_dir: Path | None = None) -> None:
        base = logs_dir if logs_dir is not None else _LOGS_DIR
        base.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._path = base / f"{prefixo}_{ts}.log"
        # cria o arquivo já na construção pra o caminho existir antes do 1º evento.
        self._path.touch()

    @property
    def path(self) -> Path:
        return self._path

    def registrar(self, mensagem: str) -> None:
        linha = f"{datetime.now().isoformat(timespec='seconds')}  {mensagem}\n"
        with self._path.open("a", encoding="utf-8") as f:
            f.write(linha)

    # callable -> encaixa direto onde se espera on_aviso/on_evento: Callable[[str], None]
    def __call__(self, mensagem: str) -> None:
        self.registrar(mensagem)
