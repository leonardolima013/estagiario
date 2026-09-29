import os

import pytest

from tools.reliability import (
    FonteCampo,
    InfoProvider,
    LinhaAtividade,
    buscar_fonte_atual,
    nivel_confiabilidade,
    resolver_fonte_de_linhas,
)

_RUN_DB_TESTS = os.environ.get("ESTAGIARIO_RUN_DB_TESTS") == "1"
_SKIP_REASON = (
    "Teste de integração contra a réplica de dev real — desligado por padrão. "
    "Rode com ESTAGIARIO_RUN_DB_TESTS=1 apontando .env para a réplica antes de habilitar."
)

_BRAND_ID = 1
_MANUFACTURER_DA_MARCA = 100


def _fakes(manufacturer_provider=None, nome_provider="ALGUM PROVIDER", manufacturer_owner=None):
    def buscar_info_provider(provider_id):
        if manufacturer_provider is None and nome_provider is None:
            return None
        return InfoProvider(nome=nome_provider, manufacturer_id=manufacturer_provider)

    def buscar_manufacturer_do_owner(owner_id):
        return manufacturer_owner

    def buscar_manufacturer_da_marca(brand_id):
        return _MANUFACTURER_DA_MARCA

    return buscar_info_provider, buscar_manufacturer_do_owner, buscar_manufacturer_da_marca


def test_provider_da_mesma_manufacturer_e_alta():
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_provider=_MANUFACTURER_DA_MARCA)
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


def test_provider_fraga_e_media():
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_provider=999, nome_provider="fraga")
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "media"


def test_provider_suiv_e_media():
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_provider=999, nome_provider="SUIV")
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "media"


def test_provider_outro_e_baixa():
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_provider=999, nome_provider="OUTRO PROVIDER QUALQUER")
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "baixa"


