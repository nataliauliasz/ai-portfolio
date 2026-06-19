import copy
import hashlib
import html
import io
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, render_template, request, session
from openai import OpenAI
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from app.env_utils import get_app_env, is_production_env, load_local_env_file, parse_bool_env, require_http_or_https_url
except ModuleNotFoundError:
    from env_utils import get_app_env, is_production_env, load_local_env_file, parse_bool_env, require_http_or_https_url

load_local_env_file()

try:
    from app.db import AUTH_SESSION_DB_PATH, LESSONS_LEARNED_DB_PATH, PEOPLE_DB_PATH, PEOPLE_SEED_PATH, TEMPLATES_DIR
except ModuleNotFoundError:
    from db import AUTH_SESSION_DB_PATH, LESSONS_LEARNED_DB_PATH, PEOPLE_DB_PATH, PEOPLE_SEED_PATH, TEMPLATES_DIR

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

app = Flask(__name__, template_folder=TEMPLATES_DIR)

if parse_bool_env(os.environ.get("TRUST_PROXY_HEADERS"), default=False):
    # Only enable this behind a trusted reverse proxy that sets X-Forwarded-* correctly.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1, x_port=1)

# =========================
# Environment defaults
# =========================
APP_ENV = get_app_env()
IS_PRODUCTION = is_production_env()
JAZZ_BASE_URL = require_http_or_https_url("JAZZ_BASE_URL", "https://jazz.example.internal/", trailing_slash=True)
CCM_APP_URL = require_http_or_https_url("CCM_APP_URL", urljoin(JAZZ_BASE_URL, "ccm/"), trailing_slash=True)
OSLC_CATALOG_URL = require_http_or_https_url(
    "OSLC_CATALOG_URL",
    urljoin(CCM_APP_URL, "oslc/workitems/catalog"),
    trailing_slash=False,
)
JTS_APP_URL = require_http_or_https_url("JTS_APP_URL", urljoin(JAZZ_BASE_URL, "jts/"), trailing_slash=True)
RS_BASE_URL = require_http_or_https_url("RS_BASE_URL", JAZZ_BASE_URL.rstrip("/"), trailing_slash=False)
RS_REPORT_ID_COMBINED = "135123"
RS_WIDGET_URL_TMPL = RS_BASE_URL + "/rs/widget/content?lang=en&country=US&reportID={rid}&allowExport=true"
RS_PARTIAL_URL_TMPL = RS_BASE_URL + "/rs/reportdefinition/{rid}/view/partial?pageIndex={page}&maxRows={rows}&gadgetMode=false&viz=reportvisualization-table&lqeQueryTracing=0&init=true"
RS_EXPECTED_TABLE_HEADERS = ["project", "type", "owner", "creator", "work item id", "work item"]
LESSONS_LEARNED_PROJECT_NAME = "Lessons Learned"
LESSONS_LEARNED_WORK_ITEM_TYPE = "LL Candidate"
LESSONS_LEARNED_STATUS_CANDIDATE = "candidate"
LESSONS_LEARNED_STATUS_EXCLUDED = "excluded"

CREATED_AFTER = datetime(2025, 1, 1, tzinfo=timezone.utc)
PAGE_SIZE_DEFAULT = 200
MAX_PAGES_DEFAULT = 50
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")
OPENAI_MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "35000"))
DEFAULT_VERIFY_SSL = parse_bool_env(os.environ.get("VERIFY_SSL"), default=True)
PEOPLE_MANAGEMENT_ALLOWED_USERNAMES = {
    "example_admin",
    "quality_lead",
    "engineering_manager",
    "review_owner"
}
ANALYSIS_ALLOWED_USERNAMES = set(PEOPLE_MANAGEMENT_ALLOWED_USERNAMES)

DEPARTMENT_MANAGERS = {
    "DD": "Department Manager DD",
    "DEC": "Department Manager DEC",
    "DEF": "Department Manager DEF",
    "DEPL": "Department Manager DEPL",
    "DER1": "Department Manager DER1",
    "DER2": "Department Manager DER2",
    "DEM1": "Department Manager DEM1",
    "DEM2": "Department Manager DEM2",
    "DEM3": "Department Manager DEM3",
    "DEL": "Department Manager DEL"
}


# XML namespace OSLC
NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "oslc": "http://open-services.net/ns/core#",
}


def parse_bool(v, default=True) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def read_secret_env(secret_name: str) -> str:
    secret_file = (os.environ.get(f"{secret_name}_FILE") or "").strip()
    if secret_file:
        try:
            with open(secret_file, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise RuntimeError(f"Nie udalo sie odczytac pliku sekretu {secret_name}_FILE: {exc}") from exc

    return (os.environ.get(secret_name) or "").strip()


def get_flask_secret_key() -> str:
    secret_key = ""
    secret_key_file = (os.environ.get("FLASK_SECRET_KEY_FILE") or "").strip()
    if secret_key_file:
        try:
            secret_key = read_secret_env("FLASK_SECRET_KEY")
        except RuntimeError:
            if IS_PRODUCTION:
                raise
    if not secret_key:
        secret_key = (os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY") or "").strip()
    if secret_key:
        return secret_key
    if IS_PRODUCTION:
        raise RuntimeError("Brak FLASK_SECRET_KEY lub FLASK_SECRET_KEY_FILE dla srodowiska produkcyjnego.")
    return secrets.token_hex(32)


app.config["SECRET_KEY"] = get_flask_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = os.environ.get("SESSION_COOKIE_SAMESITE", "Lax")
app.config["SESSION_COOKIE_SECURE"] = parse_bool(os.environ.get("SESSION_COOKIE_SECURE"), default=IS_PRODUCTION)
app.config["PREFERRED_URL_SCHEME"] = os.environ.get("PREFERRED_URL_SCHEME", "https" if IS_PRODUCTION else "http")

AUTH_SESSION_ID_KEY = "auth_session_id"
AUTH_SESSION_COOKIES_KEY = "cookies"
AUTH_SESSION_TTL_SECONDS = int(os.environ.get("AUTH_SESSION_TTL_SECONDS", "28800"))
CSRF_SESSION_KEY = "csrf_token"
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}
AUTH_SESSION_STORE_LOCK = threading.Lock()
app.config["SESSION_PERMANENT"] = False
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(seconds=AUTH_SESSION_TTL_SECONDS)

trusted_hosts = [host.strip() for host in (os.environ.get("TRUSTED_HOSTS") or "").split(",") if host.strip()]
if trusted_hosts:
    app.config["TRUSTED_HOSTS"] = trusted_hosts


class AuthenticationError(RuntimeError):
    pass


def normalize_username(value: str) -> str:
    return (value or "").strip().lower()


def _connect_auth_session_db() -> sqlite3.Connection:
    conn = sqlite3.connect(AUTH_SESSION_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_auth_session_database() -> None:
    auth_session_dir = os.path.dirname(AUTH_SESSION_DB_PATH)
    if auth_session_dir:
        os.makedirs(auth_session_dir, exist_ok=True)

    with AUTH_SESSION_STORE_LOCK:
        with _connect_auth_session_db() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL,
                    cookies_json TEXT NOT NULL,
                    verify_ssl INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_last_seen_at
                ON auth_sessions(last_seen_at)
                """
            )
            conn.commit()


def _upsert_auth_session(auth_state: dict | None) -> None:
    if not auth_state or not auth_state.get("id"):
        return

    cookies_json = json.dumps(auth_state.get(AUTH_SESSION_COOKIES_KEY) or [], ensure_ascii=True, separators=(",", ":"))
    with AUTH_SESSION_STORE_LOCK:
        with _connect_auth_session_db() as conn:
            conn.execute(
                """
                INSERT INTO auth_sessions (
                    id,
                    username,
                    username_normalized,
                    cookies_json,
                    verify_ssl,
                    created_at,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    username = excluded.username,
                    username_normalized = excluded.username_normalized,
                    cookies_json = excluded.cookies_json,
                    verify_ssl = excluded.verify_ssl,
                    created_at = excluded.created_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    str(auth_state.get("id") or ""),
                    str(auth_state.get("username") or ""),
                    str(auth_state.get("username_normalized") or ""),
                    cookies_json,
                    1 if bool(auth_state.get("verify_ssl", DEFAULT_VERIFY_SSL)) else 0,
                    float(auth_state.get("created_at") or time.time()),
                    float(auth_state.get("last_seen_at") or time.time()),
                ),
            )
            conn.commit()


def _load_auth_session(auth_session_id: str | None) -> dict | None:
    if not auth_session_id:
        return None

    with AUTH_SESSION_STORE_LOCK:
        with _connect_auth_session_db() as conn:
            row = conn.execute(
                """
                SELECT id, username, username_normalized, cookies_json, verify_ssl, created_at, last_seen_at
                FROM auth_sessions
                WHERE id = ?
                """,
                (str(auth_session_id),),
            ).fetchone()

    if row is None:
        return None

    try:
        cookies = json.loads(row["cookies_json"] or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        cookies = []

    return {
        "id": str(row["id"] or ""),
        "username": str(row["username"] or ""),
        "username_normalized": str(row["username_normalized"] or ""),
        AUTH_SESSION_COOKIES_KEY: cookies,
        "verify_ssl": bool(row["verify_ssl"]),
        "created_at": float(row["created_at"] or 0.0),
        "last_seen_at": float(row["last_seen_at"] or 0.0),
    }


def _touch_auth_session(auth_session_id: str | None, last_seen_at: float) -> None:
    if not auth_session_id:
        return

    with AUTH_SESSION_STORE_LOCK:
        with _connect_auth_session_db() as conn:
            conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ? WHERE id = ?",
                (float(last_seen_at), str(auth_session_id)),
            )
            conn.commit()


def _delete_auth_session(auth_session_id: str | None) -> None:
    if not auth_session_id:
        return

    with AUTH_SESSION_STORE_LOCK:
        with _connect_auth_session_db() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE id = ?", (str(auth_session_id),))
            conn.commit()


def _serialize_session_cookies(cookie_jar: requests.cookies.RequestsCookieJar) -> list[dict[str, object]]:
    serialized: list[dict[str, object]] = []
    for cookie in cookie_jar:
        if not cookie.name:
            continue
        serialized.append(
            {
                "name": str(cookie.name),
                "value": str(cookie.value),
                "domain": str(cookie.domain or ""),
                "path": str(cookie.path or "/"),
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
            }
        )
    return serialized


def _build_requests_session_from_auth(auth_state: dict) -> requests.Session:
    session_obj = requests.Session()
    session_obj.verify = bool(auth_state.get("verify_ssl", DEFAULT_VERIFY_SSL))
    serialized_cookies = auth_state.get(AUTH_SESSION_COOKIES_KEY) or {}
    if not serialized_cookies:
        raise AuthenticationError("Sesja Jazz wygasla. Zaloguj sie ponownie do Jazz.")

    if isinstance(serialized_cookies, dict):
        session_obj.cookies = requests.utils.cookiejar_from_dict(
            {str(name): str(value) for name, value in serialized_cookies.items() if name and value is not None},
            cookiejar=None,
            overwrite=True,
        )
        return session_obj

    cookie_jar = requests.cookies.RequestsCookieJar()
    for cookie_data in serialized_cookies:
        name = str(cookie_data.get("name") or "").strip()
        value = str(cookie_data.get("value") or "")
        if not name:
            continue
        cookie_jar.set_cookie(
            requests.cookies.create_cookie(
                name=name,
                value=value,
                domain=str(cookie_data.get("domain") or ""),
                path=str(cookie_data.get("path") or "/"),
                secure=bool(cookie_data.get("secure", False)),
                expires=cookie_data.get("expires"),
            )
        )
    session_obj.cookies = cookie_jar
    return session_obj


def persist_authenticated_session_cookies(auth_state: dict | None, jazz_session: requests.Session | None) -> None:
    if not auth_state or jazz_session is None:
        return
    auth_state[AUTH_SESSION_COOKIES_KEY] = _serialize_session_cookies(jazz_session.cookies)
    auth_state["last_seen_at"] = time.time()
    _upsert_auth_session(auth_state)


def ensure_csrf_token(*, force: bool = False) -> str:
    csrf_token = session.get(CSRF_SESSION_KEY)
    if force or not csrf_token:
        csrf_token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = csrf_token
        session.modified = True
    return csrf_token


def attach_debug_log(payload: dict, debug_log: list[str] | None) -> dict:
    if debug_log is not None:
        payload["debug_log"] = debug_log
    return payload


def build_debug_log(payload: dict | None = None) -> list[str] | None:
    payload = payload or {}
    return [] if parse_bool(payload.get("debug"), default=False) else None


@app.before_request
def enforce_csrf_for_api_requests():
    if request.method in SAFE_HTTP_METHODS:
        return None
    if not request.path.startswith("/api/"):
        return None
    if request.path == "/api/session/login":
        return None

    expected_token = session.get(CSRF_SESSION_KEY) or ""
    provided_token = (request.headers.get("X-CSRF-Token") or "").strip()
    if not expected_token or not provided_token or not secrets.compare_digest(expected_token, provided_token):
        return jsonify({"ok": False, "error": "Brak lub nieprawidlowy token CSRF."}), 403
    return None


@app.after_request
def apply_security_headers(response: Response) -> Response:
    response.headers.setdefault("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0, private")
    response.headers.setdefault("Pragma", "no-cache")
    response.headers.setdefault("Expires", "0")
    if request.is_secure or app.config.get("SESSION_COOKIE_SECURE"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'",
    )
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


def _cleanup_expired_auth_sessions(now: float | None = None) -> None:
    now = now or time.time()
    expires_before = float(now) - AUTH_SESSION_TTL_SECONDS

    with AUTH_SESSION_STORE_LOCK:
        with _connect_auth_session_db() as conn:
            conn.execute("DELETE FROM auth_sessions WHERE last_seen_at < ?", (expires_before,))
            conn.commit()


def _drop_auth_session(auth_session_id: str | None) -> None:
    _delete_auth_session(auth_session_id)


def clear_authenticated_session() -> None:
    auth_session_id = session.pop(AUTH_SESSION_ID_KEY, None)
    session.pop(CSRF_SESSION_KEY, None)
    ensure_csrf_token(force=True)
    _drop_auth_session(auth_session_id)


def create_authenticated_session(username: str, verify_ssl: bool, jazz_session: requests.Session) -> dict:
    clean_username = " ".join(str(username or "").split())
    if not clean_username:
        raise AuthenticationError("Podaj login do Jazz.")

    clear_authenticated_session()

    auth_session_id = secrets.token_urlsafe(32)
    now = time.time()
    auth_state = {
        "id": auth_session_id,
        "username": clean_username,
        "username_normalized": normalize_username(clean_username),
        AUTH_SESSION_COOKIES_KEY: _serialize_session_cookies(jazz_session.cookies),
        "verify_ssl": bool(verify_ssl),
        "created_at": now,
        "last_seen_at": now,
    }

    session[AUTH_SESSION_ID_KEY] = auth_session_id
    session.permanent = False
    ensure_csrf_token(force=True)
    session.modified = True
    _upsert_auth_session(auth_state)
    return auth_state


def get_authenticated_session(required: bool = False) -> dict | None:
    auth_session_id = session.get(AUTH_SESSION_ID_KEY)
    if not auth_session_id:
        if required:
            raise AuthenticationError("Zaloguj sie do Jazz, aby kontynuowac.")
        return None

    now = time.time()
    _cleanup_expired_auth_sessions(now)

    auth_state = _load_auth_session(auth_session_id)
    if auth_state:
        auth_state["last_seen_at"] = now
        _touch_auth_session(auth_session_id, now)

    if auth_state:
        return auth_state

    session.pop(AUTH_SESSION_ID_KEY, None)
    session.modified = True
    if required:
        raise AuthenticationError("Sesja wygasla. Zaloguj sie ponownie do Jazz.")
    return None


def build_current_user_payload(auth_state: dict | None = None) -> dict:
    auth_state = auth_state or get_authenticated_session(required=False)
    username = auth_state.get("username", "") if auth_state else ""
    return {
        "authenticated": bool(auth_state),
        "username": username,
        "can_access_analysis": can_access_analysis(username),
        "can_manage_people": can_manage_people(username),
        "csrf_token": ensure_csrf_token(),
    }


def authenticate_request_session(
    username: str,
    password: str,
    verify_ssl: bool,
    debug_log: list[str] | None = None,
) -> dict:
    clean_username = " ".join(str(username or "").split())
    if not clean_username or not password:
        raise AuthenticationError("Podaj login i haslo do Jazz.")

    jazz_session = jazz_login_session(clean_username, password, verify_ssl, debug_log)
    return create_authenticated_session(clean_username, verify_ssl, jazz_session)


def ensure_authenticated_session_from_payload(
    payload: dict | None = None,
    *,
    debug_log: list[str] | None = None,
    default_verify_ssl: bool = False,
) -> dict:
    auth_state = get_authenticated_session(required=False)
    if auth_state:
        return auth_state

    payload = payload or {}
    username = payload.get("username")
    password = payload.get("password") or ""
    verify_ssl = parse_bool(payload.get("verify_ssl"), default=default_verify_ssl)
    return authenticate_request_session(username, password, verify_ssl, debug_log)


def build_jazz_requests_session(auth_state: dict, debug_log: list[str] | None = None) -> requests.Session:
    return _build_requests_session_from_auth(auth_state)


ensure_auth_session_database()


def _normalize_person_name(value: str) -> str:
    value = " ".join((value or "").strip().split()).lower()
    if not value:
        return ""
    # NFKD nie rozkĹ‚ada poprawnie polskiego "Ĺ‚", wiÄ™c mapujemy je jawnie przed ASCII fold.
    value = value.replace("Ĺ‚", "l")
    value = value.replace("\u0142", "l")
    ascii_value = unicodedata.normalize("NFKD", value)
    ascii_value = ascii_value.encode("ascii", "ignore").decode("ascii")
    ascii_value = re.sub(r"[^a-z0-9]+", " ", ascii_value)
    return re.sub(r"\s+", " ", ascii_value).strip()


def _hash_person_name(name: str) -> str:
    normalized = _normalize_person_name(name)
    if not normalized:
        return ""
    return hashlib.sha256(f"person::{normalized}".encode("utf-8")).hexdigest()


def _normalize_person_tokens_key(value: str) -> str:
    normalized = _normalize_person_name(value)
    if not normalized:
        return ""
    tokens = sorted(token for token in normalized.split() if token)
    return " ".join(tokens)


def normalize_jazz_login(value: str) -> str:
    normalized = _normalize_person_name(value)
    return re.sub(r"[^a-z0-9]+", "", normalized)


def build_default_jazz_login(name: str) -> str:
    raw_tokens = [token for token in re.split(r"\s+", str(name or "").strip()) if token]
    if len(raw_tokens) >= 2:
        surname = normalize_jazz_login(re.split(r"[-\u2010-\u2015]", raw_tokens[0], maxsplit=1)[0])
        given_name = normalize_jazz_login(raw_tokens[-1])
        if surname and given_name:
            return f"{surname}{given_name[:1]}"
    normalized_tokens = [token for token in _normalize_person_name(name).split() if token]
    if not normalized_tokens:
        return ""
    return normalize_jazz_login(normalized_tokens[0])


def _find_person_record_loose(name: str) -> dict:
    normalized_name = _normalize_person_name(name)
    if not normalized_name:
        return {}

    name_tokens = set(normalized_name.split())
    if not name_tokens:
        return {}

    best_record = {}
    best_token_count = 0

    for record in PEOPLE_DIRECTORY["records"]:
        record_tokens = set((record.get("tokens_key") or "").split())
        if not record_tokens:
            continue
        if not record_tokens.issubset(name_tokens):
            continue

        token_count = len(record_tokens)
        if token_count > best_token_count:
            best_record = record
            best_token_count = token_count

    return best_record


def _repair_people_database(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, name, normalized_name, department, person_hash, jazz_login FROM people ORDER BY id"
    ).fetchall()

    grouped_rows: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        clean_name = " ".join(str(row["name"] or "").strip().split())
        clean_department = (row["department"] or "").strip().upper()
        canonical_normalized_name = _normalize_person_name(clean_name)
        canonical_person_hash = _hash_person_name(clean_name)
        if not clean_name or not clean_department or not canonical_normalized_name or not canonical_person_hash:
            continue

        grouped_rows[(canonical_normalized_name, clean_department)].append(
            {
                "id": row["id"],
                "name": clean_name,
                "department": clean_department,
                "normalized_name": row["normalized_name"],
                "person_hash": row["person_hash"],
                "jazz_login": normalize_jazz_login(row["jazz_login"] or ""),
                "canonical_normalized_name": canonical_normalized_name,
                "canonical_person_hash": canonical_person_hash,
                "canonical_jazz_login": normalize_jazz_login(row["jazz_login"] or "") or build_default_jazz_login(clean_name),
            }
        )

    for grouped_key in sorted(grouped_rows):
        group = grouped_rows[grouped_key]
        primary = next(
            (
                row
                for row in group
                if row["normalized_name"] == row["canonical_normalized_name"]
                and row["person_hash"] == row["canonical_person_hash"]
            ),
            group[0],
        )

        conn.execute(
            """
            UPDATE people
            SET name = ?, normalized_name = ?, department = ?, person_hash = ?, jazz_login = ?
            WHERE id = ?
            """,
            (
                primary["name"],
                primary["canonical_normalized_name"],
                primary["department"],
                primary["canonical_person_hash"],
                primary["canonical_jazz_login"],
                primary["id"],
            ),
        )

        duplicate_ids = [row["id"] for row in group if row["id"] != primary["id"]]
        if duplicate_ids:
            placeholders = ",".join("?" for _ in duplicate_ids)
            conn.execute(f"DELETE FROM people WHERE id IN ({placeholders})", duplicate_ids)


def ensure_people_database() -> None:
    if not os.path.exists(PEOPLE_SEED_PATH):
        raise RuntimeError(f"Brak pliku seed dla bazy osĂłb: {PEOPLE_SEED_PATH}")

    with open(PEOPLE_SEED_PATH, "r", encoding="utf-8") as fh:
        seed_rows = json.load(fh)

    with sqlite3.connect(PEOPLE_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE,
                department TEXT NOT NULL,
                person_hash TEXT NOT NULL UNIQUE,
                jazz_login TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_people_department ON people(department)")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(people)").fetchall()}
        if "jazz_login" not in columns:
            conn.execute("ALTER TABLE people ADD COLUMN jazz_login TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_people_jazz_login ON people(jazz_login)")

        for row in seed_rows:
            name = " ".join(str(row.get("name") or "").strip().split())
            department = (row.get("department") or "").strip().upper()
            normalized_name = _normalize_person_name(name)
            person_hash = _hash_person_name(name)
            jazz_login = normalize_jazz_login(row.get("jazz_login") or "") or build_default_jazz_login(name)
            if not normalized_name or not department or not person_hash:
                continue

            conn.execute(
                """
                INSERT INTO people(name, normalized_name, department, person_hash, jazz_login)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(normalized_name) DO UPDATE SET
                    name=excluded.name,
                    department=excluded.department,
                    person_hash=excluded.person_hash,
                    jazz_login=CASE
                        WHEN people.jazz_login IS NULL OR people.jazz_login = '' THEN excluded.jazz_login
                        ELSE people.jazz_login
                    END
                """,
                (name, normalized_name, department, person_hash, jazz_login),
            )

        _repair_people_database(conn)
        conn.commit()


