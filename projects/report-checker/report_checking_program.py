import sys
import re
import pdfplumber
from pdf2image import convert_from_path
from pdf2image.exceptions import PDFInfoNotInstalledError
from PIL import Image
import cv2
import numpy as np
from PyPDF2 import PdfReader
import time
import os
import platform
from pathlib import Path
from shutil import which
sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)

# --- Stałe i ścieżki ---
BASE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = platform.system().lower().startswith("win")
IS_WSL = not IS_WINDOWS and "microsoft" in platform.release().lower()


def resolve_poppler_path() -> str | None:
    env_candidate = os.environ.get("POPPLER_PATH")
    if env_candidate:
        candidate_path = Path(env_candidate).expanduser()
        if candidate_path.is_dir():
            return str(candidate_path)

    bundled_path = BASE_DIR / "Release-24.08.0-0" / "poppler-24.08.0" / "Library" / "bin"
    if bundled_path.is_dir() and IS_WINDOWS:
        return str(bundled_path)

    return None


POPPLER_PATH = resolve_poppler_path()
WZORZEC_PATH = BASE_DIR / "patterns" / "wzorzec.png"
ALLOWED_STATUSES = {"Failed", "Passed", "Blocked"}
PAGE_DPI = 150


def ensure_poppler_available():
    global POPPLER_PATH

    if POPPLER_PATH:
        candidates = ["pdfinfo.exe"] if IS_WINDOWS else ["pdfinfo"]
        poppler_dir = Path(POPPLER_PATH)
        for executable in candidates:
            if (poppler_dir / executable).exists():
                return
        POPPLER_PATH = None  # upuść niekompletną ścieżkę i spróbuj dalej

    if which("pdfinfo"):
        POPPLER_PATH = None
        return

    komunikat = (
        "Brak narzędzi Poppler (pdfinfo). Zainstaluj je (np. w WSL: "
        "'sudo apt update && sudo apt install poppler-utils') lub ustaw zmienną "
        "środowiskową POPPLER_PATH na katalog zawierający pdfinfo/pdftoppm."
    )
    print(f"\n❌ {komunikat}")
    raise SystemExit(1)


def sprawdz_nazwe_pliku(nazwa_pliku):
    regex = (
        r'^\d{3}_(QVMA|QVME|QVFM|QVE|QVM|QVEM|QVSW)_' 
        r'\d{4}_'
        r'(PV|DV|CR|IC|RQ|DW|DT|EO|KW)_' 
        r'(MATERIAL|EMC|POWERNET|CEEMC|ENV|ILUMINATION|DIMENSIONAL|FUNCTIONAL|SOFTWARE)_TEST_REPORT_'
        r'.+_R\d+.*$'
    )
    if re.match(regex, nazwa_pliku):
        print("\n ➣✔️ Nazwa pliku jest zgodna z wymaganiami instrukcji.")
    else:
        print("\n ➣ 🔴 Nazwa pliku NIE jest zgodna z wymaganiami!")
        print("Oczekiwany format np.: 057_QVMA_2023_RQ_MATERIAL_REPORT_0854_086_01_2296_001_R1.pdf")

def sprawdz_numer_rewizji_w_nazwie_pliku(sciezka_pliku):
   
    # Wyciągnięcie samej nazwy pliku bez ścieżki i rozszerzenia
    nazwa_pliku = os.path.basename(sciezka_pliku)
    nazwa_bez_rozszerzenia = os.path.splitext(nazwa_pliku)[0]

    # Dopasowanie końcowego wzorca R i liczba, np. R1, R10, R2
    wzorzec = r'R\d+$'
    dopasowanie = re.search(wzorzec, nazwa_bez_rozszerzenia)

    if dopasowanie:
        print(f"✅ Numer rewizji został znaleziony: {dopasowanie.group()}")
        return True
    else:
        print("❌ Brak numeru rewizji na końcu nazwy pliku.")
        return False
    
