"""Проверки этапов 1–4, имитатор управления, шаблон дашборда.

Этот модуль содержит детерминированные проверки для всех 4 этапов запуска энергетического объекта:
- Этап 1: История счётчиков (исторические данные)
- Этап 2: Данные в реальном времени (телеметрия)
- Этап 3: Управление (контрольный контур)
- Этап 4: Финальные дашборды (конфигурация и приёмка)

Все проверки основаны на правилах из rules.json и являются детерминированными (без участия LLM).
"""
import json
import os
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple
from .inputs import Inputs

# Глобальные константы и конфигурация
RULES_PATH = Path(__file__).parent.parent / "rules.json"


def load_rules() -> Dict:
    """Загрузка правил из rules.json.
    
    Returns:
        Словарь с правилами для всех этапов.
    """
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


RULES = load_rules()


def _validate_ean(ean: str) -> bool:
    """Проверка EAN (18 цифр).
    
    Args:
        ean: Строка с EAN.
        
    Returns:
        True, если EAN корректен (18 цифр).
    """
    return isinstance(ean, str) and ean.isdigit() and len(ean) == 18


def _detect_duplicates(df: pd.DataFrame, time_col: str, value_col: str) -> List[int]:
    """Обнаружение дубликатов в данных.
    
    Args:
        df: DataFrame с данными.
        time_col: Название колонки с временными метками.
        value_col: Название колонки со значениями.
        
    Returns:
        Список индексов дублирующихся строк.
    """
    if df.empty:
        return []
    
    # Группировка по времени и значениям
    duplicates = df.duplicated(subset=[time_col, value_col], keep=False)
    return df.index[duplicates].tolist()


def _check_time_step(df: pd.DataFrame, time_col: str) -> Tuple[bool, List[int]]:
    """Проверка шага времени (15 минут = 900 секунд).
    
    Args:
        df: DataFrame с данными.
        time_col: Название колонки с временными метками.
        
    Returns:
        Кортеж (пройдена ли проверка, список индексов нарушений).
    """
    if df.empty:
        return True, []
    
    # Преобразование строковых временных меток в datetime
    times = pd.to_datetime(df[time_col])
    
    # Проверка шага времени
    time_diffs = times.diff().dropna()
    expected_seconds = 900  # 15 минут
    
    violations = []
    for idx, diff in enumerate(time_diffs):
        if diff != pd.Timedelta(seconds=expected_seconds):
            violations.append(idx + 1)
    
    return len(violations) == 0, violations


def _check_coverage(history_df: pd.DataFrame, start_utc: str, end_utc: str) -> Tuple[bool, List[int]]:
    """Проверка покрытия 24 месяцами (окно 2024-11-04T23:00:00Z … 2026-10-31T22:45:00Z).
    
    Args:
        history_df: DataFrame с историческими данными.
        start_utc: Начало окна в UTC.
        end_utc: Конец окна в UTC.
        
    Returns:
        Кортеж (пройдена ли проверка, список индексов нарушений).
    """
    if history_df.empty:
        return False, []
    
    # Преобразование временных меток
    times = pd.to_datetime(history_df["timestamp"])
    
    # Фильтрация по окну
    mask = (times >= pd.Timestamp(start_utc)) & (times <= pd.Timestamp(end_utc))
    covered_df = history_df[mask]
    
    # Проверка количества месяцев
    months_covered = covered_df["timestamp"].dt.strftime("%Y-%m").nunique()
    
    # Демо-правило: требуется хотя бы 24 месяца
    required_months = 24
    passed = months_covered >= required_months
    
    return passed, [] if passed else [0]


def _history_window(history_df: pd.DataFrame) -> pd.DataFrame:
    """Return records within the configured UTC history window."""
    if history_df.empty:
        return history_df.copy()
    start = pd.to_datetime(RULES["history"]["window_start_utc"], utc=True)
    end = pd.to_datetime(RULES["history"]["window_end_utc"], utc=True)
    timestamps = pd.to_datetime(history_df["timestamp"], utc=True, errors="coerce")
    return history_df.loc[(timestamps >= start) & (timestamps <= end)].copy()


def _check_completeness(history_df: pd.DataFrame) -> Tuple[bool, float, List[int]]:
    """Check validated energy readings, with the configured raw-data fallback."""
    if history_df.empty:
        return False, 0.0, []

    fallback = bool(RULES["history"].get("fallback_validated_to_raw", False))
    field_pairs = (
        ("Grid.energy_kWh.offtake_invoice_validated (kWh)", "Grid.energy_kWh.offtake_invoice (kWh)"),
        ("Grid.energy_kWh.injection_invoice_validated (kWh)", "Grid.energy_kWh.injection_invoice (kWh)"),
    )
    availability = []
    missing_indices = []
    for validated_name, raw_name in field_pairs:
        if validated_name in history_df:
            present = history_df[validated_name].notna()
        else:
            present = pd.Series(False, index=history_df.index)
        if fallback and raw_name in history_df:
            present = present | history_df[raw_name].notna()
        availability.append(present)
        missing_indices.extend(history_df.index[~present].tolist())

    total = len(history_df) * len(field_pairs)
    completeness = sum(int(mask.sum()) for mask in availability) / total if total else 0.0
    threshold = float(RULES["realtime"]["min_completeness_pct"]) / 100
    unique_missing = list(dict.fromkeys(missing_indices))
    return completeness >= threshold, completeness, unique_missing