def test_owner_da_mesma_manufacturer_e_alta():
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_owner=_MANUFACTURER_DA_MARCA)
    fonte = FonteCampo(provider_id=None, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


def test_owner_de_outra_manufacturer_e_baixa():
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_owner=999)
    fonte = FonteCampo(provider_id=None, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "baixa"


def test_sem_provider_nem_owner_e_desconhecido():
    info_fn, owner_fn, marca_fn = _fakes()
    fonte = FonteCampo(provider_id=None, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel is None


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_buscar_fonte_atual_sem_atividade_retorna_none():
    # Um part_id que certamente não existe em catalog_partactivity.
    fonte = buscar_fonte_atual(part_id=-1, campo="width")
    assert fonte == FonteCampo(provider_id=None, owner_id=None)



# ---------------------------------------------------------------------------
# Tarefa 1.2 — Classificação estendida: nível `minima` (POSTGRES) e provedor
# da marca ignorando o nome normalizado.
# _Requirements: 3.1, 3.2, 3.3, 3.4_
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "nome_provider",
    ["POSTGRES", "postgres", "  POSTGRES  ", "  postgres  ", "Postgres"],
)
def test_provider_postgres_nao_da_marca_e_minima(nome_provider):
    # Provider POSTGRES (variações de caixa/espaços), de OUTRA manufacturer que
    # não a da marca -> nível `minima` (R3.4). A normalização é strip().upper().
    info_fn, owner_fn, marca_fn = _fakes(manufacturer_provider=999, nome_provider=nome_provider)
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "minima"


def test_provider_da_marca_com_nome_postgres_ainda_e_alta():
    # O vínculo de manufacturer com a marca vence o nome: mesmo que o nome
    # normalizado seja POSTGRES, se o provider é o Provedor_Da_Marca -> `alta`
    # (R3.1), independentemente do nome normalizado.
    info_fn, owner_fn, marca_fn = _fakes(
        manufacturer_provider=_MANUFACTURER_DA_MARCA, nome_provider="  postgres  "
    )
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


@pytest.mark.parametrize("nome_provider", ["FRAGA", "  suiv  ", "SuIv"])
def test_provider_da_marca_com_nome_intermediario_ainda_e_alta(nome_provider):
    # Idem: o vínculo com a marca vence qualquer nome (FRAGA/SUIV incluídos).
    info_fn, owner_fn, marca_fn = _fakes(
        manufacturer_provider=_MANUFACTURER_DA_MARCA, nome_provider=nome_provider
    )
    fonte = FonteCampo(provider_id=42, owner_id=None)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


# ---------------------------------------------------------------------------
# Tarefa 2.3 — Resolução CRE/UPD: função pura `resolver_fonte_de_linhas`.
# Listas de LinhaAtividade construídas diretamente (sem banco), ORDENADAS por
# `created DESC` como a query entrega.
# _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5_
# ---------------------------------------------------------------------------


def _linha(provider_id, owner_id, activity_type, attribute, created):
    return LinhaAtividade(
        current_provider_id=provider_id,
        current_owner_id=owner_id,
        activity_type=activity_type,
        attribute=attribute,
        created=created,
    )


def test_resolver_fonte_so_cre_usa_fonte_do_cre():
    # (a) Só CRE aplicável -> fonte do CRE (R4.3).
    linhas = [_linha(provider_id=11, owner_id=None, activity_type="CRE", attribute=None, created=10)]

    fonte = resolver_fonte_de_linhas(linhas)

    assert fonte == FonteCampo(provider_id=11, owner_id=None)


def test_resolver_fonte_upd_mais_recente_vence_cre():
    # (b) CRE + UPD do campo mais recente (created maior) -> fonte do UPD (R4.2).
    # Lista ordenada por created DESC: UPD (created=20) antes do CRE (created=10).
    linhas = [
        _linha(provider_id=22, owner_id=None, activity_type="UPD", attribute="width", created=20),
        _linha(provider_id=11, owner_id=None, activity_type="CRE", attribute=None, created=10),
    ]

    fonte = resolver_fonte_de_linhas(linhas)

    assert fonte == FonteCampo(provider_id=22, owner_id=None)


def test_resolver_fonte_upd_de_outro_campo_nao_aparece_na_lista():
    # (c) UPD de outro campo não deve aparecer na lista (a query filtra por
    # attribute=campo). Modelamos a lista já filtrada: só o CRE presente ->
    # fonte do CRE (R4.1, R4.3).
    linhas = [_linha(provider_id=11, owner_id=None, activity_type="CRE", attribute=None, created=10)]

    fonte = resolver_fonte_de_linhas(linhas)

    assert fonte == FonteCampo(provider_id=11, owner_id=None)


def test_resolver_fonte_empate_de_timestamp_no_topo_sem_fonte():
    # (d) Empate de timestamp no topo (2 linhas com o mesmo created máximo) ->
    # sem fonte rastreável, provider e owner ambos ausentes (R4.4).
    linhas = [
        _linha(provider_id=22, owner_id=None, activity_type="UPD", attribute="width", created=20),
        _linha(provider_id=11, owner_id=None, activity_type="CRE", attribute=None, created=20),
    ]

    fonte = resolver_fonte_de_linhas(linhas)

    assert fonte == FonteCampo(provider_id=None, owner_id=None)


def test_resolver_fonte_lista_vazia_sem_fonte():
    # (e) Lista vazia -> sem fonte rastreável (R4.5).
    fonte = resolver_fonte_de_linhas([])

    assert fonte == FonteCampo(provider_id=None, owner_id=None)


# ---------------------------------------------------------------------------
# Tarefa 3.2 — Fallback owner (R5.4–R5.6) em `nivel_confiabilidade`, com fakes.
# _Requirements: 5.4, 5.5, 5.6_
# ---------------------------------------------------------------------------


def test_provider_resolve_vence_owner_ignora_owner():
    # R5.4: provider e owner ambos presentes, provider resolve -> decide pelo
    # provider (aqui provider da marca -> `alta`), ignorando o owner (que seria
    # de outra manufacturer -> `baixa` se fosse consultado).
    info_fn, owner_fn, marca_fn = _fakes(
        manufacturer_provider=_MANUFACTURER_DA_MARCA, manufacturer_owner=999
    )
    fonte = FonteCampo(provider_id=42, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


def test_provider_nao_resolve_cai_no_owner():
    # R5.5: provider presente que NÃO resolve (buscar_info_provider -> None) +
    # owner presente com manufacturer da marca -> decide por owner = `alta`.
    def info_fn(provider_id):
        return None

    def owner_fn(owner_id):
        return _MANUFACTURER_DA_MARCA

    def marca_fn(brand_id):
        return _MANUFACTURER_DA_MARCA

    fonte = FonteCampo(provider_id=42, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel == "alta"


def test_owner_sem_manufacturer_e_desconhecido():
    # R5.6: owner sem manufacturer (buscar_manufacturer_do_owner -> None) ->
    # `None`, sem classificar alta/baixa.
    def info_fn(provider_id):
        return None

    def owner_fn(owner_id):
        return None

    def marca_fn(brand_id):
        return _MANUFACTURER_DA_MARCA

    fonte = FonteCampo(provider_id=None, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel is None


def test_marca_nao_resolvivel_com_owner_conhecido_e_desconhecido():
    # R5.6 (subcaso corrigido na Tarefa 3.1): marca não resolvível
    # (buscar_manufacturer_da_marca -> None) com owner de manufacturer conhecido
    # -> `None`, pois não há como comparar sem palpite.
    def info_fn(provider_id):
        return None

    def owner_fn(owner_id):
        return 555

    def marca_fn(brand_id):
        return None

    fonte = FonteCampo(provider_id=None, owner_id=7)

    nivel = nivel_confiabilidade(fonte, _BRAND_ID, info_fn, owner_fn, marca_fn)

    assert nivel is None


# ---------------------------------------------------------------------------
# Tarefa 9.1 — Testes de integração opt-in contra a réplica de dev (DESLIGADOS
# por padrão) + validação de somente-leitura da query da tool.
#
# Decisão: ADICIONADOS ao final de tests/test_reliability.py (em vez de um novo
# arquivo) para reaproveitar o scaffolding de skip já presente aqui
# (`_RUN_DB_TESTS`, `_SKIP_REASON`) e manter o padrão do teste de integração já
# existente (`test_buscar_fonte_atual_sem_atividade_retorna_none`).
#
# Divisão de responsabilidade:
# - Os testes que TOCAM a réplica ficam guardados por
#   `pytest.mark.skipif(not _RUN_DB_TESTS, ...)` e só rodam com
#   ESTAGIARIO_RUN_DB_TESTS=1 e `.env` apontando para a réplica de dev. Nunca
#   produção; `consultar_banco` executa em sessão somente-leitura, SELECT único,
#   com limite de linhas e timeout (R8.1).
# - A validação de somente-leitura de `_QUERY_FONTE_ATUAL` e a rejeição de
#   statements não-SELECT são PURAS: `_validar_select_unico` roda ANTES de
#   qualquer conexão, então esses testes NÃO precisam de banco e ficam FORA do
#   bloco opt-in (R8.2).
#
# _Requirements: 8.1, 8.2, 8.3_
# ---------------------------------------------------------------------------

from tools.db_query import QueryValidationError, _validar_select_unico
from tools.reliability import (
    _QUERY_BRAND_MANUFACTURER,
    _QUERY_FONTE_ATUAL,
    _QUERY_OWNER_MANUFACTURER,
    _QUERY_PROVIDER,
    buscar_info_provider,
    buscar_manufacturer_da_marca,
    buscar_manufacturer_do_owner,
)


# --- Testes puros de somente-leitura (sem banco): R8.1/R8.2 -----------------


def test_query_fonte_atual_e_select_unico_valido():
    # R8.1/R8.2: `_QUERY_FONTE_ATUAL` é um SELECT único top-level e passa pela
    # validação de `consultar_banco` sem levantar QueryValidationError. A
    # validação acontece antes de qualquer conexão, então isto é um teste puro.
    _validar_select_unico(_QUERY_FONTE_ATUAL)


def test_todas_as_queries_da_tool_sao_select_unico_valido():
    # R8.1: todas as consultas usadas pela tool (catalog_partactivity,
    # catalog_informationprovider, catalog_partowner, manufacturer_brand) são
    # SELECT único top-level e passam pela validação sem levantar.
    for query in (
        _QUERY_FONTE_ATUAL,
        _QUERY_PROVIDER,
        _QUERY_OWNER_MANUFACTURER,
        _QUERY_BRAND_MANUFACTURER,
    ):
        _validar_select_unico(query)


def test_query_nao_select_e_rejeitada_antes_de_conectar():
    # R8.2: uma tentativa de operação que não é um SELECT único top-level
    # (aqui, um UPDATE de escrita) é rejeitada com QueryValidationError ANTES de
    # tocar o banco, preservando os dados de origem inalterados. Teste puro.
    with pytest.raises(QueryValidationError):
        _validar_select_unico(
            "UPDATE catalog_partactivity SET attribute = 'x' WHERE part_id = 1"
        )


def test_cte_de_escrita_e_rejeitada_antes_de_conectar():
    # R8.2: CTE que contorna a regra escrevendo e retornando via SELECT é
    # rejeitada (o DELETE aparece como keyword proibida), sem tocar o banco.
    with pytest.raises(QueryValidationError):
        _validar_select_unico(
            "WITH x AS (DELETE FROM catalog_partactivity RETURNING *) SELECT * FROM x"
        )


# --- Testes de integração opt-in (tocam a réplica): R8.1/R8.3 ---------------


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_buscar_fonte_atual_part_conhecido_retorna_fonte_coerente():
    # R8.1: `buscar_fonte_atual` contra a réplica real para um part_id conhecido
    # retorna um FonteCampo coerente (provider/owner ausentes quando sem
    # atividade rastreável, ou ids inteiros quando há fonte) e nunca levanta.
    # Ajuste _PART_ID_CONHECIDO para um part real da réplica antes de habilitar.
    _PART_ID_CONHECIDO = 1
    fonte = buscar_fonte_atual(part_id=_PART_ID_CONHECIDO, campo="name")

    assert isinstance(fonte, FonteCampo)
    assert fonte.provider_id is None or isinstance(fonte.provider_id, int)
    assert fonte.owner_id is None or isinstance(fonte.owner_id, int)


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_query_fonte_atual_executa_somente_leitura_com_params_dummy():
    # R8.1/R8.3: a própria `_QUERY_FONTE_ATUAL` executa via `consultar_banco`
    # em sessão somente-leitura, respeitando limite de linhas e timeout, sem
    # levantar QueryValidationError. Com params dummy (part_id inexistente),
    # esperamos um resultado vazio; o importante é NÃO levantar e respeitar
    # `max_rows` (nunca retornar mais linhas do que o limite).
    from tools.db_query import consultar_banco

    resultado = consultar_banco(
        _QUERY_FONTE_ATUAL,
        params={"part_id": -1, "campo": "name"},
        max_rows=10,
    )

    assert len(resultado.rows) <= 10  # limite de linhas respeitado
    assert resultado.rows == []  # part_id inexistente -> sem atividade


@pytest.mark.skipif(not _RUN_DB_TESTS, reason=_SKIP_REASON)
def test_resolucao_real_provider_owner_marca_nao_levanta():
    # R8.1: os lookups de resolução real (provider/owner/marca) executam contra
    # a réplica somente-leitura sem levantar. Para ids inexistentes retornam
    # None (sem linhas); para ids reais retornam o valor tipado. Ajuste os ids
    # conhecidos abaixo para valores reais da réplica antes de habilitar.
    info = buscar_info_provider(provider_id=-1)
    assert info is None or isinstance(info, InfoProvider)

    manufacturer_owner = buscar_manufacturer_do_owner(owner_id=-1)
    assert manufacturer_owner is None or isinstance(manufacturer_owner, int)

    manufacturer_marca = buscar_manufacturer_da_marca(brand_id=-1)
    assert manufacturer_marca is None or isinstance(manufacturer_marca, int)
