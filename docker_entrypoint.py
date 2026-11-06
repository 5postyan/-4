"""Start the data demo, generate the dashboard, and serve it locally."""
import logging
import os
import subprocess
import sys
from pathlib import Path
from launch.logging_config import configure_logging

ROOT = Path(__file__).resolve().parent
logger = configure_logging(os.environ.get("LAUNCH_LOG_DIR", ROOT / "logs"))


def main():
    commands = [
        [sys.executable, str(ROOT / "run_demo.py")],
        [sys.executable, str(ROOT / "build_dashboard.py")],
    ]
    for command in commands:
        logger.info("Starting task=%s", Path(command[1]).name)
        subprocess.run(command, cwd=ROOT, check=True)
    from launch.serve import serve
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Application startup failed")
        raise
