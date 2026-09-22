"""Submitting through the browser.

Google does not allow attaching files or turning in, through the API, assignments the
teacher created from the Classroom web app (@ProjectPermissionDenied). This module does
what the student would do on classroom.google.com, but with Playwright driving Google Chrome.

There is one Chrome profile per account (~/.config/google-classroom-mcp/browser-profiles/<alias>),
each with a single Google session, signed in once with
`google-classroom-mcp browser-login <alias>`. That way we do not depend on Google's
multi-account sign-in, which sometimes replaces one account when you add another.

It runs in a separate process from the MCP server (sync Playwright cannot run inside an
asyncio loop):

  python -m google_classroom_mcp.browser login ALIAS [--email EMAIL] [--plain]
  python -m google_classroom_mcp.browser status
  python -m google_classroom_mcp.browser submit '{"alias": ..., "url": ..., "email": ..., "files": [...], "turn_in": true}'
  python -m google_classroom_mcp.browser reclaim '{"alias": ..., "url": ..., "email": ...}'
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
SHOTS_DIR = DOWNLOAD_DIR / "_browser"
# By default the submitting Chrome runs hidden; GOOGLE_CLASSROOM_BROWSER_HEADLESS=0 shows it
# (useful to watch what it does when something fails). browser-login always opens a window: the user types there.
HEADLESS = os.environ.get("GOOGLE_CLASSROOM_BROWSER_HEADLESS", "1").lower() not in ("0", "false", "no")
CHROME_MAC = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CLASSROOM = "https://classroom.google.com"

# Classroom UI labels in English and Spanish.
RX_ADD = r"Add or create|Agregar o crear|Añadir o crear"
RX_FILE_ITEM = r"^\s*(File|Archivo)\s*$"
RX_UPLOAD_TAB = r"^\s*(Upload|Subir)\s*$"
RX_BROWSE = r"Browse|Explorar|Select files|Seleccionar archivos|Choose files|Elegir archivos"
RX_TURN_IN = r"^\s*(Turn in|Entregar)\s*$"
RX_MARK_DONE = r"Mark as done|Marcar como (completad[ao]|hech[ao])"
RX_UNSUBMIT = r"Unsubmit|Anular (la )?entrega"
# The "X" button on each attachment is named "Remove <file>" (in Spanish "Eliminar a <file>").
RX_REMOVE = r"^(Remove|Eliminar a|Quitar)\s"
RX_EMAIL = r"[\w.+-]+@[\w.-]+\.\w+"


class BrowserError(Exception):
    def __init__(self, msg: str, screenshot: str | None = None) -> None:
        super().__init__(msg)
        self.screenshot = screenshot


def profile_dir(alias: str) -> Path:
    alias = re.sub(r"[^A-Za-z0-9._@+-]+", "_", alias.strip())
    if not alias:
        raise BrowserError("Give the account alias (the same one shown by `google-classroom-mcp accounts`).")
    return PROFILES_DIR / alias


def _chrome_binary() -> str:
    if Path(CHROME_MAC).exists():
        return CHROME_MAC
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    raise BrowserError("Could not find a Google Chrome installation.")


def _profile_in_use(profile: Path) -> bool:
    """True if a Chrome is open with that profile (Chrome does not allow two at once)."""
    try:
        r = subprocess.run(["pgrep", "-f", f"user-data-dir={profile}"], capture_output=True, text=True)
    except OSError:
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


class ClassroomBrowser:
    """Real Chrome (channel=chrome) on an account's profile, driven with Playwright."""

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
                f"Account '{self.alias}' has no browser profile. Run `google-classroom-mcp browser-login {self.alias}`."
            )
        if _profile_in_use(self.profile):
            raise BrowserError(
                f"A Chrome window is already open with the '{self.alias}' profile. Close it and retry."
            )
        self._p = sync_playwright().start()
        # Playwright starts Chrome with --use-mock-keychain: cookies get encrypted with that
        # key, so signing in must also happen from here (see login).
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

    # --- helpers -----------------------------------------------------------
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
        return BrowserError(msg + (f" Screenshot: {shot}" if shot else ""), shot)

    def _settle(self, ms: int = 1500) -> None:
        self.page.wait_for_timeout(ms)

    # --- session -----------------------------------------------------------
    def session_email(self) -> str | None:
        """Email of the profile's Google session, or None if there is no session."""
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
                f"The '{self.alias}' profile has no Google session. Run `google-classroom-mcp browser-login {self.alias}`."
            )
        if found != email.lower():
            raise BrowserError(
                f"The '{self.alias}' profile is signed in as {found}, not {email}. "
                f"Run `google-classroom-mcp browser-login {self.alias} --email {email}`."
            )

    # --- submission --------------------------------------------------------
    def open_assignment(self, url: str) -> str:
        """Opens the assignment and waits for the "Your work" panel."""
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
                raise BrowserError("The assignment already shows as turned in ('Unsubmit'). Unsubmit it in Classroom if you want to change it.")
            for f in files:
                self._attach(Path(f))
                steps.append(f"attached {Path(f).name}")
            if turn_in:
                self._turn_in(has_files=bool(files) or self._has_attachments())
                steps.append("turned in")
        except BrowserError:
            raise
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            raise self.fail(f"Browser failure after {steps or 'opening the assignment'}: {type(e).__name__}: {msg}") from e
        return {"url": target, "steps": steps, "screenshot": self.shot("final")}

    def _has_attachments(self) -> bool:
        # With attachments the button says "Turn in"; without them, "Mark as done".
        return bool(self.page.get_by_role("button", name=re.compile(RX_TURN_IN, re.IGNORECASE)).count())

    def _attach(self, path: Path) -> None:
        page = self.page
        page.get_by_role("button", name=re.compile(RX_ADD, re.IGNORECASE)).first.click()
        page.get_by_role("menuitem", name=re.compile(RX_FILE_ITEM, re.IGNORECASE)).first.click()

        # Google's picker lives in an iframe (drive.google.com/picker) and opens on "Upload".
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

        # When the upload finishes the picker closes and the file shows up under "Your work"
        # with its "Remove <file>" button (the chip shows a truncated name, so it is no use).
        picker.wait_for(state="hidden", timeout=300_000)
        self._remove_button(path.name).first.wait_for(timeout=180_000)
        self._settle()

    def _remove_button(self, name: str):
        rx = re.compile(rf"^(Remove|Eliminar a|Quitar)\s+{re.escape(name)}\s*$", re.IGNORECASE)
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

    def reclaim(self, url: str, email: str) -> dict:
        """Unsubmits an assignment that was already turned in ("Unsubmit"). Attachments stay."""
        page = self.page
        target = url
        try:
            self.require_session(email)
            target = self.open_assignment(url)
            btn = page.get_by_role("button", name=re.compile(RX_UNSUBMIT, re.IGNORECASE)).first
            if not btn.count():
                raise BrowserError("The assignment does not show as turned in on Classroom, nothing to unsubmit.")
            btn.click()
            # Classroom asks for confirmation in a dialog whose button has the same label.
            dialog = page.get_by_role("dialog").last
            dialog.wait_for(state="visible", timeout=15_000)
            dialog.get_by_role("button", name=re.compile(RX_UNSUBMIT, re.IGNORECASE)).last.click()
            page.get_by_role("button", name=re.compile(f"{RX_TURN_IN}|{RX_MARK_DONE}", re.IGNORECASE)).first.wait_for(
                timeout=60_000
            )
            self._settle()
        except BrowserError:
            raise
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            raise self.fail(f"Browser failure while unsubmitting: {type(e).__name__}: {msg}") from e
        return {"url": target, "steps": ["unsubmitted"], "screenshot": self.shot("unsubmitted")}

    def detach(self, name: str | None = None) -> int:
        """Removes attachments from a submission that is not turned in: the one called `name`, or all of them."""
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
# Login and status
# --------------------------------------------------------------------------- #
def login(alias: str, email: str | None = None, plain: bool = False) -> int:
    """Opens Chrome with the account's profile so the user can sign in.

    By default it opens through Playwright (the same Chrome that later submits). On macOS
    Playwright uses a mock keychain to encrypt cookies; if the user signed in with a regular
    Chrome on the same profile, those cookies would be encrypted with another key and the
    automated Chrome could not read them (and wipes them). With --plain it opens Chrome
    without automation but with that same mock keychain, in case Google blocks signing in
    from the controlled browser."""
    profile = profile_dir(alias)
    profile.mkdir(parents=True, exist_ok=True)
    start = (
        f"https://accounts.google.com/AddSession?Email={email}&continue={CLASSROOM}/" if email else f"{CLASSROOM}/"
    )
    who = f" as {email}" if email else ""
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
            f"A separate Chrome window was opened for account '{alias}' (profile: {profile}).\n"
            f"Sign in{who} until you see Classroom, then quit that instance with Cmd+Q (your usual Chrome keeps running).\n"
            "Then check with:  google-classroom-mcp browser-status"
        )
        return 0

    print(f"A Chrome window controlled by Playwright is about to open for account '{alias}' (profile: {profile}).")
    print(f"Sign in{who} until you see Classroom, then close the window. Only that account in this profile.")
    print("Waiting for you to close the window...", flush=True)
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
    print("Window closed. Checking the session...", flush=True)
    with ClassroomBrowser(alias, headless=True) as b:
        found = b.session_email()
    if not found:
        print(f"The '{alias}' profile still has no Google session. Try again (or use --plain).")
        return 1
    if email and found != email.lower():
        print(f"The '{alias}' profile ended up signed in as {found}, not {email}. Run browser-login {alias} again.")
        return 1
    print(f"  {alias}\t{found}")
    return 0


def status() -> int:
    if not PROFILES_DIR.is_dir() or not any(PROFILES_DIR.iterdir()):
        print("No browser profiles. Run `google-classroom-mcp browser-login <alias>`.")
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
        print(f"{profile.name}\t{found or '(no session)'}")
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
            print("Usage: google-classroom-mcp browser-login ALIAS [--email EMAIL] [--plain]", file=sys.stderr)
            sys.exit(2)
        sys.exit(login(positional[0], email=_opt(rest, "--email"), plain="--plain" in rest))
    if cmd == "status":
        sys.exit(status())
    if cmd in ("submit", "reclaim"):
        payload = json.loads(argv[1])
        try:
            with ClassroomBrowser(payload["alias"], headless=payload.get("headless", HEADLESS)) as b:
                if cmd == "submit":
                    result = b.submit(
                        payload["url"], payload["email"], payload.get("files") or [], payload.get("turn_in", True)
                    )
                else:
                    result = b.reclaim(payload["url"], payload["email"])
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