def load_people_directory() -> dict:
    ensure_people_database()

    directory = {
        "by_normalized": {},
        "by_tokens": {},
        "by_hash": {},
        "records": [],
    }

    with sqlite3.connect(PEOPLE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, normalized_name, department, person_hash, jazz_login FROM people ORDER BY name"
        ).fetchall()

    for row in rows:
        record = {
            "name": row["name"],
            "normalized_name": row["normalized_name"],
            "tokens_key": _normalize_person_tokens_key(row["name"]),
            "department": row["department"],
            "person_hash": row["person_hash"],
            "jazz_login": normalize_jazz_login(row["jazz_login"] or ""),
        }
        directory["by_normalized"][record["normalized_name"]] = record
        if record["tokens_key"]:
            directory["by_tokens"][record["tokens_key"]] = record
        directory["by_hash"][record["person_hash"]] = record
        directory["records"].append(record)

    directory["records"].sort(key=lambda row: len(row["name"]), reverse=True)
    return directory


PEOPLE_DIRECTORY = load_people_directory()


def refresh_people_directory() -> dict:
    global PEOPLE_DIRECTORY
    PEOPLE_DIRECTORY = load_people_directory()
    return PEOPLE_DIRECTORY


def can_manage_people(username: str) -> bool:
    return normalize_username(username) in PEOPLE_MANAGEMENT_ALLOWED_USERNAMES


def require_people_management_access(auth_state: dict | None = None) -> str:
    auth_state = auth_state or get_authenticated_session(required=True)
    username = auth_state.get("username", "")
    if can_manage_people(username):
        return username
    raise PermissionError(
        "ZarzÄ…dzanie bazÄ… pracownikĂłw jest dostÄ™pne tylko dla wybranych loginĂłw Jazz."
    )


def can_access_analysis(username: str) -> bool:
    return normalize_username(username) in ANALYSIS_ALLOWED_USERNAMES


def require_analysis_access(auth_state: dict | None = None) -> str:
    auth_state = auth_state or get_authenticated_session(required=True)
    username = auth_state.get("username", "")
    if can_access_analysis(username):
        return username
    raise PermissionError(
        "Zakladka Analiza jest dostepna tylko dla wybranych loginow Jazz."
    )


def list_people_records() -> list[dict]:
    ensure_people_database()

    with sqlite3.connect(PEOPLE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT name, department, normalized_name, jazz_login FROM people ORDER BY name COLLATE NOCASE"
        ).fetchall()

    return [
        {
            "name": row["name"],
            "department": row["department"],
            "normalized_name": row["normalized_name"],
            "jazz_login": normalize_jazz_login(row["jazz_login"] or ""),
        }
        for row in rows
    ]


def upsert_person_record(name: str, department: str, jazz_login: str = "") -> dict:
    clean_name = " ".join((name or "").strip().split())
    clean_department = (department or "").strip().upper()
    normalized_name = _normalize_person_name(clean_name)
    person_hash = _hash_person_name(clean_name)
    clean_jazz_login = normalize_jazz_login(jazz_login) or build_default_jazz_login(clean_name)

    if not clean_name or not normalized_name:
        raise ValueError("Pole Nazwa jest wymagane.")
    if not clean_department:
        raise ValueError("Pole DziaĹ‚ jest wymagane.")
    if not clean_jazz_login:
        raise ValueError("Pole Login Jazz jest wymagane.")
    if not person_hash:
        raise ValueError("Nie udaĹ‚o siÄ™ przygotowaÄ‡ identyfikatora pracownika.")

    ensure_people_database()

    with sqlite3.connect(PEOPLE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT name, department, jazz_login FROM people WHERE normalized_name = ?",
            (normalized_name,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO people(name, normalized_name, department, person_hash, jazz_login)
            VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(normalized_name) DO UPDATE SET
                name=excluded.name,
                department=excluded.department,
                person_hash=excluded.person_hash,
                jazz_login=excluded.jazz_login
            """,
            (clean_name, normalized_name, clean_department, person_hash, clean_jazz_login),
        )
        conn.commit()

    refresh_people_directory()

    return {
        "name": clean_name,
        "department": clean_department,
        "normalized_name": normalized_name,
        "person_hash": person_hash,
        "jazz_login": clean_jazz_login,
        "updated": bool(existing),
    }


def delete_person_record(normalized_name: str) -> dict:
    clean_normalized_name = (normalized_name or "").strip()
    if not clean_normalized_name:
        raise ValueError("Brak identyfikatora pracownika do usuniÄ™cia.")

    ensure_people_database()

    with sqlite3.connect(PEOPLE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT name, department, normalized_name, jazz_login FROM people WHERE normalized_name = ?",
            (clean_normalized_name,),
        ).fetchone()
        if not existing:
            raise ValueError("Nie znaleziono pracownika do usuniÄ™cia.")

        conn.execute(
            "DELETE FROM people WHERE normalized_name = ?",
            (clean_normalized_name,),
        )
        conn.commit()

    refresh_people_directory()

    return {
        "name": existing["name"],
        "department": existing["department"],
        "normalized_name": existing["normalized_name"],
        "jazz_login": normalize_jazz_login(existing["jazz_login"] or ""),
    }


def update_person_record(original_normalized_name: str, name: str, department: str, jazz_login: str = "") -> dict:
    source_normalized_name = (original_normalized_name or "").strip()
    clean_name = " ".join((name or "").strip().split())
    clean_department = (department or "").strip().upper()
    normalized_name = _normalize_person_name(clean_name)
    person_hash = _hash_person_name(clean_name)
    clean_jazz_login = normalize_jazz_login(jazz_login) or build_default_jazz_login(clean_name)

    if not source_normalized_name:
        raise ValueError("Brak identyfikatora pracownika do aktualizacji.")
    if not clean_name or not normalized_name:
        raise ValueError("Pole Nazwa jest wymagane.")
    if not clean_department:
        raise ValueError("Pole DziaĹ‚ jest wymagane.")
    if not clean_jazz_login:
        raise ValueError("Pole Login Jazz jest wymagane.")
    if not person_hash:
        raise ValueError("Nie udaĹ‚o siÄ™ przygotowaÄ‡ identyfikatora pracownika.")

    ensure_people_database()

    with sqlite3.connect(PEOPLE_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        existing = conn.execute(
            "SELECT id FROM people WHERE normalized_name = ?",
            (source_normalized_name,),
        ).fetchone()
        if not existing:
            raise ValueError("Nie znaleziono pracownika do aktualizacji.")

        conflict = conn.execute(
            "SELECT id FROM people WHERE normalized_name = ? AND id <> ?",
            (normalized_name, existing["id"]),
        ).fetchone()
        if conflict:
            raise ValueError("Pracownik o takiej nazwie juĹĽ istnieje w bazie danych.")

        conn.execute(
            """
            UPDATE people
            SET name = ?, normalized_name = ?, department = ?, person_hash = ?, jazz_login = ?
            WHERE id = ?
            """,
            (clean_name, normalized_name, clean_department, person_hash, clean_jazz_login, existing["id"]),
        )
        conn.commit()

    refresh_people_directory()

    return {
        "name": clean_name,
        "department": clean_department,
        "normalized_name": normalized_name,
        "person_hash": person_hash,
        "jazz_login": clean_jazz_login,
    }


def get_person_record(name: str) -> dict:
    normalized_name = _normalize_person_name(name)
    if not normalized_name:
        return {}
    record = PEOPLE_DIRECTORY["by_normalized"].get(normalized_name, {})
    if record:
        return record

    tokens_key = _normalize_person_tokens_key(name)
    if not tokens_key:
        return {}
    record = PEOPLE_DIRECTORY["by_tokens"].get(tokens_key, {})
    if record:
        return record

    return _find_person_record_loose(name)


def build_lessons_learned_case_key(row: dict) -> str:
    normalized_summary = " ".join(str(row.get("normalized_summary") or "").strip().lower().split())
    if normalized_summary:
        return normalized_summary

    normalized_title = normalize_summary_for_analysis(str(row.get("title") or ""))
    if normalized_title:
        return normalized_title

    raw_title = " ".join(str(row.get("title") or "").split())
    if raw_title:
        return raw_title.lower()

    fallback = json.dumps(
        {
            "count": int(row.get("count") or 0),
            "items": row.get("items") or [],
            "projects": row.get("projects") or [],
            "type": str(row.get("type") or ""),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()


def ensure_lessons_learned_database() -> None:
    with sqlite3.connect(LESSONS_LEARNED_DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lessons_learned_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_key TEXT NOT NULL UNIQUE,
                case_title TEXT NOT NULL,
                normalized_summary TEXT NOT NULL DEFAULT '',
                decision_status TEXT NOT NULL DEFAULT 'candidate',
                ll_candidate_id TEXT NOT NULL,
                ll_candidate_uri TEXT NOT NULL DEFAULT '',
                responsible_de_name TEXT NOT NULL DEFAULT '',
                responsible_de_department TEXT NOT NULL DEFAULT '',
                saved_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(lessons_learned_candidates)").fetchall()
        }
        if "decision_status" not in columns:
            conn.execute(
                """
                ALTER TABLE lessons_learned_candidates
                ADD COLUMN decision_status TEXT NOT NULL DEFAULT 'candidate'
                """
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lessons_learned_candidates_saved_at ON lessons_learned_candidates(saved_at)"
        )
        conn.commit()


def list_saved_lessons_learned_candidates(case_keys: list[str] | None = None) -> list[dict]:
    ensure_lessons_learned_database()

    with sqlite3.connect(LESSONS_LEARNED_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        if case_keys is not None:
            normalized_keys = [" ".join(str(key or "").strip().lower().split()) for key in case_keys if str(key or "").strip()]
            normalized_keys = list(dict.fromkeys(normalized_keys))
            if not normalized_keys:
                return []
            placeholders = ",".join("?" for _ in normalized_keys)
            rows = conn.execute(
                f"""
                SELECT case_key, case_title, normalized_summary, decision_status, ll_candidate_id, ll_candidate_uri,
                       responsible_de_name, responsible_de_department, saved_at
                FROM lessons_learned_candidates
                WHERE case_key IN ({placeholders})
                ORDER BY case_title COLLATE NOCASE
                """,
                normalized_keys,
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT case_key, case_title, normalized_summary, decision_status, ll_candidate_id, ll_candidate_uri,
                       responsible_de_name, responsible_de_department, saved_at
                FROM lessons_learned_candidates
                ORDER BY case_title COLLATE NOCASE
                """
            ).fetchall()

    return [
        {
            "case_key": row["case_key"],
            "case_title": row["case_title"],
            "normalized_summary": row["normalized_summary"],
            "decision_status": row["decision_status"] or LESSONS_LEARNED_STATUS_CANDIDATE,
            "ll_candidate_id": row["ll_candidate_id"],
            "ll_candidate_uri": row["ll_candidate_uri"],
            "responsible_de_name": row["responsible_de_name"],
            "responsible_de_department": row["responsible_de_department"],
            "saved_at": row["saved_at"],
        }
        for row in rows
    ]


def _save_lessons_learned_case_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        return []

    ensure_lessons_learned_database()
    saved_at = datetime.now(timezone.utc).isoformat()
    saved_keys: list[str] = []

    with sqlite3.connect(LESSONS_LEARNED_DB_PATH) as conn:
        for row in rows:
            case_key = build_lessons_learned_case_key(row)
            decision_status = " ".join(str(row.get("decision_status") or "").split()).lower()
            if not case_key or decision_status not in {
                LESSONS_LEARNED_STATUS_CANDIDATE,
                LESSONS_LEARNED_STATUS_EXCLUDED,
            }:
                continue

            conn.execute(
                """
                INSERT INTO lessons_learned_candidates(
                    case_key, case_title, normalized_summary, decision_status, ll_candidate_id, ll_candidate_uri,
                    responsible_de_name, responsible_de_department, saved_at
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_key) DO UPDATE SET
                    case_title=excluded.case_title,
                    normalized_summary=excluded.normalized_summary,
                    decision_status=excluded.decision_status,
                    ll_candidate_id=excluded.ll_candidate_id,
                    ll_candidate_uri=excluded.ll_candidate_uri,
                    responsible_de_name=excluded.responsible_de_name,
                    responsible_de_department=excluded.responsible_de_department,
                    saved_at=excluded.saved_at
                """,
                (
                    case_key,
                    " ".join(str(row.get("title") or row.get("normalized_summary") or "").split()) or "-",
                    " ".join(str(row.get("normalized_summary") or "").split()),
                    decision_status,
                    " ".join(str(row.get("ll_candidate_id") or "").split()),
                    " ".join(str(row.get("ll_candidate_uri") or "").split()),
                    " ".join(str(row.get("responsible_de_name") or "").split()),
                    " ".join(str(row.get("responsible_de_department") or "").split()),
                    saved_at,
                ),
            )
            saved_keys.append(case_key)

        conn.commit()

    return list_saved_lessons_learned_candidates(saved_keys)


def save_lessons_learned_candidates(selected_cases: list[dict], created_items: list[dict]) -> list[dict]:
    if not selected_cases or not created_items:
        return []

    cases_by_key = {build_lessons_learned_case_key(case): case for case in selected_cases}
    rows_to_save: list[dict] = []

    for created in created_items:
        case_key = build_lessons_learned_case_key(created)
        source_case = cases_by_key.get(case_key)
        candidate_id = " ".join(str(created.get("identifier") or created.get("resource_uri") or "").split())
        if not source_case or not case_key or not candidate_id:
            continue
        rows_to_save.append(
            {
                **source_case,
                "decision_status": LESSONS_LEARNED_STATUS_CANDIDATE,
                "ll_candidate_id": candidate_id,
                "ll_candidate_uri": " ".join(str(created.get("resource_uri") or "").split()),
            }
        )

    return _save_lessons_learned_case_rows(rows_to_save)


def save_lessons_learned_exclusions(selected_cases: list[dict]) -> list[dict]:
    rows_to_save = [
        {
            **case,
            "decision_status": LESSONS_LEARNED_STATUS_EXCLUDED,
            "ll_candidate_id": "",
            "ll_candidate_uri": "",
            "responsible_de_name": "",
            "responsible_de_department": "",
        }
        for case in selected_cases or []
        if build_lessons_learned_case_key(case)
    ]
    return _save_lessons_learned_case_rows(rows_to_save)


def get_department_for_person(name: str) -> str:
    return get_person_record(name).get("department", "")


def get_jazz_login_for_person(name: str) -> str:
    return get_person_record(name).get("jazz_login", "")


def get_hash_for_person(name: str) -> str:
    return get_person_record(name).get("person_hash", "")


def get_department_for_hash(person_hash: str) -> str:
    return PEOPLE_DIRECTORY["by_hash"].get((person_hash or "").strip(), {}).get("department", "")


def get_name_for_hash(person_hash: str) -> str:
    return PEOPLE_DIRECTORY["by_hash"].get((person_hash or "").strip(), {}).get("name", "")


def get_person_record_by_normalized_name(normalized_name: str) -> dict:
    return PEOPLE_DIRECTORY["by_normalized"].get((normalized_name or "").strip(), {})


def get_names_for_hashes(person_hashes: list[str]) -> list[str]:
    names = []
    seen = set()

    for person_hash in person_hashes or []:
        name = get_name_for_hash(person_hash)
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)

    return names


def anonymize_person_names(value: str) -> str:
    text = value or ""
    for record in PEOPLE_DIRECTORY["records"]:
        text = re.sub(re.escape(record["name"]), record["person_hash"], text, flags=re.IGNORECASE)
    return text


