"""Общий модуль логирования приложения. Никогда не логируем сырые показания и секреты."""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(log_dir):
    """Настройка лог-режима.
    
    Эта функция конфигурирует логгер 'launch' с:
    - Запись в файл application.log (с ротацией при 5 МБ, 3 backup-файлов)
    - Вывод в stdout
    
    Args:
        log_dir: Директория для логов.
        
    Returns:
        Логгер уровня INFO.
    """
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)  # Создаём директорию, если её нет
    
    logger = logging.getLogger("launch")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # Отключаем propagate, чтобы избежать дублирования в root-логгере
    
    if logger.handlers:  # Если handlers уже есть (некоторые модули могут их добавить), возвращаем существующий
        return logger
    
    # Форматтер:timestamp level name message
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    
    # FileHandler: запись в файл с ротацией (5 МБ, 3 backup)
    file_handler = RotatingFileHandler(
        directory / "application.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    
    # ConsoleHandler: вывод в stdout для отладки
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger
