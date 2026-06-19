from __future__ import annotations
import json
"""
UI rendering helpers for Referenced Standards Checker.
"""
TRANSLATIONS = {
    "pl": {
        "help": "Pomoc",
        "login": "Logowanie",
        "how": "Jak działa analiza?",
        "limits": "Możliwe ograniczenia",
        "missing": "Braki w dokumentach na SVN",
        "back": "Wróć",
        "results": "Wyniki analizy",
        "filter": "Filtruj:",
        "filters": "Filtry",
        "export_txt": "Eksport TXT",
        "export_csv": "Eksport CSV",
        "sort_az": "Sortuj: A - Z",
        "sort_za": "Sortuj: Z - A",
        "sort_ok": "Sortuj: Dostępne najpierw",
        "sort_nok": "Sortuj: Niedostępne najpierw",
        "search_placeholder": "Szukaj w wynikach...",
        "welcome_back": "Witaj ponownie",
        "login_hint": "Zaloguj się danymi domeny BURY, a następnie wgraj pliki do analizy.",
        "step_login": "Krok 1/2 · Logowanie",
        "step_upload": "Krok 2/2 · Dodaj pliki",
        "username": "Nazwa użytkownika",
        "password": "Hasło",
        "files": "Pliki (PDF/DOC/DOCX)",
        "supported": "Obsługiwane formaty: PDF, DOC, DOCX. Limit: 50 MB na plik.",
        "btn_login": "Zaloguj",
        "btn_analyze": "Analizuj",
        "progress": "Analiza i porównywanie z SVN...",
        "done": "Zakończono",
        "err_login": "Podaj nazwę użytkownika i hasło, aby kontynuować.",
        "err_add_file": "Dodaj co najmniej jeden plik do analizy.",
        "err_http": "Błąd podczas analizy (status {status}). Spróbuj ponownie.",
        "err_net": "Nie udało się wysłać żądania. Sprawdź połączenie i spróbuj ponownie.",
        "on_svn": "Jest na SVN",
        "not_on_svn": "Brak w SVN",
        "no_data": "Brak danych.",
        "results_of": "Wyniki analizy ({count} plikow)",
        "password_placeholder": "Hasło SVN",
        "hero_p1": "Aplikacja do automatycznego sprawdzania przywołanych norm i standardów w wymaganiach klienta.",
        "hero_p2": "Wgraj dokument zawierający wymagania, aby zweryfikować, które normy są przywołane i czy znajdują się w repozytorium BURY SVN. Wyniki analizy pokażą listę odnalezionych standardów oraz odnośniki do ich lokalizacji w SVN.",
        "feature_1": "Automatyczna identyfikacja przywołanych norm",
        "feature_2": "Dostęp chroniony danymi domenowymi",
        "feature_3": "Szybkie wyszukiwanie i linki do konkretnych norm w SVN",
        "login_subtext": "Zaloguj się danymi domeny BURY, a następnie wgraj pliki do analizy.",
        "login_placeholder": "Login",
        "analyzing": "Analizuję...",
        "help_login_p1": "Login: nazwisko + pierwsza litera imienia (np. kowalskim).",
        "help_login_p2": "Hasło: takie samo jak do systemu Windows oraz IBM Jazz.",
        "help_login_p3": "Jeśli nie możesz się zalogować, sprawdź, czy posiadasz dostęp do folderu Standards na SVN. W przypadku braku dostępu — złóż wniosek o nadanie uprawnień w Helpdesku.",
        "help_how_p1": "Program wykonuje OCR dokumentu oraz sprawdza ostatnie 100 stron, gdzie najczęściej znajdują się referencje do norm i standardów.",
        "help_how_p2": "Wykryte odwołania są porównywane z dokumentami znajdującymi się w repozytorium SVN.",
        "help_limits_p1": "Ze względu na niską jakość dokumentu, nietypowe formatowanie lub błędną konwersję pliku, program może niepoprawnie odczytać część treści.",
        "help_limits_p2": "W takich przypadkach niektóre standardy mogą nie zostać zidentyfikowane lub odnalezione w SVN mimo ich obecności.",
        "help_missing_p1": "Jeśli analiza wskaże brakujące normy lub masz wątpliwości co do poprawności wyników, skontaktuj się ze specjalistą ds. norm klientowskich, który zweryfikuje obecność dokumentów w repozytorium i uzupełni ewentualne braki.",
        "status": "Status",
        "export_txt_filename": "wyniki.txt",
        "export_csv_filename": "wyniki.csv",
        "standard": "Standard"
    },
    "en": {
        "help": "Help",
        "login": "Login",
        "how": "How does the analysis work?",
        "limits": "Possible limitations",
        "missing": "Missing documents in SVN",
        "back": "Back",
        "results": "Analysis results",
        "filter": "Filter:",
        "filters": "Filters",
        "export_txt": "Export TXT",
        "export_csv": "Export CSV",
        "sort_az": "Sort: A - Z",
        "sort_za": "Sort: Z - A",
        "sort_ok": "Sort: Available first",
        "sort_nok": "Sort: Missing first",
        "search_placeholder": "Search results...",
        "welcome_back": "Welcome back",
        "login_hint": "Log in with your BURY domain credentials, then upload files for analysis.",
        "step_login": "Step 1/2 · Login",
        "step_upload": "Step 2/2 · Add files",
        "username": "Username",
        "password": "Password",
        "files": "Files (PDF/DOC/DOCX)",
        "supported": "Supported formats: PDF, DOC, DOCX. Limit: 50 MB per file.",
        "btn_login": "Log in",
        "btn_analyze": "Analyze",
        "progress": "Analyzing and comparing with SVN...",
        "done": "Done",
        "err_login": "Enter username and password to continue.",
        "err_add_file": "Add at least one file to analyze.",
        "err_http": "Analysis error (status {status}). Please try again.",
        "err_net": "Request failed. Check your connection and try again.",
        "on_svn": "On SVN",
        "not_on_svn": "Missing on SVN",
        "no_data": "No data.",
        "results_of": "Analysis results ({count} files)",
        "password_placeholder": "SVN password",
        "hero_p1": "An app for automated checking of referenced standards in customer requirements.",
        "hero_p2": "Upload a requirements document to verify which standards are referenced and whether they exist in the BURY SVN repository. The analysis results will show the detected standards and links to their locations in SVN.",
        "feature_1": "Automatic detection of referenced standards",
        "feature_2": "Access protected with domain credentials",
        "feature_3": "Fast search and direct links to standards in SVN",
        "login_subtext": "Log in with your BURY domain credentials, then upload files for analysis.",
        "login_placeholder": "Login",
        "analyzing": "Analyzing...",
        "help_login_p1": "Login: last name + first letter of first name (e.g., kowalskim).",
        "help_login_p2": "Password: same as for Windows and IBM Jazz.",
        "help_login_p3": "If you cannot log in, check whether you have access to the Standards folder on SVN. If you do not have access, request permissions via the Helpdesk.",
        "help_how_p1": "The program performs OCR and checks the last 100 pages, where references to standards are most commonly found.",
        "help_how_p2": "Detected references are compared with documents stored in the SVN repository.",
        "help_limits_p1": "Due to low document quality, unusual formatting, or incorrect file conversion, the program may read some content incorrectly.",
        "help_limits_p2": "In such cases, some standards may not be identified or may appear missing in SVN even if they exist.",
        "help_missing_p1": "If the analysis indicates missing standards or you are unsure about the results, contact the customer standards specialist, who will verify the repository and fill in any missing documents if needed.",
        "status": "Status",
        "export_txt_filename": "results.txt",
        "export_csv_filename": "results.csv",
        "standard": "Standard"
    }
}

