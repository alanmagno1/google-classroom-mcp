"""MCP server for Google Classroom (student view), with several accounts.

Exposes tools to look up courses, coursework and materials, the state of your
submissions and grades, announcements, and to submit coursework (attach Drive files
or links to the submission, turn it in and reclaim it).

It asks for Classroom permissions and read access to Drive: attachments of coursework,
materials and announcements can be downloaded to disk with download_file /
download_assignment_files (Google Docs, Sheets and Slides are exported to PDF, xlsx,
etc.). With the optional drive.file permission, upload_file and
submit_assignment(files=...) upload local files to the "Classroom Submissions" folder
of your Drive so they can be attached to a submission.

Several accounts: run `setup` once per Google account. Every tool accepts an optional
`account` parameter (alias or email). If you omit it and there is a single account,
that one is used. With several, list_courses and list_pending_assignments query all of
them and the other tools find the account from the course.

Note on submissions: the Google API only allows attaching or turning in from the same
application that created the coursework. For coursework the teacher created from the
Classroom web UI, Google answers 403 (@ProjectPermissionDenied). For those,
submit_in_browser turns the work in by driving Google Chrome (Playwright) on a
per-account profile where you signed in once with `browser-login <alias>`, and
reclaim_in_browser unsubmits it the same way.

Configuration:
  ~/.config/google-classroom-mcp/client_secret.json      OAuth credentials (Google Cloud Console)
  ~/.config/google-classroom-mcp/accounts/<alias>.json   token of each account (created by `setup`)
  ~/Downloads/google-classroom-mcp/                      downloads (GOOGLE_CLASSROOM_DOWNLOAD_DIR)

Commands:
  google-classroom-mcp                                   starts the MCP server (stdio)
  google-classroom-mcp setup [client.json] [--as ALIAS] [--hint EMAIL]
                                                         saves the client secret and authorizes an account
                                                         (--hint: require that it is this email)
  google-classroom-mcp accounts                          lists the configured accounts
  google-classroom-mcp remove ALIAS                      removes an account
  google-classroom-mcp check                             checks the connection of every account
  google-classroom-mcp browser-login ALIAS [--email X]  opens Chrome to sign in to that account's profile
  google-classroom-mcp browser-status                    session of each browser profile
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

# Minimum permissions: without them no tool works.
CORE_SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.courseworkmaterials.readonly",
    "https://www.googleapis.com/auth/classroom.announcements.readonly",
    "https://www.googleapis.com/auth/classroom.topics.readonly",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    "https://www.googleapis.com/auth/classroom.profile.emails",
    # Drive read-only: to download attachments (Google does not serve them through the Classroom API).
    "https://www.googleapis.com/auth/drive.readonly",
]
# Optional: create files in Drive (it only sees the ones this app creates). Used by upload_file and
# submit_assignment(files=...). A token authorized without it keeps working for everything else.
UPLOAD_SCOPE = "https://www.googleapis.com/auth/drive.file"
SCOPES = [*CORE_SCOPES, UPLOAD_SCOPE]
UPLOAD_FOLDER = "Classroom Submissions"
FOLDER_MIME = "application/vnd.google-apps.folder"

# Google-native files (Docs, Sheets, Slides...) have no binary: they are exported.
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
    "Google only allows attaching or turning in from the application that created the coursework "
    "(@ProjectPermissionDenied). The teacher created this one from the Classroom web UI, "
    "so it has to be submitted from classroom.google.com."
)

mcp = MCPServer(
    "google-classroom",
    instructions=(
        "Tools for Google Classroom with one or several Google accounts. To find out which "
        "coursework is still pending use list_pending_assignments (it checks every account). For a URL "
        "like classroom.google.com/c/XXX/a/YYY/details use get_assignment with the full URL. The "
        "account parameter (alias or email) is optional. If there are several accounts and a tool "
        "cannot work out the account, it will tell you. To download the attachments of an assignment use "
        "download_assignment_files (or download_file with a drive_id or Drive URL). They return local "
        "paths you can read with Read. To submit an assignment use submit_in_browser(files=[path]): it "
        "drives Chrome with the user's session, attaches, clicks Turn in and verifies through the API (it "
        "is the only way for coursework the teacher created from the web UI, which the API rejects with "
        "@ProjectPermissionDenied). submit_assignment tries the API route and, with files=, leaves the file "
        "in Drive. To unsubmit work that was already turned in use reclaim_in_browser (reclaim_submission "
        "is the API route and fails with that same coursework). upload_file, submit_assignment, "
        "submit_in_browser, reclaim_submission and reclaim_in_browser create files or modify the "
        "submission: use them only when the user explicitly asks."
    ),
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class ClassroomError(Exception):
    pass


def _fmt_dt(value: str | None) -> str | None:
    """Converts an RFC3339 timestamp from the API to local time."""
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
    """Due date of an assignment. Classroom stores it in UTC as separate date + time."""
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
    """IDs in Classroom URLs are base64. The API ones are numeric."""
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
    """Extracts course_id and coursework_id/material_id from a Classroom URL, if present."""
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
    """Accepts a Drive id or a URL (…/d/ID/…, …?id=ID, …/file/d/ID, …/drive/folders/ID)."""
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
    raise ClassroomError(f"Could not extract a Drive id from: {value}")


def _http_error_message(e: Exception) -> str:
    """Readable message for a googleapiclient HttpError."""
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
        msg += " (run `google-classroom-mcp setup` again to renew that account's permissions)"
    elif e.resp.status == 403 and "has not been used in project" in msg:
        msg += (
            " (enable that API in Google Cloud: APIs & Services > Library, in the same project as the client secret)"
        )
    return f"Google API error [{status}]: {msg}"


def _safe_filename(name: str, fallback: str = "file") -> str:
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
# Accounts and Google client
# --------------------------------------------------------------------------- #
def _safe_alias(alias: str) -> str:
    alias = re.sub(r"[^A-Za-z0-9._@+-]+", "_", alias.strip())
    if not alias:
        raise ClassroomError("The account alias cannot be empty.")
    return alias


def _granted_scopes(scopes, who: str, alias: str) -> set[str]:
    """Permissions a token was authorized with. Fails if any of the minimum ones is missing."""
    granted = set(scopes or CORE_SCOPES)
    missing = set(CORE_SCOPES) - granted
    if missing:
        short = ", ".join(sorted(s.rsplit("/", 1)[1] for s in missing))
        raise ClassroomError(
            f"The token of account {who} does not have all the permissions this version needs "
            f"(missing: {short}). Run `google-classroom-mcp setup --as {alias}` again."
        )
    return granted


class Account:
    """An authorized Google account: credentials, services and profile."""

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

    # --- credentials -------------------------------------------------------
    def _load_credentials(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        data = json.loads(self.path.read_text())
        info = data.get("credentials", data)
        # Without passing SCOPES: the token loads with the permissions it was authorized with. If
        # the current ones were requested and the token came from an older version, Google would
        # refuse to refresh it and even the tools that do not need the new scope would stop working.
        creds = Credentials.from_authorized_user_info(info)
        self.granted_scopes = _granted_scopes(creds.scopes, repr(self), self.alias)
        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception as e:  # noqa: BLE001
                    raise ClassroomError(
                        f"Could not refresh the session of {self} ({e}). "
                        f"Run `google-classroom-mcp setup --as {self.alias}` again."
                    ) from e
                self.save(creds)
            else:
                raise ClassroomError(
                    f"The session of {self} expired. Run `google-classroom-mcp setup --as {self.alias}` again."
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
        self.creds  # noqa: B018  (loads granted_scopes)
        return UPLOAD_SCOPE in self.granted_scopes

    def require_upload(self) -> None:
        if not self.can_upload:
            hint = f" --hint {self.email}" if self.email else ""
            raise ClassroomError(
                f"Account {self} was authorized without the permission to upload files to Drive. "
                f"Run `google-classroom-mcp setup --as {self.alias}{hint}` and try again."
            )

    # --- helpers -----------------------------------------------------------
    @staticmethod
    def paged(request_fn, key: str, **params: Any) -> list[dict]:
        """Walks through every page of an API list method."""
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
        """Metadata of a Drive file, or None if this account cannot see it."""
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
                "No Google account is configured. Run `google-classroom-mcp setup` "
                f"(it looks for the client secret at {CLIENT_SECRET_FILE})."
            )
        return [self.get(a) for a in aliases]

    def find(self, account: str) -> Account:
        """Looks up by exact alias, email, or a case-insensitive partial match."""
        accounts = self.all()
        needle = account.strip().lower()
        for a in accounts:
            if a.alias.lower() == needle or (a.email or "").lower() == needle:
                return a
        partial = [a for a in accounts if needle in a.alias.lower() or needle in (a.email or "").lower()]
        if len(partial) == 1:
            return partial[0]
        raise ClassroomError(
            f"No account matches '{account}'. Available accounts: "
            + ", ".join(repr(a) for a in accounts)
        )

    def resolve(self, account: str | None, course_id: str | None = None) -> Account:
        """Picks the account: the one given, the only one there is, or the one that has the course."""
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
            msg = f"No account has access to course {course_id}. Accounts: " + ", ".join(repr(a) for a in accounts)
            if problems:
                msg += ". Also: " + " | ".join(problems)
            raise ClassroomError(msg)
        raise ClassroomError(
            "Several accounts are configured. Pass account=<alias or email>. Accounts: "
            + ", ".join(repr(a) for a in accounts)
        )

    def selection(self, account: str | None) -> list[Account]:
        """For tools that can go through every account."""
        return [self.find(account)] if account else self.all()

    def resolve_drive_file(self, account: str | None, file_id: str) -> tuple[Account, dict]:
        """The account that can see the Drive file, and its metadata."""
        candidates = [self.find(account)] if account else self.all()
        for a in candidates:
            meta = a.drive_file(file_id)
            if meta is not None:
                return a, meta
        who = repr(candidates[0]) if len(candidates) == 1 else ", ".join(repr(a) for a in candidates)
        raise ClassroomError(f"Drive file {file_id} does not exist or the account has no access ({who}).")

    def reset(self) -> None:
        self._cache.clear()
        self._course_owner.clear()


accounts = Accounts()


def _tool(fn):
    """Wraps a tool so it returns readable errors instead of tracebacks.

    Removes the return annotation so MCP does not validate the result against a
    strict schema (an error is a str, not a dict or a list)."""

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
# Query tools
# --------------------------------------------------------------------------- #
@mcp.tool()
@_tool
def list_accounts() -> list[dict]:
    """Configured Google accounts (alias and email). No API call."""
    return [{"account": a.alias, "email": a.email, "name": a.name} for a in accounts.all()]


@mcp.tool()
@_tool
def get_profile(account: str | None = None) -> list[dict]:
    """Checks the connection and returns the authenticated user of each account (or of the given one)."""
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
    """Classroom courses you are enrolled in as a student. Without account it goes
    through every configured account."""
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
    """Coursework you have not turned in yet (state NEW, CREATED or RECLAIMED_BY_STUDENT),
    grouped by account and course and sorted by due date. Without account or course_id it
    checks every active course of every account. Says whether it is already past due."""
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
    """Classwork of a course grouped by topic: assignments (with the state of your
    submission and grade), questions and materials. Accepts the course id or URL."""
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
        sections.append({"topic": topics.get(topic_id) if topic_id else "(no topic)", "items": items})
    sections.sort(key=lambda s: s["topic"] == "(no topic)")

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
        raise ClassroomError("Pass course_id or the full URL of the coursework.")
    return course_id, coursework_id, ids


@mcp.tool()
@_tool
def get_assignment(coursework_id_or_url: str, course_id: str | None = None, account: str | None = None) -> dict:
    """Details of an assignment or question: instructions, due date, points, attached
    materials and the state of your submission (files, grade). Accepts the full URL
    (https://classroom.google.com/c/XXX/a/YYY/details) or the coursework id together with course_id."""
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
    """Announcements from a course stream, newest first."""
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
# Download tools
# --------------------------------------------------------------------------- #
def _dest_folder(dest_dir: str | None, default: Path) -> Path:
    if not dest_dir:
        return default
    p = Path(dest_dir).expanduser()
    return p if p.is_absolute() else DOWNLOAD_DIR / p


def _download_drive_file(
    a: Account, meta: dict, dest_dir: Path, filename: str | None = None, export_format: str | None = None
) -> dict:
    """Downloads a Drive file to disk. Google-native files are exported."""
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
                raise ClassroomError(f"'{meta.get('name')}' is a Drive shortcut and I cannot see the file it points to.")
            return _download_drive_file(a, target_meta, dest_dir, filename, export_format)
        if kind in ("folder", "form", "site", "map", "fusiontable"):
            raise ClassroomError(
                f"'{meta.get('name')}' is a Google {kind} and cannot be downloaded as a file. "
                f"Open it at https://drive.google.com/open?id={file_id}"
            )
        fmt = (export_format or DEFAULT_EXPORT.get(kind, "pdf")).lower().lstrip(".")
        if fmt not in EXPORT_MIME:
            raise ClassroomError(f"Unknown export format: {fmt}. Options: {', '.join(EXPORT_MIME)}")
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
    """Downloads a Drive file (the drive_id or url the other tools return) to
    ~/Downloads/google-classroom-mcp/ (or to dest_dir) and returns the local path, which
    you can then read with Read. Google Docs, Sheets and Slides have no file of their own
    and are exported: Docs and Slides to pdf, Sheets to xlsx, drawings to png.
    export_format changes that (pdf, docx, txt, md, html, xlsx, csv, pptx, png...). Without
    account it tries every account until it finds the one that can see the file."""
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
    """Downloads every Drive attachment of an assignment or material (accepts the full
    Classroom URL or the id together with course_id) to
    ~/Downloads/google-classroom-mcp/<course>/<assignment>/ (or to dest_dir). With
    include_submission=True it also downloads the files of your own submission. Links,
    YouTube videos and forms are not downloaded: they come in not_downloadable with their url.
    Google files are exported the same way as in download_file."""
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
                raise ClassroomError("the account has no access to this file in Drive")
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
# Drive upload
# --------------------------------------------------------------------------- #
def _q(value: str) -> str:
    """Escapes a literal for a Drive API query."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _ensure_folder(a: Account, name: str, parent_id: str | None = None) -> str:
    """Id of folder `name` inside `parent_id` (or the root). Creates it if it does not exist."""
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
    """(id, description) of the target folder: a Drive id/URL, or the name of a
    subfolder inside "Classroom Submissions" (by default, that folder itself)."""
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
        raise ClassroomError(f"File {p} does not exist")
    return p


ATTACH_IN_WEB = (
    "The files are already in your Drive. In Classroom open the assignment, click 'Add or create' > "
    "'Google Drive', pick them (they show up under Recent) and click 'Turn in'."
)


@mcp.tool()
@_tool
def upload_file(path: str, folder: str | None = None, name: str | None = None, account: str | None = None) -> dict:
    """Uploads a local file to the "Classroom Submissions" folder of your Drive (or to the
    subfolder `folder`, for example the course name. It also accepts a Drive folder id or
    URL) and returns its drive_id and url. With those you can call
    submit_assignment(drive_ids=[...]) or attach it from the Classroom web UI. Requires the
    account to have been authorized with the file upload permission (drive.file). Use it only
    when the user explicitly asks."""
    p = _local_file(path)
    a = accounts.resolve(account)
    a.require_upload()
    folder_id, where = _resolve_folder(a, folder)
    return {"account": a.alias, "folder": where, **_upload_to_drive(a, p, folder_id, name)}


# --------------------------------------------------------------------------- #
# Submission tools
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
    """Submits an assignment: uploads the local files in `files` to your Drive (folder
    "Classroom Submissions/<course>"), attaches those plus the ones in `drive_ids` (Drive ids
    or URLs) and/or `links` to your submission and, if turn_in=True, turns it in. Use it only
    when the user explicitly asks.

    Note: Google only allows attaching and turning in through the API from the app that
    created the coursework. If the teacher created it from the Classroom web UI (the usual
    case), attaching returns 403 @ProjectPermissionDenied. The files in `files` are already
    in Drive and the result carries in next_step how to finish from classroom.google.com."""
    from googleapiclient.errors import HttpError

    if not (files or drive_ids or links or turn_in):
        raise ClassroomError("Pass files, drive_ids, links or turn_in=True.")
    paths = [_local_file(f) for f in files or []]
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if paths:
        a.require_upload()

    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("Could not find your submission for this coursework (is it an ASSIGNMENT and are you enrolled?).")
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
        result["turn_in_error"] = "Turn in was not attempted because the attach step failed."

    result["submission"] = _submission(sub)
    return result


@mcp.tool()
@_tool
def reclaim_submission(coursework_id_or_url: str, course_id: str | None = None, account: str | None = None) -> dict:
    """Reclaims a submission that was already turned in (same as "Unsubmit") so it can be
    modified. Same Google restriction as submit_assignment. Use it only if the user asks."""
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("Could not find your submission for this coursework.")
    api = a.classroom.courses().courseWork().studentSubmissions()
    api.reclaim(courseId=course_id, courseWorkId=coursework_id, id=subs[0]["id"], body={}).execute()
    sub = api.get(courseId=course_id, courseWorkId=coursework_id, id=subs[0]["id"]).execute()
    return {"account": a.alias, "reclaimed": True, "submission": _submission(sub)}


# --------------------------------------------------------------------------- #
# Browser submission
# --------------------------------------------------------------------------- #
def _run_browser(payload: dict, timeout: int = 900, cmd: str = "submit") -> dict:
    """Runs the browser flow (`submit` or `reclaim`) in a separate process and returns its final JSON."""
    args = [sys.executable, "-m", "google_classroom_mcp.browser", cmd, json.dumps(payload)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise ClassroomError(f"The browser took more than {timeout // 60} minutes and was cancelled.") from e
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    try:
        out = json.loads(lines[-1]) if lines else {}
    except ValueError:
        out = {}
    if not out:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        raise ClassroomError("The browser finished without a response: " + " | ".join(tail))
    if not out.get("ok"):
        raise ClassroomError(out.get("error") or "unknown failure in the browser")
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
    """Submits an assignment the way you would on classroom.google.com: opens Google Chrome
    with that account's profile (signed in once with `google-classroom-mcp browser-login
    <alias>`), attaches the local files in `files` through "Add or create > File", clicks
    "Turn in" (or "Mark as done" if there are no files) and finally verifies through the
    API that the submission is TURNED_IN. It is the only way for coursework the teacher
    created from the web UI, which the API rejects. It takes about a minute and runs in the
    background without showing a window (GOOGLE_CLASSROOM_BROWSER_HEADLESS=0 to show it).
    With turn_in=False it only attaches. Use it only when the user explicitly asks."""
    paths = [_local_file(f) for f in files or []]
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if not a.email:
        raise ClassroomError(f"Account {a} has no saved email. Run setup again for it.")
    cw = a.classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("Could not find your submission for this coursework (is it an ASSIGNMENT and are you enrolled?).")
    if subs[0].get("state") == "TURNED_IN":
        raise ClassroomError("This coursework is already turned in. To change it, unsubmit it first with reclaim_in_browser.")
    if not paths and cw.get("workType") != "ASSIGNMENT":
        raise ClassroomError("Only coursework of type ASSIGNMENT can be submitted through the browser.")

    payload = {
        "alias": a.alias,
        "url": cw["alternateLink"],
        "email": a.email,
        "files": [str(p) for p in paths],
        "turn_in": turn_in,
    }
    browser = _run_browser(payload)

    # The API takes a few seconds to reflect what the browser did: retry a little.
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
        result["warning"] = f"The browser said it attached {missing} but the API does not show them yet. Check in Classroom."
    elif turn_in and not result["turned_in"]:
        result["warning"] = "The browser finished but the API does not show the submission as TURNED_IN yet. Check in Classroom."
    return result


@mcp.tool()
@_tool
def reclaim_in_browser(coursework_id_or_url: str, course_id: str | None = None, account: str | None = None) -> dict:
    """Unsubmits work that was already turned in, the way you would on classroom.google.com
    ("Unsubmit"): opens Google Chrome with that account's profile, clicks the button, confirms
    and verifies through the API that the submission is no longer TURNED_IN. The attachments
    stay, so afterwards you can change them or turn in again with submit_in_browser. It is the
    way for coursework the teacher created from the web UI, where reclaim_submission fails. Use
    it only when the user explicitly asks."""
    course_id, coursework_id, _ = _locate_coursework(coursework_id_or_url, course_id)
    a = accounts.resolve(account, course_id)
    if not a.email:
        raise ClassroomError(f"Account {a} has no saved email. Run setup again for it.")
    cw = a.classroom.courses().courseWork().get(courseId=course_id, id=coursework_id).execute()
    subs = a.my_submissions(course_id, coursework_id)
    if not subs:
        raise ClassroomError("Could not find your submission for this coursework.")
    if subs[0].get("state") != "TURNED_IN":
        raise ClassroomError(f"This coursework is not turned in (state {subs[0].get('state')}), there is nothing to unsubmit.")

    browser = _run_browser({"alias": a.alias, "url": cw["alternateLink"], "email": a.email}, cmd="reclaim")

    # As when turning in, the API takes a few seconds to reflect the change.
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
        result["warning"] = "The browser finished but the API still shows the submission as TURNED_IN. Check in Classroom."
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
CLOUD_INSTRUCTIONS = f"""\
You need an OAuth client secret from Google Cloud (free, about 5 minutes):

  1. Go to https://console.cloud.google.com and create a project (for example "classroom-mcp").
  2. APIs & Services > Library: enable "Google Classroom API" and "Google Drive API"
     (Drive is needed to download the attachments).
  3. APIs & Services > OAuth consent screen (or "Google Auth Platform"):
     user type External, fill in name and email, and under "Test users"
     add EVERY Google account you use to sign in to Classroom.
  4. APIs & Services > Credentials > Create credentials > OAuth client ID:
     application type "Desktop app". Download the JSON.
  5. Recommended: in Google Auth Platform > Audience click "Publish app". While in
     "Testing" status Google expires the authorization every 7 days.
  6. Run again:  google-classroom-mcp setup ~/Downloads/client_secret_XXXX.json

The file will be copied to {CLIENT_SECRET_FILE}. The same client secret works for all
your accounts: run `setup` once per account.
"""


def _print_registration() -> None:
    print("\nIf you have not done it yet, register the server in Claude Code:")
    custom_dir = os.environ.get("GOOGLE_CLASSROOM_MCP_CONFIG_DIR")
    env_flag = f"-e GOOGLE_CLASSROOM_MCP_CONFIG_DIR={custom_dir} " if custom_dir else ""
    print(
        f"  claude mcp add google-classroom -s user {env_flag}-- "
        "uvx --from git+https://github.com/AlanMagno1/google-classroom-mcp google-classroom-mcp"
    )


def _setup(argv: list[str]) -> int:
    """Saves the client secret, opens the browser to authorize an account and saves its token."""
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
            print(f"File {src} does not exist", file=sys.stderr)
            return 1
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, CLIENT_SECRET_FILE)
        CLIENT_SECRET_FILE.chmod(0o600)
        print(f"Client secret copied to {CLIENT_SECRET_FILE}")

    if not CLIENT_SECRET_FILE.is_file():
        print(f"Cannot find {CLIENT_SECRET_FILE}\n", file=sys.stderr)
        print(CLOUD_INSTRUCTIONS, file=sys.stderr)
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    existing = accounts.aliases()
    if existing:
        print("Accounts already configured: " + ", ".join(repr(accounts.get(a)) for a in existing))
        print("You are about to add another one (or renew one). In the browser pick the matching Google account.\n")
    print(
        "The browser will open so you can authorize access to Classroom, reading your Drive files "
        "and uploading your submissions to Drive."
    )
    print("If Google says the app is not verified, choose 'Continue' (the app is yours).\n")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_FILE), SCOPES)
    # With --hint, Google goes straight to that account (or asks to sign in with it). Without a
    # hint, select_account forces the chooser even if only one session is open. Otherwise, with
    # several accounts Google authorizes the one that is open without asking.
    if hint:
        print(f"Expected account: {hint}")
        creds = flow.run_local_server(port=0, prompt="consent", login_hint=hint)
    else:
        creds = flow.run_local_server(port=0, prompt="select_account consent")

    # Identify the account to name the file.
    from googleapiclient.discovery import build

    profile = build("classroom", "v1", credentials=creds, cache_discovery=False).userProfiles().get(userId="me").execute()
    email = profile.get("emailAddress") or profile.get("id")
    name = (profile.get("name") or {}).get("fullName")
    if hint and (email or "").lower() != hint.lower():
        print(
            f"\nYou authorized with {email}, but asked for {hint}. Nothing was saved.\n"
            "Run setup again and pick that account in the browser (or 'Use another account' and sign in with it).",
            file=sys.stderr,
        )
        return 1
    alias = _safe_alias(alias_opt or email)

    # If this account already existed under another alias, update that file instead of duplicating.
    for a in existing:
        acc = accounts.get(a)
        if acc.email == email and alias_opt is None:
            alias = a
            break

    acc = Account(alias, ACCOUNTS_DIR / f"{alias}.json")
    acc.save(creds, email=email, name=name)
    accounts.reset()
    print(f"\nAccount saved as '{alias}' at {acc.path}")
    print(f"Connected as {name} ({email}).")

    courses = list_courses(account=alias)
    if isinstance(courses, list):
        print(f"Active courses: {len(courses)}")
        for c in courses:
            print(f"  - {c['name']}" + (f" ({c['section']})" if c.get("section") else ""))
    else:
        print(courses, file=sys.stderr)

    print("\nTo add another Google account, run `google-classroom-mcp setup` again.")
    _print_registration()
    return 0


def _list_accounts_cli() -> int:
    aliases = accounts.aliases()
    if not aliases:
        print("No accounts configured. Run `google-classroom-mcp setup`.")
        return 1
    for a in aliases:
        acc = accounts.get(a)
        print(f"{a}\t{acc.email or '?'}\t{acc.name or ''}")
    return 0


def _remove_account(argv: list[str]) -> int:
    if not argv:
        print("Usage: google-classroom-mcp remove ALIAS", file=sys.stderr)
        return 1
    try:
        acc = accounts.find(argv[0])
    except ClassroomError as e:
        print(e, file=sys.stderr)
        return 1
    acc.path.unlink(missing_ok=True)
    print(f"Account {acc} removed ({acc.path}).")
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
