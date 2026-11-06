"""Запуск демо-данных,生成 дашборд и serve его локально."""
import logging
import os
import subprocess
import sys
from pathlib import Path
from launch.logging_config import configure_logging

ROOT = Path(__file__).resolve().parent  # Корневая директория проекта
logger = configure_logging(os.environ.get("LAUNCH_LOG_DIR", ROOT / "logs"))


def main():
    """Основной entry-point для Docker-запуска.
    
    Запускает два этапа:
    1. run_demo.py — генерация демо-данных и протоколов
    2. build_dashboard.py — сборка HTML-дашборда
    
    Затем запускает HTTP-сервер для serve дашборда.
    """
    commands = [
        [sys.executable, str(ROOT / "run_demo.py")],
        [sys.executable, str(ROOT / "build_dashboard.py")],
    ]
    for command in commands:
        logger.info("Starting task=%s", Path(command[1]).name)
        subprocess.run(command, cwd=ROOT, check=True)  # check=True → raise на ошибке
    
    from launch.serve import serve  # Импорт deferred, чтобы не загружать лишние модули
    serve()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Application startup failed")  # Логируем traceback
        raise  # Пробрасываем ошибку дальше