def t(key: str, lang: str = "pl", **fmt) -> str:
    text = TRANSLATIONS.get(lang, TRANSLATIONS["pl"]).get(key, key)
    return text.format(**fmt) if fmt else text

BASE_PAGE = """
<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__TITLE__</title>
  <style>
    :root {
      --accent: #0f5fa8;
      --accent-2: #0ca58f;
      --border: #d4d7de;
      --text: #0b172a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background: radial-gradient(circle at 20% 20%, #e9f5ff, #f7fbff 38%, #eef7ff 68%);
      color: #0f172a;
      font-family: "Manrope", "Segoe UI", Arial, sans-serif;
      padding: 2rem 1.25rem;
      display: flex;
      justify-content: center;
    }
    .help-btn {
      position: fixed;
      top: 18px;
      right: 18px;
      width: 42px;
      height: 42px;
      border-radius: 50%;
      border: 1px solid #d4d7de;
      background: #ffffff;
      color: #0f172a;
      font-weight: 800;
      font-size: 18px;
      cursor: pointer;
      box-shadow: 0 12px 26px rgba(15,23,42,0.12);
      display: inline-flex;
      align-items: center;
      justify-content: center;
      z-index: 10;
      transition: transform .12s ease, box-shadow .15s ease;
    }
    .help-btn:hover { transform: translateY(-1px); box-shadow: 0 14px 30px rgba(15,23,42,0.18); }
    .help-popover {
      position: fixed;
      top: 70px;
      right: 18px;
      width: min(420px, calc(100vw - 36px));
      background: #ffffff;
      color: #0b172a;
      border-radius: 14px;
      border: 1px solid #d4d7de;
      box-shadow: 0 18px 36px rgba(0,0,0,0.16);
      padding: 1rem 1.1rem;
      display: none;
      z-index: 10;
    }
    .help-popover.show { display: block; }
    .help-popover h3 { margin: 0 0 0.35rem 0; font-size: 1rem; }
    .help-popover p { margin: 0.15rem 0 0.4rem 0; color: #111827; line-height: 1.4; }
    .help-popover .section { margin-bottom: 0.65rem; }
    .help-popover .label { font-weight: 800; display: inline-flex; align-items: center; gap: 0.35rem; }
    .help-popover .label .icon { font-size: 1.05rem; }
    .shell { width: 100%; max-width: 1100px; }
    .layout {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 1.5rem;
      align-items: stretch;
    }
    .hero {
      position: relative;
      overflow: hidden;
      border-radius: 20px;
      padding: 1.75rem;
      color: #f9fafb;
      background: linear-gradient(135deg, rgba(16,52,120,0.92), rgba(14,168,133,0.82));
      box-shadow: 0 18px 32px rgba(0,0,0,0.2);
      min-height: 520px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      isolation: isolate;
    }
    .hero::before {
      content: "";
      position: absolute;
      inset: -12% -10%;
      background: radial-gradient(circle at 20% 20%, rgba(255,255,255,0.08), transparent 45%),
                  radial-gradient(circle at 80% 30%, rgba(255,255,255,0.08), transparent 50%),
                  linear-gradient(120deg, rgba(255,255,255,0.08), transparent 60%);
      opacity: 0.8;
      z-index: 0;
    }
    .hero h1 { margin: 0 0 0.6rem 0; letter-spacing: 0.4px; position: relative; z-index: 1; }
    .hero p { margin: 0 0 1.2rem 0; color: #e3e8ef; line-height: 1.45; position: relative; z-index: 1; }
    .hero .list {
      margin: 0; padding: 0; list-style: none; position: relative; z-index: 1;
      display: grid; gap: 0.75rem;
    }
    .hero .list li {
      display: flex; gap: 0.6rem; align-items: flex-start;
      font-weight: 600; color: #f8fafc;
      background: rgba(255,255,255,0.08);
      border-radius: 12px;
      padding: 0.65rem 0.85rem;
      backdrop-filter: blur(4px);
    }
    .hero .list .icon {
      width: 26px; height: 26px;
      border-radius: 10px;
      background: rgba(255,255,255,0.18);
      display: inline-flex; align-items: center; justify-content: center;
      font-size: 14px;
    }
    .hero .footer {
      position: relative; z-index: 1; margin-top: 1.2rem;
      font-weight: 600; letter-spacing: 0.2px;
    }
    .card {
      background: #ffffff;
      border-radius: 20px;
      padding: 1.75rem 1.5rem;
      box-shadow: 0 18px 32px rgba(15,23,42,0.12);
      display: flex;
      flex-direction: column;
      gap: 1rem;
    }
    .card h2 { margin: 0; font-size: 1.35rem; letter-spacing: 0.2px; color: #0f172a; }
    .card p { margin: 0; color: #4b5563; }
    form label { display:block; margin-bottom: .35rem; font-weight: 700; color: #0f172a; font-size: 0.95rem; }
    .field { margin-bottom: 1rem; }
    .input {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      border: 1px solid #d4d7de;
      border-radius: 12px;
      padding: 0.65rem 0.75rem;
      background: #f8fafc;
    }
    .input svg { width: 16px; height: 16px; color: #6b7280; flex-shrink: 0; }
    .input input {
      border: none;
      background: transparent;
      width: 100%;
      font-size: 0.95rem;
      outline: none;
      color: #0f172a;
    }
    .input input::placeholder { color: #9ca3af; }
    .file-box {
      border: 1px dashed var(--border);
      border-radius: 14px;
      padding: 0.9rem;
      background: #fdfefe;
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      color: #111827;
    }
    form input[type=file] {
      padding: .55rem;
      border: 1px solid transparent;
      border-radius: 10px;
      width: 100%;
      background: #fff;
    }
    form button {
      padding: .95rem 1.25rem;
      border-radius: 14px;
      border: 0;
      background: linear-gradient(135deg, #0f5fa8, #0ca58f);
      color: #fff;
      cursor: pointer;
      font-weight: 800;
      letter-spacing: 0.25px;
      transition: transform .1s ease, box-shadow .15s ease, opacity .2s ease;
      width: 100%;
      font-size: 1.02rem;
      box-shadow: 0 16px 32px rgba(12,165,143,0.2);
    }
    form button:hover { transform: translateY(-1px); box-shadow: 0 18px 36px rgba(12,165,143,0.26); }
    form button:disabled { opacity: .65; cursor: not-allowed; box-shadow: none; transform: none; }
    .hidden { display: none !important; }
    .step-label {
      font-weight: 800;
      color: #0f5fa8;
      letter-spacing: 0.2px;
      font-size: 0.9rem;
      margin-bottom: 0.15rem;
      text-transform: uppercase;
    }
    .subtext { color: #6b7280; font-size: 0.92rem; margin: 0; }
    .error { color: #b91c1c; font-weight: 700; font-size: 0.9rem; margin: 0 0 0.5rem 0; }
    .spinner {
      display: none;
      margin-top: 1rem;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      gap: .55rem;
      color: #1f2937;
      font-weight: 600;
      width: 100%;
    }
    .spinner.show { display: flex; }
    .spinner .ring {
      width: 30px;
      height: 30px;
      border: 4px solid rgba(16,52,120,0.18);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 1s linear infinite;
    }
    .spinner .label { font-weight: 800; color: #0f172a; }
    .progress-box {
      width: 100%;
      max-width: 360px;
      display: flex;
      flex-direction: column;
      gap: 0.4rem;
      align-items: stretch;
      color: #0f172a;
    }
    .progress-meta {
      font-size: 0.9rem;
      color: #4b5563;
      text-align: center;
    }
    .progress-bar {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      background: #e5e7eb;
      overflow: hidden;
    }
    .progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(135deg, #0f5fa8, #0ca58f);
      transition: width .2s ease;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .results-wrap {
      margin-top: 2rem;
      display: flex;
      justify-content: center;
    }
    .results-card {
      width: 100%;
      max-width: 920px;
      background: #ffffff;
      border: 1px solid #d4d7de;
      border-radius: 14px;
      padding: 1.2rem;
      box-shadow: 0 16px 38px rgba(0,0,0,0.18);
      color: #0b172a;
    }
    .results-card header {
      background: linear-gradient(135deg, #0f5fa8, #0ca58f);
      padding: 1rem 1.25rem;
      border-radius: 12px;
      color: #fff;
      margin-bottom: 0.9rem;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.08);
    }
    .results-card header h1 { margin: 0; color: #fff; }
    .results-card header p { margin: 0.35rem 0 0; color: #e9f5ff; }
    .control-bar {
      min-height: 48px;
      display: flex;
      gap: 0.4rem;
      align-items: stretch;
      flex-wrap: wrap;
      margin-bottom: 1rem;
    }
    .control-bar > * { align-self: stretch; }
    .pill {
      padding: 0 12px;
      border-radius: 10px;
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: #0b172a;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .pill:hover { border-color: #94a3b8; box-shadow: 0 6px 16px rgba(0,0,0,0.08); }
    .control-bar select,
    .control-bar .pill,
    .filter-bar input {
      height: 42px;
      line-height: 42px;
      padding: 0 12px;
      display: flex;
      align-items: center;
    }
    .control-bar select {
      min-width: 180px;
      background: #ffffff;
      color: #0b172a;
      height: 42px;
      padding: 0 0.75rem;
      border: 1px solid #cbd5e1;
      border-radius: 10px;
    }
    .control-bar select option { color: #000; background: #fff; }
    .filter-bar {
      display: inline-flex;
      align-items: center;
      gap: 0.35rem;
      padding: 0 12px;
      height: 40px;
      border-radius: 10px;
      border: 1px solid #cbd5e1;
      background: #ffffff;
      color: #0b172a;
      flex: 0 0 auto;
    }
    .filter-bar span { display: flex; align-items: center; height: 100%; font-weight: 500; }
    .filter-bar input {
      height: 100%;
      border: none;
      background: transparent;
      color: #0b172a;
      padding: 0;
      width: 160px;
    }
    .filter-bar input::placeholder { color: #6b7280; }
    .filter-bar input:focus { outline: none; }
    .filter-panel {
      display: none;
      margin-top: 0.6rem;
      padding: 0.75rem;
      background: rgba(0,0,0,0.25);
      border: 1px solid var(--border);
      border-radius: 10px;
      color: #f3f4f6;
    }
    .filter-panel.show { display: block; }
    .filter-panel .group { margin-bottom: 0.6rem; }
    .filter-panel label { display: block; color: #f3f4f6; }
    .filter-panel input[type=checkbox] { margin-right: 0.4rem; }
    .export-buttons {
      margin-top: 0.75rem;
      display: flex;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .export-buttons button {
      padding: 0.55rem 0.9rem;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: rgba(255,255,255,0.12);
      color: #f3f4f6;
      cursor: pointer;
    }
    .export-buttons button:hover { border-color: #fff; }
    .details-card {
      margin-top: 1.2rem;
      background: #f7fbff;
      border: 1px solid #e3e8f0;
      border-radius: 12px;
      padding: 1rem 1.2rem;
      box-shadow: 0 8px 18px rgba(0,0,0,0.12);
    }
    .details-card h3 {
      margin: 0 0 0.6rem 0;
      color: #0b172a;
      letter-spacing: 0.3px;
      font-size: 1.05rem;
    }
    .top-back {
      position: fixed;
      top: 14px;
      left: 14px;
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      padding: 0.55rem 0.85rem;
      border-radius: 12px;
      background: rgba(255,255,255,0.96);
      color: #0b172a;
      text-decoration: none;
      border: 1px solid #d4d7de;
      box-shadow: 0 12px 26px rgba(15,23,42,0.12);
      transition: transform .12s ease, box-shadow .15s ease;
      z-index: 5;
    }
    .top-back:hover { transform: translateY(-1px); box-shadow: 0 14px 30px rgba(15,23,42,0.18); }
    .top-back .arrow { font-size: 1rem; line-height: 1; }
    table { width: 100%; border-collapse: collapse; }
    th, td {
      padding: 0.85rem 0.9rem;
      border-bottom: 1px solid #e5e7eb;
      text-align: left;
      color: #0b172a;
    }
    th { font-weight: 700; color: #0b172a; letter-spacing: .3px; cursor: pointer; }
    tr:last-child td { border-bottom: none; }
    table td a {
      display: inline-block;
      background: #f7fbff;
      color: #0b172a !important;
      padding: 0.35rem 0.55rem;
      border-radius: 8px;
      border: none;
      text-decoration: none;
      font-weight: 500;
    }
    table td a:hover { box-shadow: 0 8px 18px rgba(0,0,0,0.12); }
    .status-ok a { color: #0b172a !important; }
    .status-nok {
      background: #f7fbff;
      color: #0b172a;
      font-weight: 500;
      padding: 0.35rem 0.55rem;
      border-radius: 8px;
      border: none;
      display: inline-block;
    }
    .results-card a, .details-card a { color: #0b172a !important; }
    .back-link { text-align: center; margin-top: 1.5rem; }
    .back-link a { color: #dbeafe; text-decoration: none; }
    .back-link a:hover { text-decoration: underline; }
  </style>
</head>
<body>
  <button class="help-btn" id="help-btn" aria-label="__HELP__">?</button>
  <a class="pill" style="position:fixed;top:18px;right:70px;text-decoration:none;" href="?lang=pl">PL</a>
  <a class="pill" style="position:fixed;top:18px;right:120px;text-decoration:none;" href="?lang=en">EN</a>
  <div class="help-popover" id="help-popover" role="dialog" aria-label="__HELP__">
    <div class="section">
      <div class="label"><span class="icon">🔐</span><span>__HELP_LOGIN__</span></div>
      <p>__HELP_LOGIN_P1__</p>
      <p>__HELP_LOGIN_P2__</p>
      <p>__HELP_LOGIN_P3__</p>
    </div>
    <div class="section">
      <div class="label"><span class="icon">📄</span><span>__HELP_HOW__</span></div>
      <p>__HELP_HOW_P1__</p>
      <p>__HELP_HOW_P2__</p>
    </div>
    <div class="section">
      <div class="label"><span class="icon">⚠️</span><span>__HELP_LIMITS__</span></div>
      <p>__HELP_LIMITS_P1__</p>
      <p>__HELP_LIMITS_P2__</p>
    </div>
    <div class="section" style="margin-bottom:0;">
      <div class="label"><span class="icon">📞</span><span>__HELP_MISSING__</span></div>
      <p>__HELP_MISSING_P1__</p>
    </div>
  </div>
  <div class="shell">
    __CONTENT__
  </div>
  <script>
    const helpBtn = document.getElementById('help-btn');
    const helpPopover = document.getElementById('help-popover');
    helpBtn?.addEventListener('click', (e) => {
      e.stopPropagation();
      helpPopover?.classList.toggle('show');
    });
    document.addEventListener('click', (e) => {
      if (!helpPopover || !helpPopover.classList.contains('show')) return;
      if (helpPopover.contains(e.target) || helpBtn?.contains(e.target)) return;
      helpPopover.classList.remove('show');
    });
  </script>
  <script>
  window.I18N = __I18N__;
  </script>
  __SCRIPT__
</body>
</html>
"""

