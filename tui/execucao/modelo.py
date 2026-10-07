"""Modelo puro do painel de execução, sem Textual.

Recebe os eventos ao vivo do core (`loop.models.EventoAoVivo`) e mantém a árvore
que `tui/execucao/widgets.py` desenha: um bloco por família e, dentro dele, as
etapas com início e fim, o raciocínio do modelo, as decisões por campo e os
avisos. A família guarda também os registros sorteados e o registro final de
cada merge, que a "Rodar loop" mostra em tabelas. Tudo aqui é testável sem
subir a app, como `tui/seletores_execucao.py`.

Correlação (DESIGN §2.3): uma `EtapaIniciada` abre um item, e o próximo
`EventoExecucao` de mesmo `nome` o fecha. O pipeline roda uma etapa por vez numa
thread só, então uma pilha por família basta. Eventos sem item aberto viram
itens pontuais.

O painel só mostra o que já está no trace sanitizado ou nos registros que o JSON
do loop grava (`pecas` e `registro_final`), e portanto na auditoria, ou no texto
dos avisos: nada de prompt, nada de raciocínio privado.
"""

from __future__ import annotations

import json
import textwrap
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit

from coleta_paginas.integracao import PREFIXO_AVISO as PREFIXO_COLETA
from loop.models import (
    AvisoEmitido,
    EtapaIniciada,
    EventoExecucao,
    FamiliaSorteada,
    GrupoCarregado,
    IteracaoConcluida,
    IteracaoIniciada,
    IteracaoStatus,
    RespostaParcial,
)
from partitioning.models import ROTULOS_VALIDOS
from verification.serper_agent import PREFIXO_RESULTADO_ORGANICO

CURSOR = "▍"
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
MAX_FAMILIAS = 30
LINHAS_PARA_RECOLHER = 6
LINHAS_PREVIA = 2
TICKS_PARA_ALCANCAR = 10
PASSO_MINIMO = 3
_MAX_VALOR = 60
_MAX_TITULO_WEB = 60

TipoItem = Literal[
    "etapa", "raciocinio", "intervencao", "decisao", "sem_conflito", "sinalizacao", "aviso", "erro", "info",
]
EstadoItem = Literal["em_curso", "ok", "erro", "escalado", "interrompido", "aviso"]
EstadoFamilia = Literal["em_curso", "ok", "sinalizada", "erro", "cancelada"]


# ---------------------------------------------------------------------------
# Fila entre a thread do worker e a da interface
# ---------------------------------------------------------------------------


class FilaEventos:
    """Fila thread-safe: o worker publica sem esperar a interface, que drena no tick.

    Respostas parciais seguidas da mesma chamada LLM são coalescidas: cada uma já
    traz o texto inteiro até ali, então só a última importa.
    """

    def __init__(self) -> None:
        self._itens: deque[object] = deque()
        self._lock = Lock()

    def publicar(self, evento: object) -> None:
        with self._lock:
            if (
                isinstance(evento, RespostaParcial)
                and self._itens
                and isinstance(self._itens[-1], RespostaParcial)
                and self._itens[-1].nome == evento.nome
            ):
                self._itens[-1] = evento
            else:
                self._itens.append(evento)

    def drenar(self) -> list[object]:
        with self._lock:
            itens = list(self._itens)
            self._itens.clear()
        return itens

    def __len__(self) -> int:
        with self._lock:
            return len(self._itens)


# ---------------------------------------------------------------------------
# Árvore do painel
# ---------------------------------------------------------------------------


@dataclass(eq=False)
class Item:
    id: int
    tipo: TipoItem
    fase: str
    nome: str
    titulo: str
    estado: EstadoItem = "em_curso"
    resumo: str = ""
    texto: str = ""
    detalhes: Any = None
    duracao_ms: float | None = None
    campos: list[str] = field(default_factory=list)
    filhos: list[Item] = field(default_factory=list)
    versao: int = 0

    @property
    def aberto(self) -> bool:
        return self.estado == "em_curso"

    def tocar(self) -> None:
        self.versao += 1


@dataclass(eq=False)
class Familia:
    id: int
    indice: int
    total: int
    familia: FamiliaSorteada | None
    estado: EstadoFamilia = "em_curso"
    pecas: int | None = None
    merges: int = 0
    sinalizados: int = 0
    escalados: int = 0
    duracao_ms: float | None = None
    erro: str | None = None
    itens: list[Item] = field(default_factory=list)
    # Registros de `GrupoCarregado` (`None` até a busca do grupo terminar) e o
    # `registro_final` de cada merge, que chega com a conclusão da família.
    registros: tuple[dict[str, Any], ...] | None = None
    registros_finais: tuple[dict[str, Any], ...] = ()
    versao: int = 0
    pilha: list[Item] = field(default_factory=list, repr=False)
    ultimo_agrupador: Item | None = field(default=None, repr=False)

    @property
    def em_curso(self) -> bool:
        return self.estado == "em_curso"

    @property
    def precisa_atencao(self) -> bool:
        return self.estado in ("erro", "sinalizada") or self.escalados > 0

    def tocar(self) -> None:
        self.versao += 1