def strip_known_person_names(value: str) -> str:
    text = value or ""
    for record in PEOPLE_DIRECTORY["records"]:
        text = re.sub(re.escape(record["name"]), " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_lessons_learned_case(row: dict) -> dict:
    normalized_name = " ".join(str(row.get("responsible_de_normalized_name") or "").split())
    provided_name = " ".join(str(row.get("responsible_de_name") or "").split())

    record = get_person_record_by_normalized_name(normalized_name) if normalized_name else {}
    if not record and provided_name:
        record = get_person_record(provided_name)

    if not record:
        title = " ".join(str(row.get("title") or row.get("normalized_summary") or "").split()) or "wybrany przypadek"
        raise ValueError(f"Nie znaleziono osoby odpowiedzialnej DE w bazie danych dla przypadku: {title}.")

    normalized_row = dict(row)
    normalized_row["responsible_de_name"] = record.get("name", "")
    normalized_row["responsible_de_department"] = record.get("department", "")
    normalized_row["responsible_de_normalized_name"] = record.get("normalized_name", "")
    normalized_row["responsible_de_jazz_login"] = record.get("jazz_login", "")
    return normalized_row

ANALYSIS_STOPWORDS = {
    "a", "aby", "albo", "ale", "and", "bez", "bĹ‚Ä…d", "blad", "brak", "by", "byÄ‡", "byl",
    "czy", "dla", "do", "does", "dot", "ecu", "error", "failure", "finding", "findingi",
    "findings", "from", "function", "funkcji", "hardware", "if", "in", "issue", "jest",
    "nie", "oraz", "problem", "project", "przy", "siÄ™", "sie", "software", "status",
    "system", "the", "to", "type", "ukĹ‚ad", "uklad", "w", "when", "wi", "with", "z",
    "za", "defect", "defects", "module", "test", "tests", "sw", "hw", "radar",
}
def get_manager_for_department(department: str) -> str:
    return DEPARTMENT_MANAGERS.get((department or "").strip().upper(), "")


def get_openai_client() -> OpenAI:
    api_key = read_secret_env("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Brak ustawionego OPENAI_API_KEY. Ustaw OPENAI_API_KEY albo OPENAI_API_KEY_FILE "
            "przed uruchomieniem aplikacji."
        )
    return OpenAI(api_key=api_key)


def normalize_summary_for_analysis(value: str) -> str:
    value = (value or "").lower()
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"\b[a-z]{1,4}-\d+\b", " ", value)
    value = re.sub(r"\b(?:wi|id)\s*[:#-]?\s*\d+\b", " ", value)
    value = re.sub(r"\b\d{4,}\b", " ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def classify_work_item_type(value: str) -> str:
    text = (value or "").strip().lower()
    if "defect" in text:
        return "Defect"
    if "finding" in text:
        return "Finding"
    return "Other"


def extract_keywords(text: str) -> list[str]:
    normalized = normalize_summary_for_analysis(strip_known_person_names(text))
    tokens = []
    for token in normalized.split():
        if len(token) < 4:
            continue
        if token.isdigit():
            continue
        if token in ANALYSIS_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def merge_owner_creator_items(owner_items: list[dict], creator_items: list[dict]) -> list[dict]:
    creator_by_id = {
        (item.get("work_item_id") or "").strip(): item
        for item in creator_items
        if (item.get("work_item_id") or "").strip()
    }

    merged = []
    seen_ids = set()

    for owner_item in owner_items:
        work_item_id = (owner_item.get("work_item_id") or "").strip()
        if not work_item_id:
            continue
        creator_item = creator_by_id.get(work_item_id, {})
        merged.append({
            "work_item_id": work_item_id,
            "project": owner_item.get("project") or creator_item.get("project") or "",
            "type": owner_item.get("type") or creator_item.get("type") or "",
            "work_item": owner_item.get("work_item") or creator_item.get("work_item") or "",
            "work_item_url": owner_item.get("work_item_url") or creator_item.get("work_item_url") or "",
            "owner": owner_item.get("owner") or "",
            "owner_department": owner_item.get("department") or get_department_for_person(owner_item.get("owner") or ""),
            "owner_hash": owner_item.get("person_hash") or get_hash_for_person(owner_item.get("owner") or ""),
            "creator": creator_item.get("owner") or "",
            "creator_department": creator_item.get("department") or get_department_for_person(creator_item.get("owner") or ""),
            "creator_hash": creator_item.get("person_hash") or get_hash_for_person(creator_item.get("owner") or ""),
        })
        seen_ids.add(work_item_id)

    for creator_item in creator_items:
        work_item_id = (creator_item.get("work_item_id") or "").strip()
        if not work_item_id or work_item_id in seen_ids:
            continue
        merged.append({
            "work_item_id": work_item_id,
            "project": creator_item.get("project") or "",
            "type": creator_item.get("type") or "",
            "work_item": creator_item.get("work_item") or "",
            "work_item_url": creator_item.get("work_item_url") or "",
            "owner": "",
            "owner_department": "",
            "owner_hash": "",
            "creator": creator_item.get("owner") or "",
            "creator_department": creator_item.get("department") or get_department_for_person(creator_item.get("owner") or ""),
            "creator_hash": creator_item.get("person_hash") or get_hash_for_person(creator_item.get("owner") or ""),
        })

    return merged


def build_top_people_rows(counter: Counter, limit: int = 3) -> list[dict]:
    rows = []
    for person_hash, count in counter.most_common(limit):
        if not person_hash:
            continue
        rows.append({
            "person_hash": person_hash,
            "department": get_department_for_hash(person_hash),
            "count": count,
        })
    return rows


def build_local_analysis_payload(merged_items: list[dict]) -> dict:
    type_counter = Counter()
    owner_department_counter = Counter()
    creator_department_counter = Counter()
    owner_hash_counter = Counter()
    creator_hash_counter = Counter()
    project_counter = Counter()
    repeated_groups: dict[str, dict] = {}
    owner_department_details: dict[str, Counter] = defaultdict(Counter)
    creator_department_details: dict[str, Counter] = defaultdict(Counter)
    owner_department_people: dict[str, Counter] = defaultdict(Counter)
    creator_department_people: dict[str, Counter] = defaultdict(Counter)
    project_details: dict[str, dict] = {}

    for item in merged_items:
        item_type = classify_work_item_type(item.get("type"))
        summary = (item.get("work_item") or "").strip()
        safe_summary = anonymize_person_names(summary)
        scrubbed_summary = strip_known_person_names(summary)
        project = (item.get("project") or "").strip()
        owner_department = (item.get("owner_department") or "").strip()
        creator_department = (item.get("creator_department") or "").strip()
        owner_hash = (item.get("owner_hash") or get_hash_for_person(item.get("owner") or "")).strip()
        creator_hash = (item.get("creator_hash") or get_hash_for_person(item.get("creator") or "")).strip()

        type_counter[item_type] += 1
        if project:
            project_counter[project] += 1
            project_row = project_details.setdefault(project, {
                "project": project,
                "total": 0,
                "types": Counter(),
                "owner_departments": Counter(),
                "creator_departments": Counter(),
            })
            project_row["total"] += 1
            project_row["types"][item_type] += 1

        if owner_department:
            owner_department_counter[owner_department] += 1
            owner_department_details[owner_department]["total"] += 1
            owner_department_details[owner_department][item_type] += 1
            if project:
                project_details[project]["owner_departments"][owner_department] += 1
        if creator_department:
            creator_department_counter[creator_department] += 1
            creator_department_details[creator_department]["total"] += 1
            creator_department_details[creator_department][item_type] += 1
            if project:
                project_details[project]["creator_departments"][creator_department] += 1
        if owner_hash:
            owner_hash_counter[owner_hash] += 1
            if owner_department:
                owner_department_people[owner_department][owner_hash] += 1
        if creator_hash:
            creator_hash_counter[creator_hash] += 1
            if creator_department:
                creator_department_people[creator_department][creator_hash] += 1

        normalized_summary = normalize_summary_for_analysis(scrubbed_summary)
        if not normalized_summary:
            normalized_summary = f"wi-{item.get('work_item_id')}"

        group = repeated_groups.setdefault(normalized_summary, {
            "normalized_summary": normalized_summary,
            "titles": Counter(),
            "count": 0,
            "types": Counter(),
            "projects": Counter(),
            "owner_departments": Counter(),
            "creator_departments": Counter(),
            "owner_people": Counter(),
            "creator_people": Counter(),
            "items": [],
        })

        group["titles"][safe_summary or f"WI {item.get('work_item_id')}"] += 1
        group["count"] += 1
        group["types"][item_type] += 1
        if project:
            group["projects"][project] += 1
        if owner_department:
            group["owner_departments"][owner_department] += 1
        if creator_department:
            group["creator_departments"][creator_department] += 1
        if owner_hash:
            group["owner_people"][owner_hash] += 1
        if creator_hash:
            group["creator_people"][creator_hash] += 1
        if len(group["items"]) < 12:
            group["items"].append({
                "work_item_id": item.get("work_item_id") or "",
                "project": item.get("project") or "",
                "type": item_type,
                "summary": safe_summary,
                "work_item_url": item.get("work_item_url") or "",
                "owner_department": owner_department,
                "creator_department": creator_department,
                "owner_hash": owner_hash,
                "creator_hash": creator_hash,
            })

    repeated_cases = []
    repeated_project_counter = Counter()
    for group in repeated_groups.values():
        if group["count"] <= 1:
            continue

        title = group["titles"].most_common(1)[0][0] if group["titles"] else ""
        non_zero_types = [label for label in ("Defect", "Finding") if group["types"].get(label, 0) > 0]
        dominant_type = non_zero_types[0] if len(non_zero_types) == 1 else "Mixed"

        for project, count in group["projects"].items():
            repeated_project_counter[project] += count

        repeated_cases.append({
            "title": title,
            "normalized_summary": group["normalized_summary"],
            "count": group["count"],
            "type": dominant_type,
            "type_breakdown": dict(group["types"]),
            "projects": [
                {
                    "project": project,
                    "count": count,
                }
                for project, count in group["projects"].most_common(8)
            ],
            "owner_departments": [
                {
                    "department": department,
                    "count": count,
                }
                for department, count in group["owner_departments"].most_common(5)
            ],
            "creator_departments": [
                {
                    "department": department,
                    "count": count,
                }
                for department, count in group["creator_departments"].most_common(5)
            ],
            "owner_people": build_top_people_rows(group["owner_people"], limit=5),
            "creator_people": build_top_people_rows(group["creator_people"], limit=5),
            "items": sorted(
                group["items"],
                key=lambda row: (
                    (row.get("project") or "").lower(),
                    row.get("work_item_id") or "",
                ),
            ),
        })

    repeated_cases.sort(key=lambda row: (-row["count"], (row["title"] or "").lower()))

    department_split = []
    all_departments = set(owner_department_counter) | set(creator_department_counter)
    for department in sorted(all_departments, key=lambda item: (-(owner_department_counter[item] + creator_department_counter[item]), item)):
        department_split.append({
            "department": department,
            "manager": get_manager_for_department(department),
            "owner_count": owner_department_counter.get(department, 0),
            "creator_count": creator_department_counter.get(department, 0),
            "owner_defects": owner_department_details[department].get("Defect", 0),
            "owner_findings": owner_department_details[department].get("Finding", 0),
            "creator_defects": creator_department_details[department].get("Defect", 0),
            "creator_findings": creator_department_details[department].get("Finding", 0),
        })

    summary = {
        "total_items": len(merged_items),
        "defects": type_counter.get("Defect", 0),
        "findings": type_counter.get("Finding", 0),
        "other": type_counter.get("Other", 0),
        "owner_departments": len(owner_department_counter),
        "creator_departments": len(creator_department_counter),
        "projects": len(project_counter),
        "repeated_groups": len(repeated_cases),
        "repeated_items": sum(row["count"] for row in repeated_cases),
    }

    return {
        "summary": summary,
        "type_counts": [
            {"label": label, "value": value}
            for label, value in type_counter.items()
            if value > 0
        ],
        "owner_department_counts": [
            {
                "department": department,
                "value": value,
            }
            for department, value in owner_department_counter.most_common(10)
        ],
        "owner_department_details": [
            {
                "department": department,
                "total": stats.get("total", 0),
                "defects": stats.get("Defect", 0),
                "findings": stats.get("Finding", 0),
                "other": stats.get("Other", 0),
                "top_people": build_top_people_rows(owner_department_people[department], limit=3),
            }
            for department, stats in sorted(
                owner_department_details.items(),
                key=lambda item: (-item[1].get("total", 0), item[0])
            )[:10]
        ],
        "creator_department_counts": [
            {
                "department": department,
                "value": value,
            }
            for department, value in creator_department_counter.most_common(10)
        ],
        "project_counts": [
            {
                "project": project,
                "value": value,
                "repeated_value": repeated_project_counter.get(project, 0),
            }
            for project, value in project_counter.most_common(10)
        ],
        "project_details": [
            {
                "project": project,
                "total": stats.get("total", 0),
                "repeated": repeated_project_counter.get(project, 0),
                "defects": stats["types"].get("Defect", 0),
                "findings": stats["types"].get("Finding", 0),
                "owner_departments": [
                    {"department": department, "count": count}
                    for department, count in stats["owner_departments"].most_common(3)
                ],
                "creator_departments": [
                    {"department": department, "count": count}
                    for department, count in stats["creator_departments"].most_common(3)
                ],
            }
            for project, stats in sorted(
                project_details.items(),
                key=lambda item: (-item[1].get("total", 0), item[0])
            )[:12]
        ],
        "department_split": department_split[:12],
        "owner_hash_counts": [
            {
                "person_hash": person_hash,
                "department": get_department_for_hash(person_hash),
                "value": value,
            }
            for person_hash, value in owner_hash_counter.most_common(20)
        ],
        "creator_hash_counts": [
            {
                "person_hash": person_hash,
                "department": get_department_for_hash(person_hash),
                "value": value,
            }
            for person_hash, value in creator_hash_counter.most_common(20)
        ],
        "repeated_cases": repeated_cases[:20],
        "sample_records": [
            {
                "work_item_id": item.get("work_item_id") or "",
                "type": classify_work_item_type(item.get("type")),
                "summary": anonymize_person_names(item.get("work_item") or ""),
                "owner_department": item.get("owner_department") or "",
                "creator_department": item.get("creator_department") or "",
                "owner_hash": item.get("owner_hash") or "",
                "creator_hash": item.get("creator_hash") or "",
                "project": item.get("project") or "",
            }
            for item in merged_items[:80]
        ],
    }


def extract_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("OpenAI zwrĂłciĹ‚ pustÄ… odpowiedĹş.")

    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ValueError("Nie udaĹ‚o siÄ™ znaleĹşÄ‡ obiektu JSON w odpowiedzi OpenAI.")

    return json.loads(raw[start:end + 1])


def with_department_managers(rows: list[dict]) -> list[dict]:
    enriched = []
    for row in rows or []:
        item = dict(row)
        item["manager"] = item.get("manager") or get_manager_for_department(item.get("department") or "")
        enriched.append(item)
    return enriched


def decode_people_rows(rows: list[dict]) -> list[dict]:
    decoded = []
    for row in rows or []:
        person_hash = (row.get("person_hash") or "").strip()
        item = dict(row)
        item["name"] = get_name_for_hash(person_hash)
        item["department"] = item.get("department") or get_department_for_hash(person_hash)
        decoded.append(item)
    return decoded


def append_people_note(text: str, people_rows: list[dict]) -> str:
    names = [row.get("name") or "" for row in people_rows if row.get("name")]
    if not names:
        return text or ""

    note = f" Osoby: {', '.join(names)}."
    base = (text or "").strip()
    if note.strip() in base:
        return base
    return f"{base}{note}" if base else f"Osoby: {', '.join(names)}."


def hydrate_ai_payload(ai_payload: dict) -> dict:
    payload = copy.deepcopy(ai_payload or {})

    for row in payload.get("case_observations", []) or []:
        row["projects"] = row.get("projects") or []
        row["owner_departments"] = with_department_managers(row.get("owner_departments") or [])
        row["creator_departments"] = with_department_managers(row.get("creator_departments") or [])

    for row in payload.get("project_insights", []) or []:
        row["owner_departments"] = with_department_managers(row.get("owner_departments") or [])
        row["creator_departments"] = with_department_managers(row.get("creator_departments") or [])

    return payload


def build_openai_analysis_prompt(local_payload: dict) -> str:
    return f"""
Jestes analitykiem jakosci oprogramowania. Analizujesz dane Defect i Finding z systemu Jazz.
Zwroc wylacznie poprawny JSON, bez markdownu i bez komentarzy.
Analiza ma wskazywac konkretne powtarzajace sie przypadki, a nie ogolne tematy.

Wymagany format:
{{
  "executive_summary": "2-4 zdania po polsku",
  "case_observations": [
    {{
      "title": "dokladny powtarzajacy sie przypadek",
      "type": "Defect|Finding|Mixed",
      "count": 0,
      "projects": [
        {{"project": "PRJ", "count": 0}}
      ],
      "owner_departments": [
        {{"department": "DER1", "count": 0}}
      ],
      "creator_departments": [
        {{"department": "DEM1", "count": 0}}
      ],
      "comment": "1 zdanie po polsku o projektach oraz podziale creator i owner"
    }}
  ],
  "project_insights": [
    {{
      "project": "PRJ",
      "total": 0,
      "repeated": 0,
      "defects": 0,
      "findings": 0,
      "owner_departments": [
        {{"department": "DER1", "count": 0}}
      ],
      "creator_departments": [
        {{"department": "DEM1", "count": 0}}
      ],
      "comment": "1 zdanie po polsku"
    }}
  ],
  "recommendations": ["krotka rekomendacja po polsku"]
}}

Reguly:
- Uzywaj przekazanych liczebnosci, nie wymyslaj nowych danych.
- Nie tworz ogolnych tematow ani kategorii semantycznych.
- Pokazuj wylacznie przypadki wynikajace z przekazanych grup powtorzen.
- Dla kazdego przypadku wskaz projekty, dzialy creator i dzialy owner.
- "projects", "owner_departments" i "creator_departments" ogranicz do maksymalnie 3 rekordow.
- "case_observations" ogranicz do 8 rekordow.
- "project_insights" ogranicz do 8 rekordow.
- Pisz po polsku.

Dane wejsciowe:
{json.dumps(local_payload, ensure_ascii=False)}
""".strip()


def run_openai_analysis(local_payload: dict) -> dict:
    client = get_openai_client()

    prompt = build_openai_analysis_prompt(local_payload)
    unused_prompt_template = f"""
JesteĹ› analitykiem jakoĹ›ci oprogramowania. Analizujesz dane Defect i Finding z systemu Jazz.
ZwrĂłÄ‡ wyĹ‚Ä…cznie poprawny JSON, bez markdownu i bez komentarzy.
W danych wejĹ›ciowych identyfikatory osĂłb wystÄ™pujÄ… wyĹ‚Ä…cznie jako hashe. Nigdy nie prĂłbuj odgadywaÄ‡ ani wypisywaÄ‡ prawdziwych nazw osĂłb.

Wymagany format:
{{
  "executive_summary": "2-4 zdania po polsku",
  "top_repeated_items": [
    {{
      "title": "oryginalny tytuĹ‚ lub skrĂłt",
      "type": "Defect|Finding|Mixed",
      "topic": "krĂłtki temat po polsku",
      "count": 0,
      "top_departments": [
        {{"department": "DER1", "count": 0}}
      ],
      "top_people": [
        {{"person_hash": "sha256...", "department": "DER1", "count": 0}}
      ],
      "comment": "krĂłtki komentarz po polsku"
    }}
  ],
  "top_topics": [
    {{
      "topic": "krĂłtka etykieta po polsku",
      "count": 0,
      "type_focus": "Defect|Finding|Mixed",
      "top_departments": [
        {{"department": "DER1", "count": 0}}
      ],
      "top_people": [
        {{"person_hash": "sha256...", "department": "DER1", "count": 0}}
      ],
      "description": "1 zdanie po polsku"
    }}
  ],
  "department_insights": [
    {{
      "department": "DER1",
      "total": 0,
      "defects": 0,
      "findings": 0,
      "dominant_topics": ["temat 1", "temat 2"],
      "top_people": [
        {{"person_hash": "sha256...", "department": "DER1", "count": 0}}
      ],
      "comment": "1 zdanie po polsku"
    }}
  ],
  "recommendations": ["krĂłtka rekomendacja po polsku"]
}}

ReguĹ‚y:
- UĹĽywaj przekazanych liczebnoĹ›ci, nie wymyĹ›laj nowych danych.
- JeĹĽeli nie masz pewnoĹ›ci co do tematu, nazwij go ostroĹĽnie i opisowo.
- Nigdy nie zwracaj prawdziwych nazw osĂłb. JeĹ›li chcesz wskazaÄ‡ osobÄ™, uĹĽywaj tylko pola "person_hash".
- "top_departments" ogranicz do maksymalnie 3 dziaĹ‚Ăłw.
- "top_people" ogranicz do maksymalnie 3 hashy.
- "top_repeated_items" ogranicz do 8 rekordĂłw.
- "top_topics" ogranicz do 6 rekordĂłw.
- "department_insights" ogranicz do 8 rekordĂłw.
- Pisz po polsku.

Dane wejĹ›ciowe:
{json.dumps(local_payload, ensure_ascii=False)}
""".strip()

    if hasattr(client, "responses"):
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
            reasoning={"effort": "low"},
            max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
        )
        return extract_json_object(getattr(response, "output_text", ""))

    response = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {
                "role": "user",
                "content": prompt,
            }
        ],
        max_completion_tokens=min(8000, OPENAI_MAX_OUTPUT_TOKENS),
    )
    content = ""
    if getattr(response, "choices", None):
        content = (response.choices[0].message.content or "").strip()
    return extract_json_object(content)


def build_fallback_ai_payload(local_payload: dict) -> dict:
    case_observations = []
    for row in local_payload.get("repeated_cases", [])[:8]:
        case_observations.append({
            "title": row.get("title") or "",
            "type": max((row.get("type_breakdown") or {"Mixed": 0}).items(), key=lambda item: item[1])[0],
            "topic": "PowtarzajÄ…cy siÄ™ problem",
            "count": row.get("count") or 0,
            "top_departments": row.get("owner_departments") or row.get("creator_departments") or [],
            "top_people": row.get("top_people") or [],
            "comment": "Grupa wyznaczona na podstawie podobnego, znormalizowanego tytuĹ‚u.",
        })

    department_insights = []
    for row in local_payload.get("owner_department_details", [])[:8]:
        department_insights.append({
            "department": row.get("department") or "",
            "total": row.get("total") or 0,
            "defects": row.get("defects") or 0,
            "findings": row.get("findings") or 0,
            "dominant_topics": [],
            "top_people": row.get("top_people") or [],
            "comment": "Insight wygenerowany lokalnie, poniewaĹĽ analiza OpenAI nie byĹ‚a dostÄ™pna.",
        })

    top_topics = []
    for row in local_payload.get("keyword_counts", [])[:6]:
        top_topics.append({
            "topic": row.get("keyword") or "",
            "count": row.get("value") or 0,
            "type_focus": "Mixed",
            "top_departments": [],
            "top_people": [],
            "description": "NajczÄ™Ĺ›ciej wystÄ™pujÄ…ce sĹ‚owo kluczowe w opisach work itemĂłw.",
        })

    return {
        "executive_summary": "Nie udaĹ‚o siÄ™ pobraÄ‡ peĹ‚nej analizy OpenAI, dlatego pokazano lokalnie policzone agregaty i grupy powtarzajÄ…cych siÄ™ tytuĹ‚Ăłw.",
        "top_repeated_items": case_observations,
        "top_topics": top_topics,
        "department_insights": department_insights,
        "recommendations": [
            "Ustaw poprawny OPENAI_API_KEY_FILE albo OPENAI_API_KEY, aby uzyskaÄ‡ peĹ‚nÄ… analizÄ™ semantycznÄ… tematĂłw.",
        ],
    }


def create_analysis_result(merged_items: list[dict]) -> dict:
    local_payload = build_local_analysis_payload(merged_items)

    try:
        ai_payload = run_openai_analysis(local_payload)
        ai_status = "ok"
        ai_error = ""
    except Exception as exc:
        ai_payload = build_fallback_ai_payload(local_payload)
        ai_status = "fallback"
        ai_error = str(exc)

    hydrated_ai_payload = hydrate_ai_payload(ai_payload)

    return {
        "summary": local_payload.get("summary", {}),
        "charts": {
            "type_split": local_payload.get("type_counts", []),
            "owner_department_split": with_department_managers(local_payload.get("owner_department_counts", [])),
            "creator_department_split": with_department_managers(local_payload.get("creator_department_counts", [])),
            "project_split": [
                {
                    "label": row.get("project") or "",
                    "value": row.get("repeated_value") or row.get("value") or 0,
                }
                for row in local_payload.get("project_counts", [])
                if (row.get("repeated_value") or row.get("value") or 0) > 0
            ],
        },
        "ai": hydrated_ai_payload,
        "repeated_cases": local_payload.get("repeated_cases", []),
        "project_details": local_payload.get("project_details", []),
        "department_split": local_payload.get("department_split", []),
        "ai_status": ai_status,
        "ai_error": ai_error,
    }


def _ascii_fold(value: str) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", text).strip()


def _pdf_safe_text(value: str, fallback: str = "-") -> str:
    text = _ascii_fold(value)
    if not text:
        text = fallback
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pdf_safe_literal(value: str, fallback: str = "") -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        text = fallback
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_analysis_pdf_filename(department: str, saved_at: datetime | None = None) -> str:
    clean_department = (department or "").strip().upper()
    department_label = re.sub(r"[^A-Z0-9]+", "_", _ascii_fold(clean_department)).strip("_") or "Wszystkie_Dzialy"
    date_label = (saved_at or datetime.now()).strftime("%d-%m-%Y")
    return f"raport_{department_label}_{date_label}_Defects_Findings.pdf"


def _build_lessons_learned_pdf_filename(department: str, saved_at: datetime | None = None) -> str:
    clean_department = (department or "").strip().upper()
    department_label = re.sub(r"[^A-Z0-9]+", "_", _ascii_fold(clean_department)).strip("_") or "Wszystkie_Dzialy"
    date_label = (saved_at or datetime.now()).strftime("%d-%m-%Y")
    return f"lessons_learned_{department_label}_{date_label}.pdf"


PDF_UI_COLORS = {
    "accent": colors.HexColor("#0F5CC0") if REPORTLAB_AVAILABLE else None,
    "accent_dark": colors.HexColor("#0B4A98") if REPORTLAB_AVAILABLE else None,
    "text": colors.HexColor("#172131") if REPORTLAB_AVAILABLE else None,
    "muted": colors.HexColor("#64758B") if REPORTLAB_AVAILABLE else None,
    "line": colors.HexColor("#D7E1EE") if REPORTLAB_AVAILABLE else None,
    "line_soft": colors.HexColor("#E7EDF5") if REPORTLAB_AVAILABLE else None,
    "panel_alt": colors.HexColor("#EDF2F8") if REPORTLAB_AVAILABLE else None,
    "panel": colors.HexColor("#F8FBFF") if REPORTLAB_AVAILABLE else None,
    "card": colors.HexColor("#FFFFFF") if REPORTLAB_AVAILABLE else None,
    "chip": colors.HexColor("#EDF3F9") if REPORTLAB_AVAILABLE else None,
    "success": colors.HexColor("#1F6A3D") if REPORTLAB_AVAILABLE else None,
}

_PDF_FONT_REGISTRATION_STATE = {"done": False, "regular": "Helvetica", "bold": "Helvetica-Bold"}


