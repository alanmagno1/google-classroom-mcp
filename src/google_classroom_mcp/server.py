"""Servidor MCP para Google Classroom (vista de alumno), con varias cuentas.

Expone herramientas para consultar cursos, tareas y materiales, el estado de tus
entregas y calificaciones, anuncios, y para entregar tareas (adjuntar archivos de
Drive o enlaces a la entrega, entregar y retirar la entrega).

Pide permisos de Classroom y lectura de Drive: los adjuntos de tareas, materiales y
anuncios se pueden bajar al disco con download_file / download_assignment_files (los
Docs, Sheets y Slides de Google se exportan a PDF, xlsx, etc.). Con el permiso opcional
drive.file, upload_file y submit_assignment(files=...) suben archivos locales a la
carpeta "Entregas Classroom" de tu Drive para adjuntarlos a una entrega.

Varias cuentas: corre `setup` una vez por cuenta de Google. Cada herramienta acepta
un parámetro `account` opcional (alias o correo). Si lo omites y hay una sola cuenta
se usa esa; con varias, list_courses y list_pending_assignments consultan todas y las
demás herramientas ubican la cuenta a partir del curso.

Aviso sobre entregas: la API de Google solo permite adjuntar o entregar desde la
misma aplicación que creó la tarea. Con tareas creadas por el profesor desde la web
de Classroom, Google responde 403 (@ProjectPermissionDenied). Para esas,
submit_in_browser hace la entrega manejando Google Chrome (Playwright) sobre un
perfil por cuenta en el que iniciaste sesión una vez con `browser-login <alias>`, y
reclaim_in_browser la anula por la misma vía.

Configuración:
  ~/.config/google-classroom-mcp/client_secret.json      credenciales OAuth (Google Cloud Console)
  ~/.config/google-classroom-mcp/accounts/<alias>.json   token de cada cuenta (lo genera `setup`)
  ~/Downloads/google-classroom-mcp/                      descargas (GOOGLE_CLASSROOM_DOWNLOAD_DIR)

Comandos:
  google-classroom-mcp                                   arranca el servidor MCP (stdio)
  google-classroom-mcp setup [client.json] [--as ALIAS] [--hint CORREO]
                                                         guarda el client secret y autoriza una cuenta
                                                         (--hint: exige que sea ese correo)
  google-classroom-mcp accounts                          lista las cuentas configuradas
  google-classroom-mcp remove ALIAS                      quita una cuenta
  google-classroom-mcp check                             verifica la conexión de todas las cuentas
  google-classroom-mcp browser-login ALIAS [--email X]  abre Chrome para iniciar sesión en el perfil de esa cuenta
  google-classroom-mcp browser-status                    sesión de cada perfil de navegador
"""

from __future__ import annotations

import base64
import functools
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp.server.mcpserver import MCPServer

CONFIG_DIR = Path(
    os.environ.get("GOOGLE_CLASSROOM_MCP_CONFIG_DIR", Path.home() / ".config" / "google-classroom-mcp")
)
CLIENT_SECRET_FILE = Path(os.environ.get("GOOGLE_CLASSROOM_CLIENT_SECRET", CONFIG_DIR / "client_secret.json"))
ACCOUNTS_DIR = CONFIG_DIR / "accounts"
DOWNLOAD_DIR = Path(os.environ.get("GOOGLE_CLASSROOM_DOWNLOAD_DIR", Path.home() / "Downloads" / "google-classroom-mcp"))

# Permisos mínimos: sin ellos ninguna herramienta funciona.
CORE_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.profile.emails",
    # Solo lectura de Drive: para bajar los adjuntos (Google no los sirve por la API de Classroom).
    "https://www.googleapis.com/auth/drive.readonly",
]
# Opcional: crear archivos en Drive (solo ve los que crea esta app). Lo usan upload_file y
# submit_assignment(files=...). Un token autorizado sin él sigue sirviendo para todo lo demás.
UPLOAD_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = [*CORE_SCOPES, UPLOAD_SCOPE]
UPLOAD_FOLDER = "Entregas Classroom"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Archivos nativos de Google (Docs, Sheets, Slides...) no tienen binario: se exportan.
GOOGLE_APPS_PREFIX = "application/vnd.google-apps."
EXPORT_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "epub": "application/epub+zip",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odp": "application/vnd.oasis.opendocument.presentation",
    "png": "image/png",
    "jpg": "image/jpeg",
    "svg": "image/svg+xml",
    "json": "application/vnd.google-apps.script+json",
}
DEFAULT_EXPORT = {
    "document": "pdf",
    "presentation": "pdf",
    "spreadsheet": "xlsx",
    "drawing": "png",
    "script": "json",
    "jam": "pdf",
}

PENDING_STATES = ("NEW", "CREATED", "RECLAIMED_BY_STUDENT")

PROJECT_PERMISSION_HINT = (
    "Google solo permite adjuntar o entregar desde la aplicación que creó la tarea "
    "(@ProjectPermissionDenied). Esta tarea la creó el profesor desde la web de Classroom, "
    "así que la entrega hay que hacerla desde classroom.google.com."
)

mcp = MCPServer(
    "google-classroom",
    instructions=(
        "Herramientas para Google Classroom con una o varias cuentas de Google. Para saber qué "
        "tareas faltan usa list_pending_assignments (revisa todas las cuentas). Para una URL como "
        "classroom.google.com/c/XXX/a/YYY/details usa get_assignment con la URL completa. El "
        "parámetro account (alias o correo) es opcional; si hay varias cuentas y una herramienta "
        "no puede deducir la cuenta, te lo dirá. Para bajar los adjuntos de una tarea usa "
        "download_assignment_files (o download_file con un drive_id o URL de Drive); devuelven rutas "
        "locales que puedes leer con Read. Para entregar una tarea usa submit_in_browser(files=[ruta]): "
        "maneja Chrome con la sesión del usuario, adjunta, da Entregar y verifica por la API (es la única "
        "vía para tareas creadas por el profesor desde la web; la API las rechaza con "
        "@ProjectPermissionDenied). submit_assignment intenta la vía API y, con files=, deja el archivo "
        "en Drive. Para anular una entrega ya enviada usa reclaim_in_browser (reclaim_submission es la vía "
        "API y falla con esas mismas tareas). upload_file, submit_assignment, submit_in_browser, "
        "reclaim_submission y reclaim_in_browser crean archivos o modifican la entrega: úsalas solo cuando "
        "el usuario lo pida explícitamente."
    ),
)


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
class ClassroomError(Exception):
    pass


