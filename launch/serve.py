"""Local HTTP server for the generated dashboards and safe input uploads."""
import errno
import email
from email import policy
from email.parser import BytesParser
import json
import logging
import os
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .data_uploads import MAX_BUNDLE_BYTES, MAX_BUNDLE_UNPACKED_BYTES, MAX_UPLOAD_BYTES, add_uploaded_bundle, add_uploaded_file, add_uploaded_files
from .logging_config import configure_logging

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("LAUNCH_OUT_DIR", ROOT / "out"))
LOG_DIR = Path(os.environ.get("LAUNCH_LOG_DIR", ROOT / "logs"))
HOME_PAGE = "energy.html"
logger = configure_logging(LOG_DIR)


class DashboardServer(ThreadingHTTPServer):
    # Avoid attaching a second run to a stale server already bound to this port.
    allow_reuse_address = False


class DashboardHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OUT), **kwargs)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/health":
            self._json(200, {"status": "ok"})
            return
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/energy.html")
            self.end_headers()
            return
        super().do_GET()

    def do_POST(self):
        request = urlsplit(self.path)
        if request.path == "/api/upload-files":
            self._upload_files()
            return
        if request.path == "/api/upload-bundle":
            self._upload_bundle()
            return
        if request.path != "/api/upload":
            self.send_error(404)
            return

        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in {"127.0.0.1", "localhost"}:
            self._json(403, {"ok": False, "message": "Загрузка разрешена только со страницы локального приложения."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"ok": False, "message": "Некорректный размер файла."})
            return
        if length < 1:
            self._json(400, {"ok": False, "message": "Файл пуст."})
            return
        if length > MAX_UPLOAD_BYTES:
            self._json(413, {"ok": False, "message": "Файл больше 32 МБ."})
            return

        params = parse_qs(request.query)
        building = params.get("building", [""])[0]
        kind = params.get("kind", [""])[0]
        mode = params.get("mode", [""])[0]
        content = self.rfile.read(length)
        if len(content) != length:
            self._json(400, {"ok": False, "message": "Файл передан не полностью."})
            return
        try:
            result = add_uploaded_file(building, kind, mode, content)
            self._json(200, result)
        except ValueError as error:
            self._json(400, {"ok": False, "message": str(error)})
        except RuntimeError as error:
            logger.error("upload_rebuild_failed object=%s kind=%s reason=%s", building, kind, error)
            self._json(500, {"ok": False, "message": str(error)})
        except Exception as error:
            logger.error("upload_failed object=%s kind=%s error_type=%s", building, kind, type(error).__name__)
            self._json(500, {"ok": False, "message": "Не удалось обработать файл. Проверьте журнал приложения."})

    def _upload_bundle(self) -> None:
        """Accept one ZIP containing a complete object's files."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in {"127.0.0.1", "localhost"}:
            self._json(403, {"ok": False, "message": "Загрузка разрешена только со страницы локального приложения."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"ok": False, "message": "Некорректный размер архива."})
            return
        if length < 1:
            self._json(400, {"ok": False, "message": "Выберите ZIP-архив с комплектом объекта."})
            return
        if length > MAX_BUNDLE_BYTES:
            self._json(413, {"ok": False, "message": "Архив больше 64 МБ."})
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self._json(400, {"ok": False, "message": "Архив передан не полностью."})
            return
        try:
            result = add_uploaded_bundle(content)
            logger.info("upload_bundle_success object=%s files=%s", result.get("building"), ",".join(result.get("files", [])))
            self._json(200, result)
        except ValueError as error:
            self._json(400, {"ok": False, "message": str(error)})
        except RuntimeError as error:
            logger.error("upload_bundle_rebuild_failed reason=%s", error)
            self._json(500, {"ok": False, "message": str(error)})
        except Exception as error:
            logger.error("upload_bundle_failed error_type=%s", type(error).__name__)
            self._json(500, {"ok": False, "message": "Не удалось обработать комплект. Проверьте журнал приложения."})

    def _upload_files(self) -> None:
        """Accept object.json and CSV files selected together in the browser."""
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in {"127.0.0.1", "localhost"}:
            self._json(403, {"ok": False, "message": "Загрузка разрешена только со страницы локального приложения."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"ok": False, "message": "Некорректный размер файлов."})
            return
        if length < 1:
            self._json(400, {"ok": False, "message": "Выберите файлы объекта."})
            return
        if length > MAX_BUNDLE_UNPACKED_BYTES + 1024 * 1024:
            self._json(413, {"ok": False, "message": "Общий размер выбранных файлов слишком большой."})
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self._json(400, {"ok": False, "message": "Файлы переданы не полностью."})
            return
        content_type = self.headers.get("Content-Type", "")
        try:
            message = BytesParser(policy=policy.default).parsebytes(
                f"MIME-Version: 1.0\r\nContent-Type: {content_type}\r\n\r\n".encode("ascii") + content
            )
            if not message.is_multipart():
                raise ValueError("Форма загрузки должна передавать несколько файлов.")
            files: dict[str, bytes] = {}
            for part in message.iter_parts():
                filename = part.get_filename()
                if not filename:
                    continue
                name = Path(filename.replace("\\", "/")).name.casefold()
                if name not in {"object.json", "invoice.csv", "telemetry.csv"}:
                    raise ValueError("Выберите только object.json, invoice.csv и telemetry.csv.")
                if name in files:
                    raise ValueError(f"Файл {name} выбран дважды.")
                files[name] = part.get_payload(decode=True) or b""
            result = add_uploaded_files(files)
            logger.info("upload_files_success object=%s files=%s", result.get("building"), ",".join(result.get("files", [])))
            self._json(200, result)
        except ValueError as error:
            self._json(400, {"ok": False, "message": str(error)})
        except RuntimeError as error:
            logger.error("upload_files_rebuild_failed reason=%s", error)
            self._json(500, {"ok": False, "message": str(error)})
        except Exception as error:
            logger.error("upload_files_failed error_type=%s", type(error).__name__)
            self._json(500, {"ok": False, "message": "Не удалось обработать файлы. Проверьте журнал приложения."})

    def log_message(self, fmt, *args):
        logger.info("http client=%s %s", self.client_address[0], fmt % args)


def serve():
    host = os.environ.get("LAUNCH_HOST", "0.0.0.0")
    port = int(os.environ.get("LAUNCH_PORT", "8000"))
    if not (OUT / "index.html").is_file():
        raise FileNotFoundError(f"Launch dashboard not found: {OUT / 'index.html'}")
    if not (OUT / "energy.html").is_file():
        raise FileNotFoundError(f"Energy dashboard not found: {OUT / 'energy.html'}")

    server = None
    last_error = None
    for candidate in range(port, port + 21):
        try:
            server = DashboardServer((host, candidate), DashboardHandler)
            port = candidate
            break
        except OSError as error:
            last_error = error
            if error.errno not in {errno.EADDRINUSE, 48, 98, 10048} and getattr(error, "winerror", None) != 10048:
                raise
            logger.warning("Port %s is already in use; trying the next port", candidate)
    if server is None:
        raise OSError(f"No free port found from {port - 20} to {port}") from last_error

    logger.info("Dashboard available at http://%s:%s/%s", host, port, HOME_PAGE)
    if os.environ.get("LAUNCH_OPEN_BROWSER") == "1":
        threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{port}/{HOME_PAGE}",)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Dashboard server stopped by user")
    finally:
        server.server_close()