def _get_unicode_pdf_font_paths() -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []

    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        candidates.extend(
            [
                (os.path.join(windir, "Fonts", "segoeui.ttf"), os.path.join(windir, "Fonts", "segoeuib.ttf")),
                (os.path.join(windir, "Fonts", "arial.ttf"), os.path.join(windir, "Fonts", "arialbd.ttf")),
                (
                    os.path.join(windir, "Fonts", "bahnschrift.ttf"),
                    os.path.join(windir, "Fonts", "bahnschrift.ttf"),
                ),
            ]
        )

    candidates.extend(
        [
            (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
            (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
            (r"C:\Windows\Fonts\bahnschrift.ttf", r"C:\Windows\Fonts\bahnschrift.ttf"),
            ("/mnt/c/Windows/Fonts/segoeui.ttf", "/mnt/c/Windows/Fonts/segoeuib.ttf"),
            ("/mnt/c/Windows/Fonts/arial.ttf", "/mnt/c/Windows/Fonts/arialbd.ttf"),
            ("/mnt/c/Windows/Fonts/bahnschrift.ttf", "/mnt/c/Windows/Fonts/bahnschrift.ttf"),
            ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
            (
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
            ),
            (
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ),
            ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
            ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
        ]
    )

    home_dir = os.path.expanduser("~")
    if home_dir:
        candidates.extend(
            [
                (
                    os.path.join(home_dir, ".local", "share", "fonts", "DejaVuSans.ttf"),
                    os.path.join(home_dir, ".local", "share", "fonts", "DejaVuSans-Bold.ttf"),
                ),
                (
                    os.path.join(home_dir, ".fonts", "DejaVuSans.ttf"),
                    os.path.join(home_dir, ".fonts", "DejaVuSans-Bold.ttf"),
                ),
            ]
        )

    for regular_path, bold_path in candidates:
        if os.path.exists(regular_path) and os.path.exists(bold_path):
            return regular_path, bold_path
    for regular_path, bold_path in candidates:
        if os.path.exists(regular_path):
            return regular_path, bold_path if os.path.exists(bold_path) else regular_path
    raise FileNotFoundError("Nie znaleziono fontu TrueType z obsluga polskich znakow.")


def _ensure_unicode_pdf_fonts() -> tuple[str, str]:
    if _PDF_FONT_REGISTRATION_STATE["done"]:
        return _PDF_FONT_REGISTRATION_STATE["regular"], _PDF_FONT_REGISTRATION_STATE["bold"]

    regular_name = "AppSans"
    bold_name = "AppSansBold"

    if REPORTLAB_AVAILABLE:
        regular_path, bold_path = _get_unicode_pdf_font_paths()
        if regular_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(regular_name, regular_path))
        if bold_name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(bold_name, bold_path))
        _PDF_FONT_REGISTRATION_STATE.update({"done": True, "regular": regular_name, "bold": bold_name})

    return _PDF_FONT_REGISTRATION_STATE["regular"], _PDF_FONT_REGISTRATION_STATE["bold"]


def _ll_pdf_escape(value: str) -> str:
    return html.escape(str(value or "").replace("\r", " ").replace("\n", " "), quote=True)


def _build_analysis_export_payload(items: list[dict]) -> dict:
    local_payload = build_local_analysis_payload(items)
    return {
        "summary": local_payload.get("summary", {}),
        "owner_department_split": with_department_managers(local_payload.get("owner_department_counts", [])),
        "creator_department_split": with_department_managers(local_payload.get("creator_department_counts", [])),
        "project_split": [
            {
                "label": row.get("project") or "",
                "value": row.get("repeated_value") or row.get("value") or 0,
            }
            for row in local_payload.get("project_counts", [])
            if (row.get("repeated_value") or row.get("value") or 0) > 0
        ],
        "repeated_cases": [
            {
                **row,
                "owner_departments": with_department_managers(row.get("owner_departments") or []),
                "creator_departments": with_department_managers(row.get("creator_departments") or []),
            }
            for row in local_payload.get("repeated_cases", [])
        ],
    }


def _build_analysis_pdf(items: list[dict], department: str = "", saved_at: datetime | None = None) -> bytes:
    saved_at = saved_at or datetime.now()
    export_payload = _build_analysis_export_payload(items)
    summary = export_payload.get("summary", {})
    department_label = (department or "").strip().upper() or "Wszystkie dzialy"
    owner_rows = export_payload.get("owner_department_split") or []
    creator_rows = export_payload.get("creator_department_split") or []
    project_rows = export_payload.get("project_split") or []
    repeated_cases = export_payload.get("repeated_cases") or []

    page_width = 595
    page_height = 842
    margin_left = 42
    margin_right = 42
    top = 800
    bottom = 52
    streams: list[str] = []
    current_lines: list[str] = []
    y = top
    page_number = 0

    def start_page() -> None:
        nonlocal current_lines, y, page_number
        if current_lines:
            streams.append("\n".join(current_lines))
        current_lines = []
        y = top
        page_number += 1
        current_lines.append("0.2 w")
        current_lines.append(f"{margin_left} 820 m {page_width - margin_right} 820 l S")

    def ensure_space(height: int) -> None:
        nonlocal y
        if page_number == 0:
            start_page()
        if y - height < bottom:
            start_page()

    def write_line(text: str, font: str = "F1", size: int = 11, indent: int = 0, gap_after: int = 4) -> None:
        nonlocal y
        plain_text = _ascii_fold(text) or "-"
        usable_width = page_width - margin_left - margin_right - indent
        approx_chars = max(24, int(usable_width / max(size * 0.56, 1)))
        words = plain_text.split()
        wrapped_lines: list[str] = []
        current = ""

        if not words:
            wrapped_lines = ["-"]
        else:
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > approx_chars:
                    wrapped_lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                wrapped_lines.append(current)

        line_height = max(size + 4, 14)
        ensure_space(line_height * len(wrapped_lines) + gap_after)
        x = margin_left + indent
        for line in wrapped_lines:
            current_lines.append(f"BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm ({_pdf_safe_text(line)}) Tj ET")
            y -= line_height
        y -= gap_after

    def write_section(title: str) -> None:
        ensure_space(28)
        write_line(title, font="F2", size=14, gap_after=6)

    def write_rule() -> None:
        nonlocal y
        ensure_space(12)
        current_lines.append(f"0.2 w {margin_left} {y} m {page_width - margin_right} {y} l S")
        y -= 12

    def write_metric(label: str, value) -> None:
        write_line(f"{label}: {value}", size=11, gap_after=2)

    def write_counter_rows(rows: list[dict], empty_message: str) -> None:
        if not rows:
            write_line(empty_message, size=11)
            return
        for row in rows:
            manager = _ascii_fold(row.get("manager") or "")
            manager_part = f" | Manager: {manager}" if manager else ""
            write_line(
                f"- {row.get('department') or '-'} | Liczba: {row.get('value') or row.get('count') or 0}{manager_part}",
                size=11,
                indent=8,
                gap_after=2,
            )

    def write_project_rows(rows: list[dict]) -> None:
        if not rows:
            write_line("Brak projektow z powtorzeniami.", size=11)
            return
        for row in rows:
            write_line(f"- {row.get('label') or '-'} | Powtorzenia: {row.get('value') or 0}", size=11, indent=8, gap_after=2)

    def _format_department_badges(rows: list[dict]) -> str:
        if not rows:
            return "brak"
        parts = []
        for row in rows:
            manager = _ascii_fold(row.get("manager") or "")
            segment = f"{row.get('department') or '-'} ({row.get('count') or 0})"
            if manager:
                segment += f", manager: {manager}"
            parts.append(segment)
        return "; ".join(parts)

    def write_repeated_cases(rows: list[dict]) -> None:
        if not rows:
            write_line("Brak powtarzajacych sie przypadkow dla wybranego widoku.", size=11)
            return
        for index, row in enumerate(rows, start=1):
            write_line(
                f"{index}. {row.get('title') or '-'} | Typ: {row.get('type') or '-'} | Liczba: {row.get('count') or 0}",
                font="F2",
                size=12,
                gap_after=3,
            )
            write_line(f"Identyfikator grupy: {row.get('normalized_summary') or '-'}", size=10, indent=10, gap_after=2)
            project_text = "; ".join(
                f"{item.get('project') or '-'} ({item.get('count') or 0})"
                for item in (row.get("projects") or [])
            ) or "brak"
            write_line(f"Projekty: {project_text}", size=10, indent=10, gap_after=2)
            write_line(
                f"Creator: {_format_department_badges(row.get('creator_departments') or [])}",
                size=10,
                indent=10,
                gap_after=2,
            )
            write_line(
                f"Owner: {_format_department_badges(row.get('owner_departments') or [])}",
                size=10,
                indent=10,
                gap_after=2,
            )
            items_rows = row.get("items") or []
            if not items_rows:
                write_line("Work items: brak", size=10, indent=10, gap_after=4)
            else:
                write_line("Work items:", size=10, indent=10, gap_after=2)
                for item in items_rows[:12]:
                    summary_text = item.get("summary") or item.get("work_item") or "-"
                    write_line(
                        f"- {item.get('work_item_id') or '-'} | {item.get('project') or '-'} | {item.get('type') or '-'} | {summary_text}",
                        size=10,
                        indent=20,
                        gap_after=1,
                    )
                    if item.get("work_item_url"):
                        write_line(f"  URL: {item.get('work_item_url')}", size=9, indent=24, gap_after=1)
            write_rule()

    write_line("Raport analizy Defects and Findings", font="F2", size=18, gap_after=10)
    write_metric("Filtr dzialu", department_label)
    write_metric("Data zapisu", saved_at.strftime("%d-%m-%Y %H:%M"))
    write_metric("Liczba rekordow w eksporcie", summary.get("total_items", len(items)))
    write_rule()

    write_section("Podsumowanie")
    write_metric("Rekordy lacznie", summary.get("total_items", 0))
    write_metric("Projekty", summary.get("projects", 0))
    write_metric("Powtarzajace sie grupy", summary.get("repeated_groups", 0))
    write_metric("Rekordy w powtorzeniach", summary.get("repeated_items", 0))
    write_metric("Dzialy owner", summary.get("owner_departments", 0))
    write_metric("Dzialy creator", summary.get("creator_departments", 0))

    write_section("Department Owner")
    write_counter_rows(owner_rows, "Brak danych dla department owner.")

    write_section("Department Creator")
    write_counter_rows(creator_rows, "Brak danych dla department creator.")

    write_section("Projekty powtorzen")
    write_project_rows(project_rows)

    write_section("Powtarzajace sie przypadki")
    write_repeated_cases(repeated_cases)

    if current_lines:
        streams.append("\n".join(current_lines))

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    }
    page_object_ids = []
    max_object_id = 4

    for stream in streams:
        page_id = max_object_id + 1
        content_id = max_object_id + 2
        page_object_ids.append(page_id)
        stream_bytes = stream.encode("ascii", "ignore")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        max_object_id = content_id

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects[2] = f"<< /Type /Pages /Count {len(page_object_ids)} /Kids [{kids}] >>".encode("ascii")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_object_id + 1)

    for object_id in range(1, max_object_id + 1):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {max_object_id + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, max_object_id + 1):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {max_object_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("ascii")
    )
    return bytes(pdf)


def _collect_lessons_learned_case_groups(selected_cases: list[dict]) -> list[dict]:
    case_groups: list[dict] = []

    for row in selected_cases:
        group_entries: list[dict] = []
        seen_items: set[tuple[str, str, str]] = set()

        for item in row.get("items") or []:
            project = " ".join(str(item.get("project") or "").split())
            work_item_id = " ".join(str(item.get("work_item_id") or "").split())
            summary = " ".join(
                str(item.get("summary") or item.get("work_item") or "").replace("\r", " ").replace("\n", " ").split()
            )
            url = str(item.get("work_item_url") or "").strip()
            dedupe_key = (url.lower(), work_item_id.lower(), summary.lower())

            if dedupe_key in seen_items:
                continue

            seen_items.add(dedupe_key)
            label_parts = [part for part in [project, work_item_id, summary] if part]
            group_entries.append(
                {
                    "label": " | ".join(label_parts) or "-",
                    "url": url,
                    "project": project or "-",
                    "work_item_id": work_item_id or "-",
                    "summary": summary or "-",
                    "type": " ".join(str(item.get("type") or "").split()) or "-",
                    "creator_department": " ".join(str(item.get("creator_department") or "").split()) or "-",
                    "owner_department": " ".join(str(item.get("owner_department") or "").split()) or "-",
                }
            )

        case_title = " ".join(str(row.get("title") or row.get("normalized_summary") or "").split()) or "-"
        normalized_summary = " ".join(str(row.get("normalized_summary") or "").split())
        responsible_name = " ".join(str(row.get("responsible_de_name") or "").split()) or "-"
        responsible_department = " ".join(str(row.get("responsible_de_department") or "").split())
        responsible_jazz_login = normalize_jazz_login(row.get("responsible_de_jazz_login") or "")
        projects = []
        explicit_projects = row.get("projects") or []
        if explicit_projects:
            for project_row in explicit_projects:
                project_name = " ".join(str(project_row.get("project") or "").split())
                if not project_name:
                    continue
                projects.append(
                    {
                        "project": project_name,
                        "count": int(project_row.get("count") or 0),
                    }
                )
        else:
            project_counts = Counter(entry.get("project") or "-" for entry in group_entries)
            projects = [
                {"project": project_name, "count": count}
                for project_name, count in sorted(project_counts.items(), key=lambda item: (-item[1], item[0]))
            ]
        case_groups.append(
            {
                "title": case_title,
                "normalized_summary": normalized_summary,
                "type": " ".join(str(row.get("type") or "").split()) or "-",
                "count": int(row.get("count") or len(group_entries) or 0),
                "projects": projects,
                "creator_departments": row.get("creator_departments") or [],
                "owner_departments": row.get("owner_departments") or [],
                "responsible_de_name": responsible_name,
                "responsible_de_department": responsible_department,
                "responsible_de_jazz_login": responsible_jazz_login,
                "entry_count": len(group_entries),
                "entries": group_entries,
            }
        )

    return case_groups


def _build_lessons_learned_pdf_legacy(selected_cases: list[dict], username: str, saved_at: datetime | None = None) -> bytes:
    saved_at = saved_at or datetime.now()
    generated_by = " ".join(str(username or "").split()) or "nieznany_uzytkownik"
    case_groups = _collect_lessons_learned_case_groups(selected_cases)

    page_width = 595
    page_height = 842
    margin_left = 42
    margin_right = 42
    top = 800
    bottom = 52
    streams: list[str] = []
    page_annotations: list[list[dict]] = []
    current_lines: list[str] = []
    current_annotations: list[dict] = []
    y = top
    page_number = 0

    def start_page() -> None:
        nonlocal current_lines, current_annotations, y, page_number
        if current_lines:
            streams.append("\n".join(current_lines))
            page_annotations.append(current_annotations)
        current_lines = []
        current_annotations = []
        y = top
        page_number += 1

    def ensure_space(height: int) -> None:
        nonlocal y
        if page_number == 0:
            start_page()
        if y - height < bottom:
            start_page()

    def write_rule() -> None:
        nonlocal y
        ensure_space(12)
        current_lines.append(f"0.2 w {margin_left} {y} m {page_width - margin_right} {y} l S")
        y -= 12

    def write_line(
        text: str,
        font: str = "F1",
        size: int = 11,
        indent: int = 0,
        gap_after: int = 4,
        link_url: str = "",
    ) -> None:
        nonlocal y
        plain_text = _ascii_fold(text) or "-"
        usable_width = page_width - margin_left - margin_right - indent
        approx_chars = max(24, int(usable_width / max(size * 0.56, 1)))
        words = plain_text.split()
        wrapped_lines: list[str] = []
        current = ""

        if not words:
            wrapped_lines = ["-"]
        else:
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > approx_chars:
                    wrapped_lines.append(current)
                    current = word
                else:
                    current = candidate
            if current:
                wrapped_lines.append(current)

        line_height = max(size + 4, 14)
        ensure_space(line_height * len(wrapped_lines) + gap_after)
        x = margin_left + indent

        for line in wrapped_lines:
            current_lines.append(f"BT /{font} {size} Tf 1 0 0 1 {x} {y} Tm ({_pdf_safe_text(line)}) Tj ET")
            if link_url:
                line_width = min(usable_width, max(36, len(line) * size * 0.56))
                current_annotations.append(
                    {
                        "rect": (x, y - 3, x + line_width, y + size + 2),
                        "url": link_url,
                    }
                )
            y -= line_height
        y -= gap_after

    write_line("Przypadki Lessons learned:", font="F2", size=16, gap_after=10)

    if case_groups:
        for group_index, group in enumerate(case_groups):
            responsible_line = f"Przypadek: {group.get('title') or '-'} | Odpowiedzialnosc DE: {group.get('responsible_de_name') or '-'}"
            if group.get("responsible_de_department"):
                responsible_line += f" ({group.get('responsible_de_department')})"
            write_line(responsible_line, font="F2", size=12, gap_after=6)

            group_entries = group.get("entries") or []
            if not group_entries:
                write_line("- Brak powiazanych work itemow.", size=12, indent=8, gap_after=3)

            for entry in group_entries:
                write_line(
                    f"- {entry.get('label') or '-'}",
                    size=12,
                    indent=8,
                    gap_after=3,
                    link_url=entry.get("url") or "",
                )
            if group_index < len(case_groups) - 1:
                write_rule()
    else:
        write_line("Brak wybranych projektow.", size=12, gap_after=6)

    footer_gap = 20 if case_groups else 12
    write_line(
        f"Wygenerowano przez: {generated_by} {saved_at.strftime('%d-%m-%Y %H:%M:%S')}",
        size=9,
        gap_after=footer_gap,
    )

    if current_lines:
        streams.append("\n".join(current_lines))
        page_annotations.append(current_annotations)

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    }
    page_object_ids = []
    max_object_id = 4

    for stream, annotations in zip(streams, page_annotations):
        page_id = max_object_id + 1
        content_id = max_object_id + 2
        max_object_id = content_id
        annotation_ids: list[int] = []

        for annotation in annotations:
            annotation_id = max_object_id + 1
            max_object_id = annotation_id
            x1, y1, x2, y2 = annotation["rect"]
            objects[annotation_id] = (
                f"<< /Type /Annot /Subtype /Link /Rect [{x1:.2f} {y1:.2f} {x2:.2f} {y2:.2f}] "
                f"/Border [0 0 0] /A << /S /URI /URI ({_pdf_safe_literal(annotation.get('url') or '')}) >> >>"
            ).encode("ascii", "ignore")
            annotation_ids.append(annotation_id)

        annot_part = f" /Annots [{' '.join(f'{annotation_id} 0 R' for annotation_id in annotation_ids)}]" if annotation_ids else ""
        page_object_ids.append(page_id)
        stream_bytes = stream.encode("ascii", "ignore")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R{annot_part} >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )

    kids = " ".join(f"{page_id} 0 R" for page_id in page_object_ids)
    objects[2] = f"<< /Type /Pages /Count {len(page_object_ids)} /Kids [{kids}] >>".encode("ascii")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0] * (max_object_id + 1)

    for object_id in range(1, max_object_id + 1):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode("ascii"))
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {max_object_id + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, max_object_id + 1):
        pdf.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        (
            f"trailer\n<< /Size {max_object_id + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("ascii")
    )
    return bytes(pdf)


