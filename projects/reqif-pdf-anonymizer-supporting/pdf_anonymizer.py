# pdf_anonymizer.py
import os
import fitz
from PySide6.QtWidgets import QInputDialog, QFileDialog
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
import re
import spacy
import json
from pathlib import Path

# Modelo multilingüe de spaCy para detectar personas
nlp = spacy.load("xx_ent_wiki_sm")

# List of known client names
CLIENTS_PATH = Path(__file__).with_name("clients.json")
FONT_ENV_VAR = "ANONYMIZER_FONT_PATH"
UNICODE_FONT_NAME = "unicode_fallback"
ACCENTED_LOWER = "ąćęłńóśźż"
ACCENTED_UPPER = "ĄĆĘŁŃÓŚŹŻ"

HYPHEN_CODES = (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212)
HYPHEN_CHARS = "-" + "".join(chr(code) for code in HYPHEN_CODES)
# Klasa znaków łączników do użycia w [ ... ]
HYPHEN_CHAR_PATTERN = f"[{re.escape(HYPHEN_CHARS)}]"
SOFT_HYPHEN = chr(0x00AD)
CLIENT_NAME_SPLIT_RE = re.compile(r"[\s-]+")
HARD_HYPHEN_SPLIT_RE = re.compile(r"(\w)-\s*\n+\s*(\w)")
LOWER_TO_UPPER_BOUNDARY = re.compile(
    rf"([a-z{ACCENTED_LOWER}])([A-Z{ACCENTED_UPPER}])"
)
LINE_NOISE_PREFIX = re.compile(
    r"(?m)^\s*(?:[\-\u2022\u2023\u25AA\u25CF•·▪]+|\d{1,3}[.)]|[|/])\s+(?=\w)"
)
HARD_BREAK_RE = re.compile(r"[ \t]*[\r\n]+[ \t]*")
MULTISPACE_RE = re.compile(r"\s{2,}")
CAMEL_CASE_PERSON_RE = re.compile(
    rf"\b[A-Z{ACCENTED_UPPER}][a-z{ACCENTED_LOWER}]+(?:[A-Z{ACCENTED_UPPER}][a-z{ACCENTED_LOWER}]+)+\b"
)


def _iter_font_candidates():
    env_font = os.environ.get(FONT_ENV_VAR)
    if env_font:
        candidate = Path(env_font).expanduser()
        if candidate.exists():
            yield candidate

    win_dir = os.environ.get("WINDIR")
    if win_dir:
        fonts_dir = Path(win_dir) / "Fonts"
        for name in ("arial.ttf", "arialbd.ttf", "calibri.ttf", "segoeui.ttf"):
            candidate = fonts_dir / name
            if candidate.exists():
                yield candidate

    linux_candidates = (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    )
    for candidate in linux_candidates:
        if candidate.exists():
            yield candidate

    mac_candidates = (
        Path("/Library/Fonts/Arial Unicode.ttf"),
        Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    )
    for candidate in mac_candidates:
        if candidate.exists():
            yield candidate


def _load_unicode_font():
    for candidate in _iter_font_candidates():
        return candidate
    return None


UNICODE_FONT_PATH = _load_unicode_font()
def _font_kwargs(font_name: str):
    """
    Build the kwargs required by PyMuPDF for inserting text. When a Unicode
    capable font is available we always use it to avoid glyph fallbacks.
    """
    if UNICODE_FONT_PATH:
        return {
            "fontname": UNICODE_FONT_NAME,
            "fontfile": str(UNICODE_FONT_PATH),
        }
    return {"fontname": font_name}


def _preclean_text_for_ner(text: str) -> str:
    """
    Prepare noisy PDF text for NER. We remove table bullet leftovers, insert
    spaces between glued lower→upper boundaries and collapse hard breaks.
    """
    if not text:
        return ""

    cleaned = LINE_NOISE_PREFIX.sub("", text)
    cleaned = LOWER_TO_UPPER_BOUNDARY.sub(r"\1 \2", cleaned)
    cleaned = HARD_BREAK_RE.sub(" ", cleaned)
    cleaned = MULTISPACE_RE.sub(" ", cleaned)
    return cleaned.strip()


def _build_client_pattern(name, flags=re.IGNORECASE):
    parts = [re.escape(part) for part in CLIENT_NAME_SPLIT_RE.split(name) if part]
    if not parts:
        body = re.escape(name)
        return re.compile(rf"(?<!\w){body}(?!\w)", flags)

    if len(parts) == 1:
        body = parts[0]
        return re.compile(rf"(?<!\w){body}(?![\w{re.escape(HYPHEN_CHARS)}])", flags)

    separator = rf"(?:\s*{HYPHEN_CHAR_PATTERN}\s*|\s+)"
    body = separator.join(parts)
    return re.compile(rf"(?<!\w){body}(?!\w)", flags)