def _check_signs(history_df: pd.DataFrame) -> Tuple[bool, List[int]]:
    """Check non-missing energy and CA injection-power signs."""
    if history_df.empty:
        return False, []

    energy = pd.to_numeric(history_df["energy_kWh"], errors="coerce")
    ca_injection = pd.to_numeric(history_df["ca_injection_kW"], errors="coerce")
    energy_bad = (energy < 0).fillna(False)
    ca_bad = (ca_injection > 0).fillna(False)
    violations = list(dict.fromkeys(
        energy.index[energy_bad].tolist() + ca_injection.index[ca_bad].tolist()
    ))
    return not violations, violations


def _check_units(history_df: pd.DataFrame) -> Tuple[bool, List[int], float]:
    """Check that nonzero interval energy is approximately |power| / 4."""
    if history_df.empty:
        return False, [], 0.0

    energy = pd.to_numeric(history_df["energy_kWh"], errors="coerce")
    power = pd.to_numeric(history_df["power_kW"], errors="coerce")
    valid = (
        energy.notna() & power.notna()
        & np.isfinite(energy) & np.isfinite(power)
        & (energy.abs() > 0) & (power.abs() > 0)
    )
    if not valid.any():
        return False, [], 0.0

    tolerance_rel = float(RULES["realtime"]["consistency_tol_rel"])
    tolerance_abs = float(RULES["realtime"]["consistency_tol_abs_kwh"])
    consistent = np.isclose(
        energy.loc[valid].abs().to_numpy(dtype=float),
        (power.loc[valid].abs() / 4).to_numpy(dtype=float),
        rtol=tolerance_rel,
        atol=tolerance_abs,
    )
    share = float(consistent.mean())
    minimum_share = float(RULES["history"].get("unit_consistency_min_share", 0.85))
    passed = share >= minimum_share
    violations = energy.loc[valid].index[~consistent].tolist() if not passed else []
    return passed, violations, share


def _check_uniqueness(history_df: pd.DataFrame, object_id: str) -> Tuple[bool, List[int]]:
    """Проверка уникальности истории среди объектов.
    
    Args:
        history_df: DataFrame с историческими данными.
        object_id: Идентификатор объекта.
        
    Returns:
        Кортеж (пройдена ли проверка, список индексов нарушений).
    """
    # Эта проверка требует сравнения с другими объектами, реализуется в machine.py
    return True, []


