from db.rule_store import Regra, RuleStore
from memory.models import PedidoIntervencao
from memory.recuperador import RecuperadorLexico, consultar_intervencao, sinais_para_busca, tokens_busca


def _regra(id_, titulo, condicao, resolucao, sinais, criado_em="2026-01-01T00:00:00+00:00"):
    return Regra(
        id=id_, categoria="intervencao_humana", campo="nome", condicao=condicao,
        resolucao=resolucao, grupo_exemplo_ref="CASO-ORIGINAL", criado_por="leo",
        criado_em=criado_em, ativo=True, titulo=titulo, caso_episodico="caso original",
        sinais_busca=sinais,
    )


def test_tokens_normalizados_removem_acento_caixa_e_pontuacao():
    assert tokens_busca("PIVÔ, Kit da Correia!") == {"pivo", "kit", "correia"}


def test_sinais_para_busca_e_deterministico():
    assert sinais_para_busca("Kit correia", "POLIA correia") == "correia kit polia"


def test_recupera_mesmo_padrao_em_peca_diferente():
    regra = _regra(
        1,
        "Kit contém componente avulso",
        "Quando o grupo reúne uma peça avulsa e um kit que a contém",
        "Classificar o kit como kit_componente e comparar as peças avulsas entre si",
        "kit componente avulso polia correia",
    )
    caso_de_outra_peca = PedidoIntervencao(
        ponto="nome", grupo_ref="OUTRO:MARCA", search_ref="OUTRO", marca="MARCA",
        nomes_conflitantes=["ENGRENAGEM", "KIT DE ENGRENAGEM"],
        motivo="o nome sugere peça avulsa e kit; a busca web não confirmou a relação",
    )

    recuperada = consultar_intervencao(
        caso_de_outra_peca,
        _StoreFake([regra]),
        campo="nome",
        limiar=0.30,
    )

    assert recuperada is not None
    assert recuperada.regra.id == 1
    assert recuperada.score >= 0.30


def test_caso_nao_relacionado_fica_abaixo_do_limiar():
    regra = _regra(
        1, "Kit contém componente avulso", "kit e componente", "separar kit", "kit componente avulso"
    )

    recuperada = RecuperadorLexico().recuperar(
        "sensor de temperatura com conector elétrico", [regra], limiar=0.45
    )

    assert recuperada is None


def test_escolhe_maior_score_e_desempata_por_regra_recente():
    ampla = _regra(1, "Kit componente", "kit componente avulso", "separar", "kit componente avulso")
    especifica = _regra(
        2, "Kit correia componente", "kit componente avulso correia", "separar correia", "kit componente avulso correia"
    )
    motor = RecuperadorLexico()

    recuperada = motor.recuperar("kit componente avulso correia", [ampla, especifica], limiar=0.1)

    assert recuperada is not None
    assert recuperada.regra.id == 2


def test_consultar_intervencao_aceita_caso_string_e_store_real(tmp_path):
    store = RuleStore(tmp_path / "memoria.db")
    regra = store.registrar_intervencao(
        titulo="Kit contém componente",
        caso_episodico="caso original",
        condicao="kit junto de componente avulso",
        resolucao="separar kit",
        criado_por="leo",
        sinais_busca="kit componente avulso",
        campo="nome",
    )

    recuperada = consultar_intervencao("kit componente avulso em outro código", store, campo="nome", limiar=0.3)

    assert recuperada is not None
    assert recuperada.regra.id == regra.id


class _StoreFake:
    def __init__(self, regras):
        self._regras = regras

    def listar_intervencoes(self, campo=None):
        return self._regras
