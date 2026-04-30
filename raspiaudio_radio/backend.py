from __future__ import annotations

import atexit
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from legacy.dab_radio_i2c_safe2 import (
    DAB_BAND_III,
    FLASH_ADDR_DAB,
    FLASH_ADDR_PATCH_FULL,
    FLASH_SECTOR_SIZE,
    FLASH_WRITE_BLOCK,
    PROP_AUDIO_MUTE,
    Si468xDabRadio,
    load_scan_file,
)

try:
    import RPi.GPIO as GPIO  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    GPIO = None
    _GPIO_IMPORT_ERROR = exc
else:
    _GPIO_IMPORT_ERROR = None

try:
    from smbus2 import SMBus, i2c_msg  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    SMBus = None
    i2c_msg = None
    _SMBUS_IMPORT_ERROR = exc
else:
    _SMBUS_IMPORT_ERROR = None

try:
    from PIL import Image, ImageDraw, ImageFont  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    Image = None
    ImageDraw = None
    ImageFont = None
    _PIL_IMPORT_ERROR = exc
else:
    _PIL_IMPORT_ERROR = None

MODE_DEFS: Dict[str, Dict[str, Any]] = {
    "dab": {"id": "dab", "label": "DAB", "band": "dab", "firmware": "dab", "scan_key": "dab", "tune_mode": None},
    "fm": {"id": "fm", "label": "FM", "band": "fm", "firmware": "fmhd", "scan_key": "fm", "tune_mode": 0},
    "hd": {"id": "hd", "label": "HD Radio", "band": "fm", "firmware": "fmhd", "scan_key": "hd", "tune_mode": 3},
    "am": {"id": "am", "label": "AM", "band": "am", "firmware": "amhd", "scan_key": "am", "tune_mode": 0},
    "am_hd": {"id": "am_hd", "label": "AM HD", "band": "am", "firmware": "amhd", "scan_key": "am_hd", "tune_mode": 2},
}
SCAN_KEYS = ("dab", "fm", "hd", "am", "am_hd")
VALID_AUDIO_OUT = {"analog", "i2s", "both"}
SCAN_FILE_HEADER = "Automatically generated and machine read file, do not change!\n"
FLASH_ADDR_FMHD = 0x00006000
FLASH_ADDR_AMHD = 0x0011E000
DAB_DATA_SRC_PAD_DLS = 0x02
DAB_DATA_SRC_PAD_DATA = 0x01
DAB_DSCTY_MOT = 60
DAB_MOT_HEADER_PACKET = 0x73
DAB_MOT_BODY_PACKET = 0x74
MAX_DAB_MOT_OBJECTS = 8
MAX_DAB_MOT_AGE_S = 45.0
_MOT_IMAGE_NAME_RE = re.compile(rb"([A-Za-z0-9_.-]+\.(?:jpe?g|png|gif|bmp))", re.IGNORECASE)
_ARECORD_CARD_RE = re.compile(r"^card\s+\d+:\s+(?P<card>[^\s]+)\s+\[.*?\],\s+device\s+(?P<device>\d+):")
_ALSA_DEVICE_CARD_RE = re.compile(r"(?:^|:)CARD=(?P<card>[^,]+)")
_AUTO_RECORD_DEVICE_NAMES = (
    "plughw:CARD=si4689i2s,DEV=0",
    "plughw:CARD=si4689_i2s,DEV=0",
    "sysdefault:CARD=si4689i2s",
    "sysdefault:CARD=si4689_i2s",
    "hw:CARD=si4689i2s,DEV=0",
    "hw:CARD=si4689_i2s,DEV=0",
)
_AUTO_RECORD_DEVICE_HINTS = ("si4689", "adau7002", "i2s")
OLED_LINE_WIDTH = 12


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _dab_score(status: Dict[str, Any]) -> int:
    ficq = _clamp_int(int(status.get("fic_quality", 0)), 0, 100)
    cnr = _clamp_int(int(status.get("cnr", 0)), 0, 30)
    rssi = _clamp_int(int(status.get("rssi", -120)), -120, 20)
    return _clamp_int(int(round(ficq * 0.5 + cnr * 3.5 + (rssi + 120) * 0.1)), 0, 100)


def _analog_score(status: Dict[str, Any]) -> int:
    snr = _clamp_int(int(status.get("snr", 0)), 0, 50)
    rssi = _clamp_int(int(status.get("rssi", 0)), -10, 75)
    bonus = 15 if status.get("hd_detected") or status.get("digital_source") else 0
    return _clamp_int(int(round((snr * 1.2) + ((rssi + 10) * 0.7) + bonus)), 0, 100)


def _station_sort_key(station: Dict[str, Any]) -> tuple[str, int, str]:
    band = str(station.get("band", "")).casefold()
    label = str(station.get("label", "")).casefold()
    freq_khz = int(station.get("freq_khz") or 0)
    station_id = str(station.get("station_id", ""))
    if band in {"fm", "am"}:
        return (band, freq_khz, f"{label}\t{station_id}")
    return (band, 0, f"{label}\t{freq_khz}\t{station_id}")


def _sanitize_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_")[:64] or "recording"


def _iso_or_none(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


def _compact_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())


