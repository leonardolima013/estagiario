#!/usr/bin/env bash
# Roda o loop headless (python -m loop.headless) dentro de um limite de memória,
# para o processo ser encerrado em vez de travar a máquina
# (docs/auditoria-memoria/RELATORIO.md). Os argumentos vão direto para o headless:
#
#   scripts/headless.sh --iteracoes 50
#   scripts/headless.sh --status-fila
#
# Limite: ESTAGIARIO_HEADLESS_MEMORIA (padrão 4G), sem swap.
set -euo pipefail

raiz="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
memoria="${ESTAGIARIO_HEADLESS_MEMORIA:-4G}"

if ! command -v systemd-run >/dev/null 2>&1; then
    echo "systemd-run não encontrado; rode sem limite: venv/bin/python -m loop.headless $*" >&2
    exit 2
fi

cd "$raiz"
cat <<'AJUDA'
Outros comandos:
  scripts/headless.sh --status-fila                       # quantas famílias pendentes, concluídas e com erro
  scripts/headless.sh --atualizar-fila                    # relê do Postgres as famílias duplicadas (mantém concluídas/erros, acrescenta novas)
  scripts/headless.sh --reprocessar-erros --iteracoes 20  # devolve à fila as famílias com erro e roda
  scripts/headless.sh --iteracoes 10 --verbose            # mostra os avisos da pesquisa web
  scripts/headless.sh --recuperar-json output/.loop_<run_id>.jsonl  # gera o JSON de uma execução que morreu
  ESTAGIARIO_HEADLESS_MEMORIA=2G scripts/headless.sh ...  # muda o limite de memória (padrão 4G)

AJUDA
echo "Limite de memória: ${memoria} (sem swap)"
# --scope executa o comando neste terminal: Ctrl+C e o código de saída
# continuam sendo os do headless.
exec systemd-run --user --scope --quiet \
    -p MemoryMax="${memoria}" -p MemorySwapMax=0 \
    "${raiz}/venv/bin/python" -m loop.headless "$@"
