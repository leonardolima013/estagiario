"""Modelo puro do painel de execução (tui/execucao/modelo.py), sem Textual."""

from __future__ import annotations

import json

from arbitration.models import DecisaoCampo
from coleta_paginas.integracao import mensagem_desfecho, mensagem_evento
from loop.models import (
    AvisoEmitido,
    EtapaIniciada,
    EventoExecucao,
    FamiliaSorteada,
    GrupoCarregado,
    IteracaoConcluida,
    IteracaoIniciada,
    IteracaoStatus,
    LoopProgresso,
    RespostaParcial,
)
from loop.serializacao import para_json, registro_final_para_json
from sql_generation.models import DecisaoMerge
from tui.execucao.modelo import (
    COLUNAS_REGISTRO,
    CURSOR,
    LINHAS_PREVIA,
    PASSO_MINIMO,
    TICKS_PARA_ALCANCAR,
    EstadoDigitacao,
    FilaEventos,
    ModeloPainel,
    celula,
    classificar_aviso,
    colunas_vencedor,
    formatar_duracao,
    legenda_registros,
    legenda_vencedor,
    linha_familia,
    linha_item,
    linha_registro,
    recolher_texto,
    resumo_aplicacao,
    texto_raciocinio,
    valores_registro_final,
)
from verification.serper_agent import PREFIXO_RESULTADO_ORGANICO

TS = "2026-10-05T14:00:00.000+00:00"
FAMILIA = FamiliaSorteada("JE4699", 7, "DRIVEWAY")


def inicio(fase, nome, **detalhes):
    return EtapaIniciada(timestamp=TS, fase=fase, nome=nome, detalhes=detalhes)


def evento(fase, nome, *, status="ok", entrada=None, saida=None, justificativa=None, duracao_ms=10.0):
    return EventoExecucao(
        timestamp=TS, fase=fase, nome=nome, status=status, duracao_ms=duracao_ms,
        entrada=entrada or {}, saida=saida or {}, justificativa=justificativa,
    )


def iniciar(modelo, indice=1, total=3, familia=FAMILIA):
    modelo.aplicar(IteracaoIniciada(timestamp=TS, indice=indice, total=total, familia=familia))


def concluir(modelo, indice=1, total=3, status=IteracaoStatus.SUCESSO, **kwargs):
    modelo.aplicar(IteracaoConcluida(timestamp=TS, indice=indice, total=total, status=status, familia=FAMILIA, **kwargs))


def campo(nome, fonte, valor=None, status="ok", justificativa=None):
    return evento(
        "arbitragem", "arbitrar_campo", status=status,
        entrada={"campo": nome, "subcluster_ids": [1, 2]},
        saida={"valor": valor, "fonte": fonte, "confianca": "alta" if fonte != "sem_conflito" else None},
        justificativa=justificativa,
    )


# --- fila ---------------------------------------------------------------------------


def test_fila_coalesce_parciais_seguidas_da_mesma_chamada():
    fila = FilaEventos()
    fila.publicar(RespostaParcial(TS, "particao", {"justificativa": "a"}))
    fila.publicar(RespostaParcial(TS, "particao", {"justificativa": "ab"}))
    fila.publicar(RespostaParcial(TS, "decisao_nome", {"justificativa": "x"}))
    fila.publicar(AvisoEmitido(TS, "aviso"))
    fila.publicar(RespostaParcial(TS, "decisao_nome", {"justificativa": "xy"}))

    drenados = fila.drenar()

    assert [getattr(e, "resposta", None) for e in drenados] == [
        {"justificativa": "ab"}, {"justificativa": "x"}, None, {"justificativa": "xy"},
    ]
    assert fila.drenar() == []


# --- correlação ------------------------------------------------------------------------


def test_etapas_aninhadas_abrem_e_fecham_pelo_nome():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("tool", "particionar_grupo", pecas=4))
    modelo.aplicar(inicio("llm", "particao"))
    modelo.aplicar(evento("llm", "particao", saida={"resposta_estruturada": {"subclusters": []}}))
    modelo.aplicar(evento("tool", "particionar_grupo", saida={"subclusters": [{"label": "duplicata_real"}]}))

    (particionar,) = modelo.atual.itens
    (raciocinio,) = particionar.filhos
    assert (particionar.estado, raciocinio.estado) == ("ok", "ok")
    assert particionar.resumo == "1 subcluster(s): 1 duplicata_real"
    assert raciocinio.tipo == "raciocinio"
    assert modelo.atual.pilha == []