def _truncate_text(value: Any, width: int) -> str:
    text = _compact_text(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def _marquee_text(value: Any, width: int, tick: int) -> str:
    text = _compact_text(value)
    if len(text) <= width:
        return text
    spacer = "   "
    loop = text + spacer
    offset = tick % len(loop)
    repeated = loop + loop
    return repeated[offset: offset + width]


def _empty_dab_media() -> Dict[str, Any]:
    return {
        "text": "",
        "title": None,
        "artist": None,
        "encoding": None,
        "toggle": None,
        "updated_at": None,
        "artwork_url": None,
        "artwork_supported": False,
    }


def _guess_image_content_type(filename: Optional[str]) -> Optional[str]:
    if not filename:
        return None
    content_type, _ = mimetypes.guess_type(filename)
    if content_type and content_type.startswith("image/"):
        return content_type
    return None


def _extract_mot_filename(header_bytes: bytes) -> Optional[str]:
    match = _MOT_IMAGE_NAME_RE.search(bytes(header_bytes or b""))
    if match is None:
        return None
    return match.group(1).decode("ascii", errors="ignore")


def _join_mot_segments(segments: Dict[int, bytes], last_index: Optional[int]) -> Optional[bytes]:
    if last_index is None:
        return None
    if any(index not in segments for index in range(last_index + 1)):
        return None
    return b"".join(segments[index] for index in range(last_index + 1))


def _extract_image_payload(payload: bytes, filename: Optional[str]) -> tuple[Optional[bytes], Optional[str]]:
    blob = bytes(payload or b"")
    for signature, content_type in ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png")):
        offset = blob.find(signature)
        if offset >= 0:
            image = blob[offset:]
            if content_type == "image/jpeg":
                end = image.rfind(b"\xff\xd9")
                if end >= 0:
                    image = image[: end + 2]
            return image, content_type
    if filename:
        guessed = _guess_image_content_type(filename)
        if guessed and blob:
            return blob, guessed
    return None, None


def _parse_mot_segment(payload: bytes) -> Optional[Dict[str, Any]]:
    blob = bytes(payload or b"")
    if len(blob) < 11:
        return None
    chunk_length = int.from_bytes(blob[7:9], "big")
    chunk_end = 9 + chunk_length
    if chunk_end + 2 > len(blob):
        return None
    segment_raw = int.from_bytes(blob[2:4], "big")
    return {
        "packet_type": blob[0],
        "segment_index": segment_raw & 0x7FFF,
        "is_last": bool(segment_raw & 0x8000),
        "object_id": int.from_bytes(blob[4:7], "big"),
        "chunk": blob[9:chunk_end],
    }


def _decode_dab_text(payload: bytes, encoding: Optional[int]) -> str:
    raw = bytes(payload or b"").replace(b"\x00", b" ").strip()
    if not raw:
        return ""
    enc = int(encoding) if encoding is not None else -1
    candidates = ["latin-1"]
    if enc in {6, 4}:
        candidates = ["utf-16-be", "utf-16-le", "utf-8", "latin-1"]
    elif enc == 0x0F:
        candidates = ["utf-8", "latin-1"]
    for codec in candidates:
        try:
            text = raw.decode(codec, errors="strict")
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", errors="replace")
    return " ".join(text.replace("\r", " ").replace("\n", " ").split())


def _infer_artist_title(text: str) -> tuple[Optional[str], Optional[str]]:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return None, None
    for separator in (" - ", " – ", " | ", " / "):
        if separator in cleaned:
            left, right = [part.strip() for part in cleaned.split(separator, 1)]
            if left and right:
                return left, right
    match = re.match(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+)$", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group("artist").strip(), match.group("title").strip()
    return None, None


def _list_arecord_named_devices() -> List[str]:
    try:
        result = subprocess.run(
            ["arecord", "-L"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    devices: List[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or raw_line[:1].isspace():
            continue
        devices.append(line)
    return devices


def _list_arecord_capture_hardware() -> List[str]:
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return []
    devices: List[str] = []
    for raw_line in result.stdout.splitlines():
        match = _ARECORD_CARD_RE.match(raw_line.strip())
        if match is None:
            continue
        devices.append(f"plughw:CARD={match.group('card')},DEV={match.group('device')}")
    return devices


def _auto_detect_record_device() -> str:
    named_devices = _list_arecord_named_devices()
    for candidate in _AUTO_RECORD_DEVICE_NAMES:
        if candidate in named_devices:
            return candidate
    for candidate in named_devices:
        lowered = candidate.casefold()
        if any(hint in lowered for hint in _AUTO_RECORD_DEVICE_HINTS):
            return candidate
    for candidate in _list_arecord_capture_hardware():
        lowered = candidate.casefold()
        if any(hint in lowered for hint in _AUTO_RECORD_DEVICE_HINTS):
            return candidate
    return "default"


def _extract_alsa_card_name(device: str) -> Optional[str]:
    match = _ALSA_DEVICE_CARD_RE.search(str(device or ""))
    if match is None:
        return None
    return match.group("card")


def _resolve_shared_capture_device(device: str) -> str:
    named_devices = _list_arecord_named_devices()
    configured = str(device or "").strip()
    if configured in named_devices and configured.startswith("dsnoop:"):
        return configured

    card_name = _extract_alsa_card_name(configured)
    if card_name:
        prefix = f"dsnoop:CARD={card_name},DEV="
        for candidate in named_devices:
            if candidate.startswith(prefix):
                return candidate

    hinted_dsnoop_devices = [
        candidate
        for candidate in named_devices
        if candidate.startswith("dsnoop:")
        and any(hint in candidate.casefold() for hint in _AUTO_RECORD_DEVICE_HINTS)
    ]
    if hinted_dsnoop_devices:
        return hinted_dsnoop_devices[0]

    dsnoop_devices = [candidate for candidate in named_devices if candidate.startswith("dsnoop:")]
    if len(dsnoop_devices) == 1:
        return dsnoop_devices[0]

    return configured or "default"


def _wav_duration_seconds(file_path: Path) -> Optional[float]:
    try:
        with wave.open(str(file_path), "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            if rate <= 0:
                return None
            return round(frames / rate, 1)
    except (FileNotFoundError, wave.Error, OSError):
        return None


def _trim_wav_leading_seconds(file_path: Path, trim_seconds: float) -> float:
    trim_seconds = max(0.0, float(trim_seconds))
    if trim_seconds <= 0.0 or not file_path.exists():
        return 0.0
    try:
        with wave.open(str(file_path), "rb") as source:
            params = source.getparams()
            rate = source.getframerate()
            if rate <= 0:
                return 0.0
            trim_frames = int(round(trim_seconds * rate))
            total_frames = source.getnframes()
            if trim_frames <= 0 or total_frames <= trim_frames:
                return 0.0
            source.setpos(trim_frames)
            remaining = source.readframes(total_frames - trim_frames)
    except (wave.Error, OSError):
        return 0.0
    temp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with wave.open(str(temp_path), "wb") as target:
        target.setparams(params)
        target.writeframes(remaining)
    temp_path.replace(file_path)
    return round(trim_frames / rate, 1)


@dataclass(frozen=True)
class RadioConfig:
    patch_path: Path
    mini_patch_path: Path
    dab_firmware_path: Path
    fmhd_firmware_path: Path
    amhd_firmware_path: Path
    dab_scan_file: Path
    fm_scan_file: Path
    hd_scan_file: Path
    am_scan_file: Path
    am_hd_scan_file: Path
    favorites_file: Path
    recordings_dir: Path
    runtime_state_file: Path
    i2c_bus: int = 1
    i2c_addr: int = 0x64
    spi_bus: int = 0
    spi_dev: int = 0
    spi_speed_hz: int = 30_000_000
    flash_program_spi_hz: int = 1_000_000
    rst_pin: int = 25
    int_pin: Optional[int] = None
    amp_pin: Optional[int] = 17
    amp_active_high: bool = True
    nav_cw_pin: Optional[int] = 5
    nav_push_pin: Optional[int] = 6
    nav_ccw_pin: Optional[int] = 13
    nav_active_low: bool = True
    nav_debounce_ms: int = 80
    nav_combo_window_s: float = 0.7
    nav_station_timeout_s: float = 1.5
    nav_poll_interval_s: float = 0.02
    oled_enabled: bool = True
    oled_i2c_bus: int = 1
    oled_i2c_addr: int = 0x3C
    oled_update_interval_s: float = 0.35
    audio_out: str = "both"
    i2s_master: bool = False
    sample_rate: int = 48_000
    sample_size: int = 16
    xtal_freq: int = 19_200_000
    ctun: int = 0x07
    antcap: int = 0
    lock_ms: int = 5000
    flash_patch_addr: int = FLASH_ADDR_PATCH_FULL
    flash_dab_addr: int = FLASH_ADDR_DAB
    flash_fmhd_addr: int = FLASH_ADDR_FMHD
    flash_amhd_addr: int = FLASH_ADDR_AMHD
    default_volume: int = 40
    default_mode: str = "dab"
    fm_min_khz: int = 87_500
    fm_max_khz: int = 108_000
    fm_step_khz: int = 100
    fm_rssi_min: int = 20
    fm_snr_min: int = 10
    fm_hd_timeout_ms: int = 2500
    am_min_khz: int = 531
    am_max_khz: int = 1710
    am_step_khz: int = 9
    am_rssi_min: int = 20
    am_snr_min: int = 4
    am_peak_rssi_min: int = 10
    am_peak_prominence: float = 5.0
    am_peak_window_channels: int = 2
    am_hd_timeout_ms: int = 2500
    record_device: str = "auto"
    record_channels: int = 2
    record_format: str = "S16_LE"
    record_trim_leading_seconds: float = 3.0
    system_service_name: str = "raspiaudio-radio.service"


class AmplifierGate:
    def __init__(self, pin: Optional[int], active_high: bool) -> None:
        self.pin = pin
        self.active_high = active_high
        self.enabled = False
        self._ready = False

    def set_enabled(self, enabled: bool) -> bool:
        if self.pin is None:
            self.enabled = False
            return False
        if GPIO is None:
            raise RuntimeError(
                "RPi.GPIO is required to control the amplifier GPIO. "
                f"Import failed with: {_GPIO_IMPORT_ERROR}"
            )
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        if not self._ready:
            inactive = GPIO.LOW if self.active_high else GPIO.HIGH
            GPIO.setup(self.pin, GPIO.OUT, initial=inactive)
            self._ready = True
        active = GPIO.HIGH if self.active_high else GPIO.LOW
        inactive = GPIO.LOW if self.active_high else GPIO.HIGH
        GPIO.output(self.pin, active if enabled else inactive)
        self.enabled = bool(enabled)
        return self.enabled

    def close(self) -> None:
        try:
            self.set_enabled(False)
        except Exception:
            pass
        if self.pin is not None and GPIO is not None and self._ready:
            try:
                GPIO.cleanup(self.pin)
            except Exception:
                pass
        self._ready = False
        self.enabled = False


class ButtonNavigator:
    def __init__(
        self,
        cw_pin: Optional[int],
        push_pin: Optional[int],
        ccw_pin: Optional[int],
        *,
        active_low: bool,
        debounce_ms: int,
        poll_interval_s: float,
        on_event: Callable[[str, float], None],
    ) -> None:
        self.cw_pin = cw_pin
        self.push_pin = push_pin
        self.ccw_pin = ccw_pin
        self.active_low = bool(active_low)
        self.debounce_s = max(0.01, float(debounce_ms) / 1000.0)
        self.poll_interval_s = max(0.01, float(poll_interval_s))
        self._on_event = on_event
        self._pins = {"cw": self.cw_pin, "push": self.push_pin, "ccw": self.ccw_pin}
        self.enabled = False
        self._ready = False
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pressed: Dict[str, bool] = {name: False for name in self._pins}
        self._next_event_at: Dict[str, float] = {name: 0.0 for name in self._pins}

    def start(self) -> bool:
        if not all(pin is not None for pin in self._pins.values()):
            self.enabled = False
            return False
        if GPIO is None:
            self.enabled = False
            return False
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        pull = GPIO.PUD_UP if self.active_low else GPIO.PUD_DOWN
        for name, pin in self._pins.items():
            assert pin is not None
            GPIO.setup(pin, GPIO.IN, pull_up_down=pull)
            self._pressed[name] = self._is_active(pin)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="button-nav", daemon=True)
        self._thread.start()
        self._ready = True
        self.enabled = True
        return True

    def _is_active(self, pin: int) -> bool:
        level = GPIO.input(pin) if GPIO is not None else 0
        return level == (GPIO.LOW if self.active_low else GPIO.HIGH)

    def _run(self) -> None:
        while not self._stop.wait(self.poll_interval_s):
            now = time.monotonic()
            for name, pin in self._pins.items():
                if pin is None:
                    continue
                active = self._is_active(pin)
                if active and not self._pressed[name] and now >= self._next_event_at[name]:
                    self._next_event_at[name] = now + self.debounce_s
                    try:
                        self._on_event(name, now)
                    except Exception:
                        pass
                self._pressed[name] = active

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if self._ready and GPIO is not None:
            for pin in self._pins.values():
                if pin is None:
                    continue
                try:
                    GPIO.cleanup(pin)
                except Exception:
                    pass
        self._thread = None
        self._ready = False
        self.enabled = False


class OledStatusDisplay:
    def __init__(
        self,
        *,
        enabled: bool,
        bus_num: int,
        address: int,
        update_interval_s: float,
        status_supplier: Callable[[], Dict[str, Any]],
    ) -> None:
        self._requested = bool(enabled)
        self.bus_num = int(bus_num)
        self.address = int(address)
        self.update_interval_s = max(0.2, float(update_interval_s))
        self._status_supplier = status_supplier
        self._bus: Optional[SMBus] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_lines: Optional[tuple[str, str]] = None
        self._font_primary = None
        self._font_secondary = None
        self.error: Optional[str] = None
        self.enabled = False

    def start(self) -> bool:
        if not self._requested:
            return False
        if SMBus is None or i2c_msg is None:
            self.error = f"smbus2 unavailable: {_SMBUS_IMPORT_ERROR}"
            return False
        if Image is None or ImageDraw is None or ImageFont is None:
            self.error = f"Pillow unavailable: {_PIL_IMPORT_ERROR}"
            return False
        self._font_primary, self._font_secondary = self._load_fonts()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="oled-status", daemon=True)
        self._thread.start()
        try:
            self._ensure_ready()
        except Exception as exc:
            self.error = str(exc)
        return True

    def _ensure_ready(self) -> None:
        if self.enabled:
            return
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                if self._bus is None:
                    self._bus = SMBus(self.bus_num)
                self._init_display()
                self._clear()
                self.enabled = True
                self.error = None
                return
            except Exception as exc:
                last_error = exc
                try:
                    if self._bus is not None:
                        self._bus.close()
                except Exception:
                    pass
                self._bus = None
                time.sleep(0.15)
        raise last_error or RuntimeError("OLED init failed")

    def _load_fonts(self) -> tuple[Any, Any]:
        assert ImageFont is not None
        font_candidates = (
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        )
        for candidate in font_candidates:
            try:
                primary = ImageFont.truetype(candidate, 16)
                secondary = ImageFont.truetype(candidate, 16)
                return primary, secondary
            except Exception:
                continue
        fallback = ImageFont.load_default()
        return fallback, fallback

    def _write_command(self, *values: int) -> None:
        if self._bus is None or i2c_msg is None:
            raise RuntimeError("OLED bus is not initialized.")
        payload = bytes([0x00, *[int(value) & 0xFF for value in values]])
        last_error: Optional[Exception] = None
        for _ in range(3):
            try:
                self._bus.i2c_rdwr(i2c_msg.write(self.address, payload))
                return
            except Exception as exc:
                last_error = exc
                time.sleep(0.03)
        raise last_error or RuntimeError("OLED command write failed")

    def _write_data(self, data: bytes) -> None:
        if self._bus is None or i2c_msg is None:
            raise RuntimeError("OLED bus is not initialized.")
        blob = bytes(data or b"")
        for start in range(0, len(blob), 16):
            chunk = blob[start:start + 16]
            last_error: Optional[Exception] = None
            for _ in range(3):
                try:
                    self._bus.i2c_rdwr(i2c_msg.write(self.address, bytes([0x40]) + chunk))
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    time.sleep(0.03)
            if last_error is not None:
                raise last_error

    def _init_display(self) -> None:
        self._write_command(
            0xAE,
            0xD5, 0x80,
            0xA8, 0x1F,
            0xD3, 0x00,
            0x40,
            0x8D, 0x14,
            0x20, 0x00,
            0xA1,
            0xC8,
            0xDA, 0x02,
            0x81, 0x8F,
            0xD9, 0xF1,
            0xDB, 0x40,
            0xA4,
            0xA6,
            0x2E,
            0xAF,
        )

    def _clear(self) -> None:
        self._show_buffer(bytes([0x00] * (128 * 4)))

    def _show_buffer(self, buffer: bytes) -> None:
        self._write_command(0x21, 0x00, 0x7F, 0x22, 0x00, 0x03)
        self._write_data(buffer)

    def _image_to_buffer(self, image: Any) -> bytes:
        pixels = image.load()
        buffer = bytearray(128 * 4)
        for page in range(4):
            for x in range(128):
                value = 0
                for bit in range(8):
                    if pixels[x, (page * 8) + bit]:
                        value |= 1 << bit
                buffer[(page * 128) + x] = value
        return bytes(buffer)

    def _render_lines(self, lines: tuple[str, str]) -> None:
        assert Image is not None and ImageDraw is not None
        image = Image.new("1", (128, 32), 0)
        draw = ImageDraw.Draw(image)
        draw.text((0, 1), lines[0], font=self._font_primary, fill=255)
        draw.text((0, 17), lines[1], font=self._font_secondary, fill=255)
        self._show_buffer(self._image_to_buffer(image))

    def _format_lines(self, snapshot: Dict[str, Any], tick: int) -> tuple[str, str]:
        mode = _compact_text(snapshot.get("mode_label") or "RADIO").upper()
        volume = int(snapshot.get("volume") or 0)
        muted = bool(snapshot.get("muted"))
        recording = bool(snapshot.get("recording_active"))
        recording_elapsed = int(snapshot.get("recording_elapsed") or 0)
        station_label = _compact_text(snapshot.get("station_label") or "")
        if not station_label:
            station_label = "Waiting for station" if snapshot.get("booted") else "Server ready"
        meta = _compact_text(snapshot.get("dab_now") or snapshot.get("freq_label") or snapshot.get("last_error") or "")
        if not meta:
            meta = "Use Web UI or buttons"
        signal = snapshot.get("signal") or {}
        score = signal.get("score")
        if recording:
            status_text = f"REC {recording_elapsed}s"
        elif muted:
            status_text = f"{mode} MUTE"
        elif score is not None:
            status_text = f"{mode} V{volume:02d} S{int(score):02d}"
        else:
            status_text = f"{mode} V{volume:02d}"
        page = (tick // 12) % 2
        primary_source = station_label
        if page == 1 and meta and meta.casefold() != station_label.casefold():
            primary_source = meta
        primary = _marquee_text(primary_source, OLED_LINE_WIDTH, tick)
        secondary = _marquee_text(status_text, OLED_LINE_WIDTH, tick // 2)
        return (primary, secondary)

    def _run(self) -> None:
        while not self._stop.wait(self.update_interval_s):
            try:
                if not self.enabled:
                    self._ensure_ready()
                snapshot = self._status_supplier()
                tick = int(time.monotonic() / self.update_interval_s)
                lines = self._format_lines(snapshot, tick)
                if lines != self._last_lines:
                    self._render_lines(lines)
                    self._last_lines = lines
            except Exception as exc:
                self.error = str(exc)
                self.enabled = False
                self._last_lines = None
                try:
                    if self._bus is not None:
                        self._bus.close()
                except Exception:
                    pass
                self._bus = None

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        if self._bus is not None:
            try:
                self._clear()
            except Exception:
                pass
            try:
                self._write_command(0xAE)
            except Exception:
                pass
            try:
                self._bus.close()
            except Exception:
                pass
        self._bus = None
        self._thread = None
        self.enabled = False


class RadioBackend:
    def __init__(self, config: RadioConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._radio: Optional[Si468xDabRadio] = None
        self._amp = AmplifierGate(config.amp_pin, config.amp_active_high)
        self._amp_requested = True
        self._muted = False
        self._booted = False
        self._loaded_firmware: Optional[str] = None
        self._audio_out_mode = config.audio_out if config.audio_out in VALID_AUDIO_OUT else "both"
        self._current_mode = config.default_mode if config.default_mode in MODE_DEFS else "dab"
        self._current_station: Optional[Dict[str, Any]] = None
        self._current_volume = _clamp_int(config.default_volume, 0, 63)
        self._last_signal: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._closing = False
        self._nav_pending_push = False
        self._nav_station_mode = False
        self._nav_push_timer: Optional[threading.Timer] = None
        self._nav_station_timer: Optional[threading.Timer] = None
        self._resume_timer: Optional[threading.Timer] = None
        self._resume_attempts = 0
        self._dab_media: Dict[str, Any] = _empty_dab_media()
        self._dab_artwork_bytes: Optional[bytes] = None
        self._dab_artwork_content_type: Optional[str] = None
        self._dab_artwork_name: Optional[str] = None
        self._dab_artwork_updated_at: Optional[float] = None
        self._dab_mot_objects: Dict[int, Dict[str, Any]] = {}
        self._stations: Dict[str, List[Dict[str, Any]]] = {key: [] for key in SCAN_KEYS}
        self._last_scan_count: Dict[str, int] = {key: 0 for key in SCAN_KEYS}
        self._last_scan_time: Dict[str, Optional[float]] = {key: None for key in SCAN_KEYS}
        self._favorites: set[str] = set()
        self._recording_process: Optional[subprocess.Popen[str]] = None
        self._recording_meta: Optional[Dict[str, Any]] = None
        self._oled_requested = bool(config.oled_enabled)
        self._resume_station_id: Optional[str] = None
        self._service_status_cache: Dict[str, Any] = {
            "service": self.config.system_service_name,
            "enabled": None,
            "active": None,
            "available": False,
            "error": None,
        }
        self._service_status_updated_at = 0.0
        self._dab_freqs = [freq for _, freq in DAB_BAND_III]
        self._dab_freq_index = {freq: idx for idx, freq in enumerate(self._dab_freqs)}
        self._load_scan_files_locked()
        self._load_favorites_locked()
        self.config.recordings_dir.mkdir(parents=True, exist_ok=True)
        self._load_runtime_state_locked()
        self._button_nav = ButtonNavigator(
            config.nav_cw_pin,
            config.nav_push_pin,
            config.nav_ccw_pin,
            active_low=config.nav_active_low,
            debounce_ms=config.nav_debounce_ms,
            poll_interval_s=config.nav_poll_interval_s,
            on_event=self._handle_nav_button_event,
        )
        if not self._button_nav.start() and all(
            pin is not None for pin in (config.nav_cw_pin, config.nav_push_pin, config.nav_ccw_pin)
        ) and GPIO is None:
            self._last_error = (
                "GPIO button navigation requested, but RPi.GPIO is unavailable. "
                f"Import failed with: {_GPIO_IMPORT_ERROR}"
            )
        self._oled = self._new_oled_locked(enabled=self._oled_requested)
        self._oled.start()
        atexit.register(self.close)
        with self._lock:
            self._schedule_runtime_resume_locked(delay_s=2.0)

    def boot(self, mode: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if mode is not None:
                self._set_mode_locked(mode)
            return self._boot_locked(force=force)

    def set_mode(self, mode: str) -> Dict[str, Any]:
        with self._lock:
            self._set_mode_locked(mode)
            self._save_runtime_state_locked()
            return self._boot_locked(force=False)

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return self._status_payload_locked(refresh_signal=True)

    def get_stations(self, mode: Optional[str] = None, refresh_from_disk: bool = False) -> List[Dict[str, Any]]:
        with self._lock:
            scan_key = self._scan_key(mode)
            if refresh_from_disk:
                self._load_scan_file_locked(scan_key)
            return [self._decorate_station_locked(station) for station in self._stations[scan_key]]

    def get_favorites(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._favorite_stations_locked()

    def get_recordings(self) -> List[Dict[str, Any]]:
        with self._lock:
            return self._list_recordings_locked()

    def get_dab_artwork(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if not self._dab_artwork_bytes:
                return None
            return {
                "content": bytes(self._dab_artwork_bytes),
                "content_type": self._dab_artwork_content_type or "application/octet-stream",
                "name": self._dab_artwork_name,
            }

    def get_live_stream_metadata(self) -> Dict[str, Any]:
        with self._lock:
            if self._booted and self._current_station and self._current_station.get("mode") == "dab":
                self._poll_dab_media_locked()
            return {
                "station": self._decorate_station_locked(self._current_station) if self._current_station else None,
                "dab_media": self._dab_media_payload_locked(),
            }

    def scan(self, force: bool = True) -> Dict[str, Any]:
        with self._lock:
            self._boot_locked(force=False)
            scan_key = self._scan_key()
            if self._stations[scan_key] and not force:
                stations = self.get_stations()
                return {"stations": stations, "count": len(stations), "scan_key": scan_key}
            stations = self._scan_dab_locked() if self._current_mode == "dab" else self._scan_analog_locked()
            self._last_error = None
            return {"stations": stations, "count": len(stations), "scan_key": scan_key}

    def play(
        self,
        *,
        index: Optional[int] = None,
        label: Optional[str] = None,
        station_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            station = self._resolve_station_locked(index=index, label=label, station_id=station_id)
            self._ensure_station_playing_locked(station)
            return self._status_payload_locked(refresh_signal=True)

    def set_volume(self, *, level: Optional[int] = None, delta: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            self._boot_locked(force=False)
            radio = self._require_radio_locked()
            next_level = self._current_volume if level is None else int(level)
            if delta is not None:
                next_level += int(delta)
            if self._muted:
                self._muted = False
                self._apply_mute_locked(radio)
            self._current_volume = radio.set_volume(_clamp_int(next_level, 0, 63))
            self._save_runtime_state_locked()
            return self._status_payload_locked(refresh_signal=False)

    def set_amplifier(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            self._amp_requested = bool(enabled)
            if self._booted:
                self._amp.set_enabled(self._amp_requested)
            self._save_runtime_state_locked()
            return self._status_payload_locked(refresh_signal=False)

    def set_muted(self, enabled: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            self._boot_locked(force=False)
            self._muted = (not self._muted) if enabled is None else bool(enabled)
            self._apply_mute_locked()
            self._last_error = None
            self._save_runtime_state_locked()
            return self._status_payload_locked(refresh_signal=False)

    def set_oled_enabled(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            self._oled_requested = bool(enabled)
            previous = self._oled
            self._oled = self._new_oled_locked(enabled=self._oled_requested)
            current = self._oled
        previous.close()
        current.start()
        with self._lock:
            self._save_runtime_state_locked()
            return self._status_payload_locked(refresh_signal=False)

    def set_start_with_system(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            service_name = str(self.config.system_service_name or "").strip()
            if not service_name:
                raise RuntimeError("No systemd service is configured for autostart.")
        command = ["sudo", "-n", "systemctl", "enable" if enabled else "disable", service_name]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=8.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Unable to update autostart for {service_name}: {exc}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout or f"systemctl exited with {result.returncode}").strip()
            raise RuntimeError(f"Unable to {'enable' if enabled else 'disable'} {service_name}: {message}")
        with self._lock:
            self._refresh_system_service_status_locked(force=True)
            return self._status_payload_locked(refresh_signal=False)

    def set_favorite(self, station_id: str, favorite: Optional[bool] = None) -> Dict[str, Any]:
        with self._lock:
            station_id = str(station_id).strip()
            if not station_id:
                raise ValueError("station_id is required.")
            if favorite is None:
                favorite = station_id not in self._favorites
            if favorite:
                self._favorites.add(station_id)
            else:
                self._favorites.discard(station_id)
            self._save_favorites_locked()
            return {
                "station_id": station_id,
                "favorite": station_id in self._favorites,
                "favorites": self._favorite_stations_locked(),
            }

    def record(self, action: str = "toggle") -> Dict[str, Any]:
        with self._lock:
            token = str(action or "toggle").strip().lower()
            if token == "toggle":
                token = "stop" if self._recording_active_locked() else "start"
            if token == "start":
                self._start_recording_locked()
            elif token == "stop":
                self._stop_recording_locked()
            else:
                raise ValueError("record action must be start, stop or toggle.")
            return self._status_payload_locked(refresh_signal=False)

    def flash_program(self, mode: Optional[str] = None, run_self_test: bool = True) -> Dict[str, Any]:
        with self._lock:
            target_mode = self._normalize_mode(mode) if mode is not None else self._current_mode
            if self._recording_active_locked():
                self._stop_recording_locked()
            self._shutdown_locked(close_amp=False)

            report = {
                "mode": target_mode,
                "mode_label": MODE_DEFS[target_mode]["label"],
                "firmware_key": self._mode_info(target_mode)["firmware"],
                "patch_image": str(self.config.patch_path),
                "mini_patch_image": str(self.config.mini_patch_path),
                "firmware_image": str(self._firmware_path_for_mode(target_mode)),
                "flash_patch_addr": self.config.flash_patch_addr,
                "flash_firmware_addr": self._flash_firmware_addr_for_mode(target_mode),
                "self_test_requested": bool(run_self_test),
                "self_test": [],
                "programmed": False,
            }
            restore_exc: Optional[Exception] = None
            try:
                self._flash_program_locked(target_mode, report)
                report["programmed"] = True
                if run_self_test:
                    report["bootable"] = self._flash_self_test_locked(target_mode, report)
                else:
                    report["bootable"] = None
                report["status"] = "ok" if report["bootable"] is not False else "self_test_failed"
                report["error"] = None if report["bootable"] is not False else self._flash_self_test_error(report)
                self._last_error = report["error"]
                report["restored_status"] = self._restore_runtime_locked()
                return report
            except Exception as exc:
                self._last_error = str(exc)
                try:
                    report["restored_status"] = self._restore_runtime_locked()
                except Exception as inner_exc:
                    restore_exc = inner_exc
                    report["restore_error"] = str(inner_exc)
                if restore_exc is not None:
                    raise RuntimeError(f"{exc} | restore failed: {restore_exc}") from exc
                raise

    def close(self) -> None:
        button_nav = self._button_nav
        oled = self._oled
        with self._lock:
            self._closing = True
            self._cancel_nav_push_timer_locked()
            self._cancel_nav_station_timer_locked()
            self._cancel_resume_timer_locked()
            self._save_runtime_state_locked()
            self._stop_recording_locked()
            self._shutdown_locked(close_amp=True)
        button_nav.close()
        oled.close()

    def _new_oled_locked(self, *, enabled: bool) -> OledStatusDisplay:
        return OledStatusDisplay(
            enabled=enabled,
            bus_num=self.config.oled_i2c_bus,
            address=self.config.oled_i2c_addr,
            update_interval_s=self.config.oled_update_interval_s,
            status_supplier=self._oled_snapshot,
        )

    def _refresh_system_service_status_locked(self, *, force: bool = False) -> Dict[str, Any]:
        now = time.monotonic()
        if not force and (now - self._service_status_updated_at) < 2.0:
            return dict(self._service_status_cache)
        service_name = str(self.config.system_service_name or "").strip()
        status = {
            "service": service_name,
            "enabled": None,
            "active": None,
            "available": False,
            "error": None,
        }
        if not service_name:
            status["error"] = "No systemd service configured."
            self._service_status_cache = status
            self._service_status_updated_at = now
            return dict(status)

        try:
            enabled_result = subprocess.run(
                ["systemctl", "is-enabled", service_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
            active_result = subprocess.run(
                ["systemctl", "is-active", service_name],
                check=False,
                capture_output=True,
                text=True,
                timeout=3.0,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            status["error"] = str(exc)
            self._service_status_cache = status
            self._service_status_updated_at = now
            return dict(status)

        enabled_text = (enabled_result.stdout or enabled_result.stderr or "").strip().lower()
        active_text = (active_result.stdout or active_result.stderr or "").strip().lower()
        status["enabled"] = enabled_text == "enabled"
        status["active"] = active_text == "active"
        status["available"] = enabled_text not in {"", "not-found"} and active_text not in {"", "unknown"}
        errors = []
        if enabled_result.returncode not in {0, 1} and enabled_text not in {"disabled", "indirect", "static"}:
            errors.append(enabled_text or f"is-enabled rc={enabled_result.returncode}")
        if active_result.returncode not in {0, 3} and active_text not in {"inactive", "failed"}:
            errors.append(active_text or f"is-active rc={active_result.returncode}")
        if enabled_text == "not-found" or active_text == "unknown":
            errors.append("systemd service not found")
        if errors:
            status["error"] = "; ".join(error for error in errors if error)
        self._service_status_cache = status
        self._service_status_updated_at = now
        return dict(status)

    def _normalize_mode(self, mode: str) -> str:
        normalized = str(mode).strip().lower().replace("-", "_")
        if normalized not in MODE_DEFS:
            raise ValueError(f"Unsupported mode: {mode}")
        return normalized

    def _set_mode_locked(self, mode: str) -> None:
        normalized = self._normalize_mode(mode)
        if normalized == self._current_mode:
            return
        if self._recording_active_locked():
            self._stop_recording_locked()
        self._current_mode = normalized
        self._current_station = None
        self._last_signal = None
        self._reset_dab_media_locked(clear_artwork=True)

    def _mode_info(self, mode: Optional[str] = None) -> Dict[str, Any]:
        return MODE_DEFS[mode or self._current_mode]

    def _scan_key(self, mode: Optional[str] = None) -> str:
        return self._mode_info(mode)["scan_key"]

    def _scan_file_for_key(self, scan_key: str) -> Path:
        return {
            "dab": self.config.dab_scan_file,
            "fm": self.config.fm_scan_file,
            "hd": self.config.hd_scan_file,
            "am": self.config.am_scan_file,
            "am_hd": self.config.am_hd_scan_file,
        }[scan_key]

    def _firmware_path_for_mode(self, mode: Optional[str] = None) -> Path:
        firmware_key = self._mode_info(mode)["firmware"]
        return {
            "dab": self.config.dab_firmware_path,
            "fmhd": self.config.fmhd_firmware_path,
            "amhd": self.config.amhd_firmware_path,
        }[firmware_key]

    def _flash_firmware_addr_for_mode(self, mode: Optional[str] = None) -> int:
        firmware_key = self._mode_info(mode)["firmware"]
        return {
            "dab": self.config.flash_dab_addr,
            "fmhd": self.config.flash_fmhd_addr,
            "amhd": self.config.flash_amhd_addr,
        }[firmware_key]

    def _load_scan_files_locked(self) -> None:
        for scan_key in SCAN_KEYS:
            self._load_scan_file_locked(scan_key)

    def _load_scan_file_locked(self, scan_key: str) -> None:
        path = self._scan_file_for_key(scan_key)
        raw_items = load_scan_file(path) or []
        stations = [self._normalize_station(scan_key, raw) for raw in raw_items if isinstance(raw, dict)]
        stations.sort(key=_station_sort_key)
        self._stations[scan_key] = stations
        self._last_scan_count[scan_key] = len(stations)
        self._last_scan_time[scan_key] = path.stat().st_mtime if path.exists() else None

    def _save_scan_file_locked(self, scan_key: str, stations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        path = self._scan_file_for_key(scan_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = []
        for station in sorted(stations, key=_station_sort_key):
            item = dict(station)
            item.pop("favorite", None)
            item.pop("is_current", None)
            payload.append(item)
        path.write_text(SCAN_FILE_HEADER + json.dumps(payload, indent=2), encoding="utf-8")
        self._stations[scan_key] = payload
        self._last_scan_count[scan_key] = len(payload)
        self._last_scan_time[scan_key] = time.time()
        return [self._decorate_station_locked(station) for station in payload]

    def _load_favorites_locked(self) -> None:
        path = self.config.favorites_file
        if not path.exists():
            self._favorites = set()
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._favorites = set()
            return
        self._favorites = {str(item).strip() for item in data if str(item).strip()}

    def _save_favorites_locked(self) -> None:
        path = self.config.favorites_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(self._favorites), indent=2), encoding="utf-8")

    def _load_runtime_state_locked(self) -> None:
        path = self.config.runtime_state_file
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        mode = str(data.get("mode") or "").strip().lower()
        if mode in MODE_DEFS:
            self._current_mode = mode
        if "volume" in data:
            try:
                self._current_volume = _clamp_int(int(data["volume"]), 0, 63)
            except Exception:
                pass
        self._muted = bool(data.get("muted", self._muted))
        self._amp_requested = bool(data.get("amp_requested", self._amp_requested))
        self._oled_requested = bool(data.get("oled_requested", self._oled_requested))
        station_id = str(data.get("station_id") or "").strip()
        self._resume_station_id = station_id or None

    def _save_runtime_state_locked(self) -> None:
        path = self.config.runtime_state_file
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "mode": self._current_mode,
            "volume": self._current_volume,
            "muted": self._muted,
            "amp_requested": self._amp_requested,
            "oled_requested": self._oled_requested,
            "station_id": (self._current_station or {}).get("station_id"),
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _resume_runtime_state_locked(self) -> bool:
        station_id = self._resume_station_id
        if not station_id:
            return False
        station: Optional[Dict[str, Any]] = None
        for scan_key in SCAN_KEYS:
            for candidate in self._stations[scan_key]:
                if candidate.get("station_id") == station_id:
                    station = dict(candidate)
                    break
            if station is not None:
                break
        if station is None:
            self._resume_station_id = None
            return False
        if station["mode"] != self._current_mode:
            self._current_mode = station["mode"]
        self._boot_locked(force=False)
        if station["mode"] == "dab":
            self._play_dab_locked(station)
        else:
            self._play_analog_locked(station)
        self._resume_station_id = None
        self._resume_attempts = 0
        self._last_error = None
        self._save_runtime_state_locked()
        return True

    def _cancel_resume_timer_locked(self) -> None:
        timer = self._resume_timer
        self._resume_timer = None
        if timer is not None:
            timer.cancel()

    def _schedule_runtime_resume_locked(self, delay_s: float = 2.0) -> None:
        if self._closing or not self._resume_station_id or self._current_station is not None:
            return
        self._cancel_resume_timer_locked()
        timer = threading.Timer(max(0.2, float(delay_s)), self._resume_runtime_timer_fired)
        timer.daemon = True
        self._resume_timer = timer
        timer.start()

    def _resume_runtime_timer_fired(self) -> None:
        with self._lock:
            self._resume_timer = None
            if self._closing or not self._resume_station_id or self._current_station is not None:
                return
            self._resume_attempts += 1
            try:
                if self._resume_runtime_state_locked():
                    return
            except Exception as exc:
                self._last_error = str(exc)
            if self._resume_attempts < 4:
                self._schedule_runtime_resume_locked(delay_s=2.0)

    def _status_payload_locked(self, refresh_signal: bool) -> Dict[str, Any]:
        self._refresh_recording_state_locked()
        if self._booted and self._current_station is not None and self._current_station.get("mode") == "dab":
            self._poll_dab_media_locked()
        if refresh_signal and self._booted and self._current_station is not None:
            try:
                self._last_signal = self._read_current_signal_locked()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
        scan_key = self._scan_key()
        service_status = self._refresh_system_service_status_locked()
        return {
            "booted": self._booted,
            "transport": "spi",
            "mode": self._current_mode,
            "mode_label": self._mode_info()["label"],
            "available_modes": [dict(MODE_DEFS[key]) for key in SCAN_KEYS],
            "firmware": self._loaded_firmware,
            "audio_out": self._audio_out_mode,
            "volume": self._current_volume,
            "muted": self._muted,
            "amp_enabled": self._amp.enabled if self._booted else False,
            "amp_requested": self._amp_requested,
            "amp_pin": self.config.amp_pin,
            "button_nav": {
                "enabled": self._button_nav.enabled,
                "mode": "station" if self._nav_station_mode else "volume",
                "pending_push": self._nav_pending_push,
                "cw_pin": self.config.nav_cw_pin,
                "push_pin": self.config.nav_push_pin,
                "ccw_pin": self.config.nav_ccw_pin,
            },
            "oled": {
                "requested": self._oled_requested,
                "enabled": self._oled.enabled,
                "i2c_bus": self.config.oled_i2c_bus,
                "i2c_addr": self.config.oled_i2c_addr,
                "error": self._oled.error,
            },
            "system_service": service_status,
            "current_station": self._decorate_station_locked(self._current_station) if self._current_station else None,
            "dab_media": self._dab_media_payload_locked(),
            "signal": dict(self._last_signal or {}),
            "station_count": len(self._stations[scan_key]),
            "favorite_count": len(self._favorites),
            "scan_key": scan_key,
            "last_scan_count": self._last_scan_count[scan_key],
            "last_scan_time": _iso_or_none(self._last_scan_time[scan_key]),
            "recording": self._recording_payload_locked(),
            "live_stream": {
                "supported": shutil.which("ffmpeg") is not None,
                "path": "/audio/live.mp3",
                "ready": self._current_station is not None,
                "station_path_template": "/audio/stations/{station_id}.mp3",
                "playlist_paths": {
                    "dab": "/playlists/dab.m3u",
                    "favorites": "/playlists/favorites.m3u",
                },
            },
            "recordings_count": len(self._list_recordings_locked()),
            "last_error": self._last_error,
        }

    def _oled_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._refresh_recording_state_locked()
            station = self._current_station or {}
            signal = dict(self._last_signal or {})
            recording = self._recording_payload_locked()
            station_label = _compact_text(station.get("label") or "")
            freq_khz = int(station.get("freq_khz") or 0)
            if station.get("band") == "fm":
                freq_label = f"{freq_khz / 1000.0:.1f} MHz" if freq_khz else ""
            else:
                freq_label = f"{freq_khz} kHz" if freq_khz else ""
            dab_now = self._dab_media.get("text") or ""
            if not dab_now:
                title = _compact_text(self._dab_media.get("title") or "")
                artist = _compact_text(self._dab_media.get("artist") or "")
                dab_now = " - ".join(part for part in (title, artist) if part)
            return {
                "booted": self._booted,
                "mode": self._current_mode,
                "mode_label": self._mode_info()["label"],
                "volume": self._current_volume,
                "muted": self._muted,
                "amp_enabled": self._amp.enabled if self._booted else False,
                "station_label": station_label,
                "freq_label": freq_label,
                "dab_now": dab_now,
                "recording_active": bool(recording.get("active")),
                "recording_elapsed": int(round(float(recording.get("elapsed_seconds") or 0))),
                "signal": signal,
                "last_error": self._last_error,
            }

    def _stations_for_mode_locked(self) -> List[Dict[str, Any]]:
        return self._stations[self._scan_key()]

    def _favorite_stations_locked(self) -> List[Dict[str, Any]]:
        favorites: List[Dict[str, Any]] = []
        for scan_key in SCAN_KEYS:
            for station in self._stations[scan_key]:
                if station["station_id"] in self._favorites:
                    favorites.append(self._decorate_station_locked(station))
        favorites.sort(key=_station_sort_key)
        return favorites

    def _decorate_station_locked(self, station: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if station is None:
            return None
        item = dict(station)
        item["favorite"] = item["station_id"] in self._favorites
        item["is_current"] = self._current_station is not None and item["station_id"] == self._current_station.get("station_id")
        item["mode_label"] = MODE_DEFS[item["mode"]]["label"]
        return item

    def _dab_media_payload_locked(self) -> Dict[str, Any]:
        payload = dict(self._dab_media)
        payload["updated_at"] = _iso_or_none(payload.get("updated_at"))
        payload["artwork_url"] = self._dab_artwork_url_locked()
        payload["artwork_content_type"] = self._dab_artwork_content_type
        payload["artwork_name"] = self._dab_artwork_name
        payload["artwork_updated_at"] = _iso_or_none(self._dab_artwork_updated_at)
        payload["artwork_supported"] = bool(payload.get("artwork_supported") or self._dab_artwork_bytes)
        payload["available"] = bool(payload.get("text") or payload.get("artwork_url"))
        return payload

    def _poll_dab_media_locked(self, max_packets: int = 16) -> None:
        station = self._current_station
        if station is None or station.get("mode") != "dab":
            return
        radio = self._require_radio_locked()
        try:
            status = radio.get_digital_service_data(status_only=True, ack=False)
        except Exception:
            return
        packets = int(status.get("buffer_count") or 0)
        if not status.get("packet_ready") and packets <= 0:
            return
        packets = max(1 if status.get("packet_ready") else 0, min(max_packets, packets))
        for _ in range(packets):
            try:
                packet = radio.get_digital_service_data(status_only=False, ack=True)
            except Exception:
                return
            self._consume_dab_packet_locked(packet)

    def _consume_dab_packet_locked(self, packet: Dict[str, Any]) -> None:
        station = self._current_station
        if station is None or station.get("mode") != "dab":
            return
        if int(packet.get("byte_count") or 0) <= 0:
            return
        if int(packet.get("service_id") or 0) != int(station.get("service_id") or 0):
            return
        if int(packet.get("component_id") or 0) != int(station.get("component_id") or 0):
            return
        payload = bytes(packet.get("payload") or b"")
        data_src = int(packet.get("data_src") or -1)
        dscty = int(packet.get("dscty") or -1)
        if data_src == DAB_DATA_SRC_PAD_DATA and dscty == DAB_DSCTY_MOT:
            self._consume_dab_mot_packet_locked(payload)
            return
        if data_src != DAB_DATA_SRC_PAD_DLS:
            return
        if len(payload) < 2:
            return
        prefix0 = payload[0]
        if (prefix0 & 0x10) == 0:
            encoding = (payload[1] >> 4) & 0x0F
            if encoding == 0x04:
                encoding = 0x06
            text = _decode_dab_text(payload[2:], encoding)
            artist, title = _infer_artist_title(text)
            self._dab_media = {
                "text": text,
                "title": title,
                "artist": artist,
                "encoding": encoding,
                "toggle": 1 if (prefix0 & 0x80) else 0,
                "updated_at": time.time(),
                "artwork_url": None,
                "artwork_supported": bool(self._dab_media.get("artwork_supported") or self._dab_artwork_bytes),
            }
            return
        command_type = prefix0 & 0x0F
        if command_type == 0x01:
            self._reset_dab_media_locked(clear_artwork=False)

    def _reset_dab_media_locked(self, clear_artwork: bool) -> None:
        self._dab_media = _empty_dab_media()
        if clear_artwork:
            self._dab_artwork_bytes = None
            self._dab_artwork_content_type = None
            self._dab_artwork_name = None
            self._dab_artwork_updated_at = None
            self._dab_mot_objects = {}
        elif self._dab_artwork_bytes:
            self._dab_media["artwork_supported"] = True

    def _dab_artwork_url_locked(self) -> Optional[str]:
        if not self._dab_artwork_bytes:
            return None
        if self._dab_artwork_updated_at is None:
            return "/api/dab/artwork"
        return f"/api/dab/artwork?ts={int(self._dab_artwork_updated_at * 1000)}"

    def _consume_dab_mot_packet_locked(self, payload: bytes) -> None:
        mot = _parse_mot_segment(payload)
        if mot is None:
            return
        self._dab_media["artwork_supported"] = True
        now = time.time()
        object_id = int(mot["object_id"])
        entry = self._dab_mot_objects.get(object_id)
        if entry is None:
            entry = {
                "header_segments": {},
                "body_segments": {},
                "header_last": None,
                "body_last": None,
                "filename": None,
                "updated_at": now,
            }
            self._dab_mot_objects[object_id] = entry
        entry["updated_at"] = now

        packet_type = int(mot["packet_type"])
        segment_index = int(mot["segment_index"])
        if packet_type == DAB_MOT_HEADER_PACKET:
            entry["header_segments"][segment_index] = bytes(mot["chunk"])
            if mot["is_last"]:
                entry["header_last"] = segment_index
            header_bytes = _join_mot_segments(entry["header_segments"], entry["header_last"])
            if header_bytes:
                entry["filename"] = _extract_mot_filename(header_bytes) or entry.get("filename")
        elif packet_type == DAB_MOT_BODY_PACKET:
            entry["body_segments"][segment_index] = bytes(mot["chunk"])
            if mot["is_last"]:
                entry["body_last"] = segment_index
            body_bytes = _join_mot_segments(entry["body_segments"], entry["body_last"])
            if body_bytes:
                header_bytes = _join_mot_segments(entry["header_segments"], entry["header_last"])
                filename = entry.get("filename") or _extract_mot_filename(header_bytes or b"")
                if filename:
                    entry["filename"] = filename
                image_bytes, content_type = _extract_image_payload(body_bytes, filename)
                if image_bytes:
                    self._set_dab_artwork_locked(image_bytes, content_type, filename)
        self._prune_dab_mot_objects_locked(now)

    def _set_dab_artwork_locked(
        self,
        image_bytes: bytes,
        content_type: Optional[str],
        filename: Optional[str],
    ) -> None:
        image = bytes(image_bytes or b"")
        if not image:
            return
        effective_type = content_type or _guess_image_content_type(filename) or "application/octet-stream"
        if self._dab_artwork_bytes == image and self._dab_artwork_content_type == effective_type:
            return
        self._dab_artwork_bytes = image
        self._dab_artwork_content_type = effective_type
        self._dab_artwork_name = filename
        self._dab_artwork_updated_at = time.time()
        self._dab_media["artwork_supported"] = True

    def _prune_dab_mot_objects_locked(self, now: float) -> None:
        stale_ids = [
            object_id
            for object_id, entry in self._dab_mot_objects.items()
            if (now - float(entry.get("updated_at") or now)) > MAX_DAB_MOT_AGE_S
        ]
        for object_id in stale_ids:
            self._dab_mot_objects.pop(object_id, None)
        if len(self._dab_mot_objects) <= MAX_DAB_MOT_OBJECTS:
            return
        oldest = sorted(
            self._dab_mot_objects.items(),
            key=lambda item: float(item[1].get("updated_at") or 0.0),
        )
        for object_id, _ in oldest[: len(self._dab_mot_objects) - MAX_DAB_MOT_OBJECTS]:
            self._dab_mot_objects.pop(object_id, None)

    def _normalize_station(self, scan_key: str, raw: Dict[str, Any]) -> Dict[str, Any]:
        mode_info = MODE_DEFS[scan_key]
        station = dict(raw)
        station["mode"] = mode_info["id"]
        station["band"] = mode_info["band"]
        if mode_info["band"] == "dab":
            station["service_id"] = int(station.get("service_id", 0))
            station["component_id"] = int(station.get("component_id", 0))
            station["freq_index"] = int(station.get("freq_index", self._dab_freq_index.get(int(station.get("freq_khz", 0)), 0)))
            station["freq_khz"] = int(station.get("freq_khz", self._dab_freqs[station["freq_index"]]))
            station["station_id"] = f"dab:{station['service_id']:08x}:{station['component_id']:08x}:{station['freq_khz']}"
        else:
            station["freq_khz"] = int(station.get("freq_khz", 0))
            station["service_id"] = int(station.get("service_id", station["freq_khz"]))
            station["component_id"] = int(station.get("component_id", 0))
            station["analog_available"] = bool(station.get("analog_available", scan_key in {"fm", "am"}))
            station["hd_available"] = bool(station.get("hd_available", scan_key in {"hd", "am_hd"}))
            station["program_mask"] = int(station.get("program_mask", 0))
            station["program_id"] = int(station.get("program_id", 0))
            station["station_id"] = f"{scan_key}:{station['freq_khz']}"
        label = str(station.get("label") or "").strip()
        if not label:
            label = self._default_station_label(scan_key, station["freq_khz"], station.get("program_mask", 0), station.get("hd_available", False))
        station["label"] = label
        return station

    def _default_station_label(self, scan_key: str, freq_khz: int, program_mask: int, hd_available: bool) -> str:
        if scan_key == "dab":
            return f"DAB {freq_khz} kHz"
        if scan_key in {"fm", "hd"}:
            mhz = freq_khz / 1000.0
            prefix = "HD" if scan_key == "hd" or hd_available else "FM"
            suffix = ""
            if (scan_key == "hd" or hd_available) and program_mask:
                count = bin(program_mask).count("1")
                if count > 1:
                    suffix = f" ({count})"
            return f"{prefix} {mhz:.1f}{suffix}"
        prefix = "AM HD" if scan_key == "am_hd" or hd_available else "AM"
        return f"{prefix} {freq_khz} kHz"

    def _boot_locked(self, force: bool) -> Dict[str, Any]:
        mode_info = self._mode_info()
        firmware_key = mode_info["firmware"]
        if force or not self._booted or self._radio is None or self._loaded_firmware != firmware_key:
            if self._recording_active_locked():
                self._stop_recording_locked()
            self._shutdown_locked(close_amp=False)
            radio = self._create_radio()
            try:
                radio.reset()
                radio.power_up(xtal_freq=self.config.xtal_freq, ctun=self.config.ctun, retries=2)
                radio.load_patch_and_firmware(self.config.patch_path, self._firmware_path_for_mode())
                if self._current_mode == "dab":
                    radio.configure_dab_frontend()
                    radio.set_dab_freq_list(self._dab_freqs)
                elif self._mode_info()["band"] == "fm":
                    radio.configure_fmhd_frontend()
                elif self._mode_info()["band"] == "am":
                    radio.configure_amhd_frontend()
                self._radio = radio
                self._booted = True
                self._loaded_firmware = firmware_key
            except Exception:
                try:
                    radio.close()
                except Exception:
                    pass
                self._radio = None
                self._booted = False
                raise
        radio = self._require_radio_locked()
        self._apply_audio_config_locked(radio)
        self._current_volume = radio.set_volume(self._current_volume)
        self._apply_mute_locked(radio)
        self._amp.set_enabled(self._amp_requested)
        return self._status_payload_locked(refresh_signal=False)

    def _create_radio(self, *, spi_speed_hz: Optional[int] = None) -> Si468xDabRadio:
        return Si468xDabRadio(
            i2c_bus=self.config.i2c_bus,
            i2c_addr=self.config.i2c_addr,
            rst_pin=self.config.rst_pin,
            int_pin=self.config.int_pin,
            use_spi=True,
            spi_bus=self.config.spi_bus,
            spi_dev=self.config.spi_dev,
            spi_speed_hz=self.config.spi_speed_hz if spi_speed_hz is None else int(spi_speed_hz),
        )

    def _restore_runtime_locked(self) -> Dict[str, Any]:
        return self._boot_locked(force=True)

    def _flash_program_locked(self, mode: str, report: Dict[str, Any]) -> None:
        radio = self._create_radio()
        try:
            radio.reset()
            radio.power_up(xtal_freq=self.config.xtal_freq, ctun=self.config.ctun, retries=2)
            radio.load_patch_only(self.config.mini_patch_path)
            if radio.use_spi and radio.spi is not None:
                radio.spi.max_speed_hz = min(int(radio.spi.max_speed_hz), int(self.config.flash_program_spi_hz))
            radio.flash_enter_program_mode()
            self._program_flash_image_locked(
                radio,
                self.config.patch_path,
                self.config.flash_patch_addr,
                "full patch",
            )
            self._program_flash_image_locked(
                radio,
                self._firmware_path_for_mode(mode),
                self._flash_firmware_addr_for_mode(mode),
                f"{report['mode_label']} firmware",
            )
        finally:
            radio.close()

    def _flash_self_test_locked(self, mode: str, report: Dict[str, Any]) -> bool:
        attempts = [
            ("mini", self._flash_boot_mini_locked),
            ("full", self._flash_boot_full_locked),
        ]
        bootable = False
        for name, boot_fn in attempts:
            entry: Dict[str, Any] = {"method": name, "ok": False}
            radio = self._create_radio()
            try:
                radio.reset()
                radio.power_up(xtal_freq=self.config.xtal_freq, ctun=self.config.ctun, retries=2)
                boot_fn(radio, mode)
                entry["probe"] = self._probe_firmware_locked(radio, mode)
                entry["ok"] = True
                bootable = True
            except Exception as exc:
                entry["error"] = str(exc)
            finally:
                report["self_test"].append(entry)
                radio.close()
            if bootable:
                break
        return bootable

    def _flash_self_test_error(self, report: Dict[str, Any]) -> str:
        attempts = report.get("self_test") or []
        if not attempts:
            return "Flash self-test failed."
        return "Flash self-test failed. " + " | ".join(
            f"{item['method']}: {item.get('error', 'unknown error')}"
            for item in attempts
        )

    def _flash_boot_mini_locked(self, radio: Si468xDabRadio, mode: str) -> None:
        radio.load_patch_only(self.config.mini_patch_path, allow_cmd_error=True)
        time.sleep(0.004)
        radio.flash_load_mini_and_boot(
            self.config.flash_patch_addr,
            self._flash_firmware_addr_for_mode(mode),
            full_patch_wait_ms=4,
            nvmspi_rate_khz=0,
            allow_cmd_error=True,
        )

    def _flash_boot_full_locked(self, radio: Si468xDabRadio, mode: str) -> None:
        radio.load_patch_only(self.config.patch_path, allow_cmd_error=True)
        time.sleep(0.004)
        radio.flash_load_and_boot(
            self._flash_firmware_addr_for_mode(mode),
            allow_cmd_error=True,
        )

    def _probe_firmware_locked(self, radio: Si468xDabRadio, mode: str) -> Dict[str, Any]:
        band = self._mode_info(mode)["band"]
        if band == "dab":
            status = radio.dab_digrad_status()
            status["probe"] = "dab_digrad_status"
            return status
        if band == "fm":
            status = radio.fm_rsq_status(attune=False)
            status["probe"] = "fm_rsq_status"
            return status
        status = radio.am_rsq_status(attune=False)
        status["probe"] = "am_rsq_status"
        return status

    def _program_flash_image_locked(
        self,
        radio: Si468xDabRadio,
        image_path: Path,
        start_addr: int,
        label: str,
    ) -> None:
        image_size = image_path.stat().st_size
        sectors = (image_size + FLASH_SECTOR_SIZE - 1) // FLASH_SECTOR_SIZE
        for sector in range(sectors):
            radio.flash_erase_sector(start_addr + (sector * FLASH_SECTOR_SIZE))
        written = 0
        with image_path.open("rb") as handle:
            while True:
                chunk = handle.read(FLASH_WRITE_BLOCK)
                if not chunk:
                    break
                radio.flash_write_block(start_addr + written, chunk)
                written += len(chunk)
        if written != image_size:
            raise RuntimeError(f"Incomplete flash write for {label}: {written}/{image_size} bytes")

    def _shutdown_locked(self, close_amp: bool) -> None:
        if close_amp:
            self._amp.close()
        else:
            try:
                self._amp.set_enabled(False)
            except Exception:
                pass
        if self._radio is not None:
            try:
                self._radio.close()
            except Exception:
                pass
        self._radio = None
        self._booted = False
        self._loaded_firmware = None
        self._reset_dab_media_locked(clear_artwork=True)

    def _require_radio_locked(self) -> Si468xDabRadio:
        if self._radio is None or not self._booted:
            raise RuntimeError("Radio is not booted.")
        return self._radio

    def _apply_audio_config_locked(self, radio: Si468xDabRadio) -> None:
        radio.configure_audio(
            mode=self._audio_out_mode,
            master=self.config.i2s_master,
            sample_rate=self.config.sample_rate,
            sample_size=self.config.sample_size,
        )

    def _apply_mute_locked(self, radio: Optional[Si468xDabRadio] = None) -> None:
        target = self._require_radio_locked() if radio is None else radio
        target.set_property(PROP_AUDIO_MUTE, 0x0003 if self._muted else 0x0000)

    def _toggle_mute_locked(self) -> None:
        self._boot_locked(force=False)
        self._muted = not self._muted
        self._apply_mute_locked()
        self._last_error = None
        self._save_runtime_state_locked()

    def _step_volume_locked(self, delta: int) -> None:
        self._boot_locked(force=False)
        radio = self._require_radio_locked()
        next_level = _clamp_int(self._current_volume + int(delta), 0, 63)
        if self._muted:
            self._muted = False
            self._apply_mute_locked(radio)
        self._current_volume = radio.set_volume(next_level)
        self._last_error = None
        self._save_runtime_state_locked()

    def _current_station_index_locked(self) -> Optional[int]:
        station = self._current_station
        if station is None:
            return None
        station_id = str(station.get("station_id") or "")
        for index, candidate in enumerate(self._stations_for_mode_locked()):
            if candidate.get("station_id") == station_id:
                return index
        return None

    def _step_station_locked(self, delta: int) -> bool:
        self._boot_locked(force=False)
        stations = self._stations_for_mode_locked()
        if not stations:
            self._last_error = (
                f"No saved {self._mode_info()['label']} stations available for button navigation. "
                "Scan stations first."
            )
            return False
        current_index = self._current_station_index_locked()
        if current_index is None:
            next_index = 0 if delta >= 0 else len(stations) - 1
        else:
            step = 1 if delta >= 0 else -1
            next_index = (current_index + step) % len(stations)
        station = dict(stations[next_index])
        if self._current_mode == "dab":
            self._play_dab_locked(station)
        else:
            self._play_analog_locked(station)
        self._last_error = None
        self._save_runtime_state_locked()
        return True

    def _cancel_nav_push_timer_locked(self) -> None:
        timer = self._nav_push_timer
        self._nav_push_timer = None
        if timer is not None:
            timer.cancel()

    def _cancel_nav_station_timer_locked(self) -> None:
        timer = self._nav_station_timer
        self._nav_station_timer = None
        if timer is not None:
            timer.cancel()

    def _arm_nav_push_timer_locked(self) -> None:
        self._cancel_nav_push_timer_locked()
        timer = threading.Timer(self.config.nav_combo_window_s, self._nav_push_timeout_fired)
        timer.daemon = True
        self._nav_push_timer = timer
        timer.start()

    def _arm_nav_station_timer_locked(self) -> None:
        self._cancel_nav_station_timer_locked()
        timer = threading.Timer(self.config.nav_station_timeout_s, self._nav_station_timeout_fired)
        timer.daemon = True
        self._nav_station_timer = timer
        timer.start()

    def _nav_push_timeout_fired(self) -> None:
        with self._lock:
            self._nav_push_timer = None
            if self._closing or not self._nav_pending_push:
                return
            self._nav_pending_push = False
            try:
                self._toggle_mute_locked()
            except Exception as exc:
                self._last_error = f"Button mute failed: {exc}"

    def _nav_station_timeout_fired(self) -> None:
        with self._lock:
            self._nav_station_timer = None
            self._nav_station_mode = False

    def _handle_nav_button_event(self, name: str, _event_time: float) -> None:
        with self._lock:
            if self._closing:
                return
            try:
                if name == "push":
                    self._nav_pending_push = True
                    self._arm_nav_push_timer_locked()
                    return
                step = 1 if name == "cw" else -1
                if self._nav_pending_push:
                    self._nav_pending_push = False
                    self._cancel_nav_push_timer_locked()
                    if self._step_station_locked(step):
                        self._nav_station_mode = True
                        self._arm_nav_station_timer_locked()
                    return
                if self._nav_station_mode:
                    if self._step_station_locked(step):
                        self._arm_nav_station_timer_locked()
                    return
                self._step_volume_locked(step)
            except Exception as exc:
                self._last_error = f"Button navigation failed: {exc}"

    def _scan_dab_locked(self) -> List[Dict[str, Any]]:
        radio = self._require_radio_locked()
        found: Dict[str, Dict[str, Any]] = {}
        for freq_index, freq_khz in enumerate(self._dab_freqs):
            radio.dab_tune(freq_index, antcap=self.config.antcap)
            status = self._wait_dab_ready_locked()
            if status is None:
                continue
            services = self._grab_dab_services_locked()
            for service in services:
                station = self._normalize_station(
                    "dab",
                    {
                        "service_id": service["service_id"],
                        "component_id": service["component_id"],
                        "label": service["label"],
                        "freq_index": freq_index,
                        "freq_khz": freq_khz,
                    },
                )
                station["score"] = _dab_score(status)
                found[station["station_id"]] = station
        return self._save_scan_file_locked("dab", list(found.values()))

    def _wait_dab_ready_locked(self, timeout_ms: Optional[int] = None) -> Optional[Dict[str, Any]]:
        radio = self._require_radio_locked()
        deadline = time.time() + ((timeout_ms or self.config.lock_ms) / 1000.0)
        last_status: Optional[Dict[str, Any]] = None
        while time.time() < deadline:
            status = radio.dab_digrad_status()
            last_status = dict(status)
            status["score"] = _dab_score(status)
            if status["valid"] and status["acq"] and (
                status["fic_quality"] > 0 or status["snr"] > 0 or status["cnr"] > 0
            ):
                return status
            time.sleep(0.05)
        return last_status if last_status and last_status.get("valid") and last_status.get("acq") else None

    def _grab_dab_services_locked(self) -> List[Dict[str, Any]]:
        radio = self._require_radio_locked()
        for _ in range(40):
            event_status = radio.dab_get_event_status(ack=False)
            if event_status["svrlist"]:
                radio.dab_get_event_status(ack=True)
                break
            time.sleep(0.1)
        return [dict(service) for service in radio.get_audio_services()]

    def _scan_analog_locked(self) -> List[Dict[str, Any]]:
        scan_key = self._scan_key()
        band = self._mode_info()["band"]
        require_hd = scan_key in {"hd", "am_hd"}
        if band == "fm" and not require_hd:
            return self._scan_fm_clusters_locked()
        if band == "am" and not require_hd:
            return self._scan_am_peaks_locked()
        found: Dict[int, Dict[str, Any]] = {}
        for freq_khz in self._analog_scan_frequencies_locked(band, require_hd):
            station = self._probe_analog_station_locked(freq_khz, band, require_hd)
            if station is None:
                continue
            existing = found.get(station["freq_khz"])
            if existing is None or int(station.get("score", 0)) > int(existing.get("score", 0)):
                found[station["freq_khz"]] = station
        return self._save_scan_file_locked(scan_key, list(found.values()))

    def _scan_am_peaks_locked(self) -> List[Dict[str, Any]]:
        radio = self._require_radio_locked()
        samples: List[Dict[str, Any]] = []
        for freq_khz in self._analog_scan_frequencies_locked("am", require_hd=False):
            radio.am_tune(freq_khz, antcap=self.config.antcap, tune_mode=0)
            time.sleep(0.25)
            signal = self._merge_fmhd_status(radio.am_rsq_status(attune=True), radio.hd_digrad_status())
            signal["freq_khz"] = int(signal.get("freq_khz") or freq_khz)
            samples.append(signal)

        stations: List[Dict[str, Any]] = []
        window = max(1, int(self.config.am_peak_window_channels))
        for index, signal in enumerate(samples):
            rssi = int(signal.get("rssi", 0))
            if rssi < int(self.config.am_peak_rssi_min):
                continue
            neighbors = samples[max(0, index - window):index] + samples[index + 1:index + window + 1]
            if not neighbors:
                continue
            neighbor_rssi = [int(item.get("rssi", 0)) for item in neighbors]
            if any(rssi < value for value in neighbor_rssi):
                continue
            prominence = rssi - (sum(neighbor_rssi) / len(neighbor_rssi))
            if prominence < float(self.config.am_peak_prominence):
                continue
            freq_khz = int(signal.get("freq_khz") or 0)
            radio.am_tune(freq_khz, antcap=self.config.antcap, tune_mode=0)
            confirmed = self._wait_am_signal_locked(require_hd=False)
            if confirmed is None:
                continue
            station = self._normalize_station(
                "am",
                {
                    "freq_khz": freq_khz,
                    "label": self._default_station_label("am", freq_khz, 0, False),
                    "analog_available": True,
                    "hd_available": False,
                    "program_mask": 0,
                    "program_id": 0,
                },
            )
            station["score"] = _analog_score(confirmed)
            stations.append(station)
        return self._save_scan_file_locked("am", stations)

    def _scan_fm_clusters_locked(self) -> List[Dict[str, Any]]:
        cluster_window_khz = 200
        stations: List[Dict[str, Any]] = []
        best_station: Optional[Dict[str, Any]] = None
        last_success_freq: Optional[int] = None

        def flush() -> None:
            nonlocal best_station, last_success_freq
            if best_station is not None:
                stations.append(best_station)
            best_station = None
            last_success_freq = None

        for freq_khz in self._analog_scan_frequencies_locked("fm", require_hd=False):
            station = self._probe_analog_station_locked(freq_khz, "fm", require_hd=False)
            if station is None:
                flush()
                continue
            if best_station is None:
                best_station = station
                last_success_freq = freq_khz
                continue
            if last_success_freq is not None and (freq_khz - last_success_freq) > cluster_window_khz:
                flush()
                best_station = station
                last_success_freq = freq_khz
                continue
            if int(station.get("score", 0)) > int(best_station.get("score", 0)):
                best_station = station
            last_success_freq = freq_khz
        flush()
        return self._save_scan_file_locked("fm", stations)

    def _analog_scan_frequencies_locked(self, band: str, require_hd: bool) -> List[int]:
        frequencies: List[int] = []
        if band == "fm":
            if require_hd and self._stations["fm"]:
                frequencies = sorted({int(station["freq_khz"]) for station in self._stations["fm"]})
            else:
                current = self.config.fm_min_khz
                while current <= self.config.fm_max_khz:
                    frequencies.append(current)
                    current += self.config.fm_step_khz
        else:
            if require_hd and self._stations["am"]:
                frequencies = sorted({int(station["freq_khz"]) for station in self._stations["am"]})
            else:
                current = self.config.am_min_khz
                while current <= self.config.am_max_khz:
                    frequencies.append(current)
                    current += self.config.am_step_khz
        return frequencies

    def _probe_analog_station_locked(self, freq_khz: int, band: str, require_hd: bool) -> Optional[Dict[str, Any]]:
        radio = self._require_radio_locked()
        tune_mode = 3 if band == "fm" and require_hd else 2 if band == "am" and require_hd else 0
        if band == "fm":
            radio.fm_tune(freq_khz, antcap=self.config.antcap, tune_mode=tune_mode)
            signal = self._wait_fm_signal_locked(require_hd=require_hd)
            if signal is None:
                return None
            analog_ready = self._is_fm_analog_ready(signal)
            hd_ready = self._is_hd_digital_ready(signal)
            if require_hd and not hd_ready:
                return None
            if not require_hd and not analog_ready:
                return None
            measured_freq = int(signal.get("freq_khz") or freq_khz)
            scan_key = "hd" if require_hd else "fm"
        else:
            radio.am_tune(freq_khz, antcap=self.config.antcap, tune_mode=tune_mode)
            signal = self._wait_am_signal_locked(require_hd=require_hd)
            if signal is None:
                return None
            analog_ready = self._is_am_analog_ready(signal)
            hd_ready = self._is_am_hd_ready(signal)
            if require_hd and not hd_ready:
                return None
            if not require_hd and not analog_ready:
                return None
            measured_freq = int(signal.get("freq_khz") or freq_khz)
            scan_key = "am_hd" if require_hd else "am"
        station = self._normalize_station(
            scan_key,
            {
                "freq_khz": measured_freq,
                "label": self._default_station_label(scan_key, measured_freq, int(signal.get("audio_program_available", 0)), hd_ready),
                "analog_available": analog_ready,
                "hd_available": hd_ready,
                "program_mask": int(signal.get("audio_program_available", 0)),
                "program_id": 0,
            },
        )
        station["score"] = _analog_score(signal)
        return station

    def _merge_fmhd_status(self, base: Dict[str, Any], hd: Dict[str, Any]) -> Dict[str, Any]:
        merged = dict(base)
        merged.update(hd)
        merged["hd_detected"] = bool(base.get("hd_detected") or hd.get("digital_source"))
        merged["score"] = _analog_score(merged)
        return merged

    def _wait_fm_signal_locked(self, require_hd: bool) -> Optional[Dict[str, Any]]:
        radio = self._require_radio_locked()
        if not require_hd:
            time.sleep(0.2)
            best: Optional[Dict[str, Any]] = None
            for _ in range(3):
                signal = self._merge_fmhd_status(radio.fm_rsq_status(attune=True), radio.hd_digrad_status())
                if best is None or signal["score"] > best["score"]:
                    best = signal
                time.sleep(0.05)
            if best and self._is_fm_analog_ready(best):
                return best
            return None
        deadline = time.time() + (self.config.fm_hd_timeout_ms / 1000.0)
        best = None
        while time.time() < deadline:
            signal = self._merge_fmhd_status(radio.fm_rsq_status(attune=True), radio.hd_digrad_status())
            if best is None or signal["score"] > best["score"]:
                best = signal
            if self._is_hd_digital_ready(signal):
                return signal
            time.sleep(0.08)
        return best if best and self._is_hd_digital_ready(best) else None

    def _wait_am_signal_locked(self, require_hd: bool) -> Optional[Dict[str, Any]]:
        radio = self._require_radio_locked()
        if not require_hd:
            time.sleep(0.35)
            best: Optional[Dict[str, Any]] = None
            for _ in range(5):
                signal = self._merge_fmhd_status(radio.am_rsq_status(attune=True), radio.hd_digrad_status())
                if best is None or signal["score"] > best["score"]:
                    best = signal
                time.sleep(0.08)
            if best and self._is_am_analog_ready(best):
                return best
            if best and int(best.get("snr", -128)) <= -120 and int(best.get("rssi", 0)) > 0:
                return best
            return None
        deadline = time.time() + (self.config.am_hd_timeout_ms / 1000.0)
        best = None
        while time.time() < deadline:
            signal = self._merge_fmhd_status(radio.am_rsq_status(attune=True), radio.hd_digrad_status())
            if best is None or signal["score"] > best["score"]:
                best = signal
            if self._is_am_hd_ready(signal):
                return signal
            time.sleep(0.08)
        return best if best and self._is_am_hd_ready(best) else None

    def _is_fm_analog_ready(self, signal: Dict[str, Any]) -> bool:
        return int(signal.get("rssi", 0)) >= self.config.fm_rssi_min and int(signal.get("snr", 0)) >= self.config.fm_snr_min

    def _is_hd_digital_ready(self, signal: Dict[str, Any]) -> bool:
        return (
            int(signal.get("hdlevel", 0)) >= 20
            and bool(signal.get("acq"))
            and bool(signal.get("digital_source"))
            and int(signal.get("audio_program_available", 0)) > 0
        )

    def _is_am_analog_ready(self, signal: Dict[str, Any]) -> bool:
        if int(signal.get("snr", -128)) <= -120:
            return int(signal.get("rssi", 0)) >= self.config.am_peak_rssi_min
        return (
            bool(signal.get("valid"))
            and int(signal.get("rssi", 0)) >= self.config.am_rssi_min
            and int(signal.get("snr", 0)) >= self.config.am_snr_min
        )

    def _is_am_hd_ready(self, signal: Dict[str, Any]) -> bool:
        return (
            (bool(signal.get("hd_detected")) or bool(signal.get("digital_source")))
            and bool(signal.get("acq"))
            and int(signal.get("audio_program_available", 0)) > 0
        )

    def _resolve_station_locked(
        self,
        *,
        index: Optional[int],
        label: Optional[str],
        station_id: Optional[str],
    ) -> Dict[str, Any]:
        if station_id:
            for scan_key in SCAN_KEYS:
                for station in self._stations[scan_key]:
                    if station["station_id"] == station_id:
                        return dict(station)
            raise ValueError(f"Unknown station_id: {station_id}")
        stations = self._stations_for_mode_locked()
        if index is not None:
            idx = int(index)
            if idx < 0 or idx >= len(stations):
                raise ValueError(f"Station index {idx} is out of range.")
            return dict(stations[idx])
        if label:
            needle = str(label).strip().casefold()
            exact = next((station for station in stations if station["label"].casefold() == needle), None)
            if exact is not None:
                return dict(exact)
            partial = next((station for station in stations if needle in station["label"].casefold()), None)
            if partial is not None:
                return dict(partial)
            raise ValueError(f"Station not found: {label}")
        raise ValueError("One of index, label or station_id is required.")

    def _ensure_station_playing_locked(self, station: Dict[str, Any]) -> Dict[str, Any]:
        if station["mode"] != self._current_mode:
            self._set_mode_locked(station["mode"])
        self._boot_locked(force=False)
        if not self._stations_for_mode_locked():
            self.scan(force=False)
        current_station_id = str(self._current_station.get("station_id")) if self._current_station else None
        if current_station_id == station["station_id"]:
            return dict(self._current_station or station)
        if self._current_mode == "dab":
            self._play_dab_locked(station)
        else:
            self._play_analog_locked(station)
        self._last_error = None
        self._save_runtime_state_locked()
        return dict(self._current_station or station)

    def _play_dab_locked(self, station: Dict[str, Any]) -> None:
        radio = self._require_radio_locked()
        if self._current_station and self._current_station.get("mode") == "dab":
            try:
                radio.stop_digital_service(
                    int(self._current_station["service_id"]),
                    int(self._current_station["component_id"]),
                )
            except Exception:
                pass
        self._reset_dab_media_locked(clear_artwork=True)
        radio.dab_tune(int(station["freq_index"]), antcap=self.config.antcap)
        status = self._wait_dab_ready_locked(timeout_ms=max(self.config.lock_ms, 8000))
        if status is None:
            raise RuntimeError(f"Failed to lock DAB service {station['label']}.")
        radio.start_digital_service(int(station["service_id"]), int(station["component_id"]))
        self._current_station = dict(station)
        self._last_signal = dict(status)
        self._last_signal["score"] = _dab_score(status)
        self._apply_mute_locked(radio)
        self._poll_dab_media_locked()

    def _play_analog_locked(self, station: Dict[str, Any]) -> None:
        radio = self._require_radio_locked()
        self._reset_dab_media_locked(clear_artwork=True)
        self._apply_mute_locked(radio)
        band = station["band"]
        require_hd = station["mode"] in {"hd", "am_hd"}
        tune_mode = 3 if band == "fm" and require_hd else 2 if band == "am" and require_hd else 0
        if band == "fm":
            radio.fm_tune(int(station["freq_khz"]), antcap=self.config.antcap, tune_mode=tune_mode)
            signal = self._wait_fm_signal_locked(require_hd=require_hd)
        else:
            radio.am_tune(int(station["freq_khz"]), antcap=self.config.antcap, tune_mode=tune_mode)
            signal = self._wait_am_signal_locked(require_hd=require_hd)
        if signal is None:
            raise RuntimeError(f"Failed to tune {station['label']}.")
        self._current_station = dict(station)
        self._last_signal = dict(signal)
        self._apply_mute_locked(radio)

    def _read_current_signal_locked(self) -> Dict[str, Any]:
        radio = self._require_radio_locked()
        station = self._current_station
        if station is None:
            return {}
        if station["mode"] == "dab":
            signal = radio.dab_digrad_status()
            signal["score"] = _dab_score(signal)
            return signal
        if station["band"] == "fm":
            return self._merge_fmhd_status(radio.fm_rsq_status(attune=True), radio.hd_digrad_status())
        return self._merge_fmhd_status(radio.am_rsq_status(attune=True), radio.hd_digrad_status())

    def _recording_active_locked(self) -> bool:
        return self._recording_process is not None and self._recording_process.poll() is None

    def _recording_payload_locked(self) -> Dict[str, Any]:
        self._refresh_recording_state_locked()
        if self._recording_meta is None:
            return {"active": False}
        payload = self._recording_public_meta(self._recording_meta)
        payload["active"] = self._recording_active_locked()
        if payload["active"]:
            payload["elapsed_seconds"] = round(time.time() - float(self._recording_meta["_started_epoch"]), 1)
        return payload

    def _refresh_recording_state_locked(self) -> None:
        if self._recording_process is None:
            return
        if self._recording_process.poll() is None:
            return
        stderr = ""
        try:
            _, err = self._recording_process.communicate(timeout=0.2)
            stderr = (err or "").strip()
        except Exception:
            pass
        self._finalize_recording_locked(stderr=stderr)

    def _start_recording_locked(self) -> None:
        if self._recording_active_locked():
            return
        if shutil.which("arecord") is None:
            raise RuntimeError("arecord is not installed on the Raspberry Pi.")
        self._boot_locked(force=False)
        if self._current_station is None:
            raise RuntimeError("Tune a station before starting a recording.")
        if self._audio_out_mode not in {"i2s", "both"}:
            self._audio_out_mode = "both"
            self._apply_audio_config_locked(self._require_radio_locked())
        timestamp = datetime.now()
        station_label = self._current_station["label"]
        base_name = f"{timestamp.strftime('%Y%m%d-%H%M%S')}_{self._current_mode}_{_sanitize_filename(station_label)}"
        audio_path = self.config.recordings_dir / f"{base_name}.wav"
        meta_path = self.config.recordings_dir / f"{base_name}.json"
        meta = {
            "file_name": audio_path.name,
            "file_path": str(audio_path),
            "meta_name": meta_path.name,
            "started_at": timestamp.isoformat(timespec="seconds"),
            "mode": self._current_mode,
            "station_id": self._current_station["station_id"],
            "station_label": station_label,
            "sample_rate": self.config.sample_rate,
            "channels": self.config.record_channels,
            "format": self.config.record_format,
            "device": self._resolve_record_device_locked(),
            "_started_epoch": time.time(),
        }
        command = [
            "arecord",
            "-q",
            "-D",
            meta["device"],
            "-f",
            self.config.record_format,
            "-r",
            str(self.config.sample_rate),
            "-c",
            str(self.config.record_channels),
            "-t",
            "wav",
            str(audio_path),
        ]
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.25)
        if process.poll() is not None:
            stderr = ""
            try:
                _, stderr = process.communicate(timeout=0.5)
            except Exception:
                pass
            raise RuntimeError(
                f"Recording failed to start on ALSA device {meta['device']}. Check the I2S capture path."
                + (f" Details: {stderr.strip()}" if stderr and stderr.strip() else "")
            )
        self._recording_process = process
        self._recording_meta = meta
        self._write_recording_meta_locked(meta)

    def _resolve_record_device_locked(self) -> str:
        configured = str(self.config.record_device or "").strip()
        if configured and configured.casefold() != "auto":
            return configured
        return _auto_detect_record_device()

    def _resolve_live_stream_device_locked(self) -> str:
        return _resolve_shared_capture_device(self._resolve_record_device_locked())

    def prepare_live_stream(self, station_id: Optional[str] = None, auto_tune: bool = False) -> Dict[str, Any]:
        with self._lock:
            return self._prepare_live_stream_locked(station_id=station_id, auto_tune=auto_tune)

    def _prepare_live_stream_locked(
        self,
        *,
        station_id: Optional[str] = None,
        auto_tune: bool = False,
    ) -> Dict[str, Any]:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is not installed on the Raspberry Pi.")
        self._boot_locked(force=False)
        selected_station: Optional[Dict[str, Any]] = None
        if station_id:
            selected_station = self._resolve_station_locked(index=None, label=None, station_id=station_id)
            if auto_tune:
                selected_station = self._ensure_station_playing_locked(selected_station)
        elif self._current_station is not None:
            selected_station = dict(self._current_station)
        if selected_station is None:
            raise RuntimeError("Tune a station before requesting the live stream.")
        if self._audio_out_mode not in {"i2s", "both"}:
            self._audio_out_mode = "both"
            self._apply_audio_config_locked(self._require_radio_locked())
        return {
            "device": self._resolve_live_stream_device_locked(),
            "sample_rate": self.config.sample_rate,
            "channels": self.config.record_channels,
            "format": self.config.record_format,
            "ffmpeg_input_format": str(self.config.record_format).replace("_", "").lower(),
            "station_label": selected_station["label"],
            "station_id": selected_station["station_id"],
            "mode": selected_station["mode"],
        }

    def _stop_recording_locked(self) -> None:
        if self._recording_process is None:
            self._recording_meta = None
            return
        stderr = ""
        if self._recording_process.poll() is None:
            self._recording_process.terminate()
            try:
                _, stderr = self._recording_process.communicate(timeout=3.0)
            except subprocess.TimeoutExpired:
                self._recording_process.kill()
                _, stderr = self._recording_process.communicate(timeout=1.0)
        else:
            try:
                _, stderr = self._recording_process.communicate(timeout=0.2)
            except Exception:
                pass
        self._finalize_recording_locked(stderr=stderr.strip())

    def _finalize_recording_locked(self, stderr: str = "") -> None:
        meta = self._recording_meta
        self._recording_process = None
        if meta is None:
            return
        finished_at = datetime.now()
        meta["finished_at"] = finished_at.isoformat(timespec="seconds")
        meta["duration_seconds"] = round(time.time() - float(meta["_started_epoch"]), 1)
        trimmed = _trim_wav_leading_seconds(Path(meta["file_path"]), self.config.record_trim_leading_seconds)
        if trimmed > 0:
            meta["trimmed_leading_seconds"] = trimmed
        actual_duration = _wav_duration_seconds(Path(meta["file_path"]))
        if actual_duration is not None:
            meta["duration_seconds"] = actual_duration
        if stderr:
            meta["note"] = stderr
        self._write_recording_meta_locked(meta)
        self._recording_meta = None

    def _write_recording_meta_locked(self, meta: Dict[str, Any]) -> None:
        payload = {key: value for key, value in meta.items() if not key.startswith("_")}
        meta_path = self.config.recordings_dir / str(meta["meta_name"])
        meta_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _list_recordings_locked(self) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        self.config.recordings_dir.mkdir(parents=True, exist_ok=True)
        for audio_path in sorted(self.config.recordings_dir.glob("*.wav"), key=lambda p: p.stat().st_mtime, reverse=True):
            meta_path = audio_path.with_suffix(".json")
            info: Dict[str, Any]
            if meta_path.exists():
                try:
                    info = json.loads(meta_path.read_text(encoding="utf-8"))
                except Exception:
                    info = {}
            else:
                info = {}
            info.setdefault("file_name", audio_path.name)
            info.setdefault("file_path", str(audio_path))
            info.setdefault("started_at", datetime.fromtimestamp(audio_path.stat().st_mtime).isoformat(timespec="seconds"))
            info.setdefault("station_label", audio_path.stem)
            info["url"] = f"/recordings/{audio_path.name}"
            info["size_bytes"] = audio_path.stat().st_size
            active = self._recording_meta is not None and self._recording_meta.get("file_name") == audio_path.name and self._recording_active_locked()
            info["active"] = active
            items.append(info)
        return items

    def _recording_public_meta(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "file_name": meta["file_name"],
            "file_path": meta["file_path"],
            "started_at": meta["started_at"],
            "mode": meta["mode"],
            "station_id": meta["station_id"],
            "station_label": meta["station_label"],
            "sample_rate": meta["sample_rate"],
            "channels": meta["channels"],
            "format": meta["format"],
            "device": meta["device"],
            "url": f"/recordings/{meta['file_name']}",
        }
