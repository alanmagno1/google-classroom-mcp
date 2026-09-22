"""Entrega por navegador.

Google no permite adjuntar ni entregar por API las tareas que el profesor creó desde la
web de Classroom (@ProjectPermissionDenied). Este módulo hace lo mismo que haría el
alumno en classroom.google.com, pero con Playwright manejando Google Chrome.

Hay un perfil de Chrome por cuenta (~/.config/google-classroom-mcp/browser-profiles/<alias>),
cada uno con una sola sesión de Google, iniciada una vez con
`google-classroom-mcp browser-login <alias>`. Así no dependemos de la multisesión de
Google, que al agregar una cuenta a veces reemplaza la otra.

Se ejecuta en un proceso aparte del servidor MCP (Playwright síncrono no puede correr
dentro de un bucle asyncio):

  python -m google_classroom_mcp.browser login ALIAS [--email CORREO] [--plain]
  python -m google_classroom_mcp.browser status
  python -m google_classroom_mcp.browser submit '{"alias": ..., "url": ..., "email": ..., "files": [...], "turn_in": true}'
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("GOOGLE_CLASSROOM_MCP_CONFIG_DIR", Path.home() / ".config" / "google-classroom-mcp")
)
PROFILES_DIR = Path(os.environ.get("GOOGLE_CLASSROOM_BROWSER_PROFILES", CONFIG_DIR / "browser-profiles"))
DOWNLOAD_DIR = Path(os.environ.get("GOOGLE_CLASSROOM_DOWNLOAD_DIR", Path.home() / "Downloads" / "google-classroom-mcp"))
SHOTS_DIR = DOWNLOAD_DIR / "_navegador"
HEADLESS = os.environ.get("GOOGLE_CLASSROOM_BROWSER_HEADLESS", "").lower() in ("1", "true", "yes")
CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CLASSROOM = "https://classroom.google.com"

# Textos de la interfaz de Classroom en español e inglés.
RX_ADD = r"Agregar o crear|Añadir o crear|Add or create"
RX_FILE_ITEM = r"^\s*(Archivo|File)\s*$"
RX_UPLOAD_TAB = r"^\s*(Subir|Upload)\s*$"
RX_BROWSE = r"Explorar|Browse|Seleccionar archivos|Select files|Elegir archivos|Choose files"
RX_TURN_IN = r"^\s*(Entregar|Turn in)\s*$"
RX_MARK_DONE = r"Marcar como (completad[ao]|hech[ao])|Mark as done"
RX_UNSUBMIT = r"Anular entrega|Unsubmit"
# El botón "X" de cada adjunto se llama "Eliminar a <archivo>" (en inglés "Remove <archivo>").
RX_REMOVE = r"^(Eliminar a|Quitar|Remove)\s"
RX_EMAIL = r"[\w.+-]+@[\w.-]+\.\w+"


class BrowserError(Exception):
    def __init__(self, msg: str, screenshot: str | None = None) -> None:
        super().__init__(msg)
        self.screenshot = screenshot


def profile_dir(alias: str) -> Path:
    alias = re.sub(r"[^A-Za-z0-9._@+-]+", "_", alias.strip())
    if not alias:
        raise BrowserError("Indica el alias de la cuenta (el mismo de `google-classroom-mcp accounts`).")
    return PROFILES_DIR / alias


def _chrome_binary() -> str:
    if Path(CHROME_MAC).exists():
        return CHROME_MAC
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise BrowserError("No encuentro Google Chrome instalado.")


def _profile_in_use(profile: Path) -> bool:
    """True si hay un Chrome abierto con ese perfil (Chrome no permite dos a la vez)."""
    try:
        r = subprocess.run(["pgrep", "-f", f"user-data-dir={profile}"], capture_output=True, text=True)
    except OSError:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


class ClassroomBrowser:
    """Chrome real (channel=chrome) sobre el perfil de una cuenta, manejado con Playwright."""

    def __init__(self, alias: str, headless: bool = HEADLESS, slow_mo: int = 0) -> None:
        self.alias = alias
        self.profile = profile_dir(alias)
        self.headless = headless
        self.slow_mo = slow_mo
        self._p = None
        self.ctx = None
        self.page = None

    def __enter__(self) -> ClassroomBrowser:
        from playwright.sync_api import sync_playwright

        if not self.profile.is_dir():
            raise BrowserError(
                f"La cuenta '{self.alias}' no tiene perfil de navegador. Corre `google-classroom-mcp browser-login {self.alias}`."
            )
        if _profile_in_use(self.profile):
            raise BrowserError(
                f"Hay una ventana de Chrome abierta con el perfil de '{self.alias}'. Ciérrala y reintenta."
            )
        self._p = sync_playwright().start()
        # Playwright arranca Chrome con --use-mock-keychain: las cookies quedan cifradas con
        # esa llave, así que el inicio de sesión también debe hacerse desde aquí (ver login).
        self.ctx = self._p.chromium.launch_persistent_context(
            str(self.profile),
            channel="chrome",
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
            viewport={"width": 1280, "height": 900},
            slow_mo=self.slow_mo,
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_default_timeout(30_000)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        finally:
            if self._p:
                self._p.stop()

    # --- utilidades --------------------------------------------------------
    def shot(self, tag: str) -> str | None:
        try:
            SHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = SHOTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{self.alias}-{tag}.png"
            self.page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception:
            return None

    def fail(self, msg: str, tag: str = "error") -> BrowserError:
        shot = self.shot(tag)
        return BrowserError(msg + (f" Captura: {shot}" if shot else ""), shot)

    def _settle(self, ms: int = 1500) -> None:
        self.page.wait_for_timeout(ms)

    # --- sesión ------------------------------------------------------------
    def session_email(self) -> str | None:
        """Correo de la sesión de Google del perfil, o None si no hay sesión."""
        self.page.goto(f"{CLASSROOM}/u/0/h", wait_until="domcontentloaded")
        try:
            self.page.locator("[aria-label*='@']").first.wait_for(timeout=12_000)
        except Exception:
            return None
        if not self.page.url.startswith(f"{CLASSROOM}/"):
            return None
        loc = self.page.locator("[aria-label*='@']")
        for k in range(min(loc.count(), 12)):
            m = re.search(RX_EMAIL, loc.nth(k).get_attribute("aria-label") or "")
            if m:
                return m.group(0).lower()
        return None

    def require_session(self, email: str) -> None:
        found = self.session_email()
        if not found:
            raise BrowserError(
                f"El perfil de '{self.alias}' no tiene sesión de Google. Corre `google-classroom-mcp browser-login {self.alias}`."
            )
        if found != email.lower():
            raise BrowserError(
                f"El perfil de '{self.alias}' tiene sesión de {found}, no de {email}. "
                f"Corre `google-classroom-mcp browser-login {self.alias} --email {email}`."
            )

    # --- entrega -----------------------------------------------------------
    def open_assignment(self, url: str) -> str:
        """Abre la tarea y espera el panel "Tu trabajo"."""
        target = re.sub(rf"^{re.escape(CLASSROOM)}/", f"{CLASSROOM}/u/0/", url)
        self.page.goto(target, wait_until="domcontentloaded")
        self._settle(2500)
        self.page.get_by_role(
            "button", name=re.compile(f"{RX_ADD}|{RX_TURN_IN}|{RX_MARK_DONE}|{RX_UNSUBMIT}", re.IGNORECASE)
        ).first.wait_for()
        return target

    def submit(self, url: str, email: str, files: list[str], turn_in: bool = True) -> dict:
        page = self.page
        steps: list[str] = []
        target = url
        try:
            self.require_session(email)
            target = self.open_assignment(url)
            if page.get_by_role("button", name=re.compile(RX_UNSUBMIT, re.IGNORECASE)).count():
                raise BrowserError("La tarea ya aparece entregada ('Anular entrega'). Anúlala en Classroom si quieres cambiarla.")
            for f in files:
                self._attach(Path(f))
                steps.append(f"adjuntado {Path(f).name}")
            if turn_in:
                self._turn_in(has_files=bool(files) or self._has_attachments())
                steps.append("entregado")
        except BrowserError:
            raise
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            raise self.fail(f"Fallo en el navegador tras {steps or 'abrir la tarea'}: {type(e).__name__}: {msg}") from e
        return {"url": target, "steps": steps, "screenshot": self.shot("final")}

    def _has_attachments(self) -> bool:
        # Con adjuntos el botón dice "Entregar"; sin ellos, "Marcar como completada".
        return bool(self.page.get_by_role("button", name=re.compile(RX_TURN_IN, re.IGNORECASE)).count())

    def _attach(self, path: Path) -> None:
        page = self.page
        page.get_by_role("button", name=re.compile(RX_ADD, re.IGNORECASE)).first.click()
        page.get_by_role("menuitem", name=re.compile(RX_FILE_ITEM, re.IGNORECASE)).first.click()

        # El selector de Google vive en un iframe (drive.google.com/picker) y abre en "Subir".
        picker = page.locator("iframe[src*='picker'], iframe.picker-frame").first
        picker.wait_for(state="visible", timeout=30_000)
        frame = page.frame_locator("iframe[src*='picker'], iframe.picker-frame").first
        browse = frame.get_by_role("button", name=re.compile(RX_BROWSE, re.IGNORECASE)).first
        try:
            browse.wait_for(state="visible", timeout=8_000)
        except Exception:
            frame.get_by_role("tab", name=re.compile(RX_UPLOAD_TAB, re.IGNORECASE)).first.click()
            browse.wait_for(state="visible", timeout=15_000)
        with page.expect_file_chooser(timeout=30_000) as fc:
            browse.click()
        fc.value.set_files(str(path))

        # Al terminar de subir, el selector se cierra y el archivo aparece en "Tu trabajo" con
        # su botón "Eliminar a <archivo>" (el chip muestra el nombre recortado, no sirve).
        picker.wait_for(state="hidden", timeout=300_000)
        self._remove_button(path.name).first.wait_for(timeout=180_000)
        self._settle()

    def _remove_button(self, name: str):
        rx = re.compile(rf"^(Eliminar a|Quitar|Remove)\s+{re.escape(name)}\s*$", re.IGNORECASE)
        return self.page.get_by_role("button", name=rx)

    def _turn_in(self, has_files: bool) -> None:
        page = self.page
        rx = RX_TURN_IN if has_files else RX_MARK_DONE
        btn = page.get_by_role("button", name=re.compile(rx, re.IGNORECASE)).first
        btn.wait_for(state="visible", timeout=30_000)
        page.wait_for_function(
            "b => !b.disabled && b.getAttribute('aria-disabled') !== 'true'", arg=btn.element_handle(), timeout=180_000
        )
        btn.click()
        dialog = page.get_by_role("dialog").last
        dialog.wait_for(state="visible", timeout=15_000)
        dialog.get_by_role("button", name=re.compile(f"{RX_TURN_IN}|{RX_MARK_DONE}", re.IGNORECASE)).last.click()
        page.get_by_role("button", name=re.compile(RX_UNSUBMIT, re.IGNORECASE)).first.wait_for(timeout=60_000)
        self._settle()

    def detach(self, name: str | None = None) -> int:
        """Quita adjuntos de una entrega no enviada: el de nombre `name`, o todos."""
        removed = 0
        while removed < 20:
            btn = self._remove_button(name) if name else self.page.get_by_role("button", name=re.compile(RX_REMOVE, re.IGNORECASE))
            if not btn.count():
                break
            btn.first.click()
            self._settle(1500)
            removed += 1
            if name:
                break
        return removed


# --------------------------------------------------------------------------- #
# Inicio de sesión y estado
# --------------------------------------------------------------------------- #
def login(alias: str, email: str | None = None, plain: bool = False) -> int:
    """Abre Chrome con el perfil de la cuenta para que el usuario inicie sesión.

    Por default lo abre con Playwright (el mismo Chrome que luego entrega). En macOS
    Playwright usa un llavero simulado para cifrar cookies; si el usuario iniciara sesión
    en un Chrome normal sobre el mismo perfil, esas cookies quedarían cifradas con otra
    llave y el Chrome automatizado no podría leerlas (y las borra). Con --plain se abre
    Chrome sin automatizar pero con ese mismo llavero simulado, por si Google bloquea el
    inicio de sesión en el navegador controlado."""
    profile = profile_dir(alias)
    profile.mkdir(parents=True, exist_ok=True)
    start = (
        f"https://accounts.google.com/AddSession?Email={email}&continue={CLASSROOM}/" if email else f"{CLASSROOM}/"
    )
    who = f" con {email}" if email else ""
    if plain:
        subprocess.Popen(
            [
                _chrome_binary(),
                f"--user-data-dir={profile}",
                "--use-mock-keychain",
                "--password-store=basic",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                start,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(
            f"Se abrió una ventana de Chrome aparte para la cuenta '{alias}' (perfil: {profile}).\n"
            f"Inicia sesión{who} hasta ver Classroom y cierra esa instancia con Cmd+Q (tu Chrome de siempre sigue).\n"
            "Después comprueba con:  google-classroom-mcp browser-status"
        )
        return 0

    print(f"Se va a abrir una ventana de Chrome controlada por Playwright para la cuenta '{alias}' (perfil: {profile}).")
    print(f"Inicia sesión{who} hasta ver Classroom y cierra la ventana. Solo esa cuenta en este perfil.")
    print("Esperando a que cierres la ventana...", flush=True)
    with ClassroomBrowser(alias, headless=False) as b:
        b.page.goto(start, wait_until="domcontentloaded")
        deadline = time.time() + 20 * 60
        while time.time() < deadline:
            try:
                if not b.ctx.pages:
                    break
                b.ctx.pages[0].wait_for_timeout(1500)
            except Exception:
                break
    print("Ventana cerrada. Revisando la sesión...", flush=True)
    with ClassroomBrowser(alias, headless=True) as b:
        found = b.session_email()
    if not found:
        print(f"El perfil de '{alias}' sigue sin sesión de Google. Vuelve a intentar (o usa --plain).")
        return 1
    if email and found != email.lower():
        print(f"El perfil de '{alias}' quedó con sesión de {found}, no de {email}. Vuelve a correr browser-login {alias}.")
        return 1
    print(f"  {alias}\t{found}")
    return 0


def status() -> int:
    if not PROFILES_DIR.is_dir() or not any(PROFILES_DIR.iterdir()):
        print("No hay perfiles de navegador. Corre `google-classroom-mcp browser-login <alias>`.")
        return 1
    ok = True
    for profile in sorted(p for p in PROFILES_DIR.iterdir() if p.is_dir()):
        try:
            with ClassroomBrowser(profile.name, headless=True) as b:
                found = b.session_email()
        except BrowserError as e:
            found = None
            print(f"{profile.name}\t(error: {e})")
            ok = False
            continue
        print(f"{profile.name}\t{found or '(sin sesión)'}")
        ok = ok and bool(found)
    return 0 if ok else 1


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def _opt(args: list[str], flag: str) -> str | None:
    if flag in args and args.index(flag) + 1 < len(args):
        return args[args.index(flag) + 1]
    return None


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd == "login":
        rest = argv[1:]
        positional = [a for i, a in enumerate(rest) if not a.startswith("--") and (i == 0 or not rest[i - 1].startswith("--"))]
        if not positional:
            print("Uso: google-classroom-mcp browser-login ALIAS [--email CORREO] [--plain]", file=sys.stderr)
            sys.exit(2)
        sys.exit(login(positional[0], email=_opt(rest, "--email"), plain="--plain" in rest))
    if cmd == "status":
        sys.exit(status())
    if cmd == "submit":
        payload = json.loads(argv[1])
        try:
            with ClassroomBrowser(payload["alias"], headless=payload.get("headless", HEADLESS)) as b:
                result = b.submit(
                    payload["url"], payload["email"], payload.get("files") or [], payload.get("turn_in", True)
                )
            _emit({"ok": True, **result})
        except BrowserError as e:
            _emit({"ok": False, "error": str(e), "screenshot": e.screenshot})
            sys.exit(1)
        except Exception as e:
            _emit({"ok": False, "error": f"{type(e).__name__}: {str(e).splitlines()[0][:300]}"})
            sys.exit(1)
        return
    print(__doc__)
    sys.exit(2)


if __name__ == "__main__":
    main()