def collect_test_cases_from_tables(tables, page_number, state, accumulator):
    """
    Replikuje logikę znajdz_test_case_z_tabeli, ale działa w trakcie głównej pętli analizy.
    """
    for table in tables:
        if not table:
            continue

        contains_header = any(
            cell and "TEST CASE" in str(cell).upper()
            for row in table for cell in row
        )
        if contains_header:
            state["pending_header"] = page_number

        for row in table:
            for cell in row:
                if not cell:
                    continue
                match = re.match(r"\s*(\d{4,5}):\s*(.+)", str(cell).strip())
                if match:
                    case_id = match.group(1)
                    case_name = match.group(2)
                    header_page = state.get("pending_header")
                    accumulator.append({
                        "id": case_id,
                        "nazwa": case_name,
                        "strona": header_page if header_page else page_number
                    })
                    state["pending_header"] = None


def clean_line(line):
    return " ".join(line.strip().lower().split())

def show_actual_test_result(table):
    for row in table:
        if not row or not isinstance(row, list):
            continue
        if any(cell and "Actual test result" in cell for cell in row):
            try:
                idx = [i for i, cell in enumerate(row) if cell and "Actual test result" in cell][0]
                status = None
                if idx+1 < len(row) and row[idx+1]:
                    status = row[idx+1].strip()
                elif table.index(row)+1 < len(table):
                    next_row = table[table.index(row)+1]
                    if next_row and len(next_row) > 0 and next_row[0]:
                        status = next_row[0].strip()
                if status:
                    if status in ALLOWED_STATUSES:
                        print(f"\n ➣ 🟢 Poprawna tabela z result overview. Status overview: {status}")
                    else:
                        print(f"\n ➣ Błędnie podany status overview: {status}")
                else:
                    print("Status overview: brak statusu")
                return
            except Exception:
                continue
    print(" ➣ Brak tabeli z overview status")

def find_pattern_opencv(page_img, pattern_img, threshold=0.85):
    page_np = np.array(page_img)
    pattern_np = np.array(pattern_img)
    res = cv2.matchTemplate(page_np, pattern_np, cv2.TM_CCOEFF_NORMED)
    min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
    if max_val >= threshold:
        return True
    return False

def logic_status_from_steps(steps):
    results = set(s[1].lower() for s in steps)
    if results == {'passed'}:
        return 'Passed'
    if results == {'failed'}:
        return 'Failed'
    if results == {'blocked'}:
        return 'Blocked'
    if results == {'passed', 'blocked'}:
        return 'Blocked'
    if 'failed' in results:
        return 'Failed'
    if results == {'passed', 'failed'}:
        return 'Failed'
    return '/'.join(r.capitalize() for r in results)

def znajdz_test_case_dla_strony(defect_page, test_cases):
    # Zbierz zakresy stron dla każdego testu
    test_case_ranges = []
    for case in test_cases:
        strony = set()
        for step in case.get("steps", []):
            if isinstance(step[2], int):
                strony.add(step[2])
        if isinstance(case.get("result_page"), int):
            strony.add(case["result_page"])
        if strony:
            min_strona = min(strony)
            max_strona = max(strony)
            test_case_ranges.append((min_strona, max_strona, case))
    # Znajdź ostatni test, którego końcowa strona jest nie później niż defect_page
    przypisany = None
    najblizsza_strona = -1
    for min_strona, max_strona, case in test_case_ranges:
        if max_strona <= defect_page and max_strona > najblizsza_strona:
            przypisany = case
            najblizsza_strona = max_strona
    return przypisany


def check_missing_comment_refined_text(plumber_page, page_number):
    text = plumber_page.extract_text() or ""
    lines = text.splitlines()
    issues = set()

    for line in lines:
        line_clean = line.strip()
        match = re.search(r"\b(failed|blocked)\b", line_clean, re.IGNORECASE)
        if match:
            after = line_clean[match.end():].strip(" :-–—")
            if not after or after.lower() in ["n/a", "none", "brak", "-"]:
                issues.add(page_number)

    return issues


