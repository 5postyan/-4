"""HTTP-сервер для сгенерированного дашборда."""
import logging
import os
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .logging_config import configure_logging

ROOT = Path(__file__).resolve().parent.parent  # Корневая директория проекта
OUT = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))  # Директория с дашбордом
LOG_DIR = Path(os.environ.get("LAUNCH_LOG_DIR", ROOT / "logs"))  # Директория логов
logger = configure_logging(LOG_DIR)


class DashboardHandler(SimpleHTTPRequestHandler):
    """HTTP-обработчик для дашборда.
    
    Поддерживает два URL-пути:
    - /health: health-чек (JSON {"status":"ok"})
    - /: главная страница (index.html)
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUT), **kwargs)
    
    def do_GET(self):
        """Обработка GET-запроса.
        
        Проверяем URL-путь:
        - Если /health → возвращаем health-чек
        - Если / → перенаправляем на index.html
        - Otherwise → стандартная обработка SimpleHTTPRequestHandler
        """
        if urlsplit(self.path).path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        
        if urlsplit(self.path).path == "/":
            self.path = "/index.html"  # Перенаправляем на главную страницу
        
        super().do_GET()  # Стандартная обработка остальных запросов
    
    def log_message(self, fmt, *args):
        """Логирование HTTP-запросов (только IP и путь, без тела запроса)."""
        logger.info("http client=%s %s", self.client_address[0], fmt % args)


def serve():
    """Запуск HTTP-сервера.
    
    Args:
        host: Адрес сервера (по умолчанию 0.0.0.0 для доступа извне).
        port: Номер порта (по умолчанию 8000).
        
    Raises:
        FileNotFoundError: Если дашборд не найден.
    """
    host = os.environ.get("LAUNCH_HOST", "0.0.0.0")
    port = int(os.environ.get("LAUNCH_PORT", "8000"))
    
    if not (OUT / "index.html").exists():  # Проверяем, существует ли дашборд
        raise FileNotFoundError(f"Dashboard not found: {OUT / 'index.html'}")
    
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    logger.info("Dashboard available on port=%s", port)
    
    # Автоматически открываем браузер, если LAUNCH_OPEN_BROWSER=1
    if os.environ.get("LAUNCH_OPEN_BROWSER") == "1":
        threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
    
    try:
        server.serve_forever()  # Блокируем выполнение until SIGINT
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.server_close()  # Закрываем сокеты
