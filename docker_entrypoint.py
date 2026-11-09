"""Build the local energy balance and launch dashboards inside Docker."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from launch.data_uploads import prepare_data_workspace
from launch.logging_config import configure_logging
from launch.paths import ROOT


def main() -> None:
    os.environ.setdefault("LAUNCH_BASE_DATA_DIR", os.environ.get("LAUNCH_DATA_DIR", str(ROOT / "data")))
    os.environ.setdefault("LAUNCH_OUT_DIR", str(ROOT / "out"))
    os.environ.setdefault("LAUNCH_LOG_DIR", str(ROOT / "logs"))
    os.environ.setdefault("LAUNCH_UPLOAD_DIR", str(ROOT / "added_data"))
    os.environ.setdefault("LAUNCH_RUNTIME_DIR", str(ROOT / "runtime_data"))
    base_dir = Path(os.environ["LAUNCH_BASE_DATA_DIR"]).expanduser().resolve()
    out_dir = Path(os.environ["LAUNCH_OUT_DIR"]).expanduser().resolve()
    logger = configure_logging(os.environ["LAUNCH_LOG_DIR"])
    out_dir.mkdir(parents=True, exist_ok=True)
    data_dir = prepare_data_workspace(
        base_dir,
        Path(os.environ["LAUNCH_UPLOAD_DIR"]),
        Path(os.environ["LAUNCH_RUNTIME_DIR"]),
        out_dir,
    )
    os.environ["LAUNCH_DATA_DIR"] = str(data_dir)
    logger.info("Docker startup base_data_dir=%s working_data_dir=%s out_dir=%s", base_dir, data_dir, out_dir)

    for script in ("run_demo.py", "build_dashboard.py", "build_energy_dashboard.py"):
        logger.info("Starting task=%s", script)
        subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT, env=os.environ.copy(), check=True)

    from launch.serve import serve
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.getLogger("launch").exception("Application startup failed")
        raise
