from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from typing import Any, Dict, List
from urllib.parse import parse_qs, quote, unquote, urlparse

from .backend import RadioBackend, RadioConfig

STATIC_DIR = Path(__file__).resolve().parent / "static"
ICY_META_INTERVAL = 16 * 1024
ICY_METADATA_POLL_INTERVAL_S = 1.0
_ARECORD_AVAILABLE_FORMAT_RE = re.compile(r"^\s*-\s*(?P<format>[A-Z0-9_]+)\s*$")


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
        if parsed.path == "/api/station-streams":
            self._serve_station_streams(query=query, send_body=send_body)
            return
        if parsed.path == "/api/live-metadata":
            self._serve_live_metadata(send_body=send_body)
            return
        if parsed.path == "/api/recordings":
            recordings = self.server.backend.get_recordings()
            self._send_ok({"recordings": recordings, "count": len(recordings)}, send_body=send_body)
            return
        if parsed.path == "/api/dab/artwork":
            self._serve_dab_artwork(send_body=send_body)
            return
        if parsed.path == "/audio/live.mp3":
            station_id = query.get("station_id", [None])[0]
            self._serve_live_audio(send_body=send_body, station_id=station_id, query=query)
            return
        if parsed.path.startswith("/audio/stations/") and parsed.path.endswith(".mp3"):
            self._serve_station_audio(parsed.path, query=query, send_body=send_body)
            return
        if parsed.path.startswith("/playlists/") and parsed.path.endswith(".m3u"):
            self._serve_m3u_playlist(parsed.path, send_body=send_body)
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

    def _request_base_url(self) -> str:
        host = (self.headers.get("Host") or "").strip()
        if host:
            return f"http://{host}"
        bind_host, port = self.server.server_address[:2]
        host_name = str(bind_host or "127.0.0.1")
        if host_name in {"0.0.0.0", "::"}:
            host_name = "127.0.0.1"
        return f"http://{host_name}:{port}"

    def _absolute_url(self, path: str) -> str:
        return f"{self._request_base_url()}{path if path.startswith('/') else '/' + path}"

    def _station_stream_path(self, station_id: str, *, icy_metadata: bool = False) -> str:
        path = f"/audio/stations/{quote(str(station_id), safe='')}.mp3"
        if icy_metadata:
            return f"{path}?icy=1"
        return path

    @staticmethod
    def _truthy_token(value: Any) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _falsey_token(value: Any) -> bool:
        return str(value or "").strip().lower() in {"0", "false", "no", "off"}

    def _client_wants_icy_metadata(self, query: Dict[str, List[str]] | None = None) -> bool:
        query = query or {}
        for key in ("icy", "metadata", "icy-metadata"):
            if key not in query:
                continue
            value = (query.get(key) or [""])[0]
            if self._falsey_token(value):
                return False
            return self._truthy_token(value)
        return self._truthy_token(self.headers.get("Icy-MetaData") or self.headers.get("icy-metadata"))

    def _playlist_path(self, mode: str | None = None, favorites_only: bool = False) -> str:
        if favorites_only:
            return "/playlists/favorites.m3u"
        playlist_mode = str(mode or "dab").strip().lower().replace("-", "_")
        return f"/playlists/{playlist_mode}.m3u"

    def _station_stream_entries(self, mode: str | None = None, favorites_only: bool = False) -> List[Dict[str, Any]]:
        stations = self.server.backend.get_favorites() if favorites_only else self.server.backend.get_stations(mode=mode)
        items: List[Dict[str, Any]] = []
        for station in stations:
            item = dict(station)
            item["stream_path"] = self._station_stream_path(item["station_id"])
            item["stream_url"] = self._absolute_url(item["stream_path"])
            item["metadata_stream_path"] = self._station_stream_path(item["station_id"], icy_metadata=True)
            item["metadata_stream_url"] = self._absolute_url(item["metadata_stream_path"])
            items.append(item)
        return items

    def _serve_station_streams(self, query: Dict[str, List[str]], send_body: bool = True) -> None:
        favorites_only = query.get("favorites", ["0"])[0].lower() in {"1", "true", "yes"}
        mode = query.get("mode", [None])[0]
        items = self._station_stream_entries(mode=mode, favorites_only=favorites_only)
        resolved_mode = "favorites" if favorites_only else str(mode or self.server.backend.get_status()["mode"])
        playlist_path = self._playlist_path(mode=mode, favorites_only=favorites_only)
        self._send_ok(
            {
                "mode": resolved_mode,
                "favorites": favorites_only,
                "count": len(items),
                "single_tuner": True,
                "playlist_path": playlist_path,
                "playlist_url": self._absolute_url(playlist_path),
                "stations": items,
            },
            send_body=send_body,
        )

    def _serve_live_metadata(self, send_body: bool = True) -> None:
        payload = self.server.backend.get_live_stream_metadata()
        dab_media = dict(payload.get("dab_media") or {})
        artwork_url = dab_media.get("artwork_url")
        if artwork_url:
            dab_media["artwork_url"] = self._absolute_url(str(artwork_url))
        payload["dab_media"] = dab_media
        self._send_ok(payload, send_body=send_body)

    def _serve_station_audio(
        self,
        request_path: str,
        query: Dict[str, List[str]] | None = None,
        send_body: bool = True,
    ) -> None:
        encoded_station = Path(request_path).name
        if not encoded_station.endswith(".mp3"):
            self._send_error_json(404, "Station stream not found.", send_body=send_body)
            return
        station_id = unquote(encoded_station[:-4])
        if not station_id:
            self._send_error_json(400, "station_id is required.", send_body=send_body)
            return
        self._serve_live_audio(send_body=send_body, station_id=station_id, auto_tune=True, query=query)

    def _serve_m3u_playlist(self, request_path: str, send_body: bool = True) -> None:
        playlist_name = Path(request_path).name
        if playlist_name == "favorites.m3u":
            favorites_only = True
            mode = None
            title = "Raspiaudio Favorites"
        else:
            favorites_only = False
            mode = playlist_name[:-4].strip().lower().replace("-", "_")
            if mode not in {"dab", "fm", "hd", "am", "am_hd"}:
                self._send_error_json(404, "Playlist not found.", send_body=send_body)
                return
            title = f"Raspiaudio {mode.upper()}"
        stations = self._station_stream_entries(mode=mode, favorites_only=favorites_only)
        lines = [
            "#EXTM3U",
            f"#PLAYLIST:{title}",
            "# Raspiaudio uses a single tuner: starting a different station retunes the hardware.",
        ]
        for station in stations:
            group_title = "Raspiaudio Favorites" if favorites_only else f"Raspiaudio {station['mode_label']}"
            station_id = str(station["station_id"]).replace('"', "'")
            station_label = str(station["label"]).replace("\n", " ").strip()
            lines.append(f'#EXTINF:-1 tvg-id="{station_id}" group-title="{group_title}",{station_label}')
            lines.append(station["metadata_stream_url"])
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        self._serve_bytes(
            payload,
            "audio/x-mpegurl; charset=utf-8",
            cache_control="no-store",
            send_body=send_body,
        )

    def _compact_live_text(self, value: Any) -> str:
        return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())

    def _sanitize_icy_value(self, value: Any, limit: int = 512) -> str:
        text = self._compact_live_text(value)
        text = text.replace("\\", "/").replace("'", '"')
        if len(text) > limit:
            text = text[:limit].rstrip()
        return text

    def _build_icy_payload(self, metadata: Dict[str, Any], station_header: str) -> bytes:
        dab_media = metadata.get("dab_media") or {}
        stream_text = self._sanitize_icy_value(dab_media.get("text") or "")
        title = self._sanitize_icy_value(dab_media.get("title") or "")
        artist = self._sanitize_icy_value(dab_media.get("artist") or "")
        stream_title = ""
        if stream_text:
            stream_title = stream_text
        elif artist and title:
            stream_title = f"{artist} - {title}"
        elif title:
            stream_title = title
        elif artist:
            stream_title = artist
        else:
            stream_title = station_header
        artwork_url = dab_media.get("artwork_url")
        if artwork_url:
            artwork_url = self._absolute_url(str(artwork_url))
        parts = [f"StreamTitle='{stream_title}';"]
        if artist:
            parts.append(f"StreamArtist='{artist}';")
        if title:
            parts.append(f"StreamSong='{title}';")
        if artwork_url:
            safe_artwork_url = self._sanitize_icy_value(artwork_url, limit=1024)
            parts.append(f"StreamUrl='{safe_artwork_url}';")
            parts.append(f"StreamArtwork='{safe_artwork_url}';")
        payload = "".join(parts).encode("utf-8", errors="ignore")
        if len(payload) > (255 * 16):
            payload = payload[: 255 * 16]
        block_length = (len(payload) + 15) // 16
        if block_length <= 0:
            return b"\x00"
        padded = payload.ljust(block_length * 16, b"\x00")
        return bytes([block_length]) + padded

    def _send_live_stream_headers(
        self,
        station_header: str,
        station_id: str,
        *,
        icy_enabled: bool,
        icy_logo_url: str | None = None,
    ) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Radio-Station", station_header)
        self.send_header("X-Radio-Station-Id", station_id)
        self.send_header("X-Radio-Single-Tuner", "true")
        if icy_enabled:
            self.send_header("icy-name", station_header)
            self.send_header("icy-description", "Raspiaudio Digital Radio live stream")
            self.send_header("icy-br", "192")
            self.send_header("icy-metaint", str(ICY_META_INTERVAL))
            if icy_logo_url:
                self.send_header("icy-logo", icy_logo_url)
        self.end_headers()

    def _serve_live_audio(
        self,
        send_body: bool = True,
        station_id: str | None = None,
        auto_tune: bool | None = None,
        query: Dict[str, List[str]] | None = None,
    ) -> None:
        try:
            stream_info = self.server.backend.prepare_live_stream(
                station_id=station_id,
                auto_tune=((station_id is not None) if auto_tune is None else auto_tune),
            )
        except RuntimeError as exc:
            self._send_error_json(503, str(exc), send_body=send_body)
            return
        except ValueError as exc:
            self._send_error_json(400, str(exc), send_body=send_body)
            return
        icy_enabled = self._client_wants_icy_metadata(query)
        station_header = str(stream_info["station_label"]).encode("latin-1", errors="ignore").decode("latin-1") or "live"
        station_id_header = str(stream_info["station_id"]).encode("latin-1", errors="ignore").decode("latin-1")
        initial_metadata = self.server.backend.get_live_stream_metadata()
        initial_artwork_url = initial_metadata.get("dab_media", {}).get("artwork_url")
        if initial_artwork_url:
            initial_artwork_url = self._absolute_url(str(initial_artwork_url))

        if not send_body:
            self._send_live_stream_headers(
                station_header,
                station_id_header,
                icy_enabled=icy_enabled,
                icy_logo_url=str(initial_artwork_url) if initial_artwork_url else None,
            )
            return

        def _start_processes(capture_format: str) -> tuple[subprocess.Popen[bytes], subprocess.Popen[bytes]]:
            arecord_command = [
                "arecord",
                "-q",
                "-D",
                str(stream_info["device"]),
                "-f",
                capture_format,
                "-r",
                str(stream_info["sample_rate"]),
                "-c",
                str(stream_info["channels"]),
                "-t",
                "raw",
            ]
            ffmpeg_command = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-thread_queue_size",
                "1024",
                "-f",
                capture_format.replace("_", "").lower(),
                "-ar",
                str(stream_info["sample_rate"]),
                "-ac",
                str(stream_info["channels"]),
                "-i",
                "pipe:0",
                "-vn",
                "-acodec",
                "libmp3lame",
                "-b:a",
                "192k",
                "-f",
                "mp3",
                "-",
            ]
            capture_process = subprocess.Popen(  # noqa: S603
                arecord_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            process = subprocess.Popen(  # noqa: S603
                ffmpeg_command,
                stdin=capture_process.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if capture_process.stdout is not None:
                capture_process.stdout.close()
            return capture_process, process

        def _collect_startup_error(
            capture_process: subprocess.Popen[bytes], process: subprocess.Popen[bytes]
        ) -> tuple[bytes, bytes]:
            time.sleep(0.25)
            if capture_process.poll() is None and process.poll() is None:
                return b"", b""
            capture_stderr = b""
            ffmpeg_stderr = b""
            with contextlib.suppress(Exception):
                _, capture_stderr = capture_process.communicate(timeout=0.5)
            with contextlib.suppress(Exception):
                _, ffmpeg_stderr = process.communicate(timeout=0.5)
            return capture_stderr, ffmpeg_stderr

        def _extract_retry_format(capture_stderr: bytes, current_format: str) -> str | None:
            seen_formats = False
            for line in capture_stderr.decode("utf-8", errors="ignore").splitlines():
                if "Available formats:" in line:
                    seen_formats = True
                    continue
                if not seen_formats:
                    continue
                match = _ARECORD_AVAILABLE_FORMAT_RE.match(line)
                if match is None:
                    if line.strip():
                        break
                    continue
                candidate = match.group("format")
                if candidate and candidate != current_format:
                    return candidate
            return None

        capture_format = str(stream_info["format"])
        capture_process, process = _start_processes(capture_format)
        capture_stderr, stderr = _collect_startup_error(capture_process, process)
        if capture_stderr or stderr:
            retry_format = _extract_retry_format(capture_stderr, capture_format)
            if retry_format:
                capture_format = retry_format
                capture_process, process = _start_processes(capture_format)
                capture_stderr, stderr = _collect_startup_error(capture_process, process)
        if capture_stderr or stderr:
            self._send_error_json(
                503,
                "Live stream failed to start on ALSA device "
                f"{stream_info['device']}. "
                + (
                    capture_stderr.decode("utf-8", errors="ignore").strip()
                    or stderr.decode("utf-8", errors="ignore").strip()
                    or "Check the I2S capture path."
                ),
                send_body=True,
            )
            return

        self._send_live_stream_headers(
            station_header,
            station_id_header,
            icy_enabled=icy_enabled,
            icy_logo_url=str(initial_artwork_url) if initial_artwork_url else None,
        )
        try:
            assert process.stdout is not None
            metadata_block = self._build_icy_payload(initial_metadata, station_header)
            last_metadata_poll = time.monotonic()
            bytes_until_metadata = ICY_META_INTERVAL
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                if not icy_enabled:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    continue
                offset = 0
                while offset < len(chunk):
                    write_size = min(bytes_until_metadata, len(chunk) - offset)
                    if write_size > 0:
                        self.wfile.write(chunk[offset : offset + write_size])
                        offset += write_size
                        bytes_until_metadata -= write_size
                    if bytes_until_metadata == 0:
                        now = time.monotonic()
                        if (now - last_metadata_poll) >= ICY_METADATA_POLL_INTERVAL_S:
                            metadata_block = self._build_icy_payload(
                                self.server.backend.get_live_stream_metadata(),
                                station_header,
                            )
                            last_metadata_poll = now
                        self.wfile.write(metadata_block)
                        bytes_until_metadata = ICY_META_INTERVAL
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        finally:
            if capture_process.poll() is None:
                capture_process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    capture_process.communicate(timeout=2.0)
                if capture_process.poll() is None:
                    capture_process.kill()
                    with contextlib.suppress(Exception):
                        capture_process.communicate(timeout=1.0)
            if process.poll() is None:
                process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.communicate(timeout=2.0)
                if process.poll() is None:
                    process.kill()
                    with contextlib.suppress(Exception):
                        process.communicate(timeout=1.0)
            with contextlib.suppress(Exception):
                if process.stdout is not None:
                    process.stdout.close()
            with contextlib.suppress(Exception):
                if process.stderr is not None:
                    process.stderr.close()
            with contextlib.suppress(Exception):
                if capture_process.stderr is not None:
                    capture_process.stderr.close()

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
    print("Live MPEG stream:")
    for url in _startup_urls(host, port, alias):
        print(f"  {url.rstrip('/')}/audio/live.mp3")
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
