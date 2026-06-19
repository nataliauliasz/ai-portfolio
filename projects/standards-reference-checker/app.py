"""
Standards Checker - FastAPI app

Aplikacja do wykrywania odwolan do norm w plikach PDF/DOC/DOCX i porownania ich z repozytorium SVN.
"""

from __future__ import annotations

import os
import re
import time
import shutil
import sqlite3
import tempfile
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional, Iterable
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from standards_extractor import extract_norms, normalize_norm, make_patterns
from svn_inventory import build_svn_inventory, DOC_EXTENSIONS
from ui import render_form_page, render_results_page, t

from svn_sync import resolve_svn_bin
from svn_webdav import list_remote_svn
import asyncio  # używamy do nieblokującego uruchomienia sync



# .env loader (optional)
try:
    from dotenv import load_dotenv
    load_dotenv()  # wczyta .env z biezacego katalogu
except Exception:
    pass

# PDF
try:
    import fitz  # PyMuPDF
except Exception:
    fitz = None

# DOCX
try:
    from docx import Document as DocxDocument
except Exception:
    DocxDocument = None

# legacy .doc
try:
    import textract
except Exception:
    textract = None

APP_TITLE = "Referenced Standards Checker"
DB_PATH = os.environ.get("STANDARDS_DB_PATH", os.path.join(os.getcwd(), "standards_index.sqlite"))
SVN_ROOT_URL = os.environ.get("SVN_ROOT_URL", "https://svn.example.internal/Standards/")
USE_SVN_MIRROR = os.environ.get("USE_SVN_MIRROR", "0").lower() in ("1", "true", "yes")
SVN_ROOT = os.environ.get("SVN_ROOT") if USE_SVN_MIRROR else None
SVN_WEB_PREFIX = os.environ.get("SVN_WEB_PREFIX", "https://svn.example.internal/!/#Standards/view/head")
SVN_STRIP_PREFIX = os.environ.get("SVN_STRIP_PREFIX")
SVN_USERNAME = os.environ.get("SVN_USERNAME")
SVN_PASSWORD = os.environ.get("SVN_PASSWORD")
SVN_BIN = os.environ.get("SVN_BIN")  # pełna ścieżka do svn.exe (opcjonalnie)
SVN_AUTH_METHOD = os.environ.get("SVN_AUTH_METHOD", "basic").lower()
SVN_AUTH_METHOD = SVN_AUTH_METHOD if SVN_AUTH_METHOD in ("basic", "negotiate") else "basic"
SVN_INSECURE = os.environ.get("SVN_INSECURE", "0").lower() in ("1", "true", "yes")
try:
    SVN_LIST_TIMEOUT = float(os.environ.get("SVN_LIST_TIMEOUT", "15"))
except Exception:
    SVN_LIST_TIMEOUT = 15.0
INDEX_TTL_DAYS = int(os.environ.get("INDEX_TTL_DAYS", "7"))
LAST_PAGES_DEFAULT = 100  # analizujemy zawsze ostatnie 100 stron PDF
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))
STANDARD_PREFIXES = [p.strip() for p in os.environ.get("STANDARD_PREFIXES", "").split(",") if p.strip()]
BASE_DIR = Path(__file__).resolve().parent

# Rozszerzenia, które indeksujemy w SVN (jeśli pliki standardów są w różnych formatach, można rozszerzyć)
INDEXED_EXT = set(DOC_EXTENSIONS)

# Jeden uogólniony wzorzec dla norm (prefiks + numer + opcjonalne części)
norm_key = normalize_norm  # alias dla zgodności z wcześniejszą logiką

# Leniwa pamięć podręczna dla inwentarza lokalnego SVN
svn_inventory_cache: Optional[Dict[str, Dict[str, str]]] = None


def get_svn_inventory() -> Dict[str, Dict[str, str]]:
    """Buduje map?t norm z lokalnego mirrora SVN tylko raz na proces."""
    global svn_inventory_cache
    if svn_inventory_cache is not None:
        return svn_inventory_cache
    if not SVN_ROOT:
        return {}
    try:
        svn_inventory_cache = build_svn_inventory(
            root=SVN_ROOT,
            web_prefix=SVN_WEB_PREFIX,
            strip_prefix=SVN_STRIP_PREFIX,
        )
    except Exception:
        svn_inventory_cache = {}
    return svn_inventory_cache