def _build_lessons_learned_pdf_reportlab(
    selected_cases: list[dict], username: str, saved_at: datetime | None = None
) -> bytes:
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("ReportLab jest niedostepny.")

    saved_at = saved_at or datetime.now()
    generated_by = " ".join(str(username or "").split()) or "nieznany_uzytkownik"
    case_groups = _collect_lessons_learned_case_groups(selected_cases)
    total_items = sum(len(group.get("entries") or []) for group in case_groups)
    regular_font, bold_font = _ensure_unicode_pdf_fonts()
    palette = PDF_UI_COLORS
    buffer = io.BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=13 * mm,
        title="Lessons Learned",
        author=generated_by,
        pageCompression=0,
    )

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "LLBody",
        parent=styles["BodyText"],
        fontName=regular_font,
        fontSize=9.2,
        leading=11.2,
        textColor=palette["text"],
        alignment=TA_LEFT,
        spaceAfter=0,
    )
    small_style = ParagraphStyle(
        "LLSmall",
        parent=body_style,
        fontSize=8.1,
        leading=10,
        textColor=palette["muted"],
    )
    eyebrow_style = ParagraphStyle(
        "LLEyebrow",
        parent=body_style,
        fontName=bold_font,
        fontSize=8,
        leading=9.5,
        textColor=colors.white,
        alignment=TA_LEFT,
    )
    title_style = ParagraphStyle(
        "LLTitle",
        parent=body_style,
        fontName=bold_font,
        fontSize=18,
        leading=21,
        textColor=colors.white,
    )
    subtitle_style = ParagraphStyle(
        "LLSubtitle",
        parent=body_style,
        fontSize=8.8,
        leading=10.8,
        textColor=colors.white,
    )
    card_title_style = ParagraphStyle(
        "LLCardTitle",
        parent=body_style,
        fontName=bold_font,
        fontSize=12.2,
        leading=14.2,
        textColor=palette["text"],
    )
    card_subtitle_style = ParagraphStyle(
        "LLCardSubtitle",
        parent=body_style,
        fontSize=8.1,
        leading=10,
        textColor=palette["muted"],
    )
    case_title_style = ParagraphStyle(
        "LLCaseTitle",
        parent=body_style,
        fontName=bold_font,
        fontSize=11,
        leading=12.8,
        textColor=palette["text"],
    )
    case_meta_style = ParagraphStyle(
        "LLCaseMeta",
        parent=body_style,
        fontSize=8.6,
        leading=10.4,
        textColor=palette["muted"],
    )
    link_style = ParagraphStyle(
        "LLLink",
        parent=body_style,
        fontSize=8.9,
        leading=10.6,
        leftIndent=1,
        textColor=palette["text"],
    )
    chip_style = ParagraphStyle(
        "LLChip",
        parent=body_style,
        fontName=bold_font,
        fontSize=8.1,
        leading=9.6,
        textColor=palette["accent_dark"],
    )
    story = []

    def build_card(rows, width, background=None, border_color=None, paddings=None, inner_lines=None):
        card = Table([[row] for row in rows], colWidths=[width], repeatRows=0)
        left, right, top, bottom = paddings or (10, 10, 8, 8)
        style_commands = [
            ("BACKGROUND", (0, 0), (-1, -1), background or palette["card"]),
            ("BOX", (0, 0), (-1, -1), 0.6, border_color or palette["line"]),
            ("LEFTPADDING", (0, 0), (-1, -1), left),
            ("RIGHTPADDING", (0, 0), (-1, -1), right),
            ("TOPPADDING", (0, 0), (-1, -1), top),
            ("BOTTOMPADDING", (0, 0), (-1, -1), bottom),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]
        for line_row in inner_lines or []:
            style_commands.append(("LINEBELOW", (0, line_row), (-1, line_row), 0.35, palette["line_soft"]))
        card.setStyle(TableStyle(style_commands))
        return card

    def format_department_line(rows: list[dict], label: str) -> str:
        if not rows:
            return f"{label}: brak danych"
        segments = []
        for row in rows:
            department = " ".join(str(row.get("department") or "").split()) or "-"
            count = int(row.get("count") or row.get("value") or 0)
            segments.append(f"{department} ({count})")
        return f"{label}: " + ", ".join(segments)

    hero_meta = Table(
        [
            [
                Paragraph(f"Wygenerowano przez: <b>{_ll_pdf_escape(generated_by)}</b>", subtitle_style),
                Paragraph(saved_at.strftime("%d-%m-%Y %H:%M:%S"), subtitle_style),
            ]
        ],
        colWidths=[90 * mm, 65 * mm],
    )
    hero_meta.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
            ]
        )
    )

    header_card = build_card(
        [
            Paragraph("JAZZ REPORTING", eyebrow_style),
            Paragraph("Lessons Learned", title_style),
            Paragraph(
                "Raport przypadkĂłw wybranych w analizie z przypisanÄ… odpowiedzialnoĹ›ciÄ… DE. "
                "UkĹ‚ad odwzorowuje karty i etykiety widoczne w GUI.",
                subtitle_style,
            ),
            hero_meta,
        ],
        174 * mm,
        background=palette["accent"],
        border_color=palette["accent"],
        paddings=(12, 12, 10, 10),
    )
    story.append(header_card)
    story.append(Spacer(1, 6))

    if not case_groups:
        empty_table = build_card([Paragraph("Brak wybranych przypadkĂłw.", body_style)], 174 * mm)
        story.append(empty_table)
    else:
        for index, group in enumerate(case_groups, start=1):
            responsible_name = group.get("responsible_de_name") or "-"
            responsible_department = group.get("responsible_de_department") or ""
            assignee_label = responsible_name
            if responsible_department:
                assignee_label += f" ({responsible_department})"

            entries = group.get("entries") or []
            case_card_rows = [[Paragraph(f"{index}. {_ll_pdf_escape(group.get('title') or '-')}", card_title_style)]]
            if group.get("normalized_summary"):
                case_card_rows.append(
                    [
                        Paragraph(
                            f"Identyfikator grupy: {_ll_pdf_escape(group.get('normalized_summary') or '-')}",
                            card_subtitle_style,
                        )
                    ]
                )
            header_last_row = len(case_card_rows) - 1

            meta_grid = Table(
                [
                    [
                        build_card(
                            [
                                Paragraph("OdpowiedzialnoĹ›Ä‡ DE", chip_style),
                                Paragraph(_ll_pdf_escape(assignee_label), case_title_style),
                                Paragraph("Przypisanie z modalu Lessons Learned w GUI.", small_style),
                            ],
                            85 * mm,
                            background=palette["panel"],
                            paddings=(9, 9, 7, 7),
                        ),
                        build_card(
                            [
                                Paragraph("Zakres", chip_style),
                                Paragraph(format_department_line(group.get("creator_departments") or [], "Creator"), case_meta_style),
                                Paragraph(format_department_line(group.get("owner_departments") or [], "Owner"), case_meta_style),
                            ],
                            85 * mm,
                            background=palette["panel"],
                            paddings=(9, 9, 7, 7),
                        ),
                    ]
                ],
                colWidths=[86 * mm, 86 * mm],
            )
            meta_grid.setStyle(
                TableStyle(
                    [
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            case_card_rows.append([meta_grid])

            entry_rows = [[Paragraph("PowiÄ…zane work itemy", chip_style)]]
            if entries:
                for entry in entries:
                    link_target = entry.get("url") or ""
                    label = _ll_pdf_escape(entry.get("label") or "-")
                    if link_target:
                        main_line = (
                            f'<link href="{_ll_pdf_escape(link_target)}" color="#0B4A98">{label}</link>'
                        )
                    else:
                        main_line = label
                    entry_rows.append(
                        [
                            Paragraph(
                                f"<b>{_ll_pdf_escape(entry.get('type') or '-')}</b><br/>{main_line}<br/>"
                                f'<font color="#64758B">Creator: {_ll_pdf_escape(entry.get("creator_department") or "-")} | '
                                f'Owner: {_ll_pdf_escape(entry.get("owner_department") or "-")}</font>',
                                link_style,
                            )
                        ]
                    )
            else:
                entry_rows.append([Paragraph("Brak powiÄ…zanych work itemĂłw.", body_style)])

            footer_row = [
                Paragraph(f"Liczba wpisĂłw: {len(entries)}", chip_style),
                Paragraph("ĹąrĂłdĹ‚o: Analiza GUI", chip_style),
            ]
            footer_table = Table([footer_row], colWidths=[84 * mm, 84 * mm])
            footer_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), palette["chip"]),
                        ("BOX", (0, 0), (-1, -1), 0.45, palette["line"]),
                        ("INNERGRID", (0, 0), (-1, -1), 0.3, palette["line"]),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            case_card_rows.append([footer_table])

            entries_table = Table(entry_rows, colWidths=[174 * mm], repeatRows=1)
            entries_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), palette["panel_alt"]),
                        ("BACKGROUND", (0, 1), (-1, -1), colors.white),
                        ("BOX", (0, 0), (-1, -1), 0.45, palette["line"]),
                        ("INNERGRID", (0, 0), (-1, -1), 0.25, palette["line_soft"]),
                        ("LEFTPADDING", (0, 0), (-1, -1), 7),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            case_card_rows.append([entries_table])

            case_card = Table(case_card_rows, colWidths=[174 * mm], repeatRows=0)
            case_card_style = [
                ("BACKGROUND", (0, 0), (-1, -1), palette["card"]),
                ("BOX", (0, 0), (-1, -1), 0.6, palette["line"]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, header_last_row), 10),
                ("RIGHTPADDING", (0, 0), (-1, header_last_row), 10),
                ("TOPPADDING", (0, 0), (-1, 0), 10),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 4 if header_last_row > 0 else 8),
                ("LINEBELOW", (0, header_last_row), (-1, header_last_row), 0.35, palette["line_soft"]),
                ("LEFTPADDING", (0, header_last_row + 1), (-1, -1), 0),
                ("RIGHTPADDING", (0, header_last_row + 1), (-1, -1), 0),
                ("TOPPADDING", (0, header_last_row + 1), (-1, -1), 0),
                ("BOTTOMPADDING", (0, header_last_row + 1), (-1, -1), 0),
            ]
            if header_last_row > 0:
                case_card_style.extend(
                    [
                        ("TOPPADDING", (0, 1), (-1, header_last_row), 0),
                        ("BOTTOMPADDING", (0, 1), (-1, header_last_row), 8),
                    ]
                )
            case_card.setStyle(TableStyle(case_card_style))
            story.extend([case_card, Spacer(1, 7)])

    def draw_page(canvas, doc_obj):
        width, height = A4
        canvas.saveState()
        canvas.setFillColor(colors.HexColor("#F8FBFF"))
        canvas.rect(0, 0, width, height, stroke=0, fill=1)
        canvas.setFillColor(colors.HexColor("#EAF2FB"))
        canvas.rect(0, height - 18 * mm, width, 18 * mm, stroke=0, fill=1)
        canvas.setStrokeColor(palette["line"])
        canvas.setLineWidth(0.4)
        canvas.line(doc.leftMargin, 12 * mm, width - doc.rightMargin, 12 * mm)
        canvas.setFont(regular_font, 7.6)
        canvas.setFillColor(palette["muted"])
        canvas.drawRightString(width - doc.rightMargin, 8 * mm, f"Strona {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    return buffer.getvalue()


def _build_lessons_learned_pdf(selected_cases: list[dict], username: str, saved_at: datetime | None = None) -> bytes:
    if REPORTLAB_AVAILABLE:
        return _build_lessons_learned_pdf_reportlab(selected_cases, username=username, saved_at=saved_at)
    return _build_lessons_learned_pdf_legacy(selected_cases, username=username, saved_at=saved_at)


def _parse_created_date(value: str):
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _safe_display_name(obj) -> str:
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return obj.get("title") or obj.get("label") or obj.get("name") or obj.get("value") or ""
    if isinstance(obj, list) and obj:
        return _safe_display_name(obj[0])
    return ""

def _safe_display_name(value) -> str:
    if not value:
        return ""

    # string â€“ moĹĽe byÄ‡ juĹĽ imiÄ™ nazwisko albo URI
    if isinstance(value, str):
        # jeĹ›li URI â€“ weĹş ostatni fragment po '/' lub '#'
        if "://" in value:
            part = value.rsplit("/", 1)[-1]
            part = part.rsplit("#", 1)[-1]
            return part
        return value

    # dict z rdf:resource / dcterms:title / name
    if isinstance(value, dict):
        for k in ("dcterms:title", "dc:title", "name", "foaf:name", "displayName"):
            if k in value and isinstance(value[k], str):
                return value[k]
        res = value.get("rdf:resource") or value.get("resource") or ""
        return _safe_display_name(res)

    # lista â€“ weĹş pierwszy element
    if isinstance(value, list) and value:
        return _safe_display_name(value[0])

    return str(value)



# --- Caching + RDF helpers (ĹĽeby mapowaÄ‡ login -> imiÄ™ i nazwisko oraz dociÄ…gaÄ‡ typ WI/ownedBy) ---
_USER_NAME_CACHE: dict[str, str] = {}
_WI_DETAIL_CACHE: dict[str, dict] = {}

_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dcterms": "http://purl.org/dc/terms/",
    "dc": "http://purl.org/dc/elements/1.1/",
    "foaf": "http://xmlns.com/foaf/0.1/",
    "oslc": "http://open-services.net/ns/core#",
    "rtc_cm": "http://jazz.net/xmlns/prod/jazz/rtc/cm/1.0/",
    "oslc_cm": "http://open-services.net/ns/cm#",
}


def _get_rdf_xml(session: requests.Session, url: str, debug_log=None) -> str | None:
    try:
        r = session.get(url, headers={"Accept": "application/rdf+xml"}, timeout=60, allow_redirects=True)
        ct = r.headers.get("Content-Type", "")
        if r.status_code != 200:
            if debug_log is not None:
                _log(debug_log, f"  RDF GET {url} -> {r.status_code} ct={ct}")
                _log(debug_log, f"  RDF body head: {(r.text or '')[:200]}")
            return None
        if ("xml" not in ct) and ("rdf" not in ct):
            # czasem serwer mimo Accept oddaje HTML
            if debug_log is not None:
                _log(debug_log, f"  RDF GET {url} -> 200 ale ct={ct} (oczekiwano xml).")
                _log(debug_log, f"  body head: {(r.text or '')[:200]}")
            return None
        return r.text
    except Exception as e:
        if debug_log is not None:
            _log(debug_log, f"  RDF GET exception for {url}: {e}")
        return None


def resolve_user_fullname(session: requests.Session, user_uri: str, debug_log=None) -> str:
    """Z /jts/users/<login> prĂłbuje wydobyÄ‡ peĹ‚ne imiÄ™ i nazwisko (foaf:name / dcterms:title)."""
    if not user_uri:
        return ""
    if user_uri in _USER_NAME_CACHE:
        return _USER_NAME_CACHE[user_uri]

    xml_text = _get_rdf_xml(session, user_uri, debug_log=debug_log)
    if not xml_text:
        # fallback: login z koĹ„cĂłwki URL
        name = _safe_display_name(user_uri)
        _USER_NAME_CACHE[user_uri] = name
        return name

    try:
        root = ET.fromstring(xml_text)

        # najczÄ™Ĺ›ciej dziaĹ‚a foaf:name
        el = root.find(".//foaf:name", _NS)
        if el is not None and (el.text or "").strip():
            name = el.text.strip()
            _USER_NAME_CACHE[user_uri] = name
            return name

        # czasem dcterms:title / dc:title
        for path in (".//dcterms:title", ".//dc:title"):
            el = root.find(path, _NS)
            if el is not None and (el.text or "").strip():
                name = el.text.strip()
                _USER_NAME_CACHE[user_uri] = name
                return name

    except Exception as e:
        if debug_log is not None:
            _log(debug_log, f"  parse user rdf failed for {user_uri}: {e}")

    name = _safe_display_name(user_uri)
    _USER_NAME_CACHE[user_uri] = name
    return name


def fetch_workitem_detail(session: requests.Session, wi_about_url: str, debug_log=None) -> dict:
    """DociÄ…ga szczegĂłĹ‚y WI po rdf:about (RDF/XML) i prĂłbuje wyciÄ…gnÄ…Ä‡ ownedBy + workItemType + creator URI."""
    if not wi_about_url:
        return {}
    if wi_about_url in _WI_DETAIL_CACHE:
        return _WI_DETAIL_CACHE[wi_about_url]

    xml_text = _get_rdf_xml(session, wi_about_url, debug_log=debug_log)
    if not xml_text:
        _WI_DETAIL_CACHE[wi_about_url] = {}
        return {}

    out: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)

        # ownedBy
        el = root.find(".//rtc_cm:ownedBy", _NS)
        if el is not None:
            uri = el.attrib.get(f"{{{_NS['rdf']}}}resource", "")
            if uri:
                out["ownedBy"] = uri

        # workItemType
        el = root.find(".//rtc_cm:workItemType", _NS)
        if el is not None:
            uri = el.attrib.get(f"{{{_NS['rdf']}}}resource", "")
            if uri:
                out["workItemType"] = uri
            else:
                # czasem jako tekst
                if (el.text or "").strip():
                    out["workItemTypeName"] = el.text.strip()

        # creator (czasem jest teĹĽ w szczegĂłĹ‚ach)
        el = root.find(".//dcterms:creator", _NS) or root.find(".//dc:creator", _NS)
        if el is not None:
            uri = el.attrib.get(f"{{{_NS['rdf']}}}resource", "")
            if uri:
                out["creator"] = uri

    except Exception as e:
        if debug_log is not None:
            _log(debug_log, f"  parse WI rdf failed for {wi_about_url}: {e}")

    _WI_DETAIL_CACHE[wi_about_url] = out
    return out

def _parse_oslc_rdf_xml_to_pseudo_json(xml_text: str) -> dict:
    """
    Minimalny parser OSLC RDF/XML -> pseudo-json:
    Zwraca dict z kluczem "oslc:results": [ {dc:identifier, dc:title, dc:created, dc:creator, rtc_cm:ownedBy, dc:type}, ... ]
    Uwaga: to jest fallback diagnostyczny â€“ ma wystarczyÄ‡ do zobaczenia statusĂłw i czy w ogĂłle sÄ… wyniki.
    """
    ns = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "dcterms": "http://purl.org/dc/terms/",
        "dc": "http://purl.org/dc/elements/1.1/",
        "oslc": "http://open-services.net/ns/core#",
        "rtc_cm": "http://jazz.net/xmlns/prod/jazz/rtc/cm/1.0/",
    }

    root = ET.fromstring(xml_text)

    # Heurystyka: rekordy to "opisy" zasobĂłw zawierajÄ…ce identifier + title
    results = []

    for desc in root.findall(".//rdf:Description", ns):
        ident_el = desc.find("./dcterms:identifier", ns) or desc.find("./dc:identifier", ns)
        title_el = desc.find("./dcterms:title", ns) or desc.find("./dc:title", ns)
        created_el = desc.find("./dcterms:created", ns)

        if ident_el is None and title_el is None:
            continue

        item = {}
        if ident_el is not None and ident_el.text:
            item["dc:identifier"] = ident_el.text.strip()
        if title_el is not None and title_el.text:
            item["dc:title"] = title_el.text.strip()
        if created_el is not None and created_el.text:
            item["dc:created"] = created_el.text.strip()

        # creator / ownedBy czÄ™sto sÄ… jako rdf:resource bez tytuĹ‚u
        creator_el = desc.find("./dcterms:creator", ns)
        if creator_el is not None:
            res = creator_el.attrib.get(f"{{{ns['rdf']}}}resource")
            if res:
                item["dc:creator"] = res

        owned_el = desc.find("./rtc_cm:ownedBy", ns)
        if owned_el is not None:
            res = owned_el.attrib.get(f"{{{ns['rdf']}}}resource")
            if res:
                item["rtc_cm:ownedBy"] = res

        # type (teĹĽ bywa jako resource)
        type_el = desc.find("./rdf:type", ns)
        if type_el is not None:
            res = type_el.attrib.get(f"{{{ns['rdf']}}}resource")
            if res:
                item["dc:type"] = res

        results.append(item)

    return {"oslc:results": results}

def _log(debug_log: list[str], msg: str):
    if debug_log is not None:
        debug_log.append(msg)


