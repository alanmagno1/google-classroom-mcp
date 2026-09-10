"""Servidor MCP para Google Classroom (vista de alumno), con varias cuentas.

Expone herramientas para consultar cursos, tareas y materiales, el estado de tus
entregas y calificaciones, anuncios, descargar adjuntos de Drive y entregar tareas
(subir archivos a Drive, adjuntarlos a la entrega, entregar y retirar la entrega).

Varias cuentas: corre `setup` una vez por cuenta de Google. Cada herramienta acepta
un parámetro `account` opcional (alias o correo). Si lo omites y hay una sola cuenta
se usa esa; con varias, list_courses y list_pending_assignments consultan todas y las
demás herramientas ubican la cuenta a partir del curso.

Aviso sobre entregas: la API de Google solo permite adjuntar o entregar desde la
misma aplicación que creó la tarea. Con tareas creadas por el profesor desde la web
de Classroom, Google responde 403 (@ProjectPermissionDenied). En ese caso el archivo
ya quedó en tu Drive y solo falta adjuntarlo desde la web.

Configuración:
  ~/.config/google-classroom-mcp/client_secret.json      credenciales OAuth (Google Cloud Console)
  ~/.config/google-classroom-mcp/accounts/<alias>.json   token de cada cuenta (lo genera `setup`)

Comandos:
  google-classroom-mcp                                   arranca el servidor MCP (stdio)
  google-classroom-mcp setup [client.json] [--as ALIAS]  guarda el client secret y autoriza una cuenta
  google-classroom-mcp accounts                          lista las cuentas configuradas
  google-classroom-mcp remove ALIAS                      quita una cuenta
  google-classroom-mcp check                             verifica la conexión de todas las cuentas
"""

from __future__ import annotations

import base64
import functools
import inspect
import io
import json
import os
import re
import shutil
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
DOWNLOAD_DIR = Path(
    os.environ.get("GOOGLE_CLASSROOM_DOWNLOAD_DIR", Path.home() / "Downloads" / "google-classroom-mcp")
)

SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.profile.emails",
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]

PENDING_STATES = ("NEW", "CREATED", "RECLAIMED_BY_STUDENT")

# Formatos de exportación para archivos nativos de Google (Docs, Sheets...).
EXPORT_FORMATS = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.drawing": ("image/png", ".png"),
    "application/vnd.google-apps.script": ("application/vnd.google-apps.script+json", ".json"),
    "application/vnd.google-apps.jam": ("application/pdf", ".pdf"),
}

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
        "no puede deducir la cuenta, te lo dirá. submit_assignment y reclaim_submission modifican "
        "la entrega: úsalas solo cuando el usuario lo pida explícitamente."
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
    """Acepta un id de Drive o una URL (…/d/ID/…, …?id=ID, …/file/d/ID)."""
    value = value.strip()
    if not value.startswith("http"):
        return value
    u = urlparse(value)
    qs = parse_qs(u.query)
    if "id" in qs:
        return qs["id"][0]
    parts = [p for p in u.path.split("/") if p]
    for i, p in enumerate(parts[:-1]):
        if p == "d":
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
    return f"Error de la API de Google [{status}]: {msg}"


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

    def __repr__(self) -> str:
        return f"{self.alias} ({self.email})" if self.email and self.email != self.alias else self.alias

    # --- credenciales ------------------------------------------------------
    def _load_credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        data = json.loads(self.path.read_text())
        info = data.get("credentials", data)
        creds = Credentials.from_authorized_user_info(info, SCOPES)
        if creds.scopes and not set(SCOPES) <= set(creds.scopes):
            raise ClassroomError(
                f"El token de la cuenta {self} no tiene todos los permisos que necesita esta versión. "
                f"Vuelve a correr `google-classroom-mcp setup --as {self.alias}`."
            )
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


