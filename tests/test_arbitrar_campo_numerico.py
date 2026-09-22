from arbitration.campo_numerico import arbitrar_campo_numerico
from tests.fakes import FakeLLMProvider
from tools.reliability import FonteCampo

_BRAND_ID = 1


def _buscar_fonte_por_id(mapa: dict[int, FonteCampo]):
    def _fn(part_id, campo):
        return mapa.get(part_id, FonteCampo(provider_id=None, owner_id=None))

    return _fn


def _nivel_por_id(mapa_niveis: dict[int, str | None]):
    # calcular_nivel_confiabilidade recebe (fonte, brand_id); mapeamos por fonte.provider_id
    # pra simplificar os testes (cada registro usa um provider_id distinto como "id lógico").
    def _fn(fonte, brand_id):
        return mapa_niveis.get(fonte.provider_id)

    return _fn


def test_sem_divergencia_retorna_valor_unico(fazer_registro):
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=3.2)]

    decisao = arbitrar_campo_numerico(registros, "width", llm=None, rule_store=None, brand_id=_BRAND_ID)

    assert decisao.fonte == "sem_conflito"
    assert decisao.valor == 3.2


def test_divergencia_pequena_abaixo_do_threshold_nao_conta(fazer_registro):
    registros = [fazer_registro(1, "X", width=3.20), fazer_registro(2, "Y", width=3.21)]

    decisao = arbitrar_campo_numerico(
        registros, "width", llm=None, rule_store=None, brand_id=_BRAND_ID, threshold_divergencia=0.15
    )

    assert decisao.fonte == "sem_conflito"


def test_vitoria_por_confiabilidade_alta(fazer_registro):
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]
    buscar_fonte = _buscar_fonte_por_id({1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)})
    calcular_nivel = _nivel_por_id({1: "alta", 2: "baixa"})

    decisao = arbitrar_campo_numerico(
        registros, "width", llm=None, rule_store=None, brand_id=_BRAND_ID,
        buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel,
    )

    assert decisao.fonte == "regra_confiabilidade"
    assert decisao.valor == 3.2
    assert decisao.origem_id == 1


def test_sem_divergencia_nao_tem_origem_id(fazer_registro):
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=3.2)]

    decisao = arbitrar_campo_numerico(registros, "width", llm=None, rule_store=None, brand_id=_BRAND_ID)

    assert decisao.origem_id is None


def test_empate_de_confiabilidade_cai_pro_modelo(fazer_registro):
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]
    buscar_fonte = _buscar_fonte_por_id({1: FonteCampo(provider_id=1, owner_id=None), 2: FonteCampo(provider_id=2, owner_id=None)})
    calcular_nivel = _nivel_por_id({1: "alta", 2: "alta"})
    fake_llm = FakeLLMProvider({"part_id_escolhido": 1, "justificativa": "peça maior faz mais sentido", "confianca": "alta"})

    decisao = arbitrar_campo_numerico(
        registros, "width", llm=fake_llm, rule_store=None, brand_id=_BRAND_ID,
        buscar_fonte=buscar_fonte, calcular_nivel_confiabilidade=calcular_nivel,
    )

    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == 3.2
    assert decisao.origem_id == 1


def test_modelo_com_baixa_confianca_escala_pra_humano(fazer_registro, sem_banco):
    registros = [fazer_registro(1, "X", width=3.2), fazer_registro(2, "Y", width=2.0)]
    fake_llm = FakeLLMProvider({"part_id_escolhido": 1, "justificativa": "não tenho certeza", "confianca": "baixa"})

    decisao = arbitrar_campo_numerico(registros, "width", llm=fake_llm, rule_store=None, brand_id=_BRAND_ID)

    assert decisao.fonte == "escalado_humano"
    assert decisao.escalado_humano is True
    assert decisao.valor is None
    assert decisao.origem_id is None


def test_campo_categorico_qualquer_diferenca_e_divergencia(fazer_registro, sem_banco):
    registros = [fazer_registro(1, "X", ncm="1234"), fazer_registro(2, "Y", ncm="1235")]
    fake_llm = FakeLLMProvider({"part_id_escolhido": 1, "justificativa": "ncm mais recente", "confianca": "alta"})

    decisao = arbitrar_campo_numerico(registros, "ncm", llm=fake_llm, rule_store=None, brand_id=_BRAND_ID)

    assert decisao.fonte == "julgamento_modelo"
    assert decisao.valor == "1234"
