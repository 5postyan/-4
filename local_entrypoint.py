"""Native Windows launcher: generate results and serve the dashboard locally."""
import logging
import os
import subprocess
import sys
from pathlib import Path
from launch.logging_config import configure_logging

ROOT = Path(__file__).resolve().parent
os.environ.setdefault("LAUNCH_DATA_DIR", str(ROOT / "data"))
os.environ.setdefault("LAUNCH_OUT_DIR", str(ROOT / "out"))
os.environ.setdefault("LAUNCH_LOG_DIR", str(ROOT / "logs"))
os.environ.setdefault("LAUNCH_HOST", "127.0.0.1")
os.environ.setdefault("LAUNCH_PORT", "8001")
os.environ.setdefault("LAUNCH_OPEN_BROWSER", "1")
logger = configure_logging(os.environ["LAUNCH_LOG_DIR"])


def main():
    for script in ("run_demo.py", "build_dashboard.py"):
        logger.info("Starting task=%s", script)
        subprocess.run([sys.executable, str(ROOT / script)], cwd=ROOT, check=True, env=os.environ.copy())
    from launch.serve import serve
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Local application startup failed")
        raise