def test_erro_do_pipeline_fecha_a_etapa_inclusive_pelo_alias():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("arbitragem", "recuperar_distintos_name", membro_ids=[3, 4]))
    modelo.aplicar(inicio("tool", "verificar_nomenclatura_peca", nomes_conflitantes=["A", "B"]))
    modelo.aplicar(evento("pipeline", "recuperar_distintos", status="erro", saida={"erro": "TimeoutError"}))

    (recuperar,) = modelo.atual.itens
    (verificar,) = recuperar.filhos
    assert recuperar.estado == "erro"
    assert recuperar.resumo == "erro: TimeoutError"
    assert verificar.estado == "interrompido"
    assert modelo.atual.pilha == []


def test_ecos_e_eventos_redundantes_ficam_fora_do_painel():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(evento("loop", "sortear_grupo_aleatorio"))
    modelo.aplicar(evento("observabilidade", "aviso", saida={"mensagem": "x"}))
    modelo.aplicar(evento("tool", "verificar_nomenclatura_peca", saida={"status": "resultado"}))
    modelo.aplicar(evento("tool", "intervencao_humana", saida={"campo": "name"}))

    assert modelo.atual.itens == []


def test_decisoes_ficam_sob_o_merge_e_campos_sem_conflito_sao_agrupados():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("tool", "montar_decisao_merge", membro_ids=[1, 2]))
    modelo.aplicar(evento(
        "tool", "montar_decisao_merge",
        saida={"tipo": "DecisaoMerge", "decisao": {"vencedor_id": 1, "perdedor_ids": [2]}},
    ))
    for nome in ("width", "depth", "height"):
        modelo.aplicar(campo(nome, "sem_conflito"))
    modelo.aplicar(campo("name", "verificacao_web", valor="PIVO SUPERIOR"))
    modelo.aplicar(campo("ncm", "escalado_humano", status="escalado", justificativa="fontes divergentes"))

    (merge,) = modelo.atual.itens
    agregado, nome, ncm = merge.filhos
    assert merge.resumo == "merge: mantém 1, remove 2"
    assert (agregado.titulo, agregado.resumo) == ("3 campo(s) sem conflito", "width, depth, height")
    assert nome.titulo == "name = PIVO SUPERIOR"
    assert nome.resumo == "verificação web · confiança alta"
    assert (ncm.titulo, ncm.estado) == ("ncm → revisão humana", "escalado")
    assert "fontes divergentes" in ncm.resumo
    assert modelo.atual.escalados == 1


def test_sinalizacao_vai_para_o_ultimo_merge():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("tool", "montar_decisao_merge", membro_ids=[1, 2]))
    modelo.aplicar(evento("tool", "montar_decisao_merge", saida={"tipo": "GrupoSinalizado"}))
    modelo.aplicar(evento(
        "decisao", "grupo_sinalizado", status="revisao_manual", saida={"motivo": "similarity_id conflitante"},
    ))

    (merge,) = modelo.atual.itens
    (sinalizacao,) = merge.filhos
    assert merge.resumo == "sinalizado para revisão"
    assert (sinalizacao.tipo, sinalizacao.estado) == ("sinalizacao", "escalado")
    assert sinalizacao.resumo == "similarity_id conflitante"


def test_intervencao_mostra_espera_e_depois_a_resposta():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("intervencao", "intervencao_humana", motivo="web inconclusiva"))
    aberta = modelo.atual.itens[0]
    assert (aberta.tipo, aberta.aberto) == ("intervencao", True)
    assert aberta.resumo == "aguardando o operador · web inconclusiva"

    modelo.aplicar(evento(
        "intervencao", "intervencao_humana", entrada={"ponto": "nome"},
        saida={"regra": {"titulo": "Pivô"}, "valor": "PIVO INFERIOR", "acao": None},
    ))
    assert aberta.estado == "ok"
    assert aberta.resumo == "regra 'Pivô' · valor PIVO INFERIOR"


