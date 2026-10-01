"""Integração real do fallback stealth (opt-in: ESTAGIARIO_RUN_STEALTH_TESTS=1).

Abre o Google Chrome do sistema, headed, com o perfil de
``config.stealth_profile_dir()``, e acessa a rede (ip-api.com, se o fuso não
estiver em cache, e mercadocar.com.br). Exige display e o perfil livre (feche
``browserscan/main.py`` antes). Nunca roda na suíte padrão.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("ESTAGIARIO_RUN_STEALTH_TESTS") != "1",
    reason="integração real do stealth: defina ESTAGIARIO_RUN_STEALTH_TESTS=1",
)


def test_diagnosticar_mercadocar() -> None:
    from tools.buscador_stealth import diagnosticar_stealth

    saida = diagnosticar_stealth("https://www.mercadocar.com.br/")
    print(json.dumps(saida, ensure_ascii=False, indent=2))

    assert saida["desfecho"] in ("pagina", "falha")
    assert all(set(cookie) == {"nome", "dominio"} for cookie in saida["cookies"])



def test_coletor_com_fallback_real_apos_403(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Req 12.5: ``ColetorPaginas.coletar(stealth=True)`` com o Fallback_Stealth
    real (padrão ``coletar_via_playwright_stealth`` e ``TRAVA_NAVEGADOR``).

    O Transporte_HTTP é fake e devolve 403 para a URL do mercadocar, então o
    fallback é acionado; o armazém fica em ``tmp_path``. A Allowlist_Stealth é
    a padrão de ``config`` (variável removida só neste teste).
    """
    import config
    from coleta_paginas.coletor import ColetorPaginas
    from db.armazem_paginas import ArmazemPaginas
    from tests.test_coletor_paginas import TransporteRoteiro, peca, resultado
    from tools import buscador_stealth as bs

    url = "https://www.mercadocar.com.br/"
    dominio = "www.mercadocar.com.br"
    monkeypatch.delenv("ESTAGIARIO_COLETA_DOMINIOS_STEALTH", raising=False)
    assert config.coleta_dominios_stealth() == frozenset({"mercadocar.com.br"})

    armazem = ArmazemPaginas(
        tmp_path / "paginas.db", caminho_rule_store=tmp_path / "rule_store.db"
    )
    transporte = TransporteRoteiro({url: (403, {}, b"")})
    coletor = ColetorPaginas(armazem, transporte=transporte)

    relatorio = coletor.coletar(peca(), [resultado(url, 1)], stealth=True)
    print(json.dumps([repr(e) for e in relatorio.entradas], ensure_ascii=False, indent=2))

    assert transporte.chamadas == [url]
    assert not bs.TRAVA_NAVEGADOR.locked()
    assert len(relatorio.entradas) == 1
    entrada = relatorio.entradas[0]
    assert entrada.desfecho == "armazenado", (entrada.motivo, entrada.status_http)
    assert entrada.camada == "playwright_stealth"
    assert entrada.motivo_urllib == "status_http"
    assert entrada.hash_conteudo
    assert armazem.ler_conteudo(entrada.hash_conteudo)

    (urllib,) = armazem.ultimas_tentativas(dominio, "urllib")
    assert (urllib.sucesso, urllib.motivo, urllib.status_http) == (False, "status_http", 403)

    (stealth,) = armazem.ultimas_tentativas(dominio, "playwright_stealth")
    assert stealth.sucesso is True
    assert stealth.motivo is None
    assert stealth.status_http == entrada.status_http
    assert urllib.id < stealth.id
