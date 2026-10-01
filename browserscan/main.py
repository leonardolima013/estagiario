import json
import urllib.request
from pathlib import Path

# patchright é um drop-in do Playwright que remove os vazamentos de automação (CDP Runtime.enable, navigator.webdriver, etc.)
from patchright.sync_api import BrowserContext, Playwright, sync_playwright

URL = "https://www.browserscan.net/bot-detection"
# Perfil persistente em disco: evita o modo anônimo que o new_context() do Playwright usa
PROFILE_DIR = Path(__file__).parent / "chrome-profile"
FALLBACK_TIMEZONE = "America/Sao_Paulo"


def get_ip_timezone() -> str:
    """Retorna o fuso horário (IANA) geolocalizado do IP público, para bater com o que os sites veem."""
    try:
        with urllib.request.urlopen("http://ip-api.com/json/?fields=timezone", timeout=5) as resp:
            return json.load(resp)["timezone"]
    except Exception as exc:
        print(f"Não consegui descobrir o fuso do IP ({exc}); usando {FALLBACK_TIMEZONE}")
        return FALLBACK_TIMEZONE


def set_webrtc_policy(policy: str) -> None:
    """Grava a política de IP do WebRTC nas preferências do perfil (o flag de linha de comando é ignorado pelo Chrome)."""
    prefs_file = PROFILE_DIR / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True, exist_ok=True)
    prefs = json.loads(prefs_file.read_text()) if prefs_file.exists() else {}
    prefs.setdefault("webrtc", {})["ip_handling_policy"] = policy
    prefs_file.write_text(json.dumps(prefs))


def launch_browser(p: Playwright) -> BrowserContext:
    timezone = get_ip_timezone()
    print(f"Fuso horário do IP: {timezone}")
    # Impede o WebRTC de expor um IP diferente do usado nas requisições HTTP
    set_webrtc_policy("disable_non_proxied_udp")
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",  # Google Chrome real em vez do Chromium de teste
        headless=False,
        no_viewport=True,  # usa o tamanho real da janela, sem emular viewport
        timezone_id=timezone,
    )


def main() -> None:
    with sync_playwright() as p:
        context = launch_browser(p)
        page = context.pages[0] if context.pages else context.new_page()

        page.goto(URL, wait_until="domcontentloaded")
        print(f"Página aberta: {page.title()}")

        # Mantém o navegador aberto até você pressionar Enter no terminal
        input("Pressione Enter para fechar o navegador...")
        context.close()


if __name__ == "__main__":
    main()