def _fmt_dt(value: str | None) -> str | None:
    """Convierte un timestamp RFC3339 de la API a hora local."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone().isoformat(timespec="minutes")
    except ValueError:
        return value


def _due_dt(item: dict) -> datetime | None:
    d = item.get("dueDate")
    if not d:
        return None
    t = item.get("dueTime") or {"hours": 23, "minutes": 59}
    return datetime(
        d["year"], d.get("month", 1), d.get("day", 1), t.get("hours", 0), t.get("minutes", 0), tzinfo=timezone.utc
    )


def _due(item: dict) -> str | None:
    """Fecha límite de una tarea. Classroom la guarda en UTC como fecha + hora separadas."""
    d = item.get("dueDate")
    if not d:
        return None
    if not item.get("dueTime"):
        return f"{d['year']:04d}-{d.get('month', 1):02d}-{d.get('day', 1):02d}"
    return _due_dt(item).astimezone().isoformat(timespec="minutes")


def _is_past_due(item: dict) -> bool | None:
    dt = _due_dt(item)
    return None if dt is None else datetime.now(timezone.utc) > dt


def _decode_id(segment: str) -> str:
    """Los IDs en las URLs de Classroom van en base64; los de la API son numéricos."""
    segment = segment.strip()
    if segment.isdigit():
        return segment
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode()
        if raw.isdigit():
            return raw
    except (ValueError, UnicodeDecodeError):
        pass
    return segment


def _ids_from_url(value: str) -> dict[str, str]:
    """Extrae course_id y coursework_id/material_id de una URL de Classroom, si los tiene."""
    out: dict[str, str] = {}
    if not value or not value.startswith("http"):
        return out
    parts = [p for p in urlparse(value).path.split("/") if p]
    for i, p in enumerate(parts[:-1]):
        nxt = parts[i + 1]
        if p == "c":
            out["course_id"] = _decode_id(nxt)
        elif p == "a":
            out["coursework_id"] = _decode_id(nxt)
        elif p == "m":
            out["material_id"] = _decode_id(nxt)
        elif p == "p":
            out["announcement_id"] = _decode_id(nxt)
    return out


def _course_id_of(value: str) -> str:
    return _ids_from_url(value).get("course_id", value.strip())


def _drive_id(value: str) -> str:
    """Acepta un id de Drive o una URL (…/d/ID/…, …?id=ID, …/file/d/ID, …/drive/folders/ID)."""
    value = value.strip()
    if not value.startswith("http"):
        return value
    u = urlparse(value)
    qs = parse_qs(u.query)
    if "id" in qs:
        return qs["id"][0]
    parts = [p for p in u.path.split("/") if p]
    for i, p in enumerate(parts[:-1]):
        if p in ("d", "folders"):
            return parts[i + 1]
    raise ClassroomError(f"No pude extraer un id de Drive de: {value}")


def _http_error_message(e: Exception) -> str:
    """Mensaje legible para un HttpError de googleapiclient."""
    from googleapiclient.errors import HttpError

    if not isinstance(e, HttpError):
        return f"{type(e).__name__}: {e}"
    status = e.resp.status
    try:
        detail = json.loads(e.content.decode()).get("error", {})
        msg = detail.get("message") or str(e)
        status = detail.get("status") or status
    except Exception:  # noqa: BLE001
        msg = str(e)
    if "ProjectPermissionDenied" in msg:
        msg = f"{msg}. {PROJECT_PERMISSION_HINT}"
    elif e.resp.status == 403 and "insufficient" in msg.lower():
        msg += " (vuelve a correr `google-classroom-mcp setup` para renovar los permisos de esa cuenta)"
    elif e.resp.status == 403 and "has not been used in project" in msg:
        msg += (
            " (habilita esa API en Google Cloud: APIs y servicios > Biblioteca, en el mismo proyecto del client secret)"
        )
    return f"Error de la API de Google [{status}]: {msg}"


def _safe_filename(name: str, fallback: str = "archivo") -> str:
    name = re.sub(r"[\\/\x00-\x1f]+", "_", (name or "").strip()).strip(". ")
    return name or fallback


def _materials(mats: list[dict] | None) -> list[dict] | None:
    out = []
    for m in mats or []:
        if "driveFile" in m:
            df = m["driveFile"].get("driveFile", {})
            out.append(
                {
                    "type": "drive",
                    "title": df.get("title"),
                    "drive_id": df.get("id"),
                    "url": df.get("alternateLink"),
                    "share_mode": m["driveFile"].get("shareMode"),
                }
            )
        elif "link" in m:
            out.append({"type": "link", "title": m["link"].get("title"), "url": m["link"].get("url")})
        elif "youtubeVideo" in m:
            yt = m["youtubeVideo"]
            out.append({"type": "youtube", "title": yt.get("title"), "url": yt.get("alternateLink")})
        elif "form" in m:
            f = m["form"]
            out.append({"type": "form", "title": f.get("title"), "url": f.get("formUrl")})
    return out or None


def _submission(s: dict | None) -> dict | None:
    if not s:
        return None
    atts = []
    for a in (s.get("assignmentSubmission") or {}).get("attachments", []):
        if "driveFile" in a:
            atts.append({"type": "drive", "title": a["driveFile"].get("title"), "drive_id": a["driveFile"].get("id"), "url": a["driveFile"].get("alternateLink")})
        elif "link" in a:
            atts.append({"type": "link", "title": a["link"].get("title"), "url": a["link"].get("url")})
        elif "youTubeVideo" in a:
            atts.append({"type": "youtube", "title": a["youTubeVideo"].get("title"), "url": a["youTubeVideo"].get("alternateLink")})
        elif "form" in a:
            atts.append({"type": "form", "title": a["form"].get("title"), "url": a["form"].get("formUrl")})
    out: dict[str, Any] = {
        "id": s.get("id"),
        "state": s.get("state"),
        "late": s.get("late", False),
        "assigned_grade": s.get("assignedGrade"),
        "draft_grade": s.get("draftGrade"),
        "created": _fmt_dt(s.get("creationTime")),
        "updated": _fmt_dt(s.get("updateTime")),
        "url": s.get("alternateLink"),
        "attachments": atts or None,
    }
    if "shortAnswerSubmission" in s:
        out["short_answer"] = s["shortAnswerSubmission"].get("answer")
    if "multipleChoiceSubmission" in s:
        out["multiple_choice_answer"] = s["multipleChoiceSubmission"].get("answer")
    return out


def _coursework_summary(cw: dict, sub: dict | None = None) -> dict:
    out = {
        "id": cw.get("id"),
        "course_id": cw.get("courseId"),
        "type": cw.get("workType"),
        "title": cw.get("title"),
        "description": cw.get("description") or None,
        "state": cw.get("state"),
        "due": _due(cw),
        "past_due": _is_past_due(cw),
        "max_points": cw.get("maxPoints"),
        "created": _fmt_dt(cw.get("creationTime")),
        "updated": _fmt_dt(cw.get("updateTime")),
        "url": cw.get("alternateLink"),
        "materials": _materials(cw.get("materials")),
    }
    if "multipleChoiceQuestion" in cw:
        out["choices"] = cw["multipleChoiceQuestion"].get("choices")
    if sub is not None:
        out["my_submission"] = _submission(sub)
    return out


# --------------------------------------------------------------------------- #
# Cuentas y cliente de Google
# --------------------------------------------------------------------------- #
def _safe_alias(alias: str) -> str:
    alias = re.sub(r"[^A-Za-z0-9._@+-]+", "_", alias.strip())
    if not alias:
        raise ClassroomError("El alias de la cuenta no puede estar vacío.")
    return alias


def _granted_scopes(scopes, who: str, alias: str) -> set[str]:
    """Permisos con los que se autorizó un token; falla si falta alguno de los mínimos."""
    granted = set(scopes or CORE_SCOPES)
    missing = set(CORE_SCOPES) - granted
    if missing:
        short = ", ".join(sorted(s.rsplit("/", 1)[1] for s in missing))
        raise ClassroomError(
            f"El token de la cuenta {who} no tiene todos los permisos que necesita esta versión "
            f"(faltan: {short}). Vuelve a correr `google-classroom-mcp setup --as {alias}`."
        )
    return granted


class Account:
    """Una cuenta de Google autorizada: credenciales, servicios y perfil."""

    def __init__(self, alias: str, path: Path) -> None:
        self.alias = alias
        self.path = path
        meta = {}
        try:
            meta = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        self.email: str | None = meta.get("email")
        self.name: str | None = meta.get("name")
        self._creds = None
        self._classroom = None
        self._drive = None
        self._profile: dict | None = None
        self.granted_scopes: set[str] = set()

    def __repr__(self) -> str:
        return f"{self.alias} ({self.email})" if self.email and self.email != self.alias else self.alias

    # --- credenciales ------------------------------------------------------
    def _load_credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        data = json.loads(self.path.read_text())
        info = data.get("credentials", data)
        # Sin pasar SCOPES: el token se carga con los permisos con que se autorizó. Si se
        # pidieran los actuales y el token fuera de una versión anterior, Google rechazaría
        # renovarlo y dejarían de funcionar hasta las herramientas que no necesitan lo nuevo.
        creds = Credentials.from_authorized_user_info(info)
        self.granted_scopes = _granted_scopes(creds.scopes, repr(self), self.alias)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:  # noqa: BLE001
                    raise ClassroomError(
                        f"No pude renovar la sesión de {self} ({e}). "
                        f"Vuelve a correr `google-classroom-mcp setup --as {self.alias}`."
                    ) from e
                self.save(creds)
            else:
                raise ClassroomError(
                    f"La sesión de {self} expiró. Vuelve a correr `google-classroom-mcp setup --as {self.alias}`."
                )
        return creds

    def save(self, creds, email: str | None = None, name: str | None = None) -> None:
        self.email = email or self.email
        self.name = name or self.name
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "alias": self.alias,
            "email": self.email,
            "name": self.name,
            "credentials": json.loads(creds.to_json()),
        }
        self.path.write_text(json.dumps(payload, indent=2))
        self.path.chmod(0o600)

    @property
    def creds(self):
        if self._creds is None:
            self._creds = self._load_credentials()
        return self._creds

    @property
    def classroom(self):
        if self._classroom is None:
            from googleapiclient.discovery import build

            self._classroom = build("classroom", "v1", credentials=self.creds, cache_discovery=False)
        return self._classroom

    @property
    def drive(self):
        if self._drive is None:
            from googleapiclient.discovery import build

            self._drive = build("drive", "v3", credentials=self.creds, cache_discovery=False)
        return self._drive

    @property
    def can_upload(self) -> bool:
        self.creds  # noqa: B018  (carga granted_scopes)
        return UPLOAD_SCOPE in self.granted_scopes

    def require_upload(self) -> None:
        if not self.can_upload:
            hint = f" --hint {self.email}" if self.email else ""
            raise ClassroomError(
                f"La cuenta {self} se autorizó sin el permiso para subir archivos a Drive. "
                f"Corre `google-classroom-mcp setup --as {self.alias}{hint}` y vuelve a intentar."
            )

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def paged(request_fn, key: str, **params: Any) -> list[dict]:
        """Recorre todas las páginas de un método list de la API."""
        items: list[dict] = []
        token = None
        while True:
            resp = request_fn(pageToken=token, **params).execute()
            items.extend(resp.get(key, []))
            token = resp.get("nextPageToken")
            if not token:
                break
        return items

    def profile(self) -> dict:
        if self._profile is None:
            self._profile = self.classroom.userProfiles().get(userId="me").execute()
        return self._profile

    def courses(self, states: list[str]) -> list[dict]:
        return self.paged(self.classroom.courses().list, "courses", studentId="me", courseStates=states)

    def my_submissions(self, course_id: str, coursework_id: str = "-", states: list[str] | None = None) -> list[dict]:
        params: dict[str, Any] = {"courseId": course_id, "courseWorkId": coursework_id, "userId": "me"}
        if states:
            params["states"] = states
        return self.paged(self.classroom.courses().courseWork().studentSubmissions().list, "studentSubmissions", **params)

    def has_course(self, course_id: str) -> bool:
        from googleapiclient.errors import HttpError

        try:
            self.classroom.courses().get(id=course_id).execute()
            return True
        except HttpError as e:
            if e.resp.status in (403, 404):
                return False
            raise

    def drive_file(self, file_id: str) -> dict | None:
        """Metadatos de un archivo de Drive, o None si esta cuenta no lo ve."""
        from googleapiclient.errors import HttpError

        try:
            return (
                self.drive.files()
                .get(fileId=file_id, fields="id,name,mimeType,size,shortcutDetails", supportsAllDrives=True)
                .execute()
            )
        except HttpError as e:
            if e.resp.status == 404:
                return None
            raise


class Accounts:
    def __init__(self) -> None:
        self._cache: dict[str, Account] = {}
        self._course_owner: dict[str, str] = {}

    def aliases(self) -> list[str]:
        if not ACCOUNTS_DIR.is_dir():
            return []
        return sorted(p.stem for p in ACCOUNTS_DIR.glob("*.json"))

    def get(self, alias: str) -> Account:
        if alias not in self._cache:
            self._cache[alias] = Account(alias, ACCOUNTS_DIR / f"{alias}.json")
        return self._cache[alias]

    def all(self) -> list[Account]:
        aliases = self.aliases()
        if not aliases:
            raise ClassroomError(
                "No hay ninguna cuenta de Google configurada. Ejecuta `google-classroom-mcp setup` "
                f"(busca el client secret en {CLIENT_SECRET_FILE})."
            )
        return [self.get(a) for a in aliases]

    def find(self, account: str) -> Account:
        """Busca por alias exacto, correo, o coincidencia parcial sin importar mayúsculas."""
        accounts = self.all()
        needle = account.strip().lower()
        for a in accounts:
            if a.alias.lower() == needle or (a.email or "").lower() == needle:
                return a
        partial = [a for a in accounts if needle in a.alias.lower() or needle in (a.email or "").lower()]
        if len(partial) == 1:
            return partial[0]
        raise ClassroomError(
            f"No hay una cuenta que coincida con '{account}'. Cuentas disponibles: "
            + ", ".join(repr(a) for a in accounts)
        )

    def resolve(self, account: str | None, course_id: str | None = None) -> Account:
        """Elige la cuenta: la indicada, la única que hay, o la que tiene el curso."""
        if account:
            return self.find(account)
        accounts = self.all()
        if len(accounts) == 1:
            return accounts[0]
        if course_id:
            if course_id in self._course_owner:
                return self.get(self._course_owner[course_id])
            problems = []
            for a in accounts:
                try:
                    if a.has_course(course_id):
                        self._course_owner[course_id] = a.alias
                        return a
                except ClassroomError as e:
                    problems.append(str(e))
            msg = f"Ninguna cuenta tiene acceso al curso {course_id}. Cuentas: " + ", ".join(repr(a) for a in accounts)
            if problems:
                msg += ". Además: " + " | ".join(problems)
            raise ClassroomError(msg)
        raise ClassroomError(
            "Hay varias cuentas configuradas; indica account=<alias o correo>. Cuentas: "
            + ", ".join(repr(a) for a in accounts)
        )

    def selection(self, account: str | None) -> list[Account]:
        """Para herramientas que pueden recorrer todas las cuentas."""
        return [self.find(account)] if account else self.all()

    def resolve_drive_file(self, account: str | None, file_id: str) -> tuple[Account, dict]:
        """Cuenta que ve el archivo de Drive y sus metadatos."""
        candidates = [self.find(account)] if account else self.all()
        for a in candidates:
            meta = a.drive_file(file_id)
            if meta is not None:
                return a, meta
        who = repr(candidates[0]) if len(candidates) == 1 else ", ".join(repr(a) for a in candidates)
        raise ClassroomError(f"El archivo de Drive {file_id} no existe o la cuenta no tiene acceso ({who}).")

    def reset(self) -> None:
        self._cache.clear()
        self._course_owner.clear()


accounts = Accounts()


def _tool(fn):
    """Envuelve una herramienta para devolver errores legibles en vez de trazas.

    Quita la anotación de retorno para que MCP no valide el resultado contra un
    esquema estricto (un error es un str, no un dict ni una lista)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClassroomError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001
            return _http_error_message(e) if _is_http_error(e) else f"Error: {type(e).__name__}: {e}"

    wrapper.__signature__ = inspect.signature(fn).replace(return_annotation=inspect.Signature.empty)
    wrapper.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "return"}
    return wrapper


