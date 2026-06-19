from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any


CRITERIA_DOCUMENT_PATH = Path(__file__).with_name("kryteria_NOT_OK_DEFECT.txt")
NOT_APPLICABLE_TEXT_PATTERNS = (
    "test not required",
)
SETUP_ISSUE_TEXT_PATTERNS = (
    "blower fault",
)
CHARGING_NOT_OK_TEXT_PATTERNS = (
    "toggling",
    "toggling status",
    "no charging",
    "charging interrupted",
    "charging interference",
    "charging inter",
)
FOD_NOT_OK_TEXT_PATTERNS = (
    "no fod detected",
    "inappropriate temperature",
)
NFC_NOT_OK_TEXT_PATTERNS = (
    "no ok",
    "nok",
    "not ok",
)
DEFECT_REFERENCE_PATTERNS = (
    re.compile(r"\bdefect(?:\s*id)?\b\s*[:#=-]?\s*([A-Za-z]+-\d+|\d{3,}|[A-Za-z0-9_-]{4,})", flags=re.IGNORECASE),
    re.compile(r'"defect(?:\s*_?id)?"\s*:\s*"([^"]+)"', flags=re.IGNORECASE),
)
CHARGING_FAILURE_STATUSES = {"0", "1", "4", "6", "8", "9", "13", "14", "17"}
CHARGING_CONTEXTUAL_STATUSES = {"2", "5", "15", "16"}
FOD_BLOCKING_EXPECTED_STATUSES = {"4", "6"}
FOD_NOT_OK_STATUSES = {"3", "5", "8", "9", "13", "14", "17"}
RFID_PROTECTION_OK_STATUSES = {"15", "16"}
GENERAL_RULE_ROWS = [
    {
        "scenario_label": "Charging / WLC / Start Charging",
        "pass_rule": "OK: status 3, czyli telefon wykryty i ladowanie trwa.",
        "not_ok_rule": "NOT OK: NO CHARGING, toggling status oraz statusy 0/1/4/5/6/8/9/13/14/17; status 2 zalezy od SoC i celu testu.",
        "defect_rule": "DEFECT: dopiero gdy NOT OK jest potwierdzony, powtarzalny albo ma przypisany Defect ID.",
    },
    {
        "scenario_label": "Charging 0-100%",
        "pass_rule": "OK: stabilne ladowanie bez przerw, bez bledu i bez przegrzania; typowo status 3.",
        "not_ok_rule": "NOT OK: NO CHARGING, toggling status oraz statusy 0/1/4/6/8/9/13/14/17; status 5 jest podejrzany i zalezy od interpretacji testu.",
        "defect_rule": "DEFECT: tylko po potwierdzeniu, powtarzalnosci albo przy wpisanym Defect ID.",
    },
    {
        "scenario_label": "NFC",
        "pass_rule": "OK: bezposrednio wpisane OK.",
        "not_ok_rule": "NOT OK: NO OK / NOT OK / NOK albo zachowanie niezgodne z oczekiwaniem testu NFC.",
        "defect_rule": "DEFECT: gdy problem NFC jest potwierdzony lub ma przypisany Defect ID / opis defektu.",
    },
    {
        "scenario_label": "FOD",
        "pass_rule": "OK: zachowanie zgodne z oczekiwaniem dla konkretnego obiektu FOD, temperatury i pozycji; czesto status 4 lub 6, ale zalezy od scenariusza.",
        "not_ok_rule": "NOT OK: NO FOD DETECTED, status 3 lub 5 przy oczekiwanej blokadzie, status 6 przy nieoczekiwanym braku ladowania, nieprawidlowa temperatura albo wynik procentowy poza oczekiwaniem.",
        "defect_rule": "DEFECT: gdy taki wynik FOD jest potwierdzony, powtarzalny albo ma przypisany Defect ID.",
    },
    {
        "scenario_label": "RFID Protection",
        "pass_rule": "OK: status 15 albo 16, jesli ochrona RFID/NFC zadzialala zgodnie z logika testu.",
        "not_ok_rule": "NOT OK: brak 15/16 tam, gdzie ochrona powinna sie aktywowac, albo status 3/4/13/17/toggling status.",
        "defect_rule": "DEFECT: dopiero po potwierdzeniu, powtarzalnosci albo przy wpisanym Defect ID.",
    },
]
GENERAL_NOTES = [
    "Pelny tekst zasad obowiazujacych w aplikacji znajduje sie w pliku rules/kryteria_NOT_OK_DEFECT.txt i zostal przeniesiony 1:1 z zalacznika.",
    "Sam status nie wystarcza do ustawienia DEFECT. Najpierw wynik jest oceniany jako OK / NOT OK / brak danych / N/A, a DEFECT wymaga potwierdzenia, powtarzalnosci albo Defect ID.",
    "Interpretacja zawsze zalezy od typu testu oraz kombinacji sample + software + phone/receiver + position + dodatkowy kontekst FOD lub RFID.",
]