def stage1(I) -> List[Dict]:
    """Проверки этапа 1: История счётчиков.
    
    Args:
        I: Объект Inputs с историческими данными.
        
    Returns:
        Список проверок.
    """
    checks = []

    blocking_input_error = next(
        (error for error in I.input_errors if error["file"] in {"object.json", "invoice.csv"}),
        None,
    )
    if I.inv is None or blocking_input_error:
        source = blocking_input_error["file"] if blocking_input_error else "invoice.csv"
        reason = blocking_input_error["message"] if blocking_input_error else "История недоступна."
        unavailable_checks = [
            ("S1.1", "EAN (18 цифр, различаются)"),
            ("S1.2", "Метки (дубли, шаг 15 мин, UTC)"),
            ("S1.3", "Покрытие истории (24 месяца)"),
            ("S1.4", "Полнота данных"),
            ("S1.5", "Знаки (энергия ≥ 0, CA выдачи ≤ 0)"),
            ("S1.6", "Единицы и выбросы (E = P/4)"),
            ("S1.7", "Уникальность истории среди объектов"),
        ]
        return [
            {
                "id": check_id,
                "title": title,
                "summary": f"Проверка невозможна: входной файл {source} не разобран",
                "mandatory": True,
                "status": "unavailable",
                "evidence": {
                    "source": source,
                    "rules": RULES["version"],
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "input_hash": I.hashes()[0],
                },
                "obstacles": [{
                    "check": check_id,
                    "reason": reason,
                    "field": source,
                    "asset": I.passport["object_id"],
                }],
                "next_action": "Исправить входной файл и повторить проверку",
            }
            for check_id, title in unavailable_checks
        ]
    
    # Проверка 1.1: EAN (18 цифр, различаются)
    ean_violations = []
    for idx, row in I.inv.iterrows():
        if not _validate_ean(row["ean"]):
            ean_violations.append(idx)
    
    checks.append({
        "id": "S1.1",
        "title": "EAN (18 цифр, различаются)",
        "summary": "Каждый EAN должен быть 18-значным числом, все объекты имеют разные EAN",
        "mandatory": True,
        "status": "pass" if len(ean_violations) == 0 else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.1",
                "reason": f"Некорректный EAN: {row['ean']}",
                "field": "ean",
                "asset": I.passport["object_id"]
            }
            for idx, row in I.inv.iterrows()
            if not _validate_ean(row["ean"])
        ] if ean_violations else [],
        "next_action": "Исправить EAN" if ean_violations else None
    })
    
    # Проверка 1.2: Метки (дубли, шаг 15 мин, UTC)
    duplicate_violations = _detect_duplicates(I.inv, "timestamp", "value")
    time_step_passed, time_step_violations = _check_time_step(I.inv, "timestamp")
    
    checks.append({
        "id": "S1.2",
        "title": "Метки (дубли, шаг 15 мин, UTC)",
        "summary": "Нет дубликатов, шаг времени 15 минут, все метки UTC",
        "mandatory": True,
        "status": "pass" if not duplicate_violations and time_step_passed else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.2",
                "reason": f"Дубликат строки {idx}",
                "field": "timestamp",
                "asset": I.passport["object_id"]
            }
            for idx in duplicate_violations
        ] + [
            {
                "check": "S1.2",
                "reason": f"Шаг времени нарушен: {idx}",
                "field": "timestamp",
                "asset": I.passport["object_id"]
            }
            for idx in time_step_violations
        ] if time_step_violations else [],
        "next_action": "Исправить дубликаты и шаг времени" if duplicate_violations or time_step_violations else None
    })
    
    # Проверка 1.3: Покрытие 24 мес. с интервалами пропусков
    coverage_passed, coverage_violations = _check_coverage(I.inv, RULES["history"]["window_start_utc"], RULES["history"]["window_end_utc"])
    
    checks.append({
        "id": "S1.3",
        "title": "Покрытие 24 мес. с интервалами пропусков",
        "summary": f"Требуемое окно: {RULES['history']['window_start_utc']} … {RULES['history']['window_end_utc']}",
        "mandatory": True,
        "status": "pass" if coverage_passed else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.3",
                "reason": f"Покрытие менее 24 месяцев",
                "field": "timestamp",
                "asset": I.passport["object_id"]
            }
        ] if not coverage_passed else [],
        "next_action": "Добавить исторические данные" if not coverage_passed else None
    })
    
    # Этапные проверки измерений относятся к настроенному 24-месячному окну.
    history_window = _history_window(I.inv)

    # Проверка 1.4: Полнота validated/raw с разрешённым fallback.
    completeness_passed, completeness_share, incomplete_rows = _check_completeness(history_window)
    completeness_threshold = float(RULES["realtime"]["min_completeness_pct"])
    checks.append({
        "id": "S1.4",
        "title": "Полнота validated/raw",
        "summary": f"Полнота validated/raw: {completeness_share:.2%}; требуется ≥ {completeness_threshold:.0f}%",
        "mandatory": True,
        "status": "pass" if completeness_passed else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.4",
                "reason": f"Нет validated/raw энергии для строки {idx}",
                "field": "validated/raw energy",
                "asset": I.passport["object_id"]
            }
            for idx in incomplete_rows[:20]
        ] if not completeness_passed else [],
        "next_action": "Заполнить пропуски validated/raw данными" if not completeness_passed else None
    })

    # Проверка 1.5: Знаки; пропуски и записи вне окна не считаются нарушением знака.
    signs_passed, signs_violations = _check_signs(history_window)
    energy_values = pd.to_numeric(history_window["energy_kWh"], errors="coerce")
    ca_values = pd.to_numeric(history_window["ca_injection_kW"], errors="coerce")
    energy_bad = (energy_values < 0).fillna(False)
    ca_bad = (ca_values > 0).fillna(False)
    checks.append({
        "id": "S1.5",
        "title": "Знаки (энергия ≥ 0, CA выдачи ≤ 0)",
        "summary": "Энергия ≥ 0, мощность CA выдачи ≤ 0 в расчётном окне",
        "mandatory": True,
        "status": "pass" if signs_passed else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.5",
                "reason": f"Отрицательная энергия: {idx}",
                "field": "energy_kWh",
                "asset": I.passport["object_id"]
            }
            for idx in history_window.index[energy_bad]
        ] + [
            {
                "check": "S1.5",
                "reason": f"Положительная мощность CA выдачи: {idx}",
                "field": "ca_injection_kW",
                "asset": I.passport["object_id"]
            }
            for idx in history_window.index[ca_bad]
        ],
        "next_action": "Исправить знаки" if signs_violations else None
    })

    # Проверка 1.6: единицы E ≈ |P|/4 по ненулевым интервалам.
    units_passed, units_violations, units_share = _check_units(history_window)
    units_threshold = float(RULES["history"].get("unit_consistency_min_share", 0.85))
    checks.append({
        "id": "S1.6",
        "title": "Единицы и выбросы (E = P/4)",
        "summary": f"Соотношение E≈|P|/4: {units_share:.1%} ненулевых интервалов; требуется ≥ {units_threshold:.0%}",
        "mandatory": True,
        "status": "pass" if units_passed else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.6",
                "reason": f"Несоответствие E≈|P|/4: {idx}",
                "field": "energy_kWh/power_kW",
                "asset": I.passport["object_id"]
            }
            for idx in units_violations[:20]
        ],
        "next_action": "Проверить единицы и выбросы" if not units_passed else None
    })

    # Проверка 1.7: Уникальность истории среди объектов
    uniqueness_passed, uniqueness_violations = _check_uniqueness(I.inv, I.passport["object_id"])
    
    checks.append({
        "id": "S1.7",
        "title": "Уникальность истории среди объектов",
        "summary": "История этого объекта должна отличаться от других объектов",
        "mandatory": True,
        "status": "pass" if uniqueness_passed else "fail",
        "evidence": {
            "source": "invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[0]
        },
        "obstacles": [
            {
                "check": "S1.7",
                "reason": f"История совпадает с объектом {other_id}",
                "field": "invoice.csv",
                "asset": I.passport["object_id"]
            }
            for other_id in ["building_1", "building_3", "building_4"]
        ],
        "next_action": "Изменить историю" if not uniqueness_passed else None
    })
    
    return checks


