from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .fields import (
    ANGLE, DROPPED_HEADERS, FIELDS_BY_KEY, HEADER_LOOKUP, KNOWN_UNMAPPED, LENGTH,
    PERCENT, RATIO, SPEED, SPIN, TIME, Field, norm_header,
)


class ParseError(ValueError):
    pass


_METERS = {"yds": 0.9144, "yd": 0.9144, "m": 1.0, "ft": 0.3048, "in": 0.0254, "cm": 0.01, "mm": 0.001}
_MPH = {"mph": 1.0, "m/s": 2.2369362920544, "km/h": 0.621371192237334, "kph": 0.621371192237334}
_SAME = {
    ANGLE: {"deg", "°", "degrees"},
    SPIN: {"rpm"},
    TIME: {"s", "sec"},
    RATIO: {""},
    PERCENT: {"%"},
}
_MISSING = {"", "-", "---", "n/a", "na", "nan"}
_DATE_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",
    "%m/%d/%Y %I:%M %p",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
)
_UNIT_IN_HEADER = re.compile(r"^(.*?)\s*\[([^\]]*)\]\s*$")


@dataclass
class ParsedExport:
    shots: list[dict]
    delimiter: str
    source_units: dict[str, str]
    unmapped: list[str]
    warnings: list[str] = field(default_factory=list)


def unit_factor(f: Field, unit: str) -> float:
    u = unit.strip().lower()
    if f.kind == LENGTH:
        if u not in _METERS:
            raise ParseError(f"{f.label}: unknown length unit [{unit}]")
        return _METERS[u] / _METERS[f.unit]
    if f.kind == SPEED:
        if u not in _MPH:
            raise ParseError(f"{f.label}: unknown speed unit [{unit}]")
        return _MPH[u]
    if u not in _SAME[f.kind]:
        raise ParseError(f"{f.label}: unexpected unit [{unit}], expected [{f.unit}]")
    return 1.0


def parse_number(text: str) -> float | None:
    s = text.strip().replace("\u2212", "-")
    if s.lower() in _MISSING:
        return None
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    value = float(s)
    return value if math.isfinite(value) else None


def parse_date(text: str) -> datetime:
    s = " ".join(text.strip().split())
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ParseError(f"unrecognised date format: {text!r}")


def _split_lines(text: str) -> tuple[str, list[str]]:
    lines = text.lstrip("\ufeff").splitlines()
    if lines and lines[0].strip().lower().startswith("sep="):
        sep = lines[0].strip()[4:5] or ","
        return sep, lines[1:]
    head = lines[0] if lines else ""
    return (";" if head.count(";") > head.count(",") else ","), lines


def _is_units_row(row: list[str]) -> bool:
    cells = [c.strip() for c in row if c.strip()]
    return bool(cells) and all(c.startswith("[") and c.endswith("]") for c in cells)


def _split_header(cell: str) -> tuple[str, str | None]:
    name = cell.strip()
    m = _UNIT_IN_HEADER.match(name)
    return (m.group(1).strip(), m.group(2)) if m else (name, None)


def _find_header(rows: list[list[str]]) -> int:
    for i, row in enumerate(rows[:10]):
        keys = {HEADER_LOOKUP.get(norm_header(_split_header(c)[0])) for c in row}
        if "club" in keys and ({"ball_speed", "club_speed"} & keys):
            return i
    raise ParseError("no TrackMan header row found (expected Club plus Ball Speed or Club Speed)")


def parse_tps_csv(source: str | Path) -> ParsedExport:
    text = Path(source).read_text(encoding="utf-8-sig") if isinstance(source, Path) else source
    delimiter, lines = _split_lines(text)
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter))
    h = _find_header(rows)
    header = rows[h]
    units_row = rows[h + 1] if h + 1 < len(rows) and _is_units_row(rows[h + 1]) else None
    data_rows = rows[h + (2 if units_row else 1):]

    columns: list[tuple[int, str, str | None, str | None]] = []
    unmapped: list[str] = []
    source_units: dict[str, str] = {}
    for i, raw_name in enumerate(header):
        name, unit = _split_header(raw_name)
        if units_row is not None and i < len(units_row) and units_row[i].strip():
            unit = units_row[i].strip()[1:-1]
        norm = norm_header(name)
        if norm in DROPPED_HEADERS or not name:
            continue
        key = HEADER_LOOKUP.get(norm)
        if key is None and norm not in KNOWN_UNMAPPED:
            unmapped.append(name)
        columns.append((i, name, key, unit))

    factors: dict[str, float] = {}
    missing_units: list[str] = []
    for _, name, key, unit in columns:
        if key in FIELDS_BY_KEY and key not in factors:
            f = FIELDS_BY_KEY[key]
            if unit is None:
                if f.kind == RATIO:
                    unit = ""
                else:
                    missing_units.append(name)
                    continue
            factors[key] = unit_factor(f, unit)
            source_units[key] = unit
    if missing_units:
        raise ParseError("no units for: " + ", ".join(missing_units) + " (expected a [unit] row under the header)")

    shots: list[dict] = []
    warnings: list[str] = []
    for line_no, row in enumerate(data_rows, start=h + 3):
        if not any(c.strip() for c in row):
            continue
        shot: dict = {"raw": {}}
        seen: set[str] = set()
        for i, name, key, _ in columns:
            cell = row[i] if i < len(row) else ""
            shot["raw"][name] = cell
            if key is None or key in seen:
                continue
            seen.add(key)
            if key in FIELDS_BY_KEY:
                try:
                    value = parse_number(cell)
                except ValueError:
                    warnings.append(f"line {line_no}: {name}={cell!r} is not a number")
                    value = None
                shot[key] = None if value is None else value * factors[key]
            else:
                shot[key] = cell.strip()
        if not shot.get("date") or not shot.get("club"):
            warnings.append(f"line {line_no}: skipped, no date or club")
            continue
        shot["ts"] = parse_date(shot["date"])
        shot["use_in_stat"] = shot.get("use_in_stat", "").strip().upper() not in {"FALSE", "0", "NO"}
        shots.append(shot)
    if not shots:
        raise ParseError("export contains no shot rows")
    return ParsedExport(shots, delimiter, source_units, unmapped, warnings)