PROJECT_RULES: dict[str, dict[str, Any]] = {
    "default": {
        "project_number": "default",
        "label": "Reguly domyslne",
        "focus_metrics": [
            "scenario",
            "hmi_status",
            "eff",
            "rx",
            "tx",
            "temperature",
            "position",
            "FOD object",
            "card position",
        ],
        "charging_defect_statuses": set(),
        "charging_not_ok_statuses": set(CHARGING_FAILURE_STATUSES) | set(CHARGING_CONTEXTUAL_STATUSES),
        "charging_ok_statuses": {"3"},
        "rfid_pass_statuses": set(RFID_PROTECTION_OK_STATUSES),
        "rfid_not_ok_statuses": set(),
        "rfid_defect_statuses": set(),
        "fod_pass_statuses": set(FOD_BLOCKING_EXPECTED_STATUSES),
        "fod_not_ok_statuses": set(FOD_NOT_OK_STATUSES),
        "fod_defect_statuses": set(),
        "temperature_warning_c": 0.0,
        "temperature_alarm_c": 0.0,
        "temperature_critical_c": 0.0,
        "temperature_extreme_c": 0.0,
        "low_eff_warning_threshold": 0.0,
        "fod_high_eff_threshold": 0.0,
        "rule_rows": list(GENERAL_RULE_ROWS),
        "notes": list(GENERAL_NOTES),
    },
    "0854_086": {
        "project_number": "0854_086",
        "label": "Projekt 0854_086",
        "focus_metrics": [
            "scenario",
            "hmi_status",
            "eff",
            "temperature",
            "position",
            "software_version",
            "FOD object",
            "card position",
        ],
        "charging_defect_statuses": set(),
        "charging_not_ok_statuses": set(CHARGING_FAILURE_STATUSES) | set(CHARGING_CONTEXTUAL_STATUSES),
        "charging_ok_statuses": {"3"},
        "rfid_pass_statuses": set(RFID_PROTECTION_OK_STATUSES),
        "rfid_not_ok_statuses": set(),
        "rfid_defect_statuses": set(),
        "fod_pass_statuses": set(FOD_BLOCKING_EXPECTED_STATUSES),
        "fod_not_ok_statuses": set(FOD_NOT_OK_STATUSES),
        "fod_defect_statuses": set(),
        "temperature_warning_c": 0.0,
        "temperature_alarm_c": 0.0,
        "temperature_critical_c": 0.0,
        "temperature_extreme_c": 0.0,
        "low_eff_warning_threshold": 0.0,
        "fod_high_eff_threshold": 0.0,
        "rule_rows": list(GENERAL_RULE_ROWS),
        "notes": list(GENERAL_NOTES),
    },
    "0854_108": {
        "project_number": "0854_108",
        "label": "Projekt 0854_108",
        "focus_metrics": [
            "scenario",
            "hmi_status",
            "eff",
            "temperature",
            "position",
            "software_version",
            "FOD object",
            "card position",
        ],
        "charging_defect_statuses": set(),
        "charging_not_ok_statuses": set(CHARGING_FAILURE_STATUSES) | set(CHARGING_CONTEXTUAL_STATUSES),
        "charging_ok_statuses": {"3"},
        "rfid_pass_statuses": set(RFID_PROTECTION_OK_STATUSES),
        "rfid_not_ok_statuses": set(),
        "rfid_defect_statuses": set(),
        "fod_pass_statuses": set(FOD_BLOCKING_EXPECTED_STATUSES),
        "fod_not_ok_statuses": set(FOD_NOT_OK_STATUSES),
        "fod_defect_statuses": set(),
        "temperature_warning_c": 0.0,
        "temperature_alarm_c": 0.0,
        "temperature_critical_c": 0.0,
        "temperature_extreme_c": 0.0,
        "low_eff_warning_threshold": 0.0,
        "fod_high_eff_threshold": 0.0,
        "rule_rows": list(GENERAL_RULE_ROWS),
        "notes": list(GENERAL_NOTES),
    },
}