def _normalize_lookup_key(value: str) -> str:
    clean = _ascii_fold(" ".join(str(value or "").split())).lower()
    clean = re.sub(r"[^a-z0-9]+", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _first_text(element, paths: list[str], ns_map: dict[str, str]) -> str:
    for path in paths:
        found = element.find(path, ns_map)
        if found is None:
            continue
        text = " ".join(str(found.text or "").split())
        if text:
            return text
    return ""


def _first_rdf_resource(element, paths: list[str], ns_map: dict[str, str]) -> str:
    for path in paths:
        found = element.find(path, ns_map)
        if found is None:
            continue
        resource = (
            found.attrib.get(f"{{{ns_map['rdf']}}}resource")
            or found.attrib.get("resource")
            or found.attrib.get("href")
            or ""
        )
        resource = resource.strip()
        if resource:
            return resource
    return ""


def _all_rdf_resources(element, paths: list[str], ns_map: dict[str, str]) -> list[str]:
    resources: list[str] = []
    seen: set[str] = set()
    for path in paths:
        for found in element.findall(path, ns_map):
            resource = (
                found.attrib.get(f"{{{ns_map['rdf']}}}resource")
                or found.attrib.get("resource")
                or found.attrib.get("href")
                or ""
            )
            resource = resource.strip()
            if not resource or resource in seen:
                continue
            seen.add(resource)
            resources.append(resource)
    return resources


def _build_about_title_index(root: ET.Element, ns_map: dict[str, str]) -> dict[str, str]:
    indexed: dict[str, str] = {}

    for element in root.iter():
        about = (element.attrib.get(f"{{{ns_map['rdf']}}}about") or "").strip()
        if not about or about in indexed:
            continue
        title = _first_text(
            element,
            ["./dcterms:title", "./dc:title", ".//dcterms:title", ".//dc:title"],
            ns_map,
        )
        if title:
            indexed[about] = title

    return indexed


def _discover_lessons_learned_service_provider(
    session: requests.Session, project_name: str, debug_log: list[str] | None
) -> dict:
    headers = {"Accept": "application/rdf+xml", "OSLC-Core-Version": "2.0"}
    response = _request_with_trace(session, "GET", OSLC_CATALOG_URL, debug_log, headers=headers, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Nie moge pobrac katalogu OSLC dla CCM: status={response.status_code}.")

    root = ET.fromstring(response.text or "")
    about_title_index = _build_about_title_index(root, _NS)
    service_providers: list[dict] = []
    seen: set[str] = set()

    for element in root.findall(".//oslc:ServiceProvider", _NS):
        about = (
            element.attrib.get(f"{{{_NS['rdf']}}}about")
            or element.attrib.get(f"{{{_NS['rdf']}}}resource")
            or ""
        ).strip()
        if not about or about in seen:
            continue
        seen.add(about)
        title = _first_text(
            element,
            ["./dcterms:title", "./dc:title", ".//dcterms:title", ".//dc:title"],
            _NS,
        ) or about_title_index.get(about, "")
        service_providers.append({"about": about, "title": title})

    wanted_key = _normalize_lookup_key(project_name)
    ranked = sorted(
        service_providers,
        key=lambda item: (
            0 if _normalize_lookup_key(item.get("title") or "") == wanted_key else 1,
            0 if wanted_key and wanted_key in _normalize_lookup_key(item.get("title") or "") else 1,
            item.get("title") or item.get("about") or "",
        ),
    )

    for candidate in ranked:
        title_key = _normalize_lookup_key(candidate.get("title") or "")
        if title_key == wanted_key or (wanted_key and wanted_key in title_key):
            _log(debug_log, f"Wybrano ServiceProvider dla projektu '{project_name}': {candidate.get('about')}")
            return candidate

    available_titles = ", ".join(
        sorted(filter(None, {candidate.get("title", "") for candidate in service_providers}))
    )
    raise RuntimeError(
        f"Nie znaleziono projektu '{project_name}' w katalogu OSLC."
        + (f" Dostepne tytuly: {available_titles}." if available_titles else "")
    )


def _discover_lessons_learned_creation_factory(
    session: requests.Session, service_provider_url: str, work_item_type: str, debug_log: list[str] | None
) -> dict:
    headers = {"Accept": "application/rdf+xml", "OSLC-Core-Version": "2.0"}
    response = _request_with_trace(session, "GET", service_provider_url, debug_log, headers=headers, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Nie moge pobrac dokumentu ServiceProvider: status={response.status_code}.")

    root = ET.fromstring(response.text or "")
    about_title_index = _build_about_title_index(root, _NS)
    factories: list[dict] = []
    seen: set[str] = set()

    for element in root.findall(".//oslc:CreationFactory", _NS):
        creation_uri = _first_rdf_resource(
            element,
            ["./oslc:creation", ".//oslc:creation"],
            _NS,
        )
        if not creation_uri or creation_uri in seen:
            continue
        seen.add(creation_uri)
        about = (element.attrib.get(f"{{{_NS['rdf']}}}about") or "").strip()
        title = _first_text(
            element,
            ["./dcterms:title", "./dc:title", ".//dcterms:title", ".//dc:title"],
            _NS,
        ) or about_title_index.get(about, "")
        resource_shape = _first_rdf_resource(
            element,
            ["./oslc:resourceShape", ".//oslc:resourceShape"],
            _NS,
        )
        resource_types = []
        for resource_type in element.findall("./oslc:resourceType", _NS) + element.findall(".//oslc:resourceType", _NS):
            uri = (resource_type.attrib.get(f"{{{_NS['rdf']}}}resource") or "").strip()
            if uri:
                resource_types.append(uri)
        factories.append(
            {
                "creation_uri": creation_uri,
                "resource_shape": resource_shape,
                "title": title,
                "resource_types": resource_types,
            }
        )

    if not factories:
        raise RuntimeError("ServiceProvider nie zawiera zadnej fabryki tworzenia work itemow.")

    wanted_key = _normalize_lookup_key(work_item_type)

    def _factory_score(factory: dict) -> tuple[int, int, str]:
        haystacks = [factory.get("title") or "", factory.get("creation_uri") or "", factory.get("resource_shape") or ""]
        haystacks.extend(factory.get("resource_types") or [])
        normalized_haystacks = [_normalize_lookup_key(value) for value in haystacks if value]
        exact = any(value == wanted_key for value in normalized_haystacks)
        contains = any(wanted_key and wanted_key in value for value in normalized_haystacks)
        return (
            0 if exact else 1,
            0 if contains else 1,
            factory.get("title") or factory.get("creation_uri") or "",
        )

    selected = sorted(factories, key=_factory_score)[0]
    selected_haystacks = [
        _normalize_lookup_key(selected.get("title") or ""),
        _normalize_lookup_key(selected.get("creation_uri") or ""),
        _normalize_lookup_key(selected.get("resource_shape") or ""),
    ] + [_normalize_lookup_key(item) for item in selected.get("resource_types") or []]

    if not any(wanted_key and wanted_key in value for value in selected_haystacks if value):
        available = ", ".join(filter(None, [factory.get("title") or factory.get("creation_uri") for factory in factories]))
        raise RuntimeError(
            f"Nie znaleziono fabryki tworzenia dla typu '{work_item_type}'."
            + (f" Dostepne fabryki: {available}." if available else "")
        )

    _log(
        debug_log,
        f"Wybrano CreationFactory dla typu '{work_item_type}': {selected.get('creation_uri')} shape={selected.get('resource_shape')}",
    )
    return selected


def _parse_resource_shape_property(element: ET.Element) -> dict:
    property_definition = _first_rdf_resource(
        element,
        ["./oslc:propertyDefinition", ".//oslc:propertyDefinition"],
        _NS,
    )
    if not property_definition:
        return {}

    return {
        "property_definition": property_definition,
        "title": _first_text(
            element,
            ["./dcterms:title", "./dc:title", ".//dcterms:title", ".//dc:title"],
            _NS,
        ),
        "value_type": _first_rdf_resource(
            element,
            ["./oslc:valueType", ".//oslc:valueType"],
            _NS,
        ),
        "representation": _first_rdf_resource(
            element,
            ["./oslc:representation", ".//oslc:representation"],
            _NS,
        ),
        "occurs": _first_rdf_resource(
            element,
            ["./oslc:occurs", ".//oslc:occurs"],
            _NS,
        ),
        "allowed_values_uri": _first_rdf_resource(
            element,
            [
                "./oslc:allowedValues",
                ".//oslc:allowedValues",
                "./oslc:allowedValues/oslc:AllowedValues/oslc:allowedValues",
            ],
            _NS,
        ),
        "allowed_value_uris": _all_rdf_resources(
            element,
            ["./oslc:allowedValue", ".//oslc:allowedValue"],
            _NS,
        ),
    }


def _load_resource_shape(session: requests.Session, resource_shape_url: str, debug_log: list[str] | None) -> dict:
    headers = {"Accept": "application/rdf+xml", "OSLC-Core-Version": "2.0"}
    response = _request_with_trace(session, "GET", resource_shape_url, debug_log, headers=headers, timeout=60)
    if response.status_code != 200:
        raise RuntimeError(f"Nie moge pobrac resource shape: status={response.status_code}.")

    root = ET.fromstring(response.text or "")
    properties: list[dict] = []
    property_by_about: dict[str, dict] = {}

    for element in root.iter():
        about = (element.attrib.get(f"{{{_NS['rdf']}}}about") or "").strip()
        parsed = _parse_resource_shape_property(element)
        if not parsed:
            continue
        if about:
            property_by_about[about] = parsed
        properties.append(parsed)

    referenced_properties: list[dict] = []
    seen_ref_uris: set[str] = set()
    for shape_element in root.findall(".//oslc:ResourceShape", _NS):
        for property_ref in shape_element.findall("./oslc:property", _NS):
            resource_uri = (property_ref.attrib.get(f"{{{_NS['rdf']}}}resource") or "").strip()
            if not resource_uri or resource_uri in seen_ref_uris:
                continue
            seen_ref_uris.add(resource_uri)
            parsed = property_by_about.get(resource_uri)
            if parsed:
                referenced_properties.append(parsed)

    if referenced_properties:
        properties = referenced_properties

    deduped_properties: list[dict] = []
    seen_property_definitions: set[str] = set()
    for prop in properties:
        property_definition = prop.get("property_definition") or ""
        if not property_definition or property_definition in seen_property_definitions:
            continue
        seen_property_definitions.add(property_definition)
        deduped_properties.append(prop)
    properties = deduped_properties

    if not properties:
        raise RuntimeError("Resource shape nie zawiera zadnych pol.")

    return {"properties": properties}


def _find_shape_property(properties: list[dict], labels: list[str] | None = None, definitions: list[str] | None = None) -> dict:
    labels = labels or []
    definitions = definitions or []
    label_keys = [_normalize_lookup_key(label) for label in labels if label]
    definition_keys = {_normalize_lookup_key(item) for item in definitions if item}
    best_match: dict = {}
    best_score = (10, 10, "")

    for prop in properties:
        definition = prop.get("property_definition") or ""
        title = prop.get("title") or ""
        definition_key = _normalize_lookup_key(definition)
        title_key = _normalize_lookup_key(title)
        exact_label = 0 if label_keys and title_key in label_keys else 1
        contains_label = 0 if any(label_key and label_key in title_key for label_key in label_keys) else 1
        exact_definition = 0 if definition in definitions or definition_key in definition_keys else 1
        contains_definition = 0 if any(item and item in definition_key for item in definition_keys) else 1
        score = (
            min(exact_label, exact_definition),
            min(contains_label, contains_definition),
            title or definition,
        )
        if score < best_score and (score[0] == 0 or score[1] == 0):
            best_score = score
            best_match = prop

    return best_match


def _select_lessons_learned_shape_fields(properties: list[dict]) -> dict:
    standard_title = _find_shape_property(
        properties,
        labels=["Title"],
        definitions=[
            "http://purl.org/dc/terms/title",
            "http://purl.org/dc/elements/1.1/title",
        ],
    ) or {
        "property_definition": "http://purl.org/dc/terms/title",
        "title": "Title",
        "value_type": "http://www.w3.org/2001/XMLSchema#string",
    }
    standard_description = _find_shape_property(
        properties,
        labels=["Description"],
        definitions=[
            "http://purl.org/dc/terms/description",
            "http://purl.org/dc/elements/1.1/description",
        ],
    ) or {
        "property_definition": "http://purl.org/dc/terms/description",
        "title": "Description",
        "value_type": "http://www.w3.org/2001/XMLSchema#string",
    }
    case_title = _find_shape_property(properties, labels=["Case Title"], definitions=["case title"])
    case_description = _find_shape_property(
        properties,
        labels=["Case Description"],
        definitions=["case description"],
    )
    input_creator = _find_shape_property(properties, labels=["Input Creator"], definitions=["input creator"])

    if not input_creator:
        raise RuntimeError("Nie znaleziono pola 'Input Creator' w resource shape projektu Lessons Learned.")

    return {
        "standard_title": standard_title,
        "standard_description": standard_description,
        "case_title": case_title,
        "case_description": case_description,
        "input_creator": input_creator,
    }


def _shape_property_expects_resource(prop: dict) -> bool:
    value_type = (prop.get("value_type") or "").lower()
    representation = (prop.get("representation") or "").lower()
    return "resource" in value_type or representation.endswith("#resource") or representation.endswith("#reference")


def _extract_allowed_value_entries(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text or "")
    entries: list[dict] = []
    seen: set[str] = set()

    for element in root.iter():
        about = (
            element.attrib.get(f"{{{_NS['rdf']}}}about")
            or element.attrib.get(f"{{{_NS['rdf']}}}resource")
            or ""
        ).strip()
        label = _first_text(
            element,
            [
                "./dcterms:title",
                "./dc:title",
                ".//dcterms:title",
                ".//dc:title",
                "./rdfs:label",
                ".//rdfs:label",
                "./foaf:name",
                ".//foaf:name",
                "./rtc_cm:displayName",
                ".//rtc_cm:displayName",
            ],
            {**_NS, "rdfs": "http://www.w3.org/2000/01/rdf-schema#"},
        )
        if not about and not label:
            continue
        key = f"{about}|{label}"
        if key in seen:
            continue
        seen.add(key)
        entries.append({"uri": about, "label": label})

    return entries


def _load_allowed_values(session: requests.Session, url: str, debug_log: list[str] | None) -> list[dict]:
    if not url:
        return []
    response = _request_with_trace(
        session,
        "GET",
        url,
        debug_log,
        headers={"Accept": "application/rdf+xml", "OSLC-Core-Version": "2.0"},
        timeout=60,
    )
    if response.status_code != 200:
        return []
    try:
        return _extract_allowed_value_entries(response.text or "")
    except Exception as exc:
        _log(debug_log, f"Nie udalo sie sparsowac allowed values dla {url}: {exc}")
        return []


def _match_allowed_value(entries: list[dict], wanted_label: str) -> dict:
    wanted_key = _normalize_lookup_key(wanted_label)
    if not wanted_key:
        return {}

    reversed_key = _normalize_lookup_key(" ".join(reversed(wanted_label.split())))
    wanted_tokens = set(wanted_key.split())
    reversed_tokens = set(reversed_key.split()) if reversed_key else set()

    def entry_score(entry: dict) -> tuple[int, int, int, str]:
        label_key = _normalize_lookup_key(entry.get("label") or "")
        uri_key = _normalize_lookup_key(entry.get("uri") or "")
        label_tokens = set(label_key.split())
        uri_tokens = set(uri_key.split())
        exact = 0 if label_key == wanted_key or uri_key == wanted_key else 1
        reversed_exact = 0 if reversed_key and (label_key == reversed_key or uri_key == reversed_key) else 1
        contains = 0 if any(key and key in value for key in [wanted_key, reversed_key] for value in [label_key, uri_key]) else 1
        token_match = 0 if (
            (wanted_tokens and wanted_tokens.issubset(label_tokens))
            or (wanted_tokens and wanted_tokens.issubset(uri_tokens))
            or (reversed_tokens and reversed_tokens.issubset(label_tokens))
            or (reversed_tokens and reversed_tokens.issubset(uri_tokens))
        ) else 1
        return (exact, reversed_exact, contains, token_match, entry.get("label") or entry.get("uri") or "")

    ranked = sorted(entries, key=entry_score)
    if not ranked:
        return {}
    best = ranked[0]
    if entry_score(best)[:4] == (1, 1, 1, 1):
        return {}
    return best


def _load_direct_allowed_value_entries(session: requests.Session, uris: list[str], debug_log: list[str] | None) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()

    for uri in uris or []:
        if not uri or uri in seen:
            continue
        seen.add(uri)
        label = ""
        if "/jts/users/" in uri:
            label = resolve_user_fullname(session, uri, debug_log=debug_log)
        if not label:
            xml_text = _get_rdf_xml(session, uri, debug_log=debug_log)
            if xml_text:
                try:
                    nested_entries = _extract_allowed_value_entries(xml_text)
                    exact_match = next((entry for entry in nested_entries if (entry.get("uri") or "").strip() == uri), {})
                    label = exact_match.get("label", "") if exact_match else ""
                except Exception:
                    label = ""
        entries.append({"uri": uri, "label": label})

    return entries


def _extract_user_candidates_from_html(html_text: str) -> list[dict]:
    soup = BeautifulSoup(html_text or "", "html.parser")
    candidates: list[dict] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()
        if "/jts/users/" not in href:
            continue
        uri = urljoin(JAZZ_BASE_URL, href)
        if uri in seen:
            continue
        seen.add(uri)
        label = " ".join(anchor.get_text(" ", strip=True).split()) or " ".join(str(anchor.get("title") or "").split())
        candidates.append({"uri": uri, "label": label})

    return candidates


def _build_jazz_login_candidates(display_name: str) -> list[str]:
    raw_tokens = [token for token in re.split(r"\s+", str(display_name or "").strip()) if token]
    if len(raw_tokens) < 2:
        return []

    def normalize_token(token: str) -> str:
        return _normalize_person_name(token).replace(" ", "")

    def first_surname_chunk(token: str) -> str:
        first_chunk = re.split(r"[-\u2010-\u2015]", token, maxsplit=1)[0]
        return normalize_token(first_chunk)

    surname_first = first_surname_chunk(raw_tokens[0])
    given_last = normalize_token(raw_tokens[-1])
    given_first = normalize_token(raw_tokens[0])
    surname_last = first_surname_chunk(raw_tokens[-1])

    candidates: list[str] = []
    seen: set[str] = set()
    orders = [
        (surname_first, given_last),  # surname first, given name last
        (surname_last, given_first),  # given name first, surname last
    ]

    for surname, given_name in orders:
        if not surname or not given_name:
            continue
        for prefix_len in (1, 2):
            fragment = given_name[:prefix_len]
            if not fragment:
                continue
            candidate = f"{surname}{fragment}"
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)

    return candidates


def _resolve_jazz_user_uri(session: requests.Session, display_name: str, debug_log: list[str] | None) -> str:
    wanted_name = " ".join(str(display_name or "").split())
    wanted_key = _normalize_lookup_key(wanted_name)
    if not wanted_key:
        return ""

    for login_candidate in _build_jazz_login_candidates(wanted_name):
        candidate_uri = urljoin(JTS_APP_URL, f"users/{quote(login_candidate)}")
        resolved_name = _normalize_lookup_key(resolve_user_fullname(session, candidate_uri, debug_log=debug_log))
        if resolved_name == wanted_key:
            return candidate_uri
        wanted_tokens = set(wanted_key.split())
        resolved_tokens = set(resolved_name.split())
        if wanted_tokens and wanted_tokens == resolved_tokens:
            return candidate_uri

    search_terms = [wanted_name]
    tokens = wanted_name.split()
    if len(tokens) > 1:
        reversed_name = " ".join(reversed(tokens))
        if reversed_name not in search_terms:
            search_terms.append(reversed_name)

    for term in search_terms:
        search_url = urljoin(JTS_APP_URL, f"users?searchTerm={quote(term)}")
        response = _request_with_trace(session, "GET", search_url, debug_log, timeout=30)
        if response.status_code != 200:
            continue
        candidates = _extract_user_candidates_from_html(response.text or "")
        for candidate in candidates:
            if _normalize_lookup_key(candidate.get("label") or "") == wanted_key:
                return candidate["uri"]
        if len(candidates) == 1 and _normalize_lookup_key(candidates[0].get("label") or ""):
            return candidates[0]["uri"]

    return ""


def _split_property_definition_uri(uri: str) -> tuple[str, str]:
    if "#" in uri:
        namespace, local_name = uri.rsplit("#", 1)
        return f"{namespace}#", local_name
    if "/" in uri:
        namespace, local_name = uri.rsplit("/", 1)
        return f"{namespace}/", local_name
    raise ValueError(f"Nieprawidlowy URI pola OSLC: {uri}")


def _build_lessons_learned_creation_payload(field_values: list[dict]) -> bytes:
    root = ET.Element(f"{{{_NS['rdf']}}}RDF")
    change_request = ET.SubElement(root, f"{{{_NS['oslc_cm']}}}ChangeRequest")
    namespace_prefixes = {
        "rdf": _NS["rdf"],
        "oslc_cm": _NS["oslc_cm"],
        "dcterms": _NS["dcterms"],
        "dc": _NS["dc"],
        "rtc_cm": _NS["rtc_cm"],
    }

    for field in field_values:
        namespace_uri, local_name = _split_property_definition_uri(field["property_definition"])
        if namespace_uri not in namespace_prefixes.values():
            prefix = f"xns{len(namespace_prefixes) + 1}"
            namespace_prefixes[prefix] = namespace_uri
            ET.register_namespace(prefix, namespace_uri)
        element = ET.SubElement(change_request, f"{{{namespace_uri}}}{local_name}")
        if field.get("kind") == "resource":
            element.set(f"{{{_NS['rdf']}}}resource", field["value"])
        else:
            element.text = field["value"]

    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _build_lessons_learned_work_item_candidates(selected_cases: list[dict]) -> list[dict]:
    candidates: list[dict] = []

    for group in _collect_lessons_learned_case_groups(selected_cases):
        responsible_name = " ".join(str(group.get("responsible_de_name") or "").split())
        responsible_department = " ".join(str(group.get("responsible_de_department") or "").split())
        responsible_jazz_login = normalize_jazz_login(group.get("responsible_de_jazz_login") or "")
        candidate_title = " ".join(str(group.get("title") or group.get("normalized_summary") or "").split()) or "-"
        case_key = build_lessons_learned_case_key(group)
        candidates.append(
            {
                "case_key": case_key,
                "title": candidate_title,
                "description": candidate_title,
                "normalized_summary": " ".join(str(group.get("normalized_summary") or "").split()),
                "input_creator_name": responsible_name,
                "input_creator_login": responsible_jazz_login,
                "input_creator_department": responsible_department,
                "group_title": group.get("title") or "",
                "entry_count": int(group.get("entry_count") or 0),
            }
        )

    return candidates


def _extract_created_work_item_details(response: requests.Response) -> dict:
    details = {
        "resource_uri": (response.headers.get("Location") or response.headers.get("Content-Location") or "").strip(),
        "identifier": "",
    }

    if response.text:
        try:
            root = ET.fromstring(response.text)
            if not details["resource_uri"]:
                for element in root.iter():
                    about = (element.attrib.get(f"{{{_NS['rdf']}}}about") or "").strip()
                    if about:
                        details["resource_uri"] = about
                        break
            identifier = _first_text(root, [".//dcterms:identifier", ".//dc:identifier"], _NS)
            if identifier:
                details["identifier"] = identifier
        except Exception:
            pass

    if not details["identifier"] and details["resource_uri"]:
        match = re.search(r"(?:id=|WorkItem/)(\d+)", details["resource_uri"], flags=re.IGNORECASE)
        if match:
            details["identifier"] = match.group(1)

    return details


def create_lessons_learned_work_items(
    session: requests.Session, selected_cases: list[dict], debug_log: list[str] | None = None
) -> dict:
    candidates = _build_lessons_learned_work_item_candidates(selected_cases)
    if not candidates:
        return {"status": "ok", "created": [], "failed": []}

    service_provider = _discover_lessons_learned_service_provider(
        session,
        project_name=LESSONS_LEARNED_PROJECT_NAME,
        debug_log=debug_log,
    )
    creation_factory = _discover_lessons_learned_creation_factory(
        session,
        service_provider_url=service_provider["about"],
        work_item_type=LESSONS_LEARNED_WORK_ITEM_TYPE,
        debug_log=debug_log,
    )
    resource_shape_url = (creation_factory.get("resource_shape") or "").strip()
    if not resource_shape_url:
        raise RuntimeError("Fabryka tworzenia LL Candidate nie udostepnia resource shape.")

    shape = _load_resource_shape(session, resource_shape_url, debug_log)
    fields = _select_lessons_learned_shape_fields(shape["properties"])
    user_uri_cache: dict[str, str] = {}
    allowed_value_cache: dict[str, list[dict]] = {}
    created: list[dict] = []
    failed: list[dict] = []

    for candidate in candidates:
        try:
            field_values_by_definition: dict[str, dict] = {}

            def add_literal(prop: dict, value: str) -> None:
                if not prop:
                    return
                clean_value = " ".join(str(value or "").split())
                if not clean_value:
                    return
                field_values_by_definition[prop["property_definition"]] = {
                    "property_definition": prop["property_definition"],
                    "value": clean_value,
                    "kind": "literal",
                }

            def add_input_creator(prop: dict, person_name: str) -> None:
                if not prop:
                    return
                clean_name = " ".join(str(person_name or "").split())
                clean_login = normalize_jazz_login(candidate.get("input_creator_login") or "")
                if not clean_name:
                    return
                direct_allowed_entries = _load_direct_allowed_value_entries(
                    session,
                    prop.get("allowed_value_uris") or [],
                    debug_log,
                )
                matched_entry = _match_allowed_value(direct_allowed_entries, clean_name)
                if matched_entry:
                    matched_uri = (matched_entry.get("uri") or "").strip()
                    matched_label = " ".join(str(matched_entry.get("label") or "").split())
                    if _shape_property_expects_resource(prop) and matched_uri:
                        field_values_by_definition[prop["property_definition"]] = {
                            "property_definition": prop["property_definition"],
                            "value": matched_uri,
                            "kind": "resource",
                        }
                        return
                    if matched_label:
                        add_literal(prop, matched_label)
                        return
                allowed_values_uri = (prop.get("allowed_values_uri") or "").strip()
                if allowed_values_uri:
                    allowed_entries = allowed_value_cache.get(allowed_values_uri)
                    if allowed_entries is None:
                        allowed_entries = _load_allowed_values(session, allowed_values_uri, debug_log)
                        allowed_value_cache[allowed_values_uri] = allowed_entries
                    matched_entry = _match_allowed_value(allowed_entries, clean_name)
                    if matched_entry:
                        matched_uri = (matched_entry.get("uri") or "").strip()
                        matched_label = " ".join(str(matched_entry.get("label") or "").split())
                        if _shape_property_expects_resource(prop) and matched_uri:
                            field_values_by_definition[prop["property_definition"]] = {
                                "property_definition": prop["property_definition"],
                                "value": matched_uri,
                                "kind": "resource",
                            }
                            return
                        if matched_label:
                            add_literal(prop, matched_label)
                            return
                if _shape_property_expects_resource(prop):
                    if clean_login:
                        field_values_by_definition[prop["property_definition"]] = {
                            "property_definition": prop["property_definition"],
                            "value": urljoin(JTS_APP_URL, f"users/{quote(clean_login)}"),
                            "kind": "resource",
                        }
                        return
                    user_uri = user_uri_cache.get(clean_name)
                    if user_uri is None:
                        user_uri = _resolve_jazz_user_uri(session, clean_name, debug_log)
                        user_uri_cache[clean_name] = user_uri
                    if not user_uri:
                        raise RuntimeError(f"Nie udalo sie odnalezc uzytkownika Jazz dla '{clean_name}'.")
                    field_values_by_definition[prop["property_definition"]] = {
                        "property_definition": prop["property_definition"],
                        "value": user_uri,
                        "kind": "resource",
                    }
                else:
                    add_literal(prop, clean_name)

            add_literal(fields["standard_title"], candidate["title"])
            add_literal(fields["standard_description"], candidate["description"])
            add_literal(fields.get("case_title") or {}, candidate["title"])
            add_literal(fields.get("case_description") or {}, candidate["description"])
            add_input_creator(fields["input_creator"], candidate["input_creator_name"])

            payload = _build_lessons_learned_creation_payload(list(field_values_by_definition.values()))
            response = _request_with_trace(
                session,
                "POST",
                creation_factory["creation_uri"],
                debug_log,
                data=payload,
                headers={
                    "Accept": "application/rdf+xml, application/xml, application/json",
                    "Content-Type": "application/rdf+xml",
                    "OSLC-Core-Version": "2.0",
                },
                timeout=60,
            )
            if response.status_code not in (200, 201):
                body_preview = " ".join((response.text or "").split())[:300]
                raise RuntimeError(
                    f"Jazz zwrocil status {response.status_code} podczas tworzenia LL Candidate."
                    + (f" Odpowiedz: {body_preview}" if body_preview else "")
                )

            details = _extract_created_work_item_details(response)
            created.append(
                {
                    "case_key": candidate.get("case_key", ""),
                    "title": candidate["title"],
                    "normalized_summary": candidate.get("normalized_summary", ""),
                    "identifier": details.get("identifier", ""),
                    "resource_uri": details.get("resource_uri", ""),
                }
            )
        except Exception as exc:
            failed.append({"title": candidate["title"], "error": str(exc)})

    status = "ok" if not failed else ("partial" if created else "failed")
    return {"status": status, "created": created, "failed": failed}


def _request_with_trace(session: requests.Session, method: str, url: str, debug_log: list[str], **kwargs) -> requests.Response:
    _log(debug_log, f"{method.upper()} {url}")
    resp = session.request(method=method, url=url, allow_redirects=True, **kwargs)
    _log(debug_log, f"  -> status={resp.status_code} final_url={resp.url}")
    return resp


def jazz_login_session(username: str, password: str, verify_ssl: bool, debug_log: list[str] | None) -> requests.Session:
    """
    Logowanie do Jazz przez JTS (SSO) â€“ dziÄ™ki temu sesja dziaĹ‚a dla /ccm i /rs.
    """
    s = requests.Session()
    s.verify = verify_ssl

    # 1) wejĹ›cie na JTS (ustawia redirect/authrequired)
    _request_with_trace(s, "GET", JTS_APP_URL, debug_log, timeout=30)

    # 2) logowanie formularzem JTS
    login_url = urljoin(JTS_APP_URL, "j_security_check")
    payload = {"j_username": username, "j_password": password}
    resp = _request_with_trace(s, "POST", login_url, debug_log, data=payload, timeout=30)

    # 3) sanity check: RS powinno odpowiadaÄ‡ (nie 401)
    rs_reports = urljoin(RS_BASE_URL, "/rs/reports")
    r2 = _request_with_trace(s, "GET", rs_reports, debug_log, timeout=30)
    if r2.status_code == 401:
        raise RuntimeError("RS zwraca 401 po logowaniu do JTS. SprawdĹş, czy masz dostÄ™p do /rs/reports w przeglÄ…darce po zalogowaniu.")
    return s



def discover_workitem_query_bases(session: requests.Session, debug_log: list[str] | None) -> list[str]:
    headers = {"Accept": "application/rdf+xml", "OSLC-Core-Version": "2.0"}
    r = _request_with_trace(session, "GET", OSLC_CATALOG_URL, debug_log, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"Nie mogÄ™ pobraÄ‡ OSLC catalog: status={r.status_code}")

    xml_text = r.text or ""
    root = ET.fromstring(xml_text)

    # Z katalogu wyciÄ…gamy bezpoĹ›rednio ServiceProvider URL-e:
    # <oslc:ServiceProvider rdf:about=".../workitems/services.xml">
    sp_urls = []
    for el in root.findall(".//{http://open-services.net/ns/core#}ServiceProvider"):
        url = el.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about")
        if url and "/ccm/oslc/contexts/" in url and "/workitems/" in url and url.endswith("services.xml"):
            sp_urls.append(url)

    # fallback: czasem ServiceProvider jest pod oslc:serviceProvider + rdf:resource
    for el in root.findall(".//{http://open-services.net/ns/core#}serviceProvider"):
        url = el.attrib.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource")
        if url and "/ccm/oslc/contexts/" in url and "/workitems/" in url and url.endswith("services.xml"):
            sp_urls.append(url)

    sp_urls = list(dict.fromkeys(sp_urls))  # unique, zachowaj kolejnoĹ›Ä‡
    _log(debug_log, f"ServiceProviders znalezione: {len(sp_urls)}")

    # Zamiast pobieraÄ‡ services.xml, budujemy queryBase przez uciÄ™cie koĹ„cĂłwki:
    # .../workitems/services.xml  ->  .../workitems
    query_bases = []
    for sp in sp_urls:
        qb = sp[:-len("/services.xml")]  # ucina dokĹ‚adnie tÄ™ koĹ„cĂłwkÄ™
        query_bases.append(qb)

    uniq = list(dict.fromkeys(query_bases))
    _log(debug_log, f"QueryBase (z URL) znalezione: {len(uniq)}")

    # opcjonalnie pokaĹĽ pierwsze kilka
    if debug_log is not None:
        for u in uniq[:10]:
            _log(debug_log, f"  QB: {u}")
        if len(uniq) > 10:
            _log(debug_log, f"  ... +{len(uniq)-10} wiÄ™cej")

    sp_urls = list(dict.fromkeys(sp_urls))
    _log(debug_log, f"ServiceProviders znalezione: {len(sp_urls)}")
    return sp_urls

def get_query_base_from_services_xml(session: requests.Session, services_xml_url: str, debug_log=None) -> str | None:
    headers = {"Accept": "application/rdf+xml", "OSLC-Core-Version": "2.0"}
    r = session.get(services_xml_url, headers=headers, timeout=60, allow_redirects=True)
    if debug_log is not None:
        _log(debug_log, f"  services.xml status={r.status_code} url={r.url}")
    if r.status_code != 200:
        return None

    ns = {
        "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
        "oslc": "http://open-services.net/ns/core#",
        "dcterms": "http://purl.org/dc/terms/",
    }

    root = ET.fromstring(r.text or "")

    caps = []
    for qc in root.findall(".//oslc:QueryCapability", ns):
        qb_el = qc.find("./oslc:queryBase", ns) or qc.find(".//oslc:queryBase", ns)
        if qb_el is None:
            continue
        qb_url = qb_el.attrib.get(f"{{{ns['rdf']}}}resource")
        if not qb_url:
            continue

        # resourceType moĹĽe byÄ‡ jeden lub wiele
        rtypes = []
        for rt in qc.findall("./oslc:resourceType", ns) + qc.findall(".//oslc:resourceType", ns):
            u = rt.attrib.get(f"{{{ns['rdf']}}}resource")
            if u:
                rtypes.append(u)

        title_el = qc.find(".//dcterms:title", ns)
        title = (title_el.text or "").strip() if title_el is not None else ""

        shape_el = qc.find(".//oslc:resourceShape", ns)
        shape_url = shape_el.attrib.get(f"{{{ns['rdf']}}}resource") if shape_el is not None else None

        caps.append({
            "query_base": qb_url,
            "resource_types": rtypes,
            "title": title,
            "shape": shape_url,
        })

    if debug_log is not None:
        _log(debug_log, f"  QueryCapability found: {len(caps)}")
        for c in caps[:8]:
            _log(debug_log, f"    cap qb={c['query_base']} title={c['title']} rtypes={c['resource_types'][:2]}")

    if not caps:
        return None

    # 1) Preferuj capability dla ChangeRequest (OSLC CM)
    preferred = []
    for c in caps:
        if any("open-services.net/ns/cm#changerequest" in rt.lower() for rt in c["resource_types"]):
            preferred.append(c)

    candidates = preferred or caps

    # 2) JeĹ›li kilka kandydatĂłw â€“ test: czy przyjmuje oslc.where (bez zgadywania URL)
    #    UĹĽywamy prostego where po dcterms:created (tak jak chcesz).
    oslc_prefix = (
        "dcterms=<http://purl.org/dc/terms/>,"
        "xsd=<http://www.w3.org/2001/XMLSchema#>"
    )
    where = 'dcterms:created>="2025-01-01T00:00:00Z"^^xsd:dateTime'

    for c in candidates:
        qb = c["query_base"]
        test_url = qb + "?" + urlencode({
            "oslc.prefix": oslc_prefix,
            "oslc.where": where,
            "oslc.pageSize": "1",
        })
        resp = session.get(test_url, headers={"Accept": "application/json", "OSLC-Core-Version": "2.0"},
                           timeout=60, allow_redirects=True)

        if debug_log is not None:
            _log(debug_log, f"    test where qb={qb} -> {resp.status_code}")

        if resp.status_code == 200:
            if debug_log is not None:
                _log(debug_log, f"  âś… wybrano queryBase (dziaĹ‚a z where): {qb}")
            return qb

    # 3) JeĹ›li ĹĽaden nie dziaĹ‚a z where â€“ zwrĂłÄ‡ taki, ktĂłry chociaĹĽ listuje (probe 200)
    for c in candidates:
        qb = c["query_base"]
        probe = session.get(qb + "?" + urlencode({"oslc.pageSize": "1"}),
                            headers={"Accept": "application/json", "OSLC-Core-Version": "2.0"},
                            timeout=60, allow_redirects=True)
        if debug_log is not None:
            _log(debug_log, f"    probe qb={qb} -> {probe.status_code}")
        if probe.status_code == 200:
            if debug_log is not None:
                _log(debug_log, f"  âš ď¸Ź gdzieĹ› jest problem z where, ale listing dziaĹ‚a. biorÄ™ qb={qb}")
            return qb

    return None

CREATED_AFTER = datetime(2025, 1, 1, tzinfo=timezone.utc)


def fetch_workitems_for_one_querybase(
    session: requests.Session,
    oslc_query_url: str,
    page_size: int,
    max_pages: int,
    debug_log=None,
    max_items_scan_per_project: int = 0,   # 0 = bez limitu
) -> list[dict]:
    """
    Pobiera WI z jednego queryBase BEZ oslc.where.
    Uwaga: u Was oslc.where daje 400, wiÄ™c:
      - robimy listing
      - filtrujemy po stronie Pythona
      - dla kandydatĂłw (data>2025-01-01) dociÄ…gamy RDF szczegĂłĹ‚y WI, ĹĽeby dostaÄ‡ ownedBy i workItemType.
    """

    headers = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}

    # oslc.select prĂłbujemy, ale jeĹ›li serwer nie odda czÄ™Ĺ›ci pĂłl, dalej jedziemy (to NIE jest krytyczne)
    oslc_select = ",".join([
        "dcterms:identifier",
        "dcterms:title",
        "dcterms:created",
        "dcterms:creator",
        "rtc_cm:ownedBy",
        "rtc_cm:workItemType",
        "rdf:about",
        "rdf:type",
    ])

    def make_url(with_select: bool) -> str:
        p = {"oslc.pageSize": str(page_size)}
        if with_select:
            p["oslc.select"] = oslc_select
        return oslc_query_url + "?" + urlencode(p)

    use_select = True
    next_url = make_url(use_select)

    kept: list[dict] = []
    scanned = 0

    # statystyki â€“ ĹĽeby nie byĹ‚o â€ž0 wynikĂłwâ€ť bez wyjaĹ›nienia
    stats = {
        "pages": 0,
        "listed": 0,
        "old_date": 0,
        "no_created": 0,
        "candidates_after_date": 0,
        "no_detail": 0,
        "type_missing": 0,
        "type_mismatch": 0,
        "people_mismatch": 0,
        "kept": 0,
    }

    for page in range(max_pages):
        r = session.get(next_url, headers=headers, allow_redirects=True, timeout=60)
        ct = r.headers.get("Content-Type", "")

        if debug_log is not None and page == 0:
            _log(debug_log, f"  WI(listing) status={r.status_code} ct={ct}")
            _log(debug_log, f"  WI(listing) url={r.url}")

        # jeĹ›li z oslc.select dostaniemy 400 â€“ sprĂłbuj jeszcze raz BEZ select
        if r.status_code == 400 and use_select:
            if debug_log is not None:
                _log(debug_log, "  !! 400 przy oslc.select -> prĂłbujÄ™ BEZ oslc.select")
                _log(debug_log, f"  !! body: {(r.text or '')[:500]}")
            use_select = False
            next_url = make_url(use_select)
            r = session.get(next_url, headers=headers, allow_redirects=True, timeout=60)
            ct = r.headers.get("Content-Type", "")

            if debug_log is not None and page == 0:
                _log(debug_log, f"  WI(listing, no select) status={r.status_code} ct={ct}")
                _log(debug_log, f"  WI(listing, no select) url={r.url}")

        r.raise_for_status()
        data = r.json()

        results = data.get("oslc:results") or data.get("results") or []
        if not results:
            break

        stats["pages"] += 1
        stats["listed"] += len(results)

        # diagnostyka: pokaĹĽ surowe pierwsze 2 WI na pierwszej stronie
        if debug_log is not None and page == 0:
            _log(debug_log, f"  liczba rekordĂłw na stronie 0: {len(results)}")
            for i in range(min(2, len(results))):
                try:
                    _log(debug_log, f"  RAW WI[{i}]: {json.dumps(results[i], ensure_ascii=False)[:800]}")
                except Exception:
                    _log(debug_log, f"  RAW WI[{i}]: {str(results[i])[:800]}")

        for wi in results:
            scanned += 1
            if max_items_scan_per_project > 0 and scanned > max_items_scan_per_project:
                if debug_log is not None:
                    _log(debug_log, f"  STOP: scanned>{max_items_scan_per_project} w tym projekcie")
                break

            wi_id = (
                wi.get("dcterms:identifier")
                or wi.get("dc:identifier")
                or wi.get("id")
                or ""
            )
            summary = (
                wi.get("dcterms:title")
                or wi.get("dc:title")
                or wi.get("title")
                or ""
            )
            created_raw = (
                wi.get("dcterms:created")
                or wi.get("dc:created")
                or wi.get("created")
                or ""
            )
            created_dt = _parse_created_date(created_raw)
            if not created_dt:
                stats["no_created"] += 1
                continue
            if created_dt <= CREATED_AFTER:
                stats["old_date"] += 1
                continue

            stats["candidates_after_date"] += 1

            # creator z listingu (URI), potem mapujemy do peĹ‚nego imienia
            creator_val = wi.get("dcterms:creator") or wi.get("dc:creator") or wi.get("creator")
            creator_uri = ""
            if isinstance(creator_val, dict):
                creator_uri = creator_val.get("rdf:resource") or ""
            elif isinstance(creator_val, str):
                creator_uri = creator_val
            creator_name = resolve_user_fullname(session, creator_uri, debug_log=debug_log) if creator_uri else _safe_display_name(creator_val)

            # dociÄ…gamy szczegĂłĹ‚y WI (owner + workItemType)
            about_url = wi.get("rdf:about") or wi.get("about") or ""
            detail = fetch_workitem_detail(session, about_url, debug_log=debug_log) if about_url else {}
            if not detail:
                stats["no_detail"] += 1
                continue

            owner_uri = detail.get("ownedBy", "")
            owner_name = resolve_user_fullname(session, owner_uri, debug_log=debug_log) if owner_uri else ""

            # type
            type_uri = detail.get("workItemType", "")
            type_name = detail.get("workItemTypeName", "")
            if not type_uri and not type_name:
                # fallback: czasem typ jest zaszyty w URI â€ž.../workitemtypes/<name>â€ť
                stats["type_missing"] += 1
                continue

            if type_uri:
                type_name = _safe_display_name(type_uri)

            t = (type_name or "").lower()
            if ("defect" not in t) and ("finding" not in t):
                stats["type_mismatch"] += 1
                continue

            owner_hash = get_hash_for_person(owner_name)
            creator_hash = get_hash_for_person(creator_name)

            # JeĹĽeli nie umiemy zmapowaÄ‡ ani ownera ani creatora do rekordu z bazy SQLite,
            # pomijamy WI w tej Ĺ›cieĹĽce raportowej.
            if not owner_hash and not creator_hash:
                stats["people_mismatch"] += 1
                continue

            kept.append({
                "id": str(wi_id),
                "summary": summary,
                "owned_by": owner_name,
                "created_by": creator_name,
                "owner_hash": owner_hash,
                "creator_hash": creator_hash,
                "created": created_dt.isoformat(),
                "type": type_name,
                "about": about_url,
            })
            stats["kept"] += 1

        # paginacja â€“ oslc:next
        nxt = data.get("oslc:next") or data.get("next")
        if isinstance(nxt, dict):
            next_url = nxt.get("rdf:resource") or nxt.get("href") or ""
        elif isinstance(nxt, str):
            next_url = nxt
        else:
            next_url = ""

        if not next_url:
            break

    if debug_log is not None:
        _log(debug_log, f"  STATS: pages={stats['pages']} listed={stats['listed']} "
                        f"old_date={stats['old_date']} candidates_after_date={stats['candidates_after_date']} "
                        f"no_detail={stats['no_detail']} type_missing={stats['type_missing']} "
                        f"type_mismatch={stats['type_mismatch']} people_mismatch={stats['people_mismatch']} "
                        f"KEPT={stats['kept']}")

        # jeĹ›li 0 wynikĂłw, a wszystko byĹ‚o odfiltrowane po dacie â€“ powiedz to wprost
        if stats["kept"] == 0 and stats["candidates_after_date"] == 0:
            _log(debug_log, "  INFO: Ten projekt ma tylko stare WI (<= 2025-01-01). To normalne, ĹĽe wynik=0.")

    return kept


def fetch_workitems_all_projects(session: requests.Session, page_size: int, max_pages: int, debug_log: list[str] | None) -> list[dict]:
    service_providers = discover_workitem_query_bases(session, debug_log)
    if not service_providers:
        raise RuntimeError("Nie znaleziono ServiceProviderĂłw w katalogu.")

    all_items = []
    seen = set()

    for i, sp in enumerate(service_providers, start=1):
        _log(debug_log, f"[{i}/{len(service_providers)}] ServiceProvider: {sp}")

        qb = get_query_base_from_services_xml(session, sp, debug_log)
        if not qb:
            continue
 
        items = fetch_workitems_for_one_querybase(session, qb, page_size, max_pages, debug_log=debug_log)
        for it in items:
            wi_id = it.get("id")
            if wi_id and wi_id not in seen:
                seen.add(wi_id)
                all_items.append(it)

    all_items.sort(key=lambda x: x.get("created", ""), reverse=True)
    return all_items


# =========================
# Report Builder /rs/partial â†’ parsowanie tabeli HTML do JSON
# =========================

def _norm_header(h: str) -> str:
    h = (h or "").strip().lower()
    h = re.sub(r"\s+", " ", h)
    return h

def _pick_col(row: dict, candidates: list[str]) -> str:
    # candidates: list of normalized header keywords
    for k, v in row.items():
        nk = _norm_header(k)
        for c in candidates:
            if c in nk:
                return (v or "").strip()
    return ""


def _find_header_index(headers: list[str], candidates: list[str]) -> int:
    normalized_headers = [_norm_header(header) for header in headers]

    for idx, header in enumerate(normalized_headers):
        if header in candidates:
            return idx

    for idx, header in enumerate(normalized_headers):
        for candidate in candidates:
            if candidate in header:
                return idx
    return -1


def _cell_text(cells: list, idx: int) -> str:
    if idx < 0 or idx >= len(cells):
        return ""
    return cells[idx].get_text(" ", strip=True)


def _cell_link(cells: list, idx: int, base_url: str) -> str:
    if idx < 0 or idx >= len(cells):
        return ""
    anchor = cells[idx].find("a")
    if not anchor or not anchor.get("href"):
        return ""
    href = anchor.get("href").strip()
    if href.startswith("/"):
        return base_url.rstrip("/") + href
    if href.startswith(".."):
        return base_url.rstrip("/") + "/" + href.lstrip("./")
    return href


def _direct_row_cells(row, names: list[str]) -> list:
    if row is None:
        return []
    return row.find_all(names, recursive=False)


def _extract_table_headers(table) -> list[str]:
    thead = table.find("thead")
    if thead is not None:
        header_row = thead.find("tr")
        header_cells = _direct_row_cells(header_row, ["th", "td"])
        if header_cells:
            return [cell.get_text(" ", strip=True) for cell in header_cells]

    first_row = None
    tbody = table.find("tbody")
    if tbody is not None:
        first_row = tbody.find("tr", recursive=False)
    if first_row is None:
        first_row = table.find("tr")
    return [cell.get_text(" ", strip=True) for cell in _direct_row_cells(first_row, ["th", "td"])]


def _normalized_headers(headers: list[str]) -> list[str]:
    return [_norm_header(header) for header in headers]


def _headers_match_expected_order(headers: list[str]) -> bool:
    normalized = _normalized_headers(headers)
    if len(normalized) < len(RS_EXPECTED_TABLE_HEADERS):
        return False
    return normalized[: len(RS_EXPECTED_TABLE_HEADERS)] == RS_EXPECTED_TABLE_HEADERS


def _headers_are_collapsed_expected_order(headers: list[str]) -> bool:
    normalized = _normalized_headers(headers)
    if len(normalized) != 1:
        return False
    return normalized[0] == " ".join(RS_EXPECTED_TABLE_HEADERS)


def _preview_table_structure(table) -> dict:
    headers = _extract_table_headers(table)
    tbody = table.find("tbody")
    if tbody is not None:
        first_row = tbody.find("tr", recursive=False)
    else:
        rows = table.find_all("tr", recursive=False)
        first_row = rows[1] if len(rows) > 1 and _direct_row_cells(rows[0], ["th"]) else (rows[0] if rows else None)

    first_cells = _direct_row_cells(first_row, ["td"])
    cell_texts = [_cell_text(first_cells, idx) for idx in range(min(len(first_cells), 6))]

    return {
        "headers": headers[:8],
        "normalized_headers": _normalized_headers(headers)[:8],
        "first_row_cell_count": len(first_cells),
        "first_row_cells": cell_texts,
    }


def filter_report_builder_items(items: list[dict]) -> list[dict]:
    filtered = []

    for item in items:
        owner_record = get_person_record(item.get("owner") or "")
        creator_record = get_person_record(item.get("creator") or "")
        if not owner_record and not creator_record:
            continue

        filtered.append({
            "project": item.get("project") or "",
            "type": item.get("type") or "",
            "owner": owner_record.get("name") or "",
            "owner_department": owner_record.get("department") or "",
            "owner_hash": owner_record.get("person_hash") or "",
            "creator": creator_record.get("name") or "",
            "creator_department": creator_record.get("department") or "",
            "creator_hash": creator_record.get("person_hash") or "",
            "work_item_id": item.get("work_item_id") or "",
            "work_item": item.get("work_item") or "",
            "work_item_url": item.get("work_item_url") or "",
        })

    return filtered


def summarize_report_builder_match_stats(items: list[dict]) -> dict:
    stats = {
        "parsed": len(items),
        "owner_matched": 0,
        "creator_matched": 0,
        "either_matched": 0,
        "both_matched": 0,
        "dropped": 0,
        "unknown_owner_samples": [],
        "unknown_creator_samples": [],
    }

    seen_unknown_owners = set()
    seen_unknown_creators = set()

    for item in items:
        owner_name = (item.get("owner") or "").strip()
        creator_name = (item.get("creator") or "").strip()
        owner_matched = bool(get_person_record(owner_name))
        creator_matched = bool(get_person_record(creator_name))

        if owner_matched:
            stats["owner_matched"] += 1
        elif owner_name and owner_name not in seen_unknown_owners and len(stats["unknown_owner_samples"]) < 5:
            seen_unknown_owners.add(owner_name)
            stats["unknown_owner_samples"].append(owner_name)

        if creator_matched:
            stats["creator_matched"] += 1
        elif creator_name and creator_name not in seen_unknown_creators and len(stats["unknown_creator_samples"]) < 5:
            seen_unknown_creators.add(creator_name)
            stats["unknown_creator_samples"].append(creator_name)

        if owner_matched or creator_matched:
            stats["either_matched"] += 1
        else:
            stats["dropped"] += 1

        if owner_matched and creator_matched:
            stats["both_matched"] += 1

    return stats


def convert_report_items_for_view(items: list[dict], person_field: str) -> list[dict]:
    if person_field not in ("owner", "creator"):
        raise ValueError("person_field must be 'owner' or 'creator'")

    department_field = f"{person_field}_department"
    hash_field = f"{person_field}_hash"

    view_items = []
    for item in items:
        person_name = (item.get(person_field) or "").strip()
        person_hash = (item.get(hash_field) or "").strip()
        if not person_name and not person_hash:
            continue

        view_items.append({
            "project": item.get("project") or "",
            "type": item.get("type") or "",
            "owner": person_name,
            "department": item.get(department_field) or "",
            "person_hash": person_hash,
            "work_item_id": item.get("work_item_id") or "",
            "work_item": item.get("work_item") or "",
            "work_item_url": item.get("work_item_url") or "",
        })

    return view_items

def parse_rs_table(html_text: str, base_url: str = RS_BASE_URL):
    """
    Parsuje HTML zwracany przez Report Builder (/rs/.../partial) i zwraca listÄ™ rekordĂłw
    z polami: project, type, owner, creator, work_item_id, work_item, work_item_url.
    Preferuje mapowanie po nagĹ‚Ăłwkach kolumn i ma fallback do pozycji.
    """
    soup = BeautifulSoup(html_text, "html.parser")

    # wybierz tabelÄ™, ktĂłra wyglÄ…da jak raport z danymi WI
    tables = soup.find_all("table")
    target = None
    best = -1
    for t in tables:
        headers = _extract_table_headers(t)
        normalized_headers = _normalized_headers(headers)

        if _headers_match_expected_order(headers) or _headers_are_collapsed_expected_order(headers):
            target = t
            best = 10_000
            break

        score = 0
        if len(headers) >= 6:
            score += 3
        elif len(headers) >= 5:
            score += 1

        for key in ["project", "work item id", "work item", "owner", "creator", "type", "owned by", "created by"]:
            if any(key in h for h in normalized_headers):
                score += 1
        if any("creator" in h for h in normalized_headers):
            score += 2
        if score > best:
            best = score
            target = t

    if target is None:
        return []

    headers = _extract_table_headers(target)

    idx_project = _find_header_index(headers, ["project"])
    idx_work_item_id = _find_header_index(headers, ["work item id"])
    idx_work_item = _find_header_index(headers, ["work item"])
    idx_owner = _find_header_index(headers, ["owner", "owned by"])
    idx_creator = _find_header_index(headers, ["creator", "created by"])
    idx_type = _find_header_index(headers, ["type"])

    tbody = target.find("tbody")
    if tbody is not None:
        row_elements = tbody.find_all("tr", recursive=False)
    else:
        row_elements = target.find_all("tr", recursive=False)
        if row_elements and _direct_row_cells(row_elements[0], ["th"]):
            row_elements = row_elements[1:]

    out = []
    for tr in row_elements:
        cells = _direct_row_cells(tr, ["td"])
        if len(cells) < 5:
            continue

        if (
            (_headers_match_expected_order(headers) or _headers_are_collapsed_expected_order(headers))
            and len(cells) >= 6
        ):
            project = _cell_text(cells, 0)
            wi_type = _cell_text(cells, 1)
            owner = _cell_text(cells, 2)
            creator = _cell_text(cells, 3)
            work_item_id = _cell_text(cells, 4)
            work_item = _cell_text(cells, 5)
            work_item_url = _cell_link(cells, 5, base_url)
        elif len(cells) >= 6:
            project = _cell_text(cells, idx_project if idx_project >= 0 else 0)
            work_item_id = _cell_text(cells, idx_work_item_id if idx_work_item_id >= 0 else 1)
            work_item = _cell_text(cells, idx_work_item if idx_work_item >= 0 else 2)
            owner = _cell_text(cells, idx_owner if idx_owner >= 0 else 3)
            creator = _cell_text(cells, idx_creator if idx_creator >= 0 else 4)
            wi_type = _cell_text(cells, idx_type if idx_type >= 0 else 5)
            work_item_url = _cell_link(cells, idx_work_item if idx_work_item >= 0 else 2, base_url)
        else:
            project = _cell_text(cells, 0)
            wi_type = _cell_text(cells, 1)
            owner = _cell_text(cells, 2)
            creator = ""
            work_item_id = _cell_text(cells, 3)
            work_item = _cell_text(cells, 4)
            work_item_url = _cell_link(cells, 4, base_url)

        out.append({
            "project": project,
            "type": wi_type,
            "owner": owner,
            "creator": creator,
            "work_item_id": work_item_id,
            "work_item": work_item,
            "work_item_url": work_item_url,
        })

    return out

def fetch_workitems_from_report_builder(session: requests.Session, report_id: str, page_size: int, max_pages: int, debug_log=None):
    # 1) wejĹ›cie na widget â€“ waĹĽne, bo referer z przeglÄ…darki wskazuje wĹ‚aĹ›nie na to
    widget_url = RS_WIDGET_URL_TMPL.format(rid=report_id)
    _log(debug_log, f"RB widget: GET {widget_url}")
    r0 = session.get(widget_url, timeout=60, allow_redirects=True)
    _log(debug_log, f"  -> status={r0.status_code} final_url={r0.url}")

    all_items = []
    for page in range(max_pages):
        partial_url = RS_PARTIAL_URL_TMPL.format(rid=report_id, page=page, rows=page_size)
        _log(debug_log, f"RB partial: GET {partial_url}")
        r = session.get(partial_url, timeout=120, allow_redirects=True, headers={"Referer": widget_url, "X-Requested-With": "XMLHttpRequest"})
        _log(debug_log, f"  -> status={r.status_code} ct={r.headers.get('Content-Type','')}")
        if r.status_code >= 500:
            # serwerowa awaria dla konkretnego requestu â†’ przerywamy (ĹĽeby nie blokowaÄ‡ caĹ‚ej aplikacji)
            _log(debug_log, "  âš ď¸Ź RB zwrĂłciĹ‚ 5xx â€“ przerywam paginacjÄ™.")
            break
        r.raise_for_status()

        html = r.content.decode('utf-8', errors='replace')
        if page == 0 and debug_log is not None:
            soup = BeautifulSoup(html, "html.parser")
            tables = soup.find_all("table")
            _log(debug_log, f"  html tables found={len(tables)}")
            for idx, table in enumerate(tables[:5]):
                preview = _preview_table_structure(table)
                _log(debug_log, f"  table[{idx}] preview={preview}")
        parsed_items = parse_rs_table(html, base_url=RS_BASE_URL)
        match_stats = summarize_report_builder_match_stats(parsed_items)
        items = filter_report_builder_items(parsed_items)
        if parsed_items:
            _log(debug_log, f"  sample parsed item keys={list(parsed_items[0].keys())}")
            _log(debug_log, f"  sample parsed item={parsed_items[0]}")
        if items:
            _log(debug_log, f"  sample filtered item={items[0]}")
        _log(
            debug_log,
            "  match stats: "
            f"parsed={match_stats['parsed']} "
            f"owner_matched={match_stats['owner_matched']} "
            f"creator_matched={match_stats['creator_matched']} "
            f"either_matched={match_stats['either_matched']} "
            f"both_matched={match_stats['both_matched']} "
            f"dropped={match_stats['dropped']}",
        )
        if match_stats["unknown_owner_samples"]:
            _log(debug_log, f"  unknown owner samples={match_stats['unknown_owner_samples']}")
        if match_stats["unknown_creator_samples"]:
            _log(debug_log, f"  unknown creator samples={match_stats['unknown_creator_samples']}")
        _log(debug_log, f"  parsed rows={len(parsed_items)} filtered rows={len(items)}")
        if not parsed_items:
            break

        all_items.extend(items)

        # jeĹ›li mniej niĹĽ page_size, to koniec
        if len(parsed_items) < page_size:
            break

    return all_items

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/session", methods=["GET"])
def api_session():
    return jsonify({"ok": True, "user": build_current_user_payload()})


@app.route("/api/session/login", methods=["POST"])
def api_session_login():
    payload = request.get_json(silent=True) or {}
    username = payload.get("username")
    password = payload.get("password") or ""
    verify_ssl = parse_bool(payload.get("verify_ssl"), default=DEFAULT_VERIFY_SSL)
    debug_log = build_debug_log(payload)

    try:
        auth_state = authenticate_request_session(username, password, verify_ssl, debug_log)
        return jsonify(attach_debug_log({"ok": True, "user": build_current_user_payload(auth_state)}, debug_log))
    except AuthenticationError as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 401
    except Exception as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 401


@app.route("/api/session/logout", methods=["POST"])
def api_session_logout():
    clear_authenticated_session()
    return jsonify({"ok": True})


@app.route("/api/workitems", methods=["POST"])
def api_workitems():
    payload = request.get_json(silent=True) or {}

    debug_log = build_debug_log(payload)
    auth_state = None

    try:
        page_size = int(payload.get("page_size") or PAGE_SIZE_DEFAULT)
        max_pages = int(payload.get("max_pages") or MAX_PAGES_DEFAULT)
    except ValueError:
        return jsonify({"ok": False, "error": "page_size i max_pages muszÄ… byÄ‡ liczbami caĹ‚kowitymi.", "items": []}), 400

        return jsonify({"ok": False, "error": "Podaj login i hasĹ‚o.", "items": [], "debug_log": debug_log}), 400

    page_size = max(10, min(page_size, 1000))
    max_pages = max(1, min(max_pages, 200))

    try:
        auth_state = ensure_authenticated_session_from_payload(payload, debug_log=debug_log, default_verify_ssl=DEFAULT_VERIFY_SSL)
        _log(debug_log, f"CCM_APP_URL={CCM_APP_URL}")
        _log(debug_log, f"OSLC_CATALOG_URL={OSLC_CATALOG_URL}")

        session = build_jazz_requests_session(auth_state, debug_log)
        report_id = (payload.get("report_id") or RS_REPORT_ID_COMBINED).strip()
        items = fetch_workitems_from_report_builder(session, report_id=report_id, page_size=page_size, max_pages=max_pages, debug_log=debug_log)
        persist_authenticated_session_cookies(auth_state, session)

        view_items = convert_report_items_for_view(items, "owner")
        _log(debug_log, f"API default owner view: merged_items={len(items)} returned_items={len(view_items)}")
        return jsonify(attach_debug_log({"ok": True, "items": view_items}, debug_log))
    except AuthenticationError as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e), "items": []}, debug_log)), 401
    except Exception as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e), "items": []}, debug_log)), 500