def _is_http_error(e: Exception) -> bool:
    from googleapiclient.errors import HttpError

    return isinstance(e, HttpError)


# --------------------------------------------------------------------------- #
# Herramientas de consulta
# --------------------------------------------------------------------------- #
@mcp.tool()
@_tool
def list_accounts() -> list[dict]:
    """Cuentas de Google configuradas (alias y correo). Sin conexión a la API."""
    return [{"account": a.alias, "email": a.email, "name": a.name} for a in accounts.all()]


@mcp.tool()
@_tool
def get_profile(account: str | None = None) -> list[dict]:
    """Verifica la conexión y devuelve el usuario autenticado de cada cuenta (o de la indicada)."""
    out = []
    for a in accounts.selection(account):
        try:
            p = a.profile()
            out.append(
                {
                    "account": a.alias,
                    "id": p.get("id"),
                    "name": (p.get("name") or {}).get("fullName"),
                    "email": p.get("emailAddress"),
                    "verified_teacher": p.get("verifiedTeacher", False),
                    "upload_to_drive": a.can_upload,
                }
            )
        except Exception as e:  # noqa: BLE001
            out.append({"account": a.alias, "error": str(e) if isinstance(e, ClassroomError) else _http_error_message(e)})
    return out


@mcp.tool()
@_tool
def list_courses(account: str | None = None, include_archived: bool = False) -> list[dict]:
    """Cursos de Classroom en los que estás inscrito como alumno. Sin account recorre
    todas las cuentas configuradas."""
    states = ["ACTIVE"] + (["ARCHIVED"] if include_archived else [])
    out = []
    for a in accounts.selection(account):
        for c in a.courses(states):
            accounts._course_owner[c["id"]] = a.alias
            out.append(
                {
                    "account": a.alias,
                    "id": c["id"],
                    "name": c.get("name"),
                    "section": c.get("section"),
                    "description_heading": c.get("descriptionHeading"),
                    "room": c.get("room"),
                    "state": c.get("courseState"),
                    "created": _fmt_dt(c.get("creationTime")),
                    "updated": _fmt_dt(c.get("updateTime")),
                    "url": c.get("alternateLink"),
                }
            )
    return out