FORM_SCRIPT = """
<script>
const I18N = window.I18N || {};
function tr(key, fallback, vars) {
  let s = (I18N[key] || fallback || key);
  if (vars) {
    Object.keys(vars).forEach(k => {
      s = s.replaceAll(`{${k}}`, String(vars[k]));
    });
  }
  return s;
}
const form = document.getElementById('upload-form');
const spinner = document.getElementById('spinner');
const submitBtn = form.querySelector('button');
const loginStep = document.getElementById('login-step');
const uploadStep = document.getElementById('upload-step');
const username = document.getElementById('username');
const password = document.getElementById('password');
const files = document.getElementById('files');
const errorBox = document.getElementById('form-error');
const progressLabel = document.getElementById('progress-label');
const progressFill = document.getElementById('progress-fill');
const progressMeta = document.getElementById('progress-meta');
let progressTimer = null;
let progressValue = 0;
let progressStartTime = null;
let progressEstMs = 30000;
let stage = 'login';

function setProgress(percent, label, meta) {
  if (progressFill) {
    progressFill.style.width = `${Math.max(0, Math.min(100, percent || 0))}%`;
  }
  if (progressLabel && label) {
    progressLabel.textContent = label;
  }
  if (progressMeta && meta !== undefined) {
    progressMeta.textContent = meta;
  }
}

function estimateDurationMs(totalBytes, totalFiles) {
  const totalMb = Math.max(1, totalBytes / (1024 * 1024));
  const baseMs = 60000; // minimum 1 min
  const perMbMs = 25000; // 25s per MB
  const perExtraFileMs = 20000; // extra 20s per dodatkowy plik
  const est = baseMs + totalMb * perMbMs + Math.max(0, totalFiles - 1) * perExtraFileMs;
  return Math.max(90000, Math.min(est, 420000)); // 1.5 min .. 7 min
}

function startAnalysisProgress(totalFiles, totalBytes) {
  if (progressTimer) clearInterval(progressTimer);
  progressValue = 5;
  progressStartTime = Date.now();
  progressEstMs = estimateDurationMs(totalBytes, totalFiles);
  setProgress(progressValue, tr('progress', 'Analiza i porównywanie z SVN...'), `${progressValue}% `);
  progressTimer = setInterval(() => {
    if (!progressStartTime) return;
    const elapsed = Date.now() - progressStartTime;
    const frac = Math.min(1, elapsed / progressEstMs);
    const target = 5 + Math.round(90 * frac); // 5% -> 95%
    progressValue = Math.min(95, Math.max(progressValue, target));
    setProgress(progressValue, tr('progress', 'Analiza i porównywanie z SVN...'), `${progressValue}% `);
  }, 1000);
}

function stopAnalysisProgress(finalPercent, totalFiles) {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  progressStartTime = null;
  if (typeof finalPercent === 'number') {
    progressValue = finalPercent;
    const label = finalPercent >= 100 ? tr('done', 'Zakończono') : tr('progress', 'Analiza i porównywanie z SVN...');
    setProgress(finalPercent, label, `${finalPercent}% `);
  }
}

form.addEventListener('submit', (e) => {
  errorBox.textContent = '';
  if (stage === 'login') {
    e.preventDefault();
    if (!username.value.trim() || !password.value.trim()) {
      errorBox.textContent = tr('err_login', 'Podaj nazwę użytkownika i hasło, aby kontynuować.');
      return;
    }
    stage = 'upload';
    loginStep.classList.add('hidden');
    uploadStep.classList.remove('hidden');
    submitBtn.textContent = tr('btn_analyze', 'Analizuj');
    submitBtn.setAttribute('aria-label', tr('btn_analyze', 'Analizuj'));
    return;
  }

  e.preventDefault();
  const totalFiles = files.files.length;
  const totalBytes = Array.from(files.files || []).reduce((acc, f) => acc + (f?.size || 0), 0);
  if (!totalFiles) {
    errorBox.textContent = tr('err_add_file', 'Dodaj co najmniej jeden plik do analizy.');
    return;
  }
  spinner.classList.add('show');
  submitBtn.disabled = true;
  startAnalysisProgress(totalFiles, totalBytes);

  const formData = new FormData(form);
  const xhr = new XMLHttpRequest();
  xhr.open(form.method || 'POST', form.action);
  xhr.responseType = 'document';

  xhr.onload = () => {
    stopAnalysisProgress(100, totalFiles);
    if (xhr.status >= 200 && xhr.status < 300) {
      const doc = xhr.responseXML;
      if (doc && doc.documentElement) {
        document.open();
        document.write(doc.documentElement.outerHTML);
        document.close();
      } else {
        document.open();
        document.write(xhr.responseText);
        document.close();
      }
    } else {
      spinner.classList.remove('show');
      submitBtn.disabled = false;
      errorBox.textContent = tr('err_http', 'Błąd podczas analizy (status {status}). Spróbuj ponownie.', { status: xhr.status });
    }
  };

  xhr.onerror = () => {
    stopAnalysisProgress(0, totalFiles);
    spinner.classList.remove('show');
    submitBtn.disabled = false;
    errorBox.textContent = tr('err_net', 'Nie udało się wysłać żądania. Sprawdź połączenie i spróbuj ponownie.');
  };

  xhr.send(formData);
});
</script>
"""