def _extract_prefix(s: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)(?:/IEC|/ISO)?", s.strip())
    return (m.group(1).upper() if m else "?")


# ====== Baza danych indeksu SVN ======

def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS svn_entries (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL,
            name TEXT NOT NULL,
            ext TEXT NOT NULL,
            std_key TEXT,
            mtime INTEGER
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            k TEXT PRIMARY KEY,
            v TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def db_set_meta(k: str, v: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))
    conn.commit()
    conn.close()


def db_get_meta(k: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (k,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def db_bulk_insert(entries: List[Tuple[str, str, str, Optional[str], int]]):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM svn_entries")
    cur.executemany(
        "INSERT INTO svn_entries(path, name, ext, std_key, mtime) VALUES(?,?,?,?,?)",
        entries,
    )
    conn.commit()
    conn.close()


def db_find_by_key(std_key: str) -> List[Tuple[str, str, str, Optional[str], int]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT path, name, ext, std_key, mtime FROM svn_entries WHERE std_key LIKE ?", (f"%{std_key}%",))
    rows = cur.fetchall()
    conn.close()
    return rows

def db_find_by_pathlike(token: str) -> List[Tuple[str, str, str, Optional[str], int]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    like = f"%{token}%"
    cur.execute(
        "SELECT path, name, ext, std_key, mtime FROM svn_entries WHERE path LIKE ? OR name LIKE ?",
        (like, like),
    )
    rows = cur.fetchall()
    conn.close()
    return rows

# ====== Lokalny mirror SVN ======
LOCAL_INVENTORY: Dict[str, Dict[str, str]] = {}
LOCAL_INVENTORY_BUILT_AT: Optional[datetime] = None


def build_local_inventory() -> Dict[str, Dict[str, str]]:
    global LOCAL_INVENTORY, LOCAL_INVENTORY_BUILT_AT
    if not SVN_ROOT:
        return {}
    inv = build_svn_inventory(
        root=SVN_ROOT,
        web_prefix=SVN_WEB_PREFIX,
        strip_prefix=SVN_STRIP_PREFIX or SVN_ROOT,
        extra_prefixes=STANDARD_PREFIXES,
    )
    LOCAL_INVENTORY = inv
    LOCAL_INVENTORY_BUILT_AT = datetime.now(timezone.utc)
    return inv


def ensure_local_inventory() -> Dict[str, Dict[str, str]]:
    if not SVN_ROOT:
        return {}
    if not LOCAL_INVENTORY_BUILT_AT or (datetime.now(timezone.utc) - LOCAL_INVENTORY_BUILT_AT) > timedelta(days=INDEX_TTL_DAYS):
        return build_local_inventory()
    return LOCAL_INVENTORY

# ====== Indeksacja SVN ======
# Fallback HTTP crawler jeżeli brak svn.exe w PATH
try:
    import requests
    from bs4 import BeautifulSoup
except Exception:
    requests = None
    BeautifulSoup = None

def http_list_recursive(root_url: str, username: Optional[str] = None, password: Optional[str] = None) -> List[str]:
    if not requests or not BeautifulSoup:
        return []
    verify = os.environ.get("REQUESTS_VERIFY", "1") not in ("0","false","False")
    try:
        timeout = float(os.environ.get("HTTP_TIMEOUT", "15"))
    except Exception:
        timeout = 15.0
    seen_dirs = set(); files: List[str] = []; stack = ["/"]
    sess = requests.Session()
    user = username or SVN_USERNAME
    pwd = password or SVN_PASSWORD
    if user and pwd:
        sess.auth = (user, pwd)
    base = root_url.rstrip("/")
    while stack:
        rel = stack.pop()
        if rel in seen_dirs: continue
        seen_dirs.add(rel)
        url = base + rel
        try:
            resp = sess.get(url, timeout=timeout, verify=verify, allow_redirects=True)
            if resp.status_code != 200: continue
        except Exception:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a"):
            href = a.get("href") or ""
            if href in ("../","./","#"): continue
            if href.endswith("/"):
                stack.append(rel + href if rel.endswith("/") else rel + "/" + href)
            else:
                path = (rel + href).lstrip("/") if rel.endswith("/") else (rel + "/" + href).lstrip("/")
                files.append(path)
    return files


def _run_svn_cli_list(root_url: str, username: Optional[str], password: Optional[str]) -> List[str]:
    """Zwraca pełną listę ścieżek w SVN (rekurencyjnie) przez svn.exe."""
    svn_bin = resolve_svn_bin()
    if not svn_bin:
        raise RuntimeError("Brak svn.exe – ustaw SVN_BIN lub dodaj do PATH (wymagane dla pełnego indeksu).")
    user = username or SVN_USERNAME
    pwd = password or SVN_PASSWORD
    cmd = [svn_bin, "list", "-R", root_url]
    if user and pwd:
        cmd = [svn_bin, "list", "-R", "--non-interactive", "--trust-server-cert",
               "--username", user, "--password", pwd, root_url]
    out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, text=True)
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return [l for l in lines if not l.endswith("/")]


def run_svn_list(root_url: str, username: Optional[str] = None, password: Optional[str] = None) -> List[str]:
    """
    Zwraca pełną listę ścieżek w SVN korzystając z PROPFIND (HTTP/S) i porównuje standardy
    na podstawie nazw plików. W razie problemów wraca do wariantu z svn.exe.
    """
    user = username or SVN_USERNAME
    pwd = password or SVN_PASSWORD
    try:
        paths = list_remote_svn(
            root_url,
            recursive=True,
            timeout=SVN_LIST_TIMEOUT,
            verify_ssl=not SVN_INSECURE,
            username=user,
            password=pwd,
            auth_method=SVN_AUTH_METHOD,
        )
        if paths:
            return paths
    except Exception:
        pass
    return _run_svn_cli_list(root_url, user, pwd)

def guess_std_key_from_filename(name: str) -> Optional[str]:
    base, ext = os.path.splitext(name)
    base = base.replace("_", " ")
    hits = extract_norms(base, extra_prefixes=STANDARD_PREFIXES)
    return hits[0] if hits else None

def derive_std_key_from_path(path: str) -> Optional[str]:
    hits = extract_norms(path, extra_prefixes=STANDARD_PREFIXES)
    return hits[0] if hits else None

def _collect_std_dirs_from_paths(paths: List[str], now_ts: int):
    seen = set(); rows = []
    for p in paths:
        parts = re.split(r"[\\/]+", p.strip("/"))
        for i in range(1, len(parts)+1):
            d = "/".join(parts[:i])
            if not d or d in seen: continue
            seen.add(d)
            key = derive_std_key_from_path(d)
            if key:
                name = parts[i-1]
                rows.append((d + "/", name, "<DIR>", key, now_ts))
    return rows

def build_index(root_url: str, username: Optional[str] = None, password: Optional[str] = None) -> Tuple[int, int]:
    paths: List[str] = []
    try:
        paths = run_svn_list(root_url, username=username, password=password)
    except Exception:
        paths = []
    if not paths:
        paths = http_list_recursive(root_url, username=username, password=password)
    if not paths:
        raise RuntimeError("Nie udało się zbudować indeksu: brak svn.exe i pusta odpowiedź HTTP.")

    rows: List[Tuple[str, str, str, Optional[str], int]] = []
    now_ts = int(time.time())

    # pliki
    for p in paths:
        name = os.path.basename(p.rstrip("/"))
        ext = os.path.splitext(name)[1].lower()
        if ext not in INDEXED_EXT:
            continue
        key = guess_std_key_from_filename(name) or derive_std_key_from_path(p)
        rows.append((p, name, ext, key, now_ts))

    # katalogi przypominające normy
    rows += _collect_std_dirs_from_paths(paths, now_ts)

    db_bulk_insert(rows)
    db_set_meta("indexed_at", datetime.now(timezone.utc).isoformat())
    return (len(rows), sum(1 for *_, k, __ in rows if k))


def ensure_index_fresh(username: Optional[str] = None, password: Optional[str] = None) -> Dict[str, str]:
    if SVN_ROOT:
        inv = ensure_local_inventory()
        built = LOCAL_INVENTORY_BUILT_AT.isoformat() if LOCAL_INVENTORY_BUILT_AT else None
        return {"status": "local", "entries": str(len(inv)), "built_at": built}

    db_init()
    indexed_at = db_get_meta("indexed_at")
    if not indexed_at:
        n_all, n_key = build_index(SVN_ROOT_URL, username=username, password=password)
        return {"status": "created", "entries": str(n_all), "with_std_key": str(n_key)}
    try:
        dt = datetime.fromisoformat(indexed_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        dt = datetime.now(timezone.utc) - timedelta(days=999)
    now = datetime.now(timezone.utc)
    if now - dt > timedelta(days=INDEX_TTL_DAYS):
        n_all, n_key = build_index(SVN_ROOT_URL, username=username, password=password)
        return {"status": "refreshed", "entries": str(n_all), "with_std_key": str(n_key)}
    return {"status": "ok", "indexed_at": indexed_at}


def extract_from_pdf(path: str) -> str:
    if not fitz:
        return ""
    text_parts: List[str] = []
    with fitz.open(path) as doc:
        n = doc.page_count
        start = max(0, n - LAST_PAGES_DEFAULT)
        for i in range(start, n):
            page = doc.load_page(i)
            text_parts.append(page.get_text("text"))
    return "\n".join(text_parts)


def extract_from_docx(path: str) -> str:
    if not DocxDocument:
        return ""
    doc = DocxDocument(path)
    paras = [p.text for p in doc.paragraphs if p.text and p.text.strip()]
    if not paras:
        return ""
    return "\n".join(paras)


def extract_from_doc(path: str) -> str:
    # legacy .doc – najlepiej przez textract jeśli dostępny
    if not textract:
        return ""
    try:
        b = textract.process(path)
        return b.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def find_standards(raw_text: str, patterns=None) -> List[Tuple[str, str]]:
    """Zwraca liste (label, norm_key)."""
    hits: List[Tuple[str, str]] = []
    for norm in extract_norms(raw_text or "", extra_prefixes=STANDARD_PREFIXES, patterns=patterns):
        label = _extract_prefix(norm)
        hits.append((label, norm))
    return hits

# ====== Dopasowanie do indeksu SVN ======

def _variant_keys(key: str) -> List[str]:
    variants = {key}
    # zrzuc rok/miesiac + ewentualny sufiks jezykowy, zeby trafic w podstawowy klucz normy
    base = re.sub(r'(?:[:/\- ]\d{4}(?:[-/]\d{2})?)?(?:\s+[A-Z]{2,3})?$', '', key).strip()
    if base:
        variants.add(base)

    def _add_dual_prefix_variants(pref1: str, pref2: str, rest: str) -> None:
        if not rest:
            return
        variants.add(f"{pref1} {rest}")
        variants.add(f"{pref2} {rest}")
        variants.add(f"{pref1} {pref2} {rest}")
        variants.add(f"{pref1}{pref2} {rest}")

    # Rozbij zlozone prefiksy (np. DIN EN 13018 -> DIN 13018, EN 13018, DIN EN 13018)
    parts = base.split()
    if len(parts) >= 3 and all(re.fullmatch(r"[A-Z]{2,6}", p) for p in parts[:2]):
        pref1, pref2 = parts[0], parts[1]
        rest = " ".join(parts[2:])
        _add_dual_prefix_variants(pref1, pref2, rest)
    else:
        m = re.match(r"^([A-Z]{2,6})[-/]+([A-Z]{2,6})\s+(.*)$", base)
        if m:
            pref1, pref2, rest = m.groups()
            _add_dual_prefix_variants(pref1, pref2, rest)

    for k in list(variants):
        variants.add(k.replace(' ', ''))
        variants.add(k.replace(' ', '_'))
    for k in list(variants):
        variants.add(k.replace('-', ''))
    for k in list(variants):
        variants.add(k.replace('.', '_'))
        variants.add(k.replace('_', '.'))
    return [v for v in variants if v]

def lookup_in_index(std_text: str) -> Tuple[bool, List[str]]:
    key = norm_key(std_text)
    if not key:
        return (False, [])

    variants = _variant_keys(key)
    keyword_markers = ("beiblatt", "amendment", "amd", "bbl")
    def _is_eu_or_cfr(text: str) -> bool:
        upper = text.upper()
        if "CFR" in upper:
            return True
        return bool(re.search(r"\b\d{2,4}[._ ]\d{1,3}[._ ](?:EU|EG|EWG|EEC)\b", upper))

    is_eu_or_cfr = _is_eu_or_cfr(key) or any(_is_eu_or_cfr(v) for v in variants)

    def _build_web_link(rel_path: str) -> str:
        rel = quote(rel_path.lstrip("/"))
        if SVN_WEB_PREFIX:
            return SVN_WEB_PREFIX.rstrip("/") + "/" + rel
        return SVN_ROOT_URL.rstrip("/") + "/" + rel

    def _version_hint_from_path(path: str) -> Tuple[int, int]:
        m = re.search(r"(20\d{2})(?:[-_/\.](0[1-9]|1[0-2]))?", path)
        if not m:
            return (0, 0)
        year = int(m.group(1))
        month = int(m.group(2) or 0)
        return (year, month)

    def _is_en(path: str) -> bool:
        upper = path.upper()
        return "_EN" in upper or upper.endswith("EN.PDF") or upper.endswith("EN.DOCX") or upper.endswith("EN.DOC")

    def _best_row(rows: List[Tuple[str, str, str, Optional[str], int]]) -> Optional[Tuple[str, str, str, Optional[str], int]]:
        if not rows:
            return None
        def _score(row):
            p = row[0]
            year, month = _version_hint_from_path(p)
            en = 1 if _is_en(p) else 0
            return (en, year, month, -len(p))
        return max(rows, key=_score)

    def _adjust_links(links: List[str]) -> List[str]:
        adj: List[str] = []
        for link in links:
            lower = link.lower()
            if any(marker in lower for marker in keyword_markers):
                folder = link.rsplit("/", 1)[0]
                if folder and not folder.endswith("/"):
                    folder += "/"
                if folder not in adj:
                    adj.append(folder)
                continue
            if link not in adj:
                adj.append(link)
        return adj

    if SVN_ROOT:
        inv = ensure_local_inventory()
        links: List[str] = []
        for k in variants:
            entry = inv.get(k)
            if entry:
                link = entry.get('link')
                if link and link not in links:
                    links.append(link)
        # fallback: szukaj po fragmencie �>cie��ki (dla dyrektyw EU/CFR zapisanych z kropkami/podkre�leniami)
        if not links and is_eu_or_cfr:
            def _norm_token(s: str) -> str:
                return re.sub(r"[^a-z0-9]+", "", s.lower())
            variant_tokens = [_norm_token(v) for v in variants]
            for entry in inv.values():
                path_token = _norm_token(entry.get("path", ""))
                if any(tok and tok in path_token for tok in variant_tokens):
                    link = entry.get("link")
                    if link and link not in links:
                        links.append(link)
        return (bool(links), _adjust_links(links))

    for k in variants:
        rows = db_find_by_key(k)
        if rows:
            best = _best_row(rows)
            if best:
                link = _build_web_link(best[0])
                return (True, _adjust_links([link]))

    for k in variants:
        rows = db_find_by_pathlike(k)
        if rows:
            best = _best_row(rows)
            if best:
                link = _build_web_link(best[0])
                return (True, _adjust_links([link]))

    return (False, [])

# ====== FastAPI ======

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Buduj zdalny indeks tylko gdy mamy kredencjały w ENV; w innym przypadku czekamy na formularz użytkownika.
    auto_user = os.environ.get("SVN_USERNAME")
    auto_pwd = os.environ.get("SVN_PASSWORD")
    if auto_user or auto_pwd:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: ensure_index_fresh(username=auto_user, password=auto_pwd),
            )
        except Exception as exc:
            print(f"[lifespan] Pominieto budowe indeksu na starcie: {exc}")
    else:
        print("[lifespan] Brak danych logowania – indeks zostanie zbudowany po zalogowaniu uzytkownika.")
    yield


app = FastAPI(title=APP_TITLE, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class AnalyzeResponse(BaseModel):
    filename: str
    references: List[Dict[str, object]]
@app.get("/health")
async def health():
    if SVN_ROOT:
        inv = ensure_local_inventory()
        built = LOCAL_INVENTORY_BUILT_AT.isoformat() if LOCAL_INVENTORY_BUILT_AT else None
        return {"status": "ok", "mode": "local", "entries": len(inv), "built_at": built}
    return {"status": "ok", "indexed": db_get_meta("indexed_at")}

@app.get("/refresh-index")
async def refresh_index(username: Optional[str] = None, password: Optional[str] = None):
    username = username or os.environ.get("SVN_USERNAME")
    password = password or os.environ.get("SVN_PASSWORD")
    if SVN_ROOT:
        inv = build_local_inventory()
        built = LOCAL_INVENTORY_BUILT_AT.isoformat() if LOCAL_INVENTORY_BUILT_AT else None
        return {"status": "local-refreshed", "entries": len(inv), "built_at": built}
    info = build_index(SVN_ROOT_URL, username=username, password=password)
    return {"status": "refreshed", "entries": info[0], "with_std_key": info[1]}

#HTML_FORM = render_form_page(APP_TITLE)

@app.get("/", response_class=HTMLResponse)
async def index(lang: str = "pl"):
    return HTMLResponse(render_form_page(APP_TITLE, lang=lang))


@app.post("/analyze", response_class=HTMLResponse)
async def analyze_html(
    files: List[UploadFile] = File(...),
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
    lang: str = Form("pl"),
):

    results = await analyze_impl(files, username=username, password=password)
    details_blocks = []
    for res in results:
        # szczegoly per norma
        norm_rows = []
        for item in res["references"]:
            links = item.get("links") or []
            link_html = "<br>".join(
                f"<a href='{l}' target='_blank' rel='noopener noreferrer'>{t('on_svn', lang)}</a>" for l in links
            ) if links else t("not_on_svn", lang)
            status_text = t("on_svn", lang) if links else t("not_on_svn", lang)
            status_cell = link_html if links else f"<span class='status-nok'>{status_text}</span>"
            status_flag = "ok" if links else "nok"
            safe_name = item["text"].replace('"', "&quot;")
            norm_rows.append(f"<tr data-status='{status_flag}' data-name=\"{safe_name}\"><td>{item['text']}</td><td>{status_cell}</td></tr>")
        details_blocks.append(
            "<div class='details-card'>"
            f"<h3>{res['filename']}</h3>"
            "<table class='detail-table'>"
            f"<thead><tr><th>{t('standard', lang)}</th><th>{t('status', lang)}</th></tr></thead>"
            "<tbody>"
            + "\n".join(norm_rows) +
            "</tbody></table></div>"
        )
    page = render_results_page(APP_TITLE, "\n".join(details_blocks), len(results), lang=lang)
    return HTMLResponse(page)

@app.post("/api/analyze", response_model=List[AnalyzeResponse])
async def analyze_api(
    files: List[UploadFile] = File(...),
    username: Optional[str] = Form(None),
    password: Optional[str] = Form(None),
):
    results = await analyze_impl(files, username=username, password=password)
    return JSONResponse(results)


async def analyze_impl(
    files: List[UploadFile],
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> List[Dict[str, object]]:
    ensure_index_fresh(username=username, password=password)
    inv = get_svn_inventory() or {}
    inv_prefixes = set()
    for norm in inv:
        m = re.match(r"([A-Z]{1,6})", norm)
        if m:
            inv_prefixes.add(m.group(1))
    patterns = make_patterns(list(inv_prefixes) + STANDARD_PREFIXES)
    out: List[Dict[str, object]] = []
    tmpdir = tempfile.mkdtemp(prefix="stdchk_")
    try:
        for uf in files:
            # ograniczenie rozmiaru
            save_path = os.path.join(tmpdir, uf.filename)
            with open(save_path, "wb") as f:
                content = await uf.read()
                if len(content) > MAX_UPLOAD_MB * 1024 * 1024:
                    raise ValueError(f"Plik {uf.filename} przekracza limit {MAX_UPLOAD_MB} MB")
                f.write(content)
            text = ""
            ext = os.path.splitext(save_path)[1].lower()
            if ext == ".pdf":
                text = extract_from_pdf(save_path)
            elif ext == ".docx":
                text = extract_from_docx(save_path)
            elif ext == ".doc":
                text = extract_from_doc(save_path)
            else:
                text = ""
            refs = find_standards(text, patterns=patterns)
            aggregated: Dict[str, Dict[str, object]] = {}
            for label, hit in refs:
                nk = norm_key(hit)
                if not nk:
                    continue
                found, links = lookup_in_index(hit)
                entry = aggregated.setdefault(
                    nk,
                    {"label": label, "text": nk, "found": False, "links": []},
                )
                entry["found"] = entry["found"] or found
                for l in links:
                    if l not in entry["links"]:
                        entry["links"].append(l)
            out.append({"filename": uf.filename, "references": list(aggregated.values())})
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)