def compile_client_patterns(record):
    """
    Build regex patterns for a client record. The canonical name is always
    included and we honour the case sensitivity flag.
    """
    flags = re.IGNORECASE if record.get("case_insensitive", True) else 0
    patterns = []

    seen = set()
    candidates = [record.get("canonical", "")]
    candidates.extend(record.get("aliases", []))
    for candidate in candidates:
        if not candidate:
            continue
        name = candidate.strip()
        if not name:
            continue
        marker = CLIENT_NAME_SPLIT_RE.sub(" ", name).strip().lower()
        if not marker or marker in seen:
            continue
        seen.add(marker)
        try:
            patterns.append(_build_client_pattern(name, flags=flags))
        except re.error:
            continue

    for raw_pattern in record.get("patterns", []):
        custom = raw_pattern.strip()
        if not custom:
            continue
        try:
            patterns.append(re.compile(custom, flags))
        except re.error:
            continue

    return patterns


class ClientStore:
    def __init__(self, path: Path = CLIENTS_PATH):
        self.path = path
        self.clients = []
        self.regexes = {}
        self.keys = set()
        self.load()

    def _build_from_records(self):
        compiled = {}
        name_keys = set()

        for rec in self.clients:
            if rec.get("status", "approved") != "approved":
                continue

            canonical = (rec.get("canonical") or "").strip()
            if not canonical:
                continue

            patterns = compile_client_patterns(rec)
            if not patterns:
                continue

            compiled[canonical] = patterns

            names_for_keys = [canonical]
            names_for_keys.extend(rec.get("aliases", []))
            for name in names_for_keys:
                if not name:
                    continue
                marker = CLIENT_NAME_SPLIT_RE.sub(" ", name).strip().lower()
                if marker:
                    name_keys.add(marker)

        self.regexes = compiled
        self.keys = name_keys

    def load(self):
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.clients = data.get("clients", [])
        else:
            self.clients = []
        self._build_from_records()

    def save(self):
        data = {"version": 1, "clients": self.clients}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._build_from_records()

    def upsert(self, record):
        found = False
        for i, r in enumerate(self.clients):
            if r.get("id") == record.get("id"):
                self.clients[i] = record
                found = True
                break
        if not found:
            self.clients.append(record)
        self.save()

CLIENT_STORE = ClientStore()


def _normalize_client_text(text):
    replacements = {SOFT_HYPHEN: ""}
    replacements.update({chr(code): "-" for code in HYPHEN_CODES})
    for needle, value in replacements.items():
        text = text.replace(needle, value)
    text = HARD_HYPHEN_SPLIT_RE.sub(r"\1-\2", text)
    return text


def find_clients(text):
    normalized = _normalize_client_text(text)
    found = set()
    for canonical, patterns in CLIENT_STORE.regexes.items():
        if any(p.search(normalized) for p in patterns):
            found.add(canonical)
    return list(found)