@app.route("/api/workitems_owner", methods=["POST"])
def api_workitems_owner():
    payload = request.get_json(silent=True) or {}
    debug_log = build_debug_log(payload)

    try:
        auth_state = ensure_authenticated_session_from_payload(payload, debug_log=debug_log, default_verify_ssl=DEFAULT_VERIFY_SSL)
        session = build_jazz_requests_session(auth_state, debug_log)

        items = fetch_workitems_from_report_builder(
            session,
            report_id=RS_REPORT_ID_COMBINED,
            page_size=200,
            max_pages=50,
            debug_log=debug_log
        )
        persist_authenticated_session_cookies(auth_state, session)

        view_items = convert_report_items_for_view(items, "owner")
        _log(debug_log, f"API owner view: merged_items={len(items)} returned_items={len(view_items)}")
        return jsonify(attach_debug_log({"ok": True, "items": view_items}, debug_log))
    except AuthenticationError as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 401
    except Exception as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 500
    
@app.route("/api/workitems_creator", methods=["POST"])
def api_workitems_creator():
    payload = request.get_json(silent=True) or {}
    debug_log = build_debug_log(payload)

    try:
        auth_state = ensure_authenticated_session_from_payload(payload, debug_log=debug_log, default_verify_ssl=DEFAULT_VERIFY_SSL)
        session = build_jazz_requests_session(auth_state, debug_log)

        items = fetch_workitems_from_report_builder(
            session,
            report_id=RS_REPORT_ID_COMBINED,
            page_size=200,
            max_pages=50,
            debug_log=debug_log
        )
        persist_authenticated_session_cookies(auth_state, session)

        view_items = convert_report_items_for_view(items, "creator")
        _log(debug_log, f"API creator view: merged_items={len(items)} returned_items={len(view_items)}")
        return jsonify(attach_debug_log({"ok": True, "items": view_items}, debug_log))
    except AuthenticationError as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 401
    except Exception as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 500


