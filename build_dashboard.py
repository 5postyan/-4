"""Сборка самодостаточного дашборда из последних результатов."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent  # Корневая директория проекта
OUT = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))  # Директория результатов
SOURCE = OUT / "results.json"  # Исходный файл с результатами
TARGET = Path(os.environ.get("LAUNCH_DASHBOARD_PATH", OUT / "index.html"))  # Целевой дашборд


def main():
    """Основная функция сборки дашборда.
    
    Шаги:
    1. Чтение results.json (результаты проверки)
    2. Подстановка данных в HTML-шаблон (замена __DATA__)
    3. Сохранение собранного HTML-файла
    
    Raises:
        SystemExit: Если results.json не найден (должен предшествовать build_dashboard.py)
    """
    if not SOURCE.exists():  # Проверяем, существует ли результаты
        raise SystemExit(f"Results file not found: {SOURCE}. Run run_demo.py first.")
    
    # Чтение и десериализация результатов
    data = json.loads(SOURCE.read_text(encoding="utf-8"))
    
    # Чтение HTML-шаблона
    html = (ROOT / "dashboard_template.html").read_text(encoding="utf-8")
    
    # Подстановка данных: замена __DATA__ в HTML
    # json.dumps → ensure_ascii=False (русский текст не экранируется)
    # separators=(',', ':') → компактный формат (без пробелов)
    # replace('</', '<\/') → экранирование </ в JS-коде (защита от XSS)
    html = html.replace("__DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/"))
    
    # Создание директории для результата
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    
    # Сохранение собранного HTML
    TARGET.write_text(html, encoding="utf-8")
    print(f"Dashboard built: {TARGET} ({len(html) // 1024} KB)")


if __name__ == "__main__":
    main()