def is_valid_comment(text):
    if not text:
        return False
    text = text.strip().lower()
    if text in ["-", "n/a", "none", "brak", "failed", "blocked", "passed"]:
        return False
    if text.startswith("general information"):
        return False
    if len(text) < 5:
        return False
    if len(text.split()) < 2:
        return False
    return True


def check_missing_comments_verified(pdf, test_cases):

        for i, page in enumerate(pdf.pages):
            #page = pdf.pages[page_num - 1]
            tables = page.extract_tables()
            for i, blok in enumerate(tables, start=1):
               for wiersz in blok:
                 for j, komorka in enumerate(wiersz):
                  if komorka in ['Blocked', 'Failed']:
                    if j + 1 < len(wiersz):
                      nastepna = wiersz[j + 1]
                    if nastepna == '':
                        print(f"\n ➣ 🔴Błąd na stronie {page}, wiersz {blok.index(wiersz) + 1}, kolumna {j}: '{komorka}' bez komentarza!")

                
def sprawdz_requirements_links(pdf_path):
    try:
        with pdfplumber.open(pdf_path) as pdf:
            total_pages = len(pdf.pages)
            for page_num, page in enumerate(pdf.pages, start=1):
                print(f"Przetwarzanie strony {page_num}/{total_pages}...", end="\r")
                tables = page.extract_tables()
                for table in tables:
                    for row in table:
                        if row and len(row) >= 2:
                            pierwsza_kolumna = row[0]
                            if pierwsza_kolumna and "Requirements Links" in pierwsza_kolumna:
                                wszystkie_puste = True
                                for f in row[1:]:
                                    if f is not None and isinstance(f, str) and f.strip() != "":
                                        wszystkie_puste = False
                                        break
                                if wszystkie_puste:
                                    print(f"\n🔴 Puste pole 'Requirements Links' na stronie {page_num}")
            print("\nPrzetwarzanie zakończone.")
    except Exception as e:
        print(f"Błąd podczas przetwarzania pliku: {e}")