@mcp.tool()
@_tool
def list_pending_assignments(account: str | None = None, course_id: str | None = None) -> list[dict]:
    """Tareas que aún no has entregado (estado NEW, CREATED o RECLAIMED_BY_STUDENT),
    agrupadas por cuenta y curso y ordenadas por fecha límite. Sin account ni course_id
    revisa todos los cursos activos de todas las cuentas. Incluye si ya venció."""
    if course_id:
        cid = _course_id_of(course_id)
        acc = accounts.resolve(account, cid)
        targets = [(acc, acc.classroom.courses().get(id=cid).execute())]
    else:
        targets = [(a, c) for a in accounts.selection(account) for c in a.courses(["ACTIVE"])]

    out = []
    for a, c in targets:
        accounts._course_owner[c["id"]] = a.alias
        subs = {s["courseWorkId"]: s for s in a.my_submissions(c["id"], states=list(PENDING_STATES))}
        if not subs:
            continue
        works = a.paged(a.classroom.courses().courseWork().list, "courseWork", courseId=c["id"], courseWorkStates=["PUBLISHED"])
        pending = [_coursework_summary(w, subs[w["id"]]) for w in works if w["id"] in subs]
        pending.sort(key=lambda w: (w["due"] is None, w["due"] or ""))
        if pending:
            out.append(
                {"account": a.alias, "course_id": c["id"], "course": c.get("name"), "section": c.get("section"), "pending": pending}
            )
    return out


@mcp.tool()
@_tool
def get_course_contents(course_id: str, account: str | None = None) -> dict:
    """Trabajo de clase de un curso agrupado por tema: tareas (con el estado de tu entrega
    y calificación), preguntas y materiales. Acepta el id o la URL del curso."""
    course_id = _course_id_of(course_id)
    a = accounts.resolve(account, course_id)
    course = a.classroom.courses().get(id=course_id).execute()

    topics = {t["topicId"]: t.get("name") for t in a.paged(a.classroom.courses().topics().list, "topic", courseId=course_id)}
    works = a.paged(a.classroom.courses().courseWork().list, "courseWork", courseId=course_id)
    mats = a.paged(a.classroom.courses().courseWorkMaterials().list, "courseWorkMaterial", courseId=course_id)
    subs = {s["courseWorkId"]: s for s in a.my_submissions(course_id)}

    grouped: dict[str | None, list[dict]] = {}
    for w in works:
        grouped.setdefault(w.get("topicId"), []).append(_coursework_summary(w, subs.get(w["id"])))
    for m in mats:
        grouped.setdefault(m.get("topicId"), []).append(
            {
                "id": m.get("id"),
                "course_id": m.get("courseId"),
                "type": "MATERIAL",
                "title": m.get("title"),
                "description": m.get("description") or None,
                "state": m.get("state"),
                "created": _fmt_dt(m.get("creationTime")),
                "updated": _fmt_dt(m.get("updateTime")),
                "url": m.get("alternateLink"),
                "materials": _materials(m.get("materials")),
            }
        )

    sections = []
    for topic_id, items in grouped.items():
        items.sort(key=lambda i: i.get("created") or "", reverse=True)
        sections.append({"topic": topics.get(topic_id) if topic_id else "(sin tema)", "items": items})
    sections.sort(key=lambda s: s["topic"] == "(sin tema)")

    return {
        "account": a.alias,
        "course": {
            "id": course["id"],
            "name": course.get("name"),
            "section": course.get("section"),
            "description": course.get("description") or None,
            "url": course.get("alternateLink"),
        },
        "sections": sections,
    }


def _locate_coursework(coursework_id_or_url: str, course_id: str | None) -> tuple[str, str, dict[str, str]]:
    ids = _ids_from_url(coursework_id_or_url)
    coursework_id = ids.get("coursework_id", coursework_id_or_url.strip())
    course_id = ids.get("course_id") or (_course_id_of(course_id) if course_id else None)
    if not course_id:
        raise ClassroomError("Indica course_id o pasa la URL completa de la tarea.")
    return course_id, coursework_id, ids


