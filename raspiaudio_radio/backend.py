from __future__ import annotations

import atexit
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from legacy.dab_radio_i2c_safe2 import (
    DAB_BAND_III,
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
    return (
        str(station.get("label", "")).casefold(),
        int(station.get("freq_khz") or 0),
        str(station.get("station_id", "")),
    )


def _sanitize_filename(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-_")[:64] or "recording"


def _iso_or_none(timestamp: Optional[float]) -> Optional[str]:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")


@dataclass(frozen=True)
class RadioConfig:
    patch_path: Path
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
    i2c_bus: int = 1
    i2c_addr: int = 0x64
    spi_bus: int = 0
    spi_dev: int = 0
    spi_speed_hz: int = 30_000_000
    rst_pin: int = 25
    int_pin: Optional[int] = None
    amp_pin: Optional[int] = 17
    amp_active_high: bool = True
    audio_out: str = "both"
    i2s_master: bool = True
    sample_rate: int = 48_000
    sample_size: int = 16
    xtal_freq: int = 19_200_000
    ctun: int = 0x07
    antcap: int = 0
    lock_ms: int = 5000
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
    am_rssi_min: int = 4
    am_snr_min: int = 0
    am_hd_timeout_ms: int = 2500
    record_device: str = "default"
    record_channels: int = 2
    record_format: str = "S16_LE"


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


class RadioBackend:
    def __init__(self, config: RadioConfig) -> None:
        self.config = config
        self._lock = threading.RLock()
        self._radio: Optional[Si468xDabRadio] = None
        self._amp = AmplifierGate(config.amp_pin, config.amp_active_high)
        self._amp_requested = True
        self._booted = False
        self._loaded_firmware: Optional[str] = None
        self._audio_out_mode = config.audio_out if config.audio_out in VALID_AUDIO_OUT else "both"
        self._current_mode = config.default_mode if config.default_mode in MODE_DEFS else "dab"
        self._current_station: Optional[Dict[str, Any]] = None
        self._current_volume = _clamp_int(config.default_volume, 0, 63)
        self._last_signal: Optional[Dict[str, Any]] = None
        self._last_error: Optional[str] = None
        self._stations: Dict[str, List[Dict[str, Any]]] = {key: [] for key in SCAN_KEYS}
        self._last_scan_count: Dict[str, int] = {key: 0 for key in SCAN_KEYS}
        self._last_scan_time: Dict[str, Optional[float]] = {key: None for key in SCAN_KEYS}
        self._favorites: set[str] = set()
        self._recording_process: Optional[subprocess.Popen[str]] = None
        self._recording_meta: Optional[Dict[str, Any]] = None
        self._dab_freqs = [freq for _, freq in DAB_BAND_III]
        self._dab_freq_index = {freq: idx for idx, freq in enumerate(self._dab_freqs)}
        self._load_scan_files_locked()
        self._load_favorites_locked()
        self.config.recordings_dir.mkdir(parents=True, exist_ok=True)
        atexit.register(self.close)

    def boot(self, mode: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        with self._lock:
            if mode is not None:
                self._set_mode_locked(mode)
            return self._boot_locked(force=force)

    def set_mode(self, mode: str) -> Dict[str, Any]:
        with self._lock:
            self._set_mode_locked(mode)
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
            if station["mode"] != self._current_mode:
                self._set_mode_locked(station["mode"])
            self._boot_locked(force=False)
            if not self._stations_for_mode_locked():
                self.scan(force=False)
            if self._current_mode == "dab":
                self._play_dab_locked(station)
            else:
                self._play_analog_locked(station)
            self._last_error = None
            return self._status_payload_locked(refresh_signal=True)

    def set_volume(self, *, level: Optional[int] = None, delta: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            self._boot_locked(force=False)
            radio = self._require_radio_locked()
            next_level = self._current_volume if level is None else int(level)
            if delta is not None:
                next_level += int(delta)
            self._current_volume = radio.set_volume(_clamp_int(next_level, 0, 63))
            return self._status_payload_locked(refresh_signal=False)

    def set_amplifier(self, enabled: bool) -> Dict[str, Any]:
        with self._lock:
            self._amp_requested = bool(enabled)
            if self._booted:
                self._amp.set_enabled(self._amp_requested)
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

    def close(self) -> None:
        with self._lock:
            self._stop_recording_locked()
            self._shutdown_locked(close_amp=True)

    def _set_mode_locked(self, mode: str) -> None:
        normalized = str(mode).strip().lower().replace("-", "_")
        if normalized not in MODE_DEFS:
            raise ValueError(f"Unsupported mode: {mode}")
        if normalized == self._current_mode:
            return
        if self._recording_active_locked():
            self._stop_recording_locked()
        self._current_mode = normalized
        self._current_station = None
        self._last_signal = None

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

    def _status_payload_locked(self, refresh_signal: bool) -> Dict[str, Any]:
        self._refresh_recording_state_locked()
        if refresh_signal and self._booted and self._current_station is not None:
            try:
                self._last_signal = self._read_current_signal_locked()
                self._last_error = None
            except Exception as exc:
                self._last_error = str(exc)
        scan_key = self._scan_key()
        return {
            "booted": self._booted,
            "transport": "spi",
            "mode": self._current_mode,
            "mode_label": self._mode_info()["label"],
            "available_modes": [dict(MODE_DEFS[key]) for key in SCAN_KEYS],
            "firmware": self._loaded_firmware,
            "audio_out": self._audio_out_mode,
            "volume": self._current_volume,
            "amp_enabled": self._amp.enabled if self._booted else False,
            "amp_requested": self._amp_requested,
            "amp_pin": self.config.amp_pin,
            "current_station": self._decorate_station_locked(self._current_station) if self._current_station else None,
            "signal": dict(self._last_signal or {}),
            "station_count": len(self._stations[scan_key]),
            "favorite_count": len(self._favorites),
            "scan_key": scan_key,
            "last_scan_count": self._last_scan_count[scan_key],
            "last_scan_time": _iso_or_none(self._last_scan_time[scan_key]),
            "recording": self._recording_payload_locked(),
            "recordings_count": len(self._list_recordings_locked()),
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
            radio = Si468xDabRadio(
                i2c_bus=self.config.i2c_bus,
                i2c_addr=self.config.i2c_addr,
                rst_pin=self.config.rst_pin,
                int_pin=self.config.int_pin,
                use_spi=True,
                spi_bus=self.config.spi_bus,
                spi_dev=self.config.spi_dev,
                spi_speed_hz=self.config.spi_speed_hz,
            )
            try:
                radio.reset()
                radio.power_up(xtal_freq=self.config.xtal_freq, ctun=self.config.ctun, retries=2)
                radio.load_patch_and_firmware(self.config.patch_path, self._firmware_path_for_mode())
                if self._current_mode == "dab":
                    radio.configure_dab_frontend()
                    radio.set_dab_freq_list(self._dab_freqs)
                elif self._mode_info()["band"] == "fm":
                    radio.configure_fmhd_frontend()
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
        self._amp.set_enabled(self._amp_requested)
        return self._status_payload_locked(refresh_signal=False)

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
        found: Dict[int, Dict[str, Any]] = {}
        for freq_khz in self._analog_scan_frequencies_locked(band, require_hd):
            station = self._probe_analog_station_locked(freq_khz, band, require_hd)
            if station is None:
                continue
            existing = found.get(station["freq_khz"])
            if existing is None or int(station.get("score", 0)) > int(existing.get("score", 0)):
                found[station["freq_khz"]] = station
        return self._save_scan_file_locked(scan_key, list(found.values()))

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
            time.sleep(0.2)
            best: Optional[Dict[str, Any]] = None
            for _ in range(3):
                signal = self._merge_fmhd_status(radio.am_rsq_status(attune=True), radio.hd_digrad_status())
                if best is None or signal["score"] > best["score"]:
                    best = signal
                time.sleep(0.05)
            if best and self._is_am_analog_ready(best):
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
        return int(signal.get("rssi", 0)) >= self.config.am_rssi_min and int(signal.get("snr", 0)) >= self.config.am_snr_min

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
        radio.dab_tune(int(station["freq_index"]), antcap=self.config.antcap)
        status = self._wait_dab_ready_locked(timeout_ms=max(self.config.lock_ms, 8000))
        if status is None:
            raise RuntimeError(f"Failed to lock DAB service {station['label']}.")
        radio.start_digital_service(int(station["service_id"]), int(station["component_id"]))
        self._current_station = dict(station)
        self._last_signal = dict(status)
        self._last_signal["score"] = _dab_score(status)

    def _play_analog_locked(self, station: Dict[str, Any]) -> None:
        radio = self._require_radio_locked()
        radio.set_property(PROP_AUDIO_MUTE, 0)
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
            "device": self.config.record_device,
            "_started_epoch": time.time(),
        }
        command = [
            "arecord",
            "-q",
            "-D",
            self.config.record_device,
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
                "Recording failed to start. Check the ALSA capture device for I2S input."
                + (f" Details: {stderr.strip()}" if stderr and stderr.strip() else "")
            )
        self._recording_process = process
        self._recording_meta = meta
        self._write_recording_meta_locked(meta)

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
