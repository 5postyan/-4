"""Load and fingerprint the local case inputs without modifying source files."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = ROOT / "rules.json"


def _stable_hash(value: Any) -> str:
    digest = hashlib.sha256()
    if isinstance(value, pd.DataFrame):
        digest.update("\0".join(map(str, value.columns)).encode("utf-8"))
        digest.update(pd.util.hash_pandas_object(value, index=True).values.tobytes())
    else:
        digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return digest.hexdigest()


def _normalized_name(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def _first_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    by_normalized_name = {_normalized_name(column): column for column in frame.columns}
    for name in names:
        column = by_normalized_name.get(_normalized_name(name))
        if column is not None:
            return column
    return None


def _timestamp_column(frame: pd.DataFrame) -> str:
    column = _first_column(frame, ("timestamp", "timestamp utc", "datetime", "date_time", "time", "date"))
    if column is None and len(frame.columns):
        # CSV exports often store their timestamp in the first, unnamed column.
        first = frame.columns[0]
        if str(first).startswith("Unnamed:"):
            column = first
    if column is None:
        raise ValueError("CSV must contain a timestamp column")
    return column


def _csv_format(path: Path) -> tuple[str, str]:
    """Detect the CSV delimiter while keeping decimal commas inside semicolon files."""
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        sample = stream.read(16384)

    try:
        delimiter = csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        header = sample.splitlines()[0] if sample.splitlines() else ""
        delimiter = max((",", ";", "\t", "|"), key=header.count)

    decimal = "," if delimiter == ";" else "."
    return delimiter, decimal


def _read_time_series(path: Path) -> pd.DataFrame:
    delimiter, decimal = _csv_format(path)
    frame = pd.read_csv(path, sep=delimiter, decimal=decimal, encoding="utf-8-sig")
    timestamp_column = _timestamp_column(frame)
    timestamps = pd.to_datetime(frame[timestamp_column], utc=True, errors="raise")
    frame["timestamp"] = timestamps
    frame.index = pd.DatetimeIndex(timestamps, name="timestamp")
    return frame


def _safe_parse_message(error: Exception) -> str:
    """Return useful parser diagnostics without including source row contents."""
    if isinstance(error, pd.errors.ParserError):
        match = re.search(r"Expected \d+ fields in line \d+, saw \d+", str(error))
        return match.group(0) if match else "Некорректная структура CSV."
    if isinstance(error, json.JSONDecodeError):
        return f"Некорректный JSON (строка {error.lineno}, столбец {error.colno})."
    if isinstance(error, pd.errors.EmptyDataError):
        return "Файл пуст или не содержит заголовка."
    if isinstance(error, UnicodeError):
        return "Не удалось прочитать файл как UTF-8."
    if isinstance(error, ValueError):
        return "Не удалось разобрать временные метки или структуру файла."
    return f"Ошибка чтения или разбора ({type(error).__name__})."


class Inputs:
    """In-memory view of one case folder; all corrections remain in memory."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        self.missing_files: list[str] = []
        self.input_errors: list[dict[str, str]] = []

        def record_error(filename: str, kind: str, message: str) -> None:
            self.input_errors.append({"file": filename, "kind": kind, "message": message})
            if kind == "missing":
                self.missing_files.append(filename)

        object_path = self.folder / "object.json"
        raw_object: dict[str, Any] = {}
        if not object_path.is_file():
            record_error("object.json", "missing", "Файл отсутствует.")
        else:
            try:
                parsed_object = json.loads(object_path.read_text(encoding="utf-8-sig"))
                if not isinstance(parsed_object, dict):
                    raise ValueError("object.json должен содержать JSON-объект")
                raw_object = parsed_object
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
                record_error("object.json", "parse", _safe_parse_message(error))

        self.passport = raw_object.get("passport", raw_object)
        if not isinstance(self.passport, dict):
            record_error("object.json", "schema", "Поле passport должно быть JSON-объектом.")
            self.passport = {}
        self.passport.setdefault("object_id", self.folder.name.casefold())

        def load_timeseries(filename: str) -> pd.DataFrame | None:
            path = self.folder / filename
            if not path.is_file():
                record_error(filename, "missing", "Файл отсутствует.")
                return None
            try:
                return _read_time_series(path)
            except (OSError, UnicodeError, ValueError, pd.errors.ParserError, pd.errors.EmptyDataError) as error:
                record_error(filename, "parse", _safe_parse_message(error))
                return None

        self.inv = load_timeseries("invoice.csv")
        self.tel = load_timeseries("telemetry.csv")
        if self.inv is not None:
            try:
                self._normalize_invoice()
            except ValueError as error:
                record_error("invoice.csv", "schema", str(error))
                self.inv = None

        dashboard = raw_object.get("dashboard", raw_object.get("dash", self.passport.get("dashboard", {})))
        if not isinstance(dashboard, dict):
            record_error("object.json", "schema", "Конфигурация dashboard должна быть JSON-объектом.")
            dashboard = {}
        self.dash = copy.deepcopy(dashboard)
        self.dash.setdefault(
            "blocks",
            [
                {"id": name, "object_id": self.passport["object_id"], "data_source": f"{self.passport['object_id']}/{name}"}
                for name in ("history", "assets", "control", "quality_status")
            ],
        )
        self.dash.setdefault("acl", {"principals": {"operator": ["read"]}, "public": False})

        self.sim = copy.deepcopy(raw_object.get("simulator", raw_object.get("sim", {"mode": "ok", "assets": []})))
        self.accept = None
        self.corrections: list[dict[str, Any]] = []

    def _normalize_invoice(self) -> None:
        aliases = {
            "ean": ("ean", "ean_code", "ean_number"),
            "value": ("value", "Grid.energy_kWh.offtake_invoice (kWh)", "energy_kWh"),
            "energy_kWh": ("energy_kWh", "Grid.energy_kWh.offtake_invoice (kWh)", "value"),
            "ca_kwh": ("ca_kwh", "ca_energy_kWh", "Grid.energy_kWh.injection_invoice (kWh)"),
            "power_kW": ("power_kW", "Grid.power_kW.invoice (kW)", "Grid.power_kW (kW)"),
        }
        object_ean = self.passport.get("ean", self.passport.get("EAN"))
        for target, candidates in aliases.items():
            if target in self.inv.columns:
                continue
            source = _first_column(self.inv, candidates)
            if source is not None:
                self.inv[target] = self.inv[source]
            elif target == "ean" and object_ean is not None:
                self.inv[target] = object_ean

        required = ("ean", "value", "energy_kWh", "ca_kwh", "power_kW")
        missing = [name for name in required if name not in self.inv.columns]
        if missing:
            raise ValueError("invoice.csv is missing required field(s): " + ", ".join(missing))

    def clone(self) -> "Inputs":
        return copy.deepcopy(self)

    def hashes(self, rules: dict[str, Any] | None = None) -> list[str]:
        if rules is None:
            rules = json.loads(RULES_PATH.read_text(encoding="utf-8"))
        rules_hash = _stable_hash(rules)
        history = _stable_hash([rules_hash, _stable_hash(self.inv)])
        telemetry = _stable_hash([rules_hash, _stable_hash(self.tel)])
        control = _stable_hash({"rules": rules, "simulator": self.sim})
        dashboard_config = {"passport": self.passport, "dashboard": self.dash}
        dashboard = _stable_hash({"rules": rules, "config": dashboard_config})
        combined = _stable_hash([history, telemetry, control, dashboard])
        # Index 0 is the aggregate; stages 1–4 use the remaining hashes.
        return [combined, history, telemetry, control, dashboard]

