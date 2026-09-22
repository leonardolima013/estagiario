import os

import pytest

from tools.reliability import FonteCampo, InfoProvider, buscar_fonte_atual, nivel_confiabilidade

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
