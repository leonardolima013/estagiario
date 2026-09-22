import pytest

from verification.validacao import VerificacaoInvalidaError, validar_resultado_bruto


def test_confirmado_valido():
    resultado = validar_resultado_bruto({
        "status": "confirmado",
        "nome_sugerido": "PIVO SUPERIOR",
        "fontes": [{"url": "https://exemplo.com", "nome_encontrado": "PIVO SUPERIOR"}],
        "justificativa": "2 de 3 fontes confirmam",
    })
    assert resultado.status == "confirmado"
    assert resultado.nome_sugerido == "PIVO SUPERIOR"
    assert resultado.fontes[0].url == "https://exemplo.com"


def test_inconclusivo_valido():
    resultado = validar_resultado_bruto({
        "status": "inconclusivo",
        "nome_sugerido": None,
        "fontes": [],
        "justificativa": "fontes conflitantes",
    })
    assert resultado.status == "inconclusivo"
    assert resultado.nome_sugerido is None


def test_inconclusivo_com_nome_sugerido_perdido_e_descartado():
    resultado = validar_resultado_bruto({
        "status": "inconclusivo",
        "nome_sugerido": "PALPITE",
        "fontes": [],
        "justificativa": "x",
    })
    assert resultado.nome_sugerido is None


def test_confirmado_sem_nome_sugerido_levanta_erro():
    with pytest.raises(VerificacaoInvalidaError):
        validar_resultado_bruto({"status": "confirmado", "fontes": [], "justificativa": "x"})


def test_status_invalido_levanta_erro():
    with pytest.raises(VerificacaoInvalidaError):
        validar_resultado_bruto({"status": "talvez", "fontes": [], "justificativa": "x"})


def test_fonte_malformada_levanta_erro():
    with pytest.raises(VerificacaoInvalidaError):
        validar_resultado_bruto({
            "status": "confirmado", "nome_sugerido": "X",
            "fontes": [{"url": "https://exemplo.com"}], "justificativa": "x",
        })