# Nome do fechamento de uma etapa quando o evento de erro usa outro nome.
_ALIAS_FECHAMENTO = {("pipeline", "recuperar_distintos"): "recuperar_distintos_name"}
# Etapas que reúnem as decisões por campo registradas logo depois delas.
_AGRUPADORES = frozenset({"montar_decisao_merge", "recuperar_distintos_name"})
# Eventos já representados de outra forma no painel.
_OCULTOS = frozenset({
    ("observabilidade", "aviso"),           # mesmo texto do AvisoEmitido
    ("loop", "sortear_grupo_aleatorio"),    # o cabeçalho da família já mostra
    ("loop", "executar_caso"),              # o erro vem completo no IteracaoConcluida
})
# Ecos que o pipeline registra depois do merge, sem `entrada`, para a auditoria.
_ECOS = frozenset({("tool", "verificar_nomenclatura_peca"), ("tool", "intervencao_humana")})


class ModeloPainel:
    def __init__(self, max_familias: int = MAX_FAMILIAS) -> None:
        self.familias: list[Familia] = []
        self.ocultas = 0
        self._removidas: list[int] = []
        self._alteradas: set[int] = set()
        self._atual: Familia | None = None
        self._proximo_id = 0
        self._max_familias = max_familias

    # -- entrada ---------------------------------------------------------------

    def aplicar(self, evento: object) -> None:
        if isinstance(evento, IteracaoIniciada):
            self._iniciar_familia(evento)
        elif isinstance(evento, IteracaoConcluida):
            self._concluir_familia(evento)
        elif isinstance(evento, EtapaIniciada):
            self._iniciar_etapa(evento)
        elif isinstance(evento, RespostaParcial):
            self._resposta_parcial(evento)
        elif isinstance(evento, EventoExecucao):
            self._evento_trace(evento)
        elif isinstance(evento, AvisoEmitido):
            self._aviso(evento)
        elif isinstance(evento, GrupoCarregado):
            self._grupo_carregado(evento)
        # LoopProgresso e qualquer outro tipo são da barra de controle.

    def consumir_alteracoes(self) -> tuple[list[int], set[int]]:
        """Famílias removidas pela janela e famílias alteradas desde a última chamada."""
        removidas, alteradas = self._removidas, self._alteradas
        self._removidas, self._alteradas = [], set()
        return removidas, alteradas

    @property
    def atual(self) -> Familia | None:
        return self._atual

    # -- famílias --------------------------------------------------------------

    def _novo_id(self) -> int:
        self._proximo_id += 1
        return self._proximo_id

    def _nova_familia(self, indice: int, total: int, familia: FamiliaSorteada | None) -> Familia:
        nova = Familia(id=self._novo_id(), indice=indice, total=total, familia=familia)
        self.familias.append(nova)
        self._alteradas.add(nova.id)
        while len(self.familias) > self._max_familias:
            removida = self.familias.pop(0)
            self.ocultas += 1
            self._removidas.append(removida.id)
            self._alteradas.discard(removida.id)
        return nova

    def _familia_do_evento(self) -> Familia:
        if self._atual is None:
            self._atual = self._nova_familia(0, 0, None)
        return self._atual

    def _iniciar_familia(self, evento: IteracaoIniciada) -> None:
        self._atual = self._nova_familia(evento.indice, evento.total, evento.familia)

    def _concluir_familia(self, evento: IteracaoConcluida) -> None:
        familia = self._atual
        if familia is None or (evento.familia is not None and familia.familia not in (None, evento.familia)):
            familia = self._nova_familia(evento.indice, evento.total, evento.familia)
        familia.indice, familia.total = evento.indice, evento.total
        if evento.familia is not None:
            familia.familia = evento.familia
        if evento.pecas or familia.pecas is None:
            familia.pecas = evento.pecas
        familia.merges, familia.sinalizados = evento.merges, evento.sinalizados
        familia.duracao_ms = evento.duracao_ms
        familia.registros_finais = tuple(evento.registros_finais)
        if evento.status == IteracaoStatus.ERRO:
            familia.estado = "erro"
        elif evento.status == IteracaoStatus.CANCELADA:
            familia.estado = "cancelada"
        elif evento.sinalizados or familia.escalados:
            familia.estado = "sinalizada"
        else:
            familia.estado = "ok"

        estado_pendente: EstadoItem = "erro" if familia.estado == "erro" else "interrompido"
        for item in familia.pilha:
            item.estado = estado_pendente
            item.tocar()
        familia.pilha.clear()

        if evento.erro and familia.estado in ("erro", "cancelada"):
            familia.erro = _uma_linha(f"{evento.erro.get('tipo', 'Erro')}: {evento.erro.get('mensagem', '')}", 200)
            self._anexar(familia, Item(
                id=self._novo_id(), tipo="erro", fase="loop", nome="erro",
                titulo="Execução interrompida" if familia.estado == "cancelada" else "Erro na família",
                estado="interrompido" if familia.estado == "cancelada" else "erro",
                resumo=familia.erro, detalhes=dict(evento.erro),
            ), None)
        familia.tocar()
        self._alteradas.add(familia.id)
        self._atual = None

    # -- itens -----------------------------------------------------------------

    def _anexar(self, familia: Familia, item: Item, pai: Item | None) -> None:
        if pai is None:
            familia.itens.append(item)
            familia.tocar()
        else:
            pai.filhos.append(item)
            pai.tocar()
        self._alteradas.add(familia.id)

    def _iniciar_etapa(self, evento: EtapaIniciada) -> None:
        familia = self._familia_do_evento()
        tipo: TipoItem = {"llm": "raciocinio", "intervencao": "intervencao"}.get(evento.fase, "etapa")
        item = Item(
            id=self._novo_id(), tipo=tipo, fase=evento.fase, nome=evento.nome,
            titulo=rotulo_etapa(evento.fase, evento.nome), resumo=resumo_inicio(evento),
        )
        self._anexar(familia, item, familia.pilha[-1] if familia.pilha else None)
        familia.pilha.append(item)

    def _resposta_parcial(self, evento: RespostaParcial) -> None:
        familia = self._atual
        if familia is None:
            return
        item = next(
            (i for i in reversed(familia.pilha) if i.tipo == "raciocinio" and i.nome == evento.nome), None
        )
        if item is None:
            return
        texto = texto_raciocinio(evento.resposta)
        if texto != item.texto:
            item.texto = texto
            item.tocar()
            self._alteradas.add(familia.id)

    def _evento_trace(self, evento: EventoExecucao) -> None:
        chave = (evento.fase, evento.nome)
        if chave in _OCULTOS or (chave in _ECOS and not evento.entrada):
            return
        familia = self._familia_do_evento()
        if chave == ("tool", "buscar_grupo") and isinstance(evento.saida.get("quantidade"), int):
            familia.pecas = evento.saida["quantidade"]
            familia.tocar()

        nome = _ALIAS_FECHAMENTO.get(chave, evento.nome)
        posicao = next(
            (
                p for p in range(len(familia.pilha) - 1, -1, -1)
                if familia.pilha[p].nome == nome and (evento.fase != "llm" or familia.pilha[p].tipo == "raciocinio")
            ),
            None,
        )
        if posicao is not None:
            item = familia.pilha[posicao]
            for acima in familia.pilha[posicao + 1:]:
                acima.estado = "interrompido"
                acima.tocar()
            del familia.pilha[posicao:]
            self._fechar(item, evento)
            if item.nome in _AGRUPADORES:
                familia.ultimo_agrupador = item
        elif chave == ("arbitragem", "arbitrar_campo"):
            self._decisao(familia, evento)
        else:
            self._pontual(familia, evento)
        self._alteradas.add(familia.id)

    def _fechar(self, item: Item, evento: EventoExecucao) -> None:
        item.duracao_ms = evento.duracao_ms
        item.detalhes = detalhes_evento(evento)
        item.estado = _estado_do_status(evento.status)
        item.resumo = resumo_erro(evento) if item.estado == "erro" else resumir_evento(evento)
        if item.tipo == "raciocinio" and item.estado != "erro":
            final = texto_raciocinio(evento.saida.get("resposta_estruturada"))
            if final:
                item.texto = final
        item.tocar()

    def _decisao(self, familia: Familia, evento: EventoExecucao) -> None:
        pai = familia.pilha[-1] if familia.pilha else familia.ultimo_agrupador
        irmaos = pai.filhos if pai is not None else familia.itens
        campo = str(evento.entrada.get("campo") or "?")
        saida = evento.saida
        fonte = saida.get("fonte")
        if fonte == "sem_conflito" and evento.status != "escalado":
            agregado = next((i for i in irmaos if i.tipo == "sem_conflito"), None)
            if agregado is None:
                agregado = Item(
                    id=self._novo_id(), tipo="sem_conflito", fase=evento.fase, nome=evento.nome,
                    titulo="", estado="ok",
                )
                self._anexar(familia, agregado, pai)
            agregado.campos.append(campo)
            agregado.titulo = f"{len(agregado.campos)} campo(s) sem conflito"
            agregado.resumo = ", ".join(agregado.campos)
            agregado.tocar()
            return

        escalado = evento.status == "escalado"
        if escalado:
            familia.escalados += 1
            familia.tocar()
        partes = [rotulo_fonte(fonte)]
        if saida.get("confianca"):
            partes.append(f"confiança {saida['confianca']}")
        if escalado and evento.justificativa:
            partes.append(_uma_linha(evento.justificativa, 120))
        self._anexar(familia, Item(
            id=self._novo_id(), tipo="decisao", fase=evento.fase, nome=evento.nome,
            titulo=f"{campo} → revisão humana" if escalado else f"{campo} = {valor_curto(saida.get('valor'))}",
            estado="escalado" if escalado else "ok",
            resumo=" · ".join(p for p in partes if p),
            detalhes=detalhes_evento(evento),
        ), pai)

    def _pontual(self, familia: Familia, evento: EventoExecucao) -> None:
        pai = familia.pilha[-1] if familia.pilha else None
        if (evento.fase, evento.nome) == ("decisao", "grupo_sinalizado"):
            item = Item(
                id=self._novo_id(), tipo="sinalizacao", fase=evento.fase, nome=evento.nome,
                titulo="Grupo sinalizado para revisão", estado="escalado",
                resumo=_uma_linha(str(evento.saida.get("motivo") or evento.justificativa or ""), 160),
                detalhes=detalhes_evento(evento),
            )
            self._anexar(familia, item, pai or familia.ultimo_agrupador)
            return
        estado = _estado_do_status(evento.status)
        self._anexar(familia, Item(
            id=self._novo_id(), tipo="erro" if estado == "erro" else "info",
            fase=evento.fase, nome=evento.nome, titulo=rotulo_etapa(evento.fase, evento.nome),
            estado=estado, resumo=resumo_erro(evento) if estado == "erro" else resumir_evento(evento),
            detalhes=detalhes_evento(evento), duracao_ms=evento.duracao_ms,
        ), pai)

    def _aviso(self, evento: AvisoEmitido) -> None:
        familia = self._familia_do_evento()
        titulo, detalhes = classificar_aviso(evento.mensagem)
        self._anexar(familia, Item(
            id=self._novo_id(), tipo="aviso", fase="aviso", nome="aviso",
            titulo=titulo, estado="aviso", detalhes=detalhes,
        ), familia.pilha[-1] if familia.pilha else None)

    def _grupo_carregado(self, evento: GrupoCarregado) -> None:
        familia = self._familia_do_evento()
        familia.registros = tuple(evento.registros)
        familia.tocar()
        self._alteradas.add(familia.id)


