"""Нативный Windows-запуск: генерация результатов и serve дашборда локально."""
import logging
import os
import subprocess
import sys
from pathlib import Path
from launch.logging_config import configure_logging

ROOT = Path(__file__).resolve().parent  # Корневая директория проекта

# Настройка环境 переменных для本地 запуска
os.environ.setdefault("LAUNCH_DATA_DIR", str(ROOT / "data"))       # Директория данных
os.environ.setdefault("LAUNCH_OUT_DIR", str(ROOT / "out"))           # Директория результатов
os.environ.setdefault("LAUNCH_LOG_DIR", str(ROOT / "logs"))           # Директория логов
os.environ.setdefault("LAUNCH_HOST", "127.0.0.1")                   # only local access
os.environ.setdefault("LAUNCH_PORT", "8001")                        # Different port from Docker
os.environ.setdefault("LAUNCH_OPEN_BROWSER", "1")                 # Auto open browser
logger = configure_logging(os.environ["LAUNCH_LOG_DIR"])


def main():
    """Основной entry-point для本地 Windows-запуска.
    
    Запускает два этапа:
    1. run_demo.py — генерация демо-данных и протоколов
    2. build_dashboard.py — сборка HTML-дашборда
    
    Затем запускает HTTP-сервер для serve дашборда.
    """
    for script in ("run_demo.py", "build_dashboard.py"):
        logger.info("Starting task=%s", script)
        result = subprocess.run(
            [sys.executable, str(ROOT / script)],
            cwd=ROOT,
            check=False,
            env=os.environ.copy(),
        )
        if result.returncode:
            logger.error(
                "Task failed task=%s exit_code=%d; see console for traceback",
                script,
                result.returncode,
            )
            raise SystemExit(result.returncode)
    
    from launch.serve import serve  # Импорт deferred
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Local application startup failed")
        raise
