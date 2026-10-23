"""Resume uma rodada do harness: memória, fds, tempo por etapa, coleta, erros e custo."""

import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

PRECO = {  # US$/MTok (Haiku 4.5): input, output, cache_read, cache_write
    "claude-haiku-4-5-20251001": (1.00, 5.00, 0.10, 1.25),
}

base = Path(sys.argv[1])
rel = json.loads((base / "relatorio.json").read_text())
amostras = list(csv.DictReader((base / "amostras.csv").open()))
loop_json = json.loads(next((base / "output").glob("loop_*.json")).read_text())
its = list(loop_json["iteracoes"].values())

print("== status", rel.get("status"), "| duração", round(rel["duracao_s"]), "s |", len(its), "iterações")
print("status:", Counter(i["status"] for i in its), "| erros:", Counter((i["erro"] or {}).get("tipo") for i in its if i["erro"]))

print("\n== memória/fds por iteração (amostras)")
for a in amostras[:: max(1, len(amostras) // 10)] + [amostras[-1]]:
    fds = {k: a[k] for k in a if k.startswith("fd_") and a[k]}
    print(f"it={a['iteracao']:>3} rss={int(a['rss_kb'])//1024}MB hwm={int(a['hwm_kb'])//1024}MB "
          f"filhos={int(a['filhos_rss_kb'])//1024}MB threads={a['threads']} t={a['t_s']}s {fds}")

print("\n== tempo por etapa (duracao_ms do trace)")
dur = defaultdict(list)
for i in its:
    for t in i["tools_utilizadas"]:
        if t.get("duracao_ms") is not None:
            dur[f"{t['fase']}/{t['tool']}"].append(t["duracao_ms"] / 1000)
for k, v in sorted(dur.items(), key=lambda kv: -sum(kv[1])):
    print(f"{k:45s} n={len(v):4d} total={sum(v):7.1f}s med={statistics.median(v):6.2f}s max={max(v):6.2f}s")

print("\n== coleta de páginas")
col = Counter()
for i in its:
    for t in i["tools_utilizadas"]:
        if t["tool"] == "coletar_paginas":
            r = t.get("resultado") or {}
            col[(t["status"], r.get("desfecho"), r.get("motivo"))] += 1
for k, v in col.most_common():
    print(v, k)

print("\n== LLM (usage do SDK)")
total = 0.0
for chave, d in sorted(rel.get("usage_anthropic", {}).items()):
    modelo = chave.split("|")[0]
    p = PRECO.get(modelo)
    custo = (d["input"] * p[0] + d["output"] * p[1] + d["cache_read"] * p[2] + d["cache_write"] * p[3]) / 1e6 if p else float("nan")
    total += custo if p else 0
    print(f"{chave:55s} chamadas={d['chamadas']:4d} in={d['input']:8d} out={d['output']:7d} "
          f"cache_r={d['cache_read']:6d} cache_w={d['cache_write']:6d} max_in={d['max_input']:6d} "
          f"{d['segundos']:6.1f}s US${custo:.4f}")
ok = sum(1 for i in its if i["status"] == "sucesso") or 1
print(f"custo total US${total:.4f} | por grupo concluído US${total/ok:.4f} | 144k grupos ~US${total/ok*144264:,.0f}")

print("\n== conexões Postgres", {k: v for k, v in rel["contadores"].items() if k.startswith("pg_")})
print("tempos", rel["tempos_s"])
print("\n== tracemalloc top")
for t in rel.get("tracemalloc_top", [])[:12]:
    print(t)
print("gc Δ", rel.get("gc_delta_tipos_top", [])[:12])
print("tamanho JSON/iter:", round(Path(next((base / "output").glob("loop_*.json"))).stat().st_size / len(its) / 1024), "KB")