def _estado_do_status(status: str) -> EstadoItem:
    if status == "erro":
        return "erro"
    if status in ("escalado", "revisao_manual"):
        return "escalado"
    if status == "cancelado":
        return "interrompido"
    return "ok"


# ---------------------------------------------------------------------------
# Textos (renderers por tipo de evento, R6)
# ---------------------------------------------------------------------------

_ROTULOS = {
    "buscar_grupo": "Buscar grupo",
    "particionar_grupo": "Particionar grupo",
    "montar_decisao_merge": "Arbitrar subcluster",
    "recuperar_distintos_name": "Recuperar distintos pelo nome",
    "verificar_nomenclatura_peca": "Verificação web do nome",
    "coletar_paginas": "Coleta de páginas",
    "gerar_sql": "Gerar SQL",
    "arbitrar_por_provedor": "Provedor de informação",
    "consultar_memoria": "Memória de intervenções",
    "intervencao_humana": "Intervenção humana",
}
_ROTULOS_LLM = {
    "particao": "particionamento",
    "decisao_nome": "nome",
    "decisao_campo": "campo",
    "decisao_similarity_id": "similarity_id",
}
_ROTULOS_FONTE = {
    "regra_confiabilidade": "regra de confiabilidade",
    "julgamento_modelo": "julgamento do modelo",
    "normalizacao": "normalização",
    "escalado_humano": "escalado",
    "verificacao_web": "verificação web",
    "intervencao_humana": "intervenção humana",
    "sem_conflito": "sem conflito",
}


