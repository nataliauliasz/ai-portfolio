import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from math import ceil
from pathlib import Path
from statistics import median
from threading import Lock
from typing import Any, Iterator
from zipfile import ZipFile
from zoneinfo import ZoneInfo

import psycopg
from flask import Flask, jsonify, render_template, request, url_for

from rules.examples_reference import (
    extract_position_number as extract_examples_position_number,
    load_examples_benchmarks as load_examples_benchmarks_from_rules,
)
from rules.project_rules import (
    assess_session_against_project_rules,
    build_project_rule_overview,
    resolve_project_profile,
)


logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
DEBUG_RANKING_SQL_FILENAME = "debug_ranking_sql.sql"
DEBUG_RANKING_SQL_PATH = BASE_DIR / DEBUG_RANKING_SQL_FILENAME
RANKING_SESSION_MV_SQL_FILENAME = "003_create_charging_log_sessions_mv.sql"
RANKING_SESSION_TABLE_SQL_FILENAME = "004_create_charging_log_sessions_table.sql"

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 250
RAW_SOURCE_RELATION = "public.remote_csv_raw"
DEFAULT_SOURCE_RELATION = "public.charging_log_processed_mv"
SOURCE_RELATION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
FILTER_OPTIONS_CACHE_TTL_SECONDS = 300
ANALYSIS_CACHE_TTL_SECONDS = 300
ANALYSIS_POLL_INTERVAL_MS = 1000
ANALYSIS_TIME_BUDGET_SECONDS = 15.0
ANALYSIS_RETRY_LIMITS_FALLBACK = (80, 40, 20)
ANALYSIS_QUERY_TIMEOUT_MS = 6000
ANALYSIS_QUERY_TIMEOUT_RETRY_MS = 3500
ANALYSIS_FAST_GROUP_LIMIT_BASE = 16
ANALYSIS_FAST_GROUP_LIMIT_MAX = 96
ANALYSIS_FAST_CANDIDATE_ROW_MULTIPLIER = 4
ANALYSIS_EMERGENCY_RETRY_LIMITS = (12, 8, 4)
ANALYSIS_RECENT_ROWS_FALLBACK_LIMIT = 1200
ANALYSIS_GROUP_ROWS_LIMIT = 12
INSIGHT_FETCH_LIMIT = 5000
EXTENDED_ANALYSIS_FETCH_LIMIT = 20000
ANALYSIS_GROUP_LIMIT_BASE = 120
ANALYSIS_GROUP_LIMIT_MAX = 1800
SESSION_BREAK_SECONDS = 20 * 60
RANKING_SESSION_BREAK_SECONDS = 15 * 60
PREFERRED_CHARGING_STATUS = "3"
EXPECTED_FINAL_STATUS = "2"
POWER_DROP_RELATIVE_THRESHOLD = 0.2
MIN_RELEASE_SESSION_COUNT = 2
RELEASE_BASELINE_WINDOW = 3
RELEASE_ALARM_DROP_ABSOLUTE_THRESHOLD = 4.0
RELEASE_WARNING_DROP_ABSOLUTE_THRESHOLD = 2.0
RELEASE_WARNING_CONSECUTIVE_COUNT = 2
TIMESTAMP_SQL = {
    "event_ts": "event_ts",
    "inserted_at": "inserted_at",
}
SORTABLE_COLUMNS = {
    "raw_id": "raw_id",
    "event_ts": TIMESTAMP_SQL["event_ts"],
    "inserted_at": TIMESTAMP_SQL["inserted_at"],
    "device_name": "device_name",
    "charger_name": "charger_name",
}
SORT_DIRECTIONS = {"asc", "desc"}
AVAILABLE_TABS = {"analysis", "ranking"}
RANKING_MIN_SESSION_COUNT = 2
RANKING_TOP_LIMIT = 6
RANKING_SCORE_WEIGHTS = {
    "median_eff": 0.4,
    "median_rx": 0.2,
    "median_tx": 0.15,
    "clean_rate": 0.25,
}
RANKING_SNAPSHOT_TABLE = "public.charging_log_ranking_snapshot"
RANKING_SESSION_RELATION = "public.charging_log_sessions_mv"
RANKING_CLASSIFIED_TEMP_TABLE = "tmp_classified_ranking_sessions"
RANKING_SNAPSHOT_TIMEZONE = "Europe/Warsaw"
RANKING_SNAPSHOT_STREAM_BATCH_SIZE = 5000
RANKING_SNAPSHOT_MIN_PHONE_SESSIONS = 30
RANKING_SNAPSHOT_MIN_PROJECT_SESSIONS = 50
RANKING_SNAPSHOT_MIN_PROJECT_SOFTWARE_SESSIONS = 10
RANKING_SNAPSHOT_MIN_GLOBAL_SOFTWARE_SESSIONS = 20
RANKING_SNAPSHOT_MIN_STATUS_COVERAGE = 0.80
RANKING_SESSION_SOURCE_REQUIRED_COLUMNS = {
    "session_key",
    "phone",
    "project_number",
    "device_name",
    "charger_name",
    "position",
    "source_row_count",
    "software_version",
    "source_csv_file",
    "scenario_hint",
    "fod_object",
    "card_position",
    "sample_label",
    "manual_result",
    "defect_id",
    "defect_comment",
    "dual_charging_label",
    "start_ts",
    "end_ts",
    "avg_eff",
    "avg_rx",
    "avg_tx",
    "max_temperature",
    "status_codes",
    "first_status",
    "last_status",
    "interruption_count",
    "eff_drop_count",
    "rx_drop_count",
    "tx_drop_count",
}


@dataclass(frozen=True)
class RankingSessionSourceDescriptor:
    session_source_sql: str
    source_relation_label: str
    using_precomputed_relation: bool
    session_count: int | None = None
POSITION_ANALYSIS_EVENT_LIMIT = 80
POSITION_ANALYSIS_RELEASE_LIMIT = 48
POSITION_ANALYSIS_POSITION_LIMIT = 24
POSITION_ANALYSIS_SESSION_LIMIT = 12
POSITION_EVENT_WARNING_THRESHOLD = 0.10
POSITION_EVENT_ALARM_THRESHOLD = 0.20
PROBLEM_SPAN_MERGE_GAP_SECONDS = 90.0
METRIC_CHART_ALERT_THRESHOLD = 0.20
METRIC_ALERT_WARMUP_SECONDS = 180
METRIC_ALERT_MIN_PREVIOUS_VALUE = {
    "rx": 2.5,
    "tx": 2.5,
    "rpp": 0.3,
    "current_a": 0.08,
    "voltage_v": 3.5,
}
EXAMPLES_DIR = BASE_DIR / "Examples"
EXAMPLES_PROJECT_PATTERN = re.compile(r"^(?P<project>\d{4}_\d{3})_")
EXAMPLES_DEFECT_STATUS_CODES = {"8", "9", "13", "17"}
EXAMPLES_NOT_OK_STATUS_CODES = {"0", "1", "4", "5", "6", "7", "10", "11", "12", "14", "15", "16"}
EXAMPLES_SCENARIO_CONFIG = {
    "CHARGING 0 -100 %": {
        "code": "charging",
        "label": "Charging 0-100%",
        "position_start_index": 6,
    },
    "NFC": {
        "code": "nfc",
        "label": "NFC",
        "position_start_index": 6,
    },
    "FOD": {
        "code": "fod",
        "label": "FOD",
        "position_start_index": 9,
    },
    "WLC": {
        "code": "wlc",
        "label": "WLC",
        "position_start_index": 7,
    },
    "RFID Protection": {
        "code": "rfid_primary",
        "label": "RFID Protection",
        "position_start_index": 6,
    },
    "RFID Protection_2": {
        "code": "rfid_secondary",
        "label": "RFID Protection 2",
        "position_start_index": 6,
    },
}
EXAMPLES_RULE_ROWS = (
    {
        "scenario_label": "Charging / WLC / Start Charging",
        "pass_rule": "OK: status 3, a dla 0-100 finalnie dopuszczalny status 2.",
        "not_ok_rule": "NOT OK: NO CHARGING, toggling oraz statusy odbiegajace od oczekiwanego ladowania.",
        "defect_rule": "DEFECT: tylko po potwierdzeniu, powtarzalnosci albo przy przypisanym Defect ID.",
    },
    {
        "scenario_label": "NFC",
        "pass_rule": "OK: bezposrednio wpisane OK.",
        "not_ok_rule": "NOT OK: NO OK / NOT OK / NOK.",
        "defect_rule": "DEFECT: tylko po potwierdzeniu albo przy Defect ID / opisie defektu.",
    },
    {
        "scenario_label": "FOD",
        "pass_rule": "OK: zachowanie zgodne z oczekiwaniem dla obiektu FOD, temperatury i pozycji; czesto 4 lub 6.",
        "not_ok_rule": "NOT OK: NO FOD DETECTED, status 3/5 przy oczekiwanej blokadzie albo nieprawidlowa temperatura.",
        "defect_rule": "DEFECT: dopiero po potwierdzeniu, powtarzalnosci albo przy przypisanym Defect ID.",
    },
    {
        "scenario_label": "RFID Protection",
        "pass_rule": "OK: status 15 albo 16, jesli ochrona RFID/NFC zadzialala zgodnie z logika testu.",
        "not_ok_rule": "NOT OK: brak 15/16 albo pojawienie sie innego statusu zamiast ochrony.",
        "defect_rule": "DEFECT: dopiero po potwierdzeniu, powtarzalnosci albo przy przypisanym Defect ID.",
    },
)
EXAMPLES_WORKBOOK_CACHE_LOCK = Lock()
EXAMPLES_WORKBOOK_CACHE: dict[str, Any] = {
    "value": None,
}
EXTRA_METRIC_CANDIDATES = {
    "rpp": ("rpp", "received_power", "rx_power", "power_rx"),
    "current_a": ("current (a)", "current_a", "coil_current", "rx_current"),
    "temperature": ("temperature", "temperature_c", "temp", "temp_c"),
    "battery_level": ("battery_level", "battery_percent", "battery_pct", "soc", "soc_percent"),
    "voltage_v": ("voltage", "voltage (v)", "voltage_v", "battery_voltage", "vbat"),
}
EXTRA_TEXT_CANDIDATES = {
    "scenario_hint": (
        "scenario",
        "test_scenario",
        "test scenario",
        "sheet_name",
        "sheet",
        "worksheet",
        "tab_name",
        "test_type",
        "test type",
    ),
    "fod_object": (
        "fod object",
        "fod_object",
        "foreign object",
        "foreign_object",
        "foreign item",
        "fod item",
    ),
    "card_position": (
        "card position",
        "card_position",
        "rfid card position",
        "nfc card position",
        "card location",
        "card_location",
    ),
    "sample_label": (
        "sample",
        "sample id",
        "sample_id",
        "prototype",
        "device sample",
    ),
    "manual_result": (
        "result",
        "manual result",
        "manual_result",
        "test result",
        "verdict",
    ),
    "defect_id": (
        "defect id",
        "defect_id",
        "defect",
        "bug id",
        "jira",
    ),
    "defect_comment": (
        "defect comment",
        "defect_comment",
        "defect description",
        "comment",
        "remarks",
        "note",
    ),
    "dual_charging_label": (
        "dual charging",
        "dual_charging",
        "dual charger",
        "dual",
    ),
}
POSITION_EVENT_METRIC_CONFIG = (
    {
        "key": "eff",
        "label": "eff",
        "unit": "%",
        "absolute_warning": 5.0,
        "absolute_alarm": 12.0,
    },
    {
        "key": "rx",
        "label": "RX",
        "unit": "",
        "absolute_warning": 0.4,
        "absolute_alarm": 0.8,
    },
    {
        "key": "tx",
        "label": "TX",
        "unit": "",
        "absolute_warning": 0.4,
        "absolute_alarm": 0.8,
    },
    {
        "key": "rpp",
        "label": "RPP",
        "unit": "",
        "absolute_warning": 0.2,
        "absolute_alarm": 0.4,
    },
    {
        "key": "current_a",
        "label": "Current",
        "unit": " A",
        "absolute_warning": 0.03,
        "absolute_alarm": 0.08,
    },
    {
        "key": "voltage_v",
        "label": "Voltage",
        "unit": " V",
        "absolute_warning": 0.15,
        "absolute_alarm": 0.35,
    },
)
FUTURE_DECISION_CRITERIA = [
    {
        "title": "Autonomia baterii",
        "summary": (
            "Poza sama sprawnoscia ladowania warto miec osobny ranking czasu pracy na "
            "baterii i zachowania telefonu w typowych scenariuszach."
        ),
        "details": "Dobry kierunek to osobne pomiary: light / moderate / heavy use oraz quick boost.",
        "source_label": "DXOMARK Battery",
        "source_url": "https://www.dxomark.com/a-closer-look-at-how-dxomark-tests-the-smartphone-battery-experience/",
    },
    {
        "title": "Jakosc aparatu",
        "summary": (
            "Do przyszlych decyzji produktowych warto porownywac nie tylko aparat ogolnie, "
            "ale jego skladowe: ekspozycje, kolor, autofocus, texture, noise i stabilizacje."
        ),
        "details": "To pozwala rozdzielic telefon dobry w dzien od telefonu stabilnego w wielu warunkach.",
        "source_label": "DXOMARK Camera V5",
        "source_url": "https://www.dxomark.com/smartphone-camera-image-quality-test-protocol-a-closer-look/",
    },
    {
        "title": "Jakosc ekranu",
        "summary": (
            "Sama rozdzielczosc i odswiezanie nie wystarczaja. Warto miec ranking czytelnosci, "
            "koloru, motion, touch i artifacts."
        ),
        "details": "To szczegolnie przydatne, gdy telefon ma sluzyc do pracy w terenie albo multimedia.",
        "source_label": "DXOMARK Display",
        "source_url": "https://corp.dxomark.com/news/dxomark-introduces-new-score-for-smartphone-display-and-expands-smartphone-rear-camera-testing/",
    },
    {
        "title": "Wydajnosc pod obciazeniem",
        "summary": (
            "Do decyzji zakupowych i projektowych liczy sie nie tylko peak performance, "
            "ale tez throttling i temperatura przy dluzszym obciazeniu."
        ),
        "details": "To osobny ranking wartosciowy dla gaming, AI i dluzszych testow ciaglych.",
        "source_label": "Notebookcheck smartphone ranking",
        "source_url": "https://www.notebookcheck.net/Ranking-Best-smartphones-reviewed-by-Notebookcheck.101858.0.html",
    },
    {
        "title": "Dlugosc wsparcia software",
        "summary": (
            "Wsparcie aktualizacyjne ma realny wplyw na bezpieczenstwo i dlugosc zycia urzadzenia, "
            "wiec warto miec osobny ranking lat wsparcia."
        ),
        "details": "Jako punkt odniesienia Google deklaruje do 7 lat aktualizacji dla Pixel 8 i nowszych.",
        "source_label": "Google Pixel update policy",
        "source_url": "https://support.google.com/pixelphone/answer/4457705?hl=en",
    },
]
STATUS_DETAILS = {
    "0": {
        "label": "status 0",
        "description": "Over/under voltage",
        "color": "#f1d5c4",
        "text_color": "#5d3720",
    },
    "1": {
        "label": "status 1",
        "description": "Surface empty / IDLE",
        "color": "#ef3d32",
        "text_color": "#fff5f2",
    },
    "2": {
        "label": "status 2",
        "description": "Phone fully charged",
        "color": "#2e7d32",
        "text_color": "#f5fff0",
    },
    "3": {
        "label": "status 3",
        "description": "Phone charging",
        "color": "#8ddf47",
        "text_color": "#17340d",
    },
    "4": {
        "label": "status 4",
        "description": "Foreign object detected (No Qi communication)",
        "color": "#ffca2c",
        "text_color": "#4c3600",
    },
    "5": {
        "label": "status 5",
        "description": "FOD and lower efficiency",
        "color": "#49a7e8",
        "text_color": "#f4fbff",
    },
    "6": {
        "label": "status 6",
        "description": "Foreign object and cell phone detected, no charging",
        "color": "#f4f04a",
        "text_color": "#403900",
    },
    "7": {
        "label": "status 7",
        "description": "Charging interrupted by key search",
        "color": "#f5eee9",
        "text_color": "#4d3728",
    },
    "8": {
        "label": "status 8",
        "description": "WCM overtemperature",
        "color": "#7a0d0d",
        "text_color": "#fff4f1",
    },
    "9": {
        "label": "status 9",
        "description": "Control unit defective / error",
        "color": "#e6d7ca",
        "text_color": "#473126",
    },
    "10": {
        "label": "status 10",
        "description": "WCM disabled",
        "color": "#f0dfd3",
        "text_color": "#4c3425",
    },
    "11": {
        "label": "status 11",
        "description": "Charging interrupt due to energy availability",
        "color": "#ead7ca",
        "text_color": "#4b3526",
    },
    "12": {
        "label": "status 12",
        "description": "Charging with low power due to energy availability drop",
        "color": "#ecd9cd",
        "text_color": "#4e3524",
    },
    "13": {
        "label": "status 13",
        "description": "Internal error from Qi device",
        "color": "#7e8791",
        "text_color": "#f5f8fb",
    },
    "14": {
        "label": "status 14",
        "description": "Unknown error",
        "color": "#efe0d5",
        "text_color": "#4b3425",
    },
    "15": {
        "label": "status 15",
        "description": "Not active WCA NFC card - RFID protection",
        "color": "#efdfd4",
        "text_color": "#4d3425",
    },
    "16": {
        "label": "status 16",
        "description": "NFC not active - two or more NFC devices detected",
        "color": "#efdfd4",
        "text_color": "#4d3425",
    },
    "17": {
        "label": "status 17",
        "description": "WCA not active safe state involving FusI or ISO26262",
        "color": "#ead8cd",
        "text_color": "#4d3425",
    },
    "N/A": {
        "label": "N/A",
        "description": "Brak statusu w row_json",
        "color": "#cbd5df",
        "text_color": "#253746",
    },
}
METRIC_SERIES_CONFIG = (
    {
        "key": "eff",
        "label": "eff",
        "color": "#111111",
        "digits": 1,
        "suffix": "%",
    },
    {
        "key": "rx",
        "label": "rx",
        "color": "#1193ea",
        "digits": 1,
        "suffix": "",
    },
    {
        "key": "tx",
        "label": "tx",
        "color": "#ff7c2a",
        "digits": 1,
        "suffix": "",
    },
    {
        "key": "rpp",
        "label": "rpp",
        "color": "#13866f",
        "digits": 2,
        "suffix": "",
    },
    {
        "key": "current_a",
        "label": "current",
        "color": "#8f54c8",
        "digits": 3,
        "suffix": " A",
    },
    {
        "key": "temperature",
        "label": "temp",
        "color": "#df4e2f",
        "digits": 1,
        "suffix": " C",
    },
    {
        "key": "battery_level",
        "label": "battery",
        "color": "#6f7782",
        "digits": 1,
        "suffix": "%",
    },
    {
        "key": "voltage_v",
        "label": "voltage",
        "color": "#cc9b00",
        "digits": 2,
        "suffix": " V",
    },
)
POSITION_STYLE_SWATCHES = (
    {
        "background_color": "rgba(186, 235, 223, 0.28)",
        "border_color": "rgba(78, 151, 130, 0.28)",
        "label_color": "#38685a",
    },
    {
        "background_color": "rgba(255, 236, 186, 0.30)",
        "border_color": "rgba(189, 146, 56, 0.26)",
        "label_color": "#7a5a14",
    },
    {
        "background_color": "rgba(205, 228, 252, 0.28)",
        "border_color": "rgba(78, 121, 177, 0.24)",
        "label_color": "#34557a",
    },
    {
        "background_color": "rgba(235, 222, 248, 0.26)",
        "border_color": "rgba(130, 102, 168, 0.22)",
        "label_color": "#644888",
    },
    {
        "background_color": "rgba(222, 239, 219, 0.28)",
        "border_color": "rgba(88, 139, 90, 0.24)",
        "label_color": "#456a47",
    },
)
SELECT_COLUMNS_SQL = f"""
    raw_id,
    device_name,
    source_csv_file,
    source_seq,
    event_ts,
    charger_name,
    phone,
    position,
    project_number,
    software_version,
    scenario_hint,
    fod_object,
    card_position,
    sample_label,
    manual_result,
    defect_id,
    defect_comment,
    dual_charging_label,
    dual_charging_flag,
    row_json,
    inserted_at,
    eff,
    rx,
    tx,
    rpp,
    current_a,
    temperature,
    battery_level,
    voltage_v,
    ingest_delay_seconds
"""
ANALYSIS_COLUMNS_SQL = """
    raw_id,
    device_name,
    source_csv_file,
    event_ts,
    charger_name,
    phone,
    position,
    project_number,
    software_version,
    scenario_hint,
    fod_object,
    card_position,
    sample_label,
    manual_result,
    defect_id,
    defect_comment,
    dual_charging_label,
    dual_charging_flag,
    row_json,
    hmi_status,
    eff,
    rx,
    tx,
    rpp,
    current_a,
    temperature,
    battery_level,
    voltage_v,
    ingest_delay_seconds
"""
ANALYSIS_FAST_REPORT_COLUMNS_SQL = """
    raw_id,
    device_name,
    source_csv_file,
    source_seq,
    event_ts,
    charger_name,
    phone,
    position,
    project_number,
    software_version,
    scenario_hint,
    fod_object,
    card_position,
    sample_label,
    manual_result,
    defect_id,
    defect_comment,
    dual_charging_label,
    dual_charging_flag,
    hmi_status,
    inserted_at,
    temperature,
    battery_level,
    ingest_delay_seconds
"""
ANALYSIS_GROUP_COLUMNS = (
    "phone",
    "project_number",
    "device_name",
    "charger_name",
    "position",
    "software_version",
    "source_csv_file",
)
ANALYSIS_SESSION_FETCH_COLUMNS = (
    "phone",
    "project_number",
    "device_name",
    "charger_name",
    "position",
    "source_csv_file",
)
FILTER_OPTIONS_CACHE_LOCK = Lock()
FILTER_OPTIONS_CACHE: dict[str, Any] = {
    "value": None,
    "expires_at": 0.0,
}
ANALYSIS_CACHE_LOCK = Lock()
ANALYSIS_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}
ANALYSIS_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="analysis")
SOURCE_SQL_CACHE_LOCK = Lock()
SOURCE_SQL_CACHE: dict[str, tuple[str, tuple[str, ...], bool]] = {}
SOURCE_COLUMN_TYPES = {
    "raw_id": "bigint",
    "device_name": "text",
    "source_csv_file": "text",
    "source_seq": "bigint",
    "event_ts": "timestamp",
    "charger_name": "text",
    "phone": "text",
    "position": "text",
    "project_number": "text",
    "software_version": "text",
    "scenario_hint": "text",
    "fod_object": "text",
    "card_position": "text",
    "sample_label": "text",
    "manual_result": "text",
    "defect_id": "text",
    "defect_comment": "text",
    "dual_charging_label": "text",
    "dual_charging_flag": "boolean",
    "row_json": "text",
    "hmi_status": "text",
    "inserted_at": "timestamp",
    "eff": "double precision",
    "rx": "double precision",
    "tx": "double precision",
    "rpp": "double precision",
    "current_a": "double precision",
    "temperature": "double precision",
    "battery_level": "double precision",
    "voltage_v": "double precision",
    "ingest_delay_seconds": "double precision",
    "analysis_candidate": "boolean",
}
FULLY_PREPARED_SOURCE_COLUMNS = frozenset(SOURCE_COLUMN_TYPES)
ENV_FILE_CANDIDATES = (
    ".env",
    ".env.local",
    "db.env",
    "db.local.env",
)


def read_source_relation() -> str:
    value = os.environ.get("DB_SOURCE_RELATION", DEFAULT_SOURCE_RELATION).strip() or DEFAULT_SOURCE_RELATION
    if not SOURCE_RELATION_PATTERN.fullmatch(value):
        raise RuntimeError(
            "DB_SOURCE_RELATION ma nieprawidlowy format. Uzyj nazwy w postaci schema.tabela "
            "albo samej tabeli."
        )
    return value


def load_local_env_files() -> None:
    for filename in ENV_FILE_CANDIDATES:
        env_path = BASE_DIR / filename
        if not env_path.is_file():
            continue
        try:
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                normalized_key = key.strip()
                if not normalized_key or normalized_key in os.environ:
                    continue
                normalized_value = value.strip()
                if (
                    len(normalized_value) >= 2
                    and normalized_value[0] == normalized_value[-1]
                    and normalized_value[0] in {"'", '"'}
                ):
                    normalized_value = normalized_value[1:-1]
                os.environ[normalized_key] = normalized_value
        except OSError:
            LOGGER.exception("Failed to read local env file %s", env_path)


load_local_env_files()


def build_source_from_sql(relation_name: str) -> str:
    if relation_name == RAW_SOURCE_RELATION:
        return """
        (
            select
                raw.raw_id,
                raw.device_name,
                raw.source_csv_file,
                raw.source_seq,
                nullif(raw.event_ts::text, '')::timestamp as event_ts,
                raw.charger_name,
                raw.phone,
                raw.position,
                raw.project_number,
                raw.software_version,
                context.scenario_hint,
                context.fod_object,
                context.card_position,
                context.sample_label,
                context.manual_result,
                context.defect_id,
                context.defect_comment,
                context.dual_charging_label,
                context.dual_charging_flag,
                raw.row_json,
                metrics.hmi_status,
                nullif(raw.inserted_at::text, '')::timestamp as inserted_at,
                metrics.eff,
                metrics.rx,
                metrics.tx,
                metrics.rpp,
                metrics.current_a,
                metrics.temperature,
                metrics.battery_level,
                metrics.voltage_v,
                case
                    when nullif(raw.event_ts::text, '')::timestamp is null
                        or nullif(raw.inserted_at::text, '')::timestamp is null then null
                    else extract(
                        epoch from (
                            nullif(raw.inserted_at::text, '')::timestamp
                            - nullif(raw.event_ts::text, '')::timestamp
                        )
                    )
                end as ingest_delay_seconds,
                flags.analysis_candidate
            from public.remote_csv_raw raw
            cross join lateral (
                select public.charging_log_try_parse_jsonb(raw.row_json::text) as payload
            ) parsed
            cross join lateral (
                select
                    public.charging_log_normalize_status(
                        public.charging_log_extract_text(parsed.payload, array['hmi_status'])
                    ) as hmi_status,
                    public.charging_log_extract_efficiency(parsed.payload) as eff,
                    public.charging_log_extract_metric(parsed.payload, array['rx']) as rx,
                    public.charging_log_extract_metric(parsed.payload, array['tx']) as tx,
                    public.charging_log_extract_metric(parsed.payload, array['rpp']) as rpp,
                    public.charging_log_extract_metric(parsed.payload, array['current (a)', 'current_a', 'coil_current', 'rx_current']) as current_a,
                    public.charging_log_extract_metric(parsed.payload, array['temperature', 'temperature_c', 'temp', 'temp_c']) as temperature,
                    public.charging_log_extract_metric(parsed.payload, array['battery_level', 'battery_percent', 'battery_pct', 'soc', 'soc_percent']) as battery_level,
                    public.charging_log_extract_metric(parsed.payload, array['voltage', 'voltage (v)', 'voltage_v', 'battery_voltage', 'vbat']) as voltage_v
            ) metrics
            cross join lateral (
                select
                    public.charging_log_extract_text(parsed.payload, array['scenario', 'test_scenario', 'test scenario', 'sheet_name', 'sheet', 'worksheet', 'tab_name', 'test_type', 'test type']) as scenario_hint,
                    public.charging_log_extract_text(parsed.payload, array['fod object', 'fod_object', 'foreign object', 'foreign_object', 'foreign item', 'fod item']) as fod_object,
                    public.charging_log_extract_text(parsed.payload, array['card position', 'card_position', 'rfid card position', 'nfc card position', 'card location', 'card_location']) as card_position,
                    public.charging_log_extract_text(parsed.payload, array['sample', 'sample id', 'sample_id', 'prototype', 'device sample']) as sample_label,
                    public.charging_log_extract_text(parsed.payload, array['result', 'manual result', 'manual_result', 'test result', 'verdict']) as manual_result,
                    public.charging_log_extract_text(parsed.payload, array['defect id', 'defect_id', 'defect', 'bug id', 'jira']) as defect_id,
                    public.charging_log_extract_text(parsed.payload, array['defect comment', 'defect_comment', 'defect description', 'comment', 'remarks', 'note']) as defect_comment,
                    public.charging_log_extract_text(parsed.payload, array['dual charging', 'dual_charging', 'dual charger', 'dual']) as dual_charging_label,
                    public.charging_log_try_parse_bool(
                        public.charging_log_extract_text(parsed.payload, array['dual charging', 'dual_charging', 'dual charger', 'dual'])
                    ) as dual_charging_flag
            ) context
            cross join lateral (
                select
                    case
                        when nullif(btrim(context.defect_id), '') is not null then true
                        when lower(coalesce(context.manual_result, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then true
                        when lower(coalesce(context.defect_comment, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)' then true
                        when metrics.hmi_status in ('0', '1', '4', '5', '6', '8', '9', '13', '14', '17') then true
                        when metrics.hmi_status = '3' and (
                            nullif(btrim(context.card_position), '') is not null
                            or nullif(btrim(context.fod_object), '') is not null
                            or lower(coalesce(context.scenario_hint, '')) like '%%rfid%%'
                            or lower(coalesce(context.scenario_hint, '')) like '%%fod%%'
                        ) then true
                        when metrics.hmi_status in ('15', '16') and (
                            nullif(btrim(context.defect_id), '') is not null
                            or lower(coalesce(context.manual_result, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)'
                            or lower(coalesce(context.defect_comment, '')) ~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure)(?:[^a-z]|$)'
                            or (
                                nullif(btrim(context.card_position), '') is null
                                and lower(coalesce(context.scenario_hint, '')) not like '%%rfid%%'
                            )
                        ) then true
                        else false
                    end as analysis_candidate
            ) flags
        ) source_data
        """
    return f"{relation_name} source_data"


SOURCE_RELATION = read_source_relation()
SOURCE_FROM_SQL = build_source_from_sql(SOURCE_RELATION)


def split_relation_name(relation_name: str) -> tuple[str | None, str]:
    if "." in relation_name:
        schema_name, table_name = relation_name.split(".", 1)
        return schema_name, table_name
    return None, relation_name


def fetch_relation_columns(conn: psycopg.Connection, relation_name: str) -> set[str]:
    schema_name, table_name = split_relation_name(relation_name)
    with conn.cursor() as cur:
        if schema_name is not None:
            cur.execute(
                """
                select a.attname
                from pg_catalog.pg_attribute a
                join pg_catalog.pg_class c on c.oid = a.attrelid
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                where n.nspname = %s
                  and c.relname = %s
                  and a.attnum > 0
                  and not a.attisdropped
                """,
                [schema_name, table_name],
            )
        else:
            cur.execute(
                """
                select a.attname
                from pg_catalog.pg_attribute a
                join pg_catalog.pg_class c on c.oid = a.attrelid
                where c.oid = to_regclass(%s)
                  and a.attnum > 0
                  and not a.attisdropped
                """,
                [relation_name],
            )
        return {str(row[0]) for row in cur.fetchall()}


def relation_exists(conn: psycopg.Connection, relation_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("select to_regclass(%s)", [relation_name])
        return cur.fetchone()[0] is not None


def fetch_relation_row_count(conn: psycopg.Connection, relation_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"select count(*) from {relation_name}")
        return int(cur.fetchone()[0] or 0)


def build_ranking_session_source_setup_message(
    *,
    missing_columns: list[str] | None = None,
) -> str:
    detail = (
        f"{RANKING_SESSION_RELATION} is missing required columns: {', '.join(missing_columns)}. "
        if missing_columns
        else f"{RANKING_SESSION_RELATION} does not exist. "
    )
    return (
        "Nightly ranking requires the precomputed session source "
        f"{RANKING_SESSION_RELATION}. "
        f"{detail}"
        f"Create the materialized view with sql/{RANKING_SESSION_MV_SQL_FILENAME}, "
        f"or if CREATE MATERIALIZED VIEW is not allowed use sql/{RANKING_SESSION_TABLE_SQL_FILENAME}. "
        "Dynamic session fallback is disabled by default because it scans the full event source. "
        "If you intentionally want the slow emergency path, rerun the generator with "
        "--allow-dynamic-session-fallback."
    )


def build_analysis_candidate_sql(alias: str, available_columns: set[str]) -> str:
    def has_column(column_name: str) -> bool:
        return column_name in available_columns

    def column_sql(column_name: str, sql_type: str = "text") -> str:
        if has_column(column_name):
            return f"{alias}.{column_name}"
        return f"null::{sql_type}"

    manual_result_sql = column_sql("manual_result")
    defect_id_sql = column_sql("defect_id")
    defect_comment_sql = column_sql("defect_comment")
    hmi_status_sql = column_sql("hmi_status")
    card_position_sql = column_sql("card_position")
    fod_object_sql = column_sql("fod_object")

    scenario_parts: list[str] = []
    for column_name, sql_type in (
        ("scenario_hint", "text"),
        ("sample_label", "text"),
        ("source_csv_file", "text"),
        ("row_json", "text"),
    ):
        if has_column(column_name):
            scenario_parts.append(f"coalesce({column_sql(column_name, sql_type)}::text, '')")
    scenario_blob_sql = (
        f"lower(concat_ws(' ', {', '.join(scenario_parts)}))" if scenario_parts else "''"
    )

    manual_not_ok_sql = (
        f"lower(coalesce({manual_result_sql}, '')) "
        "~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)'"
    )
    defect_comment_match_sql = (
        f"lower(coalesce({defect_comment_sql}, '')) "
        "~ '(?:^|[^a-z])(not ok|no ok|nok|defect|failure|charging inter|no charging|toggling)(?:[^a-z]|$)'"
    )
    explicit_defect_sql = f"nullif(btrim({defect_id_sql}), '') is not null"
    has_card_position_sql = f"nullif(btrim({card_position_sql}), '') is not null"
    has_fod_object_sql = f"nullif(btrim({fod_object_sql}), '') is not null"
    rfid_scenario_sql = f"{scenario_blob_sql} like '%%rfid%%'"
    fod_scenario_sql = (
        f"{scenario_blob_sql} like '%%fod%%' or {scenario_blob_sql} like '%%foreign object%%'"
    )
    non_rfid_scenario_sql = f"{scenario_blob_sql} not like '%%rfid%%'"

    fallback_sql = f"""
    case
        when {explicit_defect_sql} then true
        when {manual_not_ok_sql} then true
        when {defect_comment_match_sql} then true
        when {hmi_status_sql} in ('0', '1', '4', '5', '6', '8', '9', '13', '14', '17') then true
        when {hmi_status_sql} = '3' and (
            {has_card_position_sql}
            or {has_fod_object_sql}
            or {rfid_scenario_sql}
            or {fod_scenario_sql}
        ) then true
        when {hmi_status_sql} in ('15', '16') and (
            {non_rfid_scenario_sql}
            or {manual_not_ok_sql}
            or {explicit_defect_sql}
            or {defect_comment_match_sql}
        ) then true
        else false
    end
    """
    if has_column("analysis_candidate"):
        return f"coalesce({alias}.analysis_candidate, ({fallback_sql.strip()}))"
    return fallback_sql.strip()


def build_compat_source_from_sql(relation_name: str, available_columns: set[str]) -> str:
    def base_column(name: str) -> str:
        sql_type = SOURCE_COLUMN_TYPES[name]
        if name in available_columns:
            return f"base.{name} as {name}"
        return f"null::{sql_type} as {name}"

    return f"""
    (
        select
            source.raw_id,
            source.device_name,
            source.source_csv_file,
            source.source_seq,
            source.event_ts,
            source.charger_name,
            source.phone,
            source.position,
            source.project_number,
            source.software_version,
            source.scenario_hint,
            source.fod_object,
            source.card_position,
            source.sample_label,
            source.manual_result,
            source.defect_id,
            source.defect_comment,
            source.dual_charging_label,
            source.dual_charging_flag,
            source.row_json,
            source.hmi_status,
            source.inserted_at,
            source.eff,
            source.rx,
            source.tx,
            source.rpp,
            source.current_a,
            source.temperature,
            source.battery_level,
            source.voltage_v,
            case
                when source.ingest_delay_seconds is not null then source.ingest_delay_seconds
                when source.event_ts is null or source.inserted_at is null then null
                else extract(epoch from (source.inserted_at - source.event_ts))
            end as ingest_delay_seconds,
            {build_analysis_candidate_sql("source", available_columns)} as analysis_candidate
        from (
            select
                {", ".join(base_column(name) for name in SOURCE_COLUMN_TYPES)}
            from {relation_name} base
        ) source
    ) source_data
    """


def get_source_descriptor(conn: psycopg.Connection) -> tuple[str, bool]:
    if SOURCE_RELATION == RAW_SOURCE_RELATION:
        return SOURCE_FROM_SQL, False

    with SOURCE_SQL_CACHE_LOCK:
        cached = SOURCE_SQL_CACHE.get(SOURCE_RELATION)
        if cached is not None:
            cached_sql, cached_columns, cached_is_prepared = cached
            if set(cached_columns) == FULLY_PREPARED_SOURCE_COLUMNS or cached_sql:
                return cached_sql, cached_is_prepared

    available_columns = fetch_relation_columns(conn, SOURCE_RELATION)
    is_prepared = FULLY_PREPARED_SOURCE_COLUMNS.issubset(available_columns)
    if is_prepared:
        source_sql = f"{SOURCE_RELATION} source_data"
    else:
        source_sql = build_compat_source_from_sql(SOURCE_RELATION, available_columns)

    with SOURCE_SQL_CACHE_LOCK:
        SOURCE_SQL_CACHE[SOURCE_RELATION] = (source_sql, tuple(sorted(available_columns)), is_prepared)
    return source_sql, is_prepared


def get_source_from_sql(conn: psycopg.Connection) -> str:
    source_sql, _ = get_source_descriptor(conn)
    return source_sql


def build_lightweight_stream_source_from_sql(
    relation_name: str,
    available_columns: set[str],
    required_columns: tuple[str, ...],
) -> str:
    select_columns: list[str] = []
    for column_name in required_columns:
        if column_name == "event_ts":
            if column_name in available_columns:
                select_columns.append("nullif(base.event_ts::text, '')::timestamp as event_ts")
            else:
                select_columns.append("null::timestamp as event_ts")
            continue
        if column_name == "inserted_at":
            if column_name in available_columns:
                select_columns.append("nullif(base.inserted_at::text, '')::timestamp as inserted_at")
            else:
                select_columns.append("null::timestamp as inserted_at")
            continue

        sql_type = SOURCE_COLUMN_TYPES.get(column_name, "text")
        if column_name in available_columns:
            select_columns.append(f"base.{column_name} as {column_name}")
        else:
            select_columns.append(f"null::{sql_type} as {column_name}")

    return f"""
    (
        select
            {", ".join(select_columns)}
        from {relation_name} base
    ) source_data
    """


@dataclass(frozen=True)
class DbConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str


@dataclass(frozen=True)
class FilterScope:
    search_text: str
    phone: str
    project_number: str
    software_version: str
    position: str
    classification: str
    test_type: str
    sample: str
    defect_id: str
    dual_charging: str
    event_ts_from: date | None
    event_ts_to: date | None
    inserted_at_from: date | None
    inserted_at_to: date | None

    def cache_key(self) -> tuple[Any, ...]:
        return (
            SOURCE_RELATION,
            resolve_analysis_candidate_group_limit(self),
            self.search_text.lower(),
            self.phone,
            self.project_number,
            self.software_version,
            self.position,
            self.classification,
            self.test_type,
            self.sample,
            self.defect_id.lower(),
            self.dual_charging,
            self.event_ts_from.isoformat() if self.event_ts_from is not None else "",
            self.event_ts_to.isoformat() if self.event_ts_to is not None else "",
            self.inserted_at_from.isoformat() if self.inserted_at_from is not None else "",
            self.inserted_at_to.isoformat() if self.inserted_at_to is not None else "",
        )


def resolve_analysis_candidate_group_limit(scope: FilterScope) -> int:
    limit = ANALYSIS_GROUP_LIMIT_BASE
    if scope.search_text:
        limit += 120
    if scope.project_number:
        limit += 180
    if scope.software_version:
        limit += 220
    if scope.phone:
        limit += 220
    if scope.position:
        limit += 260
    if scope.sample:
        limit += 320
    if scope.defect_id:
        limit += 480
    if scope.event_ts_from is not None or scope.event_ts_to is not None:
        limit += 140
    return min(limit, ANALYSIS_GROUP_LIMIT_MAX)


def resolve_fast_analysis_candidate_group_limit(scope: FilterScope) -> int:
    limit = ANALYSIS_FAST_GROUP_LIMIT_BASE
    if scope.project_number:
        limit += 40
    if scope.software_version:
        limit += 52
    if scope.phone:
        limit += 52
    if scope.position:
        limit += 28
    if scope.sample:
        limit += 32
    if scope.defect_id:
        limit += 36
    if scope.search_text:
        limit += 24
    if scope.event_ts_from is not None or scope.event_ts_to is not None:
        limit += 20
    return min(limit, ANALYSIS_FAST_GROUP_LIMIT_MAX)


def read_required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_db_config() -> DbConfig:
    return DbConfig(
        host=read_required_env("DB_HOST"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=read_required_env("DB_NAME"),
        user=read_required_env("DB_USER"),
        password=read_required_env("DB_PASSWORD"),
        sslmode=os.environ.get("DB_SSLMODE", "require").strip() or "require",
    )


def build_db_connection_summary() -> str:
    host = os.environ.get("DB_HOST", "").strip() or "brak DB_HOST"
    port = os.environ.get("DB_PORT", "").strip() or "5432"
    dbname = os.environ.get("DB_NAME", "").strip() or "brak DB_NAME"
    user = os.environ.get("DB_USER", "").strip() or "brak DB_USER"
    sslmode = os.environ.get("DB_SSLMODE", "").strip() or "require"
    return (
        f"Host: {host}, port: {port}, baza: {dbname}, user: {user}, "
        f"sslmode: {sslmode}, relacja: {SOURCE_RELATION}."
    )


def sanitize_db_exception_message(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    if not message:
        return ""
    password = os.environ.get("DB_PASSWORD", "").strip()
    if password:
        message = message.replace(password, "***")
    return message


def create_connection() -> psycopg.Connection:
    config = load_db_config()
    return psycopg.connect(
        host=config.host,
        port=config.port,
        dbname=config.dbname,
        user=config.user,
        password=config.password,
        sslmode=config.sslmode,
        connect_timeout=10,
    )


def parse_positive_int(value: str | None, default: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default

    if parsed < 1:
        return default
    if maximum is not None:
        return min(parsed, maximum)
    return parsed


def parse_optional_date(value: str | None) -> date | None:
    raw_value = (value or "").strip()
    if not raw_value:
        return None

    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def parse_optional_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None

    raw_value = value.strip()
    if not raw_value:
        return None

    try:
        return datetime.fromisoformat(raw_value)
    except ValueError:
        return None


def build_filter_scope(
    *,
    search_text: str,
    phone: str,
    project_number: str,
    software_version: str,
    position: str,
    classification: str,
    test_type: str,
    sample: str,
    defect_id: str,
    dual_charging: str,
    event_ts_from: date | None,
    event_ts_to: date | None,
    inserted_at_from: date | None,
    inserted_at_to: date | None,
) -> FilterScope:
    return FilterScope(
        search_text=search_text,
        phone=phone,
        project_number=project_number,
        software_version=software_version,
        position=position,
        classification=classification,
        test_type=test_type,
        sample=sample,
        defect_id=defect_id,
        dual_charging=dual_charging,
        event_ts_from=event_ts_from,
        event_ts_to=event_ts_to,
        inserted_at_from=inserted_at_from,
        inserted_at_to=inserted_at_to,
    )


def parse_json_payload(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None

    raw_value = value.strip()
    if not raw_value:
        return None

    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return None


def coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    normalized = value.strip().replace(",", ".")
    if not normalized:
        return None

    match = re.search(r"-?\d+(?:\.\d+)?", normalized)
    if not match:
        return None

    try:
        return float(match.group(0))
    except ValueError:
        return None


def coerce_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if not isinstance(value, str):
        return None

    normalized = value.strip().lower()
    if not normalized:
        return None
    if normalized in {"true", "yes", "y", "tak", "1", "dual", "enabled"}:
        return True
    if normalized in {"false", "no", "n", "nie", "0", "single", "disabled"}:
        return False
    return None


def find_metric_value(payload: Any, target_key: str) -> float | None:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() == target_key:
                parsed = coerce_float(value)
                if parsed is not None:
                    return parsed

        for value in payload.values():
            parsed = find_metric_value(value, target_key)
            if parsed is not None:
                return parsed

    if isinstance(payload, list):
        for item in payload:
            parsed = find_metric_value(item, target_key)
            if parsed is not None:
                return parsed

    return None


def find_first_metric_value(payload: Any, target_keys: tuple[str, ...]) -> float | None:
    for key in target_keys:
        parsed = find_metric_value(payload, key)
        if parsed is not None:
            return parsed
    return None


def find_efficiency_value(payload: Any) -> float | None:
    direct_keys = ("eff", "efficiency")
    charger_side_keys = ("eff_left_charger", "eff_right_charger")

    for key in direct_keys:
        parsed = find_metric_value(payload, key)
        if parsed is not None:
            return parsed

    side_values = [value for value in (find_metric_value(payload, key) for key in charger_side_keys) if value is not None]
    if side_values:
        return sum(side_values) / len(side_values)

    return None


def find_payload_field(payload: Any, *target_keys: str) -> Any:
    normalized_keys = {key.strip().lower() for key in target_keys if key.strip()}

    def scan(node: Any) -> Any:
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).strip().lower() in normalized_keys:
                    return value

            for value in node.values():
                result = scan(value)
                if result is not None:
                    return result

        if isinstance(node, list):
            for item in node:
                result = scan(item)
                if result is not None:
                    return result

        return None

    return scan(payload)


def normalize_status_code(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return "N/A"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)

    raw_value = str(value).strip()
    if not raw_value or raw_value.upper() in {"N/A", "NA", "NONE", "NULL", "NAN"}:
        return "N/A"

    match = re.search(r"\d+", raw_value)
    if match:
        return match.group(0)

    return raw_value.upper()


def get_status_metadata(status_code: str) -> dict[str, str]:
    metadata = STATUS_DETAILS.get(status_code)
    if metadata is not None:
        return metadata
    return {
        "label": f"status {status_code}",
        "description": "Nieznany status",
        "color": "#d6d0c8",
        "text_color": "#3b2d22",
    }


def format_number(value: float | None, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}".replace(".", ",") + suffix


def format_duration_minutes(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 120:
        return f"{value / 60:.1f}".replace(".", ",") + " h"
    return format_number(value, digits=1, suffix=" min")


def format_gap_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    if value >= 3600:
        return f"{value / 3600:.1f}".replace(".", ",") + " h"
    if value >= 60:
        return f"{value / 60:.1f}".replace(".", ",") + " min"
    return f"{int(round(value))} s"


def format_metric_list(values: list[str]) -> str:
    return ", ".join(values) if values else "brak"


def format_rate_percent(value: float | None) -> str:
    if value is None:
        return "n/a"
    return format_number(value * 100, suffix="%")


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(value, maximum))


def compute_ingest_delay_seconds(event_ts: Any, inserted_at: Any) -> float | None:
    event_dt = parse_optional_datetime(event_ts)
    inserted_dt = parse_optional_datetime(inserted_at)
    if event_dt is None or inserted_dt is None:
        return None
    return (inserted_dt - event_dt).total_seconds()


def enrich_row(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("_source_prepared"):
        if row.get("ingest_delay_seconds") is None:
            row["ingest_delay_seconds"] = compute_ingest_delay_seconds(
                row.get("event_ts"),
                row.get("inserted_at"),
            )
        return row

    payload = None
    if (
        row.get("eff") is None
        or row.get("rx") is None
        or row.get("tx") is None
        or parse_optional_datetime(row.get("event_ts")) is None
        or any(row.get(metric_name) is None for metric_name in EXTRA_METRIC_CANDIDATES)
        or any(row.get(field_name) is None for field_name in EXTRA_TEXT_CANDIDATES)
    ):
        payload = parse_json_payload(row.get("row_json"))

    if parse_optional_datetime(row.get("event_ts")) is None:
        event_ts = parse_optional_datetime(find_payload_field(payload, "timestamp", "event_ts"))
        if event_ts is not None:
            row["event_ts"] = event_ts
    if row.get("eff") is None:
        row["eff"] = find_efficiency_value(payload)
    if row.get("rx") is None:
        row["rx"] = find_metric_value(payload, "rx")
    if row.get("tx") is None:
        row["tx"] = find_metric_value(payload, "tx")
    if row.get("ingest_delay_seconds") is None:
        row["ingest_delay_seconds"] = compute_ingest_delay_seconds(
            row.get("event_ts"),
            row.get("inserted_at"),
        )
    for metric_name, candidate_keys in EXTRA_METRIC_CANDIDATES.items():
        if row.get(metric_name) is None:
            row[metric_name] = find_first_metric_value(payload, candidate_keys)
    for field_name, candidate_keys in EXTRA_TEXT_CANDIDATES.items():
        if row.get(field_name) is None:
            value = find_payload_field(payload, *candidate_keys)
            normalized = " ".join(str(value or "").replace("\n", " ").split())
            row[field_name] = normalized or None
    return row


def summarize_metric(values: list[float]) -> float | None:
    return median(values) if values else None


def pick_dominant_text(values: list[Any]) -> str:
    counts: dict[str, int] = {}
    last_seen_index: dict[str, int] = {}

    for index, value in enumerate(values):
        normalized = str(value or "").strip()
        if not normalized:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
        last_seen_index[normalized] = index

    if not counts:
        return ""

    return max(
        counts,
        key=lambda item: (counts[item], last_seen_index[item], item),
    )


def pick_dominant_bool(values: list[Any]) -> bool | None:
    counts: dict[bool, int] = {}
    last_seen_index: dict[bool, int] = {}

    for index, value in enumerate(values):
        parsed = coerce_boolish(value)
        if parsed is None:
            continue
        counts[parsed] = counts.get(parsed, 0) + 1
        last_seen_index[parsed] = index

    if not counts:
        return None

    return max(
        counts,
        key=lambda item: (counts[item], last_seen_index[item], item),
    )


def normalize_session_context_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\n", " ").split()).strip().lower()


def normalize_session_scenario_hint(value: Any) -> str:
    normalized = normalize_session_context_text(value)
    if not normalized:
        return ""
    if "rfid" in normalized or "card position" in normalized:
        return "rfid"
    if "fod" in normalized or "foreign object" in normalized:
        return "fod"
    if "nfc" in normalized and "rfid" not in normalized:
        return "nfc"
    if "charging 0-100" in normalized or "charging 0 -100" in normalized or "0-100" in normalized or "wlc" in normalized:
        return "charging"
    return normalized


def build_session_context_markers(row: dict[str, Any]) -> dict[str, str]:
    dual_charging = coerce_boolish(row.get("dual_charging_label"))
    return {
        "software_version": normalize_session_context_text(row.get("software_version")),
        "sample_label": normalize_session_context_text(row.get("sample_label")),
        "scenario_hint": normalize_session_scenario_hint(row.get("scenario_hint")),
        "card_position": normalize_session_context_text(row.get("card_position")),
        "fod_object": normalize_session_context_text(row.get("fod_object")),
        "dual_charging": (
            ""
            if dual_charging is None
            else ("true" if dual_charging else "false")
        ),
    }


def merge_session_context_markers(
    current_markers: dict[str, set[str]] | None,
    row_markers: dict[str, str],
) -> dict[str, set[str]]:
    merged = {
        key: set(values)
        for key, values in (current_markers or {}).items()
    }
    for field_name, normalized_value in row_markers.items():
        if normalized_value:
            merged.setdefault(field_name, set()).add(normalized_value)
        else:
            merged.setdefault(field_name, set())
    return merged


def session_context_conflicts(
    current_markers: dict[str, set[str]] | None,
    row_markers: dict[str, str],
) -> bool:
    if not current_markers:
        return False

    for field_name, normalized_value in row_markers.items():
        if not normalized_value:
            continue
        current_values = current_markers.get(field_name) or set()
        if current_values and normalized_value not in current_values:
            return True
    return False


def format_delta_pp(value: float | None) -> str:
    if value is None:
        return "n/a"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.1f}".replace(".", ",") + " pp"


def format_ts_range(start_ts: datetime | None, end_ts: datetime | None) -> str:
    if start_ts is None and end_ts is None:
        return "brak event_ts"
    if start_ts is None:
        return f"do {end_ts:%Y-%m-%d}" if end_ts is not None else "brak event_ts"
    if end_ts is None:
        return f"od {start_ts:%Y-%m-%d}"
    if start_ts.date() == end_ts.date():
        return f"{start_ts:%Y-%m-%d}"
    return f"{start_ts:%Y-%m-%d} - {end_ts:%Y-%m-%d}"


def format_status_time(value: datetime | None) -> str:
    if value is None:
        return "brak czasu"
    return f"{value:%Y-%m-%d %H:%M:%S}"


def format_status_interval_range(start_ts: datetime | None, end_ts: datetime | None) -> str:
    if start_ts is None or end_ts is None:
        return "brak zakresu czasu"
    if start_ts.date() == end_ts.date():
        return f"{start_ts:%H:%M:%S} - {end_ts:%H:%M:%S}"
    return f"{start_ts:%Y-%m-%d %H:%M:%S} - {end_ts:%Y-%m-%d %H:%M:%S}"


def format_status_occurrence_range(start_ts: datetime | None, end_ts: datetime | None) -> str:
    if start_ts is None and end_ts is None:
        return "brak czasu"
    if start_ts is None:
        return f"{end_ts:%d.%m.%Y %H:%M}" if end_ts is not None else "brak czasu"
    if end_ts is None:
        return f"{start_ts:%d.%m.%Y %H:%M}"
    if start_ts == end_ts:
        return f"{start_ts:%d.%m.%Y %H:%M}"
    return f"{start_ts:%d.%m.%Y %H:%M} - {end_ts:%d.%m.%Y %H:%M}"


def format_status_clock(value: datetime | None) -> str:
    if value is None:
        return "--:--:--"
    return f"{value:%H:%M:%S}"


def build_scope_title(*, phone: str, project_number: str, software_version: str) -> str:
    scope_bits = []
    if phone:
        scope_bits.append(f"telefon {phone}")
    if project_number:
        scope_bits.append(f"projekt {project_number}")
    if software_version:
        scope_bits.append(f"release {software_version}")
    return " / ".join(scope_bits) if scope_bits else "Aktualne filtry"


def normalize_examples_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").split())


def extract_position_number(value: Any) -> int | None:
    return extract_examples_position_number(value)


def _examples_column_to_index(column_label: str) -> int:
    value = 0
    for char in column_label:
        if char.isalpha():
            value = value * 26 + (ord(char.upper()) - 64)
    return value - 1


def _examples_parse_shared_strings(archive: ZipFile) -> list[str]:
    shared_strings_path = "xl/sharedStrings.xml"
    if shared_strings_path not in archive.namelist():
        return []

    root = ET.fromstring(archive.read(shared_strings_path))
    return [
        "".join((node.text or "") for node in item.iterfind(".//main:t", {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}))
        for item in root.findall("main:si", {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"})
    ]


def _examples_resolve_sheet_map(archive: ZipFile) -> dict[str, str]:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    workbook_root = ET.fromstring(archive.read("xl/workbook.xml"))
    relations_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    relation_map = {
        relation.attrib["Id"]: relation.attrib["Target"]
        for relation in relations_root.findall(
            "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
        )
    }

    sheet_map: dict[str, str] = {}
    for sheet in workbook_root.findall("main:sheets/main:sheet", namespace):
        relation_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
        target = relation_map.get(relation_id, "")
        if not target:
            continue
        if not target.startswith("xl/"):
            target = f"xl/{target}"
        sheet_map[sheet.attrib["name"]] = target
    return sheet_map


def _examples_read_cell_text(cell: ET.Element, shared_strings: list[str]) -> str:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell_type = cell.attrib.get("t")
    value_node = cell.find("main:v", namespace)

    if cell_type == "inlineStr":
        return "".join((node.text or "") for node in cell.iterfind(".//main:t", namespace))
    if value_node is None:
        return ""

    raw_value = value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)]
        except (IndexError, ValueError):
            return raw_value
    return raw_value


def _examples_read_sheet_rows(archive: ZipFile, sheet_path: str, shared_strings: list[str]) -> list[dict[int, str]]:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(archive.read(sheet_path))
    rows: list[dict[int, str]] = []

    for row in root.findall("main:sheetData/main:row", namespace):
        values: dict[int, str] = {}
        for cell in row.findall("main:c", namespace):
            reference = cell.attrib.get("r", "")
            column_label = "".join(char for char in reference if char.isalpha())
            if not column_label:
                continue
            values[_examples_column_to_index(column_label)] = _examples_read_cell_text(cell, shared_strings)
        rows.append(values)
    return rows


def classify_examples_cell(sheet_name: str, raw_text: Any) -> str | None:
    text = normalize_examples_text(raw_text)
    if not text:
        return None

    lowered = text.lower()
    if lowered in {"#value!", "#ref!", "-", "n/a", "na"}:
        return None
    if "test not required" in lowered or lowered == "tested" or lowered.startswith("tested on sw"):
        return "skip"

    if sheet_name == "NFC":
        if lowered == "ok":
            return "pass"
        if "no ok" in lowered or lowered == "nok":
            return "not_ok"
        return None

    if sheet_name in {"CHARGING 0 -100 %", "WLC"}:
        if "blower fault" in lowered or "card damaging" in lowered:
            return "defect"
        if re.search(r"\bstatus\s*(8|9|13|17)\b|\bstate\s*(8|9|13|17)\b", lowered):
            return "defect"
        if re.search(r"\bstatus\s*3\b|\bstate\s*3\b", lowered):
            return "pass"
        if sheet_name == "CHARGING 0 -100 %" and re.search(r"\bstatus\s*2\b", lowered):
            return "pass"
        if "toggling" in lowered or "no charging" in lowered or "charging inter" in lowered:
            return "not_ok"
        if re.search(r"\bstatus\s*(0|1|4|5|6|15|16)\b|\bstate\s*(0|1|4|5|6|15|16)\b", lowered):
            return "not_ok"
        return None

    if sheet_name == "FOD":
        if re.search(r"\bstatus\s*3\b|\bstate\s*3\b", lowered):
            return "defect"
        if re.search(r"\bstatus\s*5\b|\bstate\s*5\b", lowered):
            return "not_ok"
        if re.search(r"\bstatus\s*(4|6)\b|\bstate\s*(4|6)\b", lowered):
            return "pass"
        if re.search(r"\bstatus\s*(13|17)\b|\bstate\s*(13|17)\b", lowered):
            return "defect"
        return None

    if sheet_name in {"RFID Protection", "RFID Protection_2"}:
        if "card damaging" in lowered or lowered.startswith("defect "):
            return "defect"
        if re.search(r"\bstatus\s*15\b", lowered):
            return "pass"
        if re.search(r"\bstatus\s*16\b", lowered):
            return "not_ok"
        if re.search(r"\bstatus\s*(1|3|4|6|8|9|13|17)\b", lowered):
            return "defect"
        return None

    return None


def load_examples_benchmarks() -> dict[str, Any]:
    return load_examples_benchmarks_from_rules(EXAMPLES_DIR)


def extract_status_samples(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []

    for row in rows:
        payload = None
        timestamp = parse_optional_datetime(row.get("event_ts"))
        if timestamp is None:
            payload = parse_json_payload(row.get("row_json"))
            timestamp = parse_optional_datetime(find_payload_field(payload, "timestamp", "event_ts"))
        if timestamp is None:
            continue

        raw_status = row.get("hmi_status")
        if raw_status is None:
            if payload is None:
                payload = parse_json_payload(row.get("row_json"))
            raw_status = find_payload_field(payload, "hmi_status")

        samples.append(
            {
                "raw_id": row.get("raw_id"),
                "timestamp": timestamp,
                "status": normalize_status_code(raw_status),
            }
        )

    samples.sort(key=lambda item: (item["timestamp"], item["raw_id"] or 0))
    return samples


def build_status_intervals(status_samples: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float]:
    if not status_samples:
        return [], 60.0

    gaps_seconds = [
        (current["timestamp"] - previous["timestamp"]).total_seconds()
        for previous, current in zip(status_samples, status_samples[1:])
        if (current["timestamp"] - previous["timestamp"]).total_seconds() > 0
    ]
    fallback_gap_seconds = median(gaps_seconds) if gaps_seconds else 60.0

    intervals: list[dict[str, Any]] = []
    start_index = 0

    for index in range(1, len(status_samples)):
        if status_samples[index]["status"] != status_samples[start_index]["status"]:
            start_time = status_samples[start_index]["timestamp"]
            end_time = status_samples[index]["timestamp"]
            intervals.append(
                {
                    "status": status_samples[start_index]["status"],
                    "start_time": start_time,
                    "end_time": end_time,
                    "sample_count": index - start_index,
                    "duration_seconds": max((end_time - start_time).total_seconds(), 0.0),
                }
            )
            start_index = index

    start_time = status_samples[start_index]["timestamp"]
    end_time = status_samples[-1]["timestamp"] + timedelta(seconds=max(fallback_gap_seconds, 1.0))
    intervals.append(
        {
            "status": status_samples[start_index]["status"],
            "start_time": start_time,
            "end_time": end_time,
            "sample_count": len(status_samples) - start_index,
            "duration_seconds": max((end_time - start_time).total_seconds(), 0.0),
        }
    )
    return intervals, fallback_gap_seconds


def build_status_event(
    interval: dict[str, Any],
    *,
    kind: str,
    reason: str,
) -> dict[str, Any]:
    metadata = get_status_metadata(interval["status"])
    return {
        "kind": kind,
        "label": metadata["label"],
        "description": metadata["description"],
        "time_range": format_status_interval_range(interval["start_time"], interval["end_time"]),
        "duration": format_gap_seconds(interval["duration_seconds"]),
        "reason": reason,
    }


def is_problem_severity_critical(severity: str) -> bool:
    return severity in {"deviation", "alarm"}


def build_problem_tooltip(*bits: str) -> str:
    unique_bits: list[str] = []
    seen_bits: set[str] = set()
    for bit in bits:
        normalized = " ".join(str(bit or "").split())
        if not normalized or normalized in seen_bits:
            continue
        seen_bits.add(normalized)
        unique_bits.append(normalized)
    return "\n".join(unique_bits)


def merge_problem_annotations(
    annotations: list[dict[str, Any]],
    *,
    chart_start: datetime,
    total_duration_seconds: float,
    fallback_gap_seconds: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not annotations:
        return [], []

    merge_gap_seconds = max(PROBLEM_SPAN_MERGE_GAP_SECONDS, fallback_gap_seconds * 2)
    sorted_annotations = sorted(
        annotations,
        key=lambda item: (
            item["start_time"] is None,
            item["start_time"] or datetime.max,
            item["end_time"] or datetime.max,
        ),
    )

    merged_groups: list[dict[str, Any]] = []
    for annotation in sorted_annotations:
        start_time = annotation.get("start_time")
        end_time = annotation.get("end_time")
        if start_time is None or end_time is None:
            continue

        if merged_groups:
            current = merged_groups[-1]
            gap_seconds = (start_time - current["end_time"]).total_seconds()
            if gap_seconds <= merge_gap_seconds:
                current["end_time"] = max(current["end_time"], end_time)
                if is_problem_severity_critical(annotation["severity"]):
                    current["severity"] = "alarm"
                current["details"].extend(annotation.get("details", []))
                current["titles"].append(annotation.get("title", ""))
                continue

        merged_groups.append(
            {
                "start_time": start_time,
                "end_time": end_time,
                "severity": "alarm" if is_problem_severity_critical(annotation["severity"]) else "warning",
                "details": list(annotation.get("details", [])),
                "titles": [annotation.get("title", "")],
            }
        )

    problem_spans: list[dict[str, Any]] = []
    problem_markers: list[dict[str, Any]] = []
    for group in merged_groups:
        start_seconds = max((group["start_time"] - chart_start).total_seconds(), 0.0)
        end_seconds = min((group["end_time"] - chart_start).total_seconds(), total_duration_seconds)
        minimum_span_seconds = max(fallback_gap_seconds * 0.6, 12.0)
        if end_seconds <= start_seconds:
            end_seconds = min(total_duration_seconds, start_seconds + minimum_span_seconds)

        left_pct = clamp_float(start_seconds / total_duration_seconds * 100, 0.0, 100.0)
        width_pct = clamp_float((end_seconds - start_seconds) / total_duration_seconds * 100, 0.9, 100.0)
        width_pct = min(width_pct, max(100.0 - left_pct, 0.9))
        marker_pct = min(max(left_pct + (width_pct / 2), 1.0), 99.0)
        interval_label = format_status_interval_range(group["start_time"], group["end_time"])
        tooltip = build_problem_tooltip(
            f"Problem w przedziale {interval_label}",
            *group["details"],
        )
        title = next((title for title in group["titles"] if title), "Problem")

        problem_spans.append(
            {
                "left_pct": round(left_pct, 3),
                "width_pct": round(width_pct, 3),
                "severity": group["severity"],
                "tooltip": tooltip,
            }
        )
        problem_markers.append(
            {
                "left_pct": round(marker_pct, 3),
                "severity": group["severity"],
                "tooltip": tooltip,
                "title": title,
            }
        )

    return problem_spans, problem_markers


def build_position_info(position: Any) -> dict[str, str]:
    label = str(position or "").strip() or "brak"
    if label == "brak":
        return {
            "label": label,
            "background_color": "rgba(233, 238, 243, 0.34)",
            "border_color": "rgba(120, 138, 156, 0.26)",
            "label_color": "#536373",
        }

    swatch_index = sum(ord(char) for char in label.lower()) % len(POSITION_STYLE_SWATCHES)
    swatch = POSITION_STYLE_SWATCHES[swatch_index]
    return {
        "label": label,
        "background_color": swatch["background_color"],
        "border_color": swatch["border_color"],
        "label_color": swatch["label_color"],
    }


def resolve_metric_chart_value(row: dict[str, Any], payload: Any, metric_key: str) -> float | None:
    direct_value = row.get(metric_key)
    if direct_value is not None:
        return float(direct_value)

    if metric_key == "eff":
        return find_efficiency_value(payload)
    if metric_key == "rx":
        return find_first_metric_value(payload, ("rx", "received_power", "rx_power", "power_rx"))
    if metric_key == "tx":
        return find_first_metric_value(payload, ("tx", "transmitted_power", "tx_power", "power_tx"))
    candidate_keys = EXTRA_METRIC_CANDIDATES.get(metric_key)
    if candidate_keys:
        return find_first_metric_value(payload, candidate_keys)
    return None


def build_metric_chart(
    rows: list[dict[str, Any]],
    *,
    chart_start: datetime,
    total_duration_seconds: float,
    status_samples: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    def exceeds_relative_threshold(previous_value: float, current_value: float, threshold: float) -> bool:
        baseline = abs(previous_value)
        delta = abs(current_value - previous_value)
        if baseline < 1e-9:
            return delta > 1e-9
        return (delta / baseline) > threshold

    safe_total_seconds = max(total_duration_seconds, 1.0)
    series_rows: list[dict[str, Any]] = []
    has_any_series = False
    has_any_alert_segments = False
    merged_samples: dict[datetime, dict[str, Any]] = {}

    for row in rows:
        payload = None
        timestamp = parse_optional_datetime(row.get("event_ts"))
        if timestamp is None:
            payload = parse_json_payload(row.get("row_json"))
            timestamp = parse_optional_datetime(find_payload_field(payload, "timestamp", "event_ts"))
        if timestamp is None:
            continue

        if payload is None:
            payload = parse_json_payload(row.get("row_json"))
        metric_values = {
            config["key"]: resolve_metric_chart_value(row, payload, config["key"])
            for config in METRIC_SERIES_CONFIG
        }
        if all(value is None for value in metric_values.values()):
            continue

        sample = merged_samples.get(timestamp)
        if sample is None:
            sample = {
                "timestamp": timestamp,
                "timestamp_key": timestamp.isoformat(),
                "raw_id": row.get("raw_id") or 0,
                "status": "N/A",
                "series_positions": {},
                "value_labels": {},
            }
            for config in METRIC_SERIES_CONFIG:
                sample[config["key"]] = None
            merged_samples[timestamp] = sample

        for config in METRIC_SERIES_CONFIG:
            metric_value = metric_values[config["key"]]
            if metric_value is not None:
                sample[config["key"]] = float(metric_value)

        raw_status = row.get("hmi_status")
        if raw_status is None:
            if payload is None:
                payload = parse_json_payload(row.get("row_json"))
            raw_status = find_payload_field(payload, "hmi_status")
        normalized_status = normalize_status_code(raw_status)
        if normalized_status != "N/A":
            sample["status"] = normalized_status

    hover_points: list[dict[str, Any]] = []
    hover_lookup: dict[str, dict[str, Any]] = {}
    ordered_hover_samples = sorted(
        merged_samples.values(),
        key=lambda item: (item["timestamp"], item["raw_id"]),
    )
    status_rows = status_samples or []
    status_index = 0

    for sample in ordered_hover_samples:
        while (
            status_rows
            and status_index + 1 < len(status_rows)
            and status_rows[status_index + 1]["timestamp"] <= sample["timestamp"]
        ):
            status_index += 1

        resolved_status = sample["status"]
        if resolved_status == "N/A" and status_rows:
            resolved_status = status_rows[status_index]["status"]

        status_metadata = get_status_metadata(resolved_status)
        elapsed_seconds = (sample["timestamp"] - chart_start).total_seconds()
        x_pct = clamp_float(elapsed_seconds / safe_total_seconds * 100, 0.0, 100.0)
        hover_point = {
            "timestamp_key": sample["timestamp_key"],
            "x_pct": round(x_pct, 3),
            "time_label": format_status_time(sample["timestamp"]),
            "status_label": status_metadata["label"],
            "status_description": status_metadata["description"],
            "series_positions": sample["series_positions"],
            "value_labels": {},
        }
        for config in METRIC_SERIES_CONFIG:
            hover_point["value_labels"][config["key"]] = format_number(
                sample.get(config["key"]),
                digits=config["digits"],
                suffix=config["suffix"],
            )
        hover_points.append(hover_point)
        hover_lookup[sample["timestamp_key"]] = hover_point

    for config in METRIC_SERIES_CONFIG:
        point_rows = []
        values: list[float] = []
        session_start = parse_optional_datetime(rows[0].get("event_ts")) if rows else None

        for row in rows:
            payload = None
            timestamp = parse_optional_datetime(row.get("event_ts"))
            if timestamp is None:
                payload = parse_json_payload(row.get("row_json"))
                timestamp = parse_optional_datetime(find_payload_field(payload, "timestamp", "event_ts"))
            if payload is None:
                payload = parse_json_payload(row.get("row_json"))
            value = resolve_metric_chart_value(row, payload, config["key"])
            if timestamp is None or value is None:
                continue

            elapsed_seconds = (timestamp - chart_start).total_seconds()
            x_pct = clamp_float(elapsed_seconds / safe_total_seconds * 100, 0.0, 100.0)
            numeric_value = float(value)
            timestamp_key = timestamp.isoformat()
            point_rows.append(
                {
                    "x_pct": x_pct,
                    "value": numeric_value,
                    "timestamp_key": timestamp_key,
                }
            )
            values.append(numeric_value)

        summary_value = summarize_metric(values)
        if not point_rows:
            series_rows.append(
                {
                    "key": config["key"],
                    "label": config["label"],
                    "color": config["color"],
                    "has_data": False,
                    "points": "",
                    "single_point": None,
                    "alert_segments": [],
                    "summary_label": "brak danych",
                }
            )
            continue

        has_any_series = True
        point_rows.sort(key=lambda item: (item["x_pct"], item["timestamp_key"]))
        value_min = min(values)
        value_max = max(values)
        value_span = value_max - value_min
        points: list[str] = []
        alert_segments: list[dict[str, str]] = []
        single_point = None

        for point in point_rows:
            if abs(value_span) < 1e-9:
                y_pct = 50.0
            else:
                y_pct = 100 - ((point["value"] - value_min) / value_span * 100)
            point["y_pct"] = clamp_float(y_pct, 0.0, 100.0)
            hover_point = hover_lookup.get(point["timestamp_key"])
            if hover_point is not None:
                hover_point["series_positions"][config["key"]] = round(point["y_pct"], 3)
            point_text = f"{point['x_pct']:.3f},{point['y_pct']:.3f}"
            points.append(point_text)

        if config["key"] in {"rx", "tx", "rpp", "current_a", "voltage_v"}:
            for previous_point, current_point in zip(point_rows, point_rows[1:]):
                if current_point["value"] >= previous_point["value"]:
                    continue
                previous_ts = parse_optional_datetime(previous_point["timestamp_key"])
                current_ts = parse_optional_datetime(current_point["timestamp_key"])
                if should_ignore_metric_transition(
                    metric_key=config["key"],
                    previous_value=previous_point["value"],
                    current_value=current_point["value"],
                    session_start=session_start,
                    previous_ts=previous_ts,
                    current_ts=current_ts,
                ):
                    continue
                if exceeds_relative_threshold(previous_point["value"], current_point["value"], METRIC_CHART_ALERT_THRESHOLD):
                    alert_segments.append(
                        {
                            "points": (
                                f"{previous_point['x_pct']:.3f},{previous_point['y_pct']:.3f} "
                                f"{current_point['x_pct']:.3f},{current_point['y_pct']:.3f}"
                            ),
                        }
                    )

        if alert_segments:
            has_any_alert_segments = True

        if len(points) == 1:
            x_value, y_value = points[0].split(",")
            single_point = {
                "x_pct": x_value,
                "y_pct": y_value,
            }

        series_rows.append(
            {
                "key": config["key"],
                "label": config["label"],
                "color": config["color"],
                "has_data": True,
                "points": " ".join(points),
                "single_point": single_point,
                "alert_segments": alert_segments,
                "summary_label": f"mediana {format_number(summary_value, digits=config['digits'], suffix=config['suffix'])}",
            }
        )

    return {
        "series": series_rows,
        "hover_series": [
            {
                "key": config["key"],
                "label": config["label"],
            }
            for config in METRIC_SERIES_CONFIG
        ],
        "has_any_series": has_any_series,
        "has_any_alert_segments": has_any_alert_segments,
        "hover_points": hover_points,
        "scale_note": "Kazda seria ma osobna skale, zeby zachowac ksztalt przebiegu.",
        "alert_note": "Czerwone odcinki oznaczaja nagly spadek po ustabilizowaniu sesji, a nie naturalny start ladowania.",
    }


def resolve_status_expectation(session: dict[str, Any]) -> dict[str, Any]:
    profile = resolve_project_profile(str(session.get("project_number") or "").strip())
    assessment = session.get("examples_assessment") or {}
    scenario_code = str(assessment.get("scenario_code") or "").strip().lower()

    default_expectation = {
        "allowed_codes": {EXPECTED_FINAL_STATUS, PREFERRED_CHARGING_STATUS},
        "warning_codes": set(),
        "strict": False,
        "expected_end_codes": {EXPECTED_FINAL_STATUS},
        "classification_summary": (
            "Poprawna sesja ladowania utrzymuje status 3, a w scenariuszu 0-100 moze zakonczyc sie statusem 2."
        ),
        "deviation_reason": "Pomiedzy statusami 2 i 3 pojawil sie inny status niz 2 lub 3.",
        "warning_reason": "Status odbiegal od oczekiwanego przebiegu sesji.",
        "ok_summary": "",
    }

    if scenario_code == "rfid":
        return {
            "allowed_codes": set(profile["rfid_pass_statuses"]),
            "warning_codes": set(profile["rfid_not_ok_statuses"]),
            "strict": True,
            "expected_end_codes": set(profile["rfid_pass_statuses"]),
            "classification_summary": "Scenariusz RFID powinien przejsc w ochronny status 15 albo 16.",
            "deviation_reason": "W scenariuszu RFID pojawil sie status inny niz oczekiwany status 15/16.",
            "warning_reason": "Scenariusz RFID wymaga recznej interpretacji ochrony.",
            "ok_summary": "Scenariusz RFID pozostaje w statusie 15 lub 16, zgodnie z oczekiwana blokada.",
        }

    if scenario_code == "fod":
        return {
            "allowed_codes": set(profile["fod_pass_statuses"]),
            "warning_codes": set(profile["fod_not_ok_statuses"]),
            "strict": True,
            "expected_end_codes": set(profile["fod_pass_statuses"]),
            "classification_summary": "Scenariusz FOD zwykle przechodzi w status 4 lub 6, ale ostateczna ocena zalezy od oczekiwania testu.",
            "deviation_reason": "Scenariusz FOD wszedl w status, ktory nie potwierdza oczekiwanego zachowania wobec obiektu obcego.",
            "warning_reason": "Scenariusz FOD wszedl w status czesciowo zgodny albo wymagajacy dodatkowej interpretacji.",
            "ok_summary": "Scenariusz FOD pozostaje w statusie 4 lub 6, jesli taki byl oczekiwany wynik testu.",
        }

    return default_expectation


def classify_status_intervals(
    intervals: list[dict[str, Any]],
    *,
    phone_has_status_readout: bool,
    session: dict[str, Any],
) -> dict[str, Any]:
    real_intervals = [
        (index, interval)
        for index, interval in enumerate(intervals)
        if interval["status"] != "N/A"
    ]
    real_codes = [interval["status"] for _, interval in real_intervals]
    expectation = resolve_status_expectation(session)

    flagged_by_index: dict[int, str] = {}
    deviations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    allowed_codes = set(expectation["allowed_codes"])
    warning_codes = set(expectation["warning_codes"])

    if not real_intervals:
        return {
            "flagged_by_index": flagged_by_index,
            "deviations": deviations,
            "warnings": warnings,
            "classification": "no_readout" if not phone_has_status_readout else "missing_in_session",
            "classification_label": "Brak odczytu" if not phone_has_status_readout else "Brak statusu w tej sesji",
            "summary": (
                "Ten telefon nie raportuje hmi_status w aktualnych danych."
                if not phone_has_status_readout
                else "W tej sesji nie ma odczytu hmi_status mimo dostepnosci statusu w innych sesjach."
            ),
            "final_status": None,
            "ended_expected": False,
        }

    allowed_present = any(code in allowed_codes for code in real_codes)
    intervals_to_validate = real_intervals if expectation["strict"] else (real_intervals[:-1] if len(real_intervals) > 1 else [])
    should_validate = expectation["strict"] or allowed_present

    invalid_intervals: list[tuple[int, dict[str, Any], str]] = []
    if should_validate:
        for index, interval in intervals_to_validate:
            status_code = interval["status"]
            if status_code in allowed_codes:
                continue
            marker_kind = "warning" if status_code in warning_codes else "deviation"
            invalid_intervals.append((index, interval, marker_kind))

    if invalid_intervals:
        for index, interval, marker_kind in invalid_intervals:
            flagged_by_index[index] = marker_kind
            if marker_kind == "warning":
                warnings.append(
                    build_status_event(
                        interval,
                        kind="warning",
                        reason=expectation["warning_reason"],
                    )
                )
            else:
                deviations.append(
                    build_status_event(
                        interval,
                        kind="deviation",
                        reason=expectation["deviation_reason"],
                    )
                )

    final_status = real_intervals[-1][1]["status"]
    classification = "ok"
    classification_label = "Przebieg poprawny"
    summary = expectation["ok_summary"] if allowed_present or expectation["strict"] else expectation["classification_summary"]
    if deviations:
        classification = "deviation"
        classification_label = "Odchylenie"
        summary = expectation["deviation_reason"]
    elif warnings:
        classification = "warning"
        classification_label = "Uwaga"
        summary = expectation["warning_reason"]

    return {
        "flagged_by_index": flagged_by_index,
        "deviations": deviations,
        "warnings": warnings,
        "classification": classification,
        "classification_label": classification_label,
        "summary": summary,
        "final_status": final_status,
        "ended_expected": final_status in expectation["expected_end_codes"],
        "expectation_summary": expectation["classification_summary"],
        "expected_end_codes": set(expectation["expected_end_codes"]),
    }


def build_session_status_chart(
    session: dict[str, Any],
    *,
    status_samples: list[dict[str, Any]],
    phone_has_status_readout: bool,
) -> dict[str, Any] | None:
    if not status_samples:
        return None

    intervals, fallback_gap_seconds = build_status_intervals(status_samples)
    if not intervals:
        return None

    classification = classify_status_intervals(
        intervals,
        phone_has_status_readout=phone_has_status_readout,
        session=session,
    )
    chart_start = intervals[0]["start_time"]
    chart_end = intervals[-1]["end_time"]
    total_duration_seconds = max((chart_end - chart_start).total_seconds(), max(fallback_gap_seconds, 1.0))
    position_info = build_position_info(session.get("position"))
    examples_assessment = session.get("examples_assessment") or {}
    metric_chart = build_metric_chart(
        session.get("rows") or [],
        chart_start=chart_start,
        total_duration_seconds=total_duration_seconds,
        status_samples=status_samples,
    )

    segments: list[dict[str, Any]] = []
    interval_items: list[dict[str, Any]] = []
    raw_problem_annotations: list[dict[str, Any]] = []
    problem_events: list[dict[str, Any]] = []
    seen_problem_events: set[tuple[str, str, str, str]] = set()

    def append_problem_marker(
        *,
        start_time: datetime | None,
        end_time: datetime | None,
        severity: str,
        label: str,
        description: str,
        summary: str,
        extra_details: list[str] | None = None,
    ) -> None:
        resolved_start = start_time or end_time
        resolved_end = end_time or start_time
        if resolved_start is None or resolved_end is None:
            return

        if resolved_end < resolved_start:
            resolved_start, resolved_end = resolved_end, resolved_start

        time_label = format_status_interval_range(resolved_start, resolved_end)
        detail_lines = [f"{label}: {summary} ({time_label})"]
        if description:
            detail_lines.append(f"Szczegol: {description}")
        if extra_details:
            detail_lines.extend(f"Powod: {detail}" for detail in extra_details if detail)

        raw_problem_annotations.append(
            {
                "start_time": resolved_start,
                "end_time": resolved_end,
                "severity": severity,
                "title": label,
                "details": detail_lines,
            }
        )

        event_key = (
            severity,
            label,
            format_status_interval_range(resolved_start, resolved_end),
            summary,
        )
        if event_key in seen_problem_events:
            return
        seen_problem_events.add(event_key)
        problem_events.append(
            {
                "severity": severity,
                "label": label,
                "description": description,
                "time_label": format_status_interval_range(resolved_start, resolved_end),
                "summary": summary,
            }
        )

    for index, interval in enumerate(intervals):
        metadata = get_status_metadata(interval["status"])
        left_pct = (
            max((interval["start_time"] - chart_start).total_seconds(), 0.0) / total_duration_seconds * 100
        )
        raw_width_pct = interval["duration_seconds"] / total_duration_seconds * 100
        width_pct = raw_width_pct if len(intervals) == 1 else max(raw_width_pct, 1.2)
        width_pct = min(width_pct, max(100 - left_pct, 0.8))
        marker_kind = classification["flagged_by_index"].get(index)
        marker_pct = min(max(left_pct + (width_pct / 2), 1.0), 99.0)

        tooltip_bits = [
            metadata["label"],
            metadata["description"],
            format_status_interval_range(interval["start_time"], interval["end_time"]),
            f"probki: {interval['sample_count']}",
            f"czas: {format_gap_seconds(interval['duration_seconds'])}",
        ]
        if marker_kind == "deviation":
            tooltip_bits.append("odchylenie")
        elif marker_kind == "warning":
            tooltip_bits.append("uwaga")

        segments.append(
            {
                "code": interval["status"],
                "label": metadata["label"],
                "description": metadata["description"],
                "color": metadata["color"],
                "text_color": metadata["text_color"],
                "left_pct": round(left_pct, 3),
                "width_pct": round(width_pct, 3),
                "marker_pct": round(marker_pct, 3),
                "show_label": width_pct >= 8,
                "short_label": metadata["label"],
                "is_expected_end": (
                    index == len(intervals) - 1
                    and interval["status"] in classification.get("expected_end_codes", {EXPECTED_FINAL_STATUS})
                ),
                "is_deviation": marker_kind == "deviation",
                "is_warning": marker_kind == "warning",
                "marker_kind": marker_kind,
                "marker_symbol": "!" if marker_kind == "deviation" else "?",
                "tooltip": " | ".join(tooltip_bits),
            }
        )
        if marker_kind in {"deviation", "warning"}:
            append_problem_marker(
                start_time=interval["start_time"],
                end_time=interval["end_time"],
                severity=marker_kind,
                label=metadata["label"],
                description=metadata["description"],
                summary="Status odbiegal od oczekiwanego przebiegu sesji.",
            )
        interval_items.append(
            {
                "label": metadata["label"],
                "description": metadata["description"],
                "color": metadata["color"],
                "text_color": metadata["text_color"],
                "time_range": format_status_interval_range(interval["start_time"], interval["end_time"]),
                "start_clock": format_status_clock(interval["start_time"]),
                "end_clock": format_status_clock(interval["end_time"]),
                "duration_label": format_gap_seconds(interval["duration_seconds"]),
                "sample_count": interval["sample_count"],
                "is_deviation": marker_kind == "deviation",
                "is_warning": marker_kind == "warning",
            }
        )

    for event in sorted(
        session.get("position_events") or [],
        key=lambda item: (
            item.get("start_ts") is None,
            item.get("start_ts") or datetime.max,
            item.get("metric_label") or "",
        ),
    ):
        append_problem_marker(
            start_time=event.get("start_ts"),
            end_time=event.get("end_ts"),
            severity=event.get("severity") or "warning",
            label=event.get("metric_label") or "Metryka",
            description=event.get("event_type") or "",
            summary=event.get("summary") or "",
        )

    reasons = list(
        dict.fromkeys(
            [
                *examples_assessment.get("reasons", []),
                *session.get("issues", []),
            ]
        )
    )
    assessment_problem_time = examples_assessment.get("problem_timestamp") or session.get("start_ts") or session.get("end_ts")
    if reasons and assessment_problem_time is not None:
        append_problem_marker(
            start_time=assessment_problem_time,
            end_time=assessment_problem_time,
            severity="alarm" if examples_assessment.get("verdict") in {"defect", "not_ok"} else "warning",
            label=examples_assessment.get("scenario_label", "Sesja problemowa"),
            description=examples_assessment.get("scenario_detail_label", ""),
            summary=reasons[0],
            extra_details=reasons[1:3],
        )

    problem_spans, problem_markers = merge_problem_annotations(
        raw_problem_annotations,
        chart_start=chart_start,
        total_duration_seconds=total_duration_seconds,
        fallback_gap_seconds=fallback_gap_seconds,
    )

    final_status_code = classification["final_status"] or intervals[-1]["status"]
    final_metadata = get_status_metadata(final_status_code)
    context_bits = [item for item in [session.get("phone"), session.get("charger_name"), session.get("position")] if item]
    session_title = " / ".join(context_bits) or f"Sesja {session.get('session_id', '?')}"
    problem_events.sort(
        key=lambda item: (
            0 if item["severity"] in {"deviation", "alarm"} else 1,
            item["time_label"],
            item["label"],
        )
    )
    classification_badge_class = "ok"
    if classification["classification"] in {"deviation", "warning"}:
        classification_badge_class = "warn"
    elif classification["classification"] in {"no_readout", "missing_in_session"}:
        classification_badge_class = "neutral"
    has_status_timeline = any(interval["status"] != "N/A" for interval in intervals)

    return {
        "title": session_title,
        "session_id": session.get("session_id"),
        "phone": session.get("phone") or "brak telefonu",
        "project_number": session.get("project_number") or "brak",
        "software_version": session.get("software_version") or "brak release",
        "charger_name": session.get("charger_name") or "brak ladowarki",
        "position_label": session.get("position") or "brak",
        "time_range": format_ts_range(session.get("start_ts"), session.get("end_ts")),
        "start_label": format_status_time(session.get("start_ts") or chart_start),
        "end_label": format_status_time(session.get("end_ts") or intervals[-1]["start_time"]),
        "sample_count": session.get("sample_count", len(status_samples)),
        "duration_label": format_duration_minutes(session.get("duration_minutes")),
        "final_status_label": final_metadata["label"],
        "final_status_description": final_metadata["description"],
        "ended_expected": classification["ended_expected"],
        "deviation_count": len(classification["deviations"]),
        "warning_count": len(classification["warnings"]),
        "deviations": classification["deviations"][:6],
        "warnings": classification["warnings"][:4],
        "extra_deviation_count": max(0, len(classification["deviations"]) - 6),
        "classification": classification["classification"],
        "classification_label": classification["classification_label"],
        "classification_summary": classification["summary"],
        "classification_badge_class": classification_badge_class,
        "position_info": position_info,
        "problem_reason": reasons[0] if reasons else classification["summary"],
        "examples_verdict_label": examples_assessment.get("label", "Brak porownania"),
        "examples_badge_class": examples_assessment.get("badge_class", "neutral"),
        "scenario_label": examples_assessment.get("scenario_label", "n/a"),
        "scenario_detail_label": examples_assessment.get(
            "scenario_detail_label",
            examples_assessment.get("scenario_label", "n/a"),
        ),
        "examples_status_label": examples_assessment.get("status_label", "brak statusu"),
        "examples_status_detail_label": examples_assessment.get(
            "status_detail_label",
            examples_assessment.get("status_label", "brak statusu"),
        ),
        "metric_chart": metric_chart,
        "problem_spans": problem_spans,
        "problem_span_count": len(problem_spans),
        "problem_markers": problem_markers,
        "problem_events": problem_events[:6],
        "extra_problem_event_count": max(0, len(problem_events) - 6),
        "has_status_timeline": has_status_timeline,
        "segments": segments,
        "interval_items": interval_items,
        "sort_time": session.get("end_ts") or session.get("start_ts") or chart_start,
    }


def build_problem_metric_only_chart(session: dict[str, Any]) -> dict[str, Any] | None:
    rows = session.get("rows") or []
    event_times = [
        ts for ts in (parse_optional_datetime(row.get("event_ts")) for row in rows) if ts is not None
    ]
    if not event_times:
        return None

    chart_start = event_times[0]
    chart_end = event_times[-1]
    gap_seconds = [
        (current - previous).total_seconds()
        for previous, current in zip(event_times, event_times[1:])
        if (current - previous).total_seconds() > 0
    ]
    fallback_gap_seconds = median(gap_seconds) if gap_seconds else 60.0
    total_duration_seconds = max(
        (chart_end - chart_start).total_seconds() + max(fallback_gap_seconds, 1.0),
        max(fallback_gap_seconds, 1.0),
    )

    metric_chart = build_metric_chart(
        rows,
        chart_start=chart_start,
        total_duration_seconds=total_duration_seconds,
        status_samples=[],
    )

    position_info = build_position_info(session.get("position"))
    examples_assessment = session.get("examples_assessment") or {}
    reasons = list(
        dict.fromkeys(
            [
                *examples_assessment.get("reasons", []),
                *session.get("issues", []),
            ]
        )
    )
    raw_problem_annotations: list[dict[str, Any]] = []
    problem_events: list[dict[str, Any]] = []
    seen_problem_events: set[tuple[str, str, str, str]] = set()

    def append_problem_marker(
        *,
        start_time: datetime | None,
        end_time: datetime | None,
        severity: str,
        label: str,
        description: str,
        summary: str,
        extra_details: list[str] | None = None,
    ) -> None:
        resolved_start = start_time or end_time
        resolved_end = end_time or start_time
        if resolved_start is None or resolved_end is None:
            return
        if resolved_end < resolved_start:
            resolved_start, resolved_end = resolved_end, resolved_start

        time_label = format_status_interval_range(resolved_start, resolved_end)
        detail_lines = [f"{label}: {summary} ({time_label})"]
        if description:
            detail_lines.append(f"Szczegol: {description}")
        if extra_details:
            detail_lines.extend(f"Powod: {detail}" for detail in extra_details if detail)

        raw_problem_annotations.append(
            {
                "start_time": resolved_start,
                "end_time": resolved_end,
                "severity": severity,
                "title": label,
                "details": detail_lines,
            }
        )

        event_key = (
            severity,
            label,
            format_status_interval_range(resolved_start, resolved_end),
            summary,
        )
        if event_key in seen_problem_events:
            return
        seen_problem_events.add(event_key)
        problem_events.append(
            {
                "severity": severity,
                "label": label,
                "description": description,
                "time_label": format_status_interval_range(resolved_start, resolved_end),
                "summary": summary,
            }
        )

    for event in sorted(
        session.get("position_events") or [],
        key=lambda item: (
            item.get("start_ts") is None,
            item.get("start_ts") or datetime.max,
            item.get("metric_label") or "",
        ),
    ):
        append_problem_marker(
            start_time=event.get("start_ts"),
            end_time=event.get("end_ts"),
            severity=event.get("severity") or "warning",
            label=event.get("metric_label") or "Metryka",
            description=event.get("event_type") or "",
            summary=event.get("summary") or "",
        )

    assessment_problem_time = examples_assessment.get("problem_timestamp") or session.get("start_ts") or session.get("end_ts")
    if reasons and assessment_problem_time is not None:
        append_problem_marker(
            start_time=assessment_problem_time,
            end_time=assessment_problem_time,
            severity="alarm" if examples_assessment.get("verdict") in {"defect", "not_ok"} else "warning",
            label=examples_assessment.get("scenario_label", "Sesja problemowa"),
            description=examples_assessment.get("scenario_detail_label", ""),
            summary=reasons[0],
            extra_details=reasons[1:3],
        )

    problem_spans, problem_markers = merge_problem_annotations(
        raw_problem_annotations,
        chart_start=chart_start,
        total_duration_seconds=total_duration_seconds,
        fallback_gap_seconds=fallback_gap_seconds,
    )
    problem_events.sort(
        key=lambda item: (
            0 if item["severity"] in {"deviation", "alarm"} else 1,
            item["time_label"],
            item["label"],
        )
    )

    context_bits = [item for item in [session.get("phone"), session.get("charger_name"), session.get("position")] if item]
    session_title = " / ".join(context_bits) or f"Sesja {session.get('session_id', '?')}"
    return {
        "title": session_title,
        "session_id": session.get("session_id"),
        "phone": session.get("phone") or "brak telefonu",
        "project_number": session.get("project_number") or "brak",
        "software_version": session.get("software_version") or "brak release",
        "charger_name": session.get("charger_name") or "brak ladowarki",
        "position_label": session.get("position") or "brak",
        "time_range": format_ts_range(session.get("start_ts"), session.get("end_ts")),
        "start_label": format_status_time(session.get("start_ts") or chart_start),
        "end_label": format_status_time(session.get("end_ts") or chart_end),
        "sample_count": session.get("sample_count", len(rows)),
        "duration_label": format_duration_minutes(session.get("duration_minutes")),
        "final_status_label": examples_assessment.get("status_label", "brak statusu"),
        "final_status_description": examples_assessment.get("status_detail_label", "Brak odczytu hmi_status."),
        "ended_expected": False,
        "deviation_count": 0,
        "warning_count": len(problem_events),
        "deviations": [],
        "warnings": [],
        "extra_deviation_count": 0,
        "classification": "metric_only",
        "classification_label": "Wykres metryk",
        "classification_summary": reasons[0] if reasons else "Sesja bez odczytu statusu, pokazano przebieg metryk.",
        "classification_badge_class": "neutral",
        "position_info": position_info,
        "problem_reason": reasons[0] if reasons else "Sesja bez odczytu statusu, pokazano przebieg metryk.",
        "examples_verdict_label": examples_assessment.get("label", "Brak porownania"),
        "examples_badge_class": examples_assessment.get("badge_class", "neutral"),
        "scenario_label": examples_assessment.get("scenario_label", "n/a"),
        "scenario_detail_label": examples_assessment.get(
            "scenario_detail_label",
            examples_assessment.get("scenario_label", "n/a"),
        ),
        "examples_status_label": examples_assessment.get("status_label", "brak statusu"),
        "examples_status_detail_label": examples_assessment.get(
            "status_detail_label",
            examples_assessment.get("status_label", "brak statusu"),
        ),
        "metric_chart": metric_chart,
        "problem_spans": problem_spans,
        "problem_span_count": len(problem_spans),
        "problem_markers": problem_markers,
        "problem_events": problem_events[:6],
        "extra_problem_event_count": max(0, len(problem_events) - 6),
        "has_status_timeline": False,
        "segments": [],
        "interval_items": [],
        "sort_time": session.get("end_ts") or session.get("start_ts") or chart_start,
    }


def build_status_timeline_analysis(sessions: list[dict[str, Any]]) -> dict[str, Any] | None:
    prepared_sessions: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    phone_status_readout: dict[str, bool] = {}

    for session in sessions:
        status_samples = extract_status_samples(session.get("rows") or [])
        prepared_sessions.append((session, status_samples))
        phone_key = str(session.get("phone") or "").strip().lower()
        has_real_status = any(sample["status"] != "N/A" for sample in status_samples)
        phone_status_readout[phone_key] = phone_status_readout.get(phone_key, False) or has_real_status

    session_charts = [
        chart
        for chart in (
            build_session_status_chart(
                session,
                status_samples=status_samples,
                phone_has_status_readout=phone_status_readout.get(str(session.get("phone") or "").strip().lower(), False),
            )
            for session, status_samples in prepared_sessions
        )
        if chart is not None
    ]
    if not session_charts:
        return None

    session_charts.sort(key=lambda chart: chart["sort_time"], reverse=True)
    for index, chart in enumerate(session_charts, start=1):
        chart["display_session_index"] = index

    legend_codes = [PREFERRED_CHARGING_STATUS, EXPECTED_FINAL_STATUS]
    seen_codes = set(legend_codes)
    for chart in session_charts:
        for segment in chart["segments"]:
            if segment["code"] not in seen_codes:
                legend_codes.append(segment["code"])
                seen_codes.add(segment["code"])

    legend = []
    for code in legend_codes:
        metadata = get_status_metadata(code)
        legend.append(
            {
                "label": metadata["label"],
                "description": metadata["description"],
                "color": metadata["color"],
                "text_color": metadata["text_color"],
            }
        )

    sessions_with_deviation = sum(1 for chart in session_charts if chart["deviation_count"] > 0)
    sessions_with_warning = sum(1 for chart in session_charts if chart["warning_count"] > 0)
    sessions_without_readout = sum(
        1 for chart in session_charts if chart["classification"] in {"no_readout", "missing_in_session"}
    )
    completed_sessions = sum(1 for chart in session_charts if chart["ended_expected"])
    deviation_total = sum(chart["deviation_count"] for chart in session_charts)
    warning_total = sum(chart["warning_count"] for chart in session_charts)

    return {
        "summary": (
            "Ocena paska statusow zalezy od scenariusza sesji: dla ladowania oczekiwany jest status 3 "
            "(a w 0-100 finalnie takze 2), dla RFID status 15/16, a dla FOD typowo 4/6, jesli taki jest oczekiwany wynik testu."
        ),
        "sessions": session_charts,
        "legend": legend,
        "session_count": len(session_charts),
        "sessions_displayed": len(session_charts),
        "truncated": False,
        "completed_sessions": completed_sessions,
        "sessions_with_deviation": sessions_with_deviation,
        "sessions_with_warning": sessions_with_warning,
        "sessions_without_readout": sessions_without_readout,
        "deviation_total": deviation_total,
        "warning_total": warning_total,
    }


def build_examples_session_assessment(session: dict[str, Any]) -> dict[str, Any]:
    examples_benchmarks = load_examples_benchmarks()
    project_number = str(session.get("project_number") or "").strip()
    benchmark = examples_benchmarks.get(project_number)
    return assess_session_against_project_rules(
        session,
        project_number=project_number,
        benchmark=benchmark,
        project_has_status_readout=session.get("project_has_status_readout"),
    )


def build_filters(
    *,
    search_text: str,
    phone: str,
    project_number: str,
    software_version: str,
    position: str = "",
    sample: str = "",
    defect_id: str = "",
    dual_charging: str = "",
    event_ts_from: date | None = None,
    event_ts_to: date | None = None,
    inserted_at_from: date | None = None,
    inserted_at_to: date | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    if search_text:
        pattern = f"%{search_text}%"
        clauses.append(
            """
            (
                cast(raw_id as text) ilike %s
                or device_name ilike %s
                or source_csv_file ilike %s
                or charger_name ilike %s
                or phone ilike %s
                or position ilike %s
                or project_number ilike %s
                or software_version ilike %s
            )
            """
        )
        params.extend([pattern] * 8)

    if phone:
        clauses.append("phone = %s")
        params.append(phone)

    if project_number:
        clauses.append("project_number = %s")
        params.append(project_number)

    if software_version:
        clauses.append("software_version = %s")
        params.append(software_version)

    if position:
        clauses.append("position = %s")
        params.append(position)

    if sample:
        clauses.append("sample_label = %s")
        params.append(sample)

    if defect_id:
        clauses.append("defect_id ilike %s")
        params.append(f"%{defect_id}%")

    if dual_charging:
        dual_flag = coerce_boolish(dual_charging)
        if dual_flag is not None:
            clauses.append("dual_charging_flag = %s")
            params.append(dual_flag)

    if event_ts_from is not None:
        clauses.append(f"{TIMESTAMP_SQL['event_ts']} >= %s")
        params.append(datetime.combine(event_ts_from, datetime.min.time()))

    if event_ts_to is not None:
        clauses.append(f"{TIMESTAMP_SQL['event_ts']} < %s")
        params.append(datetime.combine(event_ts_to + timedelta(days=1), datetime.min.time()))

    if inserted_at_from is not None:
        clauses.append(f"{TIMESTAMP_SQL['inserted_at']} >= %s")
        params.append(datetime.combine(inserted_at_from, datetime.min.time()))

    if inserted_at_to is not None:
        clauses.append(f"{TIMESTAMP_SQL['inserted_at']} < %s")
        params.append(datetime.combine(inserted_at_to + timedelta(days=1), datetime.min.time()))

    if not clauses:
        return "", []

    return f"where {' and '.join(clauses)}", params


def scope_to_filter_kwargs(scope: FilterScope) -> dict[str, Any]:
    return {
        "search_text": scope.search_text,
        "phone": scope.phone,
        "project_number": scope.project_number,
        "software_version": scope.software_version,
        "position": scope.position,
        "sample": scope.sample,
        "defect_id": scope.defect_id,
        "dual_charging": scope.dual_charging,
        "event_ts_from": scope.event_ts_from,
        "event_ts_to": scope.event_ts_to,
        "inserted_at_from": scope.inserted_at_from,
        "inserted_at_to": scope.inserted_at_to,
    }


def fetch_rows(
    *,
    search_text: str,
    phone: str,
    project_number: str,
    software_version: str,
    position: str,
    event_ts_from: date | None,
    event_ts_to: date | None,
    inserted_at_from: date | None,
    inserted_at_to: date | None,
    page: int,
    page_size: int,
    sort_by: str,
    sort_dir: str,
) -> dict[str, Any]:
    where_sql, where_params = build_filters(
        search_text=search_text,
        phone=phone,
        project_number=project_number,
        software_version=software_version,
        position=position,
        event_ts_from=event_ts_from,
        event_ts_to=event_ts_to,
        inserted_at_from=inserted_at_from,
        inserted_at_to=inserted_at_to,
    )
    nulls_sql = " nulls last" if sort_by in TIMESTAMP_SQL else ""
    order_sql = f"{SORTABLE_COLUMNS[sort_by]} {sort_dir}{nulls_sql}"

    with create_connection() as conn:
        source_from_sql = get_source_from_sql(conn)
        with conn.cursor() as cur:
            cur.execute(f"select count(*) from {source_from_sql} {where_sql}", where_params)
            total_rows = cur.fetchone()[0]
            total_pages = max(1, ceil(total_rows / page_size)) if total_rows else 1
            page = min(page, total_pages)
            offset = (page - 1) * page_size

            cur.execute(
                f"""
                select
                    {SELECT_COLUMNS_SQL}
                from {source_from_sql}
                {where_sql}
                order by {order_sql}
                limit %s
                offset %s
                """,
                [*where_params, page_size, offset],
            )
            column_names = [desc.name for desc in cur.description]
            rows = [enrich_row(dict(zip(column_names, row))) for row in cur.fetchall()]

    return {
        "rows": rows,
        "total_rows": total_rows,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
    }

def count_filtered_rows(scope: FilterScope) -> int:
    where_sql, where_params = build_filters(**scope_to_filter_kwargs(scope))

    with create_connection() as conn:
        source_from_sql = get_source_from_sql(conn)
        with conn.cursor() as cur:
            cur.execute(f"select count(*) from {source_from_sql} {where_sql}", where_params)
            return cur.fetchone()[0]


def build_analysis_group_join_sql(source_alias: str, group_alias: str) -> str:
    return " and ".join(
        f"coalesce({source_alias}.{column}, '') = coalesce({group_alias}.{column}, '')"
        for column in ANALYSIS_GROUP_COLUMNS
    )


def build_prefixed_column_sql(columns_sql: str, alias: str) -> str:
    prefixed_lines: list[str] = []
    for raw_line in columns_sql.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        suffix = "," if stripped.endswith(",") else ""
        column_name = stripped[:-1] if suffix else stripped
        prefixed_lines.append(f"    {alias}.{column_name}{suffix}")
    return "\n".join(prefixed_lines)


def build_analysis_group_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(column) or "").strip() for column in ANALYSIS_GROUP_COLUMNS)


def count_included_candidate_groups(rows: list[dict[str, Any]]) -> int:
    return len({build_analysis_group_identity(row) for row in rows})


def trim_rows_to_recent_candidate_groups(
    rows: list[dict[str, Any]],
    *,
    candidate_group_limit: int,
    max_rows_per_group: int,
) -> tuple[list[dict[str, Any]], int]:
    selected_rows: list[dict[str, Any]] = []
    group_order: list[tuple[str, ...]] = []
    group_row_counts: dict[tuple[str, ...], int] = {}

    for row in rows:
        group_key = build_analysis_group_identity(row)
        if group_key not in group_row_counts:
            if len(group_order) >= candidate_group_limit:
                continue
            group_order.append(group_key)
            group_row_counts[group_key] = 0
        if group_row_counts[group_key] >= max_rows_per_group:
            continue
        selected_rows.append(row)
        group_row_counts[group_key] += 1

    return selected_rows, len(group_order)


def select_recent_candidate_groups(
    cur: psycopg.Cursor[Any],
    *,
    source_from_sql: str,
    where_sql: str,
    where_params: list[Any],
    candidate_group_limit: int,
) -> list[tuple[str, ...]]:
    seed_limit = max(candidate_group_limit * ANALYSIS_FAST_CANDIDATE_ROW_MULTIPLIER, candidate_group_limit)
    candidate_group_select_sql = ", ".join(ANALYSIS_GROUP_COLUMNS)

    cur.execute(
        f"""
        with filtered_source as (
            select *
            from {source_from_sql}
            {where_sql}
        )
        select
            {candidate_group_select_sql}
        from filtered_source
        where analysis_candidate
        order by {TIMESTAMP_SQL["event_ts"]} desc nulls last, raw_id desc
        limit %s
        """,
        [*where_params, seed_limit],
    )

    groups: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()
    for row in cur.fetchall():
        normalized = tuple(str(value or "").strip() for value in row)
        if normalized in seen:
            continue
        seen.add(normalized)
        groups.append(normalized)
        if len(groups) >= candidate_group_limit:
            break
    return groups


def build_selected_groups_values_sql(groups: list[tuple[str, ...]]) -> tuple[str, list[Any]]:
    values_sql = ", ".join(
        "(" + ", ".join(["%s"] * len(ANALYSIS_GROUP_COLUMNS)) + ")"
        for _ in groups
    )
    params: list[Any] = []
    for group in groups:
        params.extend(group)
    return values_sql, params


def build_analysis_session_fetch_identity(row: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(row.get(column) or "").strip() for column in ANALYSIS_SESSION_FETCH_COLUMNS)


def build_selected_session_windows(
    rows: list[dict[str, Any]],
    *,
    padding_seconds: int = SESSION_BREAK_SECONDS,
) -> list[tuple[Any, ...]]:
    grouped_bounds: dict[tuple[str, ...], tuple[datetime, datetime]] = {}

    for row in rows:
        timestamp = parse_optional_datetime(row.get("event_ts"))
        if timestamp is None:
            continue

        fetch_key = build_analysis_session_fetch_identity(row)
        bounds = grouped_bounds.get(fetch_key)
        if bounds is None:
            grouped_bounds[fetch_key] = (timestamp, timestamp)
            continue

        grouped_bounds[fetch_key] = (
            min(bounds[0], timestamp),
            max(bounds[1], timestamp),
        )

    padding = timedelta(seconds=max(padding_seconds, 0))
    windows = [
        (*fetch_key, start_ts - padding, end_ts + padding)
        for fetch_key, (start_ts, end_ts) in grouped_bounds.items()
    ]
    windows.sort(key=lambda item: (item[6], item[0], item[1], item[2], item[3], item[4], item[5]))
    return windows


def build_selected_session_windows_values_sql(windows: list[tuple[Any, ...]]) -> tuple[str, list[Any]]:
    values_sql = ", ".join(
        "(" + ", ".join(["%s"] * (len(ANALYSIS_SESSION_FETCH_COLUMNS) + 2)) + ")"
        for _ in windows
    )
    params: list[Any] = []
    for window in windows:
        params.extend(window)
    return values_sql, params


def should_expand_problem_candidate_sessions(scope: FilterScope) -> bool:
    return any(
        (
            bool(scope.search_text),
            bool(scope.sample),
            bool(scope.defect_id),
            bool(scope.dual_charging),
            scope.event_ts_from is not None,
            scope.event_ts_to is not None,
            scope.inserted_at_from is not None,
            scope.inserted_at_to is not None,
        )
    )


def fetch_expanded_analysis_rows(
    cur: psycopg.Cursor[Any],
    *,
    source_from_sql: str,
    source_is_prepared: bool,
    seed_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    windows = build_selected_session_windows(seed_rows)
    if not windows:
        return seed_rows

    values_sql, params = build_selected_session_windows_values_sql(windows)
    qualified_analysis_columns_sql = build_prefixed_column_sql(ANALYSIS_COLUMNS_SQL, "source")
    session_join_sql = " and ".join(
        f"coalesce(source.{column}, '') = coalesce(selected_windows.{column}, '')"
        for column in ANALYSIS_SESSION_FETCH_COLUMNS
    )

    cur.execute(
        f"""
        with selected_windows (
            phone,
            project_number,
            device_name,
            charger_name,
            position,
            source_csv_file,
            window_start,
            window_end
        ) as (
            values {values_sql}
        ),
        expanded_rows as (
            select distinct on (source.raw_id)
                {qualified_analysis_columns_sql}
            from (
                select *
                from {source_from_sql}
            ) source
            join selected_windows
                on {session_join_sql}
               and source.{TIMESTAMP_SQL["event_ts"]} is not null
               and source.{TIMESTAMP_SQL["event_ts"]} >= selected_windows.window_start
               and source.{TIMESTAMP_SQL["event_ts"]} <= selected_windows.window_end
            order by source.raw_id, source.{TIMESTAMP_SQL["event_ts"]} desc nulls last
        )
        select *
        from expanded_rows
        order by {TIMESTAMP_SQL["event_ts"]} desc nulls last, raw_id desc
        """,
        params,
    )
    _, expanded_rows, _ = fetch_analysis_result_rows(cur, source_is_prepared=source_is_prepared)
    return expanded_rows


def configure_cursor_statement_timeout(cur: psycopg.Cursor[Any], timeout_ms: int | None) -> None:
    if timeout_ms is None or timeout_ms <= 0:
        return
    cur.execute("select set_config('statement_timeout', %s, true)", (str(int(timeout_ms)),))


def resolve_fast_analysis_order_sql(scope: FilterScope) -> str:
    if scope.inserted_at_from is not None or scope.inserted_at_to is not None:
        return f"{TIMESTAMP_SQL['inserted_at']} desc nulls last, raw_id desc"
    return f"{TIMESTAMP_SQL['event_ts']} desc nulls last, raw_id desc"


def fetch_analysis_result_rows(
    cur: psycopg.Cursor[Any],
    *,
    source_is_prepared: bool,
    metadata_columns: set[str] | None = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    metadata_columns = metadata_columns or set()
    column_names = [desc.name for desc in cur.description]
    row_columns = [name for name in column_names if name not in metadata_columns]
    rows: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}

    for values in cur.fetchall():
        raw_row = dict(zip(column_names, values))
        if not metadata and metadata_columns:
            metadata = {name: raw_row.get(name) for name in metadata_columns}
        row = {name: raw_row[name] for name in row_columns}
        row["_source_prepared"] = source_is_prepared
        rows.append(enrich_row(row))

    return row_columns, rows, metadata


def fetch_analysis_dataset(
    scope: FilterScope,
    *,
    limit: int | None = None,
    problem_candidates_only: bool = False,
    include_totals: bool = True,
    statement_timeout_ms: int | None = None,
) -> dict[str, Any]:
    where_sql, where_params = build_filters(**scope_to_filter_kwargs(scope))
    qualified_analysis_columns_sql = build_prefixed_column_sql(ANALYSIS_COLUMNS_SQL, "source")
    should_expand_sessions = should_expand_problem_candidate_sessions(scope)

    with create_connection() as conn:
        source_from_sql, source_is_prepared = get_source_descriptor(conn)
        with conn.cursor() as cur:
            configure_cursor_statement_timeout(cur, statement_timeout_ms)
            if not problem_candidates_only:
                limit_sql = ""
                query_params = list(where_params)
                if limit is not None:
                    limit_sql = "limit %s"
                    query_params.append(limit)

                cur.execute(
                    f"""
                    select
                        count(*) over() as dataset_total_rows,
                        {ANALYSIS_COLUMNS_SQL}
                    from {source_from_sql}
                    {where_sql}
                    order by {TIMESTAMP_SQL["event_ts"]} desc nulls last, raw_id desc
                    {limit_sql}
                    """,
                    query_params,
                )
                _, rows, metadata = fetch_analysis_result_rows(
                    cur,
                    source_is_prepared=source_is_prepared,
                    metadata_columns={"dataset_total_rows"},
                )
                source_total_rows = int(metadata.get("dataset_total_rows") or 0)
                return {
                    "rows": rows,
                    "total_rows": source_total_rows,
                    "candidate_group_total": 0,
                    "included_candidate_group_count": 0,
                    "counts_complete": True,
                }

            candidate_group_limit = limit if limit is not None else resolve_analysis_candidate_group_limit(scope)
            candidate_group_order_sql = ", ".join(ANALYSIS_GROUP_COLUMNS)
            candidate_group_select_sql = ", ".join(ANALYSIS_GROUP_COLUMNS)

            source_total_rows: int | None = None
            candidate_group_total: int | None = None
            counts_complete = include_totals

            if include_totals:
                cur.execute(
                    f"""
                    with filtered_source as (
                        select *
                        from {source_from_sql}
                        {where_sql}
                    ),
                    candidate_groups as (
                        select {candidate_group_select_sql}
                        from filtered_source
                        where analysis_candidate
                        group by {candidate_group_select_sql}
                    )
                    select
                        (select count(*) from filtered_source) as source_total_rows,
                        (select count(*) from candidate_groups) as candidate_group_total
                    """,
                    where_params,
                )
                source_total_rows, candidate_group_total = cur.fetchone()
                if source_total_rows == 0:
                    return {
                        "rows": [],
                        "total_rows": 0,
                        "candidate_group_total": 0,
                        "included_candidate_group_count": 0,
                        "counts_complete": True,
                    }
                if candidate_group_total == 0:
                    return {
                        "rows": [],
                        "total_rows": source_total_rows,
                        "candidate_group_total": 0,
                        "included_candidate_group_count": 0,
                        "counts_complete": True,
                    }
            else:
                fast_candidate_limit = max(
                    candidate_group_limit * ANALYSIS_FAST_CANDIDATE_ROW_MULTIPLIER,
                    candidate_group_limit,
                )
                fast_order_sql = resolve_fast_analysis_order_sql(scope)
                fast_where_sql = (
                    f"{where_sql} and analysis_candidate"
                    if where_sql
                    else "where analysis_candidate"
                )
                cur.execute(
                    f"""
                    select
                        {ANALYSIS_FAST_REPORT_COLUMNS_SQL}
                    from {source_from_sql}
                    {fast_where_sql}
                    order by {fast_order_sql}
                    limit %s
                    """,
                    [*where_params, fast_candidate_limit],
                )
                _, candidate_rows, _ = fetch_analysis_result_rows(cur, source_is_prepared=source_is_prepared)
                if not candidate_rows:
                    return {
                        "rows": [],
                        "total_rows": 0,
                        "candidate_group_total": 0,
                        "included_candidate_group_count": 0,
                        "counts_complete": False,
                    }
                rows, selected_group_count = trim_rows_to_recent_candidate_groups(
                    candidate_rows,
                    candidate_group_limit=candidate_group_limit,
                    max_rows_per_group=ANALYSIS_GROUP_ROWS_LIMIT,
                )
                expanded_rows = (
                    fetch_expanded_analysis_rows(
                        cur,
                        source_from_sql=source_from_sql,
                        source_is_prepared=source_is_prepared,
                        seed_rows=rows,
                    )
                    if should_expand_sessions
                    else rows
                )
                return {
                    "rows": expanded_rows,
                    "total_rows": len(expanded_rows),
                    "candidate_group_total": selected_group_count,
                    "included_candidate_group_count": selected_group_count,
                    "counts_complete": False,
                }

            cur.execute(
                f"""
                with filtered_source as (
                    select *
                    from {source_from_sql}
                    {where_sql}
                ),
                candidate_groups as (
                    select
                        {candidate_group_select_sql},
                        max(event_ts) as last_candidate_ts
                    from filtered_source
                    where analysis_candidate
                    group by {candidate_group_select_sql}
                    order by last_candidate_ts desc nulls last, {candidate_group_order_sql}
                    limit %s
                )
                select
                    {qualified_analysis_columns_sql}
                from filtered_source source
                join candidate_groups
                    on {candidate_group_join_sql}
                order by source.{TIMESTAMP_SQL["event_ts"]} desc nulls last, source.raw_id desc
                """,
                [*where_params, candidate_group_limit],
            )
            _, rows, _ = fetch_analysis_result_rows(cur, source_is_prepared=source_is_prepared)
            included_candidate_group_count = count_included_candidate_groups(rows)
            if should_expand_sessions:
                rows = fetch_expanded_analysis_rows(
                    cur,
                    source_from_sql=source_from_sql,
                    source_is_prepared=source_is_prepared,
                    seed_rows=rows,
                )
            if not include_totals:
                source_total_rows = len(rows)
                candidate_group_total = included_candidate_group_count
                counts_complete = False

    return {
        "rows": rows,
        "total_rows": source_total_rows if source_total_rows is not None else len(rows),
        "candidate_group_total": candidate_group_total if candidate_group_total is not None else included_candidate_group_count,
        "included_candidate_group_count": (
            min(candidate_group_total, candidate_group_limit)
            if candidate_group_total is not None and include_totals
            else included_candidate_group_count
        ),
        "counts_complete": counts_complete,
    }


def fetch_recent_analysis_rows(
    scope: FilterScope,
    *,
    limit: int,
    statement_timeout_ms: int | None = None,
) -> dict[str, Any]:
    where_sql, where_params = build_filters(**scope_to_filter_kwargs(scope))
    order_sql = resolve_fast_analysis_order_sql(scope)

    with create_connection() as conn:
        source_from_sql, source_is_prepared = get_source_descriptor(conn)
        with conn.cursor() as cur:
            configure_cursor_statement_timeout(cur, statement_timeout_ms)
            cur.execute(
                f"""
                select
                    {ANALYSIS_COLUMNS_SQL}
                from {source_from_sql}
                {where_sql}
                order by {order_sql}
                limit %s
                """,
                [*where_params, limit],
            )
            _, rows, _ = fetch_analysis_result_rows(cur, source_is_prepared=source_is_prepared)

    return {
        "rows": rows,
        "total_rows": len(rows),
        "candidate_group_total": 0,
        "included_candidate_group_count": 0,
        "counts_complete": False,
    }


def fetch_filter_options() -> dict[str, list[str]]:
    now = time.monotonic()
    with FILTER_OPTIONS_CACHE_LOCK:
        if (
            FILTER_OPTIONS_CACHE["value"] is not None
            and FILTER_OPTIONS_CACHE["expires_at"] > now
        ):
            return {
                key: list(values)
                for key, values in FILTER_OPTIONS_CACHE["value"].items()
            }

    options: dict[str, list[str]] = {}
    columns = ("phone", "project_number", "software_version", "position")

    with create_connection() as conn:
        source_from_sql = get_source_from_sql(conn)
        with conn.cursor() as cur:
            for column in columns:
                cur.execute(
                    f"""
                    select distinct {column}
                    from {source_from_sql}
                    where nullif(btrim({column}), '') is not null
                    order by {column}
                    """
                )
                options[column] = [row[0] for row in cur.fetchall()]

    with FILTER_OPTIONS_CACHE_LOCK:
        FILTER_OPTIONS_CACHE["value"] = {
            key: tuple(values)
            for key, values in options.items()
        }
        FILTER_OPTIONS_CACHE["expires_at"] = now + FILTER_OPTIONS_CACHE_TTL_SECONDS

    return options


def build_session_group_key(row: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        str(row.get("phone") or "").strip(),
        str(row.get("project_number") or "").strip(),
        str(row.get("device_name") or "").strip(),
        str(row.get("charger_name") or "").strip(),
        str(row.get("position") or "").strip(),
    )


def build_ranking_session_group_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("phone") or "").strip(),
        str(row.get("project_number") or "").strip(),
        str(row.get("position") or "").strip(),
        str(row.get("software_version") or "").strip(),
    )


def detect_metric_drops(
    rows: list[dict[str, Any]],
    metric_name: str,
    *,
    relative_threshold: float,
    absolute_threshold: float = 0,
) -> list[str]:
    drops: list[str] = []

    for previous, current in zip(rows, rows[1:]):
        previous_value = previous.get(metric_name)
        current_value = current.get(metric_name)
        if previous_value is None or current_value is None:
            continue

        delta = previous_value - current_value
        if previous_value <= 0 or delta <= 0:
            continue

        if delta >= absolute_threshold and (delta / previous_value) >= relative_threshold:
            previous_ts = parse_optional_datetime(previous.get("event_ts"))
            current_ts = parse_optional_datetime(current.get("event_ts"))
            window = ""
            if previous_ts is not None and current_ts is not None:
                window = f" ({previous_ts:%H:%M} -> {current_ts:%H:%M})"
            drops.append(
                f"{metric_name.upper()} spadl z {format_number(previous_value)} do "
                f"{format_number(current_value)}{window}"
            )

    return drops


def format_metric_change(value: float, unit: str = "") -> str:
    sign = "+" if value > 0 else ""
    return f"{sign}{value:.2f}".replace(".", ",") + unit


def should_ignore_metric_transition(
    *,
    metric_key: str,
    previous_value: float,
    current_value: float,
    session_start: datetime | None,
    previous_ts: datetime | None,
    current_ts: datetime | None,
) -> bool:
    delta = current_value - previous_value
    if delta <= 0:
        return False

    minimum_previous_value = METRIC_ALERT_MIN_PREVIOUS_VALUE.get(metric_key)
    if minimum_previous_value is not None and previous_value <= minimum_previous_value:
        return True

    if session_start is None or current_ts is None:
        return False

    return (current_ts - session_start).total_seconds() <= METRIC_ALERT_WARMUP_SECONDS


def detect_metric_transition_events(
    rows: list[dict[str, Any]],
    *,
    session_id: int,
    session_position: str,
    phone: str,
    project_number: str,
    software_version: str,
    charger_name: str,
    metric_key: str,
    metric_label: str,
    unit: str,
    warning_threshold: float,
    alarm_threshold: float,
    absolute_warning: float,
    absolute_alarm: float,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    session_start = parse_optional_datetime(rows[0].get("event_ts")) if rows else None

    for previous, current in zip(rows, rows[1:]):
        previous_value = previous.get(metric_key)
        current_value = current.get(metric_key)
        if previous_value is None or current_value is None:
            continue
        if abs(previous_value) < 1e-9:
            continue

        delta = current_value - previous_value
        abs_delta = abs(delta)
        relative_change = abs_delta / abs(previous_value)
        if relative_change < warning_threshold and abs_delta < absolute_warning:
            continue

        previous_ts = parse_optional_datetime(previous.get("event_ts"))
        current_ts = parse_optional_datetime(current.get("event_ts"))
        if should_ignore_metric_transition(
            metric_key=metric_key,
            previous_value=float(previous_value),
            current_value=float(current_value),
            session_start=session_start,
            previous_ts=previous_ts,
            current_ts=current_ts,
        ):
            continue

        severity = "alarm" if relative_change >= alarm_threshold or abs_delta >= absolute_alarm else "warning"
        trend = "spike" if delta > 0 else "drop"
        start_ts = previous_ts or current_ts
        end_ts = current_ts or previous_ts
        events.append(
            {
                "event_type": trend,
                "severity": severity,
                "metric_key": metric_key,
                "metric_label": metric_label,
                "session_id": session_id,
                "position": session_position or "brak",
                "phone": phone or "brak telefonu",
                "project_number": project_number or "brak projektu",
                "software_version": software_version or "brak release",
                "charger_name": charger_name or "brak ladowarki",
                "start_ts": start_ts,
                "end_ts": end_ts,
                "time_label": format_status_interval_range(start_ts, end_ts),
                "before_label": format_number(previous_value, suffix=unit),
                "after_label": format_number(current_value, suffix=unit),
                "delta_label": format_metric_change(delta, unit),
                "delta_percent_label": format_rate_percent(relative_change),
                "summary": (
                    f"{metric_label} {'wzrosl' if trend == 'spike' else 'spadl'} "
                    f"{format_rate_percent(relative_change)} ({format_number(previous_value, suffix=unit)} -> "
                    f"{format_number(current_value, suffix=unit)})."
                ),
            }
        )

    return events


def select_metric_analysis_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], datetime | None]:
    status_samples = extract_status_samples(rows)
    real_status_samples = [sample for sample in status_samples if sample["status"] != "N/A"]
    if not real_status_samples:
        return rows, None

    seen_nonfinal_status = False
    completion_ts: datetime | None = None
    for sample in real_status_samples:
        if sample["status"] == EXPECTED_FINAL_STATUS:
            if seen_nonfinal_status:
                completion_ts = sample["timestamp"]
                break
            continue
        seen_nonfinal_status = True

    if completion_ts is not None:
        metric_rows = [
            row
            for row in rows
            if (parse_optional_datetime(row.get("event_ts")) or datetime.min) < completion_ts
        ]
        return metric_rows, completion_ts

    if all(sample["status"] == EXPECTED_FINAL_STATUS for sample in real_status_samples):
        return [], real_status_samples[0]["timestamp"]

    return rows, None


def finalize_session(session_rows: list[dict[str, Any]], session_id: int) -> dict[str, Any]:
    ordered_rows = sorted(
        session_rows,
        key=lambda row: parse_optional_datetime(row.get("event_ts")) or datetime.min,
    )
    status_samples = extract_status_samples(ordered_rows)
    observed_status_codes = sorted(
        {sample["status"] for sample in status_samples if sample["status"] != "N/A"},
        key=lambda item: (int(item) if item.isdigit() else 999, item),
    )
    event_times = [
        ts for ts in (parse_optional_datetime(row.get("event_ts")) for row in ordered_rows) if ts is not None
    ]
    start_ts = event_times[0] if event_times else None
    end_ts = event_times[-1] if event_times else None
    duration_minutes = None
    if start_ts is not None and end_ts is not None:
        duration_minutes = max((end_ts - start_ts).total_seconds() / 60, 0)

    metric_rows, completed_charge_ts = select_metric_analysis_rows(ordered_rows)
    metrics_trimmed_at_full_charge = completed_charge_ts is not None and len(metric_rows) != len(ordered_rows)
    metric_event_times = [
        ts for ts in (parse_optional_datetime(row.get("event_ts")) for row in metric_rows) if ts is not None
    ]
    metric_duration_minutes = None
    if metric_event_times:
        metric_duration_minutes = max((metric_event_times[-1] - metric_event_times[0]).total_seconds() / 60, 0)

    eff_values = [value for value in (row.get("eff") for row in metric_rows) if value is not None]
    rx_values = [value for value in (row.get("rx") for row in metric_rows) if value is not None]
    tx_values = [value for value in (row.get("tx") for row in metric_rows) if value is not None]
    rpp_values = [value for value in (row.get("rpp") for row in metric_rows) if value is not None]
    current_values = [value for value in (row.get("current_a") for row in metric_rows) if value is not None]
    temperature_values = [value for value in (row.get("temperature") for row in ordered_rows) if value is not None]
    battery_values = [value for value in (row.get("battery_level") for row in ordered_rows) if value is not None]
    voltage_values = [value for value in (row.get("voltage_v") for row in metric_rows) if value is not None]
    ingest_delays = [
        value for value in (row.get("ingest_delay_seconds") for row in ordered_rows) if value is not None
    ]

    gaps_seconds: list[float] = []
    for previous, current in zip(ordered_rows, ordered_rows[1:]):
        previous_ts = parse_optional_datetime(previous.get("event_ts"))
        current_ts = parse_optional_datetime(current.get("event_ts"))
        if previous_ts is None or current_ts is None:
            continue
        gap = (current_ts - previous_ts).total_seconds()
        if gap > 0:
            gaps_seconds.append(gap)

    base_gap = median(gaps_seconds) if gaps_seconds else 60
    interruption_threshold = max(5 * 60, base_gap * 4)
    interruptions = [gap for gap in gaps_seconds if gap > interruption_threshold]

    drop_messages = []
    drop_messages.extend(
        detect_metric_drops(
            metric_rows,
            "eff",
            relative_threshold=0.25,
            absolute_threshold=15,
        )
    )
    drop_messages.extend(
        detect_metric_drops(
            metric_rows,
            "rx",
            relative_threshold=POWER_DROP_RELATIVE_THRESHOLD,
        )
    )
    drop_messages.extend(
        detect_metric_drops(
            metric_rows,
            "tx",
            relative_threshold=POWER_DROP_RELATIVE_THRESHOLD,
        )
    )

    key = build_session_group_key(ordered_rows[0]) if ordered_rows else ("", "", "", "", "")
    software_version = pick_dominant_text([row.get("software_version") for row in ordered_rows])
    source_csv_file = pick_dominant_text([row.get("source_csv_file") for row in ordered_rows])
    scenario_hints = sorted(
        {
            " ".join(str(row.get("scenario_hint") or "").replace("\n", " ").split()).lower()
            for row in ordered_rows
            if str(row.get("scenario_hint") or "").strip()
        }
    )
    fod_object = pick_dominant_text([row.get("fod_object") for row in ordered_rows])
    card_position = pick_dominant_text([row.get("card_position") for row in ordered_rows])
    sample_label = pick_dominant_text([row.get("sample_label") for row in ordered_rows])
    manual_result = pick_dominant_text([row.get("manual_result") for row in ordered_rows])
    defect_id = pick_dominant_text([row.get("defect_id") for row in ordered_rows])
    defect_comment = pick_dominant_text([row.get("defect_comment") for row in ordered_rows])
    dual_charging = pick_dominant_bool([row.get("dual_charging_label") for row in ordered_rows])
    raw_ids = [int(row["raw_id"]) for row in ordered_rows if row.get("raw_id") is not None]
    transition_events: list[dict[str, Any]] = []
    for config in POSITION_EVENT_METRIC_CONFIG:
        transition_events.extend(
            detect_metric_transition_events(
                metric_rows,
                session_id=session_id,
                session_position=key[4],
                phone=key[0],
                project_number=key[1],
                software_version=software_version,
                charger_name=key[3],
                metric_key=config["key"],
                metric_label=config["label"],
                unit=config["unit"],
                warning_threshold=POSITION_EVENT_WARNING_THRESHOLD,
                alarm_threshold=POSITION_EVENT_ALARM_THRESHOLD,
                absolute_warning=config["absolute_warning"],
                absolute_alarm=config["absolute_alarm"],
            )
        )
    interruption_events: list[dict[str, Any]] = []
    for previous, current in zip(ordered_rows, ordered_rows[1:]):
        previous_ts = parse_optional_datetime(previous.get("event_ts"))
        current_ts = parse_optional_datetime(current.get("event_ts"))
        if previous_ts is None or current_ts is None:
            continue
        gap_seconds = (current_ts - previous_ts).total_seconds()
        if gap_seconds <= interruption_threshold:
            continue
        severity = "alarm" if gap_seconds >= max(interruption_threshold * 2, 20 * 60) else "warning"
        interruption_events.append(
            {
                "event_type": "interruption",
                "severity": severity,
                "metric_key": "event_ts",
                "metric_label": "Przerwa",
                "session_id": session_id,
                "position": key[4] or "brak",
                "phone": key[0] or "brak telefonu",
                "project_number": key[1] or "brak projektu",
                "software_version": software_version or "brak release",
                "charger_name": key[3] or "brak ladowarki",
                "start_ts": previous_ts,
                "end_ts": current_ts,
                "time_label": format_status_interval_range(previous_ts, current_ts),
                "before_label": f"{previous_ts:%H:%M:%S}",
                "after_label": f"{current_ts:%H:%M:%S}",
                "delta_label": format_gap_seconds(gap_seconds),
                "delta_percent_label": "n/a",
                "summary": f"Brak probek przez {format_gap_seconds(gap_seconds)}.",
            }
        )
    position_events = sorted(
        [*transition_events, *interruption_events],
        key=lambda item: (
            item["start_ts"] is None,
            item["start_ts"] or datetime.min,
            item["metric_label"],
        ),
        reverse=True,
    )
    return {
        "session_id": session_id,
        "group_key": key,
        "peer_key": (key[0], key[1]),
        "phone": key[0],
        "project_number": key[1],
        "device_name": key[2],
        "charger_name": key[3],
        "position": key[4],
        "software_version": software_version,
        "source_csv_file": source_csv_file,
        "scenario_hints": scenario_hints,
        "fod_object": fod_object,
        "card_position": card_position,
        "sample_label": sample_label,
        "manual_result": manual_result,
        "defect_id": defect_id,
        "defect_comment": defect_comment,
        "dual_charging": dual_charging,
        "raw_ids": raw_ids,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "sample_count": len(ordered_rows),
        "duration_minutes": duration_minutes,
        "metric_duration_minutes": metric_duration_minutes,
        "metric_sample_count": len(metric_rows),
        "avg_eff": sum(eff_values) / len(eff_values) if eff_values else None,
        "avg_rx": sum(rx_values) / len(rx_values) if rx_values else None,
        "avg_tx": sum(tx_values) / len(tx_values) if tx_values else None,
        "avg_rpp": sum(rpp_values) / len(rpp_values) if rpp_values else None,
        "avg_current_a": sum(current_values) / len(current_values) if current_values else None,
        "avg_temperature": sum(temperature_values) / len(temperature_values) if temperature_values else None,
        "max_temperature": max(temperature_values) if temperature_values else None,
        "battery_start": battery_values[0] if battery_values else None,
        "battery_end": battery_values[-1] if battery_values else None,
        "voltage_start": voltage_values[0] if voltage_values else None,
        "voltage_end": voltage_values[-1] if voltage_values else None,
        "median_ingest_delay": median(ingest_delays) if ingest_delays else None,
        "max_gap_seconds": max(gaps_seconds) if gaps_seconds else None,
        "interruptions": interruptions,
        "drop_messages": drop_messages[:6],
        "position_events": position_events,
        "warning_event_count": sum(1 for event in position_events if event["severity"] == "warning"),
        "alarm_event_count": sum(1 for event in position_events if event["severity"] == "alarm"),
        "metrics_trimmed_at_full_charge": metrics_trimmed_at_full_charge,
        "completed_charge_ts": completed_charge_ts,
        "status_codes": observed_status_codes,
        "status_samples": status_samples,
        "issues": [],
        "severity_score": 0,
        "rows": ordered_rows,
    }


def build_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable_rows = [row for row in rows if parse_optional_datetime(row.get("event_ts")) is not None]
    ordered_rows = sorted(
        usable_rows,
        key=lambda row: (build_session_group_key(row), parse_optional_datetime(row.get("event_ts"))),
    )

    sessions: list[dict[str, Any]] = []
    current_rows: list[dict[str, Any]] = []
    current_key: tuple[str, str, str, str, str] | None = None
    current_context_markers: dict[str, set[str]] | None = None
    previous_ts: datetime | None = None

    for row in ordered_rows:
        current_ts = parse_optional_datetime(row.get("event_ts"))
        row_key = build_session_group_key(row)
        row_context_markers = build_session_context_markers(row)

        should_split = False
        if current_rows and current_key != row_key:
            should_split = True
        elif current_rows and session_context_conflicts(current_context_markers, row_context_markers):
            should_split = True
        elif current_rows and previous_ts is not None and current_ts is not None:
            should_split = (current_ts - previous_ts).total_seconds() > SESSION_BREAK_SECONDS

        if should_split:
            sessions.append(finalize_session(current_rows, len(sessions) + 1))
            current_rows = []
            current_context_markers = None
            previous_ts = None

        current_rows.append(row)
        current_key = row_key
        current_context_markers = merge_session_context_markers(current_context_markers, row_context_markers)
        previous_ts = current_ts

    if current_rows:
        sessions.append(finalize_session(current_rows, len(sessions) + 1))

    return sessions


def build_ranking_sessions(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    usable_rows = [row for row in rows if parse_optional_datetime(row.get("event_ts")) is not None]
    ordered_rows = sorted(
        usable_rows,
        key=lambda row: (build_ranking_session_group_key(row), parse_optional_datetime(row.get("event_ts"))),
    )

    sessions: list[dict[str, Any]] = []
    current_rows: list[dict[str, Any]] = []
    current_key: tuple[str, str, str, str] | None = None
    previous_ts: datetime | None = None

    for row in ordered_rows:
        current_ts = parse_optional_datetime(row.get("event_ts"))
        row_key = build_ranking_session_group_key(row)

        should_split = False
        if current_rows and current_key != row_key:
            should_split = True
        elif current_rows and previous_ts is not None and current_ts is not None:
            should_split = (current_ts - previous_ts).total_seconds() > RANKING_SESSION_BREAK_SECONDS

        if should_split:
            sessions.append(finalize_session(current_rows, len(sessions) + 1))
            current_rows = []
            previous_ts = None

        current_rows.append(row)
        current_key = row_key
        previous_ts = current_ts

    if current_rows:
        sessions.append(finalize_session(current_rows, len(sessions) + 1))

    return sessions


def annotate_project_analysis_context(sessions: list[dict[str, Any]]) -> None:
    field_checks = {
        "temperature": lambda session: session.get("max_temperature") is not None,
        "eff": lambda session: session.get("avg_eff") is not None,
        "card_position": lambda session: bool(str(session.get("card_position") or "").strip()),
        "fod_object": lambda session: bool(str(session.get("fod_object") or "").strip()),
        "software_version": lambda session: bool(str(session.get("software_version") or "").strip()),
    }
    project_status_readout: dict[str, bool] = {}
    coverage_by_project: dict[str, dict[str, bool]] = {}

    for session in sessions:
        project_key = str(session.get("project_number") or "").strip()
        if not project_key:
            continue
        has_real_status = any(code != "N/A" for code in (session.get("status_codes") or []))
        project_status_readout[project_key] = project_status_readout.get(project_key, False) or has_real_status
        coverage = coverage_by_project.setdefault(
            project_key,
            {field_name: False for field_name in field_checks},
        )
        for field_name, checker in field_checks.items():
            coverage[field_name] = coverage[field_name] or checker(session)

    for session in sessions:
        project_key = str(session.get("project_number") or "").strip()
        session["project_has_status_readout"] = project_status_readout.get(project_key) if project_key else None
        session["project_field_coverage"] = dict(coverage_by_project.get(project_key, {})) if project_key else {}


def add_peer_comparison_flags(sessions: list[dict[str, Any]]) -> None:
    grouped_sessions: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for session in sessions:
        grouped_sessions.setdefault(session["peer_key"], []).append(session)

    for peer_sessions in grouped_sessions.values():
        if len(peer_sessions) < 3:
            continue

        duration_values = [
            session["duration_minutes"]
            for session in peer_sessions
            if session["duration_minutes"] is not None
        ]
        eff_values = [session["avg_eff"] for session in peer_sessions if session["avg_eff"] is not None]
        rx_values = [session["avg_rx"] for session in peer_sessions if session["avg_rx"] is not None]
        tx_values = [session["avg_tx"] for session in peer_sessions if session["avg_tx"] is not None]
        lag_values = [
            session["median_ingest_delay"]
            for session in peer_sessions
            if session["median_ingest_delay"] is not None
        ]

        duration_median = summarize_metric(duration_values)
        eff_median = summarize_metric(eff_values)
        rx_median = summarize_metric(rx_values)
        tx_median = summarize_metric(tx_values)
        lag_median = summarize_metric(lag_values)

        for session in peer_sessions:
            if (
                duration_median is not None
                and session["duration_minutes"] is not None
                and session["duration_minutes"] < duration_median * 0.45
                and session["sample_count"] >= 3
            ):
                session["issues"].append(
                    f"Cykl byl wyraznie krotszy od typowego dla tego telefonu/projektu "
                    f"({format_duration_minutes(session['duration_minutes'])} vs mediana "
                    f"{format_duration_minutes(duration_median)})."
                )
                session["severity_score"] += 2

            if (
                eff_median is not None
                and session["avg_eff"] is not None
                and session["avg_eff"] < eff_median - 8
                and session["avg_eff"] < eff_median * 0.85
            ):
                session["issues"].append(
                    f"Sprawnosc byla nizsza niz w podobnych sesjach "
                    f"({format_number(session['avg_eff'])}% vs mediana {format_number(eff_median)}%)."
                )
                session["severity_score"] += 2

            if (
                rx_median is not None
                and session["avg_rx"] is not None
                and session["avg_rx"] < rx_median * 0.6
                and session["avg_rx"] < rx_median - 2
            ):
                session["issues"].append(
                    f"Telefon odbieral mniej mocy niz zwykle "
                    f"(RX {format_number(session['avg_rx'])} vs mediana {format_number(rx_median)})."
                )
                session["severity_score"] += 2

            if (
                tx_median is not None
                and session["avg_tx"] is not None
                and session["avg_tx"] < tx_median * 0.6
                and session["avg_tx"] < tx_median - 2
            ):
                session["issues"].append(
                    f"Ladowarka wysylala mniej mocy niz zwykle "
                    f"(TX {format_number(session['avg_tx'])} vs mediana {format_number(tx_median)})."
                )
                session["severity_score"] += 2

            if (
                lag_median is not None
                and session["median_ingest_delay"] is not None
                and session["median_ingest_delay"] > max(lag_median * 3, 120)
            ):
                session["issues"].append(
                    f"Dane trafialy do bazy ze zwloka "
                    f"(mediana {format_gap_seconds(session['median_ingest_delay'])} vs "
                    f"{format_gap_seconds(lag_median)})."
                )
                session["severity_score"] += 1


def build_flagged_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flagged_sessions: list[dict[str, Any]] = []

    for session in sessions:
        examples_assessment = session.get("examples_assessment")
        if examples_assessment is None:
            examples_assessment = build_examples_session_assessment(session)
            session["examples_assessment"] = examples_assessment

        if session["interruptions"]:
            session["issues"].append(
                f"Wykryto {len(session['interruptions'])} przerwe/y w trakcie cyklu; "
                f"najdluzsza trwala {format_gap_seconds(max(session['interruptions']))}."
            )
            session["severity_score"] += 3

        if session["drop_messages"]:
            session["issues"].extend(session["drop_messages"][:3])
            session["severity_score"] += min(3, len(session["drop_messages"]))

        if examples_assessment["verdict"] in {"warning", "not_ok", "defect"}:
            session["issues"].extend(examples_assessment["reasons"][:2])
            session["severity_score"] += examples_assessment["severity_score"]

        if not session["issues"]:
            continue

        start_ts = session["start_ts"]
        end_ts = session["end_ts"]
        if start_ts is not None and end_ts is not None:
            if start_ts.date() == end_ts.date():
                time_range = f"{start_ts:%Y-%m-%d %H:%M} - {end_ts:%H:%M}"
            else:
                time_range = f"{start_ts:%Y-%m-%d %H:%M} - {end_ts:%Y-%m-%d %H:%M}"
        else:
            time_range = "brak event_ts"

        severity = "critical" if session["severity_score"] >= 5 else "warn"
        context_bits = [
            item for item in [session["phone"], session["project_number"], session["charger_name"], session["position"]] if item
        ]
        summary_bits = [
            f"probek: {session['sample_count']}",
            f"czas: {format_duration_minutes(session['duration_minutes'])}",
            f"eff: {format_number(session['avg_eff'])}%",
            f"rx: {format_number(session['avg_rx'])}",
            f"tx: {format_number(session['avg_tx'])}",
        ]
        if session.get("metrics_trimmed_at_full_charge"):
            summary_bits.append("metryki liczone do statusu 2")
        flagged_sessions.append(
            {
                "session_id": session["session_id"],
                "title": " / ".join(context_bits) or f"Sesja {session['session_id']}",
                "time_range": time_range,
                "summary": format_metric_list(summary_bits),
                "issues": session["issues"][:5],
                "severity": severity,
                "severity_label": "Wysokie ryzyko" if severity == "critical" else "Do sprawdzenia",
                "position": session["position"] or "brak",
                "phone": session["phone"] or "brak telefonu",
                "project_number": session["project_number"] or "brak projektu",
                "software_version": session.get("software_version") or "brak release",
                "sort_time": end_ts or datetime.min,
                "severity_score": session["severity_score"],
                "examples_verdict": examples_assessment["verdict"],
                "examples_verdict_label": examples_assessment["label"],
                "examples_badge_class": examples_assessment["badge_class"],
                "examples_status_label": examples_assessment["status_label"],
                "examples_scenario_label": examples_assessment.get("scenario_label", "n/a"),
                "examples_evidence_rows": examples_assessment.get("evidence_rows", []),
            }
        )

    flagged_sessions.sort(
        key=lambda item: (item["severity_score"], item["sort_time"]),
        reverse=True,
    )
    return flagged_sessions[:8]


def build_release_efficiency_analysis(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    release_groups: dict[str, dict[str, Any]] = {}
    sessions_with_version = 0
    sessions_with_eff = 0
    comparable_sessions = 0

    for session in sessions:
        software_version = str(session.get("software_version") or "").strip()
        avg_eff = session.get("avg_eff")
        if software_version:
            sessions_with_version += 1
        if avg_eff is not None:
            sessions_with_eff += 1
        if not software_version or avg_eff is None:
            continue
        comparable_sessions += 1

        group = release_groups.setdefault(
            software_version,
            {
                "version": software_version,
                "eff_values": [],
                "session_count": 0,
                "sample_count": 0,
                "first_seen": None,
                "last_seen": None,
            },
        )
        group["eff_values"].append(avg_eff)
        group["session_count"] += 1
        group["sample_count"] += session.get("sample_count") or 0

        start_ts = session.get("start_ts")
        end_ts = session.get("end_ts")
        if start_ts is not None and (group["first_seen"] is None or start_ts < group["first_seen"]):
            group["first_seen"] = start_ts
        if end_ts is not None and (group["last_seen"] is None or end_ts > group["last_seen"]):
            group["last_seen"] = end_ts

    release_rows = sorted(
        (
            {
                "version": item["version"],
                "median_eff": summarize_metric(item["eff_values"]),
                "session_count": item["session_count"],
                "sample_count": item["sample_count"],
                "first_seen": item["first_seen"],
                "last_seen": item["last_seen"],
            }
            for item in release_groups.values()
        ),
        key=lambda item: (
            item["first_seen"] is None,
            item["first_seen"] or datetime.max,
            item["version"],
        ),
    )

    eligible_rows = [
        item for item in release_rows if item["session_count"] >= MIN_RELEASE_SESSION_COUNT and item["median_eff"] is not None
    ]
    eligible_versions = {item["version"] for item in eligible_rows}
    comparison_by_version: dict[str, dict[str, Any]] = {}
    first_alarm: dict[str, Any] | None = None
    first_warning: dict[str, Any] | None = None
    strongest_drop: dict[str, Any] | None = None
    evaluated_rows: list[dict[str, Any]] = []

    for index, current in enumerate(eligible_rows):
        baseline_rows = eligible_rows[max(0, index - RELEASE_BASELINE_WINDOW) : index]
        baseline_ready = len(baseline_rows) == RELEASE_BASELINE_WINDOW
        baseline_eff = summarize_metric([row["median_eff"] for row in baseline_rows]) if baseline_rows else None
        delta = current["median_eff"] - baseline_eff if baseline_eff is not None else None
        better_count = sum(1 for previous in baseline_rows if previous["median_eff"] > current["median_eff"])
        warning_candidate = (
            baseline_ready
            and delta is not None
            and delta <= -RELEASE_WARNING_DROP_ABSOLUTE_THRESHOLD
        )
        alarm = (
            baseline_ready
            and delta is not None
            and delta <= -RELEASE_ALARM_DROP_ABSOLUTE_THRESHOLD
            and better_count >= 2
        )
        evaluated_rows.append(
            {
                "index": index,
                "version": current["version"],
                "baseline_rows": baseline_rows,
                "baseline_ready": baseline_ready,
                "baseline_eff": baseline_eff,
                "delta": delta,
                "better_count": better_count,
                "warning_candidate": warning_candidate,
                "warning": False,
                "alarm": alarm,
                "current_eff": current["median_eff"],
            }
        )
        if delta is not None and (strongest_drop is None or delta < strongest_drop["delta"]):
            strongest_drop = {
                "current_version": current["version"],
                "delta": delta,
            }

    warning_candidates = [item["warning_candidate"] for item in evaluated_rows]
    for index, item in enumerate(evaluated_rows):
        run_start = max(0, index - RELEASE_WARNING_CONSECUTIVE_COUNT + 1)
        run_end = min(len(warning_candidates), index + RELEASE_WARNING_CONSECUTIVE_COUNT)
        longest_run = 0
        current_run = 0
        for candidate in warning_candidates[run_start:run_end]:
            if candidate:
                current_run += 1
                longest_run = max(longest_run, current_run)
            else:
                current_run = 0
        item["warning"] = (
            item["warning_candidate"]
            and longest_run >= RELEASE_WARNING_CONSECUTIVE_COUNT
            and not item["alarm"]
        )

    for item in evaluated_rows:
        current_version = item["version"]
        baseline_rows = item["baseline_rows"]
        baseline_eff = item["baseline_eff"]
        delta = item["delta"]
        better_count = item["better_count"]
        previous_versions = ", ".join(previous["version"] for previous in baseline_rows)

        if item["alarm"]:
            comparison_text = (
                f"Alarm: mediana eff nizsza od mediany 3 poprzednich releasow "
                f"{format_number(baseline_eff, suffix='%')} -> "
                f"{format_number(item['current_eff'], suffix='%')} "
                f"({format_delta_pp(delta)}). Lepsze byly {better_count} z 3 poprzednich "
                f"({previous_versions})."
            )
            trend = "drop"
            if first_alarm is None:
                first_alarm = {
                    "current_version": current_version,
                    "baseline_eff": baseline_eff,
                    "current_eff": item["current_eff"],
                    "delta": delta,
                }
        elif item["warning"]:
            comparison_text = (
                f"Warning: release nalezy do serii co najmniej "
                f"{RELEASE_WARNING_CONSECUTIVE_COUNT} kolejnych releasow "
                f"ponizej baseline'u z 3 poprzednich releasow "
                f"{format_number(baseline_eff, suffix='%')} -> "
                f"{format_number(item['current_eff'], suffix='%')} "
                f"({format_delta_pp(delta)})."
            )
            trend = "warning"
            if first_warning is None:
                first_warning = {
                    "current_version": current_version,
                    "baseline_eff": baseline_eff,
                    "current_eff": item["current_eff"],
                    "delta": delta,
                }
        elif not item["baseline_ready"]:
            comparison_text = (
                f"Budowanie baseline'u dla alarmu/warning "
                f"(potrzeba {RELEASE_BASELINE_WINDOW} poprzednich releasow, jest {len(baseline_rows)})."
            )
            trend = "baseline"
        elif item["warning_candidate"]:
            comparison_text = (
                f"Nizej od baseline'u 3 poprzednich releasow "
                f"{format_number(baseline_eff, suffix='%')} -> "
                f"{format_number(item['current_eff'], suffix='%')} "
                f"({format_delta_pp(delta)}), ale na razie nie przez "
                f"{RELEASE_WARNING_CONSECUTIVE_COUNT} kolejne releasy."
            )
            trend = "steady"
        elif delta is not None and delta >= RELEASE_ALARM_DROP_ABSOLUTE_THRESHOLD:
            comparison_text = (
                f"Wzrost vs mediana 3 poprzednich releasow "
                f"{format_number(baseline_eff, suffix='%')} -> "
                f"{format_number(item['current_eff'], suffix='%')} "
                f"({format_delta_pp(delta)})."
            )
            trend = "up"
        else:
            comparison_text = (
                f"Bez wyraznej zmiany vs mediana 3 poprzednich releasow: "
                f"{format_number(baseline_eff, suffix='%')} -> "
                f"{format_number(item['current_eff'], suffix='%')}."
            )
            trend = "steady"

        comparison_by_version[current_version] = {
            "comparison_text": comparison_text,
            "trend": trend,
            "delta": delta,
            "baseline_eff": baseline_eff,
        }

    for row in release_rows:
        if row["version"] not in eligible_versions:
            row["comparison_text"] = (
                f"Za malo sesji z eff do porownania "
                f"(min. {MIN_RELEASE_SESSION_COUNT}, jest {row['session_count']})."
            )
            row["trend"] = "insufficient"
        elif row["version"] in comparison_by_version:
            row.update(comparison_by_version[row["version"]])
        else:
            row["comparison_text"] = "Punkt odniesienia dla kolejnych releasow."
            row["trend"] = "baseline"

        row["median_eff_label"] = format_number(row["median_eff"], suffix="%")
        row["period_label"] = format_ts_range(row["first_seen"], row["last_seen"])

    if len(eligible_rows) < RELEASE_BASELINE_WINDOW + 1:
        verdict = (
            "Brak wystarczajacej liczby releasow z co najmniej "
            f"{MIN_RELEASE_SESSION_COUNT} sesjami, aby zbudowac baseline z "
            f"{RELEASE_BASELINE_WINDOW} poprzednich releasow."
        )
        insight = verdict
    elif first_alarm is not None:
        verdict = (
            f"Alarm od releasu {first_alarm['current_version']}: eff spada vs mediana "
            f"3 poprzednich releasow "
            f"({format_number(first_alarm['baseline_eff'], suffix='%')} -> "
            f"{format_number(first_alarm['current_eff'], suffix='%')}, "
            f"{format_delta_pp(first_alarm['delta'])})."
        )
        insight = verdict
    elif first_warning is not None:
        verdict = (
            f"Warning od releasu {first_warning['current_version']}: eff jest nizsze od "
            f"baseline'u 3 poprzednich releasow przez co najmniej "
            f"{RELEASE_WARNING_CONSECUTIVE_COUNT} kolejne releasy "
            f"({format_number(first_warning['baseline_eff'], suffix='%')} -> "
            f"{format_number(first_warning['current_eff'], suffix='%')}, "
            f"{format_delta_pp(first_warning['delta'])})."
        )
        insight = verdict
    else:
        verdict = (
            "Nie widac alarmu ani serii warning dla releasow software'u "
            "dla aktualnych filtrow."
        )
        insight = verdict

    if strongest_drop is not None and strongest_drop["delta"] < 0 and first_alarm is None and first_warning is None:
        verdict += (
            f" Najwieksza obserwowana zmiana in minus zaczyna sie od releasu "
            f"{strongest_drop['current_version']} ({format_delta_pp(strongest_drop['delta'])}), "
            "ale pozostaje ponizej progu warning/alarm."
        )

    diagnostics = {
        "total_sessions": len(sessions),
        "sessions_with_version": sessions_with_version,
        "sessions_with_eff": sessions_with_eff,
        "comparable_sessions": comparable_sessions,
        "release_count": len(release_rows),
        "eligible_release_count": len(eligible_rows),
    }

    return {
        "summary": (
            "Porownanie mediany eff releasu vs mediana 3 poprzednich releasow. "
            "Alarm: spadek >= 4 pp i co najmniej 2 z 3 poprzednich releasow lepsze. "
            "Warning: spadek >= 2 pp przez co najmniej 2 kolejne releasy."
        ),
        "verdict": verdict,
        "insight": insight,
        "release_rows": release_rows,
        "eligible_release_count": len(eligible_rows),
        "first_drop_version": first_alarm["current_version"] if first_alarm is not None else None,
        "diagnostics": diagnostics,
    }


def format_dual_charging_label(value: bool | None) -> str:
    if value is True:
        return "tak"
    if value is False:
        return "nie"
    return ""


def extract_assessment_defect_id(assessment: dict[str, Any]) -> str:
    for row in assessment.get("evidence_rows", []):
        if str(row.get("label") or "").strip().lower() == "defect id":
            return str(row.get("value") or "").strip()
    return ""


def extract_primary_problem_status(session: dict[str, Any], assessment: dict[str, Any]) -> tuple[str, str]:
    problem_timestamp = assessment.get("problem_timestamp")
    status_samples = session.get("status_samples") or []
    selected_sample = None

    if problem_timestamp is not None:
        for sample in status_samples:
            if sample.get("timestamp") == problem_timestamp and sample.get("status") != "N/A":
                selected_sample = sample
                break

    if selected_sample is None:
        for sample in status_samples:
            if sample.get("status") != "N/A":
                selected_sample = sample
                break

    if selected_sample is None:
        return "", ""

    status_code = str(selected_sample.get("status") or "").strip()
    if not status_code or status_code == "N/A":
        return "", ""
    metadata = get_status_metadata(status_code)
    return status_code, metadata["description"]


def resolve_problem_classification(session: dict[str, Any], assessment: dict[str, Any]) -> dict[str, str] | None:
    verdict = str(assessment.get("verdict") or "").strip()
    if verdict == "defect":
        return {"key": "defect", "label": "DEFECT", "badge_class": "critical"}
    if verdict == "not_ok":
        return {"key": "not_ok", "label": "NOT OK", "badge_class": "warn"}
    if verdict == "warning":
        return {
            "key": "potential_defect",
            "label": "POTENTIAL DEFECT / TO BE VERIFIED",
            "badge_class": "warning",
        }
    if verdict in {"ok", "no_data", "status_unavailable", "not_applicable"}:
        return None
    if session.get("interruptions") or session.get("drop_messages"):
        return {
            "key": "potential_defect",
            "label": "POTENTIAL DEFECT / TO BE VERIFIED",
            "badge_class": "warning",
        }
    return None


def build_problem_source_label(session: dict[str, Any]) -> str:
    parts = [SOURCE_RELATION]
    source_csv_file = str(session.get("source_csv_file") or "").strip()
    raw_ids = [raw_id for raw_id in (session.get("raw_ids") or []) if raw_id is not None]

    if source_csv_file:
        parts.append(source_csv_file)
    if raw_ids:
        if len(raw_ids) == 1:
            parts.append(f"raw_id {raw_ids[0]}")
        else:
            parts.append(f"raw_id {min(raw_ids)}-{max(raw_ids)}")
    return " | ".join(parts)


def build_problem_context_fields(session: dict[str, Any], assessment: dict[str, Any], classification_key: str) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []

    defect_id = extract_assessment_defect_id(assessment) or str(session.get("defect_id") or "").strip()
    if defect_id:
        fields.append({"label": "Defect ID", "value": defect_id})
    elif classification_key in {"not_ok", "potential_defect"}:
        fields.append({"label": "Defect ID", "value": "brak"})

    if session.get("manual_result"):
        fields.append({"label": "Wynik reczny", "value": str(session["manual_result"])})
    if session.get("fod_object"):
        fields.append({"label": "FOD object", "value": str(session["fod_object"])})
    if session.get("card_position"):
        fields.append({"label": "Card position", "value": str(session["card_position"])})
    if session.get("max_temperature") is not None:
        fields.append({"label": "Temperatura", "value": format_number(session["max_temperature"], suffix=" C")})
    if session.get("avg_eff") is not None:
        fields.append({"label": "Procent / eff", "value": format_number(session["avg_eff"], suffix="%")})
    if session.get("interruptions"):
        fields.append({"label": "Przerwy ladowania", "value": str(len(session["interruptions"]))})

    dual_label = format_dual_charging_label(session.get("dual_charging"))
    if dual_label:
        fields.append({"label": "Dual charging", "value": dual_label})

    defect_comment = str(session.get("defect_comment") or "").strip()
    if defect_comment:
        fields.append({"label": "Komentarz", "value": defect_comment})
    return fields


def build_problem_report_item(session: dict[str, Any]) -> dict[str, Any] | None:
    assessment = session.get("examples_assessment")
    if assessment is None:
        return None

    classification = resolve_problem_classification(session, assessment)
    if classification is None:
        return None

    status_code, status_description = extract_primary_problem_status(session, assessment)
    reasons = [reason for reason in assessment.get("reasons", []) if reason]
    short_reason = reasons[0] if reasons else "Wymaga weryfikacji zgodnie z kryteriami."
    test_type = str(assessment.get("scenario_label") or "").strip() or "Nierozpoznany test"
    defect_id = extract_assessment_defect_id(assessment) or str(session.get("defect_id") or "").strip()

    return {
        "session_id": session.get("session_id"),
        "classification_key": classification["key"],
        "classification_label": classification["label"],
        "badge_class": classification["badge_class"],
        "short_reason": short_reason,
        "project_number": str(session.get("project_number") or "").strip(),
        "test_type": test_type,
        "sample_label": str(session.get("sample_label") or "").strip(),
        "software_version": str(session.get("software_version") or "").strip(),
        "phone": str(session.get("phone") or "").strip(),
        "position": str(session.get("position") or "").strip(),
        "status_code": status_code,
        "status_description": status_description,
        "defect_id": defect_id,
        "dual_charging_label": format_dual_charging_label(session.get("dual_charging")),
        "context_fields": build_problem_context_fields(session, assessment, classification["key"]),
        "time_range": format_ts_range(session.get("start_ts"), session.get("end_ts")),
        "source_label": build_problem_source_label(session),
        "sort_time": session.get("end_ts") or session.get("start_ts") or datetime.min,
    }


def passes_problem_report_filters(item: dict[str, Any], scope: FilterScope) -> bool:
    if scope.project_number and item["project_number"] != scope.project_number:
        return False
    if scope.phone and item["phone"] != scope.phone:
        return False
    if scope.software_version and item["software_version"] != scope.software_version:
        return False
    if scope.position and item["position"] != scope.position:
        return False
    if scope.classification and item["classification_key"] != scope.classification:
        return False
    if scope.test_type and item["test_type"] != scope.test_type:
        return False
    if scope.sample and item["sample_label"] != scope.sample:
        return False
    if scope.defect_id and scope.defect_id.lower() not in (item["defect_id"] or "").lower():
        return False
    if scope.dual_charging and item["dual_charging_label"] != scope.dual_charging:
        return False
    return True


def build_problem_breakdown_rows(items: list[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    counter = Counter(str(item.get(field_name) or "").strip() for item in items if str(item.get(field_name) or "").strip())
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda row: (-row[1], row[0].lower()))
    ]


def build_problem_filter_options(items: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        "project_number": sorted({item["project_number"] for item in items if item["project_number"]}),
        "classification": [
            option
            for option in ["defect", "not_ok", "potential_defect"]
            if any(item["classification_key"] == option for item in items)
        ],
        "test_type": sorted({item["test_type"] for item in items if item["test_type"]}),
        "sample": sorted({item["sample_label"] for item in items if item["sample_label"]}),
        "software_version": sorted({item["software_version"] for item in items if item["software_version"]}),
        "phone": sorted({item["phone"] for item in items if item["phone"]}),
        "position": sorted({item["position"] for item in items if item["position"]}),
        "dual_charging": [
            option
            for option in ["tak", "nie"]
            if any(item["dual_charging_label"] == option for item in items)
        ],
    }


def build_analysis_session_cache_key(session: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(session.get("project_number") or "").strip(),
        str(session.get("phone") or "").strip(),
        str(session.get("device_name") or "").strip(),
        str(session.get("charger_name") or "").strip(),
        str(session.get("position") or "").strip(),
        str(session.get("software_version") or "").strip(),
        str(session.get("sample_label") or "").strip(),
        str(session.get("card_position") or "").strip(),
        str(session.get("fod_object") or "").strip(),
        str(session.get("manual_result") or "").strip(),
        str(session.get("defect_id") or "").strip(),
        str(session.get("defect_comment") or "").strip(),
        tuple(str(code or "") for code in (session.get("status_codes") or [])),
        tuple(
            (str(sample.get("status") or ""), str(sample.get("status_text") or ""))
            for sample in (session.get("status_samples") or [])
        ),
        bool(session.get("project_has_status_readout")),
        tuple(sorted((session.get("project_field_coverage") or {}).items())),
    )


def build_charging_analysis(
    rows: list[dict[str, Any]],
    *,
    total_rows: int,
    scope: FilterScope,
    candidate_group_total: int | None = None,
    included_candidate_group_count: int | None = None,
    counts_complete: bool = True,
) -> dict[str, Any]:
    candidate_group_total = candidate_group_total if candidate_group_total is not None else len(rows)
    included_candidate_group_count = (
        included_candidate_group_count if included_candidate_group_count is not None else candidate_group_total
    )
    selected_filters = {
        "project_number": scope.project_number,
        "classification": scope.classification,
        "test_type": scope.test_type,
        "sample": scope.sample,
        "software_version": scope.software_version,
        "phone": scope.phone,
        "position": scope.position,
        "defect_id": scope.defect_id,
        "dual_charging": scope.dual_charging,
    }

    if not rows:
        if total_rows > 0:
            return {
                "scope_title": "Raport problemow",
                "scope_subtitle": (
                    "Nie znaleziono kandydatow problemowych po aktualnych filtrach bazy."
                ),
                "summary_cards": [
                    {
                        "label": "Rekordy po filtrach",
                        "value": str(total_rows),
                        "help": (
                            "Laczna liczba rekordow z bazy po filtrach podstawowych."
                            if counts_complete
                            else "Liczba rekordow, ktore zostaly pobrane do szybkiej analizy."
                        ),
                    },
                    {
                        "label": "Grupy kandydatow",
                        "value": "0",
                        "help": "Brak grup, ktore spelily szybki prefiltr problemow.",
                    },
                    {
                        "label": "Wykryte przypadki",
                        "value": "0",
                        "help": "Brak przypadkow problemowych w aktualnym zakresie.",
                    },
                ],
                "highlights": [
                    (
                        f"Po filtrach podstawowych pozostalo {total_rows} rekordow."
                        if counts_complete
                        else f"Szybka analiza pobrala {total_rows} rekordow do oceny problemow."
                    ),
                    "Szybki prefiltr SQL nie wykryl grup wymagajacych glebszej analizy problemow.",
                ],
                "problem_items": [],
                "breakdowns": {
                    "project_number": [],
                    "test_type": [],
                    "software_version": [],
                    "phone": [],
                },
                "filter_options": {
                    "project_number": [],
                    "classification": [],
                    "test_type": [],
                    "sample": [],
                    "software_version": [],
                    "phone": [],
                    "position": [],
                    "dual_charging": [],
                },
                "selected_filters": selected_filters,
                "rows_analyzed": 0,
                "rows_total": total_rows,
                "candidate_group_total": 0,
                "included_candidate_group_count": 0,
                "truncated": False,
            }
        return {
            "scope_title": "Raport problemow",
            "scope_subtitle": "Brak danych po aktualnych filtrach bazy.",
            "summary_cards": [],
            "highlights": ["Brak rekordow do analizy."],
            "problem_items": [],
            "breakdowns": {
                "project_number": [],
                "test_type": [],
                "software_version": [],
                "phone": [],
            },
            "filter_options": {
                "project_number": [],
                "classification": [],
                "test_type": [],
                "sample": [],
                "software_version": [],
                "phone": [],
                "position": [],
                "dual_charging": [],
            },
            "selected_filters": selected_filters,
            "rows_analyzed": 0,
            "rows_total": total_rows,
            "candidate_group_total": 0,
            "included_candidate_group_count": 0,
            "truncated": False,
        }

    sessions = build_sessions(rows)
    annotate_project_analysis_context(sessions)
    assessment_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
    for session in sessions:
        cache_key = build_analysis_session_cache_key(session)
        cached_assessment = assessment_cache.get(cache_key)
        if cached_assessment is None:
            cached_assessment = build_examples_session_assessment(session)
            assessment_cache[cache_key] = cached_assessment
        session["examples_assessment"] = dict(cached_assessment)

    session_by_id = {
        session.get("session_id"): session
        for session in sessions
        if session.get("session_id") is not None
    }
    phone_status_readout: dict[str, bool] = {}
    for session in sessions:
        status_samples = session.get("status_samples") or extract_status_samples(session.get("rows") or [])
        session["status_samples"] = status_samples
        phone_key = str(session.get("phone") or "").strip().lower()
        has_real_status = any(sample["status"] != "N/A" for sample in status_samples)
        phone_status_readout[phone_key] = phone_status_readout.get(phone_key, False) or has_real_status

    all_problem_items = [
        item
        for item in (build_problem_report_item(session) for session in sessions)
        if item is not None
    ]
    filter_options = build_problem_filter_options(all_problem_items)
    filtered_problem_items = [item for item in all_problem_items if passes_problem_report_filters(item, scope)]
    filtered_problem_items.sort(
        key=lambda item: (
            {"defect": 0, "not_ok": 1, "potential_defect": 2}.get(item["classification_key"], 9),
            item["sort_time"],
        )
    )

    problem_chart_by_session_id: dict[Any, dict[str, Any]] = {}
    for item in filtered_problem_items:
        session_id = item.get("session_id")
        if session_id in problem_chart_by_session_id:
            continue

        session = session_by_id.get(session_id)
        if session is None:
            continue

        phone_key = str(session.get("phone") or "").strip().lower()
        session_chart = build_session_status_chart(
            session,
            status_samples=session.get("status_samples") or [],
            phone_has_status_readout=phone_status_readout.get(phone_key, False),
        )
        if session_chart is None:
            session_chart = build_problem_metric_only_chart(session)
        if session_chart is None:
            continue
        problem_chart_by_session_id[session_id] = session_chart

    for item in filtered_problem_items:
        item["session_chart"] = problem_chart_by_session_id.get(item.get("session_id"))

    classification_counts = Counter(item["classification_key"] for item in filtered_problem_items)
    software_counts = Counter(
        item["software_version"]
        for item in filtered_problem_items
        if str(item.get("software_version") or "").strip()
    )
    issue_counts = Counter(
        item["short_reason"]
        for item in filtered_problem_items
        if str(item.get("short_reason") or "").strip()
    )
    top_software = software_counts.most_common(1)
    top_issue = issue_counts.most_common(1)
    dominant_summary = {
        "software_label": top_software[0][0] if top_software else "Brak danych",
        "software_count": top_software[0][1] if top_software else 0,
        "issue_label": top_issue[0][0] if top_issue else "Brak danych",
        "issue_count": top_issue[0][1] if top_issue else 0,
    }
    if counts_complete:
        highlights = [
            (
                f"Po filtrach podstawowych baza zwrocila {total_rows} rekordow. "
                f"Do glebszej analizy pobrano {len(rows)} rekordow z {included_candidate_group_count} grup kandydatow."
            ),
            f"Wykryto {len(filtered_problem_items)} przypadkow problemowych po zastosowaniu filtrow raportu.",
            "Brak danych nie tworzy problemu, jezeli dany parametr nie byl zapisany w bazie dla tego typu testu.",
        ]
    else:
        highlights = [
            (
                f"Szybka analiza pobrala {len(rows)} rekordow z {included_candidate_group_count} grup kandydatow "
                "bez pelnego zliczania calego zakresu."
            ),
            f"Wykryto {len(filtered_problem_items)} przypadkow problemowych po zastosowaniu filtrow raportu.",
            "Brak danych nie tworzy problemu, jezeli dany parametr nie byl zapisany w bazie dla tego typu testu.",
        ]
    if counts_complete and candidate_group_total > included_candidate_group_count:
        highlights.append(
            f"Ze wzgledu na limit uwzgledniono {included_candidate_group_count} z {candidate_group_total} najnowszych grup kandydatow problemowych."
        )
    elif not counts_complete:
        highlights.append(
            "Pelne liczniki zakresu zostaly pominiete, zeby utrzymac szybki czas odpowiedzi raportu."
        )
    if not filtered_problem_items:
        highlights.append("Brak przypadkow spelniajacych aktualne filtry raportu.")

    return {
        "scope_title": "Raport problemow z bazy danych",
        "scope_subtitle": (
            "Widok pokazuje tylko przypadki sklasyfikowane jako DEFECT, NOT OK albo POTENTIAL DEFECT / TO BE VERIFIED."
        ),
        "summary_cards": [
            {
                "label": "Wykryte przypadki",
                "value": str(len(filtered_problem_items)),
                "help": "Liczba spraw pokazanych w raporcie problemow.",
            },
            {
                "label": "DEFECT",
                "value": str(classification_counts.get("defect", 0)),
                "help": "Problemy potwierdzone albo powiazane z Defect ID.",
            },
            {
                "label": "NOT OK",
                "value": str(classification_counts.get("not_ok", 0)),
                "help": "Wyniki niezgodne z oczekiwaniem, ale bez potwierdzonego defektu.",
            },
            {
                "label": "POTENTIAL DEFECT",
                "value": str(classification_counts.get("potential_defect", 0)),
                "help": "Przypadki do weryfikacji zgodnie z kryteriami.",
            },
        ],
        "highlights": highlights,
        "dominant_summary": dominant_summary,
        "problem_items": filtered_problem_items,
        "breakdowns": {
            "project_number": build_problem_breakdown_rows(filtered_problem_items, "project_number"),
            "test_type": build_problem_breakdown_rows(filtered_problem_items, "test_type"),
            "software_version": build_problem_breakdown_rows(filtered_problem_items, "software_version"),
            "phone": build_problem_breakdown_rows(filtered_problem_items, "phone"),
        },
        "filter_options": filter_options,
        "selected_filters": selected_filters,
        "rows_analyzed": len(rows),
        "rows_total": total_rows,
        "counts_complete": counts_complete,
        "candidate_group_total": candidate_group_total,
        "included_candidate_group_count": included_candidate_group_count,
        "truncated": candidate_group_total > included_candidate_group_count if counts_complete else False,
    }


def build_position_analysis(
    rows: list[dict[str, Any]],
    *,
    total_rows: int,
    phone: str,
    project_number: str,
    software_version: str,
) -> dict[str, Any]:
    if not rows:
        return {
            "scope_title": "Analiza pozycji",
            "scope_subtitle": "Brak danych po aktualnych filtrach.",
            "summary_cards": [],
            "highlights": ["Brak wierszy do analizy pozycji."],
            "position_rows": [],
            "event_rows": [],
            "coverage_rows": [],
            "release_rows": [],
            "session_rows": [],
            "rows_analyzed": 0,
            "rows_total": total_rows,
            "truncated": False,
        }

    sessions = build_ranking_sessions(rows)
    add_peer_comparison_flags(sessions)
    build_flagged_sessions(sessions)

    position_groups: dict[str, dict[str, Any]] = {}
    release_groups: dict[tuple[str, str], dict[str, Any]] = {}
    event_rows: list[dict[str, Any]] = []
    flagged_sessions = [
        session
        for session in sessions
        if session["issues"] or session["position_events"] or session["interruptions"] or session["drop_messages"]
    ]

    for session in sessions:
        position_label = str(session.get("position") or "").strip() or "brak"
        group = position_groups.setdefault(
            position_label,
            {
                "label": position_label,
                "session_count": 0,
                "sample_count": 0,
                "eff_values": [],
                "rx_values": [],
                "tx_values": [],
                "rpp_values": [],
                "current_values": [],
                "temperature_values": [],
                "peak_temperature": None,
                "battery_span_values": [],
                "voltage_change_values": [],
                "interrupted_count": 0,
                "warning_session_count": 0,
                "alarm_session_count": 0,
                "clean_count": 0,
                "release_versions": set(),
                "phones": [],
                "projects": [],
                "last_seen": None,
            },
        )
        group["session_count"] += 1
        group["sample_count"] += session.get("sample_count") or 0
        group["phones"].append(session.get("phone"))
        group["projects"].append(session.get("project_number"))
        if session.get("avg_eff") is not None:
            group["eff_values"].append(session["avg_eff"])
        if session.get("avg_rx") is not None:
            group["rx_values"].append(session["avg_rx"])
        if session.get("avg_tx") is not None:
            group["tx_values"].append(session["avg_tx"])
        if session.get("avg_rpp") is not None:
            group["rpp_values"].append(session["avg_rpp"])
        if session.get("avg_current_a") is not None:
            group["current_values"].append(session["avg_current_a"])
        if session.get("avg_temperature") is not None:
            group["temperature_values"].append(session["avg_temperature"])
        if session.get("max_temperature") is not None:
            peak_temperature = session["max_temperature"]
            current_peak = group["peak_temperature"]
            if current_peak is None or peak_temperature > current_peak:
                group["peak_temperature"] = peak_temperature
        if session.get("battery_start") is not None and session.get("battery_end") is not None:
            group["battery_span_values"].append(session["battery_end"] - session["battery_start"])
        if session.get("voltage_start") is not None and session.get("voltage_end") is not None:
            group["voltage_change_values"].append(session["voltage_end"] - session["voltage_start"])
        if session.get("software_version"):
            group["release_versions"].add(session["software_version"])

        has_alarm = session["alarm_event_count"] > 0
        has_warning = session["warning_event_count"] > 0
        has_interruption = bool(session["interruptions"])
        if has_alarm:
            group["alarm_session_count"] += 1
        if has_warning:
            group["warning_session_count"] += 1
        if has_interruption:
            group["interrupted_count"] += 1
        if not has_alarm and not has_warning and not has_interruption and not session["issues"]:
            group["clean_count"] += 1

        end_ts = session.get("end_ts")
        if end_ts is not None and (group["last_seen"] is None or end_ts > group["last_seen"]):
            group["last_seen"] = end_ts

        version_label = str(session.get("software_version") or "").strip() or "brak release"
        release_group = release_groups.setdefault(
            (position_label, version_label),
            {
                "position": position_label,
                "software_version": version_label,
                "session_count": 0,
                "eff_values": [],
                "rpp_values": [],
                "temperature_values": [],
                "alarm_count": 0,
                "warning_count": 0,
                "interruption_count": 0,
                "last_seen": None,
            },
        )
        release_group["session_count"] += 1
        if session.get("avg_eff") is not None:
            release_group["eff_values"].append(session["avg_eff"])
        if session.get("avg_rpp") is not None:
            release_group["rpp_values"].append(session["avg_rpp"])
        if session.get("max_temperature") is not None:
            release_group["temperature_values"].append(session["max_temperature"])
        release_group["alarm_count"] += session["alarm_event_count"]
        release_group["warning_count"] += session["warning_event_count"]
        release_group["interruption_count"] += len(session["interruptions"])
        if end_ts is not None and (release_group["last_seen"] is None or end_ts > release_group["last_seen"]):
            release_group["last_seen"] = end_ts

        event_rows.extend(session["position_events"])

    position_rows = []
    for item in position_groups.values():
        session_count = item["session_count"]
        interruption_rate = item["interrupted_count"] / session_count if session_count else None
        warning_rate = item["warning_session_count"] / session_count if session_count else None
        alarm_rate = item["alarm_session_count"] / session_count if session_count else None
        clean_rate = item["clean_count"] / session_count if session_count else None
        peak_temperature = item["peak_temperature"]
        risk_score = (
            (alarm_rate or 0) * 100
            + (interruption_rate or 0) * 60
            + (warning_rate or 0) * 40
            + max(0.0, (peak_temperature or 0) - 35.0)
        )
        position_rows.append(
            {
                "label": item["label"],
                "session_count": session_count,
                "sample_count": item["sample_count"],
                "median_eff": summarize_metric(item["eff_values"]),
                "median_rx": summarize_metric(item["rx_values"]),
                "median_tx": summarize_metric(item["tx_values"]),
                "median_rpp": summarize_metric(item["rpp_values"]),
                "median_current_a": summarize_metric(item["current_values"]),
                "median_temperature": summarize_metric(item["temperature_values"]),
                "peak_temperature": peak_temperature,
                "median_battery_span": summarize_metric(item["battery_span_values"]),
                "median_voltage_change": summarize_metric(item["voltage_change_values"]),
                "interruption_rate": interruption_rate,
                "warning_rate": warning_rate,
                "alarm_rate": alarm_rate,
                "clean_rate": clean_rate,
                "release_count": len(item["release_versions"]),
                "phone_count": len({value for value in item["phones"] if str(value or "").strip()}),
                "project_count": len({value for value in item["projects"] if str(value or "").strip()}),
                "risk_score": risk_score,
                "last_seen": item["last_seen"],
            }
        )

    for row in position_rows:
        row["median_eff_label"] = format_number(row["median_eff"], suffix="%")
        row["median_rx_label"] = format_number(row["median_rx"])
        row["median_tx_label"] = format_number(row["median_tx"])
        row["median_rpp_label"] = format_number(row["median_rpp"])
        row["median_current_label"] = format_number(row["median_current_a"], suffix=" A")
        row["median_temperature_label"] = format_number(row["median_temperature"], suffix=" C")
        row["peak_temperature_label"] = format_number(row["peak_temperature"], suffix=" C")
        row["battery_span_label"] = format_number(row["median_battery_span"], suffix=" pp")
        row["voltage_change_label"] = format_metric_change(row["median_voltage_change"] or 0.0, " V") if row["median_voltage_change"] is not None else "n/a"
        row["interruption_rate_label"] = format_rate_percent(row["interruption_rate"])
        row["warning_rate_label"] = format_rate_percent(row["warning_rate"])
        row["alarm_rate_label"] = format_rate_percent(row["alarm_rate"])
        row["clean_rate_label"] = format_rate_percent(row["clean_rate"])
        row["risk_score_label"] = format_number(row["risk_score"], digits=0, suffix="/100")
        row["coverage_label"] = (
            f"{row['phone_count']} tel. / {row['project_count']} proj. / {row['release_count']} rel."
        )
        row["last_seen_label"] = format_status_time(row["last_seen"])

    position_rows.sort(
        key=lambda item: (
            -(item["risk_score"] or 0),
            -(item["alarm_rate"] or 0),
            -(item["interruption_rate"] or 0),
            -(item["session_count"] or 0),
            item["label"],
        )
    )
    position_rows = position_rows[:POSITION_ANALYSIS_POSITION_LIMIT]

    coverage_source = [
        ("eff", "eff"),
        ("rx", "RX"),
        ("tx", "TX"),
        ("rpp", "RPP"),
        ("current_a", "Current (A)"),
        ("temperature", "Temperature"),
        ("battery_level", "Battery level"),
        ("voltage_v", "Voltage"),
    ]
    coverage_rows = []
    for field_name, label in coverage_source:
        available_count = sum(1 for row in rows if row.get(field_name) is not None)
        coverage_rows.append(
            {
                "label": label,
                "available_count": available_count,
                "missing_count": len(rows) - available_count,
                "coverage_rate": (available_count / len(rows)) if rows else 0,
                "coverage_rate_label": format_rate_percent((available_count / len(rows)) if rows else 0),
            }
        )

    event_rows.sort(
        key=lambda item: (
            0 if item["severity"] == "alarm" else 1,
            item["start_ts"] is None,
            -(item["start_ts"].timestamp()) if item["start_ts"] is not None else float("-inf"),
        )
    )
    event_rows = event_rows[:POSITION_ANALYSIS_EVENT_LIMIT]
    for event in event_rows:
        event["severity_label"] = "Alarm" if event["severity"] == "alarm" else "Warning"
        if event["event_type"] == "interruption":
            event["event_type_label"] = "Przerwa"
        elif event["event_type"] == "spike":
            event["event_type_label"] = "Skok"
        else:
            event["event_type_label"] = "Spadek"
        event["context_label"] = (
            f"{event['phone']} / {event['project_number']} / {event['charger_name']} / {event['position']}"
        )

    release_rows = []
    for item in release_groups.values():
        release_rows.append(
            {
                "position": item["position"],
                "software_version": item["software_version"],
                "session_count": item["session_count"],
                "median_eff": summarize_metric(item["eff_values"]),
                "median_rpp": summarize_metric(item["rpp_values"]),
                "peak_temperature": max(item["temperature_values"]) if item["temperature_values"] else None,
                "alarm_count": item["alarm_count"],
                "warning_count": item["warning_count"],
                "interruption_count": item["interruption_count"],
                "last_seen": item["last_seen"],
            }
        )
    for row in release_rows:
        row["median_eff_label"] = format_number(row["median_eff"], suffix="%")
        row["median_rpp_label"] = format_number(row["median_rpp"])
        row["peak_temperature_label"] = format_number(row["peak_temperature"], suffix=" C")
        row["last_seen_label"] = format_status_time(row["last_seen"])
    release_rows.sort(
        key=lambda item: (
            item["last_seen"] is None,
            item["last_seen"] or datetime.min,
            item["position"],
            item["software_version"],
        ),
        reverse=True,
    )
    release_rows = release_rows[:POSITION_ANALYSIS_RELEASE_LIMIT]

    session_rows = sorted(
        flagged_sessions,
        key=lambda item: (
            -item["alarm_event_count"],
            -len(item["interruptions"]),
            -item["warning_event_count"],
            -(item["severity_score"] or 0),
            item["end_ts"] or datetime.min,
        ),
        reverse=False,
    )
    session_rows = session_rows[:POSITION_ANALYSIS_SESSION_LIMIT]
    prepared_sessions = []
    for session in session_rows:
        prepared_sessions.append(
            {
                "title": " / ".join(
                    [
                        value
                        for value in [
                            session.get("phone"),
                            session.get("project_number"),
                            session.get("charger_name"),
                            session.get("position"),
                        ]
                        if value
                    ]
                )
                or f"Sesja {session['session_id']}",
                "time_range": format_ts_range(session.get("start_ts"), session.get("end_ts")),
                "severity": "critical" if session["alarm_event_count"] or len(session["interruptions"]) >= 2 else "warn",
                "severity_label": "Wysokie ryzyko" if session["alarm_event_count"] or len(session["interruptions"]) >= 2 else "Do sprawdzenia",
                "summary": (
                    f"alarmy {session['alarm_event_count']} | warningi {session['warning_event_count']} | "
                    f"przerwy {len(session['interruptions'])} | eff {format_number(session['avg_eff'], suffix='%')} | "
                    f"RPP {format_number(session['avg_rpp'])}"
                ),
                "issues": [
                    *session["issues"][:3],
                    *[event["summary"] for event in session["position_events"][:2]],
                ][:5],
            }
        )

    available_metric_labels = [item["label"] for item in coverage_rows if item["available_count"] > 0]
    missing_metric_labels = [item["label"] for item in coverage_rows if item["available_count"] == 0]
    alarm_positions = sum(1 for item in position_rows if (item["alarm_rate"] or 0) > 0)
    interrupted_sessions = sum(1 for session in sessions if session["interruptions"])
    total_alarm_events = sum(session["alarm_event_count"] for session in sessions)
    total_warning_events = sum(session["warning_event_count"] for session in sessions)
    hottest_position = max(position_rows, key=lambda item: item["peak_temperature"] or float("-inf"), default=None)

    summary_cards = [
        {
            "label": "Pozycje w analizie",
            "value": str(len(position_groups)),
            "help": "Unikalne pozycje w aktualnym zakresie filtrow.",
        },
        {
            "label": "Sesje ladowania",
            "value": str(len(sessions)),
            "help": "Sesje wyznaczone po ciaglosci event_ts i kluczu pozycji.",
        },
        {
            "label": "Pozycje z alarmem",
            "value": str(alarm_positions),
            "help": "Pozycje z co najmniej jednym alarmowym skokiem, spadkiem lub przerwa.",
        },
        {
            "label": "Sesje z przerwami",
            "value": str(interrupted_sessions),
            "help": "Sesje, w ktorych pojawila sie duza luka czasowa podczas ladowania.",
        },
        {
            "label": "Alarmy mocy",
            "value": str(total_alarm_events),
            "help": "Laczna liczba alarmowych zdarzen >20% lub ponad prog bezwzgledny.",
        },
        {
            "label": "Warningi mocy",
            "value": str(total_warning_events),
            "help": "Laczna liczba warningow dla zmian 10-20% lub mniejszych, ale istotnych skokow.",
        },
        {
            "label": "Pokrycie danych",
            "value": str(len(available_metric_labels)),
            "help": "Liczba metryk dostepnych w row_json dla aktualnego zbioru.",
        },
        {
            "label": "Najwyzsza temperatura",
            "value": hottest_position["peak_temperature_label"] if hottest_position else "n/a",
            "help": (
                f"Najgoretsza pozycja: {hottest_position['label']}."
                if hottest_position is not None
                else "Brak odczytu temperatury."
            ),
        },
    ]

    scope_title = build_scope_title(
        phone=phone,
        project_number=project_number,
        software_version=software_version,
    )
    highlights = [
        f"Analiza pozycji obejmuje {len(position_groups)} pozycji i {len(sessions)} sesji dla aktualnych filtrow.",
        (
            f"Dostepne metryki z logow: {', '.join(available_metric_labels)}."
            if available_metric_labels
            else "W aktualnych danych brak dodatkowych metryk poza tymi juz policzonymi w widoku."
        ),
    ]
    if position_rows:
        top_position = position_rows[0]
        highlights.append(
            f"Najwyzsze ryzyko ma pozycja {top_position['label']} "
            f"(alarm {top_position['alarm_rate_label']}, przerwy {top_position['interruption_rate_label']}, "
            f"clean {top_position['clean_rate_label']})."
        )
    if total_alarm_events or total_warning_events:
        highlights.append(
            f"Wykryto {total_alarm_events} alarmow i {total_warning_events} warningow dla skokow/spadkow "
            f"wzgledem kolejnych probek."
        )
    if missing_metric_labels:
        highlights.append(
            f"Brak danych dla metryk: {', '.join(missing_metric_labels)}. Zakladka pokaze je automatycznie, "
            "gdy pojawia sie w row_json."
        )

    return {
        "scope_title": scope_title,
        "scope_subtitle": "Analiza pozycji, skokow mocy, przerw i trendow release dla aktualnie wybranych danych.",
        "summary_cards": summary_cards,
        "highlights": highlights,
        "position_rows": position_rows,
        "event_rows": event_rows,
        "coverage_rows": coverage_rows,
        "release_rows": release_rows,
        "session_rows": prepared_sessions,
        "rows_analyzed": len(rows),
        "rows_total": total_rows,
        "truncated": total_rows > len(rows),
    }


def resolve_examples_project(rows: list[dict[str, Any]], project_number: str) -> str:
    examples_benchmarks = load_examples_benchmarks()
    if project_number and project_number in examples_benchmarks:
        return project_number

    projects = sorted(
        {
            str(row.get("project_number") or "").strip()
            for row in rows
            if str(row.get("project_number") or "").strip()
        }
    )
    if len(projects) == 1 and projects[0] in examples_benchmarks:
        return projects[0]
    return ""


def classify_live_position_status(project_number: str, status_code: str) -> tuple[bool, bool]:
    if status_code == "N/A":
        return False, False

    if status_code in {EXPECTED_FINAL_STATUS, PREFERRED_CHARGING_STATUS}:
        return True, False
    return False, False


def build_live_examples_position_rows(rows: list[dict[str, Any]], project_number: str) -> dict[int, dict[str, Any]]:
    grouped: dict[int, dict[str, Any]] = {}

    for row in rows:
        position_number = extract_position_number(row.get("position"))
        if position_number is None:
            continue

        group = grouped.setdefault(
            position_number,
            {
                "position_number": position_number,
                "position_label": str(row.get("position") or "").strip() or f"P{position_number}",
                "total_rows": 0,
                "considered_rows": 0,
                "pass_rows": 0,
                "not_ok_rows": 0,
                "bad_rows": 0,
                "defect_rows": 0,
                "eff_values": [],
                "status_codes": set(),
                "status_counter": Counter(),
            },
        )
        group["total_rows"] += 1

        status_code = normalize_status_code(row.get("hmi_status"))
        if status_code != "N/A":
            group["considered_rows"] += 1
            group["status_codes"].add(status_code)
            group["status_counter"][status_code] += 1
            is_expected, is_defect = classify_live_position_status(project_number, status_code)
            if is_defect:
                group["defect_rows"] += 1
            elif is_expected:
                group["pass_rows"] += 1
            else:
                group["not_ok_rows"] += 1
                group["bad_rows"] += 1
            if is_defect:
                group["bad_rows"] += 1

        eff_value = row.get("eff")
        if eff_value is not None:
            group["eff_values"].append(float(eff_value))

    for group in grouped.values():
        considered_rows = group["considered_rows"]
        group["live_bad_rate"] = (group["bad_rows"] / considered_rows) if considered_rows else None
        group["live_defect_rate"] = (group["defect_rows"] / considered_rows) if considered_rows else None
        group["live_median_eff"] = summarize_metric(group["eff_values"])
        group["live_bad_rate_label"] = format_rate_percent(group["live_bad_rate"])
        group["live_defect_rate_label"] = format_rate_percent(group["live_defect_rate"])
        group["live_median_eff_label"] = format_number(group["live_median_eff"], suffix="%")
        group["status_label"] = ", ".join(
            f"status {code}"
            for code in sorted(group["status_codes"], key=lambda item: (int(item) if item.isdigit() else 999, item))
        ) or "brak statusu"
        group["status_breakdown_label"] = ", ".join(
            f"{code} x{count}"
            for code, count in sorted(
                group["status_counter"].items(),
                key=lambda item: (int(item[0]) if item[0].isdigit() else 999, item[0]),
            )
        ) or "brak statusu"
    return grouped


def build_live_examples_status_rows(rows: list[dict[str, Any]], project_number: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for row in rows:
        status_code = normalize_status_code(row.get("hmi_status"))
        if status_code == "N/A":
            continue

        position_label = str(row.get("position") or "").strip() or "brak"
        phone_label = str(row.get("phone") or "").strip() or "brak telefonu"
        project_label = str(row.get("project_number") or "").strip() or "brak projektu"
        timestamp = parse_optional_datetime(row.get("event_ts"))
        is_expected, is_defect = classify_live_position_status(project_number, status_code)
        verdict_class = "critical" if is_defect else ("ok" if is_expected else "warn")
        verdict_label = "Defect" if is_defect else ("OK" if is_expected else "Not OK")

        group = grouped.setdefault(
            status_code,
            {
                "status_code": status_code,
                "row_count": 0,
                "positions": set(),
                "phones": set(),
                "projects": set(),
                "occurrences": [],
                "verdict_class": verdict_class,
                "verdict_label": verdict_label,
            },
        )
        group["row_count"] += 1
        group["positions"].add(position_label)
        group["phones"].add(phone_label)
        group["projects"].add(project_label)
        if timestamp is not None:
            group["occurrences"].append(
                {
                    "timestamp": timestamp,
                    "position": position_label,
                    "phone": phone_label,
                    "project": project_label,
                }
            )

    status_rows = []
    total_status_rows = sum(group["row_count"] for group in grouped.values())
    for group in grouped.values():
        occurrence_ranges: list[str] = []
        occurrences = sorted(
            group["occurrences"],
            key=lambda item: (item["timestamp"], item["phone"], item["project"], item["position"]),
        )
        active_range: dict[str, Any] | None = None
        for occurrence in occurrences:
            occurrence_key = (occurrence["phone"], occurrence["project"], occurrence["position"])
            if active_range is None:
                active_range = {
                    "key": occurrence_key,
                    "start_ts": occurrence["timestamp"],
                    "end_ts": occurrence["timestamp"],
                }
                continue

            gap_seconds = (occurrence["timestamp"] - active_range["end_ts"]).total_seconds()
            if occurrence_key == active_range["key"] and gap_seconds <= SESSION_BREAK_SECONDS:
                active_range["end_ts"] = occurrence["timestamp"]
                continue

            occurrence_ranges.append(
                format_status_occurrence_range(active_range["start_ts"], active_range["end_ts"])
            )
            active_range = {
                "key": occurrence_key,
                "start_ts": occurrence["timestamp"],
                "end_ts": occurrence["timestamp"],
            }

        if active_range is not None:
            occurrence_ranges.append(
                format_status_occurrence_range(active_range["start_ts"], active_range["end_ts"])
            )

        status_rows.append(
            {
                "status_code": group["status_code"],
                "row_count": group["row_count"],
                "row_rate": (group["row_count"] / total_status_rows) if total_status_rows else None,
                "row_rate_label": format_rate_percent((group["row_count"] / total_status_rows) if total_status_rows else None),
                "positions_label": ", ".join(sorted(group["positions"])) or "brak",
                "phones_label": ", ".join(sorted(group["phones"])) or "brak",
                "projects_label": ", ".join(sorted(group["projects"])) or "brak",
                "time_ranges_label": " | ".join(occurrence_ranges) if occurrence_ranges else "brak czasu",
                "verdict_class": group["verdict_class"],
                "verdict_label": group["verdict_label"],
            }
        )

    status_rows.sort(
        key=lambda item: (
            {"critical": 0, "warn": 1, "ok": 2}.get(item["verdict_class"], 3),
            -(item["row_count"] or 0),
            int(item["status_code"]) if item["status_code"].isdigit() else 999,
            item["status_code"],
        )
    )
    return status_rows


def build_live_examples_scenario_rows(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for session in sessions:
        assessment = session.get("examples_assessment") or {}
        scenario_label = str(assessment.get("scenario_label") or "n/a")
        verdict = str(assessment.get("verdict") or "no_data")

        group = grouped.setdefault(
            scenario_label,
            {
                "scenario_label": scenario_label,
                "session_count": 0,
                "ok_count": 0,
                "not_ok_count": 0,
                "defect_count": 0,
                "no_data_count": 0,
                "status_unavailable_count": 0,
                "positions": set(),
                "status_codes": set(),
            },
        )
        group["session_count"] += 1
        count_key = f"{verdict}_count"
        if count_key in group:
            group[count_key] += 1
        if str(session.get("position") or "").strip():
            group["positions"].add(str(session.get("position")).strip())
        for status_code in session.get("status_codes") or []:
            if status_code and status_code != "N/A":
                group["status_codes"].add(str(status_code))

    scenario_rows = []
    for group in grouped.values():
        considered_sessions = group["ok_count"] + group["not_ok_count"] + group["defect_count"]
        scenario_rows.append(
            {
                "scenario_label": group["scenario_label"],
                "session_count": group["session_count"],
                "position_count": len(group["positions"]),
                "ok_count": group["ok_count"],
                "not_ok_count": group["not_ok_count"],
                "defect_count": group["defect_count"],
                "no_data_count": group["no_data_count"],
                "status_unavailable_count": group["status_unavailable_count"],
                "bad_rate": ((group["not_ok_count"] + group["defect_count"]) / considered_sessions) if considered_sessions else None,
                "bad_rate_label": format_rate_percent(
                    ((group["not_ok_count"] + group["defect_count"]) / considered_sessions) if considered_sessions else None
                ),
                "status_label": ", ".join(
                    f"status {code}"
                    for code in sorted(
                        group["status_codes"],
                        key=lambda item: (int(item) if item.isdigit() else 999, item),
                    )
                ) or "brak statusu",
            }
        )

    scenario_rows.sort(
        key=lambda item: (
            -(item["defect_count"] or 0),
            -(item["not_ok_count"] or 0),
            -(item["session_count"] or 0),
            item["scenario_label"],
        )
    )
    return scenario_rows


def build_live_examples_overview(
    rows: list[dict[str, Any]],
    *,
    sessions: list[dict[str, Any]],
    project_number: str,
) -> dict[str, Any]:
    resolved_project = resolve_examples_project(rows, project_number)
    if not resolved_project:
        projects = sorted(
            {
                str(row.get("project_number") or "").strip()
                for row in rows
                if str(row.get("project_number") or "").strip()
            }
        )
        resolved_project = project_number or (projects[0] if projects else "")

    position_lookup = build_live_examples_position_rows(rows, resolved_project)
    position_session_counts = Counter()
    position_session_verdicts: dict[int, Counter[str]] = defaultdict(Counter)
    session_position_labels: dict[int, str] = {}

    for session in sessions:
        position_number = extract_position_number(session.get("position"))
        if position_number is None:
            continue
        session_position_labels[position_number] = str(session.get("position") or "").strip() or f"P{position_number}"
        position_session_counts[position_number] += 1
        verdict = str(session.get("examples_assessment", {}).get("verdict") or "no_data")
        position_session_verdicts[position_number][verdict] += 1

    position_rows = []
    all_position_numbers = sorted(set(position_lookup) | set(position_session_counts))
    for position_number in all_position_numbers:
        item = position_lookup.get(position_number)
        session_verdicts = position_session_verdicts.get(position_number, Counter())
        position_rows.append(
            {
                "position_number": position_number,
                "position_label": (
                    item["position_label"]
                    if item is not None
                    else session_position_labels.get(position_number, f"P{position_number}")
                ),
                "session_count": position_session_counts.get(position_number, 0),
                "considered_rows": item["considered_rows"] if item is not None else 0,
                "pass_rows": item["pass_rows"] if item is not None else 0,
                "not_ok_rows": item["not_ok_rows"] if item is not None else 0,
                "defect_rows": item["defect_rows"] if item is not None else 0,
                "bad_rows": item["bad_rows"] if item is not None else 0,
                "bad_rate": item["live_bad_rate"] if item is not None else None,
                "defect_rate": item["live_defect_rate"] if item is not None else None,
                "bad_rate_label": item["live_bad_rate_label"] if item is not None else "n/a",
                "defect_rate_label": item["live_defect_rate_label"] if item is not None else "n/a",
                "median_eff_label": item["live_median_eff_label"] if item is not None else "n/a",
                "status_label": item["status_label"] if item is not None else "brak statusu",
                "status_breakdown_label": item["status_breakdown_label"] if item is not None else "brak statusu",
                "ok_session_count": session_verdicts.get("ok", 0),
                "not_ok_session_count": session_verdicts.get("not_ok", 0),
                "defect_session_count": session_verdicts.get("defect", 0),
                "no_data_session_count": session_verdicts.get("no_data", 0),
                "status_unavailable_session_count": session_verdicts.get("status_unavailable", 0),
            }
        )

    position_rows.sort(
        key=lambda item: (
            -(item["defect_rate"] or 0),
            -(item["bad_rate"] or 0),
            -(item["defect_rows"] or 0),
            -(item["not_ok_rows"] or 0),
            item["position_number"],
        )
    )

    status_rows = build_live_examples_status_rows(rows, resolved_project)
    scenario_rows = build_live_examples_scenario_rows(sessions)
    unique_positions = len(position_rows)
    defect_positions = sum(1 for item in position_rows if item["defect_rows"] > 0)
    not_ok_positions = sum(1 for item in position_rows if item["not_ok_rows"] > 0)
    no_data_positions = sum(1 for item in position_rows if item["no_data_session_count"] > 0)
    status_unavailable_positions = sum(1 for item in position_rows if item["status_unavailable_session_count"] > 0)
    problem_positions = sum(
        1
        for item in position_rows
        if item["defect_rows"] > 0 or item["not_ok_rows"] > 0 or item["no_data_session_count"] > 0
    )
    top_position_labels = [item["position_label"] for item in position_rows[:3]]
    top_status_labels = [
        f"{item['status_code']} ({item['row_rate_label']})"
        for item in status_rows[:4]
    ]

    summary_cards = [
        {
            "label": "Projekt",
            "value": resolved_project or "mieszany",
            "help": "Profil regul wykorzystany do interpretacji statusow w aktualnym zakresie.",
        },
        {
            "label": "Pozycje",
            "value": str(problem_positions),
            "help": "Pozycje, w ktorych wykryto problemy: defect, not ok albo no data.",
        },
        {
            "label": "Statusy",
            "value": str(len(status_rows)),
            "help": "Rozne stany HMI zaobserwowane po odfiltrowaniu brakow danych.",
        },
        {
            "label": "Pozycje z defect",
            "value": str(defect_positions),
            "help": "Pozycje, dla ktorych w aktualnej klasyfikacji potwierdzono defect.",
        },
        {
            "label": "Pozycje z not ok",
            "value": str(not_ok_positions),
            "help": "Pozycje z nieoczekiwanymi statusami, ale bez krytycznego faultu.",
        },
        {
            "label": "Pozycje z no data",
            "value": str(no_data_positions),
            "help": "Pozycje, dla ktorych w sesji brakuje HMI mimo wystepowania tego parametru w projekcie.",
        },
        {
            "label": "Pozycje bez HMI",
            "value": str(status_unavailable_positions),
            "help": "Pozycje nalezace do projektow, ktore w aktualnym zakresie nie raportuja hmi_status.",
        },
    ]

    highlights = [
        f"Zbiorczy widok obejmuje {unique_positions} pozycji i {len(sessions)} sesji.",
        (
            f"Najwiecej ryzyka w aktualnych danych maja pozycje: "
            f"{', '.join(top_position_labels)}."
            if position_rows
            else "Brak pozycji z rozpoznanym statusem."
        ),
        (
            f"Najczestsze statusy: {', '.join(top_status_labels)}."
            if status_rows
            else "Brak statusow HMI do zestawienia."
        ),
    ]

    return {
        "project_number": resolved_project or "mieszany",
        "summary": "Widok zbiorczy inspirowany arkuszami Examples: pozycje, statusy i scenariusze sa liczone na jednym zestawie danych, bez porownania do benchmarku.",
        "summary_cards": summary_cards,
        "highlights": highlights,
        "position_rows": position_rows,
        "status_rows": status_rows,
        "scenario_rows": scenario_rows,
    }


def build_examples_benchmark_analysis(
    rows: list[dict[str, Any]],
    *,
    phone: str,
    project_number: str,
    software_version: str,
    total_rows: int,
    rows_analyzed: int,
    session_summary: dict[str, int] | None,
) -> dict[str, Any] | None:
    benchmark_project = resolve_examples_project(rows, project_number)
    if not benchmark_project:
        return None

    benchmarks = load_examples_benchmarks()
    benchmark = benchmarks.get(benchmark_project)
    if benchmark is None:
        return None
    project_rule = build_project_rule_overview(
        project_number=benchmark_project,
        benchmark=benchmark,
        is_mixed_scope=False,
    )

    live_position_rows = build_live_examples_position_rows(rows, benchmark_project)
    benchmark_position_lookup = {row["position_number"]: row for row in benchmark.get("position_rows", [])}
    comparison_rows = []

    for position_number in sorted(set(benchmark_position_lookup) | set(live_position_rows)):
        benchmark_position = benchmark_position_lookup.get(position_number)
        live_position = live_position_rows.get(position_number)
        if benchmark_position is None and live_position is None:
            continue

        benchmark_bad_rate = benchmark_position["bad_rate"] if benchmark_position is not None else None
        benchmark_defect_rate = benchmark_position["defect_rate"] if benchmark_position is not None else None
        live_bad_rate = live_position["live_bad_rate"] if live_position is not None else None
        live_defect_rate = live_position["live_defect_rate"] if live_position is not None else None

        if benchmark_bad_rate is None or live_bad_rate is None:
            verdict_class = "neutral"
            verdict_label = "Brak pelnego porownania"
        else:
            bad_delta = live_bad_rate - benchmark_bad_rate
            defect_delta = (live_defect_rate or 0.0) - (benchmark_defect_rate or 0.0)
            if defect_delta > 0.03 or bad_delta > 0.12:
                verdict_class = "critical"
                verdict_label = "Telemetria gorsza"
            elif bad_delta > 0.05:
                verdict_class = "warn"
                verdict_label = "Telemetria lekko gorsza"
            elif bad_delta < -0.08:
                verdict_class = "ok"
                verdict_label = "Telemetria lepsza"
            else:
                verdict_class = "neutral"
                verdict_label = "Zblizone ryzyko"

        scenario_bits = []
        for scenario_row in benchmark.get("scenario_rows", []):
            position_group = scenario_row.get("position_groups", {}).get(position_number)
            if not position_group:
                continue
            considered = (
                position_group.get("pass", 0)
                + position_group.get("not_ok", 0)
                + position_group.get("defect", 0)
            )
            if considered <= 0:
                continue
            bad_rate = (position_group.get("not_ok", 0) + position_group.get("defect", 0)) / considered
            if bad_rate <= 0:
                continue
            scenario_bits.append((bad_rate, f"{scenario_row['scenario_label']} {format_rate_percent(bad_rate)}"))
        scenario_bits.sort(key=lambda item: item[0], reverse=True)

        comparison_rows.append(
            {
                "position_number": position_number,
                "position_label": live_position["position_label"] if live_position is not None else f"P{position_number}",
                "benchmark_bad_rate": benchmark_bad_rate,
                "benchmark_bad_rate_label": format_rate_percent(benchmark_bad_rate),
                "benchmark_defect_rate_label": format_rate_percent(benchmark_defect_rate),
                "benchmark_scenario_count": benchmark_position["scenario_count"] if benchmark_position is not None else 0,
                "live_bad_rate_label": live_position["live_bad_rate_label"] if live_position is not None else "n/a",
                "live_defect_rate_label": live_position["live_defect_rate_label"] if live_position is not None else "n/a",
                "live_median_eff_label": live_position["live_median_eff_label"] if live_position is not None else "n/a",
                "status_label": live_position["status_label"] if live_position is not None else "brak telemetrii",
                "verdict_class": verdict_class,
                "verdict_label": verdict_label,
                "scenario_focus_label": ", ".join(item[1] for item in scenario_bits[:3]) or "brak problemow w benchmarku",
            }
        )

    comparison_rows.sort(
        key=lambda item: (
            {"critical": 0, "warn": 1, "neutral": 2, "ok": 3}.get(item["verdict_class"], 4),
            -(item["benchmark_bad_rate"] or 0),
            item["position_number"],
        )
    )

    scenario_rows = []
    for scenario_row in benchmark.get("scenario_rows", []):
        scenario_rows.append(
            {
                "scenario_label": scenario_row["scenario_label"],
                "file_count": scenario_row["file_count"],
                "pass_count": scenario_row["pass_count"],
                "not_ok_count": scenario_row["not_ok_count"],
                "defect_count": scenario_row["defect_count"],
                "skip_count": scenario_row["skip_count"],
                "position_count": scenario_row["position_count"],
                "bad_rate_label": format_rate_percent(scenario_row["bad_rate"]),
                "defect_rate_label": format_rate_percent(scenario_row["defect_rate"]),
                "dominant_statuses_label": scenario_row.get("dominant_statuses_label", "brak"),
                "reference_note": scenario_row.get("reference_note", ""),
            }
        )

    summary_cards = [
        {
            "label": "Benchmark project",
            "value": benchmark_project,
            "help": "Projekt z workbookow Examples dopasowany do aktualnego zakresu analizy.",
        },
        {
            "label": "Workbooki",
            "value": str(len(benchmark.get("files", []))),
            "help": "Liczba plikow xlsx wykorzystanych jako benchmark recznej oceny.",
        },
        {
            "label": "Scenariusze",
            "value": str(len(scenario_rows)),
            "help": "Scenariusze testowe odczytane z arkuszy Examples.",
        },
        {
            "label": "Pozycje porownane",
            "value": str(len(comparison_rows)),
            "help": "Liczba pozycji, dla ktorych zestawiono benchmark manualny z telemetria.",
        },
    ]
    if session_summary:
        summary_cards.extend(
            [
                {
                    "label": "Sesje OK",
                    "value": str(session_summary.get("ok", 0)),
                    "help": "Aktualne sesje zgodne z benchmarkiem Examples.",
                },
                {
                    "label": "Sesje Not OK",
                    "value": str(session_summary.get("not_ok", 0)),
                    "help": "Aktualne sesje z odchyleniami od wzorca Examples.",
                },
                {
                    "label": "Sesje Defect",
                    "value": str(session_summary.get("defect", 0)),
                    "help": "Aktualne sesje z krytycznymi statusami wedlug benchmarku Examples.",
                },
            ]
        )

    insights = [
        (
            f"Benchmark Examples dla projektu {benchmark_project} obejmuje {len(benchmark.get('files', []))} workbooki "
            f"i {len(scenario_rows)} scenariusze testowe."
        ),
        (
            f"Profil regul dla tego projektu skupia sie na danych: {project_rule['focus_metrics_label']}."
        ),
        (
            f"Do porownania telemetrycznego wykorzystano {rows_analyzed} z {total_rows} wierszy po filtrach. "
            "Sekcja pozycji ma szerszy zakres niz sam status timeline."
        ),
    ]
    if phone or software_version:
        insights.append(
            "Benchmark jest scenariuszowy i projektowy, dlatego przy dodatkowym filtrowaniu po telefonie albo release "
            "trzeba czytac go jako punkt odniesienia, a nie literalny expected result dla kazdej probki."
        )
    if comparison_rows:
        top_mismatch = comparison_rows[0]
        insights.append(
            f"Najmocniejszy rozjazd telemetryczny ma {top_mismatch['position_label']}: "
            f"{top_mismatch['verdict_label'].lower()} vs benchmark Examples."
        )

    scope_title = build_scope_title(
        phone=phone,
        project_number=project_number or benchmark_project,
        software_version=software_version,
    )
    return {
        "scope_title": scope_title,
        "scope_subtitle": "Benchmark recznych ocen z workbookow Examples zestawiony z biezaca telemetria.",
        "summary_cards": summary_cards,
        "criteria_rows": project_rule["rule_rows"],
        "scenario_rows": scenario_rows,
        "comparison_rows": comparison_rows,
        "insights": insights,
        "source_files": benchmark.get("files", []),
        "notes": project_rule["notes"],
    }


def normalize_metric(value: float | None, minimum: float | None, maximum: float | None) -> float | None:
    if value is None or minimum is None or maximum is None:
        return None
    if abs(maximum - minimum) < 1e-9:
        return 1.0
    return max(0.0, min(1.0, (value - minimum) / (maximum - minimum)))


def build_group_rankings(
    sessions: list[dict[str, Any]],
    *,
    key_name: str,
    title: str,
    subtitle: str,
) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}

    for session in sessions:
        raw_label = str(session.get(key_name) or "").strip()
        label = raw_label or "brak danych"
        group = grouped.setdefault(
            label,
            {
                "label": label,
                "eff_values": [],
                "rx_values": [],
                "tx_values": [],
                "session_count": 0,
                "sample_count": 0,
                "clean_count": 0,
                "interrupted_count": 0,
                "drop_count": 0,
                "peer_issue_count": 0,
                "release_versions": set(),
                "last_seen": None,
            },
        )
        group["session_count"] += 1
        group["sample_count"] += session.get("sample_count") or 0

        if session.get("avg_eff") is not None:
            group["eff_values"].append(session["avg_eff"])
        if session.get("avg_rx") is not None:
            group["rx_values"].append(session["avg_rx"])
        if session.get("avg_tx") is not None:
            group["tx_values"].append(session["avg_tx"])

        if session.get("software_version"):
            group["release_versions"].add(session["software_version"])

        has_interruptions = bool(session.get("interruptions"))
        has_drops = bool(session.get("drop_messages"))
        has_peer_issues = bool(session.get("issues"))
        if has_interruptions:
            group["interrupted_count"] += 1
        if has_drops:
            group["drop_count"] += 1
        if has_peer_issues:
            group["peer_issue_count"] += 1
        if not has_interruptions and not has_drops and not has_peer_issues:
            group["clean_count"] += 1

        end_ts = session.get("end_ts")
        if end_ts is not None and (group["last_seen"] is None or end_ts > group["last_seen"]):
            group["last_seen"] = end_ts

    rows = []
    for item in grouped.values():
        session_count = item["session_count"]
        rows.append(
            {
                "label": item["label"],
                "session_count": session_count,
                "sample_count": item["sample_count"],
                "median_eff": summarize_metric(item["eff_values"]),
                "median_rx": summarize_metric(item["rx_values"]),
                "median_tx": summarize_metric(item["tx_values"]),
                "clean_rate": item["clean_count"] / session_count if session_count else None,
                "interruption_rate": item["interrupted_count"] / session_count if session_count else None,
                "drop_rate": item["drop_count"] / session_count if session_count else None,
                "peer_issue_rate": item["peer_issue_count"] / session_count if session_count else None,
                "release_count": len(item["release_versions"]),
                "last_seen": item["last_seen"],
            }
        )

    eligible_rows = [row for row in rows if row["session_count"] >= RANKING_MIN_SESSION_COUNT]
    score_source = eligible_rows or rows
    metric_ranges: dict[str, tuple[float | None, float | None]] = {}
    for metric_name in RANKING_SCORE_WEIGHTS:
        values = [row[metric_name] for row in score_source if row.get(metric_name) is not None]
        metric_ranges[metric_name] = (
            min(values) if values else None,
            max(values) if values else None,
        )

    for row in rows:
        weighted_score = 0.0
        total_weight = 0.0
        for metric_name, weight in RANKING_SCORE_WEIGHTS.items():
            minimum, maximum = metric_ranges[metric_name]
            normalized = normalize_metric(row.get(metric_name), minimum, maximum)
            if normalized is None:
                continue
            weighted_score += normalized * weight
            total_weight += weight

        decision_score = (weighted_score / total_weight * 100) if total_weight else None
        row["decision_score"] = decision_score
        row["decision_score_label"] = format_number(decision_score, suffix="/100")
        row["median_eff_label"] = format_number(row["median_eff"], suffix="%")
        row["median_rx_label"] = format_number(row["median_rx"])
        row["median_tx_label"] = format_number(row["median_tx"])
        row["clean_rate_label"] = format_rate_percent(row["clean_rate"])
        row["interruption_rate_label"] = format_rate_percent(row["interruption_rate"])
        row["drop_rate_label"] = format_rate_percent(row["drop_rate"])
        row["support_label"] = (
            f"{row['session_count']} sesji / {row['sample_count']} probek / "
            f"{row['release_count']} releasow"
        )

    rows.sort(
        key=lambda item: (
            item["decision_score"] is None,
            -(item["decision_score"] or -1),
            -item["session_count"],
            -item["sample_count"],
            item["label"],
        )
    )

    return {
        "title": title,
        "subtitle": subtitle,
        "all_rows": rows,
        "rows": rows[:RANKING_TOP_LIMIT],
        "eligible_count": len(eligible_rows),
    }


def build_pair_rankings(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    paired_sessions = []
    for session in sessions:
        phone = str(session.get("phone") or "").strip() or "brak telefonu"
        project_number = str(session.get("project_number") or "").strip() or "brak projektu"
        paired_session = dict(session)
        paired_session["pair_label"] = f"{phone} / {project_number}"
        paired_sessions.append(paired_session)

    return build_group_rankings(
        paired_sessions,
        key_name="pair_label",
        title="Ranking konfiguracji telefon / projekt",
        subtitle=(
            "Najmocniejsze zestawy z punktu widzenia aktualnych danych ladowania, "
            "sprawnosci i stabilnosci sesji."
        ),
    )


def build_release_ranking(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}

    for session in sessions:
        version = str(session.get("software_version") or "").strip()
        avg_eff = session.get("avg_eff")
        if not version or avg_eff is None:
            continue

        item = grouped.setdefault(
            version,
            {
                "label": version,
                "eff_values": [],
                "session_count": 0,
                "sample_count": 0,
            },
        )
        item["eff_values"].append(avg_eff)
        item["session_count"] += 1
        item["sample_count"] += session.get("sample_count") or 0

    rows = []
    for item in grouped.values():
        rows.append(
            {
                "label": item["label"],
                "session_count": item["session_count"],
                "sample_count": item["sample_count"],
                "median_eff": summarize_metric(item["eff_values"]),
            }
        )

    rows = [
        row for row in rows if row["session_count"] >= RANKING_MIN_SESSION_COUNT and row["median_eff"] is not None
    ]
    rows.sort(
        key=lambda item: (
            -(item["median_eff"] or -1),
            -item["session_count"],
            item["label"],
        )
    )

    for row in rows:
        row["value_label"] = format_number(row["median_eff"], suffix="%")
        row["support_label"] = f"{row['session_count']} sesji / {row['sample_count']} probek"

    return rows[:RANKING_TOP_LIMIT]


def build_metric_toplist(
    rows: list[dict[str, Any]],
    *,
    title: str,
    subtitle: str,
    field_name: str,
    higher_is_better: bool,
    formatter: Any,
) -> dict[str, Any]:
    eligible_rows = [row for row in rows if row["session_count"] >= RANKING_MIN_SESSION_COUNT and row.get(field_name) is not None]
    source_rows = eligible_rows or [row for row in rows if row.get(field_name) is not None]
    source_rows = sorted(
        source_rows,
        key=lambda item: (
            item[field_name] if higher_is_better else -item[field_name],
            item["session_count"],
            item["sample_count"],
        ),
        reverse=True,
    )

    items = []
    for index, row in enumerate(source_rows[:5], start=1):
        items.append(
            {
                "position": index,
                "label": row["label"],
                "value_label": formatter(row.get(field_name)),
                "support_label": row["support_label"],
                "detail": (
                    f"eff {row['median_eff_label']} | RX {row['median_rx_label']} | "
                    f"TX {row['median_tx_label']} | clean {row['clean_rate_label']}"
                ),
            }
        )

    return {
        "title": title,
        "subtitle": subtitle,
        "items": items,
    }


def build_decision_ranking(
    rows: list[dict[str, Any]],
    *,
    total_rows: int,
    phone: str,
    project_number: str,
    software_version: str,
) -> dict[str, Any]:
    scope_title = build_scope_title(
        phone=phone,
        project_number=project_number,
        software_version=software_version,
    )
    scope_title = f"Ranking dla {scope_title.lower()}" if scope_title != "Aktualne filtry" else "Ranking dla aktualnych filtrow"

    if not rows:
        return {
            "scope_title": scope_title,
            "scope_subtitle": "Brak danych po aktualnych filtrach do zbudowania rankingu.",
            "leader_cards": [],
            "group_sections": [],
            "metric_sections": [],
            "release_rows": [],
            "future_criteria": FUTURE_DECISION_CRITERIA,
            "score_formula": "",
            "highlights": ["Brak wierszy do zbudowania rankingu."],
            "rows_analyzed": 0,
            "rows_total": total_rows,
            "truncated": False,
        }

    sessions = build_sessions(rows)
    add_peer_comparison_flags(sessions)

    phone_rankings = build_group_rankings(
        sessions,
        key_name="phone",
        title="Ranking telefonow",
        subtitle=(
            "Ranking ogolny oparty na medianie eff, RX, TX oraz odsetku czystych, "
            "stabilnych sesji bez anomalii."
        ),
    )
    project_rankings = build_group_rankings(
        sessions,
        key_name="project_number",
        title="Ranking projektow",
        subtitle=(
            "Porownanie projektow po aktualnych danych, z naciskiem na sprawnosc "
            "i przewidywalnosc zachowania w sesjach ladowania."
        ),
    )
    pair_rankings = build_pair_rankings(sessions)
    release_rows = build_release_ranking(sessions)

    metric_sections = [
        build_metric_toplist(
            phone_rankings["all_rows"],
            title="Najwyzsza mediana eff",
            subtitle="Telefony, ktore najlepiej zamieniaja energie na aktualnym zbiorze danych.",
            field_name="median_eff",
            higher_is_better=True,
            formatter=lambda value: format_number(value, suffix="%"),
        ),
        build_metric_toplist(
            phone_rankings["all_rows"],
            title="Najwyzsza mediana RX",
            subtitle="Telefony z najlepszym przyjmowaniem mocy.",
            field_name="median_rx",
            higher_is_better=True,
            formatter=format_number,
        ),
        build_metric_toplist(
            project_rankings["all_rows"],
            title="Najwyzsza mediana TX",
            subtitle="Projekty, w ktorych ladowarka oddaje najwiecej mocy.",
            field_name="median_tx",
            higher_is_better=True,
            formatter=format_number,
        ),
        build_metric_toplist(
            pair_rankings["all_rows"],
            title="Najwyzsza stabilnosc sesji",
            subtitle="Konfiguracje z najwiekszym odsetkiem czystych sesji bez przerw i dropow.",
            field_name="clean_rate",
            higher_is_better=True,
            formatter=format_rate_percent,
        ),
        {
            "title": "Najlepsze releasy po eff",
            "subtitle": "Software version z najlepsza mediana eff dla releasow z co najmniej 2 sesjami.",
            "items": [
                {
                    "position": index,
                    "label": row["label"],
                    "value_label": row["value_label"],
                    "support_label": row["support_label"],
                    "detail": "Ranking releasow oparty o mediane avg_eff w sesjach.",
                }
                for index, row in enumerate(release_rows[:5], start=1)
            ],
        },
    ]

    leader_cards = []
    if phone_rankings["rows"]:
        best_phone = phone_rankings["rows"][0]
        leader_cards.append(
            {
                "label": "Lider telefonow",
                "value": best_phone["label"],
                "help": (
                    f"Score {best_phone['decision_score_label']} | eff {best_phone['median_eff_label']} | "
                    f"clean {best_phone['clean_rate_label']}"
                ),
            }
        )
    if project_rankings["rows"]:
        best_project = project_rankings["rows"][0]
        leader_cards.append(
            {
                "label": "Lider projektow",
                "value": best_project["label"],
                "help": (
                    f"Score {best_project['decision_score_label']} | RX {best_project['median_rx_label']} | "
                    f"TX {best_project['median_tx_label']}"
                ),
            }
        )
    if pair_rankings["rows"]:
        best_pair = pair_rankings["rows"][0]
        leader_cards.append(
            {
                "label": "Najmocniejsza konfiguracja",
                "value": best_pair["label"],
                "help": (
                    f"Score {best_pair['decision_score_label']} | przerwy {best_pair['interruption_rate_label']} | "
                    f"dropy {best_pair['drop_rate_label']}"
                ),
            }
        )
    if release_rows:
        leader_cards.append(
            {
                "label": "Najlepszy release",
                "value": release_rows[0]["label"],
                "help": f"Mediana eff {release_rows[0]['value_label']} przy {release_rows[0]['support_label']}.",
            }
        )

    highlights = []
    if phone_rankings["rows"]:
        highlights.append(
            f"Najlepszy telefon w calej bazie to {phone_rankings['rows'][0]['label']} "
            f"ze score {phone_rankings['rows'][0]['decision_score_label']}."
        )
    if pair_rankings["rows"]:
        highlights.append(
            f"Najstabilniejsza konfiguracja to {pair_rankings['rows'][0]['label']} "
            f"(clean {pair_rankings['rows'][0]['clean_rate_label']})."
        )
    if release_rows:
        highlights.append(
            f"Najmocniejszy release po eff to {release_rows[0]['label']} "
            f"z mediana {release_rows[0]['value_label']}."
        )
    if total_rows > len(rows):
        highlights.append(
            f"Ranking wykorzystuje {len(rows)} z {total_rows} wierszy calej bazy, "
            "wiec czesc danych nie weszla do zestawienia."
        )

    return {
        "scope_title": scope_title,
        "scope_subtitle": (
            "Ranking jest liczony z tego samego zestawu filtrow co zakladka Wyniki "
            f"na podstawie danych z {SOURCE_RELATION}."
        ),
        "leader_cards": leader_cards,
        "group_sections": [phone_rankings, project_rankings, pair_rankings],
        "metric_sections": metric_sections,
        "release_rows": release_rows,
        "future_criteria": FUTURE_DECISION_CRITERIA,
        "score_formula": (
            "Wynik decyzji = 40% mediana eff + 20% mediana RX + 15% mediana TX + "
            "25% odsetek czystych sesji. Wynik jest normalizowany wewnatrz calej bazy."
        ),
        "highlights": highlights,
        "rows_analyzed": len(rows),
        "rows_total": total_rows,
        "truncated": total_rows > len(rows),
    }


def get_ranking_snapshot_now() -> datetime:
    return datetime.now(ZoneInfo(RANKING_SNAPSHOT_TIMEZONE))


def build_reliability_score_help() -> dict[str, Any]:
    formula = (
        "Reliability score = 100 - 60% * DEFECT rate - 25% * NOT OK rate - "
        "10% * warning rate - 5% * (1 - status coverage)."
    )
    return {
        "formula": formula,
        "intro": (
            "Score startuje od 100 i odejmuje kary za sesje problemowe oraz za brak "
            "odczytu statusu. Im mniej DEFECT, NOT OK i warningow oraz im wyzsze "
            "coverage statusu, tym wyzszy wynik."
        ),
        "components": [
            {
                "label": "DEFECT rate",
                "details": (
                    "To defect_count / session_count. Najciezsza kara w score. Sesja "
                    "wpada do DEFECT, gdy finalny verdict zostal oznaczony jako defect, "
                    "na przyklad przez explicit defect ID albo eskalacje NOT OK po "
                    "wielu przerwach lub dropach."
                ),
            },
            {
                "label": "NOT OK rate",
                "details": (
                    "To not_ok_count / session_count. Oznacza sesje niezgodne z "
                    "oczekiwaniem, ale bez potwierdzonego defectu. Typowe zrodla to "
                    "nieoczekiwane statusy, manual_result zawierajacy NOT OK albo sesja "
                    "pierwotnie OK, ktora dostala przerwy lub dropy."
                ),
            },
            {
                "label": "Warning rate",
                "details": (
                    "To warning_count / session_count. To przypadki do weryfikacji: "
                    "status lub przebieg odbiega od wzorca, ale nie na tyle mocno, aby "
                    "podniesc verdict do NOT OK albo DEFECT."
                ),
            },
            {
                "label": "Status coverage",
                "details": (
                    "To covered_session_count / session_count. Sesja jest counted jako "
                    "covered, jesli ma co najmniej jeden realny hmi_status rozny od N/A. "
                    "Brak statusu nie podbija sam z siebie DEFECT, NOT OK ani Warning, "
                    "ale obniza score przez osobna kare 5% * (1 - coverage)."
                ),
            },
            {
                "label": "Clean rate",
                "details": (
                    "To clean_count / session_count. Clean oznacza verdict OK oraz brak "
                    "interruptions i brak drop_messages. Clean rate nie wchodzi do "
                    "samego wzoru score, ale pomaga interpretowac jakosc i sluzy jako "
                    "jeden z tie-breakerow."
                ),
            },
        ],
        "verdict_sources": [
            "Klasyfikacja sesji wynika z pol statusowych i kontekstu testu: status_codes, scenario_hint, manual_result, defect_id, defect_comment, interruptions oraz drop_issue_count.",
            "Sesja moze zostac zdegradowana z OK do NOT OK przez przerwy lub dropy.",
            "Sesja moze zostac podniesiona z NOT OK do DEFECT przez explicit defect reference albo przez >= 2 interruptions lub >= 2 drop issues.",
        ],
        "eligibility": [
            (
                "Do normalnego wejscia do rankingu grupa musi miec minimalna liczbe sesji "
                f"oraz co najmniej {format_rate_percent(RANKING_SNAPSHOT_MIN_STATUS_COVERAGE)} "
                "status coverage."
            ),
            (
                "Progi sesji: telefon >= "
                f"{RANKING_SNAPSHOT_MIN_PHONE_SESSIONS}, projekt >= "
                f"{RANKING_SNAPSHOT_MIN_PROJECT_SESSIONS}, software w projekcie >= "
                f"{RANKING_SNAPSHOT_MIN_PROJECT_SOFTWARE_SESSIONS}, software globalnie >= "
                f"{RANKING_SNAPSHOT_MIN_GLOBAL_SOFTWARE_SESSIONS}."
            ),
            (
                "Jesli nic nie spelnia progow, widok pokazuje fallbackowo wszystkie "
                "wiersze, ale nadal sortowane tym samym porzadkiem."
            ),
        ],
        "sorting": [
            "Najpierw rekordy eligible, potem wyzszy reliability score.",
            "Przy remisie: nizszy DEFECT rate, potem nizszy NOT OK rate, potem nizszy warning rate.",
            "Dalej: wyzszy clean rate, potem wieksza liczba sesji, a na koncu nazwa alfabetycznie.",
        ],
        "supporting_metrics": (
            "eff, RX i TX sa metrykami wspierajacymi. Pokazuja mediany zachowania sesji "
            "w danej grupie i pomagaja interpretowac wynik, ale nie sa skladnikami score "
            "ani nie decyduja o kolejnosci rankingu."
        ),
    }


def session_has_status_readout(session: dict[str, Any]) -> bool:
    return any(code != "N/A" for code in (session.get("status_codes") or []))


def session_is_clean(session: dict[str, Any]) -> bool:
    assessment = session.get("examples_assessment") or {}
    return (
        assessment.get("verdict") == "ok"
        and not session.get("interruptions")
        and not session.get("drop_messages")
    )


def build_reliability_support_label(
    row: dict[str, Any],
    *,
    dimensions: tuple[str, ...],
) -> str:
    parts = [f"{row['session_count']} sesji"]
    labels = {
        "project_number": "projektow",
        "software_version": "releasow",
        "phone": "telefonow",
    }
    for field_name in dimensions:
        count = row.get(f"{field_name}_count")
        if count:
            parts.append(f"{count} {labels[field_name]}")
    return " / ".join(parts)


def build_reliability_group_section(
    sessions: list[dict[str, Any]],
    *,
    title: str,
    subtitle: str,
    group_key_builder: Any,
    label_builder: Any,
    minimum_session_count: int,
    dimension_fields: tuple[str, ...] = (),
    metadata_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    grouped: dict[Any, dict[str, Any]] = {}

    for session in sessions:
        key = group_key_builder(session)
        label = label_builder(session) or "brak danych"
        group = grouped.setdefault(
            key,
            {
                "label": label,
                "session_count": 0,
                "covered_session_count": 0,
                "defect_count": 0,
                "not_ok_count": 0,
                "warning_count": 0,
                "ok_count": 0,
                "other_count": 0,
                "clean_count": 0,
                "eff_values": [],
                "rx_values": [],
                "tx_values": [],
                "last_seen": None,
                "dimensions": {field_name: set() for field_name in dimension_fields},
                "metadata": {
                    field_name: str(session.get(field_name) or "").strip()
                    for field_name in metadata_fields
                },
            },
        )
        group["session_count"] += 1
        if session_has_status_readout(session):
            group["covered_session_count"] += 1
        if session_is_clean(session):
            group["clean_count"] += 1

        assessment = session.get("examples_assessment") or {}
        verdict = str(assessment.get("verdict") or "other").strip().lower()
        if verdict == "defect":
            group["defect_count"] += 1
        elif verdict == "not_ok":
            group["not_ok_count"] += 1
        elif verdict == "warning":
            group["warning_count"] += 1
        elif verdict == "ok":
            group["ok_count"] += 1
        else:
            group["other_count"] += 1

        if session.get("avg_eff") is not None:
            group["eff_values"].append(session["avg_eff"])
        if session.get("avg_rx") is not None:
            group["rx_values"].append(session["avg_rx"])
        if session.get("avg_tx") is not None:
            group["tx_values"].append(session["avg_tx"])

        end_ts = session.get("end_ts")
        if end_ts is not None and (group["last_seen"] is None or end_ts > group["last_seen"]):
            group["last_seen"] = end_ts

        for field_name in dimension_fields:
            normalized = str(session.get(field_name) or "").strip()
            if normalized:
                group["dimensions"][field_name].add(normalized)

    rows: list[dict[str, Any]] = []
    for item in grouped.values():
        session_count = item["session_count"]
        status_coverage = item["covered_session_count"] / session_count if session_count else 0.0
        defect_rate = item["defect_count"] / session_count if session_count else 0.0
        not_ok_rate = item["not_ok_count"] / session_count if session_count else 0.0
        warning_rate = item["warning_count"] / session_count if session_count else 0.0
        clean_rate = item["clean_count"] / session_count if session_count else 0.0
        reliability_score = max(
            0.0,
            100.0 - (
                60.0 * defect_rate
                + 25.0 * not_ok_rate
                + 10.0 * warning_rate
                + 5.0 * (1.0 - status_coverage)
            ),
        )
        row = {
            "label": item["label"],
            "session_count": session_count,
            "status_coverage": status_coverage,
            "defect_rate": defect_rate,
            "not_ok_rate": not_ok_rate,
            "warning_rate": warning_rate,
            "clean_rate": clean_rate,
            "median_eff": summarize_metric(item["eff_values"]),
            "median_rx": summarize_metric(item["rx_values"]),
            "median_tx": summarize_metric(item["tx_values"]),
            "reliability_score": reliability_score,
            "last_seen_label": item["last_seen"].strftime("%Y-%m-%d %H:%M") if item["last_seen"] else "brak",
            "eligible": (
                session_count >= minimum_session_count
                and status_coverage >= RANKING_SNAPSHOT_MIN_STATUS_COVERAGE
            ),
        }
        row.update(item["metadata"])
        for field_name in dimension_fields:
            row[f"{field_name}_count"] = len(item["dimensions"][field_name])
        row["reliability_score_label"] = format_number(reliability_score, suffix="/100")
        row["status_coverage_label"] = format_rate_percent(status_coverage)
        row["defect_rate_label"] = format_rate_percent(defect_rate)
        row["not_ok_rate_label"] = format_rate_percent(not_ok_rate)
        row["warning_rate_label"] = format_rate_percent(warning_rate)
        row["clean_rate_label"] = format_rate_percent(clean_rate)
        row["median_eff_label"] = format_number(row["median_eff"], suffix="%")
        row["median_rx_label"] = format_number(row["median_rx"])
        row["median_tx_label"] = format_number(row["median_tx"])
        row["support_label"] = build_reliability_support_label(row, dimensions=dimension_fields)
        rows.append(row)

    rows.sort(
        key=lambda item: (
            not item["eligible"],
            -(item["reliability_score"] or -1),
            item["defect_rate"],
            item["not_ok_rate"],
            item["warning_rate"],
            -(item["clean_rate"] or -1),
            -item["session_count"],
            item["label"],
        )
    )
    display_rows = [row for row in rows if row["eligible"]] or rows
    return {
        "title": title,
        "subtitle": subtitle,
        "rows": display_rows[:RANKING_TOP_LIMIT],
        "all_rows": rows,
        "eligible_count": sum(1 for row in rows if row["eligible"]),
        "minimum_session_count": minimum_session_count,
        "minimum_status_coverage_label": format_rate_percent(RANKING_SNAPSHOT_MIN_STATUS_COVERAGE),
    }


def build_project_software_section(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    pair_section = build_reliability_group_section(
        sessions,
        title="Najlepszy software w projekcie",
        subtitle=(
            "Najbardziej niezawodny release w ramach kazdego projektu, policzony z sesji "
            "ocenionych tymi samymi regulami co zakladka Analiza."
        ),
        group_key_builder=lambda session: (
            str(session.get("project_number") or "").strip(),
            str(session.get("software_version") or "").strip(),
        ),
        label_builder=lambda session: str(session.get("software_version") or "").strip() or "brak release",
        minimum_session_count=RANKING_SNAPSHOT_MIN_PROJECT_SOFTWARE_SESSIONS,
        dimension_fields=("phone",),
        metadata_fields=("project_number", "software_version"),
    )

    project_rows: list[dict[str, Any]] = []
    seen_projects: set[str] = set()
    source_rows = [row for row in pair_section["all_rows"] if row["eligible"]] or pair_section["all_rows"]
    for row in source_rows:
        project_number = str(row.get("project_number") or "").strip()
        if not project_number or project_number in seen_projects:
            continue
        row_copy = dict(row)
        row_copy["context_label"] = f"Projekt {project_number}"
        project_rows.append(row_copy)
        seen_projects.add(project_number)

    return {
        **pair_section,
        "rows": project_rows[:RANKING_TOP_LIMIT],
    }


def build_ranking_sessionized_source_sql(
    source_from_sql: str,
    available_columns: set[str],
) -> str:
    source_sql = build_wrapped_source_sql(source_from_sql, alias="src")
    analysis_candidate_sql = build_analysis_candidate_sql("src", available_columns)
    return f"""
    with ordered_events as (
        select
            src.raw_id,
            src.device_name,
            src.source_csv_file,
            src.event_ts,
            src.charger_name,
            src.phone,
            src.position,
            src.project_number,
            src.software_version,
            src.scenario_hint,
            src.fod_object,
            src.card_position,
            src.sample_label,
            src.manual_result,
            src.defect_id,
            src.defect_comment,
            src.dual_charging_label,
            src.dual_charging_flag,
            src.row_json,
            src.hmi_status,
            src.inserted_at,
            src.eff,
            src.rx,
            src.tx,
            src.rpp,
            src.current_a,
            src.temperature,
            src.battery_level,
            src.voltage_v,
            {analysis_candidate_sql} as analysis_candidate,
            src.ingest_delay_seconds,
            lag(src.event_ts) over partition_window as prev_event_ts,
            lag(coalesce(src.software_version, '')) over partition_window as prev_software_version,
            lag(coalesce(src.sample_label, '')) over partition_window as prev_sample_label,
            lag(coalesce(lower(trim(src.scenario_hint)), '')) over partition_window as prev_scenario_hint,
            lag(coalesce(src.card_position, '')) over partition_window as prev_card_position,
            lag(coalesce(src.fod_object, '')) over partition_window as prev_fod_object,
            lag(coalesce(src.dual_charging_flag::text, '')) over partition_window as prev_dual_charging_flag
        from {source_sql}
        where src.event_ts is not null
        window partition_window as (
            partition by
                src.phone,
                src.project_number,
                src.device_name,
                src.charger_name,
                src.position
            order by src.event_ts, src.raw_id
        )
    ),
    marked_sessions as (
        select
            ordered_events.*,
            case
                when prev_event_ts is null then 1
                when event_ts - prev_event_ts > interval '20 minutes' then 1
                when coalesce(software_version, '') is distinct from prev_software_version then 1
                when coalesce(sample_label, '') is distinct from prev_sample_label then 1
                when coalesce(lower(trim(scenario_hint)), '') is distinct from prev_scenario_hint then 1
                when coalesce(card_position, '') is distinct from prev_card_position then 1
                when coalesce(fod_object, '') is distinct from prev_fod_object then 1
                when coalesce(dual_charging_flag::text, '') is distinct from prev_dual_charging_flag then 1
                else 0
            end as session_boundary
        from ordered_events
    ),
    numbered_sessions as (
        select
            marked_sessions.*,
            sum(session_boundary) over (
                partition by
                    phone,
                    project_number,
                    device_name,
                    charger_name,
                    position
                order by event_ts, raw_id
                rows between unbounded preceding and current row
            ) as session_seq
        from marked_sessions
    ),
    session_event_metrics as (
        select
            numbered_sessions.*,
            lag(numbered_sessions.event_ts) over session_window as session_prev_event_ts,
            lag(numbered_sessions.eff) over session_window as session_prev_eff,
            lag(numbered_sessions.rx) over session_window as session_prev_rx,
            lag(numbered_sessions.tx) over session_window as session_prev_tx,
            extract(epoch from (
                numbered_sessions.event_ts - lag(numbered_sessions.event_ts) over session_window
            )) as gap_seconds
        from numbered_sessions
        window session_window as (
            partition by
                phone,
                project_number,
                device_name,
                charger_name,
                position,
                session_seq
            order by event_ts, raw_id
        )
    ),
    session_gap_summary as (
        select
            phone,
            project_number,
            device_name,
            charger_name,
            position,
            session_seq,
            percentile_cont(0.5) within group (order by gap_seconds)
                filter (where gap_seconds is not null and gap_seconds > 0) as median_gap_seconds
        from session_event_metrics
        group by
            phone,
            project_number,
            device_name,
            charger_name,
            position,
            session_seq
    ),
    session_events_enriched as (
        select
            metrics.*,
            gap_summary.median_gap_seconds,
            greatest(300.0, coalesce(gap_summary.median_gap_seconds, 60.0) * 4.0) as interruption_threshold_seconds
        from session_event_metrics metrics
        left join session_gap_summary gap_summary
            on gap_summary.phone is not distinct from metrics.phone
           and gap_summary.project_number is not distinct from metrics.project_number
           and gap_summary.device_name is not distinct from metrics.device_name
           and gap_summary.charger_name is not distinct from metrics.charger_name
           and gap_summary.position is not distinct from metrics.position
           and gap_summary.session_seq = metrics.session_seq
    )
    select
        md5(
            concat_ws(
                '|',
                coalesce(phone, ''),
                coalesce(project_number, ''),
                coalesce(device_name, ''),
                coalesce(charger_name, ''),
                coalesce(position, ''),
                session_seq::text
            )
        ) as session_key,
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        min(event_ts) as start_ts,
        max(event_ts) as end_ts,
        count(*)::integer as source_row_count,
        max(software_version) as software_version,
        max(source_csv_file) as source_csv_file,
        max(scenario_hint) as scenario_hint,
        max(fod_object) as fod_object,
        max(card_position) as card_position,
        max(sample_label) as sample_label,
        max(manual_result) as manual_result,
        max(defect_id) as defect_id,
        max(defect_comment) as defect_comment,
        max(dual_charging_label) as dual_charging_label,
        avg(eff) as avg_eff,
        avg(rx) as avg_rx,
        avg(tx) as avg_tx,
        max(temperature) as max_temperature,
        array_remove(array_agg(distinct hmi_status), null) as status_codes,
        (array_agg(hmi_status order by event_ts, raw_id) filter (where nullif(btrim(hmi_status), '') is not null))[1] as first_status,
        (array_agg(hmi_status order by event_ts desc, raw_id desc) filter (where nullif(btrim(hmi_status), '') is not null))[1] as last_status,
        count(*) filter (
            where gap_seconds is not null
              and gap_seconds > interruption_threshold_seconds
        )::integer as interruption_count,
        count(*) filter (
            where session_prev_eff is not null
              and eff is not null
              and session_prev_eff > 0
              and (session_prev_eff - eff) >= 15
              and ((session_prev_eff - eff) / session_prev_eff) >= 0.25
        )::integer as eff_drop_count,
        count(*) filter (
            where session_prev_rx is not null
              and rx is not null
              and session_prev_rx > 0
              and (session_prev_rx - rx) > 0
              and ((session_prev_rx - rx) / session_prev_rx) >= 0.20
        )::integer as rx_drop_count,
        count(*) filter (
            where session_prev_tx is not null
              and tx is not null
              and session_prev_tx > 0
              and (session_prev_tx - tx) > 0
              and ((session_prev_tx - tx) / session_prev_tx) >= 0.20
        )::integer as tx_drop_count
    from session_events_enriched
    group by
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        session_seq
    """


def build_wrapped_source_sql(source_from_sql: str, *, alias: str) -> str:
    return f"""
    (
        select *
        from {source_from_sql}
    ) {alias}
    """


def build_ranking_session_sql_validation_query(session_source_sql: str) -> str:
    return f"""
    explain
    select *
    from (
        {session_source_sql}
    ) ranking_session_source
    limit 1
    """


def write_debug_ranking_sql(query_sql: str, *, context: str) -> Path:
    DEBUG_RANKING_SQL_PATH.write_text(
        f"-- {context}\n{query_sql.strip()}\n",
        encoding="utf-8",
    )
    return DEBUG_RANKING_SQL_PATH


def validate_ranking_session_sql(
    conn: psycopg.Connection,
    session_source_sql: str,
) -> None:
    validation_sql = build_ranking_session_sql_validation_query(session_source_sql)
    try:
        with conn.cursor() as cur:
            cur.execute(validation_sql)
    except psycopg.Error as exc:
        debug_path = write_debug_ranking_sql(
            session_source_sql,
            context="Nightly ranking session SQL validation failed.",
        )
        raise RuntimeError(
            "Nightly ranking session SQL validation failed. "
            f"Saved query to {debug_path.name}. "
            f"PostgreSQL: {sanitize_db_exception_message(exc) or exc.__class__.__name__}"
        ) from exc


def get_ranking_session_source_descriptor(
    conn: psycopg.Connection,
    *,
    allow_dynamic_session_fallback: bool = False,
) -> RankingSessionSourceDescriptor:
    if SOURCE_RELATION == DEFAULT_SOURCE_RELATION:
        if relation_exists(conn, RANKING_SESSION_RELATION):
            available_columns = fetch_relation_columns(conn, RANKING_SESSION_RELATION)
            missing_columns = sorted(RANKING_SESSION_SOURCE_REQUIRED_COLUMNS - available_columns)
            if not missing_columns:
                session_count = fetch_relation_row_count(conn, RANKING_SESSION_RELATION)
                LOGGER.info(
                    "Nightly ranking session source diagnostic: using %s; session_count=%s",
                    RANKING_SESSION_RELATION,
                    session_count,
                )
                return RankingSessionSourceDescriptor(
                    session_source_sql=f"select * from {RANKING_SESSION_RELATION}",
                    source_relation_label=RANKING_SESSION_RELATION,
                    using_precomputed_relation=True,
                    session_count=session_count,
                )
            if not allow_dynamic_session_fallback:
                raise RuntimeError(
                    build_ranking_session_source_setup_message(missing_columns=missing_columns)
                )
            LOGGER.warning(
                "Nightly ranking session source diagnostic: %s is missing required columns (%s); "
                "using dynamic session fallback because allow_dynamic_session_fallback=True",
                RANKING_SESSION_RELATION,
                ", ".join(missing_columns),
            )
        else:
            if not allow_dynamic_session_fallback:
                raise RuntimeError(build_ranking_session_source_setup_message())
            LOGGER.warning(
                "Nightly ranking session source diagnostic: %s does not exist; "
                "using dynamic session fallback because allow_dynamic_session_fallback=True",
                RANKING_SESSION_RELATION,
            )
    else:
        if not allow_dynamic_session_fallback:
            raise RuntimeError(
                "Nightly ranking cannot use "
                f"{RANKING_SESSION_RELATION} because DB_SOURCE_RELATION={SOURCE_RELATION}. "
                "Point DB_SOURCE_RELATION to public.charging_log_processed_mv and create the "
                f"session source with sql/{RANKING_SESSION_MV_SQL_FILENAME} or "
                f"sql/{RANKING_SESSION_TABLE_SQL_FILENAME}, or rerun with "
                "--allow-dynamic-session-fallback."
            )
        LOGGER.info(
            "Nightly ranking session source diagnostic: DB_SOURCE_RELATION=%s, bypassing %s and "
            "using dynamic session SQL",
            SOURCE_RELATION,
            RANKING_SESSION_RELATION,
        )

    source_from_sql, _ = get_ranking_snapshot_source_descriptor(conn)
    source_available_columns = fetch_relation_columns(conn, SOURCE_RELATION)
    LOGGER.info(
        "Nightly ranking session source diagnostic: using dynamic session source derived from %s",
        SOURCE_RELATION,
    )
    return RankingSessionSourceDescriptor(
        session_source_sql=build_ranking_sessionized_source_sql(
            source_from_sql,
            source_available_columns,
        ),
        source_relation_label=f"dynamic session SQL from {SOURCE_RELATION}",
        using_precomputed_relation=False,
        session_count=None,
    )


def build_ranking_classified_sessions_cte(session_source_sql: str) -> str:
    return f"""
    with session_source as (
        {session_source_sql}
    ),
    ranked_sessions as (
        select
            session_key,
            btrim(coalesce(phone, '')) as phone_raw,
            btrim(coalesce(project_number, '')) as project_number_raw,
            btrim(coalesce(software_version, '')) as software_version_raw,
            coalesce(nullif(btrim(phone), ''), 'brak telefonu') as phone_label,
            coalesce(nullif(btrim(project_number), ''), 'brak projektu') as project_number_label,
            coalesce(nullif(btrim(software_version), ''), 'brak release') as software_version_label,
            end_ts,
            avg_eff,
            avg_rx,
            avg_tx,
            max_temperature,
            source_row_count,
            coalesce(status_codes, array[]::text[]) as status_codes,
            coalesce(first_status, '') as first_status,
            coalesce(last_status, '') as last_status,
            coalesce(interruption_count, 0) as interruption_count,
            coalesce(eff_drop_count, 0) + coalesce(rx_drop_count, 0) + coalesce(tx_drop_count, 0) as drop_issue_count,
            lower(
                concat_ws(
                    ' ',
                    coalesce(scenario_hint, ''),
                    coalesce(fod_object, ''),
                    coalesce(card_position, ''),
                    coalesce(position, ''),
                    coalesce(sample_label, ''),
                    coalesce(manual_result, ''),
                    coalesce(defect_id, ''),
                    coalesce(defect_comment, ''),
                    coalesce(source_csv_file, ''),
                    coalesce(dual_charging_label, '')
                )
            ) as hint_blob,
            nullif(btrim(fod_object), '') is not null as has_fod_object,
            nullif(btrim(card_position), '') is not null as has_card_position,
            (
                nullif(btrim(defect_id), '') is not null
                or lower(concat_ws(' ', coalesce(defect_comment, ''), coalesce(source_csv_file, ''), coalesce(manual_result, '')))
                    ~ '(?:^|[^a-z])defect(?:[^a-z]|$)'
            ) as has_explicit_defect_reference,
            exists (
                select 1
                from unnest(coalesce(status_codes, array[]::text[])) code
                where code <> 'N/A'
            ) as has_status_readout,
            case
                when (
                    nullif(btrim(card_position), '') is not null
                    or coalesce(status_codes, array[]::text[]) && array['15', '16']::text[]
                ) then 'rfid'
                when lower(
                    concat_ws(
                        ' ',
                        coalesce(scenario_hint, ''),
                        coalesce(fod_object, ''),
                        coalesce(card_position, ''),
                        coalesce(sample_label, ''),
                        coalesce(manual_result, ''),
                        coalesce(defect_comment, ''),
                        coalesce(source_csv_file, '')
                    )
                ) like '%rfid%' then 'rfid'
                when (
                    nullif(btrim(fod_object), '') is not null
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%fod%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%foreign object%'
                ) then 'fod'
                when lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) like '%nfc%'
                     and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) not like '%rfid%' then 'nfc'
                when lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%charging 0-100%'
                     or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%charging 0 -100%'
                     or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%0-100%'
                     or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%wlc%' then 'charging'
                when coalesce(status_codes, array[]::text[]) && array['4', '6']::text[]
                     and not coalesce(status_codes, array[]::text[]) && array['2', '3']::text[] then 'unknown'
                else 'charging'
            end as scenario_code,
            case
                when not exists (
                    select 1
                    from unnest(coalesce(status_codes, array[]::text[])) code
                    where code <> 'N/A'
                ) and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) like '%nfc%'
                     and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) not like '%rfid%'
                     and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, '')))
                         ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then 'not_ok'
                when not exists (
                    select 1
                    from unnest(coalesce(status_codes, array[]::text[])) code
                    where code <> 'N/A'
                ) and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) like '%nfc%'
                     and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) not like '%rfid%'
                     and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, '')))
                         ~ '(?:^|[^a-z])ok(?:[^a-z]|$)'
                     and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, '')))
                         !~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then 'ok'
                when not exists (
                    select 1
                    from unnest(coalesce(status_codes, array[]::text[])) code
                    where code <> 'N/A'
                ) then 'other'
                when (
                    nullif(btrim(card_position), '') is not null
                    or coalesce(status_codes, array[]::text[]) && array['15', '16']::text[]
                    or lower(
                        concat_ws(
                            ' ',
                            coalesce(scenario_hint, ''),
                            coalesce(fod_object, ''),
                            coalesce(card_position, ''),
                            coalesce(sample_label, ''),
                            coalesce(manual_result, ''),
                            coalesce(defect_comment, ''),
                            coalesce(source_csv_file, '')
                        )
                    ) like '%rfid%'
                ) and coalesce(status_codes, array[]::text[]) <@ array['15', '16']::text[] then 'ok'
                when (
                    nullif(btrim(card_position), '') is not null
                    or coalesce(status_codes, array[]::text[]) && array['15', '16']::text[]
                    or lower(
                        concat_ws(
                            ' ',
                            coalesce(scenario_hint, ''),
                            coalesce(fod_object, ''),
                            coalesce(card_position, ''),
                            coalesce(sample_label, ''),
                            coalesce(manual_result, ''),
                            coalesce(defect_comment, ''),
                            coalesce(source_csv_file, '')
                        )
                    ) like '%rfid%'
                ) then 'not_ok'
                when (
                    nullif(btrim(fod_object), '') is not null
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%fod%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%foreign object%'
                ) and lower(
                    concat_ws(
                        ' ',
                        coalesce(scenario_hint, ''),
                        coalesce(fod_object, ''),
                        coalesce(card_position, ''),
                        coalesce(position, ''),
                        coalesce(sample_label, ''),
                        coalesce(manual_result, ''),
                        coalesce(defect_id, ''),
                        coalesce(defect_comment, ''),
                        coalesce(source_csv_file, ''),
                        coalesce(dual_charging_label, '')
                    )
                ) like '%no fod detected%' then 'not_ok'
                when (
                    nullif(btrim(fod_object), '') is not null
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%fod%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%foreign object%'
                ) and lower(
                    concat_ws(
                        ' ',
                        coalesce(scenario_hint, ''),
                        coalesce(fod_object, ''),
                        coalesce(card_position, ''),
                        coalesce(position, ''),
                        coalesce(sample_label, ''),
                        coalesce(manual_result, ''),
                        coalesce(defect_id, ''),
                        coalesce(defect_comment, ''),
                        coalesce(source_csv_file, ''),
                        coalesce(dual_charging_label, '')
                    )
                ) like '%inappropriate temperature%' then 'not_ok'
                when (
                    nullif(btrim(fod_object), '') is not null
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%fod%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%foreign object%'
                ) and coalesce(status_codes, array[]::text[]) && array['3', '5']::text[] then 'not_ok'
                when (
                    nullif(btrim(fod_object), '') is not null
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%fod%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%foreign object%'
                ) and coalesce(status_codes, array[]::text[]) && array['8', '9', '13', '14', '17']::text[] then 'not_ok'
                when (
                    nullif(btrim(fod_object), '') is not null
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%fod%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(fod_object, ''), coalesce(defect_comment, ''))) like '%foreign object%'
                ) and coalesce(status_codes, array[]::text[]) && array['4', '6']::text[] then 'ok'
                when (
                    lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) like '%nfc%'
                    and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) not like '%rfid%'
                ) and lower(
                    concat_ws(
                        ' ',
                        coalesce(scenario_hint, ''),
                        coalesce(fod_object, ''),
                        coalesce(card_position, ''),
                        coalesce(position, ''),
                        coalesce(sample_label, ''),
                        coalesce(manual_result, ''),
                        coalesce(defect_id, ''),
                        coalesce(defect_comment, ''),
                        coalesce(source_csv_file, ''),
                        coalesce(dual_charging_label, '')
                    )
                ) ~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then 'not_ok'
                when (
                    lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) like '%nfc%'
                    and lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(manual_result, ''), coalesce(defect_comment, ''))) not like '%rfid%'
                ) and lower(
                    concat_ws(
                        ' ',
                        coalesce(scenario_hint, ''),
                        coalesce(fod_object, ''),
                        coalesce(card_position, ''),
                        coalesce(position, ''),
                        coalesce(sample_label, ''),
                        coalesce(manual_result, ''),
                        coalesce(defect_id, ''),
                        coalesce(defect_comment, ''),
                        coalesce(source_csv_file, ''),
                        coalesce(dual_charging_label, '')
                    )
                ) ~ '(?:^|[^a-z])ok(?:[^a-z]|$)'
                    and lower(
                        concat_ws(
                            ' ',
                            coalesce(scenario_hint, ''),
                            coalesce(fod_object, ''),
                            coalesce(card_position, ''),
                            coalesce(position, ''),
                            coalesce(sample_label, ''),
                            coalesce(manual_result, ''),
                            coalesce(defect_id, ''),
                            coalesce(defect_comment, ''),
                            coalesce(source_csv_file, ''),
                            coalesce(dual_charging_label, '')
                        )
                    ) !~ '(?:^|[^a-z])(not ok|no ok|nok)(?:[^a-z]|$)' then 'ok'
                when coalesce(status_codes, array[]::text[]) && array['0', '1', '4', '6', '8', '9', '13', '14', '17']::text[] then 'not_ok'
                when lower(
                    concat_ws(
                        ' ',
                        coalesce(scenario_hint, ''),
                        coalesce(fod_object, ''),
                        coalesce(card_position, ''),
                        coalesce(position, ''),
                        coalesce(sample_label, ''),
                        coalesce(manual_result, ''),
                        coalesce(defect_id, ''),
                        coalesce(defect_comment, ''),
                        coalesce(source_csv_file, ''),
                        coalesce(dual_charging_label, '')
                    )
                ) ~ '(?:^|[^a-z])(toggling|toggling status|no charging|charging interrupted|charging interference|charging inter)(?:[^a-z]|$)'
                    then 'not_ok'
                when (
                    lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%charging 0-100%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%charging 0 -100%'
                    or lower(concat_ws(' ', coalesce(scenario_hint, ''), coalesce(source_csv_file, ''), coalesce(manual_result, ''))) like '%0-100%'
                ) and coalesce(status_codes, array[]::text[]) <@ array['2', '3']::text[]
                    and coalesce(status_codes, array[]::text[]) && array['3']::text[]
                    and coalesce(last_status, '') = '2' then 'ok'
                when coalesce(status_codes, array[]::text[]) <@ array['3']::text[] then 'ok'
                when coalesce(status_codes, array[]::text[]) = array['2']::text[] then 'warning'
                when coalesce(status_codes, array[]::text[]) <@ array['2', '3']::text[] then 'warning'
                when coalesce(status_codes, array[]::text[]) && array['5', '15', '16']::text[] then 'warning'
                else 'warning'
            end as base_verdict
        from session_source
    ),
    classified_sessions as (
        select
            *,
            case
                when base_verdict = 'ok' and (interruption_count > 0 or drop_issue_count > 0) then 'not_ok'
                when base_verdict = 'not_ok' and (
                    has_explicit_defect_reference
                    or interruption_count >= 2
                    or drop_issue_count >= 2
                ) then 'defect'
                else base_verdict
            end as verdict,
            (base_verdict = 'ok' and interruption_count = 0 and drop_issue_count = 0) as is_clean
        from ranked_sessions
    )
    """


def create_temp_classified_ranking_sessions(
    conn: psycopg.Connection,
    session_source_sql: str,
) -> int:
    LOGGER.info("Nightly ranking: start create temp classified sessions")
    started_at = time.monotonic()
    validate_ranking_session_sql(conn, session_source_sql)
    cte_sql = build_ranking_classified_sessions_cte(session_source_sql)
    create_sql = f"""
        create temporary table {RANKING_CLASSIFIED_TEMP_TABLE}
        on commit drop
        as
        {cte_sql}
        select *
        from classified_sessions
    """
    with conn.cursor() as cur:
        cur.execute(f"drop table if exists {RANKING_CLASSIFIED_TEMP_TABLE}")
        try:
            cur.execute(create_sql)
        except psycopg.Error as exc:
            debug_path = write_debug_ranking_sql(
                create_sql,
                context="Nightly ranking classified session SQL failed.",
            )
            raise RuntimeError(
                "Nightly ranking classified session SQL failed. "
                f"Saved query to {debug_path.name}. "
                f"PostgreSQL: {sanitize_db_exception_message(exc) or exc.__class__.__name__}"
            ) from exc
        cur.execute(
            f"create index idx_tmp_classified_ranking_sessions_phone_raw on "
            f"{RANKING_CLASSIFIED_TEMP_TABLE} (phone_raw)"
        )
        cur.execute(
            f"create index idx_tmp_classified_ranking_sessions_project_number_raw on "
            f"{RANKING_CLASSIFIED_TEMP_TABLE} (project_number_raw)"
        )
        cur.execute(
            f"create index idx_tmp_classified_ranking_sessions_software_version_raw on "
            f"{RANKING_CLASSIFIED_TEMP_TABLE} (software_version_raw)"
        )
        cur.execute(
            f"create index idx_tmp_classified_ranking_sessions_verdict on "
            f"{RANKING_CLASSIFIED_TEMP_TABLE} (verdict)"
        )
        cur.execute(
            f"create index idx_tmp_classified_ranking_sessions_has_status_readout on "
            f"{RANKING_CLASSIFIED_TEMP_TABLE} (has_status_readout)"
        )
        cur.execute(f"select count(*) from {RANKING_CLASSIFIED_TEMP_TABLE}")
        row_count = int(cur.fetchone()[0])
    LOGGER.info(
        "Nightly ranking: end create temp classified sessions; rows=%s duration_seconds=%.2f",
        row_count,
        time.monotonic() - started_at,
    )
    return row_count


def fetch_reliability_group_aggregates(
    conn: psycopg.Connection,
    *,
    classified_sessions_sql: str,
    group_fields: tuple[tuple[str, str], ...],
    dimension_fields: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    group_select_sql = ",\n            ".join(f"{expression} as {alias}" for alias, expression in group_fields)
    group_by_sql = ", ".join(expression for _, expression in group_fields)
    dimension_select_sql = ",\n            ".join(
        f"count(distinct nullif({field_name}_raw, ''))::integer as {field_name}_count"
        for field_name in dimension_fields
    )
    if dimension_select_sql:
        dimension_select_sql = ",\n            " + dimension_select_sql

    query = f"""
    with classified_sessions as (
        {classified_sessions_sql}
    ),
    aggregated as (
        select
            {group_select_sql},
            count(*)::integer as session_count,
            count(*) filter (where has_status_readout)::integer as covered_session_count,
            count(*) filter (where verdict = 'defect')::integer as defect_count,
            count(*) filter (where verdict = 'not_ok')::integer as not_ok_count,
            count(*) filter (where verdict = 'warning')::integer as warning_count,
            count(*) filter (where is_clean)::integer as clean_count,
            percentile_cont(0.5) within group (order by avg_eff) filter (where avg_eff is not null) as median_eff,
            percentile_cont(0.5) within group (order by avg_rx) filter (where avg_rx is not null) as median_rx,
            percentile_cont(0.5) within group (order by avg_tx) filter (where avg_tx is not null) as median_tx,
            max(end_ts) as last_seen
            {dimension_select_sql}
        from classified_sessions
        group by {group_by_sql}
    )
    select *
    from aggregated
    """

    with conn.cursor() as cur:
        cur.execute(query)
        column_names = [desc.name for desc in cur.description]
        return [dict(zip(column_names, row)) for row in cur.fetchall()]


def build_reliability_group_section_from_aggregates(
    rows: list[dict[str, Any]],
    *,
    title: str,
    subtitle: str,
    minimum_session_count: int,
    dimension_fields: tuple[str, ...] = (),
    metadata_fields: tuple[str, ...] = (),
) -> dict[str, Any]:
    decorated_rows: list[dict[str, Any]] = []
    for item in rows:
        session_count = int(item.get("session_count") or 0)
        covered_session_count = int(item.get("covered_session_count") or 0)
        defect_count = int(item.get("defect_count") or 0)
        not_ok_count = int(item.get("not_ok_count") or 0)
        warning_count = int(item.get("warning_count") or 0)
        clean_count = int(item.get("clean_count") or 0)
        status_coverage = covered_session_count / session_count if session_count else 0.0
        defect_rate = defect_count / session_count if session_count else 0.0
        not_ok_rate = not_ok_count / session_count if session_count else 0.0
        warning_rate = warning_count / session_count if session_count else 0.0
        clean_rate = clean_count / session_count if session_count else 0.0
        reliability_score = max(
            0.0,
            100.0 - (
                60.0 * defect_rate
                + 25.0 * not_ok_rate
                + 10.0 * warning_rate
                + 5.0 * (1.0 - status_coverage)
            ),
        )
        row = {
            "label": str(item.get("label") or "brak danych"),
            "session_count": session_count,
            "status_coverage": status_coverage,
            "defect_rate": defect_rate,
            "not_ok_rate": not_ok_rate,
            "warning_rate": warning_rate,
            "clean_rate": clean_rate,
            "median_eff": item.get("median_eff"),
            "median_rx": item.get("median_rx"),
            "median_tx": item.get("median_tx"),
            "reliability_score": reliability_score,
            "last_seen_label": item["last_seen"].strftime("%Y-%m-%d %H:%M") if item.get("last_seen") else "brak",
            "eligible": (
                session_count >= minimum_session_count
                and status_coverage >= RANKING_SNAPSHOT_MIN_STATUS_COVERAGE
            ),
        }
        for field_name in metadata_fields:
            row[field_name] = str(item.get(field_name) or "").strip()
        for field_name in dimension_fields:
            row[f"{field_name}_count"] = int(item.get(f"{field_name}_count") or 0)
        row["reliability_score_label"] = format_number(reliability_score, suffix="/100")
        row["status_coverage_label"] = format_rate_percent(status_coverage)
        row["defect_rate_label"] = format_rate_percent(defect_rate)
        row["not_ok_rate_label"] = format_rate_percent(not_ok_rate)
        row["warning_rate_label"] = format_rate_percent(warning_rate)
        row["clean_rate_label"] = format_rate_percent(clean_rate)
        row["median_eff_label"] = format_number(row["median_eff"], suffix="%")
        row["median_rx_label"] = format_number(row["median_rx"])
        row["median_tx_label"] = format_number(row["median_tx"])
        row["support_label"] = build_reliability_support_label(row, dimensions=dimension_fields)
        decorated_rows.append(row)

    decorated_rows.sort(
        key=lambda item: (
            not item["eligible"],
            -(item["reliability_score"] or -1),
            item["defect_rate"],
            item["not_ok_rate"],
            item["warning_rate"],
            -(item["clean_rate"] or -1),
            -item["session_count"],
            item["label"],
        )
    )
    display_rows = [row for row in decorated_rows if row["eligible"]] or decorated_rows
    return {
        "title": title,
        "subtitle": subtitle,
        "rows": display_rows[:RANKING_TOP_LIMIT],
        "all_rows": decorated_rows,
        "eligible_count": sum(1 for row in decorated_rows if row["eligible"]),
        "minimum_session_count": minimum_session_count,
        "minimum_status_coverage_label": format_rate_percent(RANKING_SNAPSHOT_MIN_STATUS_COVERAGE),
    }


def build_nightly_ranking_payload_from_sql(
    conn: psycopg.Connection,
    *,
    snapshot_date: date,
    generated_at: datetime,
    allow_dynamic_session_fallback: bool = False,
) -> dict[str, Any]:
    score_help = build_reliability_score_help()
    session_source = get_ranking_session_source_descriptor(
        conn,
        allow_dynamic_session_fallback=allow_dynamic_session_fallback,
    )
    LOGGER.info(
        "Nightly ranking: session source=%s using_precomputed_relation=%s session_count=%s",
        session_source.source_relation_label,
        session_source.using_precomputed_relation,
        session_source.session_count if session_source.session_count is not None else "n/a",
    )
    aggregation_started_at = time.monotonic()
    classified_session_count = create_temp_classified_ranking_sessions(
        conn,
        session_source.session_source_sql,
    )
    classified_sessions_sql = f"select * from {RANKING_CLASSIFIED_TEMP_TABLE}"

    LOGGER.info("Nightly ranking: start phone aggregate")
    phone_rows = fetch_reliability_group_aggregates(
        conn,
        classified_sessions_sql=classified_sessions_sql,
        group_fields=(("label", "phone_label"), ("phone", "phone_raw")),
        dimension_fields=("project_number", "software_version"),
    )
    LOGGER.info("Nightly ranking: end phone aggregate")

    LOGGER.info("Nightly ranking: start project aggregate")
    project_rows = fetch_reliability_group_aggregates(
        conn,
        classified_sessions_sql=classified_sessions_sql,
        group_fields=(("label", "project_number_label"), ("project_number", "project_number_raw")),
        dimension_fields=("phone", "software_version"),
    )
    LOGGER.info("Nightly ranking: end project aggregate")

    LOGGER.info("Nightly ranking: start project software aggregate")
    project_software_rows = fetch_reliability_group_aggregates(
        conn,
        classified_sessions_sql=classified_sessions_sql,
        group_fields=(
            ("label", "software_version_label"),
            ("project_number", "project_number_raw"),
            ("software_version", "software_version_raw"),
        ),
        dimension_fields=("phone",),
    )
    LOGGER.info("Nightly ranking: end project software aggregate")

    LOGGER.info("Nightly ranking: start global software aggregate")
    global_software_rows = fetch_reliability_group_aggregates(
        conn,
        classified_sessions_sql=classified_sessions_sql,
        group_fields=(("label", "software_version_label"), ("software_version", "software_version_raw")),
        dimension_fields=("project_number", "phone"),
    )
    LOGGER.info("Nightly ranking: end global software aggregate")
    LOGGER.info(
        "Nightly ranking: end aggregation; duration_seconds=%.2f classified_sessions=%s",
        time.monotonic() - aggregation_started_at,
        classified_session_count,
    )

    phone_section = build_reliability_group_section_from_aggregates(
        phone_rows,
        title="Najbardziej bezproblemowe telefony",
        subtitle=(
            "Ranking telefonow po DEFECT, NOT OK i warningach z uwzglednieniem pokrycia statusow "
            "oraz czystych sesji bez przerw i dropow."
        ),
        minimum_session_count=RANKING_SNAPSHOT_MIN_PHONE_SESSIONS,
        dimension_fields=("project_number", "software_version"),
        metadata_fields=("phone",),
    )
    project_section = build_reliability_group_section_from_aggregates(
        project_rows,
        title="Projekty z najmniejsza liczba problemow",
        subtitle=(
            "Porownanie projektow po sesjach ocenionych regułami analizy. Projekty bez pokrycia statusu "
            "nie powinny wygrywac tylko dlatego, ze nie raportuja bledow."
        ),
        minimum_session_count=RANKING_SNAPSHOT_MIN_PROJECT_SESSIONS,
        dimension_fields=("phone", "software_version"),
        metadata_fields=("project_number",),
    )
    pair_section = build_reliability_group_section_from_aggregates(
        project_software_rows,
        title="Najlepszy software w projekcie",
        subtitle=(
            "Najbardziej niezawodny release w ramach kazdego projektu, policzony z sesji "
            "ocenionych tymi samymi regulami co zakladka Analiza."
        ),
        minimum_session_count=RANKING_SNAPSHOT_MIN_PROJECT_SOFTWARE_SESSIONS,
        dimension_fields=("phone",),
        metadata_fields=("project_number", "software_version"),
    )
    project_software_section = {
        **pair_section,
        "rows": [],
    }
    seen_projects: set[str] = set()
    source_rows = [row for row in pair_section["all_rows"] if row["eligible"]] or pair_section["all_rows"]
    for row in source_rows:
        project_number = str(row.get("project_number") or "").strip()
        if not project_number or project_number in seen_projects:
            continue
        row_copy = dict(row)
        row_copy["context_label"] = f"Projekt {project_number}"
        project_software_section["rows"].append(row_copy)
        seen_projects.add(project_number)
        if len(project_software_section["rows"]) >= RANKING_TOP_LIMIT:
            break

    global_software_section = build_reliability_group_section_from_aggregates(
        global_software_rows,
        title="Najmniej problemowy software globalnie",
        subtitle=(
            "Globalne porownanie wersji software po ocenie sesji. To ranking przekrojowy, "
            "wiec ten sam release moze obejmowac rozne telefony i projekty."
        ),
        minimum_session_count=RANKING_SNAPSHOT_MIN_GLOBAL_SOFTWARE_SESSIONS,
        dimension_fields=("project_number", "phone"),
        metadata_fields=("software_version",),
    )

    session_count = classified_session_count

    leader_cards = []
    if phone_section["rows"]:
        row = phone_section["rows"][0]
        leader_cards.append(
            {
                "label": "Lider telefonow",
                "value": row["label"],
                "help": (
                    f"DEFECT {row['defect_rate_label']} | NOT OK {row['not_ok_rate_label']} | "
                    f"coverage {row['status_coverage_label']}"
                ),
            }
        )
    if project_section["rows"]:
        row = project_section["rows"][0]
        leader_cards.append(
            {
                "label": "Lider projektow",
                "value": row["label"],
                "help": (
                    f"DEFECT {row['defect_rate_label']} | clean {row['clean_rate_label']} | "
                    f"sesje {row['session_count']}"
                ),
            }
        )
    if project_software_section["rows"]:
        row = project_software_section["rows"][0]
        leader_cards.append(
            {
                "label": "Najlepszy software w projekcie",
                "value": row["label"],
                "help": f"Projekt {row.get('project_number') or 'brak'} | score {row['reliability_score_label']}",
            }
        )
    if global_software_section["rows"]:
        row = global_software_section["rows"][0]
        leader_cards.append(
            {
                "label": "Lider software globalnie",
                "value": row["label"],
                "help": (
                    f"DEFECT {row['defect_rate_label']} | coverage {row['status_coverage_label']} | "
                    f"score {row['reliability_score_label']}"
                ),
            }
        )

    return {
        "scope_title": "Nocny ranking niezawodnosci",
        "scope_subtitle": (
            "Snapshot dla calej bazy liczony raz dziennie o 03:00 i wyswietlany przez caly dzien "
            "bez przeliczania w request-cie."
        ),
        "snapshot_date": snapshot_date.isoformat(),
        "snapshot_generated_at": generated_at.strftime("%Y-%m-%d %H:%M"),
        "snapshot_timezone": RANKING_SNAPSHOT_TIMEZONE,
        "source_relation": session_source.source_relation_label,
        "source_row_count": classified_session_count,
        "source_row_count_label": "sesji rankingowych",
        "session_count": session_count,
        "leader_cards": leader_cards,
        "group_sections": [
            phone_section,
            project_section,
            project_software_section,
            global_software_section,
        ],
        "highlights": build_snapshot_highlights(
            phone_section=phone_section,
            project_section=project_section,
            project_software_section=project_software_section,
            global_software_section=global_software_section,
            source_row_count=classified_session_count,
            session_count=session_count,
            source_row_count_label="sesji rankingowych",
        ),
        "score_formula": (
            f"{score_help['formula']} eff, RX i TX sa tylko metrykami wspierajacymi."
        ),
        "score_help": score_help,
        "truncated": False,
    }


def build_snapshot_highlights(
    *,
    phone_section: dict[str, Any],
    project_section: dict[str, Any],
    project_software_section: dict[str, Any],
    global_software_section: dict[str, Any],
    source_row_count: int,
    session_count: int,
    source_row_count_label: str = "rekordow zrodla",
) -> list[str]:
    highlights: list[str] = [
        f"Nocny snapshot objal {source_row_count} {source_row_count_label} i {session_count} sesji testowych.",
    ]
    if phone_section["rows"]:
        row = phone_section["rows"][0]
        highlights.append(
            f"Najbardziej bezproblemowy telefon: {row['label']} "
            f"(DEFECT {row['defect_rate_label']}, NOT OK {row['not_ok_rate_label']})."
        )
    if project_section["rows"]:
        row = project_section["rows"][0]
        highlights.append(
            f"Najstabilniejszy projekt z pelnym odczytem statusu: {row['label']} "
            f"(coverage {row['status_coverage_label']})."
        )
    if project_software_section["rows"]:
        row = project_software_section["rows"][0]
        highlights.append(
            f"Najlepszy software w projekcie: {row['label']} dla {row.get('project_number') or 'brak projektu'}."
        )
    if global_software_section["rows"]:
        row = global_software_section["rows"][0]
        highlights.append(
            f"Najmniej problemowy software globalnie: {row['label']} "
            f"(score {row['reliability_score_label']})."
        )
    return highlights


def build_nightly_ranking_payload(
    sessions: list[dict[str, Any]],
    *,
    snapshot_date: date,
    generated_at: datetime,
    source_row_count: int,
) -> dict[str, Any]:
    score_help = build_reliability_score_help()
    annotate_project_analysis_context(sessions)
    for session in sessions:
        session["examples_assessment"] = build_examples_session_assessment(session)

    phone_section = build_reliability_group_section(
        sessions,
        title="Najbardziej bezproblemowe telefony",
        subtitle=(
            "Ranking telefonow po DEFECT, NOT OK i warningach z uwzglednieniem pokrycia statusow "
            "oraz czystych sesji bez przerw i dropow."
        ),
        group_key_builder=lambda session: str(session.get("phone") or "").strip() or "brak telefonu",
        label_builder=lambda session: str(session.get("phone") or "").strip() or "brak telefonu",
        minimum_session_count=RANKING_SNAPSHOT_MIN_PHONE_SESSIONS,
        dimension_fields=("project_number", "software_version"),
        metadata_fields=("phone",),
    )
    project_section = build_reliability_group_section(
        sessions,
        title="Projekty z najmniejsza liczba problemow",
        subtitle=(
            "Porownanie projektow po sesjach ocenionych regułami analizy. Projekty bez pokrycia statusu "
            "nie powinny wygrywac tylko dlatego, ze nie raportuja bledow."
        ),
        group_key_builder=lambda session: str(session.get("project_number") or "").strip() or "brak projektu",
        label_builder=lambda session: str(session.get("project_number") or "").strip() or "brak projektu",
        minimum_session_count=RANKING_SNAPSHOT_MIN_PROJECT_SESSIONS,
        dimension_fields=("phone", "software_version"),
        metadata_fields=("project_number",),
    )
    project_software_section = build_project_software_section(sessions)
    global_software_section = build_reliability_group_section(
        sessions,
        title="Najmniej problemowy software globalnie",
        subtitle=(
            "Globalne porownanie wersji software po ocenie sesji. To ranking przekrojowy, "
            "wiec ten sam release moze obejmowac rozne telefony i projekty."
        ),
        group_key_builder=lambda session: str(session.get("software_version") or "").strip() or "brak release",
        label_builder=lambda session: str(session.get("software_version") or "").strip() or "brak release",
        minimum_session_count=RANKING_SNAPSHOT_MIN_GLOBAL_SOFTWARE_SESSIONS,
        dimension_fields=("project_number", "phone"),
        metadata_fields=("software_version",),
    )

    leader_cards = []
    if phone_section["rows"]:
        row = phone_section["rows"][0]
        leader_cards.append(
            {
                "label": "Lider telefonow",
                "value": row["label"],
                "help": (
                    f"DEFECT {row['defect_rate_label']} | NOT OK {row['not_ok_rate_label']} | "
                    f"coverage {row['status_coverage_label']}"
                ),
            }
        )
    if project_section["rows"]:
        row = project_section["rows"][0]
        leader_cards.append(
            {
                "label": "Lider projektow",
                "value": row["label"],
                "help": (
                    f"DEFECT {row['defect_rate_label']} | clean {row['clean_rate_label']} | "
                    f"sesje {row['session_count']}"
                ),
            }
        )
    if project_software_section["rows"]:
        row = project_software_section["rows"][0]
        leader_cards.append(
            {
                "label": "Najlepszy software w projekcie",
                "value": row["label"],
                "help": f"Projekt {row.get('project_number') or 'brak'} | score {row['reliability_score_label']}",
            }
        )
    if global_software_section["rows"]:
        row = global_software_section["rows"][0]
        leader_cards.append(
            {
                "label": "Lider software globalnie",
                "value": row["label"],
                "help": (
                    f"DEFECT {row['defect_rate_label']} | coverage {row['status_coverage_label']} | "
                    f"score {row['reliability_score_label']}"
                ),
            }
        )

    return {
        "scope_title": "Nocny ranking niezawodnosci",
        "scope_subtitle": (
            "Snapshot dla calej bazy liczony raz dziennie o 03:00 i wyswietlany przez caly dzien "
            "bez przeliczania w request-cie."
        ),
        "snapshot_date": snapshot_date.isoformat(),
        "snapshot_generated_at": generated_at.strftime("%Y-%m-%d %H:%M"),
        "snapshot_timezone": RANKING_SNAPSHOT_TIMEZONE,
        "source_relation": SOURCE_RELATION,
        "source_row_count": source_row_count,
        "session_count": len(sessions),
        "leader_cards": leader_cards,
        "group_sections": [
            phone_section,
            project_section,
            project_software_section,
            global_software_section,
        ],
        "highlights": build_snapshot_highlights(
            phone_section=phone_section,
            project_section=project_section,
            project_software_section=project_software_section,
            global_software_section=global_software_section,
            source_row_count=source_row_count,
            session_count=len(sessions),
            source_row_count_label="rekordow zrodla",
        ),
        "score_formula": (
            f"{score_help['formula']} eff, RX i TX sa tylko metrykami wspierajacymi."
        ),
        "score_help": score_help,
        "truncated": False,
    }


def get_ranking_snapshot_source_descriptor(conn: psycopg.Connection) -> tuple[str, bool]:
    required_columns = (
        "raw_id",
        "device_name",
        "source_csv_file",
        "event_ts",
        "charger_name",
        "phone",
        "position",
        "project_number",
        "software_version",
        "scenario_hint",
        "fod_object",
        "card_position",
        "sample_label",
        "manual_result",
        "defect_id",
        "defect_comment",
        "dual_charging_label",
        "dual_charging_flag",
        "row_json",
        "hmi_status",
        "inserted_at",
        "eff",
        "rx",
        "tx",
        "rpp",
        "current_a",
        "temperature",
        "battery_level",
        "voltage_v",
        "ingest_delay_seconds",
    )
    available_columns = fetch_relation_columns(conn, SOURCE_RELATION)
    is_prepared = set(required_columns).issubset(available_columns)
    if is_prepared:
        return f"{SOURCE_RELATION} source_data", True
    return build_lightweight_stream_source_from_sql(
        SOURCE_RELATION,
        available_columns,
        required_columns,
    ), False


def stream_all_sessions_for_ranking_snapshot() -> tuple[list[dict[str, Any]], int]:
    ordered_columns_sql = """
        phone,
        project_number,
        device_name,
        charger_name,
        position,
        event_ts asc nulls last,
        raw_id asc
    """
    sessions: list[dict[str, Any]] = []
    current_rows: list[dict[str, Any]] = []
    current_key: tuple[str, str, str, str, str] | None = None
    current_context_markers: dict[str, set[str]] | None = None
    previous_ts: datetime | None = None
    processed_row_count = 0

    with create_connection() as conn:
        source_from_sql, source_is_prepared = get_ranking_snapshot_source_descriptor(conn)
        with conn.cursor(name="nightly_ranking_snapshot_stream") as cur:
            cur.execute(
                f"""
                select
                    {ANALYSIS_COLUMNS_SQL}
                from {source_from_sql}
                order by {ordered_columns_sql}
                """
            )
            if hasattr(cur, "itersize"):
                cur.itersize = RANKING_SNAPSHOT_STREAM_BATCH_SIZE

            column_names = [desc.name for desc in cur.description]
            for values in cur:
                processed_row_count += 1
                row = dict(zip(column_names, values))
                row["_source_prepared"] = source_is_prepared
                row = enrich_row(row)

                current_ts = parse_optional_datetime(row.get("event_ts"))
                if current_ts is None:
                    continue

                row_key = build_session_group_key(row)
                row_context_markers = build_session_context_markers(row)
                should_split = False
                if current_rows and current_key != row_key:
                    should_split = True
                elif current_rows and session_context_conflicts(current_context_markers, row_context_markers):
                    should_split = True
                elif current_rows and previous_ts is not None:
                    should_split = (current_ts - previous_ts).total_seconds() > SESSION_BREAK_SECONDS

                if should_split:
                    sessions.append(finalize_session(current_rows, len(sessions) + 1))
                    current_rows = []
                    current_context_markers = None
                    previous_ts = None

                current_rows.append(row)
                current_key = row_key
                current_context_markers = merge_session_context_markers(current_context_markers, row_context_markers)
                previous_ts = current_ts

    if current_rows:
        sessions.append(finalize_session(current_rows, len(sessions) + 1))

    return sessions, processed_row_count


def ensure_ranking_snapshot_table(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"""
            create table if not exists {RANKING_SNAPSHOT_TABLE} (
                snapshot_date date primary key,
                generated_at timestamp without time zone not null,
                source_relation text not null,
                source_row_count bigint not null,
                session_count integer not null,
                payload jsonb not null
            )
            """
        )
        cur.execute(
            f"""
            create index if not exists idx_charging_log_ranking_snapshot_generated_at
                on {RANKING_SNAPSHOT_TABLE} (generated_at desc)
            """
        )


def refresh_processed_source_view(conn: psycopg.Connection) -> float:
    started_at = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("select public.refresh_charging_log_processed_mv()")
    duration_seconds = time.monotonic() - started_at
    LOGGER.info(
        "Nightly ranking: refreshed %s; duration_seconds=%.2f",
        DEFAULT_SOURCE_RELATION,
        duration_seconds,
    )
    return duration_seconds


def refresh_ranking_session_view(conn: psycopg.Connection) -> float | None:
    if SOURCE_RELATION != DEFAULT_SOURCE_RELATION or not relation_exists(conn, RANKING_SESSION_RELATION):
        LOGGER.info(
            "Nightly ranking: skipped refresh of %s; source_relation=%s exists=%s",
            RANKING_SESSION_RELATION,
            SOURCE_RELATION,
            relation_exists(conn, RANKING_SESSION_RELATION) if SOURCE_RELATION == DEFAULT_SOURCE_RELATION else False,
        )
        return None
    started_at = time.monotonic()
    with conn.cursor() as cur:
        cur.execute("select public.refresh_charging_log_sessions_mv()")
    duration_seconds = time.monotonic() - started_at
    LOGGER.info(
        "Nightly ranking: refreshed %s; duration_seconds=%.2f",
        RANKING_SESSION_RELATION,
        duration_seconds,
    )
    return duration_seconds


def generate_and_store_nightly_ranking_snapshot(
    *,
    refresh_source_view: bool = False,
    allow_dynamic_session_fallback: bool = False,
) -> dict[str, Any]:
    # To pelne przeliczenie rankingu dla calej bazy. Wywoluj je tylko z osobnego
    # procesu CLI albo z harmonogramu, nigdy automatycznie z requestu HTTP.
    generated_at = get_ranking_snapshot_now()
    snapshot_date = generated_at.date()

    with create_connection() as conn:
        ensure_ranking_snapshot_table(conn)
        if refresh_source_view:
            refresh_processed_source_view(conn)
            refresh_ranking_session_view(conn)
            conn.commit()

        payload = build_nightly_ranking_payload_from_sql(
            conn,
            snapshot_date=snapshot_date,
            generated_at=generated_at,
            allow_dynamic_session_fallback=allow_dynamic_session_fallback,
        )
        processed_row_count = int(payload.get("source_row_count") or 0)
        session_count = int(payload.get("session_count") or 0)
        source_relation = str(payload.get("source_relation") or SOURCE_RELATION)

        ensure_ranking_snapshot_table(conn)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                insert into {RANKING_SNAPSHOT_TABLE} (
                    snapshot_date,
                    generated_at,
                    source_relation,
                    source_row_count,
                    session_count,
                    payload
                )
                values (%s, %s, %s, %s, %s, %s::jsonb)
                on conflict (snapshot_date) do update
                set
                    generated_at = excluded.generated_at,
                    source_relation = excluded.source_relation,
                    source_row_count = excluded.source_row_count,
                    session_count = excluded.session_count,
                    payload = excluded.payload
                """,
                [
                    snapshot_date,
                    generated_at.replace(tzinfo=None),
                    source_relation,
                    processed_row_count,
                    session_count,
                    json.dumps(payload, ensure_ascii=False),
                ],
            )
        conn.commit()

    return payload


def fetch_latest_ranking_snapshot() -> dict[str, Any] | None:
    with create_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                select snapshot_date, generated_at, source_row_count, session_count, payload
                from {RANKING_SNAPSHOT_TABLE}
                order by snapshot_date desc, generated_at desc
                limit 1
                """
            )
            row = cur.fetchone()

    if row is None:
        return None

    snapshot_date, generated_at, source_row_count, session_count, payload = row
    ranking = json.loads(payload) if isinstance(payload, str) else dict(payload)
    ranking.setdefault("snapshot_date", snapshot_date.isoformat())
    ranking.setdefault("snapshot_generated_at", generated_at.strftime("%Y-%m-%d %H:%M"))
    ranking.setdefault("source_row_count", int(source_row_count))
    ranking.setdefault("source_row_count_label", "rekordow zrodla")
    ranking.setdefault("session_count", int(session_count))
    ranking.setdefault("source_relation", SOURCE_RELATION)
    ranking.setdefault("snapshot_timezone", RANKING_SNAPSHOT_TIMEZONE)
    ranking.setdefault("truncated", False)
    ranking.setdefault("score_help", build_reliability_score_help())
    if not ranking.get("score_formula"):
        score_help = ranking["score_help"]
        ranking["score_formula"] = (
            f"{score_help['formula']} eff, RX i TX sa tylko metrykami wspierajacymi."
        )
    return ranking


def parse_ranking_snapshot_date(snapshot: dict[str, Any] | None) -> date | None:
    if not snapshot:
        return None

    raw_value = str(snapshot.get("snapshot_date") or "").strip()
    if not raw_value:
        return None

    try:
        return date.fromisoformat(raw_value)
    except ValueError:
        return None


def build_missing_ranking_snapshot_message() -> str:
    return "Brak wygenerowanego snapshotu rankingu. Uruchom job generujący ranking."


def decorate_ranking_snapshot_for_view(
    ranking: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    ranking_view = dict(ranking)
    reference_now = now or get_ranking_snapshot_now()
    snapshot_date = parse_ranking_snapshot_date(ranking_view)
    ranking_view["snapshot_is_current"] = bool(snapshot_date and snapshot_date >= reference_now.date())
    ranking_view["snapshot_status_message"] = ""

    if snapshot_date is None:
        return ranking_view

    if ranking_view["snapshot_is_current"]:
        return ranking_view

    ranking_view["snapshot_status_message"] = (
        f"Pokazano ostatni zapisany snapshot z {snapshot_date.isoformat()}."
    )
    return ranking_view


def load_ranking_view_state() -> dict[str, Any]:
    # Ranking globalny obejmuje cala baze i miliony rekordow, wiec web moze tylko
    # odczytac ostatni gotowy snapshot. Request HTTP ani polling endpoint nie moga
    # uruchamiac synchronicznego ani automatycznego liczenia calego rankingu.
    ranking = fetch_latest_ranking_snapshot()
    if ranking is not None:
        reference_now = get_ranking_snapshot_now()
        return {
            "ranking": decorate_ranking_snapshot_for_view(ranking, now=reference_now),
            "ranking_error_message": "",
        }
    return {
        "ranking": None,
        "ranking_error_message": build_missing_ranking_snapshot_message(),
    }


def build_ranking_snapshot_error_message(exc: Exception) -> str:
    connection_summary = build_db_connection_summary()
    db_message = sanitize_db_exception_message(exc)

    if isinstance(exc, psycopg.errors.UndefinedTable):
        return (
            "Brak tabeli nocnego snapshotu rankingu. "
            "Uruchom generate_ranking_snapshot.py albo skrypt SQL tworzacy snapshoty. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return (
            "Brak wymaganych uprawnien do odczytu snapshotu rankingu. "
            f"{connection_summary}"
        )

    return (
        "Nie udalo sie odczytac nocnego snapshotu rankingu. "
        f"{connection_summary}"
        + (f" Szczegoly: {db_message}" if db_message else "")
    )


def build_user_error_message(exc: Exception) -> str:
    connection_summary = build_db_connection_summary()
    db_message = sanitize_db_exception_message(exc)

    if isinstance(exc, psycopg.errors.UndefinedTable):
        return (
            f"Relacja {SOURCE_RELATION} nie istnieje w bazie. "
            "Ustaw DB_SOURCE_RELATION na istniejaca tabele lub widok. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.UndefinedColumn):
        return (
            f"Relacja {SOURCE_RELATION} ma inny zestaw kolumn niz oczekiwany. "
            "Odswiez materialized view albo uruchom aplikacje na zgodnej relacji zrodlowej. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.UndefinedFunction):
        return (
            "W bazie brakuje funkcji pomocniczych wymaganych przez nowa analize. "
            "Uruchom skrypt SQL aktualizujacy widok i funkcje charging log. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.InsufficientPrivilege):
        return (
            "Brak wymaganych uprawnien w bazie. "
            "Potrzebujesz co najmniej SELECT do relacji wskazanej przez DB_SOURCE_RELATION. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.InvalidPassword):
        return (
            "Bledne haslo do PostgreSQL. Sprawdz DB_PASSWORD i DB_USER. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.InvalidCatalogName):
        return (
            "Wskazana baza PostgreSQL nie istnieje albo nie jest dostepna. "
            "Sprawdz DB_NAME. "
            f"{connection_summary}"
        )

    if isinstance(exc, psycopg.errors.QueryCanceled):
        return (
            "Zapytanie analizy przekroczylo limit czasu po stronie PostgreSQL. "
            "Zawez filtry projektu / software / telefonu albo zakres dat. "
            f"{connection_summary}"
            + (f" Szczegoly: {db_message}" if db_message else "")
        )

    if isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
        lowered = db_message.lower()
        if any(bit in lowered for bit in ("could not translate host name", "name or service not known", "nodename nor servname", "getaddrinfo")):
            return (
                "Nie mozna rozwiazac nazwy hosta PostgreSQL. Sprawdz DB_HOST i polaczenie sieciowe. "
                f"{connection_summary} Szczegoly: {db_message}"
            )
        if any(bit in lowered for bit in ("connection refused", "actively refused")):
            return (
                "Serwer PostgreSQL odrzuca polaczenie. Sprawdz DB_HOST, DB_PORT i czy serwer nasluchuje. "
                f"{connection_summary} Szczegoly: {db_message}"
            )
        if any(bit in lowered for bit in ("timeout expired", "timed out")):
            return (
                "Polaczenie do PostgreSQL przekroczylo limit czasu. Sprawdz siec, DB_HOST, DB_PORT i VPN. "
                f"{connection_summary} Szczegoly: {db_message}"
            )
        if "ssl" in lowered:
            return (
                "Problem z negocjacja SSL do PostgreSQL. Sprawdz DB_SSLMODE i ustawienia serwera. "
                f"{connection_summary} Szczegoly: {db_message}"
            )
        if any(bit in lowered for bit in ("password authentication failed", "authentication failed")):
            return (
                "Uwierzytelnienie do PostgreSQL nie powiodlo sie. Sprawdz DB_USER i DB_PASSWORD. "
                f"{connection_summary} Szczegoly: {db_message}"
            )
        return (
            "Nie udalo sie nawiazac polaczenia z PostgreSQL. "
            f"{connection_summary} Szczegoly: {db_message or exc.__class__.__name__}"
        )

    message = str(exc).strip()
    if message.startswith("Missing required environment variable:"):
        missing_name = message.split(":", 1)[1].strip()
        return (
            f"Brakuje zmiennej srodowiskowej {missing_name}. "
            "Mozesz ustawic ja w systemie albo zapisac w pliku .env / db.env obok app.py."
        )

    if isinstance(exc, TimeoutError):
        return str(exc)

    return (
        "Nie udalo sie pobrac danych z bazy. Sprawdz konfiguracje polaczenia, "
        f"DB_SOURCE_RELATION i istnienie relacji {SOURCE_RELATION}. "
        f"{connection_summary}"
        + (f" Szczegoly: {db_message}" if db_message else "")
    )


def prune_analysis_cache_locked(now: float) -> None:
    expired_keys = [
        key
        for key, entry in ANALYSIS_CACHE.items()
        if entry.get("status") != "pending" and entry.get("expires_at", 0.0) <= now
    ]
    for key in expired_keys:
        ANALYSIS_CACHE.pop(key, None)


def build_failed_analysis_result(exc: Exception) -> dict[str, Any]:
    LOGGER.exception("Background analysis job failed unexpectedly")
    return {
        "status": "error",
        "analysis": None,
        "analysis_error_message": build_user_error_message(exc),
    }


def finalize_analysis_future_locked(
    cache_key: tuple[Any, ...],
    result: dict[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    finalized_entry = {
        **result,
        "expires_at": now + ANALYSIS_CACHE_TTL_SECONDS,
    }
    ANALYSIS_CACHE[cache_key] = finalized_entry
    return finalized_entry


def store_analysis_future_result(cache_key: tuple[Any, ...], future: Future[dict[str, Any]]) -> None:
    now = time.monotonic()
    try:
        result = future.result()
    except Exception as exc:
        result = build_failed_analysis_result(exc)

    with ANALYSIS_CACHE_LOCK:
        finalize_analysis_future_locked(cache_key, result, now=now)


def build_background_analysis(scope: FilterScope) -> dict[str, Any]:
    started_at = time.monotonic()
    fast_limit = resolve_fast_analysis_candidate_group_limit(scope)
    retry_limits = [
        fast_limit,
        *(limit for limit in ANALYSIS_RETRY_LIMITS_FALLBACK if limit < fast_limit),
        *(limit for limit in ANALYSIS_EMERGENCY_RETRY_LIMITS if limit < fast_limit),
    ]
    retry_limits = list(dict.fromkeys(retry_limits))
    if fast_limit not in retry_limits:
        retry_limits.insert(0, fast_limit)

    try:
        last_timeout_error: Exception | None = None
        attempt_index = 0
        for candidate_limit in retry_limits:
            elapsed = time.monotonic() - started_at
            remaining_ms = max(1000, int((ANALYSIS_TIME_BUDGET_SECONDS - elapsed) * 1000))
            if remaining_ms <= 1000 and attempt_index > 0:
                break
            statement_timeout_ms = min(
                ANALYSIS_QUERY_TIMEOUT_MS if attempt_index == 0 else ANALYSIS_QUERY_TIMEOUT_RETRY_MS,
                remaining_ms,
            )
            try:
                dataset = fetch_analysis_dataset(
                    scope,
                    limit=candidate_limit,
                    problem_candidates_only=True,
                    include_totals=False,
                    statement_timeout_ms=statement_timeout_ms,
                )
                analysis = build_charging_analysis(
                    dataset["rows"],
                    total_rows=dataset["total_rows"],
                    scope=scope,
                    candidate_group_total=dataset.get("candidate_group_total"),
                    included_candidate_group_count=dataset.get("included_candidate_group_count"),
                    counts_complete=bool(dataset.get("counts_complete", False)),
                )
                analysis["runtime_seconds"] = round(time.monotonic() - started_at, 2)
                analysis["analysis_limit_used"] = candidate_limit
                return {
                    "status": "ready",
                    "analysis": analysis,
                    "analysis_error_message": "",
                }
            except psycopg.errors.QueryCanceled as exc:
                last_timeout_error = exc
                LOGGER.warning(
                    "Analysis query timed out for %s at limit=%s after %sms; retrying smaller scope",
                    SOURCE_RELATION,
                    candidate_limit,
                    statement_timeout_ms,
                )
                attempt_index += 1
                continue

        if last_timeout_error is not None:
            elapsed = time.monotonic() - started_at
            remaining_ms = max(1000, int((ANALYSIS_TIME_BUDGET_SECONDS - elapsed) * 1000))
            try:
                fallback_dataset = fetch_recent_analysis_rows(
                    scope,
                    limit=ANALYSIS_RECENT_ROWS_FALLBACK_LIMIT,
                    statement_timeout_ms=min(ANALYSIS_QUERY_TIMEOUT_RETRY_MS, remaining_ms),
                )
                analysis = build_charging_analysis(
                    fallback_dataset["rows"],
                    total_rows=fallback_dataset["total_rows"],
                    scope=scope,
                    candidate_group_total=fallback_dataset.get("candidate_group_total"),
                    included_candidate_group_count=fallback_dataset.get("included_candidate_group_count"),
                    counts_complete=bool(fallback_dataset.get("counts_complete", False)),
                )
                analysis["runtime_seconds"] = round(time.monotonic() - started_at, 2)
                analysis["analysis_limit_used"] = f"recent_rows:{ANALYSIS_RECENT_ROWS_FALLBACK_LIMIT}"
                analysis.setdefault("highlights", []).insert(
                    0,
                    "Pelny prefiltr problemow przekroczyl budzet czasu, wiec pokazano awaryjnie raport z najnowszych rekordow.",
                )
                return {
                    "status": "ready",
                    "analysis": analysis,
                    "analysis_error_message": "",
                }
            except psycopg.errors.QueryCanceled:
                pass
            raise TimeoutError(
                "Analiza przekroczyla budzet czasu. Zawez filtry projektu / software / telefonu albo zakres dat."
            ) from last_timeout_error
        raise RuntimeError("Background analysis finished without result.")
    except Exception as exc:
        LOGGER.exception("Failed to build background analysis from %s", SOURCE_RELATION)
        return {
            "status": "error",
            "analysis": None,
            "analysis_error_message": build_user_error_message(exc),
        }


def get_or_start_analysis_job(scope: FilterScope) -> dict[str, Any]:
    cache_key = scope.cache_key()
    now = time.monotonic()

    with ANALYSIS_CACHE_LOCK:
        prune_analysis_cache_locked(now)
        cached_entry = ANALYSIS_CACHE.get(cache_key)
        if cached_entry is not None:
            if cached_entry.get("status") == "pending":
                future = cached_entry.get("future")
                if isinstance(future, Future) and future.done():
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = build_failed_analysis_result(exc)
                    cached_entry = finalize_analysis_future_locked(cache_key, result, now=now)
                else:
                    return {"status": "pending"}
            if cached_entry is not None:
                return {key: value for key, value in cached_entry.items() if key != "future"}

        future = ANALYSIS_EXECUTOR.submit(build_background_analysis, scope)
        future.add_done_callback(lambda finished, key=cache_key: store_analysis_future_result(key, finished))
        ANALYSIS_CACHE[cache_key] = {
            "status": "pending",
            "future": future,
        }

    return {"status": "pending"}


app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)


@app.template_filter("pretty_json")
def pretty_json(value: Any) -> str:
    if value is None:
        return ""
    parsed = parse_json_payload(value)
    if parsed is not None:
        return json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def render_analysis_content(
    *,
    analysis: dict[str, Any] | None,
    analysis_error_message: str,
    page: int,
    page_size: int,
    q: str,
    phone: str,
    project_number: str,
    software_version: str,
    position: str,
    classification: str,
    test_type: str,
    sample: str,
    defect_id: str,
    dual_charging: str,
    event_ts_from: str,
    event_ts_to: str,
    inserted_at_from: str,
    inserted_at_to: str,
    sort_by: str,
    sort_dir: str,
) -> str:
    return render_template(
        "_analysis_content.html",
        analysis=analysis,
        analysis_error_message=analysis_error_message,
        page=page,
        page_size=page_size,
        q=q,
        phone=phone,
        project_number=project_number,
        software_version=software_version,
        position=position,
        classification=classification,
        test_type=test_type,
        sample=sample,
        defect_id=defect_id,
        dual_charging=dual_charging,
        event_ts_from=event_ts_from,
        event_ts_to=event_ts_to,
        inserted_at_from=inserted_at_from,
        inserted_at_to=inserted_at_to,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


def render_ranking_content(
    *,
    ranking: dict[str, Any] | None,
    ranking_error_message: str,
) -> str:
    return render_template(
        "_ranking_content.html",
        ranking=ranking,
        ranking_error_message=ranking_error_message,
    )


@app.route("/analysis/status")
def analysis_status():
    page = parse_positive_int(request.args.get("page"), default=1)
    page_size = parse_positive_int(
        request.args.get("page_size"),
        default=DEFAULT_PAGE_SIZE,
        maximum=MAX_PAGE_SIZE,
    )
    search_text = request.args.get("q", "").strip()
    phone = request.args.get("phone", "").strip()
    project_number = request.args.get("project_number", "").strip()
    software_version = request.args.get("software_version", "").strip()
    position = request.args.get("position", "").strip()
    classification = request.args.get("classification", "").strip()
    test_type = request.args.get("test_type", "").strip()
    sample = request.args.get("sample", "").strip()
    defect_id = request.args.get("defect_id", "").strip()
    dual_charging = request.args.get("dual_charging", "").strip()
    event_ts_from_raw = request.args.get("event_ts_from", "").strip()
    event_ts_to_raw = request.args.get("event_ts_to", "").strip()
    inserted_at_from_raw = request.args.get("inserted_at_from", "").strip()
    inserted_at_to_raw = request.args.get("inserted_at_to", "").strip()
    sort_by = request.args.get("sort", "event_ts")
    sort_dir = request.args.get("dir", "desc").lower()

    if sort_by not in SORTABLE_COLUMNS:
        sort_by = "event_ts"
    if sort_dir not in SORT_DIRECTIONS:
        sort_dir = "desc"

    scope = build_filter_scope(
        search_text=search_text,
        phone=phone,
        project_number=project_number,
        software_version=software_version,
        position=position,
        classification=classification,
        test_type=test_type,
        sample=sample,
        defect_id=defect_id,
        dual_charging=dual_charging,
        event_ts_from=parse_optional_date(event_ts_from_raw),
        event_ts_to=parse_optional_date(event_ts_to_raw),
        inserted_at_from=parse_optional_date(inserted_at_from_raw),
        inserted_at_to=parse_optional_date(inserted_at_to_raw),
    )
    job_state = get_or_start_analysis_job(scope)
    if job_state["status"] == "pending":
        return (
            jsonify(
                {
                    "status": "pending",
                    "message": (
                        "Analiza liczy sie w tle. Widok odswiezy sie automatycznie po zakonczeniu obliczen."
                    ),
                    "poll_interval_ms": ANALYSIS_POLL_INTERVAL_MS,
                }
            ),
            202,
        )

    html = render_analysis_content(
        analysis=job_state.get("analysis"),
        analysis_error_message=job_state.get("analysis_error_message", ""),
        page=page,
        page_size=page_size,
        q=search_text,
        phone=phone,
        project_number=project_number,
        software_version=software_version,
        position=position,
        classification=classification,
        test_type=test_type,
        sample=sample,
        defect_id=defect_id,
        dual_charging=dual_charging,
        event_ts_from=event_ts_from_raw,
        event_ts_to=event_ts_to_raw,
        inserted_at_from=inserted_at_from_raw,
        inserted_at_to=inserted_at_to_raw,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )
    return jsonify(
        {
            "status": job_state["status"],
            "html": html,
        }
    )


@app.route("/ranking/status")
def ranking_status():
    try:
        ranking_state = load_ranking_view_state()
    except Exception as exc:
        LOGGER.exception("Failed to refresh ranking snapshot state from %s", RANKING_SNAPSHOT_TABLE)
        return (
            render_ranking_content(
                ranking=None,
                ranking_error_message=build_ranking_snapshot_error_message(exc),
            ),
            500,
            {"Content-Type": "text/html; charset=utf-8"},
        )

    return (
        render_ranking_content(
            ranking=ranking_state.get("ranking"),
            ranking_error_message=ranking_state.get("ranking_error_message", ""),
        ),
        200,
        {"Content-Type": "text/html; charset=utf-8"},
    )


@app.route("/")
def index():
    page = parse_positive_int(request.args.get("page"), default=1)
    page_size = parse_positive_int(
        request.args.get("page_size"),
        default=DEFAULT_PAGE_SIZE,
        maximum=MAX_PAGE_SIZE,
    )
    search_text = request.args.get("q", "").strip()
    phone = request.args.get("phone", "").strip()
    project_number = request.args.get("project_number", "").strip()
    software_version = request.args.get("software_version", "").strip()
    position = request.args.get("position", "").strip()
    classification = request.args.get("classification", "").strip()
    test_type = request.args.get("test_type", "").strip()
    sample = request.args.get("sample", "").strip()
    defect_id = request.args.get("defect_id", "").strip()
    dual_charging = request.args.get("dual_charging", "").strip()
    event_ts_from_raw = request.args.get("event_ts_from", "").strip()
    event_ts_to_raw = request.args.get("event_ts_to", "").strip()
    inserted_at_from_raw = request.args.get("inserted_at_from", "").strip()
    inserted_at_to_raw = request.args.get("inserted_at_to", "").strip()
    event_ts_from = parse_optional_date(event_ts_from_raw)
    event_ts_to = parse_optional_date(event_ts_to_raw)
    inserted_at_from = parse_optional_date(inserted_at_from_raw)
    inserted_at_to = parse_optional_date(inserted_at_to_raw)
    scope = build_filter_scope(
        search_text=search_text,
        phone=phone,
        project_number=project_number,
        software_version=software_version,
        position=position,
        classification=classification,
        test_type=test_type,
        sample=sample,
        defect_id=defect_id,
        dual_charging=dual_charging,
        event_ts_from=event_ts_from,
        event_ts_to=event_ts_to,
        inserted_at_from=inserted_at_from,
        inserted_at_to=inserted_at_to,
    )
    sort_by = request.args.get("sort", "event_ts")
    sort_dir = request.args.get("dir", "desc").lower()
    active_tab = request.args.get("tab", "analysis").strip().lower()
    run_analysis_requested = request.args.get("run_analysis", "").strip().lower() in {"1", "true", "yes", "on"}

    if active_tab == "results":
        active_tab = "analysis"

    if sort_by not in SORTABLE_COLUMNS:
        sort_by = "event_ts"
    if sort_dir not in SORT_DIRECTIONS:
        sort_dir = "desc"
    if active_tab not in AVAILABLE_TABS:
        active_tab = "analysis"

    try:
        filter_options = {"phone": [], "project_number": [], "software_version": [], "position": []}
        result = None
        total_rows = 0
        total_pages = 1
        current_page = 1

        if active_tab == "analysis":
            filter_options = fetch_filter_options()
            total_rows = 0
            total_pages = 1
            current_page = 1

        analysis = None
        analysis_error_message = ""
        ranking = None
        ranking_error_message = ""
        analysis_autostart = False
        if active_tab == "ranking":
            try:
                ranking_state = load_ranking_view_state()
                ranking = ranking_state.get("ranking")
                ranking_error_message = ranking_state.get("ranking_error_message", "")
            except Exception as exc:
                LOGGER.exception("Failed to load nightly ranking snapshot from %s", RANKING_SNAPSHOT_TABLE)
                ranking_error_message = build_ranking_snapshot_error_message(exc)
        elif active_tab == "analysis" and run_analysis_requested:
            analysis_result = build_background_analysis(scope)
            analysis = analysis_result.get("analysis")
            analysis_error_message = analysis_result.get("analysis_error_message", "")
        return render_template(
            "index.html",
            rows=result["rows"] if result is not None else [],
            total_rows=total_rows,
            total_pages=total_pages,
            page=current_page,
            page_size=page_size,
            q=search_text,
            phone=phone,
            project_number=project_number,
            software_version=software_version,
            phone_options=filter_options["phone"],
            project_number_options=filter_options["project_number"],
            software_version_options=filter_options["software_version"],
            position_options=filter_options.get("position", []),
            event_ts_from=event_ts_from_raw,
            event_ts_to=event_ts_to_raw,
            inserted_at_from=inserted_at_from_raw,
            inserted_at_to=inserted_at_to_raw,
            position=position,
            classification=classification,
            test_type=test_type,
            sample=sample,
            defect_id=defect_id,
            dual_charging=dual_charging,
            sort_by=sort_by,
            sort_dir=sort_dir,
            db_name=os.environ.get("DB_NAME", ""),
            db_host=os.environ.get("DB_HOST", ""),
            table_name="Charging Log Viewer",
            source_relation=SOURCE_RELATION,
            active_tab=active_tab,
            run_analysis_requested=run_analysis_requested,
            analysis_autostart=analysis_autostart,
            analysis=analysis,
            analysis_error_message=analysis_error_message,
            analysis_status_url=url_for(
                "analysis_status",
                page=current_page,
                page_size=page_size,
                q=search_text,
                phone=phone,
                project_number=project_number,
                software_version=software_version,
                position=position,
                classification=classification,
                test_type=test_type,
                sample=sample,
                defect_id=defect_id,
                dual_charging=dual_charging,
                event_ts_from=event_ts_from_raw,
                event_ts_to=event_ts_to_raw,
                inserted_at_from=inserted_at_from_raw,
                inserted_at_to=inserted_at_to_raw,
                sort=sort_by,
                dir=sort_dir,
            ),
            analysis_poll_interval_ms=ANALYSIS_POLL_INTERVAL_MS,
            ranking=ranking,
            ranking_error_message=ranking_error_message,
            error_message="",
        )
    except Exception as exc:
        LOGGER.exception("Failed to load data from %s", SOURCE_RELATION)
        return (
            render_template(
                "index.html",
                rows=[],
                total_rows=0,
                total_pages=1,
                page=1,
                page_size=page_size,
                q=search_text,
                phone=phone,
                project_number=project_number,
                software_version=software_version,
                phone_options=[],
                project_number_options=[],
                software_version_options=[],
                position_options=[],
                event_ts_from=event_ts_from_raw,
                event_ts_to=event_ts_to_raw,
                inserted_at_from=inserted_at_from_raw,
                inserted_at_to=inserted_at_to_raw,
                position=position,
                classification=classification,
                test_type=test_type,
                sample=sample,
                defect_id=defect_id,
                dual_charging=dual_charging,
                sort_by=sort_by,
                sort_dir=sort_dir,
                db_name=os.environ.get("DB_NAME", ""),
                db_host=os.environ.get("DB_HOST", ""),
                table_name="Charging Log Viewer",
                source_relation=SOURCE_RELATION,
                active_tab=active_tab,
                run_analysis_requested=run_analysis_requested,
                analysis_autostart=False,
                analysis=None,
                analysis_error_message="",
                analysis_status_url="",
                analysis_poll_interval_ms=ANALYSIS_POLL_INTERVAL_MS,
                ranking=None,
                ranking_error_message="",
                error_message=build_user_error_message(exc),
            ),
            500,
        )


if __name__ == "__main__":
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "5000"))
    app.run(host=host, port=port, debug=False)