# --- raciocínio ----------------------------------------------------------------------


def test_texto_do_raciocinio_cresce_com_as_parciais_e_termina_no_final_do_trace():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("llm", "decisao_nome"))
    modelo.aplicar(RespostaParcial(TS, "decisao_nome", {"modo": "esco"}))
    item = modelo.atual.itens[0]
    assert item.texto == ""
    modelo.aplicar(RespostaParcial(TS, "decisao_nome", {"modo": "escolher", "justificativa": "As fontes 1"}))
    assert item.texto == "As fontes 1"

    final = {"modo": "escolher", "justificativa": "As fontes 1 e 3 usam PIVO SUPERIOR."}
    modelo.aplicar(evento("llm", "decisao_nome", saida={"resposta_estruturada": final}, justificativa=final["justificativa"]))
    assert item.texto == "As fontes 1 e 3 usam PIVO SUPERIOR."
    assert item.detalhes["saida"]["resposta_estruturada"] == final
    assert item.estado == "ok"


def test_raciocinio_do_particionamento_tem_uma_linha_por_subcluster():
    resposta = {"subclusters": [
        {"label": "duplicata_real", "membro_ids": [1, 2], "justificativa": "mesmo código e descrição"},
        {"label": "kit_comp", "justificativa": "rótulo ainda incompleto"},
    ]}
    assert texto_raciocinio(resposta) == "duplicata_real: mesmo código e descrição\nrótulo ainda incompleto"
    assert texto_raciocinio(None) == ""


def test_parcial_sem_etapa_aberta_e_ignorada():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(RespostaParcial(TS, "decisao_nome", {"justificativa": "x"}))
    assert modelo.atual.itens == []


# --- famílias -------------------------------------------------------------------------


def test_conclusao_fecha_pendencias_e_resume_a_familia():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("tool", "buscar_grupo", search_ref="JE4699", brand_id=7))
    modelo.aplicar(evento("tool", "buscar_grupo", saida={"quantidade": 4, "ids": [1, 2, 3, 4]}))
    modelo.aplicar(inicio("tool", "montar_decisao_merge", membro_ids=[1, 2]))
    concluir(
        modelo, status=IteracaoStatus.ERRO, merges=0, duracao_ms=1500.0,
        erro={"tipo": "RuntimeError", "mensagem": "réplica indisponível"},
    )

    (familia,) = modelo.familias
    assert modelo.atual is None
    assert (familia.estado, familia.pecas) == ("erro", 4)
    assert familia.itens[1].estado == "erro"
    assert familia.itens[-1].resumo == "RuntimeError: réplica indisponível"
    assert linha_familia(familia) == "✗ 1/3 · JE4699 · DRIVEWAY · 4 peça(s) · erro · 1,5 s"


def test_familia_sinalizada_e_ok():
    modelo = ModeloPainel()
    iniciar(modelo, 1)
    concluir(modelo, 1, merges=2, sinalizados=1, pecas=5, duracao_ms=9800.0)
    iniciar(modelo, 2)
    concluir(modelo, 2, merges=3, pecas=6, duracao_ms=14200.0)

    sinalizada, ok = modelo.familias
    assert sinalizada.estado == "sinalizada" and sinalizada.precisa_atencao
    assert ok.estado == "ok" and not ok.precisa_atencao
    assert linha_familia(ok) == "✓ 2/3 · JE4699 · DRIVEWAY · 6 peça(s) · 3 merge(s) · 14,2 s"


def test_erro_de_sorteio_vira_familia_com_erro():
    modelo = ModeloPainel()
    modelo.aplicar(IteracaoConcluida(
        timestamp=TS, indice=2, total=3, status=IteracaoStatus.ERRO,
        erro={"tipo": "OperationalError", "mensagem": "conexão recusada"},
    ))

    (familia,) = modelo.familias
    assert familia.estado == "erro"
    assert linha_familia(familia) == "✗ 2/3 · sorteio da família · erro"