def rotulo_etapa(fase: str, nome: str) -> str:
    if fase == "llm":
        return f"Raciocínio · {_ROTULOS_LLM.get(nome, nome)}"
    return _ROTULOS.get(nome, nome.replace("_", " "))


def rotulo_fonte(fonte: Any) -> str:
    return _ROTULOS_FONTE.get(fonte, str(fonte)) if fonte else ""


def _ids(valores: Any, limite: int = 4) -> str:
    if not isinstance(valores, list) or not valores:
        return ""
    texto = ", ".join(str(v) for v in valores[:limite])
    return texto + (f" +{len(valores) - limite}" if len(valores) > limite else "")


def resumo_inicio(evento: EtapaIniciada) -> str:
    d = evento.detalhes
    if evento.nome == "buscar_grupo":
        return f"{d.get('search_ref', '')} · marca {d.get('brand_id', '')}"
    if evento.nome == "particionar_grupo":
        return f"{d.get('pecas', '?')} peça(s)"
    if evento.nome in _AGRUPADORES:
        return f"ids {_ids(d.get('membro_ids'))}"
    if evento.nome == "verificar_nomenclatura_peca":
        return " / ".join(str(n) for n in d.get("nomes_conflitantes") or [])
    if evento.nome == "gerar_sql":
        return f"{d.get('decisoes', 0)} decisão(ões)"
    if evento.nome == "intervencao_humana":
        motivo = _uma_linha(str(d.get("motivo") or ""), 100)
        return "aguardando o operador" + (f" · {motivo}" if motivo else "")
    return ""