def render_page(title: str, body_html: str, with_form_script: bool = False, lang: str = "pl", script: str = "") -> str:
    final_script = script if script else (FORM_SCRIPT if with_form_script else "")
    i18n_json = json.dumps(TRANSLATIONS.get(lang, TRANSLATIONS["pl"]), ensure_ascii=False)
    return (
        BASE_PAGE
        .replace("__TITLE__", title)
        .replace("__CONTENT__", body_html)
        .replace("__SCRIPT__", final_script)
        .replace('<html lang="pl">', f'<html lang="{lang}">')
        .replace("__HELP__", t("help", lang))
        .replace("__I18N__", i18n_json)
        .replace("__LANG__", lang)
        .replace("__HELP_LOGIN__", t("login", lang))
        .replace("__HELP_HOW__", t("how", lang))
        .replace("__HELP_LIMITS__", t("limits", lang))
        .replace("__HELP_MISSING__", t("missing", lang))
        .replace("__HELP_LOGIN_P1__", t("help_login_p1", lang))
        .replace("__HELP_LOGIN_P2__", t("help_login_p2", lang))
        .replace("__HELP_LOGIN_P3__", t("help_login_p3", lang))
        .replace("__HELP_HOW_P1__", t("help_how_p1", lang))
        .replace("__HELP_HOW_P2__", t("help_how_p2", lang))
        .replace("__HELP_LIMITS_P1__", t("help_limits_p1", lang))
        .replace("__HELP_LIMITS_P2__", t("help_limits_p2", lang))
        .replace("__HELP_MISSING_P1__", t("help_missing_p1", lang))
    )