@mcp.tool()
@_tool
def download_file(
    drive_id_or_url: str, filename: str | None = None, export_mime_type: str | None = None, account: str | None = None
) -> str:
    """Descarga un archivo de Google Drive (drive_id o url devueltos por otras herramientas)
    y devuelve la ruta local. Los archivos nativos de Google se exportan: Docs y Slides a
    PDF, Sheets a CSV (o al export_mime_type que indiques). Con varias cuentas, si no
    indicas account se prueba con cada una. Luego léelo con Read."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaIoBaseDownload

    file_id = _drive_id(drive_id_or_url)
    candidates = accounts.selection(account)
    meta = None
    a = candidates[0]
    last_error: Exception | None = None
    for a in candidates:
        try:
            meta = a.drive.files().get(fileId=file_id, fields="name,mimeType,size", supportsAllDrives=True).execute()
            break
        except HttpError as e:
            last_error = e
            if e.resp.status not in (403, 404):
                raise
    if meta is None:
        raise ClassroomError(f"Ninguna cuenta puede leer el archivo {file_id}: {_http_error_message(last_error)}")

    name = filename or meta.get("name") or file_id
    mime = meta.get("mimeType", "")
    if mime.startswith("application/vnd.google-apps"):
        export_mime, ext = EXPORT_FORMATS.get(mime, ("application/pdf", ".pdf"))
        if export_mime_type:
            export_mime, ext = export_mime_type, ""
        if ext and not name.lower().endswith(ext):
            name += ext
        request = a.drive.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = a.drive.files().get_media(fileId=file_id, supportsAllDrives=True)

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = DOWNLOAD_DIR / name.replace("/", "_")
    with io.FileIO(dest, "wb") as buf:
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return str(dest)


# --------------------------------------------------------------------------- #
# Herramientas de entrega
# --------------------------------------------------------------------------- #
def _upload_to_drive(a: Account, path: str, folder_name: str | None = None) -> dict:
    from googleapiclient.http import MediaFileUpload

    p = Path(path).expanduser()
    if not p.is_file():
        raise ClassroomError(f"No existe el archivo {p}")
    body: dict[str, Any] = {"name": p.name}
    if folder_name:
        body["parents"] = [_ensure_folder(a, folder_name)]
    media = MediaFileUpload(str(p), resumable=p.stat().st_size > 5 * 1024 * 1024)
    f = a.drive.files().create(body=body, media_body=media, fields="id,name,webViewLink,mimeType,size").execute()
    return {"drive_id": f["id"], "title": f.get("name"), "url": f.get("webViewLink"), "size": f.get("size")}


def _ensure_folder(a: Account, name: str) -> str:
    q = f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' and 'root' in parents and trashed = false"
    found = a.drive.files().list(q=q, fields="files(id)", pageSize=1).execute().get("files", [])
    if found:
        return found[0]["id"]
    f = a.drive.files().create(body={"name": name, "mimeType": "application/vnd.google-apps.folder"}, fields="id").execute()
    return f["id"]


@mcp.tool()
@_tool
def upload_to_drive(path: str, account: str | None = None, folder: str | None = "Classroom MCP") -> dict:
    """Sube un archivo local a tu Google Drive (por default a la carpeta "Classroom MCP")
    y devuelve drive_id y enlace. Sirve para luego adjuntarlo a una entrega con
    submit_assignment(drive_ids=[...]) o para adjuntarlo a mano desde la web."""
    a = accounts.resolve(account)
    return {"account": a.alias, **_upload_to_drive(a, path, folder)}


@mcp.tool()
@_tool
def submit_assignment(
    coursework_id_or_url: str,
    course_id: str | None = None,
    file_paths: list[str] | None = None,
    drive_ids: list[str] | None = None,
    links: list[str] | None = None,
    turn_in: bool = False,
    account: str | None = None,
) -> dict:
    """Adjunta archivos a tu entrega de una tarea y, si turn_in=True, la entrega.
    file_paths son archivos locales (se suben primero a tu Drive), drive_ids archivos que
    ya están en Drive, links URLs. Úsala solo cuando el usuario lo pida explícitamente.

    Aviso: Google solo permite adjuntar y entregar desde la app que creó la tarea. Si el
    profesor la creó desde la web de Classroom, el paso de adjuntar/entregar devuelve
    403 @ProjectPermissionDenied; los archivos quedan subidos a tu Drive de todas formas."""
    from googleapiclient.errors import HttpError

    if not (file_paths or drive_ids or links or turn_in):
        raise ClassroomError("Indica file_paths, drive_ids, links o turn_in=True.")
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)

    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("No encontré tu entrega para esta tarea (¿es una tarea de tipo ASSIGNMENT y estás inscrito?).")
    sub = subs[0]
    result: dict[str, Any] = {"account": a.alias, "course_id": course_id, "coursework_id": coursework_id}

    uploaded = [_upload_to_drive(a, p, "Classroom MCP") for p in file_paths or []]
    if uploaded:
        result["uploaded_to_drive"] = uploaded

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
# CLI
# --------------------------------------------------------------------------- #
CLOUD_INSTRUCTIONS = f"""\
Necesitas un client secret de OAuth de Google Cloud (es gratis, 5 minutos):

  1. Entra a https://console.cloud.google.com y crea un proyecto (p. ej. "classroom-mcp").
  2. APIs y servicios > Biblioteca: habilita "Google Classroom API" y "Google Drive API".
  3. APIs y servicios > Pantalla de consentimiento de OAuth (o "Google Auth Platform"):
     tipo de usuario Externo, llena nombre y correo, y en "Usuarios de prueba"
     agrega TODAS las cuentas de Google con las que entras a Classroom.
  4. APIs y servicios > Credenciales > Crear credenciales > ID de cliente de OAuth:
     tipo de aplicación "Aplicación de escritorio". Descarga el JSON.
  5. Vuelve a correr:  google-classroom-mcp setup ~/Downloads/client_secret_XXXX.json

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
    rest: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in ("--as", "--alias") and i + 1 < len(argv):
            alias_opt = argv[i + 1]
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
    print("Se va a abrir el navegador para que autorices el acceso a Classroom y Drive.")
    print("Si Google dice que la app no está verificada, elige 'Continuar' (la app es tuya).\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")

    # Identificar la cuenta para nombrar el archivo.
    from googleapiclient.discovery import build

    profile = build("classroom", "v1", credentials=creds, cache_discovery=False).userProfiles().get(userId="me").execute()
    email = profile.get("emailAddress") or profile.get("id")
    name = (profile.get("name") or {}).get("fullName")
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