def resumo_erro(evento: EventoExecucao) -> str:
    erro = evento.saida.get("erro") or evento.detalhes.get("tipo") or evento.status
    return f"erro: {erro}"


def _resumo_particao(evento: EventoExecucao) -> str:
    subclusters = evento.saida.get("subclusters") or []
    contagem: dict[str, int] = {}
    for sub in subclusters:
        if isinstance(sub, Mapping):
            rotulo = str(sub.get("label"))
            contagem[rotulo] = contagem.get(rotulo, 0) + 1
    detalhe = ", ".join(f"{n} {rotulo}" for rotulo, n in contagem.items())
    return f"{len(subclusters)} subcluster(s)" + (f": {detalhe}" if detalhe else "")


def _resumo_merge(evento: EventoExecucao) -> str:
    tipo = evento.saida.get("tipo")
    decisao = evento.saida.get("decisao")
    if tipo == "DecisaoMerge" and isinstance(decisao, Mapping):
        return f"merge: mantém {decisao.get('vencedor_id')}, remove {_ids(decisao.get('perdedor_ids'))}"
    if tipo == "GrupoSinalizado":
        return "sinalizado para revisão"
    return "nada a mesclar"


def _resumo_verificacao(evento: EventoExecucao) -> str:
    if evento.status == "desligada":
        return "pesquisa web desligada pelo operador"
    s = evento.saida
    partes = [str(s.get("status") or "")]
    if s.get("nome_sugerido"):
        partes.append(str(s["nome_sugerido"]))
    if isinstance(s.get("fontes"), list):
        partes.append(f"{len(s['fontes'])} fonte(s)")
    return " · ".join(p for p in partes if p)


def _resumo_coleta(evento: EventoExecucao) -> str:
    s = evento.saida
    contagem = s.get("contagem") if isinstance(s.get("contagem"), Mapping) else {}
    numeros = ", ".join(f"{n} {desfecho}" for desfecho, n in contagem.items() if n)
    partes = [str(s.get("desfecho") or evento.status)]
    if s.get("motivo"):
        partes.append(str(s["motivo"]))
    if numeros:
        partes.append(numeros)
    return " · ".join(partes)


def _resumo_sql(evento: EventoExecucao) -> str:
    caracteres = evento.saida.get("caracteres") or 0
    return f"SQL revisável · {caracteres} caracteres" if caracteres else "sem SQL"


def _resumo_memoria(evento: EventoExecucao) -> str:
    s = evento.saida
    if not s.get("encontrada"):
        return "nenhuma regra acima do limiar"
    score = s.get("score")
    return f"regra {s.get('regra')!r}" + (f" · score {score:.2f}" if isinstance(score, (int, float)) else "")


def _resumo_provedor(evento: EventoExecucao) -> str:
    s = evento.saida
    if s.get("valor") is None:
        return "sem decisão do provedor"
    return f"{valor_curto(s.get('valor'))} · {rotulo_fonte(s.get('fonte'))}"


def _resumo_recuperacao(evento: EventoExecucao) -> str:
    s = evento.saida
    if evento.status == "escalado" or s.get("escalado_humano"):
        return "sem autorização para merge"
    return f"nome = {valor_curto(s.get('valor'))} · {rotulo_fonte(s.get('fonte'))}"


def _resumo_intervencao(evento: EventoExecucao) -> str:
    s = evento.saida
    regra = s.get("regra")
    partes = []
    if isinstance(regra, Mapping) and regra.get("titulo"):
        partes.append(f"regra {regra['titulo']!r}")
    if s.get("valor") is not None:
        partes.append(f"valor {valor_curto(s['valor'])}")
    if s.get("acao"):
        partes.append(f"ação {s['acao']}")
    return " · ".join(partes) or "respondida"