def analyze_pdf_all(pdf_path):
    start_time = time.time()  # Start pomiaru czasu

    ensure_poppler_available()

    reader = PdfReader(pdf_path)
    liczba_stron = len(reader.pages)
    print(f"PDF ma {liczba_stron} stron.")
    print("Rozpoczynam analizę...")

    # --- Sprawdzenie nazwy pliku PDF ---
    nazwa_pliku = pdf_path.split('/')[-1].split('\\')[-1]
    if nazwa_pliku.lower().endswith('.pdf'):
        nazwa_pliku = nazwa_pliku[:-4]
    sprawdz_nazwe_pliku(nazwa_pliku)
    sprawdz_numer_rewizji_w_nazwie_pliku(pdf_path)
    znalezione_tc = []
    tc_scan_state = {"pending_header": None}


    wzorzec = Image.open(WZORZEC_PATH).convert("L")
    headers = ["Test strategy", "Test environment"]

    found_headers = {header: [] for header in headers}
    found_status = None
    requirements_empty = []
    defect_id_empty = []
    test_case_results = []  # lista (status, strona)
    defect_id_pages = []    # strony z Defect ID
    wzorzec_found = []
    missing_comments_pages = set()


    # --- LOGIKA TEST CASE/STEP ---
    test_cases = []
    current_case = {
        "steps": [],
        "result": None,
        "result_found": False,
    }
    in_case = False

    with pdfplumber.open(pdf_path) as pdf:
        # ---- Komentarze dla Failed / Blocked ----
        for i, (plumber_page, py_page) in enumerate(zip(pdf.pages, reader.pages), 1):
          
            print(f"Przetwarzam stronę {i}")

            text = plumber_page.extract_text() or ""
            tables = plumber_page.extract_tables() or []
            collect_test_cases_from_tables(tables, i, tc_scan_state, znalezione_tc)

            # ---- Nagłówki ----
            for line in text.splitlines():
                cleaned = clean_line(line)
                for header in headers:
                    if cleaned == header.lower():
                        found_headers[header].append(i)

            # ---- Status overview ----
            if not found_status:
                for table in tables:
                    if table and table[0] and table[0][0] and "Status overview" in str(table[0][0]):
                        found_status = table
                        break
                else:
                    for line in text.split('\n'):
                        if "Status overview" in line:
                            found_status = [[line]]
                            break

            # ---- Requirements links ----
            for table in tables:
                for row in table:
                    if row and len(row) >= 2:
                        if row[0] and "Requirements Links" in str(row[0]):
                            if all((cell is None or str(cell).strip() == "") for cell in row[1:]):
                                requirements_empty.append(i)

            # ---- TEST CASE/STEPS (logika z feedbacku) ----
            for table in tables:
                for row in table:
                    if not row or all(cell is None for cell in row):
                        continue
                    step_no = None
                    result = None

                    for idx, cell in enumerate(row):
                        value = str(cell).strip().lower() if cell else ""
                        if value in ['passed', 'failed', 'skipped', 'blocked']:
                            if row[0] and str(row[0]).strip().isdigit():
                                step_no = row[0]
                            elif len(row) > 1 and row[1] and str(row[1]).strip().isdigit():
                                step_no = row[1]
                            result = cell.capitalize()
                            break

                    if step_no and result:
                        if str(step_no) == "1" and current_case["steps"]:
                            test_cases.append(current_case)
                            current_case = {"steps": [], "result": None, "result_found": False}
                        current_case["steps"].append((step_no, result, i))
                        in_case = True
                        continue

            # Po zakończeniu wszystkich tabel na stronie, szukaj "TEST CASE RESULT" w surowym tekście tej strony
            if in_case and current_case["result"] is None:
                page_text = plumber_page.extract_text()
                if page_text:
                    lines = page_text.split('\n')
                    for idx_line, line in enumerate(lines):
                        if "test case result" in line.lower():
                            parts = line.split()
                            for idx, part in enumerate(parts):
                                if part.lower() == "result" and idx+1 < len(parts):
                                    status = parts[idx+1].capitalize()
                                    current_case["result"] = status
                                    test_case_results.append((status, i))
                                    current_case["result_found"] = True
                                    in_case = False
                                    break
                            if not current_case["result"]:
                             if idx_line+1 < len(lines):
                                status = lines[idx_line+1].strip().capitalize()
                                if status in ["Passed", "Failed", "Blocked"]:
                                   current_case["result"] = status
                                   test_case_results.append((status, i))  # <-- DODANE
                                   current_case["result_found"] = True
                                   in_case = False
                                   break

            # ---- Defect ID po failu ----
            found_failed = False
            found_defect = False
            # 🆕 Rejestracja WSZYSTKICH wystąpień Defect ID na stronie i
            for table in tables:
               for row in table:
                   if row and len(row) >= 1:
                      pierwsza_kolumna = str(row[0]).strip().lower()
                      if "defect id" in pierwsza_kolumna:
                          defect_id_pages.append(i)
                          break  # nie dodawaj tej samej strony wiele razy

            for table in tables:
                for row in table:
                    if row and any(cell for cell in row):
                        if any("test case result" in str(cell).strip().lower() for cell in row):
                            if any("fail" in str(cell).strip().lower() for cell in row):
                                found_failed = True
            if found_failed:
                for table in tables:
                    for row in table:
                        if row and len(row) >= 2:
                            pierwsza_kolumna = str(row[0]).strip().lower()
                            if "defect id" in pierwsza_kolumna:
                                wszystkie_puste = all(
                                    (cell is None or str(cell).strip() == "") for cell in row[1:]
                                )
                                if wszystkie_puste:
                                    defect_id_empty.append(i)
                                    defect_id_pages.append(i)
                                    found_defect = True
            if not found_defect:
                lines = [l.strip() for l in text.split('\n')]
                for idx, line in enumerate(lines):
                    if "test case result" in line.lower() and "fail" in line.lower():
                        for j in range(idx+1, len(lines)):
                            l = lines[j].strip()
                            if l.lower().startswith("defect id"):
                                po_dwukropku = l.split(":", 1)[1].strip() if ":" in l else ""
                                if not po_dwukropku:
                                    next_line = lines[j+1].strip() if (j+1) < len(lines) else ""
                                    naglowki = [
                                        "test case", "execution", "record", "script", "comments",
                                        "expected result", "page ", "confidential", "stand:", "description"
                                    ]
                                    if (next_line == "" or any(h in next_line.lower() for h in naglowki)):
                                        defect_id_empty.append(i)
                                        defect_id_pages.append(i)
                                break
                            if l.lower().startswith("test case") and j > idx+1:
                                break

            # ---- Wzorzec ----
            convert_kwargs = {"dpi": PAGE_DPI, "first_page": i, "last_page": i}
            if POPPLER_PATH:
                convert_kwargs["poppler_path"] = POPPLER_PATH

            try:
                page_img = convert_from_path(
                    pdf_path, **convert_kwargs
                )[0].convert("L")
            except PDFInfoNotInstalledError:
                komunikat = (
                    "Brak narzędzi Poppler (pdfinfo/pdftoppm). "
                    "Zainstaluj je (np. w WSL: 'sudo apt install poppler-utils') "
                    "lub ustaw zmienną POPPLER_PATH wskazującą katalog bin."
                )
                print(f"\n❌ {komunikat}")
                raise SystemExit(1)

            if find_pattern_opencv(page_img, wzorzec, threshold=0.85):
                wzorzec_found.append(i)

    # --- PODSUMOWANIA ---

    print()  # pusta linia po progresie

   # wyniki = znalezione_tc
   # print("\n🔎 Znalezione Test Case'y z tabel:")
   # for tc in wyniki:
   #     print(f" - {tc['id']}: {tc['nazwa']} (strona {tc['strona']})")


    for header, pages in found_headers.items():
        if pages:
            print(f"\n ➣ ✔️'{header}' znaleziono na stronach: {pages}")
        else:
            print(f"\n ➣ ✔️'{header}' nie został znaleziony w dokumencie.")

    if found_status:
        show_actual_test_result(found_status)
    else:
        print("\n ➣ 🔴 Brak tabeli z overview status")

    if requirements_empty:
        print(f"\n ➣ 🔴 Puste pole 'Requirements Links' na stronach: {requirements_empty}")
    else:
        print("\n ➣ 🟢  Wszystkie pola 'Requirements Links' są uzupełnione.")

    # ---- PODSUMOWANIE TEST CASE/STEP ----
    # --- PRZYPISANIE NUMERÓW TEST CASE’ÓW DO WYNIKÓW ---
    for case in test_cases:
        strony = sorted(set(step[2] for step in case["steps"] if isinstance(step[2], int)))
        if not strony:
            case["real_id"] = None
            continue

        min_page = min(strony)
        max_page = max(strony)

        przypisany = None
        najblizsza_strona = -1
        for tc in znalezione_tc:
            # dopasuj nagłówek TEST CASE który znajduje się na tej samej lub wcześniejszej stronie niż pierwszy krok
            if tc["strona"] <= min_page and tc["strona"] > najblizsza_strona:
                przypisany = tc
                najblizsza_strona = tc["strona"]

        case["real_id"] = przypisany["id"] if przypisany else None


    # --- WYŚWIETLENIE WYNIKÓW TEST CASE’ÓW ---
    if not test_cases:
        print("\n ➣ Nie znaleziono kroków testowych.")
    else:
        for idx, case in enumerate(test_cases, 1):
            logic_status = logic_status_from_steps(case["steps"])
            pdf_status = case['result'] if case['result'] else 'Brak wyniku'
            real_id = case.get("real_id")

            if real_id:
                print(f"\nTest Case {real_id}: {pdf_status} (wg kroków: {logic_status})")
            else:
                print(f"\nTest Case {idx}: {pdf_status} (wg kroków: {logic_status})")

            print("Test Step No. | Test Step Result | Strona")
            for step in case["steps"]:
                print(f"{step[0]:<13} | {step[1]:<16} | {step[2]}")
            if pdf_status != 'Brak wyniku' and logic_status != pdf_status:
                print(f"**BŁĄD: Status test case wg PDF to '{pdf_status}', a powinno być '{logic_status}' na podstawie kroków!**")