def _clone_profile(profile: dict[str, Any]) -> dict[str, Any]:
    cloned: dict[str, Any] = {}
    for key, value in profile.items():
        if isinstance(value, dict):
            cloned[key] = dict(value)
        elif isinstance(value, list):
            cloned[key] = list(value)
        elif isinstance(value, set):
            cloned[key] = set(value)
        else:
            cloned[key] = value
    return cloned


def resolve_project_profile(project_number: str) -> dict[str, Any]:
    if project_number and project_number in PROJECT_RULES:
        return _clone_profile(PROJECT_RULES[project_number])
    profile = _clone_profile(PROJECT_RULES["default"])
    if project_number:
        profile["project_number"] = project_number
        profile["label"] = f"Projekt {project_number}"
        profile["notes"] = [
            *profile["notes"],
            "Brak dedykowanego profilu projektu, wiec aplikacja uzywa regul domyslnych.",
        ]
    return profile


def _sort_status_codes(status_codes: set[str]) -> list[str]:
    return sorted(status_codes, key=lambda item: (int(item) if item.isdigit() else 999, item))


def _format_status_label(status_codes: set[str]) -> str:
    if not status_codes:
        return "brak statusu"
    return ", ".join(f"status {code}" for code in _sort_status_codes(status_codes))


def _format_problem_timestamp(value: datetime | None) -> str:
    if value is None:
        return "brak czasu"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _normalize_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip().lower()


def _collect_session_texts(session: dict[str, Any]) -> list[str]:
    texts: list[str] = []

    for value in session.get("scenario_hints") or []:
        normalized = _normalize_text(value)
        if normalized:
            texts.append(normalized)

    for field_name in (
        "fod_object",
        "card_position",
        "position",
        "software_version",
        "sample_label",
        "manual_result",
        "defect_id",
        "defect_comment",
        "source_csv_file",
    ):
        normalized = _normalize_text(session.get(field_name))
        if normalized:
            texts.append(normalized)

    for row in session.get("rows") or []:
        for field_name in (
            "scenario_hint",
            "fod_object",
            "card_position",
            "sample_label",
            "manual_result",
            "defect_id",
            "defect_comment",
            "row_json",
        ):
            normalized = _normalize_text(row.get(field_name))
            if normalized:
                texts.append(normalized)

    return texts


def _has_any_pattern(texts: list[str], patterns: tuple[str, ...]) -> bool:
    return any(pattern in text for text in texts for pattern in patterns)


def _has_explicit_status_text(texts: list[str], status_code: str) -> bool:
    pattern = re.compile(rf"\b(?:status|state)\s*{re.escape(status_code)}\b", flags=re.IGNORECASE)
    return any(pattern.search(text) for text in texts)


def _extract_defect_references(texts: list[str]) -> list[str]:
    references: list[str] = []
    seen: set[str] = set()

    for text in texts:
        for pattern in DEFECT_REFERENCE_PATTERNS:
            for match in pattern.finditer(text):
                candidate = " ".join((match.group(1) or "").split()).strip(" :#-_,.;")
                normalized = candidate.lower()
                if not candidate or normalized in seen:
                    continue
                seen.add(normalized)
                references.append(candidate)
    return references


def _session_is_not_applicable(texts: list[str]) -> bool:
    return _has_any_pattern(texts, NOT_APPLICABLE_TEXT_PATTERNS)


def _session_has_setup_issue(texts: list[str]) -> bool:
    return _has_any_pattern(texts, SETUP_ISSUE_TEXT_PATTERNS)


def _is_charging_0_100_session(texts: list[str]) -> bool:
    return any("charging 0-100" in text or "charging 0 -100" in text or "0-100" in text for text in texts)


def _status_samples_matching(session: dict[str, Any], candidate_statuses: set[str]) -> int:
    return sum(
        1
        for sample in session.get("status_samples") or []
        if str(sample.get("status") or "") in candidate_statuses
    )


def _is_repeatable_problem(session: dict[str, Any], candidate_statuses: set[str], texts: list[str]) -> bool:
    if candidate_statuses and _status_samples_matching(session, candidate_statuses) >= 2:
        return True
    if len(session.get("interruptions") or []) >= 2:
        return True
    if session.get("alarm_event_count", 0) >= 2:
        return True
    if _has_any_pattern(texts, CHARGING_NOT_OK_TEXT_PATTERNS + FOD_NOT_OK_TEXT_PATTERNS) and (session.get("sample_count") or 0) >= 3:
        return True
    return False


def _charging_completed_normally(session: dict[str, Any], texts: list[str]) -> bool:
    if not _is_charging_0_100_session(texts):
        return False

    real_statuses = [
        str(sample.get("status") or "")
        for sample in session.get("status_samples") or []
        if str(sample.get("status") or "") != "N/A"
    ]
    if not real_statuses:
        return False
    return set(real_statuses) <= {"2", "3"} and real_statuses[-1] == "2" and "3" in real_statuses