def test_janela_mantem_as_familias_mais_recentes():
    modelo = ModeloPainel(max_familias=3)
    for indice in range(1, 6):
        iniciar(modelo, indice, 5)
        concluir(modelo, indice, 5)

    removidas, _alteradas = modelo.consumir_alteracoes()
    assert [f.indice for f in modelo.familias] == [3, 4, 5]
    assert modelo.ocultas == 2
    assert len(removidas) == 2
    assert modelo.consumir_alteracoes() == ([], set())


def test_eventos_sem_familia_criam_uma_familia_implicita_e_progresso_e_ignorado():
    modelo = ModeloPainel()
    modelo.aplicar(LoopProgresso(total=1, indice_atual=1, fase="sorteando", mensagem="..."))
    assert modelo.familias == []

    modelo.aplicar(AvisoEmitido(TS, "Configuração da execução: pesquisa web=desligada."))
    (familia,) = modelo.familias
    assert linha_familia(familia).endswith("Execução · em curso")
    assert familia.itens[0].titulo == "Configuração da execução: pesquisa web=desligada."


# --- avisos ---------------------------------------------------------------------------


def test_aviso_do_serper_vira_resumo_com_json_nos_detalhes():
    dados = {"ordinal": 2, "position": 2, "title": "Pivô de suspensão DRIVEWAY JE4699", "snippet": "s",
             "link": "https://loja.example.com.br/pivo"}
    texto = PREFIXO_RESULTADO_ORGANICO + " " + json.dumps(dados, ensure_ascii=False, separators=(",", ":"))

    resumo, detalhes = classificar_aviso(texto)

    assert resumo == "Serper #2 · Pivô de suspensão DRIVEWAY JE4699 · loja.example.com.br"
    assert detalhes == dados


def test_avisos_da_coleta_usam_as_mensagens_reais():
    entrada = {"evento": "coleta_paginas.entrada", "url": "https://a.example/x", "dominio": "a.example",
               "desfecho": "falha", "motivo": "http_403"}
    resumo_evento = {"evento": "coleta_paginas.resumo", "status": "executada",
                     "contagem": {"armazenado": 2, "falha": 1, "url_invalida": 0}}

    assert classificar_aviso(mensagem_evento(entrada))[0] == "Coleta · a.example · falha (http_403)"
    assert classificar_aviso(mensagem_evento(resumo_evento))[0] == "Coleta concluída · executada · 2 armazenado, 1 falha"

    class Desfecho:
        desfecho, motivo, variavel = "nao_executada", "sem_resultados", None

    assert classificar_aviso(mensagem_desfecho(Desfecho()))[0] == "Coleta · nao_executada (sem_resultados)"


def test_aviso_de_texto_comum_fica_intacto():
    texto = "Nomes divergentes para JE4699 (DRIVEWAY): ['PIVO INFERIOR', 'PIVO SUPERIOR'] — acionando verificação web."
    assert classificar_aviso(texto) == (texto, None)
    assert classificar_aviso(PREFIXO_RESULTADO_ORGANICO + " {quebrado") == (PREFIXO_RESULTADO_ORGANICO + " {quebrado", None)


# --- linhas, duração, digitação e recolhimento ---------------------------------------------


def test_linha_do_item_indica_detalhes_e_duracao():
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.aplicar(inicio("tool", "gerar_sql", decisoes=1))
    item = modelo.atual.itens[0]
    assert linha_item(item, quadro=0) == "⠋ Gerar SQL · 1 decisão(ões)"

    modelo.aplicar(evento("tool", "gerar_sql", saida={"sql": "BEGIN;", "caracteres": 6}, duracao_ms=320.0))
    assert linha_item(item) == "✓ Gerar SQL · SQL revisável · 6 caracteres · 320 ms ▸"
    assert linha_item(item, expandido=True).endswith(" ▾")


def test_formatar_duracao():
    assert formatar_duracao(None) == ""
    assert formatar_duracao(320.4) == "320 ms"
    assert formatar_duracao(14240.0) == "14,2 s"
    assert formatar_duracao(125000.0) == "2 min 05 s"


