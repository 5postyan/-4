"""Safe uploads for adding or replacing one object's input file without editing originals."""
from __future__ import annotations

import json
import io
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd

from .inputs import Inputs, _csv_format, _timestamp_column
from .paths import default_object_id, object_folders

ROOT = Path(__file__).resolve().parent.parent
UPLOAD_LOCK = threading.Lock()
ALLOWED_FILES = {"object": "object.json", "invoice": "invoice.csv", "telemetry": "telemetry.csv"}
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_BUNDLE_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_BUNDLE_FILES = 12


def _within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _owned_remove(path: Path, runtime: Path, prefix: str) -> None:
    resolved = path.resolve()
    if resolved.parent != runtime.resolve() or not resolved.name.startswith(prefix):
        raise ValueError("Отказано в удалении временной папки вне каталога приложения.")
    if resolved.exists():
        shutil.rmtree(resolved)


def prepare_data_workspace(base_dir: Path, upload_dir: Path, runtime_dir: Path, out_dir: Path) -> Path:
    """Copy base inputs into a private working tree and overlay saved user uploads."""
    base = base_dir.expanduser().resolve()
    uploads = upload_dir.expanduser().resolve()
    runtime = runtime_dir.expanduser().resolve()
    output = out_dir.expanduser().resolve()
    if not base.is_dir():
        raise FileNotFoundError(f"Папка исходных данных не найдена: {base}")
    working = runtime / "active"
    if working == base or _within(working, base):
        raise ValueError("Рабочая папка данных не может находиться внутри исходной папки.")
    if _within(working, output):
        raise ValueError("Рабочая папка данных не может находиться в публичной папке out.")
    if _within(uploads, base) or uploads == base:
        raise ValueError("Папка добавленных файлов должна быть отдельно от исходных данных.")

    runtime.mkdir(parents=True, exist_ok=True)
    staging = runtime / f"active-stage-{uuid.uuid4().hex}"
    backup = runtime / f"active-backup-{uuid.uuid4().hex}"
    try:
        shutil.copytree(base, staging)
    except Exception:
        if staging.exists():
            _owned_remove(staging, runtime, "active-stage-")
        raise
    if uploads.is_dir():
        for building_dir in uploads.iterdir():
            if not building_dir.is_dir() or not re.fullmatch(r"Building\d+", building_dir.name, flags=re.IGNORECASE):
                continue
            target_dir = staging / building_dir.name
            target_dir.mkdir(parents=True, exist_ok=True)
            for filename in ALLOWED_FILES.values():
                uploaded = building_dir / filename
                if uploaded.is_file():
                    shutil.copy2(uploaded, target_dir / filename)
    try:
        if working.exists():
            os.replace(working, backup)
        os.replace(staging, working)
    except Exception:
        if not working.exists() and backup.exists():
            os.replace(backup, working)
        if staging.exists():
            _owned_remove(staging, runtime, "active-stage-")
        raise
    if backup.exists():
        _owned_remove(backup, runtime, "active-backup-")
    return working


