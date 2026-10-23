"""Harness de medição de memória do loop do Estagiário (não altera o repo).

Modos:
  --modo fake : executor/writer/trace/pipeline reais; buscar_grupo real (Postgres
                local, 1 query/grupo); LLM, verificador web e coleta falsos;
                lookups de confiabilidade devolvem vazio (contados, não executados);
                sorteio a partir de uma lista pré-carregada (1 query no total).
  --modo real : executar_loop com tudo real (sorteio, LLM, Serper, coleta urllib).
  --tui       : roda pela EstagiarioApp headless (tela "Rodar loop"), como o operador.

Saída: CSV de amostras por iteração + JSON com tracemalloc/gc/contadores.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import functools
import gc
import json
import os
import sys
import time
import tracemalloc
from collections import Counter
from pathlib import Path

REPO = Path("/home/leonardo/hubbi/data-ops-estagiario/estagiario")
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import config  # noqa: E402
import loop.executor as executor_mod  # noqa: E402
import loop.serializacao as serializacao_mod  # noqa: E402
import loop.sql_saida as sql_saida_mod  # noqa: E402
import tools.db_query as db_query_mod  # noqa: E402
from loop.models import LoopConfig, LoopProgresso  # noqa: E402
from tools.fk_introspection import introspeccao_fk  # noqa: E402

AQUI = Path(__file__).resolve().parent


# --------------------------------------------------------------------------- medição

def ler_status() -> dict[str, int]:
    out = {}
    with open("/proc/self/status") as f:
        for linha in f:
            if linha.startswith(("VmRSS", "VmHWM", "VmSwap", "Threads")):
                k, v = linha.split(":", 1)
                out[k] = int(v.split()[0])
    return out


def rss_descendentes() -> int:
    """RSS (kB) somado dos processos filhos/netos (Chrome, node...)."""
    meu = os.getpid()
    filhos: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
            filhos.setdefault(ppid, []).append(int(d))
        except OSError:
            pass
    total, pilha = 0, list(filhos.get(meu, []))
    while pilha:
        p = pilha.pop()
        pilha.extend(filhos.get(p, []))
        try:
            with open(f"/proc/{p}/status") as f:
                for linha in f:
                    if linha.startswith("VmRSS"):
                        total += int(linha.split()[1])
        except OSError:
            pass
    return total


CONTADORES: Counter = Counter()
TEMPOS: Counter = Counter()


def cronometrar(nome, fn):
    @functools.wraps(fn)
    def _w(*a, **k):
        t = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            TEMPOS[nome] += time.perf_counter() - t
            CONTADORES[nome] += 1
    return _w


USAGE: dict = {}


def instrumentar_anthropic():
    """Soma `usage` por (modelo, tool forçada) em toda chamada messages.create."""
    from anthropic.resources.messages.messages import Messages
    real = Messages.create

    @functools.wraps(real)
    def create(self, *a, **k):
        t = time.perf_counter()
        resp = real(self, *a, **k)
        dt = time.perf_counter() - t
        tool = (k.get("tool_choice") or {}).get("name") or "texto/agente"
        chave = f"{k.get('model')}|{tool}"
        u = getattr(resp, "usage", None)
        d = USAGE.setdefault(chave, {"chamadas": 0, "input": 0, "output": 0, "cache_read": 0,
                                     "cache_write": 0, "segundos": 0.0, "max_input": 0})
        d["chamadas"] += 1
        d["segundos"] += dt
        if u is not None:
            d["input"] += u.input_tokens or 0
            d["output"] += u.output_tokens or 0
            d["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
            d["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0
            d["max_input"] = max(d["max_input"], u.input_tokens or 0)
        return resp

    Messages.create = create


def contar_fds() -> dict:
    tipos = Counter()
    for fd in os.listdir("/proc/self/fd"):
        try:
            alvo = os.readlink(f"/proc/self/fd/{fd}")
        except OSError:
            continue
        tipos["socket" if alvo.startswith("socket:") else ("arquivo" if alvo.startswith("/") else "outro")] += 1
    return dict(tipos)


def instrumentar_comum():
    # Custo do checkpoint JSON e do SQL acumulado (suspeitos de O(n) por iteração).
    w = serializacao_mod.LoopOutputWriter
    w.checkpoint = cronometrar("writer.checkpoint", w.checkpoint)
    if hasattr(executor_mod, "gerar_sql_loop"):  # executor antigo: SQL remontado a cada iteração
        executor_mod.gerar_sql_loop = cronometrar("gerar_sql_loop", executor_mod.gerar_sql_loop)
        executor_mod.salvar_sql_atomico = cronometrar("salvar_sql_atomico", executor_mod.salvar_sql_atomico)
    else:  # executor atual: SQL em append
        e = sql_saida_mod.EscritorSqlLoop
        e.registrar = cronometrar("gerar_sql_loop", e.registrar)
    w.registrar_iteracao = cronometrar("writer.registrar_iteracao", w.registrar_iteracao)
    # Toda conexão Postgres passa por consultar_banco: conta por módulo chamador.
    real = db_query_mod.consultar_banco

    def contar(modulo):
        def _c(*a, **k):
            CONTADORES[f"pg_conexao.{modulo}"] += 1
            t = time.perf_counter()
            try:
                return real(*a, **k)
            finally:
                TEMPOS[f"pg_conexao.{modulo}"] += time.perf_counter() - t
        return _c

    import tools.group_fetch, tools.sortear_grupo, tools.reliability  # noqa: E401
    for m in (tools.group_fetch, tools.sortear_grupo, tools.reliability):
        m.consultar_banco = contar(m.__name__)


# --------------------------------------------------------------------------- fakes

JUST = ("Os registros compartilham código, marca e medidas compatíveis; a diferença "
        "está só na redação do nome, sem evidência técnica de peça distinta. ") * 3


class LLMFalso:
    """Responde por schema_name, com payloads do tamanho dos reais."""

    def __init__(self):
        self.ids_atuais: list[int] = []

    def _resposta(self, schema_name: str) -> dict:
        ids = self.ids_atuais or [0]
        CONTADORES[f"llm.{schema_name}"] += 1
        if schema_name == "particao":
            return {"subclusters": [{"label": "duplicata_real", "membro_ids": list(ids),
                                     "justificativa": JUST}]}
        if schema_name == "decisao_nome":
            return {"modo": "escolher", "part_id_escolhido": ids[0], "justificativa": JUST}
        if schema_name == "decisao_campo":
            return {"part_id_escolhido": ids[0], "justificativa": JUST, "confianca": "alta"}
        if schema_name == "decisao_similarity_id":
            return {"acao": "manter_sinalizado", "confianca": "baixa", "justificativa": JUST}
        return {"justificativa": JUST}

    def gerar_json(self, system, user, json_schema, schema_name="output"):
        CONTADORES["llm.prompt_chars"] += len(system) + len(user)
        return self._resposta(schema_name)

    def gerar_json_transmitindo(self, system, user, json_schema, schema_name, ao_atualizar=None):
        r = self.gerar_json(system, user, json_schema, schema_name)
        j = r.get("justificativa", "")
        if ao_atualizar:
            for i in range(1, 11):
                parcial = dict(r)
                parcial["justificativa"] = j[: len(j) * i // 10]
                ao_atualizar(parcial)
        return r


def verificador_falso(codigo, marca, nomes, *, on_evento=None):
    from verification.models import ResultadoVerificacao
    CONTADORES["verificador_web"] += 1
    if on_evento:
        for i in range(7):  # ~7 avisos/família no journal real de 14/10
            on_evento(f"Serper resultado orgânico: " + json.dumps(
                {"posicao": i, "titulo": f"{codigo} {marca} {nomes[0]}", "snippet": JUST[:200],
                 "link": f"https://exemplo.com.br/{codigo}/{i}"}, ensure_ascii=False))
    from verification.models import FonteWeb
    return ResultadoVerificacao(status="confirmado", nome_sugerido=nomes[0], justificativa=JUST,
                                fontes=[FonteWeb(url="https://exemplo.com.br/a", nome_encontrado=nomes[0]),
                                        FonteWeb(url="https://exemplo.com.br/b", nome_encontrado=nomes[0])])


def preparar_fakes(llm: LLMFalso, familias: list[dict]):
    import pipeline
    import tools.group_fetch as gf
    import tools.reliability as rel

    # Lookups de confiabilidade: contados, não executados (evita dezenas de
    # conexões por grupo no banco local; volume é reportado à parte).
    from tools.db_query import QueryResult

    def rel_vazio(query, params=None, **k):
        CONTADORES["pg_conexao.tools.reliability(simulada)"] += 1
        return QueryResult(columns=[], rows=[], truncado=False)

    rel.consultar_banco = rel_vazio

    buscar_real = gf.buscar_grupo

    def buscar(search_ref, brand_id):
        grupo = buscar_real(search_ref, brand_id)
        llm.ids_atuais = [r.id for r in grupo]
        return grupo

    def executar_caso(search_ref, brand_id, llm_, deps, **kw):
        kw["buscar_grupo"] = buscar
        kw.setdefault("verificar_web", verificador_falso)
        kw["coleta_html"] = False
        return pipeline.executar_caso(search_ref, brand_id, llm_, deps, **kw)

    pendentes = list(familias)

    def sortear(*, excluir):
        CONTADORES["sortear(simulado)"] += 1
        CONTADORES["sortear.tamanho_excluir"] = len(excluir)
        while pendentes:
            f = pendentes.pop(0)
            if (f["search_ref"], f["brand_id"]) not in excluir:
                from loop.models import FamiliaSorteada
                return FamiliaSorteada(f["search_ref"], f["brand_id"], f["brand"])
        from tools.sortear_grupo import NenhumGrupoDuplicadoError
        raise NenhumGrupoDuplicadoError("lista esgotada")

    return sortear, executar_caso


def carregar_familias(n: int) -> list[dict]:
    cache = AQUI / "familias.json"
    if cache.exists():
        dados = json.loads(cache.read_text())
        if len(dados) >= n:
            return dados[:n]
    from tools.db_query import consultar_banco
    q = """
    SELECT cp.search_ref, cp.brand_id, mb.name AS brand
    FROM catalog_part cp JOIN manufacturer_brand mb ON mb.id = cp.brand_id
    WHERE cp.search_ref IS NOT NULL AND cp.search_ref <> '' AND cp.lifecycle_status = 'ACTIVE'
    GROUP BY cp.search_ref, cp.brand_id, mb.name
    HAVING COUNT(*) > 1 AND COUNT(*) <= 50
    ORDER BY md5(cp.search_ref || ':' || cp.brand_id)
    LIMIT %(n)s
    """
    r = consultar_banco(q, params={"n": max(n, 6000)}, timeout_seconds=300, max_rows=max(n, 6000))
    dados = [dict(zip(r.columns, row)) for row in r.rows]
    cache.write_text(json.dumps(dados))
    return dados[:n]


# --------------------------------------------------------------------------- execução

def top_tracemalloc(snap_ini, snap_fim, limite=20):
    filtros = [tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, "<frozen*")]
    a, b = snap_ini.filter_traces(filtros), snap_fim.filter_traces(filtros)
    out = []
    for st in b.compare_to(a, "lineno")[:limite]:
        fr = st.traceback[0]
        out.append({"arquivo": fr.filename.replace(str(REPO) + "/", ""), "linha": fr.lineno,
                    "delta_kb": round(st.size_diff / 1024, 1), "total_kb": round(st.size / 1024, 1),
                    "delta_blocos": st.count_diff})
    return out


def contar_tipos() -> Counter:
    return Counter(type(o).__name__ for o in gc.get_objects())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--modo", choices=["fake", "real"], default="fake")
    ap.add_argument("--tui", action="store_true")
    ap.add_argument("--rotulo", default=None)
    ap.add_argument("--snap-em", type=int, default=10)
    ap.add_argument("--gc-cada", type=int, default=50)
    ap.add_argument("--tracemalloc", action="store_true")
    ap.add_argument("--trim", action="store_true",
                    help="a cada amostra: gc.collect + malloc_trim e registra o RSS depois (separa fragmentacao de retencao)")
    ap.add_argument("--pular", type=int, default=0, help="modo real com --sorteio-lista: pula as N primeiras familias")
    ap.add_argument("--sorteio-lista", action="store_true",
                    help="modo real: sorteia da lista pre-carregada em vez da varredura completa a cada grupo")
    ap.add_argument("--sem-checkpoint", action="store_true",
                    help="journal JSONL por iteracao, JSON publico so no fim (isola memoria do custo O(n^2) de disco)")
    args = ap.parse_args()
    rotulo = args.rotulo or f"{args.modo}{'_tui' if args.tui else ''}_{args.n}"
    saida = REPO / "output" / "medicao_memoria" / rotulo
    saida.mkdir(parents=True, exist_ok=True)
    out_loop = saida / "output"

    instrumentar_comum()
    if args.sem_checkpoint:
        w = serializacao_mod.LoopOutputWriter
        def registrar_so_journal(self, resultado, iteracao):
            linha = json.dumps(serializacao_mod.iteracao_para_json(iteracao), ensure_ascii=False, separators=(",", ":"))
            with self._journal.open("a", encoding="utf-8") as arq:
                arq.write(linha + "\n")
            self._iteracoes_escritas += 1
        w.registrar_iteracao = registrar_so_journal
    if args.tracemalloc:
        tracemalloc.start(1)

    deps = introspeccao_fk("catalog_part", "id")
    if args.modo == "fake":
        llm = LLMFalso()
        sortear, executar_caso_fn = preparar_fakes(llm, carregar_familias(args.n))
        loop_fn = functools.partial(executor_mod.executar_loop, sortear_fn=sortear,
                                    executar_caso_fn=executar_caso_fn)
    else:
        instrumentar_anthropic()
        from llm.anthropic_provider import AnthropicProvider
        llm = AnthropicProvider()
        kw = {}
        if args.sorteio_lista:
            pendentes = list(carregar_familias(args.pular + args.n * 3))[args.pular:]

            def sortear_lista(*, excluir):
                from loop.models import FamiliaSorteada
                from tools.sortear_grupo import NenhumGrupoDuplicadoError
                CONTADORES["sortear(lista)"] += 1
                while pendentes:
                    f = pendentes.pop(0)
                    if (f["search_ref"], f["brand_id"]) not in excluir:
                        return FamiliaSorteada(f["search_ref"], f["brand_id"], f["brand"])
                raise NenhumGrupoDuplicadoError("lista esgotada")
            kw["sortear_fn"] = sortear_lista
        loop_fn = functools.partial(executor_mod.executar_loop, **kw)

    amostras: list[dict] = []
    t0 = time.perf_counter()
    estado = {"snap_ini": None, "tipos_ini": None, "tipos_por_iter": []}
    base = ler_status()

    def amostrar(p: LoopProgresso):
        if p.fase not in ("checkpoint", "erro"):
            return
        st = ler_status()
        linha = {"iteracao": p.indice_atual, "t_s": round(time.perf_counter() - t0, 2),
                 "rss_kb": st["VmRSS"], "hwm_kb": st["VmHWM"], "swap_kb": st.get("VmSwap", 0),
                 "threads": st["Threads"], "filhos_rss_kb": rss_descendentes(),
                 **{f"fd_{k}": v for k, v in contar_fds().items()},
                 "checkpoint_s_acum": round(TEMPOS["writer.checkpoint"], 3),
                 "gerar_sql_s_acum": round(TEMPOS["gerar_sql_loop"] + TEMPOS["salvar_sql_atomico"], 3)}
        try:
            linha["json_bytes"] = os.path.getsize(next(out_loop.glob("loop_*.json")))
            linha["sql_bytes"] = os.path.getsize(next(out_loop.glob("loop_*.sql")))
        except (StopIteration, OSError):
            pass
        if args.trim:
            import ctypes
            gc.collect()
            ctypes.CDLL("libc.so.6").malloc_trim(0)
            linha["rss_pos_trim_kb"] = ler_status()["VmRSS"]
        if args.gc_cada and p.indice_atual % args.gc_cada == 0:
            linha["gc_objetos"] = len(gc.get_objects())
        amostras.append(linha)
        if p.indice_atual == args.snap_em:
            gc.collect()
            estado["tipos_ini"] = contar_tipos()
            if args.tracemalloc:
                estado["snap_ini"] = tracemalloc.take_snapshot()
        print(f"[{rotulo}] it={p.indice_atual} rss={st['VmRSS']//1024}MB t={linha['t_s']}s", flush=True)

    if args.tui:
        resultado = rodar_tui(loop_fn, llm, deps, args.n, out_loop, amostrar)
    else:
        resultado = loop_fn(LoopConfig(iteracoes=args.n, output_dir=out_loop), llm, deps,
                            on_progresso=amostrar)

    gc.collect()
    fim = {"status": getattr(resultado, "status", None) and resultado.status.value,
           "iteracoes_vivas_em_resultado": len(getattr(resultado, "iteracoes", [])) if resultado else None}
    if resultado is not None:
        fim["sql_total_chars_vivo"] = sum(len(i.sql or "") for i in resultado.iteracoes)
        fim["eventos_vivos"] = sum(len(i.eventos) for i in resultado.iteracoes)
    relatorio = {"rotulo": rotulo, "n": args.n, "modo": args.modo, "tui": args.tui,
                 "base_status_kb": base, "fim_status_kb": ler_status(), "duracao_s": time.perf_counter() - t0,
                 "contadores": dict(CONTADORES), "tempos_s": {k: round(v, 3) for k, v in TEMPOS.items()},
                 "usage_anthropic": USAGE,
                 **fim}
    if estado["tipos_ini"] is not None:
        tipos_fim = contar_tipos()
        delta = tipos_fim.copy()
        delta.subtract(estado["tipos_ini"])
        relatorio["gc_delta_tipos_top"] = delta.most_common(25)
    if args.tracemalloc and estado["snap_ini"] is not None:
        relatorio["tracemalloc_top"] = top_tracemalloc(estado["snap_ini"], tracemalloc.take_snapshot())
        relatorio["tracemalloc_atual_pico_kb"] = [x // 1024 for x in tracemalloc.get_traced_memory()]
    (saida / "relatorio.json").write_text(json.dumps(relatorio, indent=2, ensure_ascii=False, default=str))
    with (saida / "amostras.csv").open("w", newline="") as f:
        campos = sorted({k for a in amostras for k in a})
        w = csv.DictWriter(f, fieldnames=campos)
        w.writeheader()
        w.writerows(amostras)
    print(json.dumps({k: relatorio[k] for k in ("rotulo", "fim_status_kb", "duracao_s")}, default=str))


def rodar_tui(loop_fn, llm, deps, n, out_loop, amostrar):
    from textual.widgets import Button, Input

    import tui.screens.rodar_loop_screen as tela
    from tui.app import EstagiarioApp

    def sem_modal(*a, **k):
        CONTADORES["intervencao_evitada"] += 1
        raise RuntimeError("intervencao simulada (harness)")
    tela.pedir_intervencao_bloqueante = sem_modal

    capturado = {}

    def loop_com_amostra(config_, llm_, deps_, **kw):
        prog = kw.get("on_progresso")

        def ambos(p):
            amostrar(p)
            if prog:
                prog(p)
        kw["on_progresso"] = ambos
        capturado["r"] = loop_fn(config_, llm_, deps_, **kw)
        return capturado["r"]

    app = EstagiarioApp(llm=llm, dependencias_fk=deps, executar_loop_fn=loop_com_amostra,
                        output_dir=out_loop)

    async def dirigir():
        async with app.run_test(size=(120, 40)) as piloto:
            await piloto.click("#btn-rodar-loop")
            await piloto.pause()
            app.query_one("#input-iteracoes", Input).value = str(n)
            app.query_one("#btn-iniciar-loop", Button).press()
            await piloto.pause()
            while "r" not in capturado or not app.query_one("#btn-iniciar-loop", Button).disabled is False:
                await asyncio.sleep(0.5)
            await piloto.pause(1.0)
            st = ler_status()
            print(f"[tui] fim rss={st['VmRSS']//1024}MB", flush=True)

    asyncio.run(dirigir())
    return capturado.get("r")


if __name__ == "__main__":
    main()