# --- DEFEKTY I WZORZEC ---

    defect_id_empty = sorted(set(defect_id_empty))
    if defect_id_empty:
        print(f"\n ➣ 🔴 TEST CASE RESULT = Failed i puste Defect ID na stronach: {defect_id_empty}")
    else:
        print("\n ➣ 🟢 Nie znaleziono przypadków z pustym Defect ID.")

    if wzorzec_found:
        print(f"\n ➣ 🔴 Błąd formatowania znaleziono na stronach: {wzorzec_found}")
    else:
        print("\n ➣ 🟢 Nie znaleziono znaku błędu formatowania na żadnej stronie.")


       # ---- Walidacja: Defect ID po Passed / Blocked ----

    bledy_defect_po_zlym_statusie = []
    defect_id_bez_test_case = []
    zarejestrowane_defect_id = set() 

    for d_page in defect_id_pages:
        if d_page in zarejestrowane_defect_id:
            continue

        przypisany_case = znajdz_test_case_dla_strony(d_page, test_cases)
    
        if  przypisany_case:
            result = przypisany_case.get("result")
            strony = {
                step[2] for step in przypisany_case.get("steps", []) if isinstance(step[2], int)
            }
            if isinstance(przypisany_case.get("result_page"), int):
                strony.add(przypisany_case["result_page"])

            max_strona_testu = max(strony) if strony else -1

            if result in ["Passed", "Blocked"] and d_page >= max_strona_testu:
                bledy_defect_po_zlym_statusie.append((result, max_strona_testu, d_page))

        else:
            defect_id_bez_test_case.append(d_page)

        zarejestrowane_defect_id.add(d_page)

