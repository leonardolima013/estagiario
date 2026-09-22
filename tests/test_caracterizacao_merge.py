"""T-01 (Lote 0 — rede de segurança): caracteriza o valor final que entra no
`UPDATE catalog_part SET ... WHERE id = vencedor` gerado por `gerar_sql`, por
`fonte` de decisão de campo.

Objetivo: fixar o comportamento ANTES de qualquer mudança de lógica de merge
(T-04 é o que altera isso). Estes testes documentam explicitamente o achado F-01:
hoje `gerar_sql` escreve TODO `DecisaoCampo.valor` não-escalado no UPDATE do
vencedor — inclusive um valor que veio de outro registro (via origem_id) e
inclusive quando o valor já é o do próprio vencedor (UPDATE inócuo).

Trabalha no nível de `gerar_sql` com `DecisaoMerge`/`DecisaoCampo` construídos à
mão porque é exatamente ali que o achado vive; os valores por `fonte` que a
arbitragem produz são caracterizados em `tests/test_arbitrar_campo_numerico.py`.

Contrato de valor por fonte (referência, confirmado na leitura do código):
- sem_conflito (numérico sem divergência): 1º valor não-nulo entre os registros,
  origem_id=None (arbitration/campo_numerico.py).
- regra_confiabilidade: valor da fonte de topo, origem_id preenchido.
- julgamento_modelo: valor do registro escolhido pelo modelo, origem_id preenchido.
- normalizacao (application/datas): valor agregado/derivado, origem_id=None.
- escalado_humano/verificacao_web inconclusivo: valor=None, escalado_humano=True
  (NÃO entra no UPDATE — vira comentário).
"""

from arbitration.models import DecisaoCampo
from sql_generation.gerar_sql import gerar_sql
from sql_generation.models import DecisaoMerge


def _merge(decisoes_campo, vencedor_id=1, perdedor_ids=(2,)):
    return DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=vencedor_id,
        perdedor_ids=list(perdedor_ids),
        decisoes_campo=decisoes_campo,
    )


def test_caracteriza_sem_conflito_entra_no_update_do_vencedor():
    # sem_conflito com origem_id=None: hoje SEMPRE entra no UPDATE do vencedor,
    # mesmo sem saber se o valor já era o do próprio vencedor (F-01).
    decisao = _merge([DecisaoCampo(campo="width", valor=3.2, justificativa="Sem divergência entre os registros.", fonte="sem_conflito")])

    script = gerar_sql([decisao], [])

    assert '"width" = 3.2' in script
    assert "WHERE id = 1;" in script


def test_caracteriza_regra_confiabilidade_grava_valor_da_fonte_no_vencedor():
    # origem_id=2 (valor veio do PERDEDOR), mas o UPDATE é no vencedor id=1.
    # Hoje o valor do perdedor é gravado no vencedor sem qualquer checagem (F-01).
    decisao = _merge(
        [DecisaoCampo(campo="ncm", valor="9999", justificativa="fonte alta decide", fonte="regra_confiabilidade", origem_id=2)]
    )

    script = gerar_sql([decisao], [])

    assert "\"ncm\" = '9999'" in script


def test_caracteriza_julgamento_modelo_grava_valor_escolhido():
    decisao = _merge(
        [DecisaoCampo(campo="height", valor=5.0, justificativa="modelo escolheu", fonte="julgamento_modelo", origem_id=2)]
    )

    script = gerar_sql([decisao], [])

    assert '"height" = 5.0' in script


def test_caracteriza_normalizacao_application_entra_no_update():
    decisao = _merge(
        [DecisaoCampo(campo="application", valor="GOL 1.0 1991/2001", justificativa="união", fonte="normalizacao")]
    )

    script = gerar_sql([decisao], [])

    assert "\"application\" = 'GOL 1.0 1991/2001'" in script


def test_caracteriza_born_at_derivado_int_entra_como_literal_inteiro():
    decisao = _merge(
        [DecisaoCampo(campo="born_at", valor=1991, justificativa="menor ano", fonte="normalizacao")]
    )

    script = gerar_sql([decisao], [])

    # int -> literal sem aspas (não '1991')
    assert '"born_at" = 1991' in script
    assert "'1991'" not in script