def _project_field_available(session: dict[str, Any], field_name: str) -> bool:
    coverage = session.get("project_field_coverage") or {}
    return bool(coverage.get(field_name))


def _candidate_problem_statuses(
    *,
    scenario_code: str,
    verdict: str,
    profile: dict[str, Any],
) -> set[str]:
    if verdict not in {"warning", "not_ok", "defect"}:
        return set()

    if scenario_code == "rfid":
        return set(str(code) for code in range(0, 18)) - set(profile["rfid_pass_statuses"])

    if scenario_code == "fod":
        return set(profile["fod_not_ok_statuses"]) | {"4", "6"}

    if scenario_code == "nfc":
        return set()

    return set(profile["charging_not_ok_statuses"])


def _find_first_problem_status_sample(
    session: dict[str, Any],
    *,
    scenario_code: str,
    verdict: str,
    profile: dict[str, Any],
) -> dict[str, Any] | None:
    candidate_statuses = _candidate_problem_statuses(
        scenario_code=scenario_code,
        verdict=verdict,
        profile=profile,
    )
    if not candidate_statuses:
        return None

    for sample in session.get("status_samples") or []:
        status_code = str(sample.get("status") or "")
        if status_code == "N/A" or status_code not in candidate_statuses:
            continue
        return {
            "status": status_code,
            "timestamp": sample.get("timestamp"),
        }
    return None


def _find_first_problem_timestamp(session: dict[str, Any], status_sample: dict[str, Any] | None) -> datetime | None:
    if status_sample is not None and status_sample.get("timestamp") is not None:
        return status_sample["timestamp"]

    event_timestamps = [
        event.get("start_ts") or event.get("end_ts")
        for event in session.get("position_events") or []
        if event.get("start_ts") is not None or event.get("end_ts") is not None
    ]
    if event_timestamps:
        return min(event_timestamps)

    return session.get("start_ts") or session.get("end_ts")


def _describe_scenario_issue(
    *,
    scenario_code: str,
    verdict: str,
    status_sample: dict[str, Any] | None,
) -> str:
    if verdict not in {"warning", "not_ok", "defect"}:
        return ""

    problem_status = str(status_sample.get("status")) if status_sample is not None else ""

    if scenario_code == "rfid":
        if problem_status == "16":
            return "RFID nie zablokowal ladowania poprawnie"
        if problem_status:
            return f"RFID nie zadzialal, pojawil sie status {problem_status}"
        return "RFID nie zadzialal zgodnie z oczekiwaniem"

    if scenario_code == "fod":
        if problem_status == "5":
            return "FOD zadzialal tylko czesciowo"
        if problem_status == "3":
            return "FOD nie zablokowal ladowania"
        if problem_status:
            return f"FOD wszedl w problematyczny status {problem_status}"
        return "Scenariusz FOD wymaga sprawdzenia"

    if scenario_code == "nfc":
        return "NFC ma wynik NO OK"

    if problem_status:
        return f"Przebieg ladowania odbiega od wzorca przez status {problem_status}"
    return "Przebieg ladowania odbiega od wzorca"


def _build_detail_labels(
    session: dict[str, Any],
    *,
    scenario: dict[str, str],
    verdict: str,
    profile: dict[str, Any],
    status_codes: set[str],
) -> tuple[str, str, datetime | None]:
    base_status_label = _format_status_label(status_codes)
    base_scenario_label = scenario["label"]

    status_sample = _find_first_problem_status_sample(
        session,
        scenario_code=scenario["code"],
        verdict=verdict,
        profile=profile,
    )
    problem_timestamp = _find_first_problem_timestamp(session, status_sample)

    if verdict not in {"warning", "not_ok", "defect"}:
        return base_status_label, base_scenario_label, problem_timestamp

    phone_label = str(session.get("phone") or "").strip() or "brak telefonu"
    timestamp_label = _format_problem_timestamp(problem_timestamp)
    scenario_issue_label = _describe_scenario_issue(
        scenario_code=scenario["code"],
        verdict=verdict,
        status_sample=status_sample,
    )

    if status_sample is not None:
        status_detail_label = (
            f"{base_status_label} "
            f"(pierwszy bledny status {status_sample['status']}, pomiar {timestamp_label}, telefon {phone_label})"
        )
    else:
        status_detail_label = f"{base_status_label} (pomiar {timestamp_label}, telefon {phone_label})"

    scenario_detail_bits = [scenario_issue_label, f"pomiar {timestamp_label}", f"telefon {phone_label}"]
    scenario_detail_label = f"{base_scenario_label} ({', '.join(bit for bit in scenario_detail_bits if bit)})"
    return status_detail_label, scenario_detail_label, problem_timestamp


