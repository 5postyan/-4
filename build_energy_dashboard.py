"""Build the energy balance page from local case inputs and current check results."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pandas as pd
from launch.paths import default_object_id, object_folders

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from launch.inputs import Inputs

DATA_DIR = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path(os.environ.get("LAUNCH_DATA_DIR", ROOT / "data"))
OUT_DIR = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))
OUTPUT = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else Path(
    os.environ.get("LAUNCH_ENERGY_DASHBOARD_PATH", OUT_DIR / "energy.html")
)
RESULTS_PATH = OUT_DIR / "results.json"


def normalized(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name).casefold())


def find_column(frame: pd.DataFrame, candidates: tuple[str, ...]) -> str | None:
    wanted = {normalized(candidate) for candidate in candidates}
    for column in frame.columns:
        label = str(column).split(" (")[0]
        if normalized(column) in wanted or normalized(label) in wanted:
            return str(column)
    return None


def numeric(frame: pd.DataFrame | None, candidates: tuple[str, ...]) -> pd.Series:
    if frame is None:
        return pd.Series(dtype="float64")
    column = find_column(frame, candidates)
    if column is None:
        return pd.Series(float("nan"), index=frame.index, dtype="float64")
    return pd.to_numeric(frame[column], errors="coerce")


def cleaned(series: pd.Series) -> tuple[pd.Series, int]:
    values = pd.to_numeric(series, errors="coerce")
    valid = values.dropna()
    if valid.empty:
        return values, 0
    cap = max(float(valid.quantile(0.999)) * 5, 1.0)
    excluded = values.lt(0) | values.gt(cap)
    return values.mask(excluded), int(excluded.sum())


def total(series: pd.Series) -> float | None:
    values = pd.to_numeric(series, errors="coerce").dropna()
    return round(float(values.sum()), 1) if not values.empty else None


def source_label(source: str) -> str:
    text = source.casefold()
    if "invoice" in text or "счёт" in text:
        return "счёт"
    if "telemetry" in text or "телеметр" in text:
        return "телеметрия"
    if "object" in text or "паспорт" in text:
        return "паспорт"
    return "проверка"


def issues_for(inputs: Inputs, result: dict[str, Any] | None) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if result:
        for stage in result.get("stages", []):
            for check in stage.get("checks", []):
                if check.get("status") not in {"fail", "unavailable", "warn", "warning"}:
                    continue
                obstacles = check.get("obstacles") or []
                reasons: list[str] = []
                examples: list[str] = []
                count = None
                for obstacle in obstacles:
                    reason = obstacle.get("reason") or obstacle.get("field")
                    if reason and str(reason) not in reasons:
                        reasons.append(str(reason))
                    if isinstance(obstacle.get("count"), (int, float)):
                        count = int(obstacle["count"])
                    for key in ("timestamp", "time", "from"):
                        value = obstacle.get(key)
                        if value and str(value) not in examples:
                            examples.append(str(value))
                detail = "; ".join(reasons[:4]) or check.get("summary", "Проверка не пройдена")
                if len(reasons) > 4:
                    detail += f"; и ещё {len(reasons) - 4} причин"
                items.append({
                    "sev": "warn" if check.get("status") in {"warn", "warning"} else "error",
                    "src": source_label(str(check.get("evidence", {}).get("source", ""))),
                    "title": f"{check.get('id', '')} · {check.get('title', 'Проверка')}",
                    "detail": detail,
                    "count": count,
                    "examples": examples[:3],
                })
    if not items:
        for error in inputs.input_errors:
            items.append({
                "sev": "error",
                "src": source_label(error.get("file", "")),
                "title": f"Проблема входного файла: {error.get('file', 'данные')}",
                "detail": error.get("message", "Файл не удалось прочитать"),
                "count": None,
                "examples": [],
            })
    return items


def recent_summary(frame: pd.DataFrame | None) -> dict[str, Any] | None:
    if frame is None or frame.empty:
        return None
    names = [str(column).split(" (")[0] for column in frame.columns]
    renamed = frame.copy()
    renamed.columns = names

    def sum_matching(pattern: str) -> float | None:
        columns = [column for column in renamed.columns if re.fullmatch(pattern, column)]
        values = [cleaned(pd.to_numeric(renamed[column], errors="coerce"))[0].dropna() for column in columns]
        available = [series for series in values if not series.empty]
        return round(float(sum(float(series.sum()) for series in available)), 1) if available else None

    return {
        "start": str(frame.index.min().date()),
        "end": str(frame.index.max().date()),
        "offtake": sum_matching(r"Grid\.energy_kWh\.offtake"),
        "injection": sum_matching(r"Grid\.energy_kWh\.injection"),
        "demand": sum_matching(r"Demand\.energy_kWh"),
        "pv": sum_matching(r"PV\d*\.energy_kWh\.production"),
        "charge": sum_matching(r"Battery\d*\.energy_kWh\.charge"),
        "discharge": sum_matching(r"Battery\d*\.energy_kWh\.discharge"),
    }


def build_objects() -> list[dict[str, Any]]:
    if not DATA_DIR.is_dir():
        raise FileNotFoundError(f"Папка с данными не найдена: {DATA_DIR}")
    results = json.loads(RESULTS_PATH.read_text(encoding="utf-8")) if RESULTS_PATH.is_file() else {}
    overview = results.get("overview", {})
    objects = []
    seen_ids: set[str] = set()

    for folder in object_folders(DATA_DIR):
        inputs = Inputs(folder)
        passport = inputs.passport
        object_id = str(passport.get("object_id", default_object_id(folder)))
        if object_id in seen_ids:
            inputs.input_errors.append({
                "file": "object.json", "kind": "schema",
                "message": f"object_id {object_id} уже используется другим объектом.",
            })
            object_id = default_object_id(folder)
            if object_id in seen_ids:
                object_id = f"{object_id}_{folder.name.casefold()}"
            passport["object_id"] = object_id
        seen_ids.add(object_id)
        result = overview.get(object_id)
        inv = inputs.inv

        off_raw = numeric(inv, ("Grid.energy_kWh.offtake_invoice", "energy_kWh", "value"))
        off_valid = numeric(inv, ("Grid.energy_kWh.offtake_invoice_validated",))
        inj_raw = numeric(inv, ("Grid.energy_kWh.injection_invoice",))
        inj_valid = numeric(inv, ("Grid.energy_kWh.injection_invoice_validated",))
        if inv is not None:
            off_series, off_excluded = cleaned(off_valid.combine_first(off_raw))
            inj_series, inj_excluded = cleaned(inj_valid.combine_first(inj_raw))
            monthly = pd.DataFrame({"off": off_series, "inj": inj_series}, index=inv.index)
            monthly = monthly.groupby(inv.index.strftime("%Y-%m")).sum(min_count=1)
            months = [
                [str(month), round(float(row["off"]), 1) if pd.notna(row["off"]) else 0.0,
                 round(float(row["inj"]), 1) if pd.notna(row["inj"]) else 0.0]
                for month, row in monthly.iterrows()
            ]
            history = {
                "start": str(inv.index.min().date()),
                "end": str(inv.index.max().date()),
                "offtake": round(float(off_series.sum(skipna=True)), 1),
                "injection": round(float(inj_series.sum(skipna=True)), 1),
                "excluded": off_excluded + inj_excluded,
                "months": months,
            }
        else:
            history = {"start": "нет данных", "end": "нет данных", "offtake": 0.0, "injection": 0.0, "excluded": 0, "months": []}

        assets = passport.get("assets", [])
        if not isinstance(assets, list):
            assets = []
        types = [str(asset.get("type", "")).casefold() for asset in assets if isinstance(asset, dict)]
        statuses = [stage.get("status", "not_started") for stage in (result or {}).get("stages", [])]
        statuses = (statuses + ["not_started"] * 4)[:4]
        address = passport.get("address") if isinstance(passport.get("address"), dict) else {}

        objects.append({
            "id": object_id,
            "folder": folder.name,
            "name": str(passport.get("name", folder.name)),
            "city": address.get("city"),
            "pv": sum(1 for kind in types if "pv" in kind),
            "battery": sum(1 for kind in types if "batter" in kind),
            "hist": history,
            "recent": recent_summary(inputs.tel),
            "issues": issues_for(inputs, result),
            "stages": statuses,
        })
    return objects


def main() -> None:
    template_path = ROOT / "energy_dashboard_template.html"
    template = template_path.read_text(encoding="utf-8")
    objects = build_objects()
    payload = json.dumps(objects, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    result_stat = RESULTS_PATH.stat() if RESULTS_PATH.is_file() else None
    revision = f"{result_stat.st_mtime_ns}:{result_stat.st_size}" if result_stat else ""
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    html = template.replace("__ENERGY__", payload.replace("</", "<\\/"))
    html = html.replace("__REVISION__", json.dumps(revision))
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"Энергобаланс собран: {OUTPUT} · объектов: {len(objects)}")


if __name__ == "__main__":
    main()
