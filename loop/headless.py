"""Execução headless (sem TUI) do loop de famílias duplicadas.

Para execuções não supervisionadas. Sempre não interativa: nenhum caso abre a
modal de intervenção; o que precisaria de um humano fica escalado e vai para
`output/escalados_humano/escalados_<run_id>.json`. Todas as opções de pesquisa
ficam ligadas: pesquisa web pelo Serper, coleta de HTML e fallback stealth (o
stealth abre o Chrome real; sem DISPLAY/WAYLAND_DISPLAY a coleta segue só com
urllib e registra `ambiente_sem_display`).

Uso:
    venv/bin/python -m loop.headless --iteracoes 50
    venv/bin/python -m loop.headless --status-fila
    venv/bin/python -m loop.headless --atualizar-fila
    venv/bin/python -m loop.headless --reprocessar-erros --iteracoes 20
    venv/bin/python -m loop.headless --recuperar-json output/.loop_<run_id>.jsonl

Recomendado rodar dentro de um limite de memória, para o processo morrer em vez
de travar a máquina. `scripts/headless.sh` faz isso (padrão 4G, sem swap;
ESTAGIARIO_HEADLESS_MEMORIA muda o limite) e repassa os argumentos:
    scripts/headless.sh --iteracoes 50

Ctrl+C (ou SIGTERM) pede cancelamento cooperativo: a família em andamento
termina e os arquivos são gerados. Um segundo Ctrl+C encerra na hora.

Códigos de saída: 0 concluído ou fila esgotada; 1 terminou com erros;
2 erro fatal (fila indisponível, configuração); 130 cancelado.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from threading import Event
from time import monotonic

from config import PROJECT_ROOT
from loop.models import LoopConfig, LoopProgresso, LoopStatus, MotivoParada, ResultadoLoop

SAIDA_OK = 0
SAIDA_COM_ERROS = 1
SAIDA_FATAL = 2
SAIDA_CANCELADO = 130


def _escrever(texto: str) -> None:
    print(texto, flush=True)


def _relativo(caminho: Path | None) -> str:
    if caminho is None:
        return "—"
    try:
        return str(Path(caminho).resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(caminho)


def _status_fila(fila) -> str:
    contagem = fila.contagem()
    meta = fila.metadados()
    partes = [f"{status}={n}" for status, n in contagem.items()]
    montada = meta.get("montada_em", "nunca")
    return f"fila {_relativo(fila.caminho)}: " + " · ".join(partes) + f" (montada em {montada})"


def codigo_saida(resultado: ResultadoLoop) -> int:
    if resultado.motivo_parada == MotivoParada.ERRO_FATAL:
        return SAIDA_FATAL
    if resultado.status == LoopStatus.CANCELADO:
        return SAIDA_CANCELADO
    if resultado.status == LoopStatus.FINALIZADO_COM_ERROS:
        return SAIDA_COM_ERROS
    return SAIDA_OK


class _Progresso:
    """Uma linha por família no stdout."""

    def __init__(self) -> None:
        self._inicio = monotonic()
        self._erros = 0

    def __call__(self, p: LoopProgresso) -> None:
        if p.fase == "preparando":
            _escrever("Preparando a fila de famílias (na primeira vez, monta a partir do Postgres)...")
        elif p.fase == "checkpoint":
            resultado = "erro" if p.erros > self._erros else "ok"
            self._erros = p.erros
            familia = f"{p.familia.search_ref}/{p.familia.brand or p.familia.brand_id}" if p.familia else "—"
            _escrever(
                f"[{p.indice_atual}/{p.total}] {familia}: {resultado} · "
                f"concl {p.concluidas} · erros {p.erros} · sinal {p.sinalizadas} · "
                f"{monotonic() - self._inicio:.0f}s"
            )
        elif p.fase == "erro":
            self._erros = p.erros
            _escrever(f"[{p.indice_atual}/{p.total}] {p.mensagem}")


def _instalar_sinais(cancel_event: Event) -> dict:
    """Ctrl+C/SIGTERM viram cancelamento cooperativo; devolve os tratadores anteriores."""
    def tratar(signum, _frame) -> None:
        if cancel_event.is_set():
            raise KeyboardInterrupt
        cancel_event.set()
        _escrever(
            "Cancelamento pedido: a família em andamento termina e os arquivos são gerados. "
            "Ctrl+C de novo encerra na hora."
        )

    anteriores = {}
    for sinal in (signal.SIGINT, signal.SIGTERM):
        anteriores[sinal] = signal.signal(sinal, tratar)
    return anteriores


def main(argv: list[str] | None = None, *, executar_loop_fn=None, fila=None, criar_llm=None,
         introspeccao_fk_fn=None, criar_rule_store=None, criar_verificador=None) -> int:
    """Ponto de entrada. As dependências são injetáveis para teste."""
    parser = argparse.ArgumentParser(prog="python -m loop.headless", description=__doc__.split("\n\n")[0])
    parser.add_argument("--iteracoes", type=int, help="quantas famílias processar (inteiro positivo)")
    parser.add_argument("--output-dir", type=Path, default=PROJECT_ROOT / "output")
    parser.add_argument("--status-fila", action="store_true", help="mostra a contagem da fila e sai")
    parser.add_argument("--atualizar-fila", action="store_true",
                        help="relê do Postgres as famílias duplicadas antes de rodar (ou só isso, sem --iteracoes)")
    parser.add_argument("--reprocessar-erros", action="store_true",
                        help="devolve à fila as famílias que terminaram em erro")
    parser.add_argument("--recuperar-json", type=Path, metavar="JOURNAL",
                        help="gera o JSON de auditoria a partir do journal de uma execução que morreu")
    parser.add_argument("--verbose", action="store_true", help="mostra os avisos da pesquisa web no stderr")
    args = parser.parse_args(argv)

    if args.recuperar_json is not None:
        from loop.serializacao import recuperar_json_de_journal

        try:
            caminho = recuperar_json_de_journal(args.recuperar_json)
        except (OSError, ValueError) as exc:
            _escrever(f"Não foi possível recuperar: {exc}")
            return SAIDA_FATAL
        _escrever(f"JSON gerado: {_relativo(caminho)}")
        return SAIDA_OK

    if fila is None:
        from loop.fila import FilaFamilias

        fila = FilaFamilias()

    try:
        if args.atualizar_fila:
            _escrever("Atualizando a fila a partir do Postgres...")
            total = fila.montar()
            _escrever(f"{total} famílias duplicadas lidas.")
        if args.reprocessar_erros:
            _escrever(f"{fila.reabrir_erros()} família(s) com erro devolvida(s) à fila.")
        if args.status_fila:
            _escrever(_status_fila(fila))
            return SAIDA_OK
    except Exception as exc:  # noqa: BLE001 — falha de banco ou de arquivo
        _escrever(f"Erro na fila: {type(exc).__name__}: {exc}")
        return SAIDA_FATAL

    if args.iteracoes is None:
        if args.atualizar_fila or args.reprocessar_erros:
            _escrever(_status_fila(fila))
            return SAIDA_OK
        parser.error("informe --iteracoes N (ou --status-fila, --atualizar-fila, --recuperar-json)")
    try:
        config_loop = LoopConfig(iteracoes=args.iteracoes, output_dir=args.output_dir)
    except ValueError as exc:
        parser.error(str(exc))

    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        _escrever("Aviso: sem DISPLAY/WAYLAND_DISPLAY; o fallback stealth não abre o Chrome e a coleta usa só urllib.")

    try:
        if executar_loop_fn is None:
            from loop.executor import executar_loop as executar_loop_fn
        if criar_llm is None:
            from llm.anthropic_provider import AnthropicProvider as criar_llm
        if introspeccao_fk_fn is None:
            from tools.fk_introspection import introspeccao_fk as introspeccao_fk_fn
        if criar_rule_store is None:
            from config import rule_store_db_path
            from db.rule_store import RuleStore

            def criar_rule_store():
                return RuleStore(rule_store_db_path())
        if criar_verificador is None:
            from verification.selector import resolver_verificacao_web

            def criar_verificador():
                # Headless usa sempre o Serper, qualquer que seja o método do .env.
                return resolver_verificacao_web("serper")

        llm = criar_llm()
        dependencias_fk = introspeccao_fk_fn("catalog_part", "id")
        rule_store = criar_rule_store()
        verificador = criar_verificador()
    except Exception as exc:  # noqa: BLE001 — configuração ou banco indisponível
        _escrever(f"Erro ao preparar a execução: {type(exc).__name__}: {exc}")
        return SAIDA_FATAL

    cancel_event = Event()
    anteriores = _instalar_sinais(cancel_event)
    on_aviso = (lambda m: print(m, file=sys.stderr, flush=True)) if args.verbose else None

    _escrever(
        f"Loop headless: {args.iteracoes} família(s), não interativo, pesquisa web=serper, "
        "coleta de HTML=ligada, fallback stealth=ligado."
    )
    try:
        resultado = executar_loop_fn(
            config_loop, llm, dependencias_fk,
            rule_store=rule_store,
            fonte_familias=fila,
            on_progresso=_Progresso(),
            on_aviso=on_aviso,
            pedir_intervencao=None,
            cancel_event=cancel_event,
            pesquisa_web=True,
            coleta_html=True,
            fallback_stealth=True,
            verificar_web=verificador,
        )
    finally:
        for sinal, tratador in anteriores.items():
            signal.signal(sinal, tratador)

    _escrever(
        f"Fim: {resultado.status.value} ({resultado.motivo_parada.value}) · "
        f"executadas {resultado.iteracoes_executadas} · concluídas {resultado.iteracoes_concluidas} · "
        f"erros {resultado.iteracoes_com_erro}"
    )
    _escrever(f"JSON: {_relativo(resultado.caminho_json)}")
    _escrever(f"SQL:  {_relativo(resultado.caminho_sql)} (nunca executado)")
    if resultado.caminho_escalados is not None:
        _escrever(f"Revisão humana: {resultado.casos_escalados} caso(s) em {_relativo(resultado.caminho_escalados)}")
    else:
        _escrever("Revisão humana: nenhum caso escalado.")
    try:
        _escrever(_status_fila(fila))
    except Exception:  # noqa: BLE001 — só informativo
        pass
    return codigo_saida(resultado)


if __name__ == "__main__":
    sys.exit(main())
