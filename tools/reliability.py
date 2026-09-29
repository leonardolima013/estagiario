"""Lookup de confiabilidade por campo (SPEC.md §4.2, passo 1 da cascata de arbitragem).

`catalog_part` não guarda quem preencheu cada campo — isso vive em
`catalog_partactivity` (log de auditoria por (part_id, attribute)). A confiabilidade
em si não é um dado do banco: é uma regra de negócio (repassada pelo time de dados,
não documentada na SPEC original):

1. Provider da mesma manufacturer da marca da peça -> confiabilidade "alta".
2. Providers FRAGA e SUIV (por nome) -> "media", empatados entre si.
3. Qualquer outro provider -> "baixa".
4. Fallback pra owner (registros legados sem provider): mesma comparação de
   manufacturer, só "alta"/"baixa" (sem o nível intermediário FRAGA/SUIV, que é
   específico de provider).
5. Sem provider nem owner rastreável -> confiabilidade desconhecida (None).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from tools.db_query import consultar_banco

NivelConfiabilidade = Literal["alta", "media", "baixa", "minima"]

# Ordem total de confiabilidade, do mais confiável (maior peso) ao menos
# confiável (menor peso). `None` representa "desconhecido" e fica abaixo de
# tudo. Fonte única de verdade da ordem — usada aqui e por callers como
# `arbitration/campo_numerico.py`.
ORDEM_CONFIABILIDADE: dict[NivelConfiabilidade | None, int] = {
    "alta": 4,
    "media": 3,
    "baixa": 2,
    "minima": 1,
    None: 0,  # desconhecido
}

_PROVIDERS_NIVEL_MEDIO = frozenset({"FRAGA", "SUIV"})
_NOME_POSTGRES = "POSTGRES"

# Linhas de atividade aplicáveis ao par (part_id, campo): CRE do próprio
# part_id (fonte de todos os campos) e UPD cujo `attribute` é o campo (fonte só
# daquela coluna). Ordenadas por `created DESC` e SEM `LIMIT 1` — precisamos de
# todas as linhas do topo para detectar empate de timestamp (R4.4). A seleção da
# fonte a partir dessas linhas é feita por `resolver_fonte_de_linhas`.
_QUERY_FONTE_ATUAL = """
SELECT current_provider_id, current_owner_id, activity_type, attribute, created
FROM catalog_partactivity
WHERE part_id = %(part_id)s
  AND (activity_type = 'CRE' OR (activity_type = 'UPD' AND attribute = %(campo)s))