@mcp.tool()
@_tool
def get_assignment(coursework_id_or_url: str, course_id: str | None = None, account: str | None = None) -> dict:
    """Detalle de una tarea o pregunta: instrucciones, fecha límite, puntos, materiales
    adjuntos y el estado de tu entrega (archivos, calificación). Acepta la URL completa
    (https://classroom.google.com/c/XXX/a/YYY/details) o el id de la tarea junto con course_id."""
    course_id, coursework_id, ids = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if "material_id" in ids and "coursework_id" not in ids:
        m = a.classroom.courses().courseWorkMaterials().get(courseId=course_id, id=ids["material_id"]).execute()
        return {
            "account": a.alias,
            "id": m.get("id"),
            "course_id": course_id,
            "type": "MATERIAL",
            "title": m.get("title"),
            "description": m.get("description") or None,
            "url": m.get("alternateLink"),
            "materials": _materials(m.get("materials")),
        }

    cw = a.classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    subs = a.my_submissions(course_id, coursework_id)
    result = {"account": a.alias, **_coursework_summary(cw, subs[0] if subs else None)}
    result["submission_modification_mode"] = cw.get("submissionModificationMode")
    return result


@mcp.tool()
@_tool
def list_announcements(course_id: str, limit: int = 20, account: str | None = None) -> list[dict]:
    """Anuncios del tablón de un curso, del más reciente al más antiguo."""
    course_id = _course_id_of(course_id)
    a = accounts.resolve(account, course_id)
    resp = (
        a.classroom.courses()
        .announcements()
        .list(courseId=course_id, orderBy="updateTime desc", pageSize=min(max(limit, 1), 100))
        .execute()
    )
    return [
        {
            "id": an.get("id"),
            "text": an.get("text"),
            "state": an.get("state"),
            "created": _fmt_dt(an.get("creationTime")),
            "updated": _fmt_dt(an.get("updateTime")),
            "url": an.get("alternateLink"),
            "materials": _materials(an.get("materials")),
        }
        for an in resp.get("announcements", [])
    ]


# --------------------------------------------------------------------------- #
# Herramientas de descarga
# --------------------------------------------------------------------------- #
def _dest_folder(dest_dir: str | None, default: Path) -> Path:
    if not dest_dir:
        return default
    p = Path(dest_dir).expanduser()
    return p if p.is_absolute() else DOWNLOAD_DIR / p


def _download_drive_file(
    a: Account, meta: dict, dest_dir: Path, filename: str | None = None, export_format: str | None = None
) -> dict:
    """Baja un archivo de Drive al disco. Los archivos nativos de Google se exportan."""
    from googleapiclient.http import MediaIoBaseDownload

    file_id = meta["id"]
    mime = meta.get("mimeType") or ""
    name = _safe_filename(filename or meta.get("name") or file_id)
    exported: str | None = None

    if mime.startswith(GOOGLE_APPS_PREFIX):
        kind = mime[len(GOOGLE_APPS_PREFIX) :]
        if kind == "shortcut":
            target = (meta.get("shortcutDetails") or {}).get("targetId")
            target_meta = a.drive_file(target) if target else None
            if target_meta is None:
                raise ClassroomError(f"'{meta.get('name')}' es un atajo de Drive y no puedo ver el archivo al que apunta.")
            return _download_drive_file(a, target_meta, dest_dir, filename, export_format)
        if kind in ("folder", "form", "site", "map", "fusiontable"):
            raise ClassroomError(
                f"'{meta.get('name')}' es un {kind} de Google y no se descarga como archivo. "
                f"Ábrelo en https://drive.google.com/open?id={file_id}"
            )
        fmt = (export_format or DEFAULT_EXPORT.get(kind, "pdf")).lower().lstrip(".")
        if fmt not in EXPORT_MIME:
            raise ClassroomError(f"Formato de exportación desconocido: {fmt}. Opciones: {', '.join(EXPORT_MIME)}")
        if not name.lower().endswith(f".{fmt}"):
            name = f"{name}.{fmt}"
        request = a.drive.files().export_media(fileId=file_id, mimeType=EXPORT_MIME[fmt])
        exported = fmt
    else:
        request = a.drive.files().get_media(fileId=file_id, supportsAllDrives=True)

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    try:
        with open(dest, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request, chunksize=8 * 1024 * 1024)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=3)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise

    out: dict[str, Any] = {
        "path": str(dest),
        "title": meta.get("name"),
        "mime_type": mime,
        "size": dest.stat().st_size,
        "drive_id": file_id,
    }
    if exported:
        out["exported_as"] = exported
    return out


@mcp.tool()
@_tool
def download_file(
    file_id_or_url: str,
    filename: str | None = None,
    dest_dir: str | None = None,
    export_format: str | None = None,
    account: str | None = None,
) -> dict:
    """Descarga un archivo de Drive (el drive_id o la url que devuelven las demás
    herramientas) a ~/Downloads/google-classroom-mcp/ (o a dest_dir) y devuelve la ruta
    local; luego puedes leerlo con Read. Los Docs, Sheets y Slides de Google no tienen
    archivo propio y se exportan: Docs y Slides a pdf, Sheets a xlsx, dibujos a png;
    export_format lo cambia (pdf, docx, txt, md, html, xlsx, csv, pptx, png...). Sin
    account prueba con todas las cuentas hasta dar con la que ve el archivo."""
    file_id = _drive_id(file_id_or_url)
    a, meta = accounts.resolve_drive_file(account, file_id)
    folder = _dest_folder(dest_dir, DOWNLOAD_DIR)
    return {"account": a.alias, **_download_drive_file(a, meta, folder, filename, export_format)}


@mcp.tool()
@_tool
def download_assignment_files(
    coursework_id_or_url: str,
    course_id: str | None = None,
    dest_dir: str | None = None,
    include_submission: bool = False,
    export_format: str | None = None,
    account: str | None = None,
) -> dict:
    """Descarga todos los adjuntos de Drive de una tarea o material (acepta la URL
    completa de Classroom o el id junto con course_id) a
    ~/Downloads/google-classroom-mcp/<curso>/<tarea>/ (o a dest_dir). Con
    include_submission=True baja también los archivos de tu propia entrega. Los enlaces,
    videos de YouTube y formularios no se descargan: vienen en not_downloadable con su url.
    Los archivos de Google se exportan igual que en download_file."""
    course_id, coursework_id, ids = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    is_material = "material_id" in ids and "coursework_id" not in ids
    if is_material:
        item = a.classroom.courses().courseWorkMaterials().get(courseId=course_id, id=ids["material_id"]).execute()
    else:
        item = a.classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()

    mats = [dict(m, source="assignment") for m in _materials(item.get("materials")) or []]
    if include_submission and not is_material:
        subs = a.my_submissions(course_id, coursework_id)
        sub = _submission(subs[0]) if subs else None
        mats += [dict(m, source="submission") for m in (sub or {}).get("attachments") or []]

    course_name = (a.classroom.courses().get(id=course_id).execute() or {}).get("name") or course_id
    folder = _dest_folder(
        dest_dir, DOWNLOAD_DIR / _safe_filename(course_name) / _safe_filename(item.get("title") or coursework_id)
    )
    files: list[dict] = []
    skipped: list[dict] = []
    for m in mats:
        if m.get("type") != "drive" or not m.get("drive_id"):
            skipped.append(m)
            continue
        try:
            meta = a.drive_file(m["drive_id"])
            if meta is None:
                raise ClassroomError("la cuenta no tiene acceso a este archivo en Drive")
            files.append({**_download_drive_file(a, meta, folder, None, export_format), "source": m["source"]})
        except Exception as e:  # noqa: BLE001
            files.append(
                {
                    "drive_id": m["drive_id"],
                    "title": m.get("title"),
                    "url": m.get("url"),
                    "source": m["source"],
                    "error": str(e) if isinstance(e, ClassroomError) else _http_error_message(e),
                }
            )

    return {
        "account": a.alias,
        "course_id": course_id,
        "id": item.get("id"),
        "title": item.get("title"),
        "url": item.get("alternateLink"),
        "folder": str(folder),
        "downloaded": sum(1 for f in files if "path" in f),
        "failed": sum(1 for f in files if "error" in f),
        "files": files,
        "not_downloadable": skipped or None,
    }


