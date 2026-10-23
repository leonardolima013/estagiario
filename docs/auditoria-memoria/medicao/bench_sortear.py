import sys, time, tracemalloc, resource
sys.path.insert(0, "/home/leonardo/hubbi/data-ops-estagiario/estagiario")
import tools.sortear_grupo as sg
from tools.db_query import _validar_select_unico
capt = {}
def falso(query, params=None, timeout_seconds=10, max_rows=500):
    capt["q"], capt["p"] = query, params
    t = time.perf_counter(); tracemalloc.start(); _validar_select_unico(query)
    capt["pico"] = tracemalloc.get_traced_memory()[1]; tracemalloc.stop(); capt["t"] = time.perf_counter() - t
    class R: rows = [("X", 1, "B")]
    return R()
sg.consultar_banco = falso
for n in [int(a) for a in sys.argv[1:]]:
    ex = {(f"REF{i:07d}", 1000 + i) for i in range(n)}
    sg.sortear_grupo_aleatorio(excluir=ex)
    print(f"excluir={n:>6} query_chars={len(capt['q']):>9} params={len(capt['p'] or {}):>6} "
          f"sqlparse_s={capt['t']:.2f} sqlparse_pico_MB={capt['pico']/2**20:.1f} maxrss_MB={resource.getrusage(resource.RUSAGE_SELF).ru_maxrss//1024}", flush=True)