def find_persons(text):
    prepared_text = _preclean_text_for_ner(text)
    if not prepared_text:
        return []

    doc = nlp(prepared_text)
    persons = set()

    def strip_punct(tok: str) -> str:
        return tok.strip(".,;:()[]{}<>\"'")

    def is_initial(tok: str) -> bool:
        return bool(re.fullmatch(r"[A-Za-z]\.(?:[A-Za-z]\.)?", tok))

    def is_title_or_hyphenated(tok: str) -> bool:
        parts = tok.split("-")
        if not parts:
            return False
        for p in parts:
            if not p or not p[0].isalpha():
                return False
            if not p.istitle():
                return False
        return True

    def is_all_caps_word(tok: str) -> bool:
        return tok.isalpha() and tok.isupper()

    def _maybe_from_camel_case(word: str):
        parts = re.findall(
            rf"[A-Z{ACCENTED_UPPER}][a-z{ACCENTED_LOWER}]+",
            word
        )
        if len(parts) < 2:
            return None
        if not all(is_title_or_hyphenated(part) for part in parts):
            return None
        return " ".join(parts)

    # --- 1) standard: NER + zaostrzone heurystyki ---
    for ent in doc.ents:
        if ent.label_ != "PER":
            continue

        candidate = re.sub(r"\s+", " ", ent.text.strip())
        if not candidate:
            continue

        toks = [strip_punct(t) for t in candidate.split()]
        toks = [t for t in toks if t]

        if len(toks) < 2:
            continue

        if any(is_all_caps_word(t) and not is_initial(t) for t in toks):
            continue

        name_like_count = sum(1 for t in toks if is_initial(t) or is_title_or_hyphenated(t))
        has_title_word   = any(is_title_or_hyphenated(t) for t in toks)
        if name_like_count < 2 or not has_title_word:
            continue

        if sum(ch.isalpha() for ch in candidate) < 2:
            continue

        normalized_candidate = CLIENT_NAME_SPLIT_RE.sub(" ", candidate).lower()
        if normalized_candidate in CLIENT_STORE.keys:
            continue

        persons.add(candidate)

    # --- 2) Fallback: "Nazwisko, Imię" (obsługa przecinka i \n) ---
    #    Przykłady: "Jadadic, Edis", "Polach, Karin", "Wagner, Clemens"
    comma_name_re = re.compile(
        r"(?<!\w)"
        r"([A-Z][a-z]+(?:-[A-Z][a-z]+)*)"     # Nazwisko (TitleCase, dopuszczamy łącznik)
        r"\s*,\s*"
        r"([A-Z][a-z]+(?:-[A-Z][a-z]+)*)"     # Imię (TitleCase, dopuszczamy łącznik)
        r"(?!\w)"
    )

    for m in comma_name_re.finditer(prepared_text):
        surname, given = m.group(1), m.group(2)

        # drobne bezpieczniki: uniknij nagłówka "Name, ..." itp.
        if surname.lower() == "name":
            continue

        candidate = f"{surname}, {given}"
        # nie anonimizuj nazw klientów (gdyby jakimś cudem trafiły w ten wzorzec)
        normalized_candidate = CLIENT_NAME_SPLIT_RE.sub(" ", candidate).lower()
        if normalized_candidate in CLIENT_STORE.keys:
            continue

        persons.add(candidate)

    for word in CAMEL_CASE_PERSON_RE.findall(text):
        candidate = _maybe_from_camel_case(word)
        if not candidate:
            continue
        normalized_candidate = CLIENT_NAME_SPLIT_RE.sub(" ", candidate).lower()
        if normalized_candidate in CLIENT_STORE.keys:
            continue
        persons.add(candidate)

    return list(persons)


def find_emails(text):
    pattern = r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"
    return re.findall(pattern, text)

def find_phone_numbers(text):
    # Liberalny wzorzec jak dotąd
    base = re.compile(r'(\+?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,4}[\s\-]?\d{1,4})')

    # Daty ISO (z różnymi łącznikami)
    date_iso = re.compile(
        r'\b(19|20)\d\d[\-/\.\u2010\u2011\u2012\u2013\u2014\u2212]'
        r'(0[1-9]|1[0-2])[\-/\.\u2010\u2011\u2012\u2013\u2014\u2212]'
        r'(0[1-9]|[12]\d|3[01])\b'
    )

    # Heurystyka „to raczej ID, nie telefon”: LITERY(2–10) + łącznik/underscore + >=3 cyfry
    id_like = re.compile(r'^[A-Za-z]{2,10}[\s_\-]?\d{3,}$')

    phones = []
    for m in base.finditer(text):
        n = m.group(0)
        digits = re.sub(r'\D', '', n)
        if len(digits) < 7:
            continue

        # Odrzuć daty
        n_norm = _normalize_client_text(n)
        if date_iso.search(n) or date_iso.search(n_norm):
            continue

        # Klasyczny układ 4-2-2 po unifikacji (YYYY-MM-DD) → też odrzuć
        parts = re.split(r'[\s\-/\.]', n_norm)
        if len(parts) == 3 and len(parts[0]) == 4 and len(parts[1]) == 2 and len(parts[2]) == 2:
            continue

        # --- NOWE: sprawdź token kontekstowy (unikaj złapania cyfr z ID typu "STM-4764956") ---
        start, end = m.start(), m.end()

        # Rozszerz do granic „słowa” złożonego z liter/cyfr/łączników/underscore
        L = start
        while L > 0 and re.match(r'[A-Za-z0-9_\-\u2010-\u2014\u2212]', text[L-1]):
            L -= 1
        R = end
        while R < len(text) and re.match(r'[A-Za-z0-9_\-\u2010-\u2014\u2212]', text[R:R+1]):
            R += 1

        token = _normalize_client_text(text[L:R]).strip()

        # Jeśli cały token wygląda na ID (np. "STM-4764956", "PV_ADC20-193"), to pomiń
        if id_like.match(token):
            continue

        phones.append(n)

    return phones

def _cleanup_half_hyphenated_clients(text: str) -> str:
    # Jeżeli została forma "Client-<drugi_człon>", zlej do "Client"
    for rec in CLIENT_STORE.clients:
        name = rec.get("canonical", "")
        parts = [p for p in CLIENT_NAME_SPLIT_RE.split(name) if p]
        if len(parts) >= 2:
            last = re.escape(parts[-1])
            text = re.sub(
                rf"\bClient\s*{HYPHEN_CHAR_PATTERN}\s*{last}\b",
                "Client",
                text,
                flags=re.IGNORECASE
            )
    return text


