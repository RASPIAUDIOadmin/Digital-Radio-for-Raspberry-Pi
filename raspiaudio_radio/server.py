from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from typing import Any, Dict, List
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
        self._dispatch_get(send_body=True)

    def do_HEAD(self) -> None:
        self._dispatch_get(send_body=False)

    def _dispatch_get(self, send_body: bool) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/status":
            self._send_ok(self.server.backend.get_status(), send_body=send_body)
            return
        if parsed.path == "/api/stations":
            mode = query.get("mode", [None])[0]
            refresh = query.get("refresh", ["0"])[0].lower() in {"1", "true", "yes"}
            stations = self.server.backend.get_stations(mode=mode, refresh_from_disk=refresh)
            self._send_ok(
                {"stations": stations, "count": len(stations), "mode": mode or self.server.backend.get_status()["mode"]},
                send_body=send_body,
            )
            return
        if parsed.path == "/api/favorites":
            favorites = self.server.backend.get_favorites()
            self._send_ok({"stations": favorites, "count": len(favorites)}, send_body=send_body)
            return
        if parsed.path == "/api/recordings":
            recordings = self.server.backend.get_recordings()
            self._send_ok({"recordings": recordings, "count": len(recordings)}, send_body=send_body)
            return
        if parsed.path == "/api/dab/artwork":
            self._serve_dab_artwork(send_body=send_body)
            return
        if parsed.path.startswith("/recordings/"):
            self._serve_recording(parsed.path, send_body=send_body)
            return
        self._serve_static(parsed.path, send_body=send_body)

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
            if parsed.path == "/api/mute":
                self._send_ok(self.server.backend.set_muted(body.get("enabled")))
                return
            if parsed.path == "/api/oled":
                self._send_ok(self.server.backend.set_oled_enabled(bool(body.get("enabled", False))))
                return
            if parsed.path == "/api/system-autostart":
                self._send_ok(self.server.backend.set_start_with_system(bool(body.get("enabled", False))))
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
            if parsed.path == "/api/flash/program":
                self._send_ok(
                    self.server.backend.flash_program(
                        mode=body.get("mode"),
                        run_self_test=bool(body.get("self_test", True)),
                    )
                )
                return
            if parsed.path == "/api/server/stop":
                self._send_ok({"stopping": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if parsed.path == "/api/server/restart":
                self._send_ok({"restarting": True})
                threading.Thread(target=_restart_process, args=(self.server,), daemon=True).start()
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

    def _send_ok(self, data: Any, send_body: bool = True) -> None:
        self._send_json(200, {"ok": True, "data": data}, send_body=send_body)

    def _send_error_json(self, status_code: int, message: str, send_body: bool = True) -> None:
        self._send_json(status_code, {"ok": False, "error": message}, send_body=send_body)

    def _send_json(self, status_code: int, payload: Dict[str, Any], send_body: bool = True) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(data)

    def _serve_static(self, request_path: str, send_body: bool = True) -> None:
        relative = request_path.lstrip("/") or "index.html"
        if relative not in {"index.html", "app.js", "styles.css", "favicon.png"}:
            self._send_error_json(404, "File not found.", send_body=send_body)
            return
        file_path = STATIC_DIR / relative
        if not file_path.exists():
            self._send_error_json(404, "File not found.", send_body=send_body)
            return
        self._serve_file(file_path, send_body=send_body)

    def _serve_recording(self, request_path: str, send_body: bool = True) -> None:
        file_name = Path(unquote(request_path[len("/recordings/") :])).name
        backend = self.server.backend
        file_path = backend.config.recordings_dir / file_name
        if not file_path.exists() or file_path.suffix.lower() != ".wav":
            self._send_error_json(404, "Recording not found.", send_body=send_body)
            return
        self._serve_file(file_path, cache_control="no-store", allow_ranges=True, send_body=send_body)

    def _serve_dab_artwork(self, send_body: bool = True) -> None:
        artwork = self.server.backend.get_dab_artwork()
        if artwork is None:
            self._send_error_json(404, "Artwork not available.", send_body=send_body)
            return
        self._serve_bytes(
            artwork["content"],
            artwork.get("content_type") or "application/octet-stream",
            cache_control="no-store",
            send_body=send_body,
        )

    def _serve_file(
        self,
        file_path: Path,
        cache_control: str = "no-cache",
        allow_ranges: bool = False,
        send_body: bool = True,
    ) -> None:
        if not file_path.exists():
            self._send_error_json(404, "File not found.", send_body=send_body)
            return
        file_size = file_path.stat().st_size
        content_type, _ = mimetypes.guess_type(file_path.name)
        range_header = self.headers.get("Range") if allow_ranges else None
        byte_range = self._parse_byte_range(range_header, file_size)
        if byte_range == "invalid":
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            return
        start = 0
        end = file_size - 1
        status_code = 200
        if isinstance(byte_range, tuple):
            start, end = byte_range
            status_code = 206
        content_length = max(0, end - start + 1)
        self.send_response(status_code)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(content_length))
        self.send_header("Cache-Control", cache_control)
        if allow_ranges:
            self.send_header("Accept-Ranges", "bytes")
        if status_code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
        self.end_headers()
        if not send_body or content_length <= 0:
            return
        with file_path.open("rb") as source:
            source.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = source.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _serve_bytes(
        self,
        content: bytes,
        content_type: str,
        cache_control: str = "no-cache",
        send_body: bool = True,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if send_body:
            self.wfile.write(content)

    def _parse_byte_range(self, header_value: str | None, file_size: int) -> tuple[int, int] | str | None:
        if not header_value:
            return None
        if not header_value.startswith("bytes="):
            return "invalid"
        spec = header_value[6:].strip()
        if "," in spec or "-" not in spec:
            return "invalid"
        start_text, end_text = spec.split("-", 1)
        try:
            if start_text == "":
                length = int(end_text)
                if length <= 0:
                    return "invalid"
                start = max(0, file_size - length)
                end = file_size - 1
            else:
                start = int(start_text)
                if start < 0 or start >= file_size:
                    return "invalid"
                end = int(end_text) if end_text else file_size - 1
                end = min(end, file_size - 1)
                if end < start:
                    return "invalid"
        except ValueError:
            return "invalid"
        return (start, end)


def _restart_process(server: RadioHTTPServer) -> None:
    time.sleep(0.35)
    try:
        server.backend.close()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass
    argv = [sys.executable, *sys.argv]
    os.execv(sys.executable, argv)


def _detect_local_ipv4_addresses() -> List[str]:
    addresses: set[str] = set()
    hostname = socket.gethostname()
    try:
        for info in socket.getaddrinfo(hostname, None, family=socket.AF_INET, type=socket.SOCK_STREAM):
            ip = info[4][0]
            if not ip.startswith("127."):
                addresses.add(ip)
    except socket.gaierror:
        pass
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        if ip and not ip.startswith("127."):
            addresses.add(ip)
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(addresses)


def _startup_urls(host: str, port: int, alias: str) -> List[str]:
    urls: List[str] = [f"http://127.0.0.1:{port}/"]
    bind_host = str(host).strip()
    if bind_host not in {"", "0.0.0.0", "::"}:
        urls.append(f"http://{bind_host}:{port}/")
    else:
        for ip in _detect_local_ipv4_addresses():
            urls.append(f"http://{ip}:{port}/")
    if alias:
        alias = alias.strip()
        if alias:
            urls.append(f"http://{alias}.local:{port}/")
    deduped: List[str] = []
    seen: set[str] = set()
    for url in urls:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped


def _print_startup_banner(host: str, port: int, alias: str) -> None:
    hostname = socket.gethostname()
    print("Raspiaudio radio server started")
    print("Open one of these URLs:")
    for url in _startup_urls(host, port, alias):
        print(f"  {url}")
    if alias:
        print(f"Suggested network alias: {alias}")
        print(f"  If your hostname or mDNS alias is set to `{alias}`, try http://{alias}.local:{port}/")
    print(f"Current host name: {hostname}")


def run_server(config: RadioConfig, host: str, port: int, alias: str = "piradio") -> None:
    backend = RadioBackend(config)
    httpd = RadioHTTPServer((host, port), backend)
    try:
        _print_startup_banner(host, port, alias)
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        backend.close()
        httpd.server_close()