def render_form_page(title: str, lang: str = "pl") -> str:
    body = """
    <div class="layout">
      <div class="hero">
        <div>
          <h1>""" + title + """</h1>
          <p>""" + t("hero_p1", lang) + """</p>
          <p>""" + t("hero_p2", lang) + """</p>
        </div>
        <ul class="list">
          <li><span class="icon">📁</span> """ + t("feature_1", lang) + """</li>
          <li><span class="icon">🔒</span> """ + t("feature_2", lang) + """</li>
          <li><span class="icon">⚡</span> """ + t("feature_3", lang) + """</li>
        </ul>
      </div>
      <div class="card">
        <div>
          <h2>""" + t("welcome_back", lang) + """</h2>
          <p class="subtext">""" + t("login_subtext", lang) + """</p>
        </div>
        <form id="upload-form" action="/analyze" method="post" enctype="multipart/form-data">
          <input type="hidden" name="lang" value="__LANG__">
          <div id="form-error" class="error"></div>
          <div id="login-step">
            <div class="step-label">""" + t("step_login", lang) + """</div>
            <div class="field">
              <label for="username">""" + t("username", lang) + """</label>
              <div class="input">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M12 12a5 5 0 1 0-5-5 5 5 0 0 0 5 5Z"/><path d="M3 20a9 9 0 0 1 18 0"/></svg>
                <input id="username" name="username" type="text" autocomplete="username" placeholder="Login">
              </div>
            </div>
            <div class="field">
              <label for="password">""" + t("password", lang) + """</label>
              <div class="input">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                <input id="password" name="password" type="password" autocomplete="current-password" placeholder=\"""" + t("password_placeholder", lang) + """\">
              </div>
            </div>
          </div>
          <div id="upload-step" class="hidden">
            <div class="step-label">""" + t("step_upload", lang) + """</div>
            <div class="field">
              <label for="files">""" + t("files", lang) + """</label>
              <div class="file-box">
                <input id="files" name="files" type="file" multiple>
                <small class="subtext" style="margin:0;">""" + t("supported", lang) + """</small>
              </div>
            </div>
          </div>
          <button type="submit" aria-label=\"""" + t("btn_login", lang) + """\">""" + t("btn_login", lang) + """</button>
          <div id="spinner" class="spinner">
            <div class="ring"></div>
            <div class="label" id="progress-label">Analizuje...</div>
            <div class="progress-box">
              <div class="progress-meta" id="progress-meta"></div>
              <div class="progress-bar">
                <div class="progress-fill" id="progress-fill"></div>
              </div>
            </div>
          </div>
        </form>
      </div>
    </div>
    """
    return render_page(title, body, with_form_script=True, lang=lang)



