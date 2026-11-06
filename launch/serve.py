"""Small standard-library HTTP server for the generated dashboard."""
import logging
import os
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from .logging_config import configure_logging

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))
LOG_DIR = Path(os.environ.get("LAUNCH_LOG_DIR", ROOT / "logs"))
logger = configure_logging(LOG_DIR)


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUT), **kwargs)

    def do_GET(self):
        if urlsplit(self.path).path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        if urlsplit(self.path).path == "/":
            self.path = "/index.html"
        super().do_GET()

    def log_message(self, fmt, *args):
        logger.info("http client=%s %s", self.client_address[0], fmt % args)


def serve():
    host = os.environ.get("LAUNCH_HOST", "0.0.0.0")
    port = int(os.environ.get("LAUNCH_PORT", "8000"))
    if not (OUT / "index.html").exists():
        raise FileNotFoundError(f"Dashboard not found: {OUT / 'index.html'}")
    server = ThreadingHTTPServer((host, port), DashboardHandler)
    logger.info("Dashboard available on port=%s", port)
    if os.environ.get("LAUNCH_OPEN_BROWSER") == "1":
        threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}",)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    finally:
        server.server_close()