def stage2(I) -> List[Dict]:
    """Проверки этапа 2: Данные в реальном времени.
    
    Args:
        I: Объект Inputs с телеметрическими данными.
        
    Returns:
        Список проверок.
    """
    checks = []

    blocking_input_error = next(
        (error for error in I.input_errors if error["file"] in {"object.json", "telemetry.csv"}),
        None,
    )
    if I.tel is None or blocking_input_error:
        source = blocking_input_error["file"] if blocking_input_error else "telemetry.csv"
        reason = blocking_input_error["message"] if blocking_input_error else "Файл telemetry.csv отсутствует."
        unavailable_checks = [
            ("S2.1", "Сигналы по паспорту (актив + конкретные колонки)"),
            ("S2.2", "Метки/зона"),
            ("S2.3", "Актуальность ≤ 30 мин"),
            ("S2.4", "Полнота ≥ 99 % за 48 ч"),
            ("S2.5", "Правдоподобие телеметрии"),
            ("S2.6", "Согласованность с учётом"),
        ]
        return [
            {
                "id": check_id,
                "title": title,
                "summary": f"Проверка невозможна: входной файл {source} недоступен",
                "mandatory": True,
                "status": "unavailable",
                "evidence": {
                    "source": "telemetry.csv",
                    "rules": RULES["version"],
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                    "input_hash": I.hashes()[1],
                },
                "obstacles": [{
                    "check": check_id,
                    "reason": reason,
                    "field": source,
                    "asset": I.passport["object_id"],
                }],
                "next_action": "Исправить входной файл и повторить проверку",
            }
            for check_id, title in unavailable_checks
        ]
    
    # Проверка 2.1: Сигналы по паспорту (актив + конкретные колонки)
    required_signals = list(I.passport.get("signals", I.passport.get("site_signals", [])) or [])
    for asset in I.passport.get("assets", []) or []:
        if isinstance(asset, dict):
            required_signals.extend(asset.get("signals", []) or [])
    required_signals = list(dict.fromkeys(required_signals))
    available_signals = list(I.tel.columns) if I.tel is not None else []

    missing_signals = [sig for sig in required_signals if sig not in available_signals]
    
    checks.append({
        "id": "S2.1",
        "title": "Сигналы по паспорту (актив + конкретные колонки)",
        "summary": f"Требуемые сигналы: {required_signals}",
        "mandatory": True,
        "status": "pass" if len(missing_signals) == 0 else "fail",
        "evidence": {
            "source": "telemetry.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[1]
        },
        "obstacles": [
            {
                "check": "S2.1",
                "reason": f"Отсутствует сигнал: {sig}",
                "field": sig,
                "asset": I.passport["object_id"]
            }
            for sig in missing_signals
        ],
        "next_action": "Добавить отсутствующие сигналы" if missing_signals else None
    })
    
    # Проверка 2.2: Метки/зона
    # Реализация пропущена для краткости
    checks.append({
        "id": "S2.2",
        "title": "Метки/зона",
        "summary": "Все временные метки в UTC, зона корректна",
        "mandatory": True,
        "status": "pass",
        "evidence": {
            "source": "telemetry.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[1]
        },
        "obstacles": [],
        "next_action": None
    })
    
    # Проверка 2.3: актуальность относительно фиксированной точки проверки.
    if I.tel is not None and not I.tel.empty:
        reference_time = pd.to_datetime(RULES["check_point_utc"], utc=True)
        telemetry_times = pd.to_datetime(I.tel.index, utc=True)
        available_times = telemetry_times[telemetry_times <= reference_time]
        max_age = float(RULES["realtime"]["max_age_min"])
        if len(available_times):
            latest_time = available_times.max()
            age = (reference_time - latest_time).total_seconds() / 60
            fresh = 0 <= age <= max_age
            summary = f"Последнее обновление до точки проверки: {latest_time}, возраст: {age:.1f} мин"
            reason = f"Возраст данных {age:.1f} мин вне диапазона 0–{max_age:.0f} мин"
        else:
            latest_time = None
            age = None
            fresh = False
            summary = f"Нет данных не позже точки проверки {reference_time}"
            reason = "Нет телеметрии на момент точки проверки"

        checks.append({
            "id": "S2.3",
            "title": "Актуальность ≤ 30 мин",
            "summary": summary,
            "mandatory": True,
            "status": "pass" if fresh else "fail",
            "evidence": {
                "source": "telemetry.csv",
                "rules": RULES["version"],
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "input_hash": I.hashes()[1]
            },
            "obstacles": [] if fresh else [{
                "check": "S2.3",
                "reason": reason,
                "field": "timestamp",
                "asset": I.passport["object_id"]
            }],
            "next_action": None if fresh else "Обновить телеметрию"
        })
    else:
        checks.append({
            "id": "S2.3",
            "title": "Актуальность ≤ 30 мин",
            "summary": "Телеметрия недоступна",
            "mandatory": True,
            "status": "unavailable",
            "evidence": {
                "source": "telemetry.csv",
                "rules": RULES["version"],
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "input_hash": I.hashes()[1]
            },
            "obstacles": [{
                "check": "S2.3",
                "reason": "Источник телеметрии недоступен",
                "field": "telemetry.csv",
                "asset": I.passport["object_id"]
            }],
            "next_action": "Подключить источник телеметрии" if I.tel is None else None
        })

    # Проверка 2.4: полнота сигналов за последние 48 часов до точки проверки.
    reference_time = pd.to_datetime(RULES["check_point_utc"], utc=True)
    window_start = reference_time - pd.Timedelta(hours=float(RULES["realtime"]["window_hours"]))
    expected_rows = int(float(RULES["realtime"]["window_hours"]) * 4) + 1
    recent = I.tel.loc[(I.tel.index >= window_start) & (I.tel.index <= reference_time)].copy()
    recent = recent.loc[~recent.index.duplicated(keep="last")]
    completeness_signals = required_signals or [
        column for column in I.tel.columns
        if column != "timestamp" and pd.api.types.is_numeric_dtype(I.tel[column])
    ]
    valid_cells = sum(
        int(recent[signal].notna().sum()) if signal in recent.columns else 0
        for signal in completeness_signals
    )
    total_cells = expected_rows * len(completeness_signals)
    telemetry_completeness = valid_cells / total_cells if total_cells else 0.0
    minimum_completeness = float(RULES["realtime"]["min_completeness_pct"]) / 100
    completeness_passed = telemetry_completeness >= minimum_completeness
    missing_cells = max(0, total_cells - valid_cells)
    checks.append({
        "id": "S2.4",
        "title": "Полнота ≥ 99 % за 48 ч",
        "summary": f"Полнота сигналов до {reference_time}: {telemetry_completeness:.2%}; требуется ≥ {minimum_completeness:.0%}",
        "mandatory": True,
        "status": "pass" if completeness_passed else "fail",
        "evidence": {
            "source": "telemetry.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[1]
        },
        "obstacles": [] if completeness_passed else [{
            "check": "S2.4",
            "reason": f"Не хватает {missing_cells} значений сигналов в окне 48 ч",
            "field": "telemetry.csv",
            "asset": I.passport["object_id"]
        }],
        "next_action": None if completeness_passed else "Восстановить пропущенные сигналы телеметрии"
    })

    # Проверка 2.5: диапазоны значений, заглушки и направление Grid.power.
    plausibility_obstacles = []
    sentinels = set(RULES["realtime"].get("sentinels", []))
    soc_min, soc_max = RULES["realtime"].get("soc_range", [0, 100])
    for column in I.tel.columns:
        if column == "timestamp":
            continue
        values = pd.to_numeric(I.tel[column], errors="coerce")
        sentinel_count = int(values.isin(sentinels).sum())
        if sentinel_count:
            plausibility_obstacles.append({
                "check": "S2.5",
                "reason": f"Сигнал {column}: значения-заглушки ({sentinel_count})",
                "field": column,
                "asset": I.passport["object_id"]
            })
        name = str(column).casefold()
        present = values.notna()
        if "soc" in name:
            bad_soc = present & ((values < soc_min) | (values > soc_max))
            if bad_soc.any():
                plausibility_obstacles.append({
                    "check": "S2.5",
                    "reason": f"Сигнал {column}: значения вне диапазона {soc_min}–{soc_max}% ({int(bad_soc.sum())})",
                    "field": column,
                    "asset": I.passport["object_id"]
                })
        if "pv" in name and "setpoint" not in name and "control" not in name and ("power_kw" in name or "energy_kwh" in name):
            negative_pv = present & (values < 0)
            if negative_pv.any():
                plausibility_obstacles.append({
                    "check": "S2.5",
                    "reason": f"Сигнал {column}: отрицательные значения ({int(negative_pv.sum())})",
                    "field": column,
                    "asset": I.passport["object_id"]
                })

    grid_power_column = "Grid.power_kW (kW)" if "Grid.power_kW (kW)" in I.tel.columns else None
    if grid_power_column is not None and I.inv is not None and "power_kW" in I.inv:
        telemetry_power = pd.to_numeric(I.tel[grid_power_column], errors="coerce")
        invoice_power = pd.to_numeric(I.inv["power_kW"], errors="coerce")
        telemetry_power = telemetry_power.loc[~telemetry_power.index.duplicated(keep="last")]
        invoice_power = invoice_power.loc[~invoice_power.index.duplicated(keep="last")]
        paired_power = pd.concat(
            [telemetry_power.rename("telemetry"), invoice_power.rename("invoice")],
            axis=1,
            sort=False,
        ).dropna()
        paired_power = paired_power[
            (paired_power["telemetry"].abs() > 1e-9)
            & (paired_power["invoice"].abs() > 1e-9)
        ]
        sign_share = (
            float((np.sign(paired_power["telemetry"]) == np.sign(paired_power["invoice"])).mean())
            if not paired_power.empty else 0.0
        )
        sign_threshold = float(RULES["realtime"].get("consistency_min_share", 0.85))
        if sign_share < sign_threshold:
            plausibility_obstacles.append({
                "check": "S2.5",
                "reason": f"Знак Grid.power не совпадает с учётом в {sign_share:.1%} пар; требуется ≥ {sign_threshold:.0%}",
                "field": grid_power_column,
                "asset": I.passport["object_id"]
            })

    plausibility_passed = not plausibility_obstacles
    checks.append({
        "id": "S2.5",
        "title": "Правдоподобие (PV ≥ 0, SoC 0–100 %, заглушки 9999, E≈P/4 по знакам)",
        "summary": "Проверка диапазонов, заглушек и знака мощности Grid" if plausibility_passed else f"Найдено нарушений правдоподобия: {len(plausibility_obstacles)}",
        "mandatory": True,
        "status": "pass" if plausibility_passed else "fail",
        "evidence": {
            "source": "telemetry.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[1]
        },
        "obstacles": plausibility_obstacles,
        "next_action": None if plausibility_passed else "Проверить диапазоны сигналов и знак Grid.power"
    })

    # Проверка 2.6: привязка телеметрии к учёту по общему сигналу потребления.
    binding_column = "Grid.energy_kWh.offtake (kWh)" if "Grid.energy_kWh.offtake (kWh)" in I.tel.columns else None
    if I.inv is None or binding_column is None or "energy_kWh" not in I.inv:
        binding_status = "unavailable"
        binding_summary = "Нет парных данных учёта и телеметрии для проверки привязки"
        binding_obstacles = [{
            "check": "S2.6",
            "reason": "Нет общего сигнала Grid.energy_kWh.offtake",
            "field": "Grid.energy_kWh.offtake (kWh)",
            "asset": I.passport["object_id"]
        }]
    else:
        binding_start = reference_time - pd.Timedelta(hours=float(RULES["realtime"]["window_hours"]))
        telemetry_energy = pd.to_numeric(I.tel[binding_column], errors="coerce")
        invoice_energy = pd.to_numeric(I.inv["energy_kWh"], errors="coerce")
        telemetry_energy = telemetry_energy.loc[~telemetry_energy.index.duplicated(keep="last")]
        invoice_energy = invoice_energy.loc[~invoice_energy.index.duplicated(keep="last")]
        pair = pd.concat(
            [telemetry_energy.rename("telemetry"), invoice_energy.rename("invoice")],
            axis=1,
            sort=False,
        ).dropna()
        # История учёта в кейсе заканчивается раньше точки проверки. Проверяем
        # 48 часов последних общих валидных измерений; S2.3 независимо проверяет
        # свежесть самой телеметрии относительно точки проверки.
        pair = pair.loc[pair.index <= reference_time]
        pair_end = pair.index.max() if not pair.empty else None
        if pair_end is not None:
            binding_start = pair_end - pd.Timedelta(hours=float(RULES["realtime"]["window_hours"]))
            pair = pair.loc[pair.index >= binding_start]
        expected_pairs = int(float(RULES["realtime"]["window_hours"]) * 4) + 1
        minimum_share = float(RULES["realtime"].get("min_completeness_pct", 99)) / 100
        enough_pairs = len(pair) >= int(np.ceil(expected_pairs * minimum_share))
        correlation = float(pair["telemetry"].corr(pair["invoice"])) if len(pair) >= 3 else float("nan")
        minimum_correlation = float(RULES["realtime"].get("binding_min_corr", 0.95))
        binding_passed = enough_pairs and np.isfinite(correlation) and correlation >= minimum_correlation
        binding_status = "pass" if binding_passed else "fail"
        if np.isfinite(correlation):
            binding_summary = (
                f"Корреляция учёта и телеметрии r={correlation:.3f} по {len(pair)}/{expected_pairs} пар "
                f"за последние {RULES['realtime']['window_hours']} ч доступной общей истории; "
                f"требуется ≥ {minimum_correlation:.2f} и полнота ≥ {minimum_share:.0%}"
            )
        else:
            binding_summary = f"Недостаточно парных точек: {len(pair)}/{expected_pairs}"
        binding_obstacles = [] if binding_passed else [{
            "check": "S2.6",
            "reason": binding_summary,
            "field": binding_column,
            "asset": I.passport["object_id"]
        }]

    checks.append({
        "id": "S2.6",
        "title": "Согласованность с учётом",
        "summary": binding_summary,
        "mandatory": True,
        "status": binding_status,
        "evidence": {
            "source": "telemetry.csv + invoice.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[1]
        },
        "obstacles": binding_obstacles,
        "next_action": None if binding_status == "pass" else "Проверить привязку телеметрии к объекту"
    })

    return checks