@app.route("/api/analysis", methods=["POST"])
def api_analysis():
    payload = request.get_json(silent=True) or {}
    debug_log = build_debug_log(payload)

    try:
        auth_state = ensure_authenticated_session_from_payload(payload, debug_log=debug_log, default_verify_ssl=DEFAULT_VERIFY_SSL)
        require_analysis_access(auth_state)
    except AuthenticationError as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 401
    except PermissionError as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 403

    try:
        session = build_jazz_requests_session(auth_state, debug_log)

        merged_items = fetch_workitems_from_report_builder(
            session,
            report_id=RS_REPORT_ID_COMBINED,
            page_size=200,
            max_pages=50,
            debug_log=debug_log
        )
        persist_authenticated_session_cookies(auth_state, session)

        analysis = create_analysis_result(merged_items)

        return jsonify(
            attach_debug_log(
                {
                    "ok": True,
                    "analysis": analysis,
                    "items": merged_items,
                },
                debug_log,
            )
        )

    except Exception as e:
        return jsonify(attach_debug_log({"ok": False, "error": str(e)}, debug_log)), 500


@app.route("/api/analysis/export-pdf", methods=["POST"])
def api_analysis_export_pdf():
    payload = request.get_json(silent=True) or {}
    try:
        auth_state = get_authenticated_session(required=True)
        require_analysis_access(auth_state)
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403

    items = payload.get("items")
    department = payload.get("department") or ""

    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Przekaz liste rekordow do eksportu."}), 400

    if not all(isinstance(item, dict) for item in items):
        return jsonify({"ok": False, "error": "Nieprawidlowy format rekordow analizy."}), 400

    saved_at = datetime.now()
    filename = _build_analysis_pdf_filename(department, saved_at=saved_at)
    pdf_bytes = _build_analysis_pdf(items, department=department, saved_at=saved_at)

    response = Response(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Length"] = str(len(pdf_bytes))
    return response


@app.route("/api/analysis/export-lessons-learned-pdf", methods=["POST"])
def api_analysis_export_lessons_learned_pdf():
    payload = request.get_json(silent=True) or {}
    try:
        auth_state = get_authenticated_session(required=True)
        username = require_analysis_access(auth_state)
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403

    selected_cases = payload.get("cases")
    department = payload.get("department") or ""

    if not isinstance(selected_cases, list):
        return jsonify({"ok": False, "error": "Przekaz liste zaznaczonych przypadkow."}), 400

    if not all(isinstance(case, dict) for case in selected_cases):
        return jsonify({"ok": False, "error": "Nieprawidlowy format przypadkow Lessons Learned."}), 400

    try:
        refresh_people_directory()
        normalized_cases = [normalize_lessons_learned_case(case) for case in selected_cases]
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    saved_at = datetime.now()
    filename = _build_lessons_learned_pdf_filename(department, saved_at=saved_at)
    pdf_bytes = _build_lessons_learned_pdf(normalized_cases, username=username, saved_at=saved_at)

    response = Response(pdf_bytes, mimetype="application/pdf")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Length"] = str(len(pdf_bytes))
    return response


@app.route("/api/analysis/lessons-learned-generated", methods=["POST"])
def api_analysis_lessons_learned_generated():
    payload = request.get_json(silent=True) or {}
    try:
        auth_state = get_authenticated_session(required=True)
        require_analysis_access(auth_state)
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403

    case_keys = payload.get("case_keys")
    if case_keys is not None:
        if not isinstance(case_keys, list) or not all(isinstance(case_key, str) for case_key in case_keys):
            return jsonify({"ok": False, "error": "Nieprawidlowy format kluczy przypadkow Lessons Learned."}), 400

    try:
        cases = list_saved_lessons_learned_candidates(case_keys=case_keys)
        return jsonify({"ok": True, "cases": cases})
    except Exception as e:
        if isinstance(e, AuthenticationError):
            return jsonify({"ok": False, "error": str(e)}), 401
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analysis/create-lessons-learned-candidates", methods=["POST"])
def api_analysis_create_lessons_learned_candidates():
    payload = request.get_json(silent=True) or {}
    try:
        auth_state = get_authenticated_session(required=True)
        username = require_analysis_access(auth_state)
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403

    selected_cases = payload.get("cases")

    if not isinstance(selected_cases, list):
        return jsonify({"ok": False, "error": "Przekaz liste zaznaczonych przypadkow."}), 400

    if not all(isinstance(case, dict) for case in selected_cases):
        return jsonify({"ok": False, "error": "Nieprawidlowy format przypadkow Lessons Learned."}), 400

    try:
        refresh_people_directory()
        normalized_cases = [normalize_lessons_learned_case(case) for case in selected_cases]
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        session = build_jazz_requests_session(auth_state, None)
        summary = create_lessons_learned_work_items(session, normalized_cases, debug_log=None)
        persist_authenticated_session_cookies(auth_state, session)
        created = summary.get("created") or []
        failed = summary.get("failed") or []
        saved_cases: list[dict] = []
        save_error = ""
        try:
            saved_cases = save_lessons_learned_candidates(normalized_cases, created)
        except Exception as save_exc:
            save_error = str(save_exc)
        app.logger.info(
            "lessons_learned created by %s: created=%s failed=%s",
            username,
            len(created),
            len(failed),
        )
        return jsonify(
            {
                "ok": True,
                "status": summary.get("status") or "ok",
                "created_count": len(created),
                "failed_count": len(failed),
                "created_ids": [item.get("identifier") for item in created if item.get("identifier")],
                "created": created,
                "failed": failed,
                "saved_cases": saved_cases,
                "save_error": save_error,
            }
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/analysis/save-lessons-learned-exclusions", methods=["POST"])
def api_analysis_save_lessons_learned_exclusions():
    payload = request.get_json(silent=True) or {}
    try:
        auth_state = get_authenticated_session(required=True)
        require_analysis_access(auth_state)
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403

    selected_cases = payload.get("cases")

    if not isinstance(selected_cases, list):
        return jsonify({"ok": False, "error": "Przekaz liste wykluczonych przypadkow."}), 400

    if not all(isinstance(case, dict) for case in selected_cases):
        return jsonify({"ok": False, "error": "Nieprawidlowy format wykluczonych przypadkow Lessons Learned."}), 400

    try:
        saved_cases = save_lessons_learned_exclusions(selected_cases)
        return jsonify({"ok": True, "saved_cases": saved_cases, "saved_count": len(saved_cases)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/people", methods=["GET"])
def api_people():
    try:
        auth_state = get_authenticated_session(required=True)
        require_people_management_access(auth_state)
        return jsonify({"ok": True, "people": list_people_records()})
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e), "people": []}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e), "people": []}), 403
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "people": []}), 500


@app.route("/api/people", methods=["POST"])
def api_people_add():
    payload = request.get_json(silent=True) or {}

    try:
        auth_state = get_authenticated_session(required=True)
        username = require_people_management_access(auth_state)
        person = upsert_person_record(
            name=payload.get("name") or "",
            department=payload.get("department") or "",
            jazz_login=payload.get("jazz_login") or "",
        )
        app.logger.info("people upsert by %s: %s", username, person.get("normalized_name"))
        action = "zaktualizowany" if person["updated"] else "dodany"
        return jsonify(
            {
                "ok": True,
                "person": person,
                "message": f"Pracownik zostaĹ‚ {action} w bazie danych.",
            }
        )
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/people/delete", methods=["POST"])
def api_people_delete():
    payload = request.get_json(silent=True) or {}

    try:
        auth_state = get_authenticated_session(required=True)
        username = require_people_management_access(auth_state)
        person = delete_person_record(
            normalized_name=payload.get("normalized_name") or "",
        )
        app.logger.info("people delete by %s: %s", username, person.get("normalized_name"))
        return jsonify(
            {
                "ok": True,
                "person": person,
                "message": "Pracownik zostaĹ‚ usuniÄ™ty z bazy danych.",
            }
        )
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/people/update", methods=["POST"])
def api_people_update():
    payload = request.get_json(silent=True) or {}

    try:
        auth_state = get_authenticated_session(required=True)
        username = require_people_management_access(auth_state)
        person = update_person_record(
            original_normalized_name=payload.get("original_normalized_name") or "",
            name=payload.get("name") or "",
            department=payload.get("department") or "",
            jazz_login=payload.get("jazz_login") or "",
        )
        app.logger.info("people update by %s: %s", username, person.get("normalized_name"))
        return jsonify(
            {
                "ok": True,
                "person": person,
                "message": "Pracownik zostaĹ‚ zaktualizowany w bazie danych.",
            }
        )
    except AuthenticationError as e:
        return jsonify({"ok": False, "error": str(e)}), 401
    except PermissionError as e:
        return jsonify({"ok": False, "error": str(e)}), 403
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=parse_bool(os.environ.get("FLASK_DEBUG"), default=False))