def infer_session_scenario(session: dict[str, Any], profile: dict[str, Any]) -> dict[str, str]:
    status_codes = set(str(code) for code in session.get("status_codes") or [])
    texts = _collect_session_texts(session)
    hint_blob = " ".join(texts)

    has_fod_object = bool(str(session.get("fod_object") or "").strip())
    has_card_position = bool(str(session.get("card_position") or "").strip())

    if has_card_position or status_codes & (profile["rfid_pass_statuses"] | profile["rfid_not_ok_statuses"]):
        label = "RFID Protection 2" if "protection_2" in hint_blob else "RFID Protection"
        reason = "status 15/16 albo card position wskazuje scenariusz RFID"
        return {"code": "rfid", "label": label, "confidence": "high", "reason": reason}

    if "rfid" in hint_blob or "card position" in hint_blob:
        label = "RFID Protection 2" if "protection_2" in hint_blob else "RFID Protection"
        return {"code": "rfid", "label": label, "confidence": "high", "reason": "tekst sesji wskazuje scenariusz RFID"}

    if has_fod_object or "fod" in hint_blob or "foreign object" in hint_blob:
        return {"code": "fod", "label": "FOD", "confidence": "high", "reason": "sesja ma FOD object albo hint FOD"}

    if "nfc" in hint_blob and "rfid" not in hint_blob:
        return {"code": "nfc", "label": "NFC", "confidence": "medium", "reason": "tekst sesji wskazuje scenariusz NFC"}

    if "charging 0-100" in hint_blob or "0-100" in hint_blob or re.search(r"\bwlc\b", hint_blob):
        return {
            "code": "charging",
            "label": "Charging / WLC",
            "confidence": "high",
            "reason": "tekst sesji wskazuje Charging/WLC",
        }

    if status_codes == {"5"}:
        return {
            "code": "unknown",
            "label": "Unknown / mixed",
            "confidence": "low",
            "reason": "status 5 jest scenariuszowo zalezne i bez FOD object pozostaje niejednoznaczny",
        }

    if status_codes & {"4", "6"} and not status_codes & {"2", "3"}:
        return {
            "code": "unknown",
            "label": "Unknown / mixed",
            "confidence": "low",
            "reason": "status 4/6 bez scenariusza moze oznaczac FOD albo odchylenie w zwyklym ladowaniu",
        }

    return {
        "code": "charging",
        "label": "Charging / WLC",
        "confidence": "medium" if status_codes else "low",
        "reason": "fallback do najczestszego scenariusza ladowania",
    }


