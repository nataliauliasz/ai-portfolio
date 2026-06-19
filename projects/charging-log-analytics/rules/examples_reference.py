from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from threading import Lock
from typing import Any
from zipfile import ZipFile


EXAMPLES_PROJECT_PATTERN = re.compile(r"^(?P<project>\d{4}_\d{3})_")
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
GREEN_FILLS = {"FF00B050", "FF47D359", "FF92D050"}
WARNING_FILLS = {"FFFFCC00", "FFF2CEEF"}
DEFECT_FILLS = {"FFFF4B4B"}

_CACHE_LOCK = Lock()
_CACHE: dict[str, Any] = {
    "source_dir": None,
    "value": None,
}


def normalize_examples_text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).replace("\n", " ").split())


def extract_position_number(value: Any) -> int | None:
    label = str(value or "").strip()
    if not label:
        return None

    match = re.search(r"\bP\s*(\d+)\b", label, re.IGNORECASE)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            return None
    return None


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
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [
        "".join((node.text or "") for node in item.iterfind(".//main:t", namespace))
        for item in root.findall("main:si", namespace)
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


def _examples_read_styles(archive: ZipFile) -> tuple[list[str | None], list[int]]:
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    styles_root = ET.fromstring(archive.read("xl/styles.xml"))

    fills: list[str | None] = []
    fills_root = styles_root.find("main:fills", namespace)
    if fills_root is not None:
        for fill in fills_root:
            pattern = fill.find("main:patternFill", namespace)
            fg = pattern.find("main:fgColor", namespace) if pattern is not None else None
            fills.append(fg.attrib.get("rgb") if fg is not None else None)

    xf_fill_ids: list[int] = []
    cell_xfs_root = styles_root.find("main:cellXfs", namespace)
    if cell_xfs_root is not None:
        for xf in cell_xfs_root:
            xf_fill_ids.append(int(xf.attrib.get("fillId", "0")))
    return fills, xf_fill_ids


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


def _examples_extract_fill_rgb(
    cell: ET.Element,
    fill_rgbs: list[str | None],
    xf_fill_ids: list[int],
) -> str | None:
    try:
        style_id = int(cell.attrib.get("s", "0"))
    except ValueError:
        return None
    if style_id < 0 or style_id >= len(xf_fill_ids):
        return None
    fill_id = xf_fill_ids[style_id]
    if fill_id < 0 or fill_id >= len(fill_rgbs):
        return None
    return fill_rgbs[fill_id]


def _parse_status_codes(text: str) -> set[str]:
    return set(re.findall(r"\b(?:status|state)\s*(\d+)\b", text, flags=re.IGNORECASE))


def _parse_percent_value(text: str) -> float | None:
    match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*%", text)
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _parse_temperature_c(text: str) -> float | None:
    match = re.search(r"(-?\d+(?:[.,]\d+)?)\s*(?:Â°c|c)\b", text, flags=re.IGNORECASE)
    if match is None:
        match = re.search(r"temp\s*(-?\d+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
    if match is None:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _classify_fod_cell(project_number: str, text: str, fill_rgb: str | None) -> tuple[str | None, str | None]:
    lowered = text.lower()
    statuses = _parse_status_codes(text)
    temperature_c = _parse_temperature_c(text)

    if "test not required" in lowered or lowered.startswith("test were stopped"):
        return "skip", "test stopped or not required"
    if "no fod detected" in lowered or "no charging" in lowered:
        return "not_ok", "text indicates missing FOD detection or unexpected lack of charging"
    if fill_rgb in DEFECT_FILLS:
        return "defect", "manual workbook marks the cell red"
    if fill_rgb in WARNING_FILLS:
        return "not_ok", "manual workbook marks the cell yellow/pink"
    if fill_rgb in GREEN_FILLS:
        return "pass", "manual workbook marks the cell green"
    if temperature_c is not None and temperature_c >= 45:
        return "not_ok", "temperature in workbook reaches a warning level"
    if statuses & {"13", "17"}:
        return "not_ok", "status 13/17 indicates system error in FOD"
    if statuses & {"4", "6"}:
        return "pass", "status 4/6 in FOD sheet"
    if statuses & {"3", "5"}:
        return "not_ok", "state 3/5 in FOD means charging still continues or only partially blocks"
    if _parse_percent_value(text) is not None:
        return "not_ok", "percentage-only FOD result without explicit rule marker"
    return None, None


def classify_examples_cell(
    project_number: str,
    sheet_name: str,
    raw_text: Any,
    fill_rgb: str | None = None,
) -> tuple[str | None, str | None]:
    text = normalize_examples_text(raw_text)
    if not text:
        return None, None

    lowered = text.lower()
    if lowered in {"#value!", "#ref!", "-", "n/a", "na"}:
        return None, None
    if "test not required" in lowered or lowered == "tested" or lowered.startswith("tested on sw"):
        return "skip", "test was marked as not required"

    if sheet_name == "NFC":
        if lowered == "ok":
            return "pass", "manual sheet marks NFC as OK"
        if "no ok" in lowered or lowered == "nok":
            return "not_ok", "manual sheet marks NFC as NO OK"
        if "defect id" in lowered or lowered.startswith("defect "):
            return "defect", "manual sheet names an NFC defect"
        return None, None

    if sheet_name in {"CHARGING 0 -100 %", "WLC"}:
        if "defect id" in lowered or lowered.startswith("defect "):
            return "defect", "manual note explicitly links the result to a defect"
        if re.search(r"\bstatus\s*3\b|\bstate\s*3\b", lowered):
            return "pass", "charging scenario stays in status 3"
        if sheet_name == "CHARGING 0 -100 %" and re.search(r"\bstatus\s*2\b", lowered):
            return "pass", "0-100 charging scenario ends in status 2"
        if "blower fault" in lowered:
            return "not_ok", "setup-related fault blocks a clean pass result"
        if "toggling" in lowered or "no charging" in lowered or "charging inter" in lowered:
            return "not_ok", "manual note points to unstable charging"
        if re.search(r"\bstatus\s*(8|9|13|14|17)\b|\bstate\s*(8|9|13|14|17)\b", lowered):
            return "not_ok", "system error or safety status in charging scenario"
        if re.search(r"\bstatus\s*(0|1|4|5|6|15|16)\b|\bstate\s*(0|1|4|5|6|15|16)\b", lowered):
            return "not_ok", "non-nominal charging status"
        return None, None

    if sheet_name == "FOD":
        return _classify_fod_cell(project_number, text, fill_rgb)

    if sheet_name in {"RFID Protection", "RFID Protection_2"}:
        if "defect id" in lowered or lowered.startswith("defect "):
            return "defect", "manual sheet names RFID defect"
        if re.search(r"\bstatus\s*(15|16)\b|\bstate\s*(15|16)\b", lowered):
            return "pass", "RFID protection behaves as expected in status 15/16"
        if "toggling" in lowered:
            return "not_ok", "RFID scenario is unstable instead of activating protection"
        if re.search(r"\bstatus\s*(1|3|4|6|8|9|13|17)\b|\bstate\s*(1|3|4|6|8|9|13|17)\b", lowered):
            return "not_ok", "RFID sheet shows unexpected charging or system-error status"
        return None, None

    return None, None


def _summarize_counter(counter: Counter[str], top_n: int = 4) -> str:
    if not counter:
        return "brak"
    return ", ".join(f"{value} ({count})" for value, count in counter.most_common(top_n))


def _build_reference_rule_notes(
    project_number: str,
    scenario_code: str,
    status_counter: Counter[str],
    fill_counter: Counter[str],
    reason_counter: Counter[str],
) -> str:
    fragments = []
    if status_counter:
        fragments.append(f"statusy: {_summarize_counter(status_counter)}")
    if fill_counter:
        fills = [f"{fill or 'brak koloru'} ({count})" for fill, count in fill_counter.most_common(3)]
        fragments.append(f"kolory: {', '.join(fills)}")
    if reason_counter:
        fragments.append(f"sygnaly: {_summarize_counter(reason_counter)}")
    if not fragments and scenario_code == "fod" and project_number == "0854_108":
        return "Referencja FOD ma slabe oznaczenia tekstowe i bez kolorow; reguly sa przyblizeniem do doprecyzowania."
    return " | ".join(fragments) if fragments else "Brak wyraznych sygnalow referencyjnych."


def load_examples_benchmarks(examples_dir: Path) -> dict[str, Any]:
    resolved_dir = str(examples_dir.resolve())
    with _CACHE_LOCK:
        if _CACHE["source_dir"] == resolved_dir and _CACHE["value"] is not None:
            return _CACHE["value"]

    benchmarks: dict[str, Any] = {}
    if examples_dir.exists():
        for workbook_path in sorted(examples_dir.glob("*.xlsx")):
            project_match = EXAMPLES_PROJECT_PATTERN.match(workbook_path.name)
            if project_match is None:
                continue

            project_number = project_match.group("project")
            project_entry = benchmarks.setdefault(
                project_number,
                {
                    "project_number": project_number,
                    "files": [],
                    "scenario_groups": {},
                    "position_groups": {},
                    "source_errors": [],
                },
            )
            project_entry["files"].append(workbook_path.name)

            with ZipFile(workbook_path) as archive:
                shared_strings = _examples_parse_shared_strings(archive)
                sheet_map = _examples_resolve_sheet_map(archive)
                fill_rgbs, xf_fill_ids = _examples_read_styles(archive)

                for sheet_name, scenario_config in EXAMPLES_SCENARIO_CONFIG.items():
                    sheet_path = sheet_map.get(sheet_name)
                    if sheet_path is None:
                        continue

                    scenario_group = project_entry["scenario_groups"].setdefault(
                        scenario_config["code"],
                        {
                            "label": scenario_config["label"],
                            "sheet_names": set(),
                            "files": set(),
                            "pass_count": 0,
                            "not_ok_count": 0,
                            "defect_count": 0,
                            "skip_count": 0,
                            "position_groups": defaultdict(lambda: Counter()),
                            "status_counter": Counter(),
                            "fill_counter": Counter(),
                            "reason_counter": Counter(),
                        },
                    )
                    scenario_group["sheet_names"].add(sheet_name)
                    scenario_group["files"].add(workbook_path.name)

                    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
                    rows_root = ET.fromstring(archive.read(sheet_path))
                    for row in rows_root.findall("main:sheetData/main:row", namespace)[3:]:
                        for cell in row.findall("main:c", namespace):
                            reference = cell.attrib.get("r", "")
                            column_label = "".join(char for char in reference if char.isalpha())
                            if not column_label:
                                continue
                            column_index = _examples_column_to_index(column_label)
                            if column_index < scenario_config["position_start_index"]:
                                continue

                            raw_value = _examples_read_cell_text(cell, shared_strings)
                            fill_rgb = _examples_extract_fill_rgb(cell, fill_rgbs, xf_fill_ids)
                            category, reason = classify_examples_cell(
                                project_number,
                                sheet_name,
                                raw_value,
                                fill_rgb=fill_rgb,
                            )
                            if category is None:
                                continue

                            position_number = column_index - scenario_config["position_start_index"] + 1
                            scenario_group[f"{category}_count"] += 1
                            scenario_group["position_groups"][position_number][category] += 1
                            if reason:
                                scenario_group["reason_counter"][reason] += 1
                            if fill_rgb:
                                scenario_group["fill_counter"][fill_rgb] += 1
                            for status_code in _parse_status_codes(normalize_examples_text(raw_value)):
                                scenario_group["status_counter"][status_code] += 1

                            overall_position_group = project_entry["position_groups"].setdefault(
                                position_number,
                                {
                                    "pass_count": 0,
                                    "not_ok_count": 0,
                                    "defect_count": 0,
                                    "skip_count": 0,
                                    "scenario_codes": set(),
                                },
                            )
                            overall_position_group[f"{category}_count"] += 1
                            overall_position_group["scenario_codes"].add(scenario_config["code"])

    for project_entry in benchmarks.values():
        scenario_rows = []
        for scenario_code, scenario_group in project_entry["scenario_groups"].items():
            considered = (
                scenario_group["pass_count"]
                + scenario_group["not_ok_count"]
                + scenario_group["defect_count"]
            )
            bad_count = scenario_group["not_ok_count"] + scenario_group["defect_count"]
            scenario_rows.append(
                {
                    "scenario_code": scenario_code,
                    "scenario_label": scenario_group["label"],
                    "file_count": len(scenario_group["files"]),
                    "pass_count": scenario_group["pass_count"],
                    "not_ok_count": scenario_group["not_ok_count"],
                    "defect_count": scenario_group["defect_count"],
                    "skip_count": scenario_group["skip_count"],
                    "position_count": len(scenario_group["position_groups"]),
                    "bad_rate": (bad_count / considered) if considered else None,
                    "defect_rate": (scenario_group["defect_count"] / considered) if considered else None,
                    "position_groups": dict(scenario_group["position_groups"]),
                    "dominant_statuses_label": _summarize_counter(scenario_group["status_counter"]),
                    "fill_label": _summarize_counter(scenario_group["fill_counter"]),
                    "reference_note": _build_reference_rule_notes(
                        project_entry["project_number"],
                        scenario_code,
                        scenario_group["status_counter"],
                        scenario_group["fill_counter"],
                        scenario_group["reason_counter"],
                    ),
                }
            )
        scenario_rows.sort(
            key=lambda item: (
                -(item["bad_rate"] or 0),
                -(item["defect_rate"] or 0),
                item["scenario_label"],
            )
        )
        project_entry["scenario_rows"] = scenario_rows

        position_rows = []
        for position_number, position_group in project_entry["position_groups"].items():
            considered = (
                position_group["pass_count"]
                + position_group["not_ok_count"]
                + position_group["defect_count"]
            )
            bad_count = position_group["not_ok_count"] + position_group["defect_count"]
            position_rows.append(
                {
                    "position_number": position_number,
                    "pass_count": position_group["pass_count"],
                    "not_ok_count": position_group["not_ok_count"],
                    "defect_count": position_group["defect_count"],
                    "skip_count": position_group["skip_count"],
                    "scenario_count": len(position_group["scenario_codes"]),
                    "bad_rate": (bad_count / considered) if considered else None,
                    "defect_rate": (position_group["defect_count"] / considered) if considered else None,
                }
            )
        position_rows.sort(
            key=lambda item: (
                -(item["bad_rate"] or 0),
                -(item["defect_rate"] or 0),
                item["position_number"],
            )
        )
        project_entry["position_rows"] = position_rows

    with _CACHE_LOCK:
        _CACHE["source_dir"] = resolved_dir
        _CACHE["value"] = benchmarks
    return benchmarks
