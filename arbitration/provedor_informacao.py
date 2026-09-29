"""Tool_Provedor_Informacao — arbitragem por confiabilidade de fonte.

Primeiro critério de desempate da cascata de arbitragem (SPEC.md §4.2; spec
`information-provider-tool`): dado um subcluster `duplicata_real` e um campo em
conflito, escolhe o valor afirmado pela FONTE de maior confiabilidade (provedor
de informação ou owner legado). Genérica por campo — vale para `name`, campos
numéricos/discretos, datas derivadas e `application`, sem lógica específica de
nome.

Pureza no centro, I/O na borda:
- `_ranquear_fontes` e `_selecionar_vencedor` são funções PURAS: recebem os
  registros, o campo e callables de lookup; não abrem conexão nem importam
  Textual.
- Os lookups (`buscar_fonte`, `calcular_nivel_confiabilidade`) são injetáveis
  (default = implementação real de `tools.reliability`), no mesmo padrão de
  `arbitration/campo_numerico.py`, permitindo testar com fakes sem banco.

A tool NUNCA inventa vencedor: quando a confiabilidade não desempata (empate no
topo com valores distintos, nenhuma fonte rastreável, ou mesmo provider
afirmando valores distintos), produz Arbitragem_Inconclusiva e delega para a
próxima etapa da cascata, preservando os valores originais dos registros.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from arbitration.models import DecisaoCampo
from tools.group_fetch import RegistroCatalogPart
from tools.reliability import (
    ORDEM_CONFIABILIDADE,
    FonteCampo,
    NivelConfiabilidade,
    buscar_fonte_atual,
    nivel_confiabilidade,
)

# Tipo de um registro anotado com a fonte do valor do campo e o nível de
# confiabilidade dessa fonte (None = desconhecido / não rastreável).
_RegistroAnotado = tuple[RegistroCatalogPart, FonteCampo, "NivelConfiabilidade | None"]


def _campos_suportados() -> frozenset[str]:
    """Fonte única de verdade dos campos suportados: `CAMPOS_SUPORTADOS` do
    dispatcher `arbitration.arbitrar`.

    O import é TARDIO (dentro da função), de propósito: a Tarefa 6 vai fazer
    `arbitration/arbitrar.py` importar `arbitrar_por_provedor` deste módulo. Um
    `from arbitration.arbitrar import CAMPOS_SUPORTADOS` no topo criaria um ciclo
    de importação (arbitrar -> provedor_informacao -> arbitrar). Adiar o import
    para o momento da chamada quebra o ciclo sem duplicar a lista de campos — a
    definição continua vivendo só em `arbitrar.py`.
    """
    from arbitration.arbitrar import CAMPOS_SUPORTADOS

    return CAMPOS_SUPORTADOS


def _ranquear_fontes(
    registros: list[RegistroCatalogPart],
    campo: str,
    brand_id: int,
    buscar_fonte: Callable[[int, str], FonteCampo],
    calcular_nivel: Callable[[FonteCampo, int], "NivelConfiabilidade | None"],
) -> list[_RegistroAnotado]:
    """Resolve, para cada registro, a `FonteCampo` do valor do campo e o
    `NivelConfiabilidade` dessa fonte. Função PURA dados os lookups.

    Retorna a lista anotada `(registro, fonte, nivel)`, um item por registro, na
    mesma ordem de entrada. `nivel` é `None` quando a fonte não é rastreável /
    desconhecida.

    _Requirements: 6.1, 8.5_
    """
    anotados: list[_RegistroAnotado] = []
    for registro in registros:
        fonte = buscar_fonte(registro.id, campo)
        nivel = calcular_nivel(fonte, brand_id)
        anotados.append((registro, fonte, nivel))
    return anotados


@dataclass(frozen=True)
class _ResultadoSelecao:
    """Resultado discriminado da seleção de vencedor, separando a ESCOLHA (esta
    dataclass) da CONSTRUÇÃO do `DecisaoCampo` (na função pública).

    - `tipo == "vencedor"`: `valor`, `origem_id` (id do registro vencedor) e
      `nivel` (nível de confiabilidade vencedor) estão preenchidos.
    - `tipo == "inconclusivo"`: `motivo` descreve o impasse (empate no topo /
      sem fonte rastreável / mesmo provider ambíguo); os demais campos ficam
      `None`.
    """

    tipo: Literal["vencedor", "inconclusivo"]
    valor: object | None = None
    origem_id: int | None = None
    nivel: "NivelConfiabilidade | None" = None
    motivo: str | None = None


# Motivos de inconclusividade — texto usado na justificativa do DecisaoCampo
# escalado, sem vazar prompt/raciocínio/segredos (R8.6).
_MOTIVO_SEM_FONTE = "nenhuma fonte rastreável entre os registros divergentes"
_MOTIVO_EMPATE_TOPO = "empate no maior nível de confiabilidade com valores distintos"
_MOTIVO_PROVIDER_AMBIGUO = "o mesmo provedor afirma valores distintos"


def _selecionar_vencedor(
    anotados: list[_RegistroAnotado],
    campo: str,
) -> _ResultadoSelecao:
    """Aplica a ordem total de confiabilidade e decide vencedor ou impasse.

    Entre as fontes rastreáveis (nível != desconhecido), acha o MAIOR nível
    presente e olha os valores distintos do `campo` afirmados pelas fontes NESSE
    nível máximo:

    - 0 fontes rastreáveis (todas `desconhecido`/None) -> inconclusivo por
      ausência.                                                    (R6.5, R7.2)
    - o mesmo `provider_id` no topo afirma 2+ valores distintos -> inconclusivo,
      mesmo que fosse o único no topo.                                    (R7.3)
    - exatamente 1 valor distinto no topo -> vencedor.                    (R6.2)
    - 2+ valores distintos no topo -> inconclusivo por empate.      (R6.4, R7.1)

    Função PURA: só olha a lista anotada.

    _Requirements: 6.1, 6.2, 6.4, 6.5, 7.1, 7.2, 7.3_
    """
    rastreaveis = [
        (registro, fonte, nivel) for (registro, fonte, nivel) in anotados if nivel is not None
    ]
    if not rastreaveis:
        return _ResultadoSelecao(tipo="inconclusivo", motivo=_MOTIVO_SEM_FONTE)

    melhor_ordem = max(ORDEM_CONFIABILIDADE[nivel] for _, _, nivel in rastreaveis)
    topo = [
        (registro, fonte, nivel)
        for (registro, fonte, nivel) in rastreaveis
        if ORDEM_CONFIABILIDADE[nivel] == melhor_ordem
    ]

    # R7.3: mesmo provider (mesmo `provider_id` não nulo) afirmando 2+ valores
    # distintos no topo é ambíguo — verificamos antes do desempate por valor.
    valores_por_provider: dict[int, set[object]] = {}
    for registro, fonte, _ in topo:
        if fonte.provider_id is not None:
            valores_por_provider.setdefault(fonte.provider_id, set()).add(getattr(registro, campo))
    if any(len(valores) >= 2 for valores in valores_por_provider.values()):
        return _ResultadoSelecao(tipo="inconclusivo", motivo=_MOTIVO_PROVIDER_AMBIGUO)

    valores_no_topo = {getattr(registro, campo) for registro, _, _ in topo}
    if len(valores_no_topo) >= 2:
        return _ResultadoSelecao(tipo="inconclusivo", motivo=_MOTIVO_EMPATE_TOPO)

    registro_vencedor, _, nivel_vencedor = topo[0]
    return _ResultadoSelecao(
        tipo="vencedor",
        valor=getattr(registro_vencedor, campo),
        origem_id=registro_vencedor.id,
        nivel=nivel_vencedor,
    )


def _evidencias(anotados: list[_RegistroAnotado]) -> list[dict[str, object]]:
    """Uma entrada de evidência por registro considerado: `part_id`, a
    `Fonte_Campo` (`provider_id`/`owner_id`) e o `nivel` correspondente.

    NUNCA inclui prompt completo, raciocínio privado, chaves ou senhas (R8.6) —
    a tool é determinística e não usa LLM; estes campos são apenas
    identificadores e o nível classificado.

    _Requirements: 8.5, 8.6_
    """
    return [
        {
            "part_id": registro.id,
            "provider_id": fonte.provider_id,
            "owner_id": fonte.owner_id,
            "nivel": nivel,
        }
        for (registro, fonte, nivel) in anotados
    ]


def arbitrar_por_provedor(
    registros: list[RegistroCatalogPart],
    campo: str,
    brand_id: int,
    buscar_fonte: Callable[[int, str], FonteCampo] = buscar_fonte_atual,
    calcular_nivel_confiabilidade: Callable[
        [FonteCampo, int], "NivelConfiabilidade | None"
    ] = nivel_confiabilidade,
) -> DecisaoCampo:
    """Primeiro desempate por confiabilidade de fonte, genérico por campo.

    - Subcluster vazio -> `ValueError` (R1.5).
    - Campo não suportado -> `ValueError` (R1.4).
    - Um único registro -> `DecisaoCampo` `sem_conflito` (R1.2).
    - Consenso (todos com o mesmo valor) -> `sem_conflito` (R1.3, R7.5).
    - Vencedor único no topo -> `regra_confiabilidade` com valor/origem/confianca
      (R6.2, R6.3, R8.4).
    - Empate no topo com valores distintos / nenhuma fonte rastreável / mesmo
      provider afirmando valores distintos -> Arbitragem_Inconclusiva:
      `escalado_humano=True`, `valor=None`, `origem_id=None`, preservando os
      valores originais dos registros (R6.4, R6.5, R7.1, R7.2, R7.3, R7.4).

    Os lookups são injetáveis (default = implementação real via
    `tools.reliability`) para permitir testar a lógica com fakes, sem banco.

    _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 6.3, 7.4, 8.4, 8.5, 8.6_
    """
    if not registros:
        raise ValueError("subcluster vazio")

    if campo not in _campos_suportados():
        raise ValueError(f"Campo não suportado pela arbitragem por confiabilidade: {campo!r}")

    # R1.2: um único registro — não há conflito possível.
    if len(registros) == 1:
        registro = registros[0]
        return DecisaoCampo(
            campo=campo,
            valor=getattr(registro, campo),
            justificativa="Subcluster com um único registro.",
            fonte="sem_conflito",
            escalado_humano=False,
        )

    # R1.3 / R7.5: consenso — todos os registros têm o mesmo valor do campo.
    valores = [getattr(registro, campo) for registro in registros]
    if len(set(valores)) == 1:
        return DecisaoCampo(
            campo=campo,
            valor=valores[0],
            justificativa="Todos os registros concordam no valor.",
            fonte="sem_conflito",
            escalado_humano=False,
        )

    # Divergência real: ranqueia as fontes e tenta desempatar por confiabilidade.
    anotados = _ranquear_fontes(
        registros, campo, brand_id, buscar_fonte, calcular_nivel_confiabilidade
    )
    evidencias = _evidencias(anotados)
    resultado = _selecionar_vencedor(anotados, campo)

    if resultado.tipo == "vencedor":
        # R6.3 / R8.4: nomeia a fonte vencedora (provider/owner) e seu nível.
        fonte_vencedora = next(
            fonte for (registro, fonte, _) in anotados if registro.id == resultado.origem_id
        )
        if fonte_vencedora.provider_id is not None:
            descricao_fonte = f"provider_id={fonte_vencedora.provider_id}"
        else:
            descricao_fonte = f"owner_id={fonte_vencedora.owner_id}"
        return DecisaoCampo(
            campo=campo,
            valor=resultado.valor,
            justificativa=(
                f"Fonte de confiabilidade {resultado.nivel!r} ({descricao_fonte}) "
                f"decide sozinha o campo {campo!r}."
            ),
            fonte="regra_confiabilidade",
            escalado_humano=False,
            origem_id=resultado.origem_id,
            confianca=resultado.nivel,
            evidencias=evidencias,
        )

    # R7.4: Arbitragem_Inconclusiva — escala para intervenção humana sem valor
    # vencedor nem origem, preservando os valores originais dos registros (não os
    # tocamos: `valores`/`registros` seguem intactos).
    return DecisaoCampo(
        campo=campo,
        valor=None,
        justificativa=f"Arbitragem por confiabilidade inconclusiva: {resultado.motivo}.",
        fonte="escalado_humano",
        escalado_humano=True,
        origem_id=None,
        confianca=None,
        evidencias=evidencias,
    )