def assess_session_against_project_rules(
    session: dict[str, Any],
    *,
    project_number: str,
    benchmark: dict[str, Any] | None = None,
    project_has_status_readout: bool | None = None,
) -> dict[str, Any]:
    profile = resolve_project_profile(project_number)
    status_codes = set(str(code) for code in session.get("status_codes") or [])
    scenario = infer_session_scenario(session, profile)
    texts = _collect_session_texts(session)
    defect_references = _extract_defect_references(texts)
    has_explicit_defect_reference = bool(defect_references)
    has_not_applicable_marker = _session_is_not_applicable(texts)
    has_setup_issue = _session_has_setup_issue(texts)
    status_detail_label, scenario_detail_label, problem_timestamp = _build_detail_labels(
        session,
        scenario=scenario,
        verdict="ok",
        profile=profile,
        status_codes=status_codes,
    )
    reasons: list[str] = []
    evidence: list[dict[str, str]] = [
        {"label": "Projekt", "value": project_number or "domyslny"},
        {"label": "Scenariusz", "value": scenario["label"]},
        {"label": "Statusy", "value": _format_status_label(status_codes)},
    ]

    if session.get("fod_object"):
        evidence.append({"label": "FOD object", "value": str(session["fod_object"])})
    if session.get("card_position"):
        evidence.append({"label": "Card position", "value": str(session["card_position"])})
    if session.get("avg_eff") is not None:
        evidence.append({"label": "Avg eff", "value": f"{session['avg_eff']:.1f}%"})
    if session.get("max_temperature") is not None:
        evidence.append({"label": "Max temp", "value": f"{session['max_temperature']:.1f} C"})
    if session.get("interruptions"):
        evidence.append({"label": "Przerwy", "value": str(len(session["interruptions"]))})
    if session.get("alarm_event_count"):
        evidence.append({"label": "Alarmy", "value": str(session["alarm_event_count"])})
    if defect_references:
        evidence.append({"label": "Defect ID", "value": ", ".join(defect_references)})

    verdict = "ok"
    label = "OK"
    badge_class = "ok"
    severity_score = 0
    defect_candidate_statuses: set[str] = set()
    defect_eligible = False

    def mark(new_verdict: str, reason: str, score: int, *, force: bool = False) -> None:
        nonlocal verdict, label, badge_class, severity_score
        priorities = {
            "ok": 0,
            "warning": 1,
            "not_applicable": 2,
            "status_unavailable": 2,
            "no_data": 3,
            "not_ok": 4,
            "defect": 5,
        }
        if force or priorities.get(new_verdict, 0) >= priorities.get(verdict, 0):
            verdict = new_verdict
            label = {
                "ok": "OK",
                "warning": "Warning",
                "not_applicable": "Not Applicable",
                "no_data": "No Data",
                "not_ok": "Not OK",
                "defect": "Defect",
                "status_unavailable": "Brak HMI w projekcie",
            }[new_verdict]
            badge_class = {
                "ok": "ok",
                "warning": "warn",
                "not_applicable": "neutral",
                "no_data": "neutral",
                "not_ok": "warn",
                "defect": "critical",
                "status_unavailable": "neutral",
            }[new_verdict]
        severity_score = max(severity_score, score)
        reasons.append(reason)

    if not status_codes:
        if scenario["code"] == "nfc":
            if _has_any_pattern(texts, NFC_NOT_OK_TEXT_PATTERNS):
                mark("not_ok", "Scenariusz NFC ma wynik NO OK / NOK.", 2)
            elif any(text == "ok" or text.endswith(" ok") for text in texts):
                reasons.append("Scenariusz NFC ma wynik OK.")
                verdict = "ok"
        elif has_not_applicable_marker:
            mark("not_applicable", "Test oznaczono jako N/A albo Test not required, wiec nie nalezy ustawiac NOT OK ani DEFECT.", 1, force=True)

        if verdict not in {"ok", "warning"}:
            pass
        elif project_has_status_readout is False:
            reasons.append(
                "Ten projekt w aktualnym zakresie nie raportuje hmi_status, wiec ocena statusowa nie ma zastosowania."
            )
            verdict = "status_unavailable"
            label = "Brak HMI w projekcie"
            badge_class = "neutral"
        else:
            reasons.append("Brak hmi_status w sesji, wiec nie da sie jednoznacznie przypisac scenariusza ani werdyktu.")
            verdict = "no_data"
            label = "Brak danych HMI"
            badge_class = "neutral"

        return {
            "verdict": verdict,
            "label": label,
            "badge_class": badge_class,
            "severity_score": severity_score,
            "reasons": reasons,
            "status_codes": [],
            "status_label": "status niedostepny w projekcie" if verdict == "status_unavailable" else "brak statusu",
            "status_detail_label": "status niedostepny w projekcie" if verdict == "status_unavailable" else "brak statusu",
            "scenario_code": scenario["code"],
            "scenario_label": scenario["label"],
            "scenario_detail_label": scenario["label"],
            "scenario_confidence": scenario["confidence"],
            "rule_label": profile["label"],
            "focus_metrics": list(profile["focus_metrics"]),
            "evidence_rows": evidence,
            "problem_timestamp": None,
            "problem_timestamp_label": "brak czasu",
        }

    if scenario["code"] == "rfid":
        unexpected_statuses = status_codes - set(profile["rfid_pass_statuses"])
        defect_candidate_statuses |= unexpected_statuses
        if status_codes <= profile["rfid_pass_statuses"]:
            reasons.append("Sesja pozostaje w statusie 15 lub 16, zgodnie z ochronna logika RFID/NFC.")
        else:
            defect_eligible = True
            mark("not_ok", "Scenariusz RFID nie aktywuje oczekiwanej ochrony 15/16 albo wchodzi w inny status.", 2)

    elif scenario["code"] == "fod":
        has_fod_object = bool(str(session.get("fod_object") or "").strip())
        if _has_any_pattern(texts, FOD_NOT_OK_TEXT_PATTERNS):
            defect_candidate_statuses |= status_codes
            defect_eligible = True
            mark("not_ok", "Scenariusz FOD zawiera NO FOD DETECTED albo nieprawidlowa temperature.", 2)
        elif status_codes & {"3", "5"}:
            defect_candidate_statuses |= status_codes & {"3", "5"}
            defect_eligible = True
            if has_fod_object or scenario["confidence"] == "high":
                mark("not_ok", "Scenariusz FOD nadal laduje mimo obiektu obcego albo blokuje tylko czesciowo.", 2)
            else:
                mark("warning", "Status 3/5 moze byc poprawny albo niepoprawny w FOD, zalezy od wymagan scenariusza.", 1)
        elif status_codes & {"6"} and _has_any_pattern(texts, CHARGING_NOT_OK_TEXT_PATTERNS):
            defect_candidate_statuses |= {"6"}
            defect_eligible = True
            mark("not_ok", "Scenariusz FOD wskazuje brak ladowania tam, gdzie test wymagal jego kontynuacji.", 2)
        elif status_codes & {"8", "9", "13", "14", "17"}:
            defect_candidate_statuses |= status_codes & {"8", "9", "13", "14", "17"}
            defect_eligible = True
            mark("not_ok", "Scenariusz FOD wszedl w blad systemowy albo przegrzanie.", 2)
        elif status_codes & profile["fod_pass_statuses"]:
            reasons.append("Status 4/6 jest akceptowany tylko wtedy, gdy odpowiada oczekiwanemu scenariuszowi FOD.")
        else:
            mark("warning", "Scenariusz FOD wymaga recznego porownania z oczekiwaniem dla obiektu i temperatury.", 1)

    elif scenario["code"] == "nfc":
        if _has_any_pattern(texts, NFC_NOT_OK_TEXT_PATTERNS):
            mark("not_ok", "Scenariusz NFC ma wynik NO OK / NOK / NOT OK.", 2)
        elif any(text == "ok" or text.endswith(" ok") for text in texts):
            reasons.append("Scenariusz NFC ma wynik OK.")
        else:
            mark("no_data", "Scenariusz NFC nie ma jednoznacznego wyniku OK / NO OK.", 1, force=True)

    else:
        if scenario["code"] == "unknown":
            if status_codes & CHARGING_FAILURE_STATUSES:
                defect_candidate_statuses |= status_codes & CHARGING_FAILURE_STATUSES
                defect_eligible = True
                mark("not_ok", "Status bez kontekstu scenariusza wyglada na problematyczny i wymaga weryfikacji.", 2)
            else:
                mark("warning", "Status jest silnie zalezny od scenariusza, ale sesja nie ma wystarczajacego kontekstu FOD/RFID.", 1)
        elif _charging_completed_normally(session, texts):
            reasons.append("Scenariusz Charging 0-100 konczy sie statusem 2 po stabilnym przebiegu statusu 3.")
        elif status_codes & CHARGING_FAILURE_STATUSES:
            defect_candidate_statuses |= status_codes & CHARGING_FAILURE_STATUSES
            defect_eligible = True
            mark(
                "not_ok",
                f"Scenariusz ladowania zawiera status odbiegajacy od oczekiwania: {_format_status_label(status_codes & CHARGING_FAILURE_STATUSES)}.",
                2,
            )
        elif _has_any_pattern(texts, CHARGING_NOT_OK_TEXT_PATTERNS):
            defect_candidate_statuses |= status_codes
            defect_eligible = True
            mark("not_ok", "Tekst sesji wskazuje no charging / toggling / charging interrupted.", 2)
        elif status_codes <= profile["charging_ok_statuses"]:
            reasons.append("Sesja utrzymuje oczekiwany status 3 dla ladowania.")
        elif status_codes == {"2"}:
            mark("warning", "Status 2 zalezy od SoC i celu testu, dlatego bez dodatkowego kontekstu nie jest jednoznaczny.", 1)
        elif status_codes <= {"2", "3"}:
            mark("warning", "Sesja zawiera tylko statusy 2/3, ale bez potwierdzenia scenariusza 0-100 nie da sie tego uznac automatycznie za OK.", 1)
        elif status_codes & {"5", "15", "16"}:
            defect_candidate_statuses |= status_codes & {"5", "15", "16"}
            mark("warning", "Status wymaga interpretacji zaleznej od typu testu i nie moze byc oceniony globalnie.", 1)
        else:
            mark("warning", "Sesja ma nietypowy przebieg, ale bez jednoznacznego wzorca defect/not ok.", 1)

    if session.get("interruptions"):
        defect_candidate_statuses |= status_codes
        mark(
            "not_ok" if verdict == "ok" else verdict,
            f"W sesji sa przerwy ({len(session['interruptions'])}), co w recznych analizach jest traktowane jako odchylenie.",
            max(severity_score, 2),
        )
    if session.get("drop_messages"):
        defect_candidate_statuses |= status_codes
        mark(
            "not_ok" if verdict == "ok" else verdict,
            "Wykryto skoki lub spadki mocy, czyli przebieg nie jest stabilny.",
            max(severity_score, 2),
        )
    if session.get("alarm_event_count"):
        defect_candidate_statuses |= status_codes
        mark(
            "not_ok" if verdict == "ok" else verdict,
            f"Sesja ma {session['alarm_event_count']} alarmowych zmian metryk, co wzmacnia kwalifikacje do sprawdzenia.",
            max(severity_score, 2),
        )
    if verdict == "not_ok":
        if has_setup_issue and not has_explicit_defect_reference:
            reasons.append("Problem moze wynikac z warunkow testu lub setupu, wiec nie przechodzi automatycznie w DEFECT.")
        elif has_explicit_defect_reference:
            mark("defect", f"Sesja ma przypisany Defect ID lub jawne odniesienie do defektu: {', '.join(defect_references)}.", 4)
        elif defect_eligible and _is_repeatable_problem(session, defect_candidate_statuses, texts):
            mark("defect", "Problem jest powtarzalny w tej samej kombinacji danych, wiec spelnia kryterium DEFECT.", 4)
        elif defect_eligible:
            reasons.append("Brak Defect ID albo potwierdzonej powtarzalnosci, dlatego wynik pozostaje NOT OK do weryfikacji.")

    if benchmark is not None:
        evidence.append({"label": "Workbooki", "value": str(len(benchmark.get("files", [])))})

    status_detail_label, scenario_detail_label, problem_timestamp = _build_detail_labels(
        session,
        scenario=scenario,
        verdict=verdict,
        profile=profile,
        status_codes=status_codes,
    )

    return {
        "verdict": verdict,
        "label": label,
        "badge_class": badge_class,
        "severity_score": severity_score,
        "reasons": reasons,
        "status_codes": _sort_status_codes(status_codes),
        "status_label": _format_status_label(status_codes),
        "status_detail_label": status_detail_label,
        "scenario_code": scenario["code"],
        "scenario_label": scenario["label"],
        "scenario_detail_label": scenario_detail_label,
        "scenario_confidence": scenario["confidence"],
        "rule_label": profile["label"],
        "focus_metrics": list(profile["focus_metrics"]),
        "evidence_rows": evidence,
        "problem_timestamp": problem_timestamp,
        "problem_timestamp_label": _format_problem_timestamp(problem_timestamp),
    }