RESULTS_SCRIPT = """
<script>
const filterInput = document.getElementById('filter-input');
const tables = Array.from(document.querySelectorAll('.detail-table'));
const sortSelect = document.getElementById('sort-select');
const filterToggle = document.getElementById('filter-toggle');
const filterPanel = document.getElementById('filter-panel');
const statusChecks = Array.from(document.querySelectorAll('.status-filter'));

function applyFilterAndSort() {
  const term = ((filterInput && filterInput.value) ? filterInput.value : '').toLowerCase();
  const activeStatuses = new Set(statusChecks.filter(c => c.checked).map(c => c.value));
  tables.forEach(table => {
    const rows = Array.from(table.querySelectorAll('tbody tr'));
    rows.forEach(row => {
      const text = row.innerText.toLowerCase();
      const status = row.dataset.status || '';
      const matchText = text.includes(term);
      const matchStatus = activeStatuses.size === 0 || activeStatuses.has(status);
      row.style.display = (matchText && matchStatus) ? '' : 'none';
    });
    const tbody = table.querySelector('tbody');
    const visibleRows = rows.filter(r => r.style.display !== 'none');
    const hiddenRows = rows.filter(r => r.style.display === 'none');
    const mode = sortSelect?.value || 'az';
    visibleRows.sort((a,b) => {
      const an = (a.dataset.name || a.children[0].innerText).toLowerCase();
      const bn = (b.dataset.name || b.children[0].innerText).toLowerCase();
      const as = a.dataset.status || '';
      const bs = b.dataset.status || '';
      const rank = (v) => v === 'ok' ? 0 : 1;
      if (mode === 'az') return an.localeCompare(bn);
      if (mode === 'za') return bn.localeCompare(an);
      if (mode === 'ok') return rank(as) - rank(bs) || an.localeCompare(bn);
      if (mode === 'nok') return rank(bs) - rank(as) || an.localeCompare(bn);
      return 0;
    });
    [...visibleRows, ...hiddenRows].forEach(r => tbody.appendChild(r));
  });
}

filterInput?.addEventListener('input', applyFilterAndSort);
sortSelect?.addEventListener('change', applyFilterAndSort);
statusChecks.forEach(c => c.addEventListener('change', applyFilterAndSort));

filterToggle?.addEventListener('click', () => {
  filterPanel?.classList.toggle('show');
});

function exportData(format) {
  let lines = [];

  const I18N = window.I18N || {};
  function tr(key, fallback) { return (I18N[key] || fallback || key); }

  tables.forEach(table => {
    const rows = Array.from(table.querySelectorAll('tbody tr')).filter(r => r.style.display !== 'none');
    rows.forEach(r => {
      const name = r.children[0].innerText.trim();

      // Zamiast innerText bierzemy href z <a> (jeśli istnieje)
      const td = r.children[1];
      const anchors = Array.from(td.querySelectorAll('a'));
      const hrefs = anchors.map(a => a.href).filter(Boolean);

      const textValue = hrefs.length
        ? hrefs.join(' ; ')
        : tr('not_on_svn', 'Brak w SVN');

      const csvValue = hrefs.length
        ? hrefs.join(' ; ')
        : tr('not_on_svn', 'Brak w SVN');


      if (format === 'csv') {
        const esc = (v) => '"' + String(v).replace(/"/g, '""') + '"';
        lines.push(esc(name) + ',' + esc(csvValue));
      } else {
        lines.push(name + ' - ' + textValue);
      }
    });
  });

  const content = lines.join('\\n');
  const blob = new Blob([content], { type: format === 'csv' ? 'text/csv' : 'text/plain' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;

  a.download = format === 'csv'
    ? tr('export_csv_filename', 'wyniki.csv')
    : tr('export_txt_filename', 'wyniki.txt');

  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

const exportTxtBtn = document.getElementById('export-txt');
const exportCsvBtn = document.getElementById('export-csv');
exportTxtBtn?.addEventListener('click', () => exportData('txt'));
exportCsvBtn?.addEventListener('click', () => exportData('csv'));

applyFilterAndSort();
</script>
"""


