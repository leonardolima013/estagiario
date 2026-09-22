from db.rule_store import RuleStore


def test_registrar_intervencao_persiste_camadas_semantica_e_episodica(tmp_path):
    store = RuleStore(tmp_path / "test.db")

    regra = store.registrar_intervencao(
        titulo="Kit contém a peça avulsa",
        caso_episodico="JE4699 / DRIVEWAY: POLIA, DESVIO DISTRIBUIÇÃO e KIT DA CORREIA DE ACESSÓRIOS",
        condicao="Quando um grupo mistura peças avulsas e um kit cujo conteúdo as inclui",
        resolucao="Classificar o kit como kit_componente e comparar as peças avulsas entre si",
        criado_por="leo",
        sinais_busca="kit componente pecas avulsas polia correia",
        campo="particionamento",
        grupo_exemplo_ref="JE4699:DRIVEWAY",
    )

    assert regra.categoria == "intervencao_humana"
    assert regra.titulo == "Kit contém a peça avulsa"
    assert regra.caso_episodico.startswith("JE4699")
    assert regra.sinais_busca == "kit componente pecas avulsas polia correia"
    assert store.consultar("intervencao_humana")[0].resolucao.startswith("Classificar o kit")
    assert store.listar_intervencoes(campo="particionamento") == [regra]


def test_migracao_de_schema_legado_e_idempotente(tmp_path):
    # Simula um arquivo criado antes da memória de intervenções.
    import sqlite3

    db_path = tmp_path / "legado.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE estagiario_regras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                categoria TEXT NOT NULL,
                campo TEXT,
                condicao TEXT NOT NULL,
                resolucao TEXT NOT NULL,
                grupo_exemplo_ref TEXT,
                criado_por TEXT NOT NULL,
                criado_em TEXT NOT NULL DEFAULT (datetime('now')),
                ativo INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        conn.execute(
            "INSERT INTO estagiario_regras (categoria, condicao, resolucao, criado_por) VALUES (?, ?, ?, ?)",
            ("kit_deteccao", "condição antiga", "resolução antiga", "leo"),
        )

    RuleStore(db_path)
    # Reabrir aplica a mesma migração novamente sem erro.
    store = RuleStore(db_path)
    colunas = {row[1] for row in sqlite3.connect(db_path).execute("PRAGMA table_info(estagiario_regras)")}

    assert {"titulo", "caso_episodico", "sinais_busca"} <= colunas
    assert store.listar_todas()[0].condicao == "condição antiga"


def test_registrar_e_consultar(tmp_path):
    store = RuleStore(tmp_path / "test.db")

    regra = store.registrar(
        categoria="confiabilidade",
        condicao="provider X sempre correto para width",
        resolucao="usar valor do provider X",
        criado_por="leo",
        campo="width",
        grupo_exemplo_ref="83061:CITROEN",
    )

    assert regra.id is not None
    assert regra.ativo is True

    encontradas = store.consultar("confiabilidade", campo="width")
    assert len(encontradas) == 1
    assert encontradas[0].condicao == "provider X sempre correto para width"


def test_consultar_filtra_por_categoria(tmp_path):
    store = RuleStore(tmp_path / "test.db")
    store.registrar("kit_deteccao", "condicao A", "resolucao A", "leo")
    store.registrar("confiabilidade", "condicao B", "resolucao B", "leo")

    assert len(store.consultar("kit_deteccao")) == 1
    assert len(store.consultar("categoria_inexistente")) == 0


def test_listar_todas_retorna_tudo(tmp_path):
    store = RuleStore(tmp_path / "test.db")
    store.registrar("a", "c", "r", "leo")
    store.registrar("b", "c", "r", "leo")

    assert len(store.listar_todas()) == 2


def test_schema_persiste_entre_instancias(tmp_path):
    db_path = tmp_path / "test.db"
    RuleStore(db_path).registrar("a", "c", "r", "leo")

    reaberto = RuleStore(db_path)
    assert len(reaberto.listar_todas()) == 1
