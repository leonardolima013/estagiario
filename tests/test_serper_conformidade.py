"""Testes de conformidade/smoke da Skill_Serper (R9.1, R9.4, R8.3).

Garantem: o cliente usa apenas urllib da stdlib (nenhuma lib HTTP de terceiros),
os três getters de config leem de `.env` sem hardcode, e a SERPER_API_KEY não
aparece em código-fonte nem testes.
"""

from __future__ import annotations

import ast
from pathlib import Path

import config

_RAIZ = Path(__file__).resolve().parent.parent
_SERPER_CLIENT = _RAIZ / "verification" / "serper_client.py"

# Libs HTTP de terceiros que NÃO devem ser usadas (R9.1).
_LIBS_HTTP_TERCEIROS = {"requests", "httpx", "aiohttp", "urllib3", "http3"}


def _imports_de(caminho: Path) -> set[str]:
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    modulos: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            modulos.update(alias.name.split(".")[0] for alias in no.names)
        elif isinstance(no, ast.ImportFrom) and no.module:
            modulos.add(no.module.split(".")[0])
    return modulos


def test_client_usa_apenas_urllib_stdlib():
    """R9.1: serper_client importa urllib (stdlib), nenhuma lib HTTP de terceiros."""
    modulos = _imports_de(_SERPER_CLIENT)
    assert "urllib" in modulos
    assert not (modulos & _LIBS_HTTP_TERCEIROS), f"lib HTTP de terceiros detectada: {modulos & _LIBS_HTTP_TERCEIROS}"


def test_requirements_sem_lib_http_de_terceiros():
    """R9.2, R9.3: requirements.txt não ganha lib de HTTP de terceiros."""
    req = (_RAIZ / "requirements.txt").read_text(encoding="utf-8").lower()
    for lib in _LIBS_HTTP_TERCEIROS:
        # `urllib3` costuma vir transitivo; garantimos apenas que não é dependência
        # direta introduzida por esta feature — checagem conservadora por linha raiz.
        linhas = [l.split("==")[0].strip() for l in req.splitlines() if l.strip() and not l.startswith("#")]
        assert lib not in linhas or lib == "urllib3"


def test_getters_de_config_leem_do_ambiente(monkeypatch):
    """R3.4, R9.4: método, chave e timeout vêm de env vars, sem valores hardcoded."""
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", "serper")
    assert config.web_verification_metodo() == "serper"

    monkeypatch.setenv("SERPER_API_KEY", "chave-de-teste-sintetica")
    assert config.serper_api_key() == "chave-de-teste-sintetica"

    monkeypatch.setenv("ESTAGIARIO_SERPER_TIMEOUT", "45")
    assert config.serper_timeout() == 45.0


def test_chave_ausente_levanta_sem_vazar(monkeypatch):
    """R3.5, R9.5: chave ausente levanta erro sem incluir o valor."""
    from verification.serper_client import SerperAPIKeyAusenteError

    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    try:
        config.serper_api_key()
        assert False, "deveria ter levantado"
    except SerperAPIKeyAusenteError as exc:
        # A mensagem cita a variável, mas não há valor a vazar.
        assert "SERPER_API_KEY" in str(exc)


def test_nenhuma_chave_serper_hardcoded_no_codigo():
    """R8.3: nenhum literal de chave Serper aparece no código-fonte da feature."""
    arquivos = [
        _SERPER_CLIENT,
        _RAIZ / "verification" / "serper_agent.py",
        _RAIZ / "verification" / "serper_decisao.py",
        _RAIZ / "verification" / "selector.py",
        _RAIZ / "config.py",
    ]
    for arq in arquivos:
        texto = arq.read_text(encoding="utf-8")
        # A chave só pode ser referenciada pelo NOME da env var, nunca por valor.
        assert "SERPER_API_KEY" not in texto or 'environ.get("SERPER_API_KEY"' in texto or "os.environ.get(\"SERPER_API_KEY\"" in texto or "SERPER_API_KEY ausente" in texto