_RENDERERS: dict[str, Callable[[EventoExecucao], str]] = {
    "buscar_grupo": lambda e: f"{e.saida.get('quantidade', '?')} registro(s)",
    "particionar_grupo": _resumo_particao,
    "montar_decisao_merge": _resumo_merge,
    "recuperar_distintos_name": _resumo_recuperacao,
    "verificar_nomenclatura_peca": _resumo_verificacao,
    "coletar_paginas": _resumo_coleta,
    "gerar_sql": _resumo_sql,
    "consultar_memoria": _resumo_memoria,
    "arbitrar_por_provedor": _resumo_provedor,
    "intervencao_humana": _resumo_intervencao,
}


def resumir_evento(evento: EventoExecucao) -> str:
    """Resumo de uma linha do evento; o JSON completo fica nos detalhes recolhidos."""
    if evento.fase == "llm":
        return ""
    renderer = _RENDERERS.get(evento.nome)
    try:
        return renderer(evento) if renderer is not None else ""
    except Exception:  # noqa: BLE001 — um formato inesperado não pode quebrar o painel
        return ""


def detalhes_evento(evento: EventoExecucao) -> dict[str, Any] | None:
    detalhes: dict[str, Any] = {}
    for chave in ("entrada", "saida"):
        valor = getattr(evento, chave)
        if valor:
            detalhes[chave] = valor
    if evento.justificativa:
        detalhes["justificativa"] = evento.justificativa
    return detalhes or None


def texto_raciocinio(resposta: Any) -> str:
    """Raciocínio observável de uma resposta estruturada, parcial ou final.

    `justificativa` de topo (nome, campo, similarity_id) e a de cada subcluster
    do particionamento, uma por linha.
    """
    if not isinstance(resposta, Mapping):
        return ""
    partes: list[str] = []
    justificativa = resposta.get("justificativa")
    if isinstance(justificativa, str) and justificativa.strip():
        partes.append(justificativa)
    subclusters = resposta.get("subclusters")
    if isinstance(subclusters, list):
        for sub in subclusters:
            if not isinstance(sub, Mapping):
                continue
            texto = sub.get("justificativa")
            if isinstance(texto, str) and texto:
                rotulo = sub.get("label")
                prefixo = f"{rotulo}: " if rotulo in ROTULOS_VALIDOS else ""
                partes.append(prefixo + texto)
    return "\n".join(partes)


def valor_curto(valor: Any) -> str:
    if valor is None:
        return "—"
    return _uma_linha(str(valor), _MAX_VALOR)


def _uma_linha(texto: str, limite: int) -> str:
    linhas = texto.strip().splitlines() or [""]
    primeira = linhas[0]
    cortado = len(linhas) > 1 or len(primeira) > limite
    return (primeira[:limite].rstrip() + "…") if cortado else primeira


# ---------------------------------------------------------------------------
# Avisos (§4.4): linhas com JSON viram resumo, o JSON fica recolhido
# ---------------------------------------------------------------------------


def classificar_aviso(texto: str) -> tuple[str, Any]:
    """(texto da linha, detalhes) de uma mensagem do `on_aviso`."""
    for prefixo, resumir in (
        (PREFIXO_RESULTADO_ORGANICO, _resumo_aviso_serper),
        (PREFIXO_COLETA, _resumo_aviso_coleta),
    ):
        if texto.startswith(prefixo):
            dados = _json_objeto(texto[len(prefixo):])
            if dados is not None:
                try:
                    return resumir(dados), dados
                except Exception:  # noqa: BLE001
                    return prefixo.rstrip(":"), dados
            break
    return texto, None


def _json_objeto(texto: str) -> dict | None:
    try:
        valor = json.loads(texto)
    except ValueError:
        return None
    return valor if isinstance(valor, dict) else None


def _host(url: Any) -> str:
    try:
        return urlsplit(str(url)).hostname or str(url)
    except ValueError:
        return str(url)


def _resumo_aviso_serper(d: dict) -> str:
    titulo = _uma_linha(str(d.get("title") or ""), _MAX_TITULO_WEB)
    return f"Serper #{d.get('ordinal', '?')} · {titulo} · {_host(d.get('link', ''))}"


def _resumo_aviso_coleta(d: dict) -> str:
    evento = d.get("evento")
    if evento == "coleta_paginas.entrada":
        motivo = f" ({d['motivo']})" if d.get("motivo") else ""
        return f"Coleta · {d.get('dominio') or _host(d.get('url', ''))} · {d.get('desfecho')}{motivo}"
    if evento == "coleta_paginas.resumo":
        contagem = d.get("contagem") if isinstance(d.get("contagem"), Mapping) else {}
        numeros = ", ".join(f"{n} {k}" for k, n in contagem.items() if n)
        return f"Coleta concluída · {d.get('status')}" + (f" · {numeros}" if numeros else "")
    if evento == "coleta_paginas.stealth_inicio":
        return f"Coleta · tentando navegador stealth em {d.get('dominio')}"
    if "desfecho" in d:
        return f"Coleta · {d['desfecho']}" + (f" ({d['motivo']})" if d.get("motivo") else "")
    if "motivo" in d:
        return f"Coleta · {d['motivo']}"
    return "Coleta de páginas"