def build_project_rule_overview(
    *,
    project_number: str,
    benchmark: dict[str, Any] | None,
    is_mixed_scope: bool = False,
) -> dict[str, Any]:
    profile = resolve_project_profile(project_number)
    notes = list(profile["notes"])
    source_files = benchmark.get("files", []) if benchmark is not None else []
    benchmark_scenarios = benchmark.get("scenario_rows", []) if benchmark is not None else []
    try:
        criteria_document_text = CRITERIA_DOCUMENT_PATH.read_text(encoding="utf-8")
    except OSError:
        criteria_document_text = ""

    if benchmark is None:
        notes.append("Brak workbooka referencyjnego dla biezacego projektu lub zakres obejmuje wiele projektow.")
    else:
        notes.append(
            f"Profil opiera sie na {len(source_files)} workbookach Examples i {len(benchmark_scenarios)} scenariuszach referencyjnych."
        )
        low_confidence = [
            row["scenario_label"]
            for row in benchmark_scenarios
            if "przyblizeniem" in row.get("reference_note", "").lower()
        ]
        if low_confidence:
            notes.append(
                f"Do doprecyzowania pozostaja scenariusze: {', '.join(low_confidence)}."
            )

    return {
        "project_number": project_number or "default",
        "title": "Reguly projektu" if not is_mixed_scope else "Reguly projektu w trybie mieszanym",
        "scope_subtitle": (
            "Werdykty sesji sa liczone per projekt_number, a ponizszy profil pokazuje aktualnie dominujace zasady."
            if is_mixed_scope
            else "Logika interpretacji oddzielona od glownego kodu i oparta o scenariusz, status i dane pomocnicze."
        ),
        "profile_label": profile["label"],
        "focus_metrics_label": ", ".join(profile["focus_metrics"]),
        "rule_rows": list(profile["rule_rows"]),
        "notes": notes,
        "source_files": source_files,
        "benchmark_available": benchmark is not None,
        "criteria_document_path": str(CRITERIA_DOCUMENT_PATH),
        "criteria_document_text": criteria_document_text,
    }
