"""Testes unitários do Coletor_Paginas (html-extract-save, tarefa 9.11).

Tudo roda sem rede: o transporte é um roteiro fake (URL → resposta ou erro) que
registra as chamadas, o armazém vive em ``tmp_path`` (com o caminho do RuleStore
também em ``tmp_path``), a configuração é injetada e o relógio é fake, em UTC.
As leituras diretas por ``sqlite3`` só inspecionam o arquivo temporário e
injetam falhas por trigger; o código de teste não é componente de produção.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coleta_paginas.coletor import (
    ATRIBUTO_EVENTO,
    CAMPOS_EVENTO_ENTRADA,
    CAMPOS_EVENTO_RESUMO,
    EVENTO_ENTRADA,
    EVENTO_RESUMO,
    ColetorPaginas,
    ConfigColeta,
    resolver_janela_reuso,
)
from coleta_paginas.modelos import (
    CodigoPecaInvalidoError,
    Confianca,
    ConfiguracaoColetaError,
    PecaConsultada,
    RelatorioColeta,
    ResultadoBusca,
)
from db.armazem_paginas import ArmazemPaginas, NovoRegistroColeta
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401
from tools.buscador_paginas import ErroTransporte, RespostaHTTP

CODIGO = "JE4699"
MARCA = "Bosch"
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
CABECALHOS_HTML = {"content-type": "text/html; charset=utf-8"}


# ---------------------------------------------------------------------------
# Fakes e auxiliares
# ---------------------------------------------------------------------------


class Relogio:
    """Relógio fake em UTC, compartilhado por coletor e armazém."""

    def __init__(self, agora: datetime = T0) -> None:
        self.agora = agora

    def __call__(self) -> datetime:
        return self.agora

    def avancar(self, delta: timedelta) -> None:
        self.agora += delta


class TransporteRoteiro:
    """Transporte fake: URL → (status, cabeçalhos, corpo) ou ``ErroTransporte``.

    Cada chamada monta uma ``RespostaHTTP`` nova (o corpo é consumível) e é
    registrada em ``chamadas``. URL fora do roteiro derruba o teste.
    """

    def __init__(self, roteiro: Mapping[str, tuple[int, dict[str, str], bytes] | ErroTransporte]):
        self.roteiro = dict(roteiro)
        self.chamadas: list[str] = []

    def __call__(self, url: str, cabecalhos: Mapping[str, str], timeout: float) -> RespostaHTTP:
        self.chamadas.append(url)
        if url not in self.roteiro:
            raise AssertionError(f"URL inesperada no transporte: {url}")
        item = self.roteiro[url]
        if isinstance(item, ErroTransporte):
            raise item
        status, cabs, corpo = item
        return RespostaHTTP(status=status, cabecalhos=dict(cabs), blocos=iter([corpo]))


def ok(corpo: bytes) -> tuple[int, dict[str, str], bytes]:
    return (200, CABECALHOS_HTML, corpo)


def config_coleta(
    *, teto: str | None = None, janela: int | str | None = 30
) -> ConfigColeta:
    return ConfigColeta(timeout_s=5.0, teto_aceitos_ambiente=teto, janela_reuso_dias=janela)


def resultado(
    url: str | None,
    posicao: int | None,
    *,
    com_codigo: bool = True,
    com_marca: bool = True,
    codigo: str = CODIGO,
) -> ResultadoBusca:
    partes = ["Filtro de óleo"]
    if com_codigo:
        partes.append(codigo)
    if com_marca:
        partes.append(MARCA)
    return ResultadoBusca(
        titulo=" ".join(partes),
        snippet="Peça automotiva",
        url=url,
        dominio=None,
        posicao=posicao,
    )


def peca(codigo: str | None = CODIGO, marca: str | None = MARCA) -> PecaConsultada:
    return PecaConsultada(codigo_peca=codigo, marca_peca=marca, nomes_candidatos=())


@pytest.fixture
def relogio() -> Relogio:
    return Relogio()


@pytest.fixture
def armazem(tmp_path: Path, relogio: Relogio) -> ArmazemPaginas:
    return ArmazemPaginas(
        tmp_path / "paginas.db",
        caminho_rule_store=tmp_path / "rule_store.db",
        relogio=relogio,
    )


def novo_coletor(
    armazem: ArmazemPaginas,
    transporte: TransporteRoteiro,
    relogio: Relogio,
    config: ConfigColeta | None = None,
) -> ColetorPaginas:
    return ColetorPaginas(
        armazem,
        transporte=transporte,
        config=config or config_coleta(),
        relogio=relogio,
    )


def snapshot(armazem: ArmazemPaginas) -> dict[str, list[tuple]]:
    with closing(sqlite3.connect(armazem.db_path)) as conn:
        return {
            tabela: conn.execute(f"SELECT * FROM {tabela} ORDER BY 1").fetchall()
            for tabela in ("conteudo_pagina", "registro_coleta")
        }


def contar(armazem: ArmazemPaginas, tabela: str) -> int:
    with closing(sqlite3.connect(armazem.db_path)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


def sha(corpo: bytes) -> str:
    return hashlib.sha256(corpo).hexdigest()


def registro_origem(url: str, coletado_em: datetime) -> NovoRegistroColeta:
    return NovoRegistroColeta(
        codigo_peca="OUTRA1",
        marca_peca="Mann",
        url_original=url,
        url_normalizada=url,
        url_final=url + "/final",
        dominio="b.com",
        confianca="media",
        codigo_confirmado=True,
        marca_confirmada=False,
        nome_reforcado=False,
        motivo_decisao="origem",
        status_http=203,
        content_type="text/html",
        charset="iso-8859-1",
        coletado_em=coletado_em,
    )


def eventos(caplog: pytest.LogCaptureFixture, nome: str) -> list[dict]:
    return [
        getattr(r, ATRIBUTO_EVENTO)
        for r in caplog.records
        if r.name == "coleta_paginas"
        and getattr(r, ATRIBUTO_EVENTO, {}).get("evento") == nome
    ]


# ---------------------------------------------------------------------------
# Ponta a ponta
# ---------------------------------------------------------------------------


def test_ponta_a_ponta_armazenado_reaproveitado_e_falha(armazem, relogio):
    url_a, url_b, url_c = "https://a.com/p", "https://b.com/p", "https://c.com/p"
    corpo_a, corpo_b = b"<html>A</html>", b"<html>B</html>"
    origem_em = T0 - timedelta(days=5)
    hash_b = armazem.gravar_coleta(corpo_b, registro_origem(url_b, origem_em))

    transporte = TransporteRoteiro({url_a: ok(corpo_a), url_c: ErroTransporte("timeout")})
    coletor = novo_coletor(armazem, transporte, relogio)
    relatorio = coletor.coletar(
        peca(),
        [resultado(url_a, 1), resultado(url_b, 2), resultado(url_c, 3)],
    )

    assert relatorio.status == "executada"
    assert relatorio.motivo is None
    assert [(e.url, e.desfecho) for e in relatorio.entradas] == [
        (url_a, "armazenado"),
        (url_b, "reaproveitado"),
        (url_c, "falha"),
    ]
    armazenado, reaproveitado, falha = relatorio.entradas
    assert armazenado.hash_conteudo == sha(corpo_a) and armazenado.status_http == 200
    assert armazenado.motivo is None
    assert reaproveitado.hash_conteudo == hash_b and reaproveitado.status_http == 203
    assert reaproveitado.motivo is None
    assert falha.motivo == "timeout" and falha.hash_conteudo is None
    assert all(e.confianca is Confianca.ALTA for e in relatorio.entradas)
    assert all(e.codigo_peca == CODIGO and e.marca_peca == MARCA for e in relatorio.entradas)
    # B reaproveitado: sem HTTP.
    assert transporte.chamadas == [url_a, url_c]

    registros = {r.url_normalizada: r for r in armazem.registros_da_peca(CODIGO, MARCA)}
    assert set(registros) == {url_a, url_b}
    assert armazem.ler_conteudo(registros[url_a].hash_conteudo) == corpo_a
    assert registros[url_a].reaproveitado is False
    assert registros[url_a].coletado_em == T0
    vinculo = registros[url_b]
    assert vinculo.reaproveitado is True
    assert vinculo.hash_conteudo == hash_b
    # Dados de fetch copiados da origem; decisão é a atual.
    assert vinculo.coletado_em == origem_em
    assert (vinculo.status_http, vinculo.content_type, vinculo.charset, vinculo.url_final) == (
        203, "text/html", "iso-8859-1", url_b + "/final",
    )
    assert vinculo.confianca == "alta" and vinculo.marca_confirmada is True
    assert vinculo.registrado_em == T0


def test_ordem_do_relatorio_selecao_e_depois_exclusoes(armazem, relogio):
    url_media, url_alta = "https://m.com/p", "https://a.com/p"
    url_invalida, url_privada = "ftp://x.com/p", "http://127.0.0.1/p"
    transporte = TransporteRoteiro({url_media: ok(b"m"), url_alta: ok(b"a")})
    coletor = novo_coletor(armazem, transporte, relogio)

    relatorio = coletor.coletar(
        peca(),
        [
            resultado(url_media, 1, com_marca=False),
            resultado(url_invalida, 2),
            resultado("https://rej.com/p", 3, com_codigo=False),
            resultado(url_alta, 4),
            resultado(url_privada, 5),
        ],
    )

    assert [(e.url, e.desfecho, e.motivo) for e in relatorio.entradas] == [
        (url_alta, "armazenado", None),
        (url_media, "armazenado", None),
        (url_invalida, "url_invalida", "url_invalida"),
        (url_privada, "url_invalida", "destino_nao_permitido"),
    ]
    assert [e.confianca for e in relatorio.entradas[:2]] == [Confianca.ALTA, Confianca.MEDIA]
    # Rejeitado não gera entrada nem HTTP.
    assert "https://rej.com/p" not in {e.url for e in relatorio.entradas}
    assert transporte.chamadas == [url_alta, url_media]
    exclusoes = relatorio.entradas[2:]
    assert all(e.status_http is None and e.hash_conteudo is None for e in exclusoes)
    assert exclusoes[0].dominio == "x.com" and exclusoes[1].dominio == "127.0.0.1"


def test_falha_de_fetch_nao_e_substituida_alem_do_teto(armazem, relogio):
    urls = [f"https://s{i}.com/p" for i in range(3)]
    transporte = TransporteRoteiro(
        {urls[0]: (500, CABECALHOS_HTML, b"erro"), urls[1]: ok(b"ok"), urls[2]: ok(b"x")}
    )
    coletor = novo_coletor(armazem, transporte, relogio)

    relatorio = coletor.coletar(peca(), [resultado(u, i + 1) for i, u in enumerate(urls)], teto_aceitos=2)

    assert [(e.url, e.desfecho) for e in relatorio.entradas] == [
        (urls[0], "falha"),
        (urls[1], "armazenado"),
    ]
    assert relatorio.entradas[0].motivo == "status_http"
    assert relatorio.entradas[0].status_http == 500
    assert transporte.chamadas == urls[:2]
    assert contar(armazem, "registro_coleta") == 1


# ---------------------------------------------------------------------------
# Ordem: código → trava → configuração
# ---------------------------------------------------------------------------


def test_codigo_invalido_levanta_antes_da_trava(armazem, relogio):
    transporte = TransporteRoteiro({})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(teto="0", janela="x"))
    antes = snapshot(armazem)

    for codigo in (None, "", "   ", "--/.."):
        with pytest.raises(CodigoPecaInvalidoError):
            coletor.coletar(peca(codigo, "OEM"), [resultado("https://a.com/p", 1)], teto_aceitos=0)

    assert transporte.chamadas == []
    assert snapshot(armazem) == antes


@pytest.mark.parametrize("marca", [None, "", "   ", "OEM", "conversão", " Original  OEM "])
def test_trava_disparada_com_config_invalida_devolve_nao_enriquecivel(
    armazem, relogio, marca
):
    transporte = TransporteRoteiro({})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(teto="abc", janela="0"))
    antes = snapshot(armazem)

    relatorio = coletor.coletar(
        peca(marca=marca), [resultado("https://a.com/p", 1)], teto_aceitos=0
    )

    assert relatorio == RelatorioColeta(
        status="nao_enriquecivel",
        motivo="marca_sem_referencia",
        codigo_peca=CODIGO,
        marca_peca=marca,
        entradas=(),
    )
    assert transporte.chamadas == []
    assert snapshot(armazem) == antes


def test_teto_invalido_na_chamada_levanta_sem_http_nem_gravacao(armazem, relogio):
    transporte = TransporteRoteiro({"https://a.com/p": ok(b"a")})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(teto="2"))

    for teto in (0, -1, True, 2.0):
        with pytest.raises(ConfiguracaoColetaError) as exc:
            coletor.coletar(peca(), [resultado("https://a.com/p", 1)], teto_aceitos=teto)
        assert exc.value.campo == "teto_aceitos"
        assert exc.value.origem == "chamada"
        assert exc.value.valor == repr(teto)

    assert transporte.chamadas == []
    assert contar(armazem, "registro_coleta") == 0
    assert contar(armazem, "conteudo_pagina") == 0


def test_teto_invalido_no_ambiente_levanta_com_origem_ambiente(armazem, relogio):
    transporte = TransporteRoteiro({})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(teto="0"))

    with pytest.raises(ConfiguracaoColetaError) as exc:
        coletor.coletar(peca(), [resultado("https://a.com/p", 1)])

    assert (exc.value.campo, exc.value.origem) == ("teto_aceitos", "ambiente")
    assert transporte.chamadas == []


@pytest.mark.parametrize("janela", [0, 366, "0", "abc", "1.5", True, ""])
def test_janela_invalida_levanta_sem_http_nem_gravacao(armazem, relogio, janela):
    transporte = TransporteRoteiro({})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(janela=janela))

    with pytest.raises(ConfiguracaoColetaError) as exc:
        coletor.coletar(peca(), [resultado("https://a.com/p", 1)])

    assert exc.value.campo == "janela_reuso_dias"
    assert exc.value.valor == repr(janela)
    assert transporte.chamadas == []
    assert contar(armazem, "registro_coleta") == 0


def test_janela_com_zeros_a_esquerda_alem_do_limite_de_int():
    """Zeros à esquerda além do limite de dígitos de int() não vazam ValueError."""
    assert resolver_janela_reuso("0" * 4301 + "30") == 30

    valor = "0" * 4301 + "366"
    with pytest.raises(ConfiguracaoColetaError) as exc:
        resolver_janela_reuso(valor)
    assert exc.value.campo == "janela_reuso_dias"
    assert exc.value.valor == repr(valor)


# ---------------------------------------------------------------------------
# Falha de armazenamento
# ---------------------------------------------------------------------------


def test_falha_de_armazenamento_numa_fonte_nao_interrompe_as_seguintes(armazem, relogio):
    urls = ["https://a.com/p", "https://b.com/p", "https://c.com/p"]
    corpos = [b"<p>a</p>", b"<p>b</p>", b"<p>c</p>"]
    with closing(sqlite3.connect(armazem.db_path)) as conn:
        conn.execute(
            "CREATE TRIGGER falha_b BEFORE INSERT ON registro_coleta "
            f"WHEN NEW.url_normalizada = '{urls[1]}' "
            "BEGIN SELECT RAISE(ABORT, 'falha injetada'); END"
        )
        conn.commit()
    transporte = TransporteRoteiro(dict(zip(urls, map(ok, corpos))))
    coletor = novo_coletor(armazem, transporte, relogio)

    relatorio = coletor.coletar(peca(), [resultado(u, i + 1) for i, u in enumerate(urls)])

    assert [(e.url, e.desfecho, e.motivo) for e in relatorio.entradas] == [
        (urls[0], "armazenado", None),
        (urls[1], "falha", "erro_armazenamento"),
        (urls[2], "armazenado", None),
    ]
    assert relatorio.entradas[1].hash_conteudo is None
    assert transporte.chamadas == urls
    # Transação atômica: nem o conteúdo de B ficou gravado.
    with closing(sqlite3.connect(armazem.db_path)) as conn:
        hashes = {h for (h,) in conn.execute("SELECT hash_conteudo FROM conteudo_pagina")}
    assert hashes == {sha(corpos[0]), sha(corpos[2])}
    assert {r.url_normalizada for r in armazem.registros_da_peca(CODIGO, MARCA)} == {
        urls[0], urls[2],
    }


# ---------------------------------------------------------------------------
# Janela de reuso e vínculo idempotente
# ---------------------------------------------------------------------------


def test_limite_da_janela_exato_reaproveita_e_alem_rebusca(armazem, relogio):
    url = "https://b.com/p"
    armazem.gravar_coleta(b"velho", registro_origem(url, T0))
    transporte = TransporteRoteiro({url: ok(b"novo")})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(janela=30))

    relogio.agora = T0 + timedelta(days=30)
    no_limite = coletor.coletar(peca(), [resultado(url, 1)])
    assert [e.desfecho for e in no_limite.entradas] == ["reaproveitado"]
    assert transporte.chamadas == []

    relogio.agora = T0 + timedelta(days=30, seconds=1)
    alem = coletor.coletar(peca(), [resultado(url, 1)])
    assert [e.desfecho for e in alem.entradas] == ["armazenado"]
    assert alem.entradas[0].hash_conteudo == sha(b"novo")
    assert transporte.chamadas == [url]


def test_reaproveitamento_nao_renova_a_janela_em_cadeia(armazem, relogio):
    url = "https://b.com/p"
    armazem.gravar_coleta(b"original", registro_origem(url, T0))
    transporte = TransporteRoteiro({url: ok(b"refeito")})
    coletor = novo_coletor(armazem, transporte, relogio, config_coleta(janela=30))

    relogio.agora = T0 + timedelta(days=29)
    primeiro = coletor.coletar(peca(), [resultado(url, 1)])
    assert primeiro.entradas[0].desfecho == "reaproveitado"
    (vinculo,) = armazem.registros_da_peca(CODIGO, MARCA)
    assert vinculo.coletado_em == T0
    assert vinculo.registrado_em == T0 + timedelta(days=29)

    # Se o vínculo tivesse renovado coletado_em, isto ainda seria reaproveitado.
    relogio.agora = T0 + timedelta(days=31)
    segundo = coletor.coletar(peca(), [resultado(url, 1)])
    assert segundo.entradas[0].desfecho == "armazenado"
    assert transporte.chamadas == [url]


def test_vinculo_reaproveitado_e_idempotente_em_chamadas_repetidas(armazem, relogio):
    urls = ["https://a.com/p", "https://b.com/p"]
    transporte = TransporteRoteiro({u: ok(u.encode()) for u in urls})
    coletor = novo_coletor(armazem, transporte, relogio)
    resultados = [resultado(u, i + 1) for i, u in enumerate(urls)]

    primeiro = coletor.coletar(peca(), resultados)
    assert [e.desfecho for e in primeiro.entradas] == ["armazenado", "armazenado"]
    for _ in range(3):
        relogio.avancar(timedelta(days=1))
        repetido = coletor.coletar(peca(), resultados)
        assert [e.desfecho for e in repetido.entradas] == ["reaproveitado", "reaproveitado"]
        assert [e.hash_conteudo for e in repetido.entradas] == [
            e.hash_conteudo for e in primeiro.entradas
        ]

    assert transporte.chamadas == urls
    assert len(armazem.registros_da_peca(CODIGO, MARCA)) == 2
    assert contar(armazem, "registro_coleta") == 2

    # Outra peça com as mesmas URLs: exatamente um vínculo novo por URL, sem HTTP.
    outra = peca("XY123", MARCA)
    for _ in range(2):
        rel = coletor.coletar(
            outra, [resultado(u, i + 1, codigo="XY123") for i, u in enumerate(urls)]
        )
        assert [e.desfecho for e in rel.entradas] == ["reaproveitado", "reaproveitado"]
    registros_outra = armazem.registros_da_peca("XY123", MARCA)
    assert len(registros_outra) == 2
    assert all(r.reaproveitado for r in registros_outra)
    assert transporte.chamadas == urls
    assert contar(armazem, "registro_coleta") == 4


# ---------------------------------------------------------------------------
# Logging estruturado
# ---------------------------------------------------------------------------


def test_eventos_de_entrada_e_resumo_no_log(armazem, relogio, caplog):
    caplog.set_level(logging.INFO, logger="coleta_paginas")
    marcador = b"MARCADOR-CORPO-7f3a"
    url_ok, url_falha, url_inv = "https://a.com/p", "https://b.com/p", "ftp://c.com/p"
    transporte = TransporteRoteiro({url_ok: ok(marcador), url_falha: ErroTransporte("rede")})
    coletor = novo_coletor(armazem, transporte, relogio)

    relatorio = coletor.coletar(
        peca(), [resultado(url_ok, 1), resultado(url_falha, 2), resultado(url_inv, 3)]
    )

    entradas_log = eventos(caplog, EVENTO_ENTRADA)
    assert len(entradas_log) == len(relatorio.entradas) == 3
    for evento, entrada in zip(entradas_log, relatorio.entradas):
        assert set(evento) == CAMPOS_EVENTO_ENTRADA
        assert evento["url"] == entrada.url
        assert evento["desfecho"] == entrada.desfecho
        assert evento["confianca"] == str(entrada.confianca)
        assert evento["hash_conteudo"] == entrada.hash_conteudo

    (resumo,) = eventos(caplog, EVENTO_RESUMO)
    assert set(resumo) == CAMPOS_EVENTO_RESUMO
    assert resumo["status"] == "executada"
    assert resumo["motivo"] is None
    assert (resumo["codigo_peca"], resumo["marca_peca"]) == (CODIGO, MARCA)
    assert resumo["contagem"] == {
        "armazenado": 1, "reaproveitado": 0, "falha": 1, "url_invalida": 1,
    }
    # O resumo é o último evento, e nenhum log carrega o corpo da página.
    assert getattr(caplog.records[-1], ATRIBUTO_EVENTO)["evento"] == EVENTO_RESUMO
    assert marcador.decode() not in caplog.text
    assert marcador.decode() not in repr(relatorio)


def test_resumo_nao_enriquecivel_no_log(armazem, relogio, caplog):
    caplog.set_level(logging.INFO, logger="coleta_paginas")
    coletor = novo_coletor(armazem, TransporteRoteiro({}), relogio)

    coletor.coletar(peca(marca="OEM"), [resultado("https://a.com/p", 1)])

    assert eventos(caplog, EVENTO_ENTRADA) == []
    (resumo,) = eventos(caplog, EVENTO_RESUMO)
    assert resumo == {
        "evento": EVENTO_RESUMO,
        "status": "nao_enriquecivel",
        "motivo": "marca_sem_referencia",
        "codigo_peca": CODIGO,
        "marca_peca": "OEM",
        "contagem": {"armazenado": 0, "reaproveitado": 0, "falha": 0, "url_invalida": 0},
    }
