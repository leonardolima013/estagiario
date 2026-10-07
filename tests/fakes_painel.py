"""Fakes do painel de execução: um caso roteirizado e um LLM com streaming simulado.

Usados com o `executar_loop` real, para exercitar o caminho inteiro
(executor → TraceCollector com ouvinte → TracingLLMProvider → painel) sem banco,
rede nem API. O LLM fatia o JSON final em trechos e os interpreta com o mesmo
parser do adaptador Anthropic, então as parciais têm a forma real.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
from typing import Any

from arbitration.models import DecisaoCampo
from coleta_paginas.integracao import mensagem_evento
from llm.anthropic_provider import _interpretar_parcial
from loop.tracing import sinalizar_grupo, sinalizar_inicio
from memory.models import PedidoIntervencao
from partitioning.models import Particao, Subcluster
from pipeline import ResultadoCaso
from sql_generation.models import DecisaoMerge, GrupoSinalizado
from tools.group_fetch import RegistroCatalogPart
from verification.serper_agent import PREFIXO_RESULTADO_ORGANICO

PARTICAO = {
    "subclusters": [
        {
            "label": "duplicata_real",
            "membro_ids": [101, 102],
            "justificativa": "Os registros 101 e 102 têm o mesmo código, a mesma marca e a mesma aplicação; "
            "a diferença entre PIVO SUPERIOR e PIVO DA SUSPENSÃO SUPERIOR é só de descrição.",
        },
        {
            "label": "kit_componente",
            "membro_ids": [103],
            "justificativa": "O 103 é o kit com dois pivôs e as buchas, não a peça avulsa.",
        },
        {
            "label": "variante_dimensional",
            "membro_ids": [104],
            "justificativa": "O 104 é a versão reforçada, com diâmetro de 18 mm em vez de 16 mm.",
        },
    ]
}
DECISAO_SIMILARITY = {
    "acao": "manter_sinalizado",
    "confianca": "baixa",
    "justificativa": "Os dois registros apontam para similarity_id diferentes (812 e 977) e nada no grupo "
    "explica qual agrupamento está certo.",
}
RESPOSTAS = {"particao": PARTICAO, "decisao_similarity_id": DECISAO_SIMILARITY}


@dataclass
class LLMRoteirizado:
    """LLMProviderComStreaming fake: transmite o JSON final em trechos de `tamanho` caracteres."""

    tamanho: int = 12
    pausa: float = 0.0
    pausar_em: dict[str, int] = field(default_factory=dict)
    pausado: Event = field(default_factory=Event)
    continuar: Event = field(default_factory=Event)
    chamadas: list[str] = field(default_factory=list)

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        self.chamadas.append(f"gerar_json:{schema_name}")
        return json.loads(json.dumps(RESPOSTAS[schema_name]))

    def gerar_json_transmitindo(self, system, user, json_schema, schema_name, ao_atualizar):
        self.chamadas.append(f"transmitindo:{schema_name}")
        texto = json.dumps(RESPOSTAS[schema_name], ensure_ascii=False)
        recebido = ""
        for numero, inicio in enumerate(range(0, len(texto), self.tamanho), start=1):
            recebido += texto[inicio:inicio + self.tamanho]
            parcial = _interpretar_parcial(recebido)
            if parcial is not None:
                ao_atualizar(parcial)
            if self.pausar_em.get(schema_name) == numero:
                self.pausado.set()
                self.continuar.wait(timeout=10)
            if self.pausa:
                time.sleep(self.pausa)
        return json.loads(recebido)


def _serper(ordinal: int, titulo: str, link: str) -> str:
    dados = {"ordinal": ordinal, "position": ordinal, "title": titulo, "snippet": "Pivô de suspensão…", "link": link}
    return f"{PREFIXO_RESULTADO_ORGANICO} " + json.dumps(dados, ensure_ascii=False, separators=(",", ":"))


def _pedido(search_ref: str, marca: str) -> PedidoIntervencao:
    return PedidoIntervencao(
        ponto="nome", grupo_ref=f"{search_ref}:{marca}", search_ref=search_ref, marca=marca,
        nomes_conflitantes=["PIVO INFERIOR", "PIVO SUPERIOR"], motivo="Verificação web inconclusiva.",
        membro_ids=[201, 202], candidatos=[(201, "PIVO SUPERIOR"), (202, "PIVO INFERIOR")],
    )


def registros_roteirizados(search_ref: str, brand_id: int, marca: str) -> list[RegistroCatalogPart]:
    """Os quatro registros que o caso roteirizado "busca"; 101 e 102 são a duplicata."""
    base = dict(search_ref=search_ref, brand_id=brand_id, brand=marca, depth=None, net_weight=None)
    return [
        RegistroCatalogPart(
            id=101, name="PIVO SUPERIOR", width=16.0, height=42.0, gross_weight=0.41, ncm="87088000",
            barcode="7891234500101", application="GOL 1.0 1991/2001", born_at=1991, deprecated_at=2001,
            similarity_id=812, created=datetime(2019, 3, 4, 10, 15, tzinfo=timezone.utc), **base,
        ),
        RegistroCatalogPart(
            id=102, name="PIVO DA SUSPENSAO SUPERIOR", width=16.0, height=42.0, gross_weight=0.62, ncm=None,
            barcode=None, application="PARATI 1.6 1996/2001", born_at=1996, deprecated_at=2001,
            similarity_id=977, created=datetime(2021, 8, 9, 14, 2, tzinfo=timezone.utc), **base,
        ),
        RegistroCatalogPart(
            id=103, name="KIT PIVO SUPERIOR + BUCHAS", width=None, height=None, gross_weight=1.2, ncm="87088000",
            barcode=None, application="GOL 1.0 1991/2001\nPARATI 1.6 1996/2001", born_at=1991,
            deprecated_at=2001, similarity_id=None, created=datetime(2022, 1, 5, 9, 0, tzinfo=timezone.utc), **base,
        ),
        RegistroCatalogPart(
            id=104, name="PIVO SUPERIOR REFORCADO 18MM", width=18.0, height=44.0, gross_weight=0.7, ncm="87088000",
            barcode=None, application="SAVEIRO 1.6 1997/2005", born_at=1997, deprecated_at=2005,
            similarity_id=None, created=datetime(2022, 6, 1, 11, 30, tzinfo=timezone.utc), **base,
        ),
    ]


@dataclass
class CasoRoteirizado:
    """`executar_caso_fn` fake com a sequência de trace do pipeline real.

    Famílias com `search_ref` em `sinalizar` terminam com o merge sinalizado e um
    campo para revisão; em `intervir`, pedem intervenção humana antes do merge.
    """

    pausa: float = 0.0
    sinalizar: frozenset[str] = frozenset({"MB1085"})
    intervir: frozenset[str] = frozenset()
    respostas_intervencao: list[Any] = field(default_factory=list)

    def _esperar(self, cancel_event) -> None:
        if self.pausa:
            time.sleep(self.pausa)

    def __call__(self, search_ref, brand_id, llm, dependencias_fk, *, trace, on_aviso=None,
                 pedir_intervencao=None, cancel_event=None, **kwargs) -> ResultadoCaso:
        marca = {7: "DRIVEWAY", 9: "AFFINIA", 3: "CITROEN"}.get(brand_id, "MARCA")
        aviso: Callable[[str], None] = on_aviso or (lambda _m: None)
        grupo_ref = f"{search_ref}:{marca}"
        grupo = registros_roteirizados(search_ref, brand_id, marca)

        sinalizar_inicio(trace, "tool", "buscar_grupo", {"search_ref": search_ref, "brand_id": brand_id})
        self._esperar(cancel_event)
        trace.registrar(
            "tool", "buscar_grupo", duracao_ms=312.0,
            entrada={"search_ref": search_ref, "brand_id": brand_id},
            saida={"quantidade": len(grupo), "ids": [registro.id for registro in grupo]},
        )
        sinalizar_grupo(trace, grupo_ref, grupo)

        sinalizar_inicio(trace, "tool", "particionar_grupo", {"grupo_ref": grupo_ref, "pecas": 4})
        particao = llm.gerar_json("system", "user", {"properties": {"subclusters": {}}}, "particao")
        trace.registrar(
            "tool", "particionar_grupo", duracao_ms=2140.0,
            entrada={"grupo_ref": grupo_ref, "peca_ids": [101, 102, 103, 104]},
            saida={"subclusters": particao["subclusters"], "sinais_heuristicos": ["sufixo 18MM"], "regras_aplicaveis": []},
            justificativa="; ".join(s["justificativa"] for s in particao["subclusters"]),
        )

        sinalizar_inicio(trace, "tool", "montar_decisao_merge", {"grupo_ref": grupo_ref, "membro_ids": [101, 102]})
        nomes = ["PIVO DA SUSPENSAO SUPERIOR", "PIVO SUPERIOR"]
        aviso(f"Nomes divergentes para {search_ref} ({marca}): {nomes} — acionando verificação web antes de decidir.")
        sinalizar_inicio(trace, "tool", "verificar_nomenclatura_peca",
                         {"codigo": search_ref, "marca": marca, "nomes_conflitantes": nomes})
        aviso(_serper(1, f"Pivô de Suspensão Superior {marca} {search_ref}", "https://www.autopecas.example.com.br/pivo"))
        aviso(_serper(2, f"{search_ref} Pivô Superior - Catálogo {marca}", "https://catalogo.example.com/je4699"))
        self._esperar(cancel_event)
        trace.registrar(
            "tool", "verificar_nomenclatura_peca", duracao_ms=2380.0,
            entrada={"codigo": search_ref, "marca": marca, "nomes_conflitantes": nomes},
            saida={"status": "confirmado", "nome_sugerido": "PIVO SUPERIOR",
                   "fontes": [{"url": "https://www.autopecas.example.com.br/pivo", "nome_encontrado": "PIVO SUPERIOR"},
                              {"url": "https://catalogo.example.com/je4699", "nome_encontrado": "PIVO SUPERIOR"}]},
            justificativa="Duas fontes com o código exato usam PIVO SUPERIOR.",
        )
        aviso("Verificação web concluída: status=confirmado, nome_sugerido='PIVO SUPERIOR'")
        sinalizar_inicio(trace, "tool", "coletar_paginas", {"codigo": search_ref, "marca": marca})
        aviso(mensagem_evento({"evento": "coleta_paginas.entrada", "url": "https://www.autopecas.example.com.br/pivo",
                               "dominio": "www.autopecas.example.com.br", "desfecho": "armazenado", "motivo": None}))
        aviso(mensagem_evento({"evento": "coleta_paginas.entrada", "url": "https://catalogo.example.com/je4699",
                               "dominio": "catalogo.example.com", "desfecho": "falha", "motivo": "http_403"}))
        trace.registrar(
            "tool", "coletar_paginas", duracao_ms=1650.0,
            entrada={"codigo_peca": search_ref, "marca_peca": marca, "metodo": "serper", "quantidade_resultados": 2},
            saida={"desfecho": "executada", "motivo": None, "contagem": {"armazenado": 1, "reaproveitado": 0, "falha": 1, "url_invalida": 0}},
        )

        if search_ref in self.intervir and pedir_intervencao is not None:
            self.respostas_intervencao.append(pedir_intervencao(_pedido(search_ref, marca)))

        sinalizado = search_ref in self.sinalizar
        campos = [
            ("name", "PIVO SUPERIOR", "verificacao_web", "ok", "alta"),
            ("width", 16.0, "sem_conflito", "ok", "alta"),
            ("depth", None, "sem_conflito", "ok", "alta"),
            ("height", 42.0, "sem_conflito", "ok", "alta"),
            ("ncm", "87088000", "regra_confiabilidade", "ok", "alta"),
            ("application", "GOL 1.0 1991/2001\nPARATI 1.6 1996/2001", "normalizacao", "ok", None),
            ("gross_weight", None, "escalado_humano", "escalado" if sinalizado else "ok", "baixa"),
        ]
        # Sem sinalização, o peso não diverge e não vira decisão.
        campos = [c for c in campos if sinalizado or c[2] != "escalado_humano"]

        def _justificativa(status: str) -> str:
            return "Pesos de 0,41 kg e 0,62 kg sem fonte confiável." if status == "escalado" else "ok"

        if sinalizado:
            llm.gerar_json("system", "user", {"properties": {"acao": {}}}, "decisao_similarity_id")
            decisao: DecisaoMerge | GrupoSinalizado = GrupoSinalizado(
                grupo_ref=grupo_ref, motivo="similarity_id conflitante entre 101 (812) e 102 (977).",
                membro_ids=[101, 102],
            )
        else:
            decisao = DecisaoMerge(
                grupo_ref=grupo_ref, vencedor_id=101, perdedor_ids=[102],
                decisoes_campo=[
                    DecisaoCampo(
                        campo=nome, valor=valor, justificativa=_justificativa(status), fonte=fonte,
                        escalado_humano=status == "escalado", confianca=confianca,
                    )
                    for nome, valor, fonte, status, confianca in campos
                ],
            )
        trace.registrar(
            "tool", "montar_decisao_merge", duracao_ms=5210.0,
            entrada={"grupo_ref": grupo_ref, "membro_ids": [101, 102]},
            saida={"tipo": type(decisao).__name__, "decisao": decisao},
        )
        for nome, valor, fonte, status, confianca in campos:
            trace.registrar(
                "arbitragem", "arbitrar_campo", status=status,
                entrada={"campo": nome, "subcluster_ids": [101, 102]},
                saida={"valor": valor, "fonte": fonte, "confianca": confianca, "origem_id": None, "evidencias": []},
                justificativa=_justificativa(status),
            )
        if sinalizado:
            trace.registrar(
                "decisao", "grupo_sinalizado", status="revisao_manual",
                saida={"membro_ids": [101, 102], "motivo": decisao.motivo}, justificativa=decisao.motivo,
            )

        sinalizar_inicio(trace, "tool", "gerar_sql", {"grupo_ref": grupo_ref, "decisoes": 1})
        sql = "" if sinalizado else "BEGIN;\nUPDATE catalog_part SET name = 'PIVO SUPERIOR' WHERE id = 101;\nCOMMIT;"
        trace.registrar("tool", "gerar_sql", duracao_ms=4.0, entrada={"decisoes": 1},
                        saida={"sql": sql, "caracteres": len(sql)})
        return ResultadoCaso(
            grupo_ref=grupo_ref,
            particao=Particao(grupo_ref=grupo_ref, subclusters=[
                Subcluster(label=s["label"], membro_ids=s["membro_ids"], justificativa=s["justificativa"])
                for s in particao["subclusters"]
            ]),
            decisoes=[decisao], sql=sql, grupo=grupo,
        )


def sorteador(familias):
    """`sortear_fn` que devolve as famílias em ordem, sem repetir."""
    from tools.sortear_grupo import NenhumGrupoDuplicadoError

    restantes = list(familias)

    def sortear(*, excluir):
        for familia in restantes:
            if familia.chave not in excluir:
                return familia
        raise NenhumGrupoDuplicadoError("esgotado")

    return sortear
