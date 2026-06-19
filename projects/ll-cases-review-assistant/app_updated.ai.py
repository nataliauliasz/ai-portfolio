from flask import Flask, request, jsonify, send_from_directory
from openai import OpenAI
import os
import json
import requests
from datetime import datetime, timezone

BASE_JAZZ_DEFAULT = "https://jazz.example.internal"

app = Flask(__name__, static_folder=".", static_url_path="")

# ===== OpenAI Responses API (AI analiza pokrycia testów) =====
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


def _extract_responses_text(resp) -> str:
    """Bezpiecznie wyciąga tekst z odpowiedzi Responses API niezależnie od wersji SDK."""
    txt = getattr(resp, "output_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt

    out = getattr(resp, "output", None)
    if isinstance(out, list):
        parts = []
        for item in out:
            content = getattr(item, "content", None) or (item.get("content") if isinstance(item, dict) else None)
            if not content:
                continue
            for c in content:
                ctype = getattr(c, "type", None) or (c.get("type") if isinstance(c, dict) else None)
                if ctype in ("output_text", "text", "message"):
                    t = getattr(c, "text", None) or (c.get("text") if isinstance(c, dict) else None)
                    if isinstance(t, str) and t.strip():
                        parts.append(t)
        if parts:
            return "\n".join(parts)

    return str(resp)

def build_ai_coverage_prompt(wi_description: str, cases: list[dict] = None, test_cases: list[dict] = None) -> str:
    if cases is None:
        cases = test_cases or []
    """
    Prompt: analiza post-project review (opis po projekcie w WI_DESCRIPTION)
    + mapowanie do istniejących casów lessons learned z XLSX.
    """
    import json
    cases_json = json.dumps(cases, ensure_ascii=False, indent=2)

    return f"""You are a senior quality engineer and lessons-learned facilitator in automotive/electronics projects.

DANE WEJŚCIOWE (WAŻNE)
Otrzymasz:
1) WI_DESCRIPTION : opis work itemu z Jazz. To jest opis PO PROJEKCIE (post-project review / lessons learned),
   więc zawiera problemy, wnioski i rekomendacje.
2) CASES_XLSX : lista istniejących “casów” z aktywnego arkusza XLSX

KONTEKST
W description workitemu znajduje się przegląd po projekcie (lista problemów i wniosków). 
W tabeli z danymi w pliku XLSX znajdują się istniejące przypadki testowe z kolumnami: Tast Case ID, Case Title, Department, Assigning Category, Tags. 
Dla każdego problemu z przeglądu po projekcie: zidentyfikuj Test Case ID / Case Title i Tags które: bezpośrednio pokrywają dany problem, lub są z nim pośrednio powiązane 
(np. obejmują podobny obszar funkcjonalny, komponent, tryb testu  np. vibration, EMC, reflashing, software update, labeling, heat sink, coil chokes, etc.).

CEL
1) Wyodrębnij z WI_DESCRIPTION listę DISTINCT problemów/wniosków (po jednym problemie na wiersz tabeli).
2) Dla każdego problemu wskaż, które CASES_XLSX:
   - bezpośrednio dotyczą problemu, lub
   - są pośrednio powiązane (podobny obszar, komponent, tryb testu/ryzyko).
3) Dla każdego dopasowania sklasyfikuj typ powiązania:
   A. Problem ujęty – case wyraźnie dotyczy tego problemu/ryzyka.
   B. Problem częściowo ujęty – case zahacza o obszar, ale nie trafia w sedno / brakuje pełnego ujęcia
      (np. brak endurance, brak in-vehicle EMC, brak reflashing access, brak worst-case).
   C. Problem nieujęty – brak sensownego istniejącego case’a, który ten problem obejmuje.

Zwróć wynik w postaci tabeli z kolumnami: 
Problem z przeglądu po projekcie Powiązany Test Case ID Case Title Typ pokrycia (A/B/C) Uzasadnienie powiązania / komentarz

ZASADY
- Używaj wyłącznie case’ów z CASES_XLSX. Nie twórz fikcyjnych wpisów.
- Jeśli do jednego problemu pasuje kilka case’ów, wypisz wszystkie (osobne wiersze).
- Jeśli do problemu nie pasuje żaden case:
  - dodaj jeden wiersz z: Typ = "C"
  - w komentarzu krótko wyjaśnij brak ujęcia (np. „Brak case’a dot. EMC w pojeździe”, „Brak case’a endurance USB” itp.)

FORMAT WYJŚCIA (BARDZO WAŻNE)
Zwróć wynik po polsku w dokładnie takiej strukturze:

1) Najpierw tabela Markdown z kolumnami w tej kolejności:
| Problem z opisu po projekcie (WI) | Powiązany Case ID (z XLSX) | Case Title | Tags | Typ powiązania (A/B/C) | Uzasadnienie powiązania / komentarz |

- "Typ powiązania (A/B/C)" musi być dokładnie: "A" albo "B" albo "C".
- Uzasadnienie: krótko dlaczego case pasuje (na podstawie tytułu/tagów/działu), albo dlaczego brak powiązania.

2) Następnie nagłówek:
## Kluczowe wnioski
Pod nim 2–5 punktów w języku polskim:
- które obszary nie mają żadnego ujęcia w istniejących casach,
- które są ujęte częściowo,
- co warto dopisać / wzmocnić w przyszłości (np. endurance, EMC in-vehicle, reflashing access, labeling vs HW info).

DANE:
WI_DESCRIPTION (post-project):
{wi_description}

CASES_XLSX (JSON):
{cases_json}
""".strip("\n") + "\n"



CHECK_DATE_PROP_URI = "http://jazz.net/xmlns/prod/jazz/rtc/ext/1.0/CheckDate"

def iso_now_utc():
    # format jak w Twoich danych: 2026-01-09T12:54:37.000Z
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def login_ccm(session: requests.Session, base_url: str, username: str, password: str):
    login_url = f"{base_url.rstrip('/')}/ccm/j_security_check"
    r = session.post(login_url, data={"j_username": username, "j_password": password}, allow_redirects=True, timeout=30)
    r.raise_for_status()

def get_workitem(session: requests.Session, base_url: str, wid: int):
    url = f"{base_url.rstrip('/')}/ccm/oslc/workitems/{wid}"
    headers = {"OSLC-Core-Version": "2.0", "Accept": "application/json"}
    r = session.get(url, headers=headers, timeout=60, allow_redirects=True)
    r.raise_for_status()
    return r.json(), r.headers.get("ETag"), url

def put_workitem(session: requests.Session, wi_url: str, payload: dict, etag: str | None):
    headers = {
        "OSLC-Core-Version": "2.0",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "If-Match": etag if etag else "*",
    }
    return session.put(wi_url, headers=headers, json=payload, timeout=60, allow_redirects=True)

@app.get("/")
def index():
    # serwujemy Twojego HTML-a z localhost, żeby fetch działał bez problemów
    return send_from_directory(".", "xlsx_viewer_v9.ai.html")


@app.post("/api/workitem_description")
def api_workitem_description():
    """Zwraca pole 'description' dla work itemu (OSLC)."""
    data = request.get_json(force=True) or {}

    base = data.get("base") or BASE_JAZZ_DEFAULT
    user = data.get("user")
    password = data.get("password")
    wid = data.get("wid")

    # Walidacja wejścia
    try:
        wid_int = int(wid)
    except Exception:
        wid_int = None

    if not user or not password or not wid_int or wid_int <= 0:
        return jsonify({"ok": False, "error": "Brak wymaganych danych"}), 400

    s = requests.Session()
    try:
        login_ccm(s, base, user, password)
    except Exception as e:
        # 401 jak w Twoich wymaganiach
        return jsonify({"ok": False, "error": f"Logowanie nieudane: {e}"}), 401

    try:
        wi, _etag, _wi_url = get_workitem(s, base, wid_int)

        # OSLC/RTC potrafi trzymać opis pod różnymi kluczami.
        # Najczęściej spotykane to: dcterms:description lub rtc_cm:description.
        candidates = [
            "dcterms:description",
            "rtc_cm:description",
            "description",
            "dc:description",
        ]

        desc = None
        found_key = None

        for k in candidates:
            if k in wi and wi.get(k) is not None:
                desc = wi.get(k)
                found_key = k
                break

        # Czasem opis bywa zagnieżdżony albo w postaci obiektu (np. {'value': '...'}).
        if isinstance(desc, dict):
            # próbujemy typowe pola
            for kk in ("value", "text", "content"):
                if kk in desc and desc.get(kk) is not None:
                    desc = desc.get(kk)
                    break

        # Jeśli nadal nic – zwróć listę kluczy, żeby łatwo dopasować do realnego JSON-a
        if desc is None:
            return jsonify({
                "ok": False,
                "error": "Pole 'description' nie znalezione",
                "keys": sorted(list(wi.keys())),
            }), 404

        # Dla debugowania/dopasowania w przyszłości – wysyłamy też użyty klucz
        return jsonify({"ok": True, "id": wid_int, "description": str(desc), "key": found_key})

    except Exception as e:
        return jsonify({"ok": False, "error": f"Nie udało się pobrać work itemu: {e}"}), 500



@app.post("/api/ai_analyze_test_coverage")
def api_ai_analyze_test_coverage():
    """Analiza pokrycia testów (project review problems -> test cases) przez OpenAI Responses API."""
    if not OPENAI_API_KEY or openai_client is None:
        return jsonify({
            "ok": False,
            "error": "Brak konfiguracji OPENAI_API_KEY. Ustaw zmienną środowiskową OPENAI_API_KEY na serwerze."
        }), 500

    data = request.get_json(force=True) or {}

    wi_description = data.get("wi_description") or ""
    test_cases = data.get("test_cases")

    # --- Walidacja wejścia ---
    if not isinstance(wi_description, str) or not wi_description.strip():
        return jsonify({"ok": False, "error": "Pole 'wi_description' jest wymagane i musi być niepustym stringiem."}), 400
    if not isinstance(test_cases, list) or not test_cases:
        return jsonify({"ok": False, "error": "Pole 'test_cases' jest wymagane i musi być niepustą listą."}), 400

    normalized = []
    for tc in test_cases:
        if not isinstance(tc, dict):
            continue
        normalized.append({
            "test_case_id": str(tc.get("test_case_id") or "").strip(),
            "case_title": str(tc.get("case_title") or "").strip(),
            "department": str(tc.get("department") or "").strip(),
            "assigning_category": str(tc.get("assigning_category") or "").strip(),
            "tags": tc.get("tags") if tc.get("tags") is not None else "",
        })

    if not normalized:
        return jsonify({"ok": False, "error": "Nie udało się zbudować listy test case'ów (brak poprawnych obiektów w 'test_cases')."}), 400

    prompt = build_ai_coverage_prompt(
        wi_description=str(wi_description or "").strip(),
        test_cases=normalized,
    )

    try:
        response = openai_client.responses.create(
            model="gpt-5-mini",
            input=prompt,
            reasoning={"effort": "low"},
            max_output_tokens=35000,
        )
        result_text = _extract_responses_text(response)
        return jsonify({"ok": True, "result_markdown": result_text})
    except Exception as e:
        return jsonify({"ok": False, "error": f"Błąd wywołania OpenAI: {e}"}), 502

@app.post("/api/set_check_date")
def api_set_check_date():
    data = request.get_json(force=True) or {}

    base = data.get("base") or BASE_JAZZ_DEFAULT
    user = data.get("user")
    password = data.get("password")
    ids = data.get("ids") or []

    if not user or not password:
        return jsonify({"ok": False, "error": "Brak loginu lub hasła."}), 400
    if not isinstance(ids, list) or not ids:
        return jsonify({"ok": False, "error": "Brak ID do aktualizacji."}), 400

    # ujednolicamy
    ids = sorted({int(x) for x in ids if str(x).strip().isdigit()})

    s = requests.Session()
    try:
        login_ccm(s, base, user, password)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Logowanie nieudane: {e}"}), 401

    value = iso_now_utc()
    results = []

    for wid in ids:
        try:
            wi, etag, wi_url = get_workitem(s, base, wid)

            # ustawiamy pole Check Date – działający wariant:
            wi["rtc_ext:CheckDate"] = value
            wi[CHECK_DATE_PROP_URI] = value  # zostawiamy też pełny URI jako fallback

            r = put_workitem(s, wi_url, wi, etag)

            if r.status_code in (200, 201, 204):
                results.append({"id": wid, "status": "OK"})
            else:
                results.append({"id": wid, "status": "FAIL", "code": r.status_code, "body": (r.text or "")[:300]})
        except Exception as e:
            results.append({"id": wid, "status": "ERROR", "error": str(e)})

    return jsonify({"ok": True, "applied_value": value, "results": results})

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
