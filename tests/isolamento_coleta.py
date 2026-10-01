"""Isolamento de ambiente para os testes da feature html-extract-on-web-search.

Req 12.5 e 12.6: a suíte padrão exercita a Integracao_Coleta e a Pesquisa_Desligada
sem criar nem alterar ``db/paginas.db`` na raiz do projeto e sem depender dos
valores do ``.env`` local.

A fixture, via ``monkeypatch``:

- aponta ``ESTAGIARIO_PAGINAS_DB_PATH`` para ``<tmp_path>/paginas.db``;
- fixa ``ESTAGIARIO_COLETA_HABILITADA="1"`` (Chave_Habilitacao habilitada, que é o
  padrão do Req 9.2);
- fixa ``ESTAGIARIO_WEB_VERIFICATION_METODO="serper"`` (método com Resultados_Estruturados);
- remove ``ESTAGIARIO_COLETA_STEALTH_HABILITADA`` (feature stealth-fallback-integration),
  para que o ``.env`` local não ligue o fallback stealth; ausente, a Chave_Stealth
  vale "desabilitada".

Os testes que precisam de outro valor sobrescrevem com ``monkeypatch.setenv`` ou
``monkeypatch.delenv`` dentro do próprio teste. O ``monkeypatch`` desfaz tudo no fim.

Precedência sobre o ``.env``: ``config.py`` chama ``load_dotenv()`` (sem
``override``) só no import, e todas as funções de configuração leem
``os.environ`` no momento da chamada. Este módulo importa ``config`` antes de
qualquer ``setenv``, então o ``.env`` já foi carregado e o valor fixado pela
fixture prevalece. Nenhum valor do ``.env`` é lido ou exibido aqui.

No teardown, a fixture compara existência, tamanho e ``mtime_ns`` de
``db/paginas.db`` na raiz com o instantâneo tirado no setup e falha o teste se
algo mudou.

Uso em um módulo de teste da feature (ativa isolamento e guarda de rede)::

    from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
    from tests.isolamento_coleta import isolamento_coleta_autouse  # noqa: F401
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import config  # noqa: F401  — garante que load_dotenv() já rodou antes do setenv

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
PAGINAS_DB_RAIZ = RAIZ_PROJETO / "db" / "paginas.db"

VALOR_COLETA_HABILITADA = "1"
VALOR_METODO_VERIFICACAO = "serper"


@dataclass(frozen=True)
class EstadoArquivo:
    """Instantâneo de um arquivo: existência, tamanho e mtime em nanossegundos."""

    existe: bool
    tamanho: int | None
    mtime_ns: int | None


@dataclass(frozen=True)
class AmbienteColeta:
    """O que a fixture configurou para o teste."""

    paginas_db_path: Path
    coleta_habilitada: str
    metodo_verificacao: str


def estado_arquivo(caminho: Path) -> EstadoArquivo:
    try:
        st = caminho.stat()
    except FileNotFoundError:
        return EstadoArquivo(existe=False, tamanho=None, mtime_ns=None)
    return EstadoArquivo(existe=True, tamanho=st.st_size, mtime_ns=st.st_mtime_ns)


def verificar_inalterado(antes: EstadoArquivo, caminho: Path) -> None:
    """Falha o teste se ``caminho`` foi criado, removido ou alterado desde ``antes``."""
    depois = estado_arquivo(caminho)
    if depois != antes:
        pytest.fail(
            f"{caminho} foi criado ou alterado durante o teste: antes={antes}, depois={depois}",
            pytrace=False,
        )


def instalar_isolamento_coleta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> AmbienteColeta:
    """Fixa as variáveis de ambiente da feature e devolve o ambiente configurado."""
    caminho = tmp_path / "paginas.db"
    monkeypatch.setenv("ESTAGIARIO_PAGINAS_DB_PATH", str(caminho))
    monkeypatch.setenv("ESTAGIARIO_COLETA_HABILITADA", VALOR_COLETA_HABILITADA)
    monkeypatch.setenv("ESTAGIARIO_WEB_VERIFICATION_METODO", VALOR_METODO_VERIFICACAO)
    monkeypatch.delenv("ESTAGIARIO_COLETA_STEALTH_HABILITADA", raising=False)
    return AmbienteColeta(
        paginas_db_path=caminho,
        coleta_habilitada=VALOR_COLETA_HABILITADA,
        metodo_verificacao=VALOR_METODO_VERIFICACAO,
    )


@pytest.fixture
def isolamento_coleta(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Isola o ambiente do teste e verifica ``db/paginas.db`` da raiz no teardown."""
    antes = estado_arquivo(PAGINAS_DB_RAIZ)
    yield instalar_isolamento_coleta(monkeypatch, tmp_path)
    verificar_inalterado(antes, PAGINAS_DB_RAIZ)


@pytest.fixture(autouse=True)
def isolamento_coleta_autouse(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Versão ``autouse``: basta importá-la no módulo de teste para ativar o isolamento.

    Não depende de ``isolamento_coleta`` para funcionar mesmo quando só ela é
    importada (pytest só registra as fixtures presentes no namespace do módulo).
    """
    antes = estado_arquivo(PAGINAS_DB_RAIZ)
    yield instalar_isolamento_coleta(monkeypatch, tmp_path)
    verificar_inalterado(antes, PAGINAS_DB_RAIZ)
