"""Testes unitários do Armazem_Paginas (html-extract-save, tarefa 7.13).

Todos os arquivos vivem em `tmp_path`; o `db/` real do projeto nunca é aberto. As
leituras diretas por `sqlite3` servem só para inspecionar o que foi gravado (Req 8.5)
e para injetar falhas por trigger; o código de teste não é componente de produção.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from db.armazem_paginas import (
    ArmazemPaginas,
    ConflitoCaminhoArmazemError,
    ConteudoNaoEncontradoError,
    DestinoExistenteError,
    ErroArmazenamento,
    NovoRegistroColeta,
)
from tests.guarda_rede import guarda_rede_autouse  # noqa: F401

INSTANTE_BASE = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
HASH_AUSENTE = "f" * 64


# ---------------------------------------------------------------------------
# Auxiliares
# ---------------------------------------------------------------------------


def _relogio_fixo(instante: datetime = INSTANTE_BASE) -> Callable[[], datetime]:
    return lambda: instante


def _armazem(tmp_path: Path, relogio: Callable[[], datetime] | None = None) -> ArmazemPaginas:
    return ArmazemPaginas(
        tmp_path / "paginas.db",
        caminho_rule_store=tmp_path / "rule_store.db",
        relogio=relogio or _relogio_fixo(),
    )


def _registro(**alteracoes: object) -> NovoRegistroColeta:
    base = NovoRegistroColeta(
        codigo_peca="JE-4699",
        marca_peca="Bosch",
        url_original="https://loja.example/peca/je-4699?utm_source=x",
        url_normalizada="https://loja.example/peca/je-4699",
        url_final="https://loja.example/peca/je-4699",
        dominio="loja.example",
        confianca="alta",
        codigo_confirmado=True,
        marca_confirmada=True,
        nome_reforcado=False,
        motivo_decisao="codigo_e_marca_confirmados",
        status_http=200,
        content_type="text/html; charset=utf-8",
        charset="utf-8",
        coletado_em=INSTANTE_BASE,
    )
    return replace(base, **alteracoes)


def _tabelas(caminho: Path) -> set[str]:
    with closing(sqlite3.connect(caminho)) as conn:
        return {
            linha[0]
            for linha in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            )
        }


def _contar(caminho: Path, tabela: str) -> int:
    with closing(sqlite3.connect(caminho)) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {tabela}").fetchone()[0]


def _criar_rule_store_falso(caminho: Path) -> None:
    """Arquivo SQLite qualquer representando o RuleStore (com uma tabela própria)."""
    with closing(sqlite3.connect(caminho)) as conn:
        conn.execute("CREATE TABLE regra (id INTEGER PRIMARY KEY)")
        conn.commit()


# ---------------------------------------------------------------------------
# Caminho do arquivo (Req 7.10)
# ---------------------------------------------------------------------------


def test_caminho_padrao_e_db_paginas_na_raiz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ESTAGIARIO_PAGINAS_DB_PATH", raising=False)
    assert config.paginas_db_path() == config.PROJECT_ROOT / "db" / "paginas.db"
    assert config.paginas_db_path() != config.PROJECT_ROOT / "db" / "estagiario.db"


def test_padrao_usado_pelo_armazem_sem_argumentos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # PROJECT_ROOT redirecionado para tmp_path: o padrão é exercitado sem tocar o db/ real.
    monkeypatch.delenv("ESTAGIARIO_PAGINAS_DB_PATH", raising=False)
    monkeypatch.delenv("ESTAGIARIO_DB_PATH", raising=False)
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)

    armazem = ArmazemPaginas(relogio=_relogio_fixo())

    assert armazem.db_path == tmp_path / "db" / "paginas.db"
    assert armazem.db_path.exists()
    assert not (tmp_path / "db" / "estagiario.db").exists()


def test_override_por_variavel_de_ambiente(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destino = tmp_path / "sub" / "outro.db"
    monkeypatch.setenv("ESTAGIARIO_PAGINAS_DB_PATH", str(destino))
    monkeypatch.setenv("ESTAGIARIO_DB_PATH", str(tmp_path / "rule_store.db"))

    assert config.paginas_db_path() == destino
    armazem = ArmazemPaginas(relogio=_relogio_fixo())

    assert armazem.db_path == destino
    assert {"conteudo_pagina", "registro_coleta", "vw_paginas_por_peca"} <= _tabelas(destino)


# ---------------------------------------------------------------------------
# Conflito com o RuleStore (Req 7.12)
# ---------------------------------------------------------------------------


def test_conflito_por_caminho_igual_nao_cria_arquivo(tmp_path: Path) -> None:
    caminho = tmp_path / "mesmo.db"
    with pytest.raises(ConflitoCaminhoArmazemError) as exc:
        ArmazemPaginas(caminho, caminho_rule_store=caminho)
    assert exc.value.caminho_armazem == caminho
    assert exc.value.caminho_rule_store == caminho
    assert not caminho.exists()


def test_conflito_por_caminho_igual_com_arquivo_existente_nao_cria_tabelas(
    tmp_path: Path,
) -> None:
    caminho = tmp_path / "rule_store.db"
    _criar_rule_store_falso(caminho)
    with pytest.raises(ConflitoCaminhoArmazemError):
        ArmazemPaginas(caminho, caminho_rule_store=caminho)
    assert _tabelas(caminho) == {"regra"}


def test_conflito_por_symlink(tmp_path: Path) -> None:
    rule_store = tmp_path / "rule_store.db"
    _criar_rule_store_falso(rule_store)
    link = tmp_path / "paginas.db"
    link.symlink_to(rule_store)

    with pytest.raises(ConflitoCaminhoArmazemError):
        ArmazemPaginas(link, caminho_rule_store=rule_store)
    assert _tabelas(rule_store) == {"regra"}


def test_conflito_por_hard_link(tmp_path: Path) -> None:
    rule_store = tmp_path / "rule_store.db"
    _criar_rule_store_falso(rule_store)
    link = tmp_path / "paginas.db"
    os.link(rule_store, link)

    with pytest.raises(ConflitoCaminhoArmazemError):
        ArmazemPaginas(link, caminho_rule_store=rule_store)
    assert _tabelas(rule_store) == {"regra"}


def test_conflito_detectado_pelos_padroes_do_ambiente(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    caminho = tmp_path / "compartilhado.db"
    _criar_rule_store_falso(caminho)
    monkeypatch.setenv("ESTAGIARIO_PAGINAS_DB_PATH", str(caminho))
    monkeypatch.setenv("ESTAGIARIO_DB_PATH", str(caminho))

    with pytest.raises(ConflitoCaminhoArmazemError):
        ArmazemPaginas()
    assert _tabelas(caminho) == {"regra"}


def test_caminhos_distintos_nao_conflitam(tmp_path: Path) -> None:
    rule_store = tmp_path / "rule_store.db"
    _criar_rule_store_falso(rule_store)
    armazem = ArmazemPaginas(tmp_path / "paginas.db", caminho_rule_store=rule_store)
    assert armazem.db_path.exists()
    assert _tabelas(rule_store) == {"regra"}


# ---------------------------------------------------------------------------
# View de inspeção (Req 8.5)
# ---------------------------------------------------------------------------


def test_view_consultavel_com_sqlite_puro(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    conteudo = b"<html>pagina</html>"
    h = armazem.gravar_coleta(conteudo, _registro())
    armazem.gravar_coleta(
        conteudo, _registro(codigo_peca="X-1", marca_peca=None, confianca="media")
    )

    with closing(sqlite3.connect(armazem.db_path)) as conn:
        cursor = conn.execute("SELECT * FROM vw_paginas_por_peca ORDER BY id")
        colunas = [d[0] for d in cursor.description]
        linhas = cursor.fetchall()

    assert colunas == [
        "id",
        "codigo_peca",
        "marca_peca",
        "url_original",
        "dominio",
        "confianca",
        "coletado_em",
        "hash_conteudo",
        "tamanho_bytes",
    ]
    assert len(linhas) == _contar(armazem.db_path, "registro_coleta") == 2
    assert linhas[0][1:3] == ("JE-4699", "Bosch")
    assert linhas[1][1:3] == ("X-1", None)
    assert all(linha[7] == h and linha[8] == len(conteudo) for linha in linhas)


# ---------------------------------------------------------------------------
# Instantes
# ---------------------------------------------------------------------------


def test_datetime_ingenuo_rejeitado_no_registro(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    with pytest.raises(ValueError):
        armazem.gravar_coleta(b"x", _registro(coletado_em=datetime(2026, 1, 1, 12, 0)))
    assert _contar(armazem.db_path, "conteudo_pagina") == 0
    assert _contar(armazem.db_path, "registro_coleta") == 0


def test_datetime_ingenuo_rejeitado_no_relogio(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path, relogio=lambda: datetime(2026, 1, 1, 12, 0))
    with pytest.raises(ValueError):
        armazem.armazenar_conteudo(b"x")
    assert _contar(armazem.db_path, "conteudo_pagina") == 0


def test_datetime_ingenuo_rejeitado_na_busca_reaproveitavel(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    with pytest.raises(ValueError):
        armazem.buscar_reaproveitavel("https://a.example", coletado_desde=datetime(2026, 1, 1))


# ---------------------------------------------------------------------------
# Hash inexistente (Req 7.8, 8.9)
# ---------------------------------------------------------------------------


def test_conteudo_nao_encontrado_contem_hash(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    for operacao in (armazem.ler_conteudo, armazem.ler_texto):
        with pytest.raises(ConteudoNaoEncontradoError) as exc:
            operacao(HASH_AUSENTE)
        assert exc.value.hash_conteudo == HASH_AUSENTE
        assert HASH_AUSENTE in str(exc.value)


# ---------------------------------------------------------------------------
# Gravação atômica (Req 7.6, 7.7)
# ---------------------------------------------------------------------------


def test_gravar_coleta_desfaz_conteudo_quando_registro_falha(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    with closing(sqlite3.connect(armazem.db_path)) as conn:
        conn.execute(
            "CREATE TRIGGER falha_registro BEFORE INSERT ON registro_coleta "
            "BEGIN SELECT RAISE(ABORT, 'falha injetada'); END"
        )
        conn.commit()

    with pytest.raises(ErroArmazenamento):
        armazem.gravar_coleta(b"<html>novo</html>", _registro())

    assert _contar(armazem.db_path, "conteudo_pagina") == 0
    assert _contar(armazem.db_path, "registro_coleta") == 0


# ---------------------------------------------------------------------------
# Reaproveitamento (Req 6.3, 6.11)
# ---------------------------------------------------------------------------


def test_registrar_reaproveitamento_idempotente(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    h = armazem.gravar_coleta(b"<html>a</html>", _registro())
    outra_peca = _registro(codigo_peca="OUTRA-1")

    novo, criado = armazem.registrar_reaproveitamento(outra_peca, h)
    repetido, criado_de_novo = armazem.registrar_reaproveitamento(outra_peca, h)

    assert criado is True and novo.reaproveitado is True
    assert criado_de_novo is False
    assert repetido == novo
    assert novo.coletado_em == INSTANTE_BASE
    assert _contar(armazem.db_path, "registro_coleta") == 2


def test_registrar_reaproveitamento_isola_marca_ausente(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    h = armazem.gravar_coleta(b"<html>a</html>", _registro(marca_peca="Bosch"))

    sem_marca, criado_sem = armazem.registrar_reaproveitamento(_registro(marca_peca=None), h)
    _, criado_sem_de_novo = armazem.registrar_reaproveitamento(_registro(marca_peca=None), h)
    _, criado_com = armazem.registrar_reaproveitamento(_registro(marca_peca="Bosch"), h)

    assert criado_sem is True and sem_marca.marca_peca is None
    assert criado_sem_de_novo is False
    assert criado_com is False  # "Bosch" já tinha o vínculo original
    assert _contar(armazem.db_path, "registro_coleta") == 2


def test_registrar_reaproveitamento_hash_inexistente_nao_grava(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    with pytest.raises(ConteudoNaoEncontradoError):
        armazem.registrar_reaproveitamento(_registro(), HASH_AUSENTE)
    assert _contar(armazem.db_path, "registro_coleta") == 0


# ---------------------------------------------------------------------------
# Consulta e listagem por peça (Req 8.2, 8.3, 8.4)
# ---------------------------------------------------------------------------


def test_registros_da_peca_igualdade_exata_e_ordem(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    t0 = INSTANTE_BASE
    # Ordem de gravação escolhida para diferir da ordem esperada.
    armazem.gravar_coleta(b"m2", _registro(confianca="media", coletado_em=t0 + timedelta(hours=2)))
    armazem.gravar_coleta(b"a2", _registro(confianca="alta", coletado_em=t0 + timedelta(hours=1)))
    armazem.gravar_coleta(b"m1", _registro(confianca="media", coletado_em=t0))
    armazem.gravar_coleta(b"a1", _registro(confianca="alta", coletado_em=t0))
    armazem.gravar_coleta(b"a1b", _registro(confianca="alta", coletado_em=t0))  # empate → id
    # Ruído: não pode aparecer na consulta exata.
    armazem.gravar_coleta(b"r1", _registro(codigo_peca="je-4699"))
    armazem.gravar_coleta(b"r2", _registro(marca_peca="bosch"))
    armazem.gravar_coleta(b"r3", _registro(marca_peca=" Bosch"))
    armazem.gravar_coleta(b"r4", _registro(marca_peca=None))

    registros = armazem.registros_da_peca("JE-4699", "Bosch")

    hashes = [r.hash_conteudo for r in registros]
    esperado = [hashlib.sha256(b).hexdigest() for b in (b"a1", b"a1b", b"a2", b"m1", b"m2")]
    assert hashes == esperado
    assert all(r.codigo_peca == "JE-4699" and r.marca_peca == "Bosch" for r in registros)


def test_registros_da_peca_marca_ausente_casa_so_com_ausente(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    armazem.gravar_coleta(b"com", _registro(marca_peca="Bosch"))
    armazem.gravar_coleta(b"vazia", _registro(marca_peca=""))
    h = armazem.gravar_coleta(b"sem", _registro(marca_peca=None))

    registros = armazem.registros_da_peca("JE-4699", None)

    assert [r.hash_conteudo for r in registros] == [h]


def test_registros_da_peca_sem_correspondencia_devolve_vazio(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    armazem.gravar_coleta(b"x", _registro())
    assert armazem.registros_da_peca("NADA", "Bosch") == []


def test_listar_pecas(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    assert armazem.listar_pecas() == []

    t0 = INSTANTE_BASE
    armazem.gravar_coleta(b"1", _registro(codigo_peca="B", marca_peca="Bosch", coletado_em=t0))
    armazem.gravar_coleta(
        b"2", _registro(codigo_peca="B", marca_peca="Bosch", coletado_em=t0 + timedelta(days=1))
    )
    armazem.gravar_coleta(b"3", _registro(codigo_peca="B", marca_peca=None, coletado_em=t0))
    armazem.gravar_coleta(b"4", _registro(codigo_peca="A", marca_peca="Zeta", coletado_em=t0))

    resumo = [
        (r.codigo_peca, r.marca_peca, r.quantidade_registros, r.ultima_coleta)
        for r in armazem.listar_pecas()
    ]
    assert resumo == [
        ("A", "Zeta", 1, t0),
        ("B", None, 1, t0),
        ("B", "Bosch", 2, t0 + timedelta(days=1)),
    ]


# ---------------------------------------------------------------------------
# Leitura como texto (Req 8.8)
# ---------------------------------------------------------------------------


def test_ler_texto_com_charset_declarado(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    texto = "Peça de reposição — ação"
    dados = texto.replace("—", "-").encode("latin-1")
    h = armazem.gravar_coleta(dados, _registro(charset="latin-1"))
    assert armazem.ler_texto(h) == texto.replace("—", "-")


def test_ler_texto_usa_charset_do_registro_mais_recente(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    dados = "ação".encode("latin-1")
    armazem.gravar_coleta(dados, _registro(charset="utf-8", coletado_em=INSTANTE_BASE))
    h = armazem.gravar_coleta(
        dados, _registro(charset="latin-1", coletado_em=INSTANTE_BASE + timedelta(hours=1))
    )
    assert armazem.ler_texto(h) == "ação"


@pytest.mark.parametrize(
    "charset",
    [
        None,  # ausente
        "charset-que-nao-existe",  # desconhecido
        "base64",  # codec existente, mas não textual
        "rot13",
        "idna",  # não aceita errors="replace"
    ],
)
def test_ler_texto_cai_em_utf8_com_replace(tmp_path: Path, charset: str | None) -> None:
    armazem = _armazem(tmp_path)
    dados = "ação ".encode("utf-8") + b"\xff\xfe"
    h = armazem.gravar_coleta(dados, _registro(charset=charset))
    assert armazem.ler_texto(h) == dados.decode("utf-8", errors="replace")


def test_ler_texto_sem_registro_cai_em_utf8(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    dados = "só conteúdo".encode("utf-8")
    h = armazem.armazenar_conteudo(dados)
    assert armazem.ler_texto(h) == "só conteúdo"


# ---------------------------------------------------------------------------
# Exportação (Req 8.6, 8.7, 8.9)
# ---------------------------------------------------------------------------


def test_exportar_conteudo_grava_bytes_originais(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    dados = b"<html>\x00\xffbin\xe1rio</html>"
    h = armazem.armazenar_conteudo(dados)
    destino = tmp_path / "saida" / "pagina.html"
    destino.parent.mkdir()

    armazem.exportar_conteudo(h, destino)

    assert destino.read_bytes() == dados
    assert hashlib.sha256(destino.read_bytes()).hexdigest() == h
    # Nenhum temporário deixado para trás.
    assert sorted(p.name for p in destino.parent.iterdir()) == ["pagina.html"]


def test_exportar_conteudo_preserva_destino_existente(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    h = armazem.armazenar_conteudo(b"<html>novo</html>")
    destino = tmp_path / "existente.html"
    destino.write_bytes(b"original")

    with pytest.raises(DestinoExistenteError):
        armazem.exportar_conteudo(h, destino)

    assert destino.read_bytes() == b"original"
    assert sorted(p.name for p in tmp_path.iterdir() if p.name.startswith(".")) == []


def test_exportar_conteudo_recusa_symlink_quebrado(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    h = armazem.armazenar_conteudo(b"x")
    destino = tmp_path / "link.html"
    destino.symlink_to(tmp_path / "nao_existe")

    with pytest.raises(DestinoExistenteError):
        armazem.exportar_conteudo(h, destino)
    assert not (tmp_path / "nao_existe").exists()


def test_exportar_conteudo_hash_inexistente_nao_cria_arquivo(tmp_path: Path) -> None:
    armazem = _armazem(tmp_path)
    destino = tmp_path / "nada.html"

    with pytest.raises(ConteudoNaoEncontradoError) as exc:
        armazem.exportar_conteudo(HASH_AUSENTE, destino)

    assert exc.value.hash_conteudo == HASH_AUSENTE
    assert not destino.exists()