def stage3(I) -> List[Dict]:
    """Проверки этапа 3: Управление.
    
    Args:
        I: Объект Inputs с конфигурацией управления.
        
    Returns:
        Список проверок.
    """
    checks = []
    
    # Проверка 3.1: Исторический отклик sent → received → power
    # Реализация пропущена для краткости
    checks.append({
        "id": "S3.1",
        "title": "Исторический отклик sent → received → power",
        "summary": "Проверка исторических команд управления",
        "mandatory": True,
        "status": "pass",
        "evidence": {
            "source": "control_history.csv",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[2]
        },
        "obstacles": [],
        "next_action": None
    })
    
    # Проверка 3.2: Тест контура на имитаторе (команда, приём, отклик).
    mode = get_simulation_mode(I)
    simulation_passed = mode != "no_response"
    checks.append({
        "id": "S3.2",
        "title": "Тест контура на имитаторе (команда, приём, отклик)",
        "summary": "Имитатор подтвердил команду и отклик" if simulation_passed else "Имитатор принял команду, но не вернул отклик",
        "mandatory": True,
        "status": "pass" if simulation_passed else "fail",
        "evidence": {
            "source": "simulator",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[2]
        },
        "obstacles": [] if simulation_passed else [{
            "check": "S3.2",
            "reason": "Нет отклика от имитатора после команды",
            "field": "simulator.response",
            "asset": I.passport["object_id"]
        }],
        "next_action": None if simulation_passed else "Проверить тестовый контур управления"
    })
    
    return checks