def test_caracteriza_escalado_humano_nao_entra_no_update():
    decisao = _merge(
        [DecisaoCampo(campo="ncm", valor=None, justificativa="ambíguo", fonte="escalado_humano", escalado_humano=True)]
    )

    script = gerar_sql([decisao], [])

    assert "UPDATE catalog_part SET" not in script
    assert "-- REVISAR MANUALMENTE: campo ncm" in script


def test_caracteriza_multiplos_campos_todos_entram_no_mesmo_update():
    decisao = _merge(
        [
            DecisaoCampo(campo="width", valor=3.2, justificativa="x", fonte="sem_conflito"),
            DecisaoCampo(campo="ncm", valor="1234", justificativa="y", fonte="regra_confiabilidade", origem_id=1),
            DecisaoCampo(campo="name", valor="POLIA DA CORREIA", justificativa="z", fonte="julgamento_modelo", origem_id=2),
        ]
    )

    script = gerar_sql([decisao], [])

    # todos num único UPDATE SET
    assert script.count("UPDATE catalog_part SET") == 1
    assert '"width" = 3.2' in script
    assert "\"ncm\" = '1234'" in script
    assert "\"name\" = 'POLIA DA CORREIA'" in script


# --- T-04: comportamento diff-only (Q-02=a) quando o snapshot do vencedor existe ---


def _merge_com_snapshot(decisoes_campo, valores_atuais, vencedor_id=1, perdedor_ids=(2,)):
    return DecisaoMerge(
        grupo_ref="83061:CITROEN",
        vencedor_id=vencedor_id,
        perdedor_ids=list(perdedor_ids),
        decisoes_campo=decisoes_campo,
        valores_atuais_vencedor=valores_atuais,
    )


def test_diff_only_omite_campo_cujo_valor_ja_e_o_do_vencedor():
    # valor decidido == valor atual do vencedor -> nada muda -> campo fora do SET.
    decisao = _merge_com_snapshot(
        [DecisaoCampo(campo="width", valor=3.2, justificativa="sem divergência", fonte="sem_conflito")],
        valores_atuais={"width": 3.2},
    )

    script = gerar_sql([decisao], [])

    assert '"width"' not in script
    assert "UPDATE catalog_part SET" not in script  # era o único campo


def test_diff_only_escreve_campo_cujo_valor_difere_do_vencedor():
    # vencedor tem ncm NULL, valor decidido veio do perdedor -> difere -> entra no SET.
    decisao = _merge_com_snapshot(
        [DecisaoCampo(campo="ncm", valor="9999", justificativa="fonte alta", fonte="regra_confiabilidade", origem_id=2)],
        valores_atuais={"ncm": None},
    )

    script = gerar_sql([decisao], [])

    assert "\"ncm\" = '9999'" in script


def test_diff_only_mistura_campos_iguais_e_diferentes():
    decisao = _merge_com_snapshot(
        [
            DecisaoCampo(campo="width", valor=3.2, justificativa="igual", fonte="sem_conflito"),
            DecisaoCampo(campo="height", valor=5.0, justificativa="difere", fonte="julgamento_modelo", origem_id=2),
        ],
        valores_atuais={"width": 3.2, "height": 1.0},
    )

    script = gerar_sql([decisao], [])

    assert '"width"' not in script  # igual -> omitido
    assert '"height" = 5.0' in script  # difere -> escrito
    assert script.count("UPDATE catalog_part SET") == 1


def test_diff_only_sem_snapshot_mantem_comportamento_legado():
    # DecisaoMerge sem valores_atuais_vencedor (dict vazio) -> escreve tudo, como antes.
    decisao = _merge(
        [DecisaoCampo(campo="width", valor=3.2, justificativa="x", fonte="sem_conflito")]
    )

    script = gerar_sql([decisao], [])

    assert '"width" = 3.2' in script