def render_results_page(title: str, details_html: str, count: int, lang: str = "pl") -> str:
    no_data_html = "<p style='color:#f3f4f6;'>" + t("no_data", lang) + "</p>"

    parts = [
        f"""
        <a class="top-back" href="/?lang=__LANG__">
          <span class="arrow">&larr;</span>
          <span>{t("back", lang)}</span>
        </a>

        <div class="results-card">
          <header>
            <h1>{title}</h1>
            <p>{t("results_of", lang, count=count)}</p>
          </header>

          <div class="control-bar">
            <select id="sort-select" class="pill">
              <option value="az">{t("sort_az", lang)}</option>
              <option value="za">{t("sort_za", lang)}</option>
              <option value="ok">{t("sort_ok", lang)}</option>
              <option value="nok">{t("sort_nok", lang)}</option>
            </select>

            <div class="filter-bar">
              <span>{t("filter", lang)}</span>
              <input id="filter-input" type="text" placeholder="{t("search_placeholder", lang)}">
            </div>

            <button type="button" id="filter-toggle" class="pill">{t("filters", lang)}</button>
            <button type="button" id="export-txt" class="pill">{t("export_txt", lang)}</button>
            <button type="button" id="export-csv" class="pill">{t("export_csv", lang)}</button>
          </div>

          <div id="filter-panel" class="filter-panel">
            <div class="group">
              <strong>{t("status", lang)}:</strong>
              <label><input type="checkbox" class="status-filter" value="ok" checked>{t("on_svn", lang)}</label>
              <label><input type="checkbox" class="status-filter" value="nok" checked>{t("not_on_svn", lang)}</label>
            </div>
          </div>
        """,
        details_html or no_data_html,
        f"""
        </div>

        <div class="back-link">
          <a href="/?lang=__LANG__">&larr; {t("back", lang)}</a>
        </div>
        """,
        RESULTS_SCRIPT,
    ]

    body = "\n".join(parts)
    return render_page(title, body, with_form_script=False, lang=lang)
