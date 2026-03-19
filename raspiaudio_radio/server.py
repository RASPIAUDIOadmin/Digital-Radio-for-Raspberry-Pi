from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict
from urllib.parse import parse_qs, unquote, urlparse

from .backend import RadioBackend, RadioConfig

STATIC_DIR = Path(__file__).resolve().parent / "static"


class RadioHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], backend: RadioBackend) -> None:
        self.backend = backend
        super().__init__(server_address, RadioRequestHandler)


class RadioRequestHandler(BaseHTTPRequestHandler):
    server: RadioHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/status":
            self._send_ok(self.server.backend.get_status())
            return
        if parsed.path == "/api/stations":
            mode = query.get("mode", [None])[0]
            refresh = query.get("refresh", ["0"])[0].lower() in {"1", "true", "yes"}
            stations = self.server.backend.get_stations(mode=mode, refresh_from_disk=refresh)
            self._send_ok({"stations": stations, "count": len(stations), "mode": mode or self.server.backend.get_status()["mode"]})
            return
        if parsed.path == "/api/favorites":
            favorites = self.server.backend.get_favorites()
            self._send_ok({"stations": favorites, "count": len(favorites)})
            return
        if parsed.path == "/api/recordings":
            recordings = self.server.backend.get_recordings()
            self._send_ok({"recordings": recordings, "count": len(recordings)})
            return
        if parsed.path.startswith("/recordings/"):
            self._serve_recording(parsed.path)
            return
        self._serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        body = self._read_json_body()
        try:
            if parsed.path == "/api/boot":
                self._send_ok(
                    self.server.backend.boot(
                        mode=body.get("mode"),
                        force=bool(body.get("force", False)),
                    )
                )
                return
            if parsed.path == "/api/mode":
                self._send_ok(self.server.backend.set_mode(str(body.get("mode", ""))))
                return
            if parsed.path == "/api/scan":
                self._send_ok(self.server.backend.scan(force=bool(body.get("force", True))))
                return
            if parsed.path == "/api/play":
                self._send_ok(
                    self.server.backend.play(
                        index=body.get("index"),
                        label=body.get("label"),
                        station_id=body.get("station_id"),
                    )
                )
                return
            if parsed.path == "/api/volume":
                self._send_ok(self.server.backend.set_volume(level=body.get("level"), delta=body.get("delta")))
                return
            if parsed.path == "/api/amplifier":
                self._send_ok(self.server.backend.set_amplifier(bool(body.get("enabled", False))))
                return
            if parsed.path == "/api/favorite":
                self._send_ok(
                    self.server.backend.set_favorite(
                        station_id=str(body.get("station_id", "")),
                        favorite=body.get("favorite"),
                    )
                )
                return
            if parsed.path == "/api/record":
                self._send_ok(self.server.backend.record(action=str(body.get("action", "toggle"))))
                return
            self._send_error_json(404, "Unknown route.")
        except ValueError as exc:
            self._send_error_json(400, str(exc))
        except Exception as exc:
            self._send_error_json(500, str(exc))

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        payload = self.rfile.read(length)
        if not payload:
            return {}
        return json.loads(payload.decode("utf-8"))

    def _send_ok(self, data: Any) -> None:
        self._send_json(200, {"ok": True, "data": data})

    def _send_error_json(self, status_code: int, message: str) -> None:
        self._send_json(status_code, {"ok": False, "error": message})

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        if relative not in {"index.html", "app.js", "styles.css"}:
            self._send_error_json(404, "File not found.")
            return
        file_path = STATIC_DIR / relative
        if not file_path.exists():
            self._send_error_json(404, "File not found.")
            return
        self._serve_file(file_path)

    def _serve_recording(self, request_path: str) -> None:
        file_name = Path(unquote(request_path[len("/recordings/") :])).name
        backend = self.server.backend
        file_path = backend.config.recordings_dir / file_name
        if not file_path.exists() or file_path.suffix.lower() != ".wav":
            self._send_error_json(404, "Recording not found.")
            return
        self._serve_file(file_path, cache_control="no-store")

    def _serve_file(self, file_path: Path, cache_control: str = "no-cache") -> None:
        content = file_path.read_bytes()
        content_type, _ = mimetypes.guess_type(file_path.name)
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        self.wfile.write(content)


def run_server(config: RadioConfig, host: str, port: int) -> None:
    backend = RadioBackend(config)
    httpd = RadioHTTPServer((host, port), backend)
    try:
        print(f"Raspiaudio radio server listening on http://{host}:{port}")
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
        httpd.server_close()