def anonymize_text(text, emails, phones, clients, persons):
    anonymized = text

    for email in sorted(set(emails), key=len, reverse=True):
        anonymized = anonymized.replace(email, "mail@mail.com")

    for phone in sorted(set(phones), key=len, reverse=True):
        pattern = re.escape(phone).replace(r"\ ", r"[\s\-]?")
        anonymized = re.sub(pattern, "phone number", anonymized)

        # Klienci
    for canonical in sorted(set(clients), key=len, reverse=True):
        for pattern in CLIENT_STORE.regexes.get(canonical, []):
            anonymized = pattern.sub("Client", anonymized)

    for person in sorted(set(persons), key=len, reverse=True):
        escaped = re.escape(person)
        escaped = escaped.replace(r"\ ", r"\s*")   # allow glued or spaced name components
        escaped = escaped.replace(",", r"\s*,\s*") # keep comma but ignore nearby spacing
        person_pattern = re.compile(rf"(?<!\w){escaped}(?!\w)")
        anonymized = person_pattern.sub("Name", anonymized)

    # NOWE: sprzątanie resztek typu "Client-Benz"
    anonymized = _cleanup_half_hyphenated_clients(anonymized)


    return anonymized

def anonymize_pdf(pdf, parent_widget=None, progress_callback=None, is_cancelled=None, selected_pages=None, output_path=None):
    if not pdf.has_document():
        return

    # Dialog wyboru stron
    if selected_pages is None:
        pages_str, ok = QInputDialog.getText(
            parent_widget, "Select pages",
            "Enter pages to export (e.g. 1-3,5):"
        )
        if not ok or not pages_str.strip():
            return

        selected_pages = set()
        for part in pages_str.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                start, end = int(start), int(end)
                selected_pages.update(range(start - 1, end))
            else:
                selected_pages.add(int(part) - 1)
    else:
        selected_pages = set(selected_pages)

    if not selected_pages:
        return

    # Plik docelowy
    if output_path is None:
        file_path, _ = QFileDialog.getSaveFileName(
            parent_widget, "Save anonymized PDF", "", "PDF files (*.pdf)"
        )
        if not file_path:
            return
    else:
        file_path = output_path

    new_doc = fitz.open()

    # Licznik do procentów (uniknij dzielenia przez zero)
    total = len(selected_pages) or 1

    for idx, p in enumerate(sorted(selected_pages), start=1):
        # Anulowanie (na początku iteracji też warto sprawdzić)
        if is_cancelled and is_cancelled():
            break

        if p < 0 or p >= pdf.page_count():
            continue

        old_page = pdf.doc[p]
        rect = old_page.rect
        new_page = new_doc.new_page(width=rect.width, height=rect.height)

        # Bloki tekstu
        blocks = pdf.get_blocks(p)

        for (x, y, w, h, texto, font_size, font_name, font_style) in blocks:
            if not texto.strip():
                continue

            r = fitz.Rect(
                x / pdf.zoom,
                y / pdf.zoom,
                (x + w),
                (y + h)
            )

            style = (font_style or "normal").lower()
            if "bold" in style and "italic" in style:
                use_font = "Times-BoldItalic"
            elif "bold" in style:
                use_font = "Times-Bold"
            elif "italic" in style:
                use_font = "Times-Italic"
            else:
                use_font = "Times-Roman"

            emails = find_emails(texto)
            phones = find_phone_numbers(texto)
            clients = find_clients(texto)
            persons = find_persons(texto)
            texto_anon = anonymize_text(texto, emails, phones, clients, persons)

            print(f"\n--- Página {p+1} ---")
            print(f"Texto original: {texto}")
            print(f"Emails detectados: {emails}")
            print(f"Teléfonos detectados: {phones}")
            print(f"Clientes detectados: {clients}")
            print(f"Personas detectadas: {persons}")
            print(f"Texto anonimizado: {texto_anon}")

            font_params = _font_kwargs(use_font)
            try:
                new_page.insert_textbox(
                    r,
                    texto_anon,
                    fontsize=font_size,
                    align=0,
                    **font_params
                )
            except Exception as e:
                print(f"⚠ Error insertando texto '{texto}': {e}")
                new_page.insert_textbox(
                    r,
                    texto_anon,
                    fontsize=font_size,
                    align=0,
                    **_font_kwargs("Times-Roman")
                )

        # 🔹 Procent postępu po przetworzeniu strony
        if progress_callback:
            progress = int((idx / total) * 100)
            progress_callback(progress)

        # 🔹 Anulowanie po stronie
        if is_cancelled and is_cancelled():
            break

    # Zapis i zamknięcie — MUSZĄ być wewnątrz funkcji (to te wcięcia!)
    new_doc.save(file_path, garbage=4, deflate=True)
    new_doc.close()



