"""Native Windows startup: prepare a private input copy, build pages, and serve them."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from launch.data_uploads import prepare_data_workspace
from launch.logging_config import configure_logging
from launch.paths import ROOT, case_data_dir


def _absolute_dir(value: str | Path, fallback: Path) -> Path:
    path = Path(value or fallback).expanduser()
    return path if path.is_absolute() else (ROOT / path).resolve()


def configure_paths() -> None:
    # The user-provided input path is the immutable base. LAUNCH_DATA_DIR is
    # switched to the application's private working copy immediately after.
    configured_base = os.environ.get("LAUNCH_BASE_DATA_DIR") or os.environ.get("LAUNCH_DATA_DIR")
    base_dir = _absolute_dir(configured_base, case_data_dir(ROOT)) if configured_base else case_data_dir(ROOT)
    os.environ["LAUNCH_BASE_DATA_DIR"] = str(base_dir.resolve())
    defaults = {
        "LAUNCH_OUT_DIR": ROOT / "out",
        "LAUNCH_LOG_DIR": ROOT / "logs",
        "LAUNCH_UPLOAD_DIR": ROOT / "added_data",
        "LAUNCH_RUNTIME_DIR": ROOT / "runtime_data",
        "LAUNCH_HOST": "127.0.0.1",
        "LAUNCH_PORT": "8001",
        "LAUNCH_OPEN_BROWSER": "1",
    }
    for key, default in defaults.items():
        if key.endswith("_DIR"):
            os.environ[key] = str(_absolute_dir(os.environ.get(key, str(default)), default).resolve())
        else:
            os.environ.setdefault(key, str(default))
    os.environ["LAUNCH_DATA_DIR"] = str(base_dir.resolve())


def main() -> None:
    configure_paths()
    logger = configure_logging(os.environ["LAUNCH_LOG_DIR"])
    base_dir = Path(os.environ["LAUNCH_BASE_DATA_DIR"])
    out_dir = Path(os.environ["LAUNCH_OUT_DIR"])
    data_dir = prepare_data_workspace(
        base_dir,
        Path(os.environ["LAUNCH_UPLOAD_DIR"]),
        Path(os.environ["LAUNCH_RUNTIME_DIR"]),
        out_dir,
    )
    os.environ["LAUNCH_DATA_DIR"] = str(data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Local startup base_data_dir=%s working_data_dir=%s out_dir=%s", base_dir, data_dir, out_dir)

    for script in ("run_demo.py", "build_dashboard.py", "build_energy_dashboard.py"):
        logger.info("Starting task=%s", script)
        completed = subprocess.run(
            [sys.executable, str(ROOT / script)], cwd=ROOT, env=os.environ.copy(), check=False
        )
        if completed.returncode:
            logger.error("Startup task failed task=%s exit_code=%d", script, completed.returncode)
            raise SystemExit(completed.returncode)

    missing = [name for name in ("index.html", "energy.html") if not (out_dir / name).is_file()]
    if missing:
        logger.error("Generated dashboard page(s) missing: %s", ", ".join(missing))
        raise SystemExit(1)

    from launch.serve import serve
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger = configure_logging(Path(__file__).resolve().parent / "logs")
        logger.exception("Local application startup failed")
        raise
