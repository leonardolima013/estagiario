"""Replay offline da coleta de páginas: páginas já guardadas em db/paginas.db
(lidas pela API pública do ArmazemPaginas) passam pelo ColetorPaginas real (classificação, seleção, decodificação com
teto, sha256, gzip, gravação SQLite) com um transporte falso. Nenhum site é
acessado e o armazém original só é lido. Mede RSS, heap Python e o efeito de
malloc_trim (fragmentação recuperável x retenção real).

uso: bench_coleta.py RODADAS
"""

import ctypes
import gc
import os
import sys
import time
import tracemalloc
from pathlib import Path

REPO = Path("/home/leonardo/hubbi/data-ops-estagiario/estagiario")
sys.path.insert(0, str(REPO))
os.chdir(REPO)

from coleta_paginas.coletor import ColetorPaginas  # noqa: E402
from coleta_paginas.modelos import PecaConsultada, ResultadoBusca  # noqa: E402
from db.armazem_paginas import ArmazemPaginas  # noqa: E402
from tools.buscador_paginas import RespostaHTTP  # noqa: E402

LIBC = ctypes.CDLL("libc.so.6")


def rss_mb() -> float:
    with open("/proc/self/status") as f:
        for linha in f:
            if linha.startswith("VmRSS"):
                return int(linha.split()[1]) / 1024
    return 0.0


def carregar_paginas():
    """Páginas já coletadas, lidas pela API pública do ArmazemPaginas (só leitura)."""
    armazem = ArmazemPaginas(REPO / "db" / "paginas.db")
    paginas = []
    for resumo in armazem.listar_pecas():
        if resumo.marca_peca is None:
            continue
        for reg in armazem.registros_da_peca(resumo.codigo_peca, resumo.marca_peca):
            if not reg.hash_conteudo:
                continue
            paginas.append((reg.codigo_peca, reg.marca_peca, reg.url_original, reg.dominio,
                            reg.content_type or "text/html; charset=utf-8",
                            armazem.ler_conteudo(reg.hash_conteudo)))
    return paginas


def main():
    rodadas = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    paginas = carregar_paginas()
    print(f"{len(paginas)} páginas, {sum(len(p[5]) for p in paginas) / 2**20:.1f} MB descomprimidos", flush=True)
    destino = REPO / "output" / "medicao_memoria" / "replay_coleta"
    destino.mkdir(parents=True, exist_ok=True)
    db = destino / "paginas_replay.db"
    for sufixo in ("", "-wal", "-shm"):
        Path(f"{db}{sufixo}").unlink(missing_ok=True)
    os.environ["ESTAGIARIO_PAGINAS_DB_PATH"] = str(db)

    corpo_atual = {}

    def transporte(url, cabecalhos, timeout):
        ct, corpo = corpo_atual["v"]
        blocos = [corpo[i:i + 65536] for i in range(0, len(corpo), 65536)]
        return RespostaHTTP(status=200, cabecalhos={"content-type": ct}, blocos=blocos)

    coletor = ColetorPaginas(ArmazemPaginas(db), transporte=transporte)
    gc.collect()
    tracemalloc.start()
    base_rss, n, desfechos = rss_mb(), 0, {}
    print(f"base rss={base_rss:.0f}MB", flush=True)
    t0 = time.perf_counter()
    for rodada in range(rodadas):
        for cod, marca, url, dom, ct, corpo in paginas:
            corpo_atual["v"] = (ct, corpo)
            sep = "&" if "?" in url else "?"
            res = ResultadoBusca(titulo=f"{cod} {marca}", snippet=f"Peça {cod} {marca}",
                                 url=f"{url}{sep}replay={rodada}", dominio=dom, posicao=1)
            rel = coletor.coletar(PecaConsultada(cod, marca, (f"PECA {cod}",)), [res], teto_aceitos=1)
            for e in rel.entradas:
                desfechos[e.desfecho] = desfechos.get(e.desfecho, 0) + 1
            n += 1
            if n % 100 == 0:
                atual, pico = tracemalloc.get_traced_memory()
                print(f"coletas={n:5d} rss={rss_mb():6.1f}MB heap_py={atual / 2**20:5.1f}MB "
                      f"pico_py={pico / 2**20:5.1f}MB t={time.perf_counter() - t0:5.1f}s", flush=True)
    gc.collect()
    antes = rss_mb()
    LIBC.malloc_trim(0)
    depois = rss_mb()
    print(f"desfechos={desfechos}")
    print(f"fim: rss={antes:.1f}MB → malloc_trim → {depois:.1f}MB (base {base_rss:.1f}MB); "
          f"heap_py={tracemalloc.get_traced_memory()[0] / 2**20:.1f}MB; db={db.stat().st_size / 2**20:.1f}MB")


if __name__ == "__main__":
    main()
