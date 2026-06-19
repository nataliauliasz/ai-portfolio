from __future__ import annotations

import re
from typing import Iterable, List

from unidecode import unidecode

# Domyślne prefiksy norm; można je rozszerzyć przez STANDARD_PREFIXES
DEFAULT_PREFIXES = [
    "A",
    "AA",
    "ADL",
    "ADR",
    "AIS",
    "ALD",
    "APQP",
    "APS",
    "AQG",
    "AR",
    "AS/NZS",
    "ASAP",
    "ASME",
    "ASTM",
    "AVC",
    "BN",
    "BS",
    "CCC",
    "CEI",
    "CIE",
    "CFR",
    "CS",
    "CISPR",
    "CMVSS",
    "CNCA",
    "CQI",
    "DBL",
    "DEF",
    "DGUV",
    "DIN",
    "DIS",
    "DM",
    "DTRR",
    "ECE",
    "EEC",
    "EIA",
    "EN",
    "ENV",
    "ETSI",
    "EWG",
    "EU",
    "EG",
    "FM",
    "FMVSS",
    "GB",
    "GBT",
    "GHG",
    "GM",
    "GMW",
    "GS",
    "HDBK",
    "IEC",
    "IEEE",
    "IPC",
    "ISO",
    "ISO/PAS",
    "ISTA",
    "JEDEC",
    "JIS",
    "KMVSS",
    "KS",
    "MBN",
    "MIL",
    "MS",
    "NF",
    "NFPA",
    "OVE",
    "PAS",
    "PN",
    "PPAP",
    "PSA",
    "PV",
    "PF",
    "QCT",
    "RECU",
    "RL",
    "RNES",
    "SAE",
    "SM",
    "SPEC",
    "SQ",
    "STD",
    "TL",
    "TR",
    "TRGS",
    "TS",
    "UL",
    "UN",
    "VDA",
    "VDE",
    "VDI",
    "VW",
    "WSS",
    "ZN",
]

EXCLUDED_PREFIXES = {"IATF"}

PV_DIGIT_ONLY = re.compile(r"^PV\s*\d+(?:[\s\-/:.]\d+)*$")

EU_DIRECTIVE_PATTERN = re.compile(
    r"\b(?P<year>\d{2,4})[\s._/-]+(?P<number>\d{1,3})[\s._/-]+(?P<suffix>E[UC]|EG|EC|EU|EWG)\b",
    re.IGNORECASE,
)
CFR_PATTERN = re.compile(r"\b(?P<title>\d{1,3})\s*CFR\s*(?P<section>\d+(?:[\.\-]\d+)*)\b", re.IGNORECASE)

IDENT_CLEANER = re.compile(r"[^A-Z0-9/\-:]+")


def normalize_norm(s: str) -> str:
    s = unidecode(s.upper())
    s = s.replace("_", " ")
    s = IDENT_CLEANER.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # ujednolicenia często spotykane
    s = s.replace("GB / T", "GB/T").replace("GB/ T", "GB/T").replace("GB /T", "GB/T")
    # brak spacji po prefiksie: MBN51012 -> MBN 51012, DBL8585 -> DBL 8585, ISO26262 -> ISO 26262
    s = re.sub(r"\b([A-Z]{2,6})(\d{3,7})\b", r"\1 \2", s)
    return s


def _sanitize_prefixes(extra_prefixes: Iterable[str] | None) -> List[str]:
    extras: List[str] = []
    if extra_prefixes:
        for p in extra_prefixes:
            if p and str(p).strip():
                extras.append(str(p).strip().upper())
    return extras


def make_patterns(extra_prefixes: Iterable[str] | None = None) -> re.Pattern:
    prefixes = DEFAULT_PREFIXES + _sanitize_prefixes(extra_prefixes)
    # dłuższe prefiksy najpierw (np. ISO/IEC przed ISO)
    prefix_group = "|".join(sorted({re.escape(p) for p in prefixes}, key=len, reverse=True))
    return re.compile(
        rf"""
        \b
        (?:
            {prefix_group}
        )
        [\s/_\-]*
        [A-Z0-9]{{0,4}}            # np. J, R, BS (opcjonalnie)
        [\s/_\-]*                # brak dwukropka przed glownym numerem
        \d{{2,7}}                  # główny numer
        (?:[-/._:]\d+)*             # części/rok np. -1, :2006, /3
        (?:[A-Z]*)?                # sufiks literowy
        (?:\s*(?:TEIL|PART|ED|REV|VERSION)?\s*\d+)?   # doprecyzowanie części/edycji
        \b
        """,
        re.IGNORECASE | re.VERBOSE,
    )


def extract_norms(
    text: str,
    extra_prefixes: Iterable[str] | None = None,
    patterns: re.Pattern | None = None,
) -> List[str]:
    pattern = patterns or make_patterns(extra_prefixes)
    hits: List[str] = []
    seen = set()
    allowed_a_prefixes = {p for p in DEFAULT_PREFIXES + _sanitize_prefixes(extra_prefixes) if p.startswith("A") and len(p) > 1}
    raw_text = text or ""

    def _add(norm: str) -> None:
        if norm and norm not in seen:
            seen.add(norm)
            hits.append(norm)

    for m in pattern.finditer(text or ""):
        norm = normalize_norm(m.group(0))
        if not norm or norm in seen:
            continue
        if any(norm.startswith(f"{prefix} ") or norm == prefix for prefix in EXCLUDED_PREFIXES):
            continue
        if norm.startswith("PV") and not PV_DIGIT_ONLY.match(norm):
            continue
        if norm.startswith("UND"):
            continue
        if norm.startswith("A"):
            if norm.startswith("AA"):
                if not re.match(r"^AA\s*\d{4,}", norm):
                    continue
            elif any(p != "AA" and (norm.startswith(f"{p} ") or norm == p) for p in allowed_a_prefixes):
                pass
            elif re.match(r"^A\s*\d{4,}", norm):
                pass
            else:
                continue
        _add(norm)

    for m in EU_DIRECTIVE_PATTERN.finditer(raw_text):
        year, number, suffix = m.group("year", "number", "suffix")
        suffix = suffix.upper().replace(" ", "")
        _add(f"{year}.{number}.{suffix}")
        _add(f"{year}_{number}_{suffix}")

    for m in CFR_PATTERN.finditer(raw_text):
        title, section = m.group("title", "section")
        section_clean = re.sub(r"\s+", "", section)
        _add(f"{title}.CFR.{section_clean}")
        _add(f"{title}CFR{section_clean}")
    return hits