# --------------------------------------------------------------------------- #
# Subida a Drive
# --------------------------------------------------------------------------- #
def _q(value: str) -> str:
    """Escapa un literal para una consulta de la API de Drive."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _ensure_folder(a: Account, name: str, parent_id: str | None = None) -> str:
    """Id de la carpeta `name` dentro de `parent_id` (o de la raíz); la crea si no existe."""
    parent = parent_id or "root"
    q = f"name = '{_q(name)}' and mimeType = '{FOLDER_MIME}' and trashed = false and '{parent}' in parents"
    resp = a.drive.files().list(q=q, fields="files(id,name)", spaces="drive", pageSize=1).execute()
    found = resp.get("files") or []
    if found:
        return found[0]["id"]
    body: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        body["parents"] = [parent_id]
    return a.drive.files().create(body=body, fields="id").execute()["id"]


def _resolve_folder(a: Account, folder: str | None) -> tuple[str, str]:
    """(id, descripción) de la carpeta destino: un id/URL de Drive, o un nombre de
    subcarpeta dentro de "Entregas Classroom" (por default, esa misma)."""
    if folder and (folder.startswith("http") or re.fullmatch(r"[A-Za-z0-9_-]{20,}", folder)):
        fid = _drive_id(folder)
        return fid, f"https://drive.google.com/drive/folders/{fid}"
    root = _ensure_folder(a, UPLOAD_FOLDER)
    if not folder:
        return root, UPLOAD_FOLDER
    sub = _safe_filename(folder)
    return _ensure_folder(a, sub, root), f"{UPLOAD_FOLDER}/{sub}"


def _upload_to_drive(a: Account, path: Path, folder_id: str, name: str | None = None) -> dict:
    import mimetypes

    from googleapiclient.http import MediaFileUpload

    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    body = {"name": _safe_filename(name or path.name), "parents": [folder_id]}
    media = MediaFileUpload(str(path), mimetype=mime, resumable=path.stat().st_size > 5 * 1024 * 1024)
    f = a.drive.files().create(body=body, media_body=media, fields="id,name,mimeType,size,webViewLink").execute()
    return {
        "drive_id": f["id"],
        "title": f.get("name"),
        "mime_type": f.get("mimeType"),
        "size": int(f.get("size") or path.stat().st_size),
        "url": f.get("webViewLink"),
        "local_path": str(path),
    }


def _local_file(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_file():
        raise ClassroomError(f"No existe el archivo {p}")
    return p


ATTACH_IN_WEB = (
    "Los archivos ya están en tu Drive. En Classroom abre la tarea, dale a 'Agregar o crear' > "
    "'Google Drive', elígelos (aparecen en Recientes) y da 'Entregar'."
)


@mcp.tool()
@_tool
def upload_file(path: str, folder: str | None = None, name: str | None = None, account: str | None = None) -> dict:
    """Sube un archivo local a la carpeta "Entregas Classroom" de tu Drive (o a la
    subcarpeta `folder`, por ejemplo el nombre del curso; también acepta un id o URL de
    carpeta de Drive) y devuelve su drive_id y url. Con ellos puedes llamar a
    submit_assignment(drive_ids=[...]) o adjuntarlo desde la web de Classroom. Requiere
    haber autorizado la cuenta con el permiso de subir archivos (drive.file). Úsala solo
    cuando el usuario lo pida explícitamente."""
    p = _local_file(path)
    a = accounts.resolve(account)
    a.require_upload()
    folder_id, where = _resolve_folder(a, folder)
    return {"account": a.alias, "folder": where, **_upload_to_drive(a, p, folder_id, name)}


# --------------------------------------------------------------------------- #
# Herramientas de entrega
# --------------------------------------------------------------------------- #
@mcp.tool()
@_tool
def submit_assignment(
    coursework_id_or_url: str,
    course_id: str | None = None,
    files: list[str] | None = None,
    drive_ids: list[str] | None = None,
    links: list[str] | None = None,
    turn_in: bool = False,
    account: str | None = None,
) -> dict:
    """Entrega una tarea: sube los archivos locales de `files` a tu Drive (carpeta
    "Entregas Classroom/<curso>"), adjunta esos y los de `drive_ids` (ids o URLs de Drive)
    y/o `links` a tu entrega y, si turn_in=True, la entrega. Úsala solo cuando el usuario
    lo pida explícitamente.

    Aviso: Google solo permite adjuntar y entregar por API desde la app que creó la tarea.
    Si el profesor la creó desde la web de Classroom (lo normal), adjuntar devuelve 403
    @ProjectPermissionDenied; los archivos de `files` ya quedaron en Drive y el resultado
    trae en next_step cómo terminar desde classroom.google.com."""
    from googleapiclient.errors import HttpError

    if not (files or drive_ids or links or turn_in):
        raise ClassroomError("Indica files, drive_ids, links o turn_in=True.")
    paths = [_local_file(f) for f in files or []]
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if paths:
        a.require_upload()

    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("No encontré tu entrega para esta tarea (¿es una tarea de tipo ASSIGNMENT y estás inscrito?).")
    sub = subs[0]
    result: dict[str, Any] = {"account": a.alias, "course_id": course_id, "coursework_id": coursework_id}

    uploaded: list[dict] = []
    if paths:
        course_name = (a.classroom.courses().get(id=course_id).execute() or {}).get("name") or course_id
        folder_id, where = _resolve_folder(a, course_name)
        uploaded = [_upload_to_drive(a, p, folder_id) for p in paths]
        result["uploaded"] = uploaded
        result["drive_folder"] = where

    attachments = [{"driveFile": {"id": u["drive_id"]}} for u in uploaded]
    attachments += [{"driveFile": {"id": _drive_id(d)}} for d in drive_ids or []]
    attachments += [{"link": {"url": url}} for url in links or []]

    api = a.classroom.courses().courseWork().studentSubmissions()
    if attachments:
        try:
            sub = api.modifyAttachments(
                courseId=course_id, courseWorkId=coursework_id, id=sub["id"], body={"addAttachments": attachments}
            ).execute()
            result["attached"] = True
        except HttpError as e:
            result["attached"] = False
            result["attach_error"] = _http_error_message(e)
            if uploaded:
                result["next_step"] = ATTACH_IN_WEB

    if turn_in and result.get("attached", True):
        try:
            api.turnIn(courseId=course_id, courseWorkId=coursework_id, id=sub["id"], body={}).execute()
            result["turned_in"] = True
            sub = api.get(courseId=course_id, courseWorkId=coursework_id, id=sub["id"]).execute()
        except HttpError as e:
            result["turned_in"] = False
            result["turn_in_error"] = _http_error_message(e)
    elif turn_in:
        result["turned_in"] = False
        result["turn_in_error"] = "No se intentó entregar porque falló el paso de adjuntar."

    result["submission"] = _submission(sub)
    return result


@mcp.tool()
@_tool
def reclaim_submission(coursework_id_or_url: str, course_id: str | None = None, account: str | None = None) -> dict:
    """Retira una entrega ya enviada (equivale a "Anular entrega") para poder modificarla.
    Misma restricción de Google que submit_assignment. Úsala solo si el usuario lo pide."""
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("No encontré tu entrega para esta tarea.")
    api = a.classroom.courses().courseWork().studentSubmissions()
    api.reclaim(courseId=course_id, courseWorkId=coursework_id, id=subs[0]["id"], body={}).execute()
    sub = api.get(courseId=course_id, courseWorkId=coursework_id, id=subs[0]["id"]).execute()
    return {"account": a.alias, "reclaimed": True, "submission": _submission(sub)}


# --------------------------------------------------------------------------- #
# Entrega por navegador
# --------------------------------------------------------------------------- #
def _run_browser(payload: dict, timeout: int = 900, cmd: str = "submit") -> dict:
    """Corre el flujo de navegador (`submit` o `reclaim`) en un proceso aparte y devuelve su JSON final."""
    args = [sys.executable, "-m", "google_classroom_mcp.browser", cmd, json.dumps(payload)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ClassroomError(f"El navegador tardó más de {timeout // 60} minutos y se canceló.") from e
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        out = json.loads(lines[-1]) if lines else {}
    except ValueError:
        out = {}
    if not out:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        raise ClassroomError("El navegador terminó sin respuesta: " + " | ".join(tail))
    if not out.get("ok"):
        raise ClassroomError(out.get("error") or "fallo desconocido en el navegador")
    return out


@mcp.tool()
@_tool
def submit_in_browser(
    coursework_id_or_url: str,
    files: list[str] | None = None,
    course_id: str | None = None,
    turn_in: bool = True,
    account: str | None = None,
) -> dict:
    """Entrega una tarea como lo harías en classroom.google.com: abre Google Chrome con el
    perfil de esa cuenta (sesión iniciada una vez con `google-classroom-mcp browser-login
    <alias>`), adjunta los archivos locales de `files` con "Agregar o crear > Archivo",
    da "Entregar" (o "Marcar como completada" si no hay archivos) y al final verifica por la
    API que la entrega quedó en TURNED_IN. Es la única vía para las tareas que el profesor
    creó desde la web, que la API rechaza. Tarda alrededor de un minuto y corre en segundo
    plano sin mostrar ventana (GOOGLE_CLASSROOM_BROWSER_HEADLESS=0 para verla). Con
    turn_in=False solo adjunta. Úsala solo cuando el usuario lo pida explícitamente."""
    paths = [_local_file(f) for f in files or []]
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if not a.email:
        raise ClassroomError(f"La cuenta {a} no tiene correo guardado; vuelve a correr setup para ella.")
    cw = a.classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("No encontré tu entrega para esta tarea (¿es una tarea de tipo ASSIGNMENT y estás inscrito?).")
    if subs[0].get("state") == "TURNED_IN":
        raise ClassroomError("Esta tarea ya está entregada. Si quieres cambiarla, primero anúlala con reclaim_in_browser.")
    if not paths and cw.get("workType") != "ASSIGNMENT":
        raise ClassroomError("Solo sé entregar tareas de tipo ASSIGNMENT por navegador.")

    payload = {
        "alias": a.alias,
        "url": cw["alternateLink"],
        "email": a.email,
        "files": [str(p) for p in paths],
        "turn_in": turn_in,
    }
    browser = _run_browser(payload)

    # La API tarda unos segundos en reflejar lo que hizo el navegador: reintentar un poco.
    import time

    wanted = {p.name for p in paths}
    sub = subs[0]
    for _ in range(8):
        sub = a.my_submissions(course_id, coursework_id)[0]
        have = {att.get("title") for att in ((_submission(sub) or {}).get("attachments") or [])}
        done = (sub.get("state") == "TURNED_IN") if turn_in else wanted <= have
        if done:
            break
        time.sleep(3)

    result: dict[str, Any] = {
        "account": a.alias,
        "course_id": course_id,
        "coursework_id": coursework_id,
        "title": cw.get("title"),
        "url": cw.get("alternateLink"),
        "browser": {k: browser.get(k) for k in ("steps", "screenshot")},
        "turned_in": sub.get("state") == "TURNED_IN",
        "submission": _submission(sub),
    }
    have = {att.get("title") for att in (result["submission"] or {}).get("attachments") or []}
    missing = sorted(wanted - have)
    if missing:
        result["warning"] = f"El navegador dijo que adjuntó {missing} pero la API aún no los muestra; revisa en Classroom."
    elif turn_in and not result["turned_in"]:
        result["warning"] = "El navegador terminó pero la API aún no muestra la entrega como TURNED_IN; revisa en Classroom."
    return result


@mcp.tool()
@_tool
def reclaim_in_browser(coursework_id_or_url: str, course_id: str | None = None, account: str | None = None) -> dict:
    """Anula una entrega ya enviada como lo harías en classroom.google.com ("Anular la
    entrega"): abre Google Chrome con el perfil de esa cuenta, da el botón, confirma y verifica
    por la API que la entrega dejó de estar en TURNED_IN. Los adjuntos se quedan, así que después
    puedes cambiarlos o volver a entregar con submit_in_browser. Es la vía para las tareas que el
    profesor creó desde la web, donde reclaim_submission falla. Úsala solo cuando el usuario lo
    pida explícitamente."""
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if not a.email:
        raise ClassroomError(f"La cuenta {a} no tiene correo guardado; vuelve a correr setup para ella.")
    cw = a.classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("No encontré tu entrega para esta tarea.")
    if subs[0].get("state") != "TURNED_IN":
        raise ClassroomError(f"Esta tarea no está entregada (estado {subs[0].get('state')}), no hay nada que anular.")

    browser = _run_browser({"alias": a.alias, "url": cw["alternateLink"], "email": a.email}, cmd="reclaim")

    # Igual que al entregar, la API tarda unos segundos en reflejar el cambio.
    import time

    sub = subs[0]
    for _ in range(8):
        sub = a.my_submissions(course_id, coursework_id)[0]
        if sub.get("state") != "TURNED_IN":
            break
        time.sleep(3)

    result: dict[str, Any] = {
        "account": a.alias,
        "course_id": course_id,
        "coursework_id": coursework_id,
        "title": cw.get("title"),
        "url": cw.get("alternateLink"),
        "browser": {k: browser.get(k) for k in ("steps", "screenshot")},
        "reclaimed": sub.get("state") != "TURNED_IN",
        "submission": _submission(sub),
    }
    if not result["reclaimed"]:
        result["warning"] = "El navegador terminó pero la API aún muestra la entrega como TURNED_IN; revisa en Classroom."
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
CLOUD_INSTRUCTIONS = f"""\
Necesitas un client secret de OAuth de Google Cloud (es gratis, 5 minutos):

  1. Entra a https://console.cloud.google.com y crea un proyecto (p. ej. "classroom-mcp").
  2. APIs y servicios > Biblioteca: habilita "Google Classroom API" y "Google Drive API"
     (Drive es para poder bajar los archivos adjuntos).
  3. APIs y servicios > Pantalla de consentimiento de OAuth (o "Google Auth Platform"):
     tipo de usuario Externo, llena nombre y correo, y en "Usuarios de prueba"
     agrega TODAS las cuentas de Google con las que entras a Classroom.
  4. APIs y servicios > Credenciales > Crear credenciales > ID de cliente de OAuth:
     tipo de aplicación "Aplicación de escritorio". Descarga el JSON.
  5. Recomendado: en Google Auth Platform > Audiencia dale "Publicar app". En estado
     "Prueba" Google caduca la autorización cada 7 días.
  6. Vuelve a correr:  google-classroom-mcp setup ~/Downloads/client_secret_XXXX.json

El archivo se copiará a {CLIENT_SECRET_FILE}. El mismo client secret sirve para todas
tus cuentas: corre `setup` una vez por cuenta.
"""


def _print_registration() -> None:
    print("\nSi aún no lo hiciste, registra el servidor en Claude Code:")
    custom_dir = os.environ.get("GOOGLE_CLASSROOM_MCP_CONFIG_DIR")
    env_flag = f"-e GOOGLE_CLASSROOM_MCP_CONFIG_DIR={custom_dir} " if custom_dir else ""
    print(
        f"  claude mcp add google-classroom -s user {env_flag}-- "
        "uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp"
    )


def _setup(argv: list[str]) -> int:
    """Guarda el client secret, abre el navegador para autorizar una cuenta y guarda su token."""
    alias_opt: str | None = None
    hint: str | None = None
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--as", "--alias") and i + 1 < len(argv):
            alias_opt = argv[i + 1]
            i += 2
        elif argv[i] in ("--hint", "--email") and i + 1 < len(argv):
            hint = argv[i + 1].strip()
            i += 2
        else:
            rest.append(argv[i])
            i += 1

    if rest:
        src = Path(rest[0]).expanduser()
        if not src.is_file():
            print(f"No existe el archivo {src}", file=sys.stderr)
            return 1
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, CLIENT_SECRET_FILE)
        CLIENT_SECRET_FILE.chmod(0o600)
        print(f"Client secret copiado a {CLIENT_SECRET_FILE}")

    if not CLIENT_SECRET_FILE.is_file():
        print(f"No encuentro {CLIENT_SECRET_FILE}\n", file=sys.stderr)
        print(CLOUD_INSTRUCTIONS, file=sys.stderr)
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    existing = accounts.aliases()
    if existing:
        print("Cuentas ya configuradas: " + ", ".join(repr(accounts.get(a)) for a in existing))
        print("Vas a agregar otra (o renovar una). En el navegador elige la cuenta de Google correspondiente.\n")
    print(
        "Se va a abrir el navegador para que autorices el acceso a Classroom, la lectura de tus archivos "
        "de Drive y subir tus entregas a Drive."
    )
    print("Si Google dice que la app no está verificada, elige 'Continuar' (la app es tuya).\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
    # Con --hint, Google va directo a esa cuenta (o pide iniciar sesión con ella). Sin hint,
    # select_account obliga a mostrar el selector aunque solo haya una sesión abierta; si no,
    # con varias cuentas Google autoriza la que está abierta sin preguntar.
    if hint:
        print(f"Cuenta esperada: {hint}")
        creds = flow.run_local_server(port=0, prompt="consent", login_hint=hint)
    else:
        creds = flow.run_local_server(port=0, prompt="select_account consent")

    # Identificar la cuenta para nombrar el archivo.
    from googleapiclient.discovery import build

    profile = build("classroom", "v1", credentials=creds, cache_discovery=False).userProfiles().get(userId="me").execute()
    email = profile.get("emailAddress") or profile.get("id")
    name = (profile.get("name") or {}).get("fullName")
    if hint and (email or "").lower() != hint.lower():
        print(
            f"\nAutorizaste con {email}, pero pediste {hint}. No guardo nada.\n"
            "Vuelve a correr setup y en el navegador elige esa cuenta (o 'Usar otra cuenta' e inicia sesión con ella).",
            file=sys.stderr,
        )
        return 1
    alias = _safe_alias(alias_opt or email)

    # Si esta cuenta ya estaba con otro alias, actualizar ese archivo en vez de duplicar.
    for a in existing:
        acc = accounts.get(a)
        if acc.email == email and alias_opt is None:
            alias = a
            break

    acc = Account(alias, ACCOUNTS_DIR / f"{alias}.json")
    acc.save(creds, email=email, name=name)
    accounts.reset()
    print(f"\nCuenta guardada como '{alias}' en {acc.path}")
    print(f"Conectado como {name} ({email}).")

    courses = list_courses(account=alias)
    if isinstance(courses, list):
        print(f"Cursos activos: {len(courses)}")
        for c in courses:
            print(f"  - {c['name']}" + (f" ({c['section']})" if c.get("section") else ""))
    else:
        print(courses, file=sys.stderr)

    print("\nPara agregar otra cuenta de Google, vuelve a correr `google-classroom-mcp setup`.")
    _print_registration()
    return 0


def _list_accounts_cli() -> int:
    aliases = accounts.aliases()
    if not aliases:
        print("No hay cuentas configuradas. Corre `google-classroom-mcp setup`.")
        return 1
    for a in aliases:
        acc = accounts.get(a)
        print(f"{a}\t{acc.email or '?'}\t{acc.name or ''}")
    return 0


def _remove_account(argv: list[str]) -> int:
    if not argv:
        print("Uso: google-classroom-mcp remove ALIAS", file=sys.stderr)
        return 1
    try:
        acc = accounts.find(argv[0])
    except ClassroomError as e:
        print(e, file=sys.stderr)
        return 1
    acc.path.unlink(missing_ok=True)
    print(f"Cuenta {acc} eliminada ({acc.path}).")
    return 0


def main() -> None:
    import logging

    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "setup":
        sys.exit(_setup(sys.argv[2:]))
    if cmd == "accounts":
        sys.exit(_list_accounts_cli())
    if cmd == "remove":
        sys.exit(_remove_account(sys.argv[2:]))
    if cmd in ("browser-login", "browser-status", "browser-submit", "browser-reclaim"):
        from . import browser

        browser.main([cmd.removeprefix("browser-"), *sys.argv[2:]])
        return
    if cmd in ("check", "--check"):
        result = get_profile()
        print(json.dumps(result, indent=2, ensure_ascii=False) if isinstance(result, list) else result)
        ok = isinstance(result, list) and all("error" not in r for r in result)
        sys.exit(0 if ok else 1)
    if cmd in ("-h", "--help", "help"):
        print(__doc__)
        return
    mcp.run()


if __name__ == "__main__":
    main()