# ---------------------------------------------------------------------------
# Linhas prontas para desenhar
# ---------------------------------------------------------------------------


def formatar_duracao(ms: float | None) -> str:
    if ms is None:
        return ""
    if ms < 1000:
        return f"{ms:.0f} ms"
    segundos = ms / 1000
    if segundos < 60:
        return f"{segundos:.1f} s".replace(".", ",")
    minutos, resto = divmod(int(round(segundos)), 60)
    return f"{minutos} min {resto:02d} s"


def quadro_spinner(quadro: int) -> str:
    return SPINNER[(quadro // 2) % len(SPINNER)]


_GLIFOS: dict[str, str] = {"ok": "✓", "erro": "✗", "escalado": "⚠", "interrompido": "■", "aviso": "·"}


def linha_item(item: Item, quadro: int = 0, expandido: bool = False) -> str:
    if item.aberto:
        glifo = quadro_spinner(quadro)
    elif item.tipo == "raciocinio" and item.estado == "ok":
        glifo = "◆"
    else:
        glifo = _GLIFOS.get(item.estado, "·")
    partes = [item.titulo]
    if item.resumo:
        partes.append(item.resumo)
    duracao = formatar_duracao(item.duracao_ms)
    if duracao:
        partes.append(duracao)
    linha = f"{glifo} " + " · ".join(partes)
    if item.detalhes is not None:
        linha += " ▾" if expandido else " ▸"
    return linha


_GLIFOS_FAMILIA = {"ok": "✓", "sinalizada": "⚠", "erro": "✗", "cancelada": "■"}


def linha_familia(familia: Familia, quadro: int = 0) -> str:
    glifo = quadro_spinner(quadro) if familia.em_curso else _GLIFOS_FAMILIA.get(familia.estado, "·")
    partes: list[str] = []
    if familia.total > 1:
        partes.append(f"{familia.indice}/{familia.total}")
    if familia.familia is not None:
        partes.extend([familia.familia.search_ref, familia.familia.brand or str(familia.familia.brand_id)])
    elif not familia.total:
        partes.append("Execução")
    else:
        partes.append("sorteio da família")
    if familia.pecas:
        partes.append(f"{familia.pecas} peça(s)")
    if familia.em_curso:
        partes.append("em curso")
    else:
        if familia.merges:
            partes.append(f"{familia.merges} merge(s)")
        if familia.sinalizados:
            partes.append(f"{familia.sinalizados} sinalizado(s)")
        if familia.escalados:
            partes.append(f"{familia.escalados} campo(s) para revisão")
        if familia.estado == "erro":
            partes.append("erro")
        elif familia.estado == "cancelada":
            partes.append("cancelada")
        duracao = formatar_duracao(familia.duracao_ms)
        if duracao:
            partes.append(duracao)
    return f"{glifo} " + " · ".join(partes)


# ---------------------------------------------------------------------------
# Tabelas de registros (tela "Rodar loop")
# ---------------------------------------------------------------------------

# Colunas da tabela dos registros sorteados, na ordem pedida pelo time de dados.
COLUNAS_REGISTRO: tuple[str, ...] = (
    "id", "name", "born_at", "deprecated_at",
    "width", "depth", "height", "gross_weight", "net_weight",
    "ncm", "barcode", "similarity_id",
)
# O vencedor mostra também os campos que a tabela dos sorteados não tem. Um campo
# a mais no registro entra depois destes, e `application` (longa) fica por último.
_EXTRAS_VENCEDOR: tuple[str, ...] = ("search_ref", "brand", "brand_id", "created")
_MAX_CELULA = 60
_MAX_APLICACAO = 40


def resumo_aplicacao(texto: str) -> str:
    """Primeira linha da aplicação e quantas linhas faltam (o texto inteiro fica no JSON)."""
    linhas = [linha.strip() for linha in texto.splitlines() if linha.strip()]
    if not linhas:
        return ""
    primeira = linhas[0]
    if len(primeira) > _MAX_APLICACAO:
        primeira = primeira[:_MAX_APLICACAO].rstrip() + "…"
    return primeira + (f" (+{len(linhas) - 1} linha(s))" if len(linhas) > 1 else "")


def celula(campo: str, valor: Any) -> str:
    """Texto de uma célula: vazio para nulo, `created` até o minuto e `application` resumida."""
    if valor is None:
        return ""
    if campo == "created":
        return str(valor).replace("T", " ")[:16]
    if campo == "application":
        return resumo_aplicacao(str(valor))
    return _uma_linha(str(valor), _MAX_CELULA)


def linha_registro(valores: Mapping[str, Any], colunas: Sequence[str]) -> list[str]:
    return [celula(coluna, valores.get(coluna)) for coluna in colunas]


def valores_registro_final(registro_final: Mapping[str, Any]) -> dict[str, Any]:
    """Campos do vencedor depois do merge, a partir do `registro_final` do JSON do loop."""
    campos = registro_final.get("campos")
    return {
        "id": registro_final.get("id_mantido"),
        "name": registro_final.get("nome"),
        "search_ref": registro_final.get("codigo"),
        "brand": registro_final.get("marca"),
        "brand_id": registro_final.get("brand_id"),
        **(dict(campos) if isinstance(campos, Mapping) else {}),
    }


def colunas_vencedor(registros_finais: Sequence[Mapping[str, Any]]) -> list[str]:
    """As colunas dos sorteados e, depois, todos os outros campos do vencedor."""
    colunas = list(COLUNAS_REGISTRO)
    extras = list(_EXTRAS_VENCEDOR)
    for registro in registros_finais:
        for chave in valores_registro_final(registro):
            if chave not in colunas and chave not in extras and chave != "application":
                extras.append(chave)
    return colunas + extras + ["application"]


def legenda_registros(familia: Familia) -> str:
    quantidade = len(familia.registros or ())
    return f"Registros sorteados · {quantidade}" if quantidade else "Nenhum registro encontrado para esta família."


def legenda_vencedor(familia: Familia) -> str:
    """Legenda da tabela do vencedor, ou o motivo de não haver vencedor."""
    finais = familia.registros_finais
    if not finais:
        if familia.estado == "erro":
            motivo = "a família terminou com erro"
        elif familia.estado == "cancelada":
            motivo = "execução cancelada"
        elif familia.merges:
            motivo = f"{familia.merges} merge(s), detalhes no JSON do loop"
        elif familia.sinalizados:
            motivo = f"{familia.sinalizados} grupo(s) sinalizado(s) para revisão, sem merge automático"
        else:
            motivo = "nenhum merge nesta família"
        return f"Nenhum registro vencedor: {motivo}."

    partes = ["Registro vencedor" if len(finais) == 1 else f"Registros vencedores ({len(finais)} merges)"]
    for registro in finais:
        removidos = _ids(registro.get("ids_removidos"), limite=6)
        partes.append(f"mantém {registro.get('id_mantido')}" + (f" (remove {removidos})" if removidos else ""))
    partes.append("valores depois do merge")
    escalados: list[str] = []
    for registro in finais:
        for campo_escalado in registro.get("campos_escalados") or []:
            if campo_escalado not in escalados:
                escalados.append(str(campo_escalado))
    if escalados:
        partes.append("revisão humana: " + ", ".join(escalados))
    return " · ".join(partes)


# ---------------------------------------------------------------------------
# Efeito digitando (R5)
# ---------------------------------------------------------------------------


@dataclass
class EstadoDigitacao:
    """Texto-alvo revelado aos poucos, a velocidade constante, alcançando o alvo
    em até `TICKS_PARA_ALCANCAR` ticks depois da última atualização."""

    alvo: str = ""
    exibidos: int = 0
    passo: int = PASSO_MINIMO

    def definir_alvo(self, texto: str) -> None:
        mostrado = self.alvo[: self.exibidos]
        if not texto.startswith(mostrado):
            comum = 0
            for a, b in zip(mostrado, texto):
                if a != b:
                    break
                comum += 1
            self.exibidos = comum
        self.alvo = texto
        restante = len(texto) - self.exibidos
        self.passo = max(PASSO_MINIMO, -(-restante // TICKS_PARA_ALCANCAR))

    @property
    def alcancou(self) -> bool:
        return self.exibidos >= len(self.alvo)

    def avancar(self) -> bool:
        if self.alcancou:
            return False
        self.exibidos = min(len(self.alvo), self.exibidos + self.passo)
        return True

    def visivel(self, aberto: bool) -> str:
        texto = self.alvo[: self.exibidos]
        return texto + CURSOR if aberto or not self.alcancou else texto


def recolher_texto(texto: str, largura: int) -> tuple[str, int] | None:
    """(prévia, linhas ocultas) quando o texto quebrado em `largura` passa de
    `LINHAS_PARA_RECOLHER` linhas; `None` quando cabe inteiro."""
    linhas: list[str] = []
    for paragrafo in texto.splitlines() or [""]:
        linhas.extend(textwrap.wrap(paragrafo, max(10, largura)) or [""])
    if len(linhas) <= LINHAS_PARA_RECOLHER:
        return None
    return "\n".join(linhas[:LINHAS_PREVIA]), len(linhas) - LINHAS_PREVIA