def _merge_csv(existing_path: Path, incoming_bytes: bytes, destination: Path) -> int:
    incoming_path = destination.with_name(f".incoming-{uuid.uuid4().hex}.csv")
    try:
        incoming_path.write_bytes(incoming_bytes)
        old_sep, old_decimal = _csv_format(existing_path)
        new_sep, new_decimal = _csv_format(incoming_path)
        old = pd.read_csv(existing_path, sep=old_sep, decimal=old_decimal, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        new = pd.read_csv(incoming_path, sep=new_sep, decimal=new_decimal, encoding="utf-8-sig", dtype=str, keep_default_na=False)
        if list(old.columns) != list(new.columns):
            raise ValueError("Заголовки CSV не совпадают с текущим файлом. Выберите замену файла целиком или подготовьте тот же формат столбцов.")
        timestamp_column = _timestamp_column(old)
        old_times = pd.to_datetime(old[timestamp_column], format="mixed", utc=True, errors="raise")
        new_times = pd.to_datetime(new[timestamp_column], format="mixed", utc=True, errors="raise")
        if new_times.duplicated().any():
            raise ValueError("В загружаемом файле есть повторяющиеся временные метки.")
        duplicated = set(old_times).intersection(set(new_times))
        if duplicated:
            raise ValueError(f"Найдено совпадающих меток: {len(duplicated)}. Удалите их из файла или замените весь файл целиком.")
        combined = pd.concat([old, new], ignore_index=True)
        combined["__sort_time"] = pd.to_datetime(combined[timestamp_column], format="mixed", utc=True, errors="raise")
        combined = combined.sort_values("__sort_time", kind="stable")
        combined[timestamp_column] = combined["__sort_time"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        combined = combined.drop(columns=["__sort_time"])
        combined.to_csv(destination, sep=old_sep, decimal=old_decimal, index=False, encoding="utf-8-sig")
        return len(new)
    except (UnicodeError, pd.errors.ParserError, pd.errors.EmptyDataError) as error:
        raise ValueError("CSV не удалось прочитать. Проверьте кодировку UTF-8, разделитель и заголовок.") from error
    except (KeyError, TypeError) as error:
        raise ValueError("В CSV не найдена корректная колонка времени.") from error
    finally:
        incoming_path.unlink(missing_ok=True)


def _validate_upload(folder: Path, filename: str, building_number: int) -> None:
    inputs = Inputs(folder)
    errors = [error for error in inputs.input_errors if error["file"] == filename]
    if errors:
        detail = "; ".join(error["message"] for error in errors)
        raise ValueError(f"Файл {filename} не прошёл проверку: {detail}")
    if filename == "invoice.csv" and inputs.inv is None:
        raise ValueError("Счёт не разобран: проверьте обязательные столбцы и временные метки.")
    if filename == "telemetry.csv" and inputs.tel is None:
        raise ValueError("Телеметрия не разобрана: проверьте столбцы и временные метки.")
    if filename == "object.json":
        expected_id = default_object_id(folder)
        object_id = str(inputs.passport.get("object_id", expected_id))
        if object_id != expected_id:
            raise ValueError(f"В паспорте object_id должен быть {expected_id} для выбранного объекта.")


def _passport_for_new_folder(content: bytes, folder: Path) -> bytes:
    """Set the new object's ID from its generated folder in the app-owned copy."""
    try:
        document = json.loads(content.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Паспорт object.json не удалось прочитать как JSON в кодировке UTF-8.") from error
    if not isinstance(document, dict):
        raise ValueError("object.json должен содержать JSON-объект.")
    passport = document.get("passport", document)
    if not isinstance(passport, dict):
        raise ValueError("Поле passport должно содержать JSON-объект.")

    old_id = str(passport.get("object_id", "")).strip()
    new_id = default_object_id(folder)
    passport["object_id"] = new_id

    # Keep any dashboard blocks in the passport linked to the newly assigned ID.
    def remap(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in list(value.items()):
                if key == "object_id" and old_id and item == old_id:
                    value[key] = new_id
                elif isinstance(item, str) and old_id and item.startswith(old_id + "/"):
                    value[key] = new_id + item[len(old_id):]
                else:
                    remap(item)
        elif isinstance(value, list):
            for item in value:
                remap(item)

    remap(document)
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8-sig")


def _rebuild(data_dir: Path, stage_out: Path, log_dir: Path) -> None:
    stage_out.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["LAUNCH_DATA_DIR"] = str(data_dir)
    env["LAUNCH_OUT_DIR"] = str(stage_out)
    env["LAUNCH_LOG_DIR"] = str(log_dir)
    env["LAUNCH_DASHBOARD_PATH"] = str(stage_out / "index.html")
    env["LAUNCH_ENERGY_DASHBOARD_PATH"] = str(stage_out / "energy.html")
    for script in ("run_demo.py", "build_dashboard.py", "build_energy_dashboard.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / script)], cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=300,
        )
        if result.returncode:
            raise RuntimeError(f"Не удалось пересобрать приложение (этап {script}, код {result.returncode}). Проверьте logs/application.log.")
    missing = [name for name in ("results.json", "index.html", "energy.html") if not (stage_out / name).is_file()]
    if missing:
        raise RuntimeError("Сборка не создала файлы: " + ", ".join(missing))


def _promote_outputs(stage_out: Path, out_dir: Path, token: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for source in stage_out.iterdir():
        if not source.is_file():
            continue
        target = out_dir / source.name
        temporary = out_dir / f".{source.name}.{token}.tmp"
        shutil.copy2(source, temporary)
        os.replace(temporary, target)


def add_uploaded_file(building_name: str, kind: str, mode: str, content: bytes) -> dict[str, Any]:
    """Add CSV rows or replace an input file, then rebuild reports from a private copy."""
    folder_match = re.fullmatch(r"Building(\d+)", building_name, flags=re.IGNORECASE)
    if not folder_match:
        raise ValueError("Имя объекта должно иметь вид Building7, Building8 и т. д.")
    building_name = f"Building{int(folder_match.group(1))}"
    if kind not in ALLOWED_FILES:
        raise ValueError("Поддерживаются object.json, invoice.csv и telemetry.csv.")
    creating = mode == "create"
    if mode not in {"append", "replace", "create"} or (kind == "object" and mode not in {"replace", "create"}) or (creating and kind != "object"):
        raise ValueError("Для CSV выберите добавление строк или замену файла; паспорт можно только заменить.")
    if not content:
        raise ValueError("Выбран пустой файл.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("Файл больше 32 МБ.")

    with UPLOAD_LOCK:
        current_data = Path(os.environ["LAUNCH_DATA_DIR"]).resolve()
        upload_dir = Path(os.environ["LAUNCH_UPLOAD_DIR"]).resolve()
        runtime_dir = Path(os.environ["LAUNCH_RUNTIME_DIR"]).resolve()
        out_dir = Path(os.environ["LAUNCH_OUT_DIR"]).resolve()
        log_dir = Path(os.environ["LAUNCH_LOG_DIR"]).resolve()
        if _within(upload_dir, current_data) or upload_dir == current_data:
            raise ValueError("Папка загруженных файлов не должна находиться внутри рабочего набора данных.")
        runtime_dir.mkdir(parents=True, exist_ok=True)
        token = uuid.uuid4().hex
        candidate_data = runtime_dir / f"upload-data-{token}"
        stage_out = runtime_dir / f"upload-out-{token}"
        filename = ALLOWED_FILES[kind]
        try:
            shutil.copytree(current_data, candidate_data)
            target_dir = candidate_data / building_name
            if creating:
                if target_dir.exists() or (upload_dir / building_name).exists():
                    raise ValueError(f"Объект {building_name} уже существует.")
                target_dir.mkdir(parents=True)
            elif not target_dir.is_dir():
                raise ValueError(f"Объект {building_name} не найден.")
            target = candidate_data / building_name / filename
            added_rows = None
            if kind != "object" and mode == "append":
                if not target.is_file():
                    raise ValueError(f"Не найден текущий файл {filename}; выберите замену файла целиком.")
                added_rows = _merge_csv(target, content, target)
            else:
                target.write_bytes(
                    _passport_for_new_folder(content, target.parent)
                    if creating and kind == "object" else content
                )
            _validate_upload(target.parent, filename, int(building_name.removeprefix("Building")))
            if creating:
                new_id = str(Inputs(target.parent).passport.get("object_id", default_object_id(target.parent)))
                for existing_dir in object_folders(current_data):
                    existing_id = str(Inputs(existing_dir).passport.get("object_id", default_object_id(existing_dir)))
                    if existing_id == new_id:
                        raise ValueError(f"object_id {new_id} уже используется объектом {existing_dir.name}.")
            _rebuild(candidate_data, stage_out, log_dir)

            overlay_file = upload_dir / building_name / filename
            overlay_file.parent.mkdir(parents=True, exist_ok=True)
            overlay_tmp = overlay_file.with_name(f".{filename}.{token}.tmp")
            shutil.copy2(target, overlay_tmp)
            os.replace(overlay_tmp, overlay_file)
            _promote_outputs(stage_out, out_dir, token)
            os.environ["LAUNCH_DATA_DIR"] = str(candidate_data)
            if current_data.parent == runtime_dir and current_data.name.startswith("upload-data-"):
                _owned_remove(current_data, runtime_dir, "upload-data-")
            return {
                "ok": True,
                "building": building_name,
                "file": filename,
                "mode": mode,
                "added_rows": added_rows,
                "message": (
                    f"Объект {building_name} создан. Добавьте для него invoice.csv и telemetry.csv. Отчёты обновлены."
                    if creating else
                    f"Добавлено строк: {added_rows}. Отчёты обновлены."
                    if added_rows is not None else "Файл сохранён отдельно от исходных данных. Отчёты обновлены."
                ),
            }
        except Exception:
            if candidate_data.exists():
                _owned_remove(candidate_data, runtime_dir, "upload-data-")
            if stage_out.exists():
                _owned_remove(stage_out, runtime_dir, "upload-out-")
            raise
        finally:
            if stage_out.exists():
                _owned_remove(stage_out, runtime_dir, "upload-out-")


def _bundle_members(content: bytes) -> tuple[str, dict[str, bytes]]:
    """Read one safe object bundle: a ZIP containing object.json and CSV files."""
    if len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("Архив больше 64 МБ.")
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile as error:
        raise ValueError("Не удалось открыть архив. Выберите ZIP с файлами объекта.") from error
    with archive:
        files: dict[str, bytes] = {}
        candidates: set[str] = set()
        total = 0
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/").strip("/")
            parts = [part for part in name.split("/") if part]
            if not parts or any(part in {".", ".."} for part in parts):
                raise ValueError("В архиве найден небезопасный путь к файлу.")
            if len(parts) > 3:
                continue
            filename = parts[-1].casefold()
            if filename not in ALLOWED_FILES.values():
                continue
            if info.file_size > MAX_UPLOAD_BYTES:
                raise ValueError(f"Файл {filename} в архиве больше 32 МБ.")
            total += info.file_size
            if total > MAX_BUNDLE_UNPACKED_BYTES:
                raise ValueError("Распакованный архив слишком большой.")
            folder = "/".join(parts[:-1])
            candidates.add(folder)
            key = f"{folder}/{filename}" if folder else filename
            if key in files:
                raise ValueError(f"В архиве два файла {filename}. Оставьте один комплект данных.")
            files[key] = archive.read(info)
        if not files:
            raise ValueError("В архиве нет object.json, invoice.csv или telemetry.csv.")
        if len(candidates) > 1:
            raise ValueError("Архив должен содержать один объект, а не несколько папок.")
        folder = next(iter(candidates), "")
        result = {key.rsplit("/", 1)[-1]: value for key, value in files.items()}
        if "object.json" not in result:
            raise ValueError("В комплекте обязательно должен быть object.json.")
        return folder, result


def add_uploaded_files(files: dict[str, bytes], folder_hint: str = "") -> dict[str, Any]:
    """Create one new object from several selected files without changing source data."""
    allowed = set(ALLOWED_FILES.values())
    if not files:
        raise ValueError("Выберите файлы объекта.")
    if any(name not in allowed for name in files):
        raise ValueError("Выберите только object.json, invoice.csv и telemetry.csv.")
    if "object.json" not in files:
        raise ValueError("В комплекте обязательно должен быть object.json.")
    if sum(map(len, files.values())) > MAX_BUNDLE_UNPACKED_BYTES:
        raise ValueError("Общий размер выбранных файлов слишком большой.")
    with UPLOAD_LOCK:
        current_data = Path(os.environ["LAUNCH_DATA_DIR"]).resolve()
        upload_dir = Path(os.environ["LAUNCH_UPLOAD_DIR"]).resolve()
        runtime_dir = Path(os.environ["LAUNCH_RUNTIME_DIR"]).resolve()
        out_dir = Path(os.environ["LAUNCH_OUT_DIR"]).resolve()
        log_dir = Path(os.environ["LAUNCH_LOG_DIR"]).resolve()
        if _within(upload_dir, current_data) or upload_dir == current_data:
            raise ValueError("Папка загруженных файлов не должна находиться внутри рабочего набора данных.")
        existing_numbers = [int(match.group(1)) for item in (*object_folders(current_data), *object_folders(upload_dir))
                            if (match := re.fullmatch(r"Building(\d+)", item.name, flags=re.IGNORECASE))]
        folder_name = folder_hint.rsplit("/", 1)[-1] if folder_hint else ""
        if not re.fullmatch(r"Building\d+", folder_name, flags=re.IGNORECASE):
            folder_name = f"Building{max(existing_numbers, default=0) + 1}"
        folder_name = f"Building{int(re.search(r'\d+', folder_name).group())}"
        if (current_data / folder_name).exists() or (upload_dir / folder_name).exists():
            raise ValueError(f"Объект {folder_name} уже существует. Переименуйте папку в архиве или удалите старую загрузку.")

        token = uuid.uuid4().hex
        candidate_data = runtime_dir / f"upload-data-{token}"
        stage_out = runtime_dir / f"upload-out-{token}"
        try:
            shutil.copytree(current_data, candidate_data)
            target_dir = candidate_data / folder_name
            target_dir.mkdir(parents=True)
            files = dict(files)
            files["object.json"] = _passport_for_new_folder(files["object.json"], target_dir)
            for filename, raw in files.items():
                target = target_dir / filename
                target.write_bytes(raw)
            # Validate only after every selected file is in place. The browser
            # preserves the user's selection order, so invoice.csv can arrive
            # before object.json; its EAN is supplied by that passport.
            for filename in files:
                _validate_upload(target_dir, filename, int(re.search(r"\d+", folder_name).group()))
            new_id = str(Inputs(target_dir).passport.get("object_id", default_object_id(target_dir)))
            for existing_dir in object_folders(current_data):
                existing_id = str(Inputs(existing_dir).passport.get("object_id", default_object_id(existing_dir)))
                if existing_id == new_id:
                    raise ValueError(f"object_id {new_id} уже используется объектом {existing_dir.name}.")
            _rebuild(candidate_data, stage_out, log_dir)
            saved_dir = upload_dir / folder_name
            saved_dir.mkdir(parents=True, exist_ok=True)
            for filename, raw in files.items():
                target = saved_dir / filename
                temporary = saved_dir / f".{filename}.{token}.tmp"
                temporary.write_bytes(raw)
                os.replace(temporary, target)
            _promote_outputs(stage_out, out_dir, token)
            os.environ["LAUNCH_DATA_DIR"] = str(candidate_data)
            missing = [name for name in ALLOWED_FILES.values() if name not in files]
            return {
                "ok": True,
                "building": folder_name,
                "mode": "bundle",
                "files": sorted(files),
                "missing": missing,
                "message": (
                    f"Объект {folder_name} добавлен одним комплектом. Отчёты пересчитаны."
                    + (f" Не хватает: {', '.join(missing)} — это будет показано как предупреждение." if missing else "")
                ),
            }
        except Exception:
            if candidate_data.exists():
                _owned_remove(candidate_data, runtime_dir, "upload-data-")
            if stage_out.exists():
                _owned_remove(stage_out, runtime_dir, "upload-out-")
            raise
        finally:
            if stage_out.exists():
                _owned_remove(stage_out, runtime_dir, "upload-out-")


def add_uploaded_bundle(content: bytes) -> dict[str, Any]:
    """Backward-compatible ZIP import; new interface uploads selected files directly."""
    folder_hint, files = _bundle_members(content)
    return add_uploaded_files(files, folder_hint)
