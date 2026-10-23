"""RSS ao criar contextos SSL padrão em sequência (o que http.client faz por
conexão HTTPS quando urllib não recebe `context`). Sem rede."""
import ctypes, gc, ssl, sys

def rss():
    for l in open("/proc/self/status"):
        if l.startswith("VmRSS"):
            return int(l.split()[1]) / 1024

n = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
print(f"base rss={rss():.1f}MB")
for i in range(1, n + 1):
    ctx = ssl.create_default_context()
    del ctx
    if i % (n // 5) == 0:
        gc.collect(); print(f"contextos={i:5d} rss={rss():.1f}MB")
ctypes.CDLL("libc.so.6").malloc_trim(0)
print(f"após malloc_trim rss={rss():.1f}MB")