# ---- WYPISYWANIE ----
    if bledy_defect_po_zlym_statusie:
       print("\n ➣ 🔴 BŁĄD: Defect ID pojawił się po Test Case Result = Passed/Blocked!")
       for status, page_status, page_defect in bledy_defect_po_zlym_statusie:
           print(f"    ➥ Status: {status} na stronie {page_status}, Defect ID na stronie {page_defect}")

    if defect_id_bez_test_case:
        print("\n ➣ ℹ️ UWAGA: Defect ID występuje na stronach, które nie mają powiązanego test case:")
        for d_page in defect_id_bez_test_case:
             print(f"    ➥ Defect ID na stronie {d_page}")

    if not bledy_defect_po_zlym_statusie:
        print("\n ➣ 🟢 Wszystkie Defect ID występują tylko po Test Case Result = Failed.")

    end_time = time.time()
    elapsed = end_time - start_time
    minutes = int(elapsed // 60)
    seconds = elapsed % 60
    print(f"\n  ✅ Analiza zakończona w {minutes} min {seconds:.2f} sekundy.")


# --- MENU WYBORU (główny blok) ---
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Użycie: python report-checking-program.py <ścieżka_do_pdf>\n"
            "Po prostu podaj ścieżkę do PDF – zostanie wykonana pełna analiza.\n"
        )
        sys.exit(1)

    pdf_path = sys.argv[1]
    analyze_pdf_all(pdf_path)
