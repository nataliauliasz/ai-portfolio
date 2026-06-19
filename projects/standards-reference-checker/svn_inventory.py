from __future__ import annotations

import os
import re
from typing import Dict, Iterable, Tuple
from urllib.parse import quote

from standards_extractor import extract_norms, normalize_norm

DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".odt"}


def _rel_path(full_path: str, strip_prefix: str | None, root: str) -> str:
    full_path = os.path.abspath(full_path)
    if strip_prefix:
        strip = os.path.abspath(strip_prefix)
        if full_path.startswith(strip):
            return full_path[len(strip) :].lstrip("\\/") or os.path.basename(full_path)
    return os.path.relpath(full_path, root)


def _strip_revision(norm: str) -> str:
    """
    Usuwa z normy przyrostki typu rok/rok-miesiac + sufiks jezykowy,
    tak aby identyczne normy w roznych rewizjach trafialy pod jednym kluczem.
    """
    norm = normalize_norm(norm)
    norm = re.sub(r"(?:[-:/\\ ]\d{4}(?:[-/]\d{2})?)?(?:\s+[A-Z]{2,3})?$", "", norm).strip()
    return norm or normalize_norm(norm)


def _version_hint(text: str) -> Tuple[int, int]:
    """
    Ekstrahuje rok/miesiac z sciezki/nazwy pliku (np. 2019-04) - uzywane do wyboru najnowszej wersji.
    Zwraca (rok, miesiac) lub (0, 0) jezeli brak informacji.
    """
    m = re.search(r"(20\d{2})(?:[-_/\.](0[1-9]|1[0-2]))?", text)
    if not m:
        return (0, 0)
    year = int(m.group(1))
    month = int(m.group(2) or 0)
    return (year, month)


def build_svn_inventory(
    root: str,
    web_prefix: str,
    strip_prefix: str | None = None,
    extra_prefixes: Iterable[str] | None = None,
) -> Dict[str, Dict[str, str]]:
    """
    Skanuje lokalny mirror SVN (root) i buduje mape {norma: {path, link}}.
    - bierze tylko DOC_EXTENSIONS
    - normy rozpoznaje na podstawie nazw plikow/sciezek
    - link = web_prefix + url-encoded rel_path po odcieciu strip_prefix
    - jesli wystepuje kilka rewizji tej samej normy, wybiera najnowsza (po roku/miesiacu, a w razie remisu po mtime)
    """
    root_abs = os.path.abspath(root)
    if not os.path.isdir(root_abs):
        raise ValueError(f"SVN mirror not found: {root_abs}")

    inventory: Dict[str, Dict[str, str]] = {}
    web_prefix = web_prefix.rstrip("/")

    def register(key: str, full_path: str, link: str, version_score: Tuple[int, int], mtime: float):
        prev = inventory.get(key)
        if not prev:
            inventory[key] = {"path": full_path, "link": link, "_v": version_score, "_m": mtime}
            return
        prev_v = prev.get("_v", (0, 0))
        prev_m = prev.get("_m", 0.0)
        # prefer higher version; when equal version use EN if available; otherwise newer mtime
        def _is_en(path: str) -> bool:
            name = os.path.basename(path).upper()
            return "_EN" in name or name.endswith("EN.PDF") or name.endswith("EN.DOCX") or name.endswith("EN.DOC")
        if version_score > prev_v:
            inventory[key] = {"path": full_path, "link": link, "_v": version_score, "_m": mtime}
        elif version_score == prev_v:
            prev_en = _is_en(prev.get("path", ""))
            curr_en = _is_en(full_path)
            if (curr_en and not prev_en) or (curr_en == prev_en and mtime > prev_m):
                inventory[key] = {"path": full_path, "link": link, "_v": version_score, "_m": mtime}

    for dirpath, _, filenames in os.walk(root_abs):
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in DOC_EXTENSIONS:
                continue
            full_path = os.path.join(dirpath, fname)
            if not os.path.exists(full_path):
                # pomiń zniknięte pliki/broken links, żeby nie wywalić całego skanowania
                continue
            rel_local = _rel_path(full_path, strip_prefix, root_abs)
            rel_url = rel_local.replace("\\", "/")
            link = f"{web_prefix}/{quote(rel_url)}"
            try:
                mtime = os.path.getmtime(full_path)
            except OSError:
                mtime = 0
            v_score = _version_hint(rel_url)

            # preferuj wykrywanie normy z samej nazwy pliku; sciezka jako fallback (zbyt "bogate" prefiksy potrafia dublowac MBN/MBN)
            hits = extract_norms(fname, extra_prefixes=extra_prefixes) or extract_norms(
                rel_url, extra_prefixes=extra_prefixes
            )
            for norm in hits:
                base = _strip_revision(norm)
                register(normalize_norm(norm), full_path, link, v_score, mtime)
                register(base, full_path, link, v_score, mtime)

    # usun wewnetrzne pola pomocnicze
    for k, v in list(inventory.items()):
        v.pop("_v", None)
        v.pop("_m", None)
    return inventory