ORDER BY created DESC
"""

_QUERY_PROVIDER = """
SELECT name, manufacturer_id
FROM catalog_informationprovider
WHERE id = %(provider_id)s
"""

_QUERY_OWNER_MANUFACTURER = """
SELECT manufacturer_id
FROM catalog_partowner
WHERE id = %(owner_id)s
"""

_QUERY_BRAND_MANUFACTURER = """
SELECT manufacturer_id
FROM manufacturer_brand
WHERE id = %(brand_id)s
"""


@dataclass(frozen=True)
class FonteCampo:
    provider_id: int | None
    owner_id: int | None


@dataclass(frozen=True)
class InfoProvider:
    nome: str
    manufacturer_id: int | None


@dataclass(frozen=True)
class LinhaAtividade:
    """Linha de `catalog_partactivity` aplicável a um par (part_id, campo).

    Carrega as colunas necessárias para decidir a fonte do valor atual do campo
    até a decisão pura em `resolver_fonte_de_linhas` (I/O na borda, decisão pura
    no centro). `created` é um timestamp comparável (datetime).
    """

    current_provider_id: int | None
    current_owner_id: int | None
    activity_type: str
    attribute: str | None
    created: object


def resolver_fonte_de_linhas(linhas: list[LinhaAtividade]) -> FonteCampo:
    """R4.1–R4.5: seleciona a fonte do valor atual do campo a partir das linhas.

    Função PURA: opera só sobre a lista recebida (linhas CRE do part + UPD do
    campo, já ordenadas por `created DESC` pela `_QUERY_FONTE_ATUAL`), sem I/O.

    - Sem linhas aplicáveis -> sem fonte rastreável (None, None).           (R4.5)
    - A mais recente por `created` vence; como a query restringe a CRE do part
      e UPD do próprio campo e ordena por `created DESC`, o UPD do campo tem
      prioridade natural por ser mais recente que o CRE, senão cai no CRE.
                                                                     (R4.2, R4.3)
    - Empate de timestamp no topo (2+ linhas com o mesmo `created` máximo) ->
      sem fonte rastreável (None, None).                                  (R4.4)
    """
    if not linhas:
        return FonteCampo(provider_id=None, owner_id=None)
    created_max = linhas[0].created
    topo = [linha for linha in linhas if linha.created == created_max]
    if len(topo) != 1:
        return FonteCampo(provider_id=None, owner_id=None)
    linha = topo[0]
    return FonteCampo(provider_id=linha.current_provider_id, owner_id=linha.current_owner_id)


def buscar_fonte_atual(part_id: int, campo: str) -> FonteCampo:
    """Busca quem é a fonte (provider/owner) do valor atual de `campo` numa peça.

    I/O na borda, decisão pura no centro: monta as `LinhaAtividade` aplicáveis a
    partir de `consultar_banco` (linhas CRE do part + UPD do campo, já ordenadas
    por `created DESC` pela `_QUERY_FONTE_ATUAL`) e delega a escolha da fonte à
    função pura `resolver_fonte_de_linhas`.

    - Sem atividade rastreável para o campo → FonteCampo(None, None).       (R4.5)
    - Empate de timestamp no topo (2+ linhas com o mesmo `created` máximo) →
      FonteCampo(None, None).                                              (R4.4)
    """
    resultado = consultar_banco(_QUERY_FONTE_ATUAL, params={"part_id": part_id, "campo": campo})
    linhas = [
        LinhaAtividade(
            current_provider_id=row[0],
            current_owner_id=row[1],
            activity_type=row[2],
            attribute=row[3],
            created=row[4],
        )
        for row in resultado.rows
    ]
    return resolver_fonte_de_linhas(linhas)


def buscar_info_provider(provider_id: int) -> InfoProvider | None:
    resultado = consultar_banco(_QUERY_PROVIDER, params={"provider_id": provider_id})
    if not resultado.rows:
        return None
    nome, manufacturer_id = resultado.rows[0]
    return InfoProvider(nome=nome, manufacturer_id=manufacturer_id)


def buscar_manufacturer_do_owner(owner_id: int) -> int | None:
    resultado = consultar_banco(_QUERY_OWNER_MANUFACTURER, params={"owner_id": owner_id})
    if not resultado.rows:
        return None
    return resultado.rows[0][0]


def buscar_manufacturer_da_marca(brand_id: int) -> int | None:
    resultado = consultar_banco(_QUERY_BRAND_MANUFACTURER, params={"brand_id": brand_id})
    if not resultado.rows:
        return None
    return resultado.rows[0][0]


def nivel_confiabilidade(
    fonte: FonteCampo,
    brand_id: int,
    buscar_info_provider=buscar_info_provider,
    buscar_manufacturer_do_owner=buscar_manufacturer_do_owner,
    buscar_manufacturer_da_marca=buscar_manufacturer_da_marca,
) -> NivelConfiabilidade | None:
    """Aplica a regra de confiabilidade do time de dados pra uma fonte de campo.

    Os lookups são parâmetros injetáveis (default = implementação real via
    consultar_banco) pra permitir testar a regra de negócio com fakes, sem banco.
    """
    if fonte.provider_id is None and fonte.owner_id is None:
        return None

    manufacturer_da_marca = buscar_manufacturer_da_marca(brand_id)

    if fonte.provider_id is not None:
        info = buscar_info_provider(fonte.provider_id)
        if info is not None:
            if manufacturer_da_marca is not None and info.manufacturer_id == manufacturer_da_marca:
                return "alta"
            nome_norm = info.nome.strip().upper()
            if nome_norm in _PROVIDERS_NIVEL_MEDIO:
                return "media"
            if nome_norm == _NOME_POSTGRES:
                return "minima"
            return "baixa"

    if fonte.owner_id is not None:
        # R5.1/R5.5: chegamos aqui quando não há provider ou o provider não
        # resolveu (info is None e o bloco acima não retornou).
        manufacturer_do_owner = buscar_manufacturer_do_owner(fonte.owner_id)
        # R5.6: owner inexistente ou com `manufacturer_id` ausente
        # (`buscar_manufacturer_do_owner` → None em ambos os casos), ou
        # manufacturer da marca não resolvível → desconhecido (None). Só
        # classificamos alta/baixa quando ambos os manufacturers são
        # conhecidos; sem isso não há como comparar sem palpite.
        if manufacturer_do_owner is not None and manufacturer_da_marca is not None:
            if manufacturer_do_owner == manufacturer_da_marca:
                return "alta"  # R5.2: owner é a própria marca
            return "baixa"  # R5.3: owner de outra manufacturer

    return None