def test_digitacao_alcanca_o_alvo_e_tira_o_cursor_no_fim():
    estado = EstadoDigitacao()
    estado.definir_alvo("x" * 200)

    ticks = 0
    while estado.avancar():
        ticks += 1
        assert estado.visivel(aberto=False).endswith(CURSOR) or estado.alcancou
    assert ticks <= TICKS_PARA_ALCANCAR
    assert estado.visivel(aberto=True) == "x" * 200 + CURSOR
    assert estado.visivel(aberto=False) == "x" * 200


def test_digitacao_tem_passo_minimo_e_recua_ate_o_prefixo_comum():
    estado = EstadoDigitacao()
    estado.definir_alvo("abcdef")
    estado.avancar()
    assert estado.exibidos == PASSO_MINIMO

    estado.definir_alvo("abXYZ")
    assert estado.exibidos == 2
    assert estado.visivel(aberto=True) == "ab" + CURSOR


def test_recolhe_texto_longo_e_mantem_o_curto():
    assert recolher_texto("linha curta", 40) is None
    longo = "\n".join(f"linha {i}" for i in range(1, 10))

    previa, ocultas = recolher_texto(longo, 40)

    assert previa.splitlines() == ["linha 1", "linha 2"][:LINHAS_PREVIA]
    assert ocultas == 9 - LINHAS_PREVIA


# --- tabelas de registros ("Rodar loop") --------------------------------------------


def _grupo(fazer_registro):
    return [
        fazer_registro(
            101, "PIVO SUPERIOR", search_ref="JE4699", brand_id=7, brand="DRIVEWAY", width=16.0, ncm="87088000",
            application="GOL 1.0 1991/2001", born_at=1991, deprecated_at=2001, similarity_id=812,
        ),
        fazer_registro(
            102, "PIVO DA SUSPENSAO SUPERIOR", search_ref="JE4699", brand_id=7, brand="DRIVEWAY", width=16.0,
            gross_weight=0.62, application="PARATI 1.6 1996/2001", born_at=1996, deprecated_at=2001,
        ),
    ]


def _registro_final(fazer_registro, *, escalado: bool = False) -> dict:
    """`registro_final` real do JSON do loop para um merge 101 ← 102."""
    decisao = DecisaoMerge(
        grupo_ref="JE4699:DRIVEWAY", vencedor_id=101, perdedor_ids=[102],
        decisoes_campo=[
            DecisaoCampo(campo="application", valor="GOL 1.0 1991/2001\nPARATI 1.6 1996/2001",
                         justificativa="união", fonte="normalizacao"),
            DecisaoCampo(campo="gross_weight", valor=None if escalado else 0.62, justificativa="j",
                         fonte="escalado_humano" if escalado else "regra_confiabilidade", escalado_humano=escalado),
        ],
    )
    return registro_final_para_json(decisao, _grupo(fazer_registro))


def test_registros_do_grupo_entram_na_familia_atual_sem_virar_item(fazer_registro):
    modelo = ModeloPainel()
    iniciar(modelo)
    modelo.consumir_alteracoes()
    familia = modelo.atual
    assert familia.registros is None
    versao = familia.versao
    registros = tuple(para_json(r) for r in _grupo(fazer_registro))

    modelo.aplicar(GrupoCarregado(timestamp=TS, grupo_ref="JE4699:DRIVEWAY", registros=registros))

    assert familia.registros == registros
    assert familia.versao > versao
    assert modelo.consumir_alteracoes() == ([], {familia.id})
    assert familia.itens == []


def test_registros_finais_chegam_com_a_conclusao(fazer_registro):
    modelo = ModeloPainel()
    iniciar(modelo)
    familia = modelo.atual
    final = _registro_final(fazer_registro)

    concluir(modelo, merges=1, registros_finais=(final,))

    assert familia.registros_finais == (final,)


def test_celulas_da_tabela():
    assert celula("ncm", None) == ""
    assert celula("born_at", 0) == "0"
    assert celula("width", "11.0000") == "11.0000"
    assert celula("created", "2024-11-09T22:40:30.578149-03:00") == "2024-11-09 22:40"
    assert celula("name", "A" * 80) == "A" * 60 + "…"
    assert celula("application", "GOL 1.0 1991/2001\n\n PARATI 1.6 1996/2001 \n") == "GOL 1.0 1991/2001 (+1 linha(s))"
    assert resumo_aplicacao("X" * 50) == "X" * 40 + "…"
    assert resumo_aplicacao(" \n ") == ""


