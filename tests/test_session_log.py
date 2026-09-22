"""T-12: cobre o SessionLogger (obs/session_log.py) — canal de logging estruturado
por sessão. Puro: escreve num tmp_path, nunca na árvore real do projeto.
"""

import obs.session_log as session_log
from obs.session_log import SessionLogger


def test_cria_arquivo_no_logs_dir_com_prefixo(tmp_path):
    logger = SessionLogger("verificacao_web", logs_dir=tmp_path)

    assert logger.path.exists()
    assert logger.path.parent == tmp_path
    assert logger.path.name.startswith("verificacao_web_")
    assert logger.path.suffix == ".log"


def test_registrar_adiciona_linha_timestampada(tmp_path):
    logger = SessionLogger("s", logs_dir=tmp_path)

    logger.registrar("evento A")
    logger.registrar("evento B")

    conteudo = logger.path.read_text(encoding="utf-8")
    linhas = [l for l in conteudo.splitlines() if l.strip()]
    assert len(linhas) == 2
    assert linhas[0].endswith("evento A")
    assert linhas[1].endswith("evento B")


def test_e_callable_compativel_com_on_aviso(tmp_path):
    # SessionLogger deve encaixar onde se espera on_aviso: Callable[[str], None].
    logger = SessionLogger("s", logs_dir=tmp_path)

    def usa_como_on_aviso(on_aviso):
        on_aviso("mensagem via callback")

    usa_como_on_aviso(logger)

    assert "mensagem via callback" in logger.path.read_text(encoding="utf-8")


def test_usa_logs_dir_do_modulo_por_padrao(tmp_path, monkeypatch):
    # Sem logs_dir explícito, usa o _LOGS_DIR do módulo (monkeypatchável em teste).
    monkeypatch.setattr(session_log, "_LOGS_DIR", tmp_path)

    logger = SessionLogger("s")

    assert logger.path.parent == tmp_path