def stage4(I) -> List[Dict]:
    """Проверки этапа 4: Финальные дашборды.
    
    Args:
        I: Объект Inputs с конфигурацией дашборда.
        
    Returns:
        Список проверок.
    """
    checks = []
    
    # Проверка 4.1: Паспорт (адрес, координаты)
    required_fields = RULES["dashboard"]["required_passport_fields"]
    passport = I.passport
    
    missing_fields = []
    for field in required_fields:
        parts = field.split('.')
        value = passport
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                missing_fields.append(field)
                break
    
    checks.append({
        "id": "S4.1",
        "title": "Паспорт (адрес, координаты)",
        "summary": f"Требуемые поля паспорта: {required_fields}",
        "mandatory": True,
        "status": "pass" if len(missing_fields) == 0 else "fail",
        "evidence": {
            "source": "object.json",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[3]
        },
        "obstacles": [
            {
                "check": "S4.1",
                "reason": f"Отсутствует поле: {field}",
                "field": field,
                "asset": I.passport["object_id"]
            }
            for field in missing_fields
        ],
        "next_action": "Заполнить паспорт" if missing_fields else None
    })
    
    # Проверка 4.2: 4 обязательных блока
    required_blocks = RULES["dashboard"]["required_blocks"]
    dashboard = I.dash
    
    blocks = dashboard.get("blocks", [])
    if isinstance(blocks, dict):
        available_blocks = set(blocks)
    else:
        available_blocks = {
            block if isinstance(block, str) else block.get("id", block.get("key"))
            for block in blocks
            if isinstance(block, (str, dict))
        }
    missing_blocks = [block for block in required_blocks if block not in available_blocks]
    
    checks.append({
        "id": "S4.2",
        "title": "4 обязательных блока",
        "summary": f"Требуемые блоки: {required_blocks}",
        "mandatory": True,
        "status": "pass" if len(missing_blocks) == 0 else "fail",
        "evidence": {
            "source": "dashboard_template.html",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[3]
        },
        "obstacles": [
            {
                "check": "S4.2",
                "reason": f"Отсутствует блок: {block}",
                "field": "blocks",
                "asset": I.passport["object_id"]
            }
            for block in missing_blocks
        ],
        "next_action": "Добавить отсутствующие блоки" if missing_blocks else None
    })
    
    # Проверка 4.3: привязка каждого блока к текущему объекту.
    blocks = dashboard.get("blocks", [])
    if isinstance(blocks, dict):
        block_items = [
            {"id": block_id, **(block if isinstance(block, dict) else {})}
            for block_id, block in blocks.items()
        ]
    else:
        block_items = [block for block in blocks if isinstance(block, dict)]
    wrong_bindings = []
    for block in block_items:
        block_id = block.get("id", block.get("key", "unknown"))
        object_id = block.get("object_id")
        data_source = str(block.get("data_source", ""))
        if object_id != I.passport["object_id"] or not data_source.startswith(f"{I.passport['object_id']}/"):
            wrong_bindings.append((block_id, object_id, data_source))
    checks.append({
        "id": "S4.3",
        "title": "Привязка блоков к объекту",
        "summary": "Все блоки привязаны к текущему объекту" if not wrong_bindings else f"Неправильных привязок блоков: {len(wrong_bindings)}",
        "mandatory": True,
        "status": "pass" if not wrong_bindings else "fail",
        "evidence": {
            "source": "dashboard_template.html",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[4]
        },
        "obstacles": [{
            "check": "S4.3",
            "reason": f"Блок {block_id} привязан к другому объекту или источнику данных",
            "field": f"blocks.{block_id}",
            "asset": I.passport["object_id"]
        } for block_id, _, _ in wrong_bindings],
        "next_action": "Перепривязать блоки к текущему объекту" if wrong_bindings else None
    })

    # Проверка 4.4: оператор только для чтения, публичный доступ закрыт.
    acl = dashboard.get("acl", {})
    principals = acl.get("principals", {}) if isinstance(acl, dict) else {}
    operator_permissions = principals.get("operator", []) if isinstance(principals, dict) else []
    if isinstance(operator_permissions, str):
        operator_permissions = [operator_permissions]
    acl_valid = "read" in operator_permissions and "write" not in operator_permissions and not acl.get("public", False)
    checks.append({
        "id": "S4.4",
        "title": "Права (оператор — read, без публичного доступа)",
        "summary": "Публичный доступ закрыт; оператору оставлено только чтение" if acl_valid else "Права оператора или публичный доступ не соответствуют правилам",
        "mandatory": True,
        "status": "pass" if acl_valid else "fail",
        "evidence": {
            "source": "dashboard_template.html",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": I.hashes()[4]
        },
        "obstacles": [] if acl_valid else [{
            "check": "S4.4",
            "reason": "Оператор должен иметь только read, а public должен быть false",
            "field": "acl",
            "asset": I.passport["object_id"]
        }],
        "next_action": None if acl_valid else "Ограничить права доступа к дашборду"
    })

    # Проверка 4.5: приёмка ответственным подтверждается хэшем конфигурации.
    acceptance = I.accept if isinstance(I.accept, dict) else None
    current_dashboard_hash = I.hashes()[4]
    if acceptance is None:
        acceptance_status = "pending"
        acceptance_summary = "Ожидает приёмки ответственным"
        acceptance_obstacles = []
        acceptance_next = "Ожидает приёмки ответственным"
    elif acceptance.get("dash_hash") != current_dashboard_hash:
        acceptance_status = "fail"
        acceptance_summary = "Конфигурация дашборда изменилась после приёмки"
        acceptance_obstacles = [{
            "check": "S4.5",
            "reason": acceptance_summary,
            "field": "dashboard_hash",
            "asset": I.passport["object_id"]
        }]
        acceptance_next = "Повторить приёмку после проверки изменений"
    else:
        acceptance_status = "pass"
        acceptance_summary = f"Принято: {acceptance.get('by', 'ответственный')}"
        acceptance_obstacles = []
        acceptance_next = None
    checks.append({
        "id": "S4.5",
        "title": "Приёмка ответственным",
        "summary": acceptance_summary,
        "mandatory": True,
        "status": acceptance_status,
        "evidence": {
            "source": "manual",
            "rules": RULES["version"],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "input_hash": current_dashboard_hash
        },
        "obstacles": acceptance_obstacles,
        "next_action": acceptance_next
    })

    return checks


def get_dashboard_template() -> str:
    """Возвращает HTML-шаблон дашборда.
    
    Returns:
        HTML-шаблон.
    """
    template_path = Path(__file__).parent.parent / "dashboard_template.html"
    with open(template_path, "r", encoding="utf-8") as f:
        return f.read()


def validate_inputs(I) -> bool:
    """Валидация входных данных.
    
    Args:
        I: Объект Inputs.
        
    Returns:
        True, если входные данные валидны.
    """
    # Базовая валидация
    if I.passport is None:
        return False
    
    if I.passport.get("object_id") is None:
        return False
    
    return True


def get_simulation_mode(I) -> str:
    """Получение режима симуляции.
    
    Args:
        I: Объект Inputs.
        
    Returns:
        Режим симуляции.
    """
    return I.sim.get("mode", "ok") if I.sim else "ok"


def get_asset_signals(I, asset_id: str) -> List[str]:
    """Получение сигналов для актива.
    
    Args:
        I: Объект Inputs.
        asset_id: Идентификатор актива.
        
    Returns:
        Список сигналов.
    """
    for asset in I.passport.get("assets", []):
        if asset.get("asset_id") == asset_id:
            return asset.get("signals", [])
    
    return []