def test_linha_dos_sorteados_segue_as_colunas_pedidas(fazer_registro):
    registro = para_json(_grupo(fazer_registro)[0])

    assert COLUNAS_REGISTRO == (
        "id", "name", "born_at", "deprecated_at", "width", "depth", "height",
        "gross_weight", "net_weight", "ncm", "barcode", "similarity_id",
    )
    assert linha_registro(registro, COLUNAS_REGISTRO) == [
        "101", "PIVO SUPERIOR", "1991", "2001", "16.0", "", "", "", "", "87088000", "", "812",
    ]


def test_vencedor_mostra_os_valores_depois_do_merge_e_todos_os_campos(fazer_registro):
    final = _registro_final(fazer_registro)
    colunas = colunas_vencedor([final])
    valores = valores_registro_final(final)

    assert colunas == [*COLUNAS_REGISTRO, "search_ref", "brand", "brand_id", "created", "application"]
    assert dict(zip(colunas, linha_registro(valores, colunas))) == {
        "id": "101", "name": "PIVO SUPERIOR", "born_at": "1991", "deprecated_at": "2001",
        "width": "16.0", "depth": "", "height": "", "gross_weight": "0.62", "net_weight": "",
        "ncm": "87088000", "barcode": "", "similarity_id": "812",
        "search_ref": "JE4699", "brand": "DRIVEWAY", "brand_id": "7", "created": "2020-01-01 00:01",
        "application": "GOL 1.0 1991/2001 (+1 linha(s))",
    }
    # Campo escalado fica com o valor do vencedor, como no SQL e no JSON.
    assert valores_registro_final(_registro_final(fazer_registro, escalado=True))["gross_weight"] is None
    # Um campo a mais no registro aparece antes de `application`.
    final["campos"]["category_id"] = 55
    assert colunas_vencedor([final])[-2:] == ["category_id", "application"]


def test_legendas_das_tabelas(fazer_registro):
    modelo = ModeloPainel()
    iniciar(modelo)
    familia = modelo.atual

    familia.registros = tuple(para_json(r) for r in _grupo(fazer_registro))
    assert legenda_registros(familia) == "Registros sorteados · 2"
    familia.registros = ()
    assert legenda_registros(familia) == "Nenhum registro encontrado para esta família."

    um = _registro_final(fazer_registro)
    familia.registros_finais = (um,)
    assert legenda_vencedor(familia) == "Registro vencedor · mantém 101 (remove 102) · valores depois do merge"

    escalado = _registro_final(fazer_registro, escalado=True)
    outro = {**um, "id_mantido": 205, "ids_removidos": [206, 207], "campos_escalados": ["gross_weight", "ncm"]}
    familia.registros_finais = (escalado, outro)
    assert legenda_vencedor(familia) == (
        "Registros vencedores (2 merges) · mantém 101 (remove 102) · mantém 205 (remove 206, 207) · "
        "valores depois do merge · revisão humana: gross_weight, ncm"
    )


def test_legenda_sem_vencedor_diz_o_motivo():
    casos = [
        (IteracaoStatus.SUCESSO, {}, "nenhum merge nesta família"),
        (IteracaoStatus.SUCESSO, {"sinalizados": 1}, "1 grupo(s) sinalizado(s) para revisão, sem merge automático"),
        (IteracaoStatus.SUCESSO, {"merges": 2}, "2 merge(s), detalhes no JSON do loop"),
        (IteracaoStatus.ERRO, {"erro": {"tipo": "RuntimeError", "mensagem": "x"}}, "a família terminou com erro"),
        (IteracaoStatus.CANCELADA, {}, "execução cancelada"),
    ]
    modelo = ModeloPainel()
    for status, kwargs, motivo in casos:
        iniciar(modelo)
        familia = modelo.atual
        concluir(modelo, status=status, **kwargs)
        assert legenda_vencedor(familia) == f"Nenhum registro vencedor: {motivo}."
