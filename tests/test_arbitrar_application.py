from arbitration.application import arbitrar_application
from tools.reliability import FonteCampo

_BRAND_ID = 1


def _buscar_fonte_por_id(mapa: dict[int, FonteCampo]):
    def _fn(part_id, campo):
        return mapa.get(part_id, FonteCampo(provider_id=None, owner_id=None))

    return _fn


def test_normaliza_espacos_e_quebras(fazer_registro, sem_banco):
    registros = [fazer_registro(1, "X", application="  GOL 1.0   \n\n  GOL 1.6  ")]

    decisao = arbitrar_application(registros, brand_id=_BRAND_ID)

    assert decisao.valor == "GOL 1.0\nGOL 1.6"
    assert decisao.fonte == "normalizacao"


def test_dedupe_case_insensitive_dentro_do_mesmo_registro(fazer_registro, sem_banco):
    registros = [fazer_registro(1, "X", application="Gol 1.0\nGOL 1.0\ngol 1.0")]

    decisao = arbitrar_application(registros, brand_id=_BRAND_ID)

    assert decisao.valor == "Gol 1.0"


def test_uniao_preserva_linhas_novas_de_todos_os_registros(fazer_registro, sem_banco):
    registros = [
        fazer_registro(1, "X", application="GOL 1.0"),
        fazer_registro(2, "Y", application="GOL 1.6"),
    ]

    decisao = arbitrar_application(registros, brand_id=_BRAND_ID)

    assert set(decisao.valor.splitlines()) == {"GOL 1.0", "GOL 1.6"}


def test_uniao_ordena_por_confiabilidade_mais_confiavel_primeiro(fazer_registro):
    registros = [
        fazer_registro(1, "X", application="LINHA A"),
        fazer_registro(2, "Y", application="LINHA B"),
    ]
    # id=2 é a fonte mais confiável -> suas linhas devem vir primeiro.
    buscar_fonte = _buscar_fonte_por_id(
        {1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)}
    )

    def calcular_nivel(fonte, brand_id):
        return {1: "baixa", 2: "alta"}[fonte.provider_id]

    decisao = arbitrar_application(
        registros, brand_id=_BRAND_ID, buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel
    )

    assert decisao.valor.splitlines() == ["LINHA B", "LINHA A"]


def test_registros_sem_application_nao_quebram(fazer_registro, sem_banco):
    registros = [fazer_registro(1, "X", application=None), fazer_registro(2, "Y", application="")]

    decisao = arbitrar_application(registros, brand_id=_BRAND_ID)

    assert decisao.valor is None
