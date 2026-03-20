from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_URL = os.environ.get("RASPIAUDIO_RADIO_URL", "http://127.0.0.1:8686")
REPO_ROOT = Path(__file__).resolve().parent.parent
FW_ROOT = REPO_ROOT / "firmwares"
MODE_CHOICES = ["dab", "fm", "hd", "am", "am_hd"]


def _request(
    base_url: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: int = 120,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="ignore")
        try:
            payload = json.loads(raw)
            raise SystemExit(payload.get("error", raw) or raw)
        except json.JSONDecodeError:
            raise SystemExit(raw or str(exc)) from exc
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"Unable to reach the radio server at {base_url}. "
            "Start it with `python radio.py serve` on the Raspberry Pi."
        ) from exc
    payload = json.loads(raw)
    if not payload.get("ok", False):
        raise SystemExit(payload.get("error", "Unknown API error."))
    return payload.get("data")


def _format_station_line(index: int, station: Dict[str, Any]) -> str:
    freq_khz = int(station.get("freq_khz") or 0)
    if station.get("band") == "fm":
        freq_label = f"{freq_khz / 1000.0:.1f} MHz"
    else:
        freq_label = f"{freq_khz} kHz" if freq_khz > 0 else "freq ?"
    tags = [station.get("mode_label", station.get("mode", "")).upper()]
    if station.get("favorite"):
        tags.append("FAV")
    if station.get("is_current"):
        tags.append("LIVE")
    return f"[{index:02d}] {station.get('label', '')} | {freq_label} | {' '.join(tags)} | {station.get('station_id', '')}"


def _print_status(status: Dict[str, Any]) -> None:
    current = status.get("current_station") or {}
    signal = status.get("signal") or {}
    recording = status.get("recording") or {"active": False}
    dab_media = status.get("dab_media") or {}
    print(f"booted: {'yes' if status.get('booted') else 'no'}")
    print(f"mode: {status.get('mode_label')} ({status.get('mode')})")
    print(f"firmware: {status.get('firmware')}")
    print(f"audio_out: {status.get('audio_out')}")
    print(f"volume: {status.get('volume')}/63")
    print(f"amplifier: {'on' if status.get('amp_enabled') else 'off'} (GPIO {status.get('amp_pin')})")
    print(f"station: {current.get('label', 'None')}")
    if signal:
        print(
            "signal: "
            f"RSSI={signal.get('rssi')} "
            f"SNR={signal.get('snr')} "
            f"FICQ={signal.get('fic_quality')} "
            f"CNR={signal.get('cnr')} "
            f"SCORE={signal.get('score')}"
        )
    if status.get("mode") == "dab":
        if dab_media.get("artist"):
            print(f"dab_artist: {dab_media['artist']}")
        if dab_media.get("title"):
            print(f"dab_title: {dab_media['title']}")
        if dab_media.get("text"):
            print(f"dab_text: {dab_media['text']}")
        elif current.get("label"):
            print("dab_text: unavailable")
    if recording.get("active"):
        print(
            "recording: "
            f"{recording.get('file_name')} "
            f"({recording.get('station_label')}, {recording.get('elapsed_seconds', 0)}s)"
        )
    else:
        print("recording: off")
    if status.get("last_error"):
        print(f"last_error: {status['last_error']}")


def _print_flash_report(report: Dict[str, Any]) -> None:
    print(f"status: {report.get('status')}")
    print(f"programmed: {'yes' if report.get('programmed') else 'no'}")
    if report.get("bootable") is not None:
        print(f"bootable: {'yes' if report.get('bootable') else 'no'}")
    print(f"mode: {report.get('mode_label')} ({report.get('mode')})")
    print(f"firmware_key: {report.get('firmware_key')}")
    print(f"patch_image: {report.get('patch_image')}")
    print(f"mini_patch_image: {report.get('mini_patch_image')}")
    print(f"firmware_image: {report.get('firmware_image')}")
    print(f"flash_patch_addr: 0x{int(report.get('flash_patch_addr', 0)):08X}")
    print(f"flash_firmware_addr: 0x{int(report.get('flash_firmware_addr', 0)):08X}")
    self_test = report.get("self_test") or []
    if not self_test:
        print("self_test: skipped")
    for item in self_test:
        status = "PASS" if item.get("ok") else "FAIL"
        line = f"self_test[{item.get('method')}]: {status}"
        if item.get("error"):
            line += f" | {item['error']}"
        if item.get("probe"):
            line += f" | probe={item['probe']}"
        print(line)
    restored = report.get("restored_status")
    if isinstance(restored, dict):
        print(f"restored_mode: {restored.get('mode_label')} ({restored.get('mode')})")
        print(f"restored_station: {(restored.get('current_station') or {}).get('label', 'None')}")
    if report.get("restore_error"):
        print(f"restore_error: {report['restore_error']}")
    if report.get("error"):
        print(f"error: {report['error']}")


def _fetch_stations(base_url: str, mode: Optional[str] = None) -> List[Dict[str, Any]]:
    suffix = f"?mode={mode}" if mode else ""
    return _request(base_url, "GET", f"/api/stations{suffix}").get("stations", [])


def _resolve_station_target(base_url: str, target: str, mode: Optional[str] = None) -> Dict[str, Any]:
    value = target.strip()
    if value.isdigit():
        return {"index": int(value)}
    if ":" in value and value.split(":", 1)[0] in set(MODE_CHOICES):
        return {"station_id": value}
    return {"label": value}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Raspiaudio radio CLI (SPI backend + web UI).")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Radio server base URL (default: {DEFAULT_URL})")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve = subparsers.add_parser("serve", help="Start the local HTTP backend and web UI.")
    serve.add_argument("--host", default="0.0.0.0", help="HTTP bind address (default: 0.0.0.0)")
    serve.add_argument("--port", type=int, default=8686, help="HTTP port (default: 8686)")
    serve.add_argument("--alias", default="piradio", help="Suggested local network alias to display at startup")
    serve.add_argument("--patch", type=Path, default=FW_ROOT / "rom00_patch.016.bin")
    serve.add_argument("--mini-patch", type=Path, default=FW_ROOT / "rom00_patch_mini.003.bin")
    serve.add_argument("--dab-fw", type=Path, default=FW_ROOT / "dab_radio_6_0_9.bin")
    serve.add_argument("--fmhd-fw", type=Path, default=FW_ROOT / "fmhd_radio_5_3_3.bin")
    serve.add_argument("--amhd-fw", type=Path, default=FW_ROOT / "amhd_radio_3_0_6.bin")
    serve.add_argument("--dab-scan", type=Path, default=REPO_ROOT / "full_scan.txt")
    serve.add_argument("--fm-scan", type=Path, default=REPO_ROOT / "fm_scan.txt")
    serve.add_argument("--hd-scan", type=Path, default=REPO_ROOT / "hd_scan.txt")
    serve.add_argument("--am-scan", type=Path, default=REPO_ROOT / "am_scan.txt")
    serve.add_argument("--am-hd-scan", type=Path, default=REPO_ROOT / "am_hd_scan.txt")
    serve.add_argument("--favorites-file", type=Path, default=REPO_ROOT / "favorites.json")
    serve.add_argument("--recordings-dir", type=Path, default=REPO_ROOT / "recordings")
    serve.add_argument("--spi-bus", type=int, default=0)
    serve.add_argument("--spi-dev", type=int, default=0)
    serve.add_argument("--spi-speed", type=int, default=30_000_000)
    serve.add_argument("--flash-program-spi-speed", type=int, default=1_000_000, help="Temporary SPI speed used for flash programming")
    serve.add_argument("--rst-pin", type=int, default=25)
    serve.add_argument("--amp-pin", type=int, default=17, help="BCM GPIO used to enable the speaker amplifier")
    serve.add_argument("--disable-amp", action="store_true", help="Disable amplifier GPIO control")
    serve.add_argument("--amp-active-low", action="store_true", help="Treat amplifier GPIO as active-low")
    serve.add_argument("--audio-out", choices=["analog", "i2s", "both"], default="both")
    serve.add_argument("--sample-rate", type=int, default=48_000)
    serve.add_argument("--sample-size", type=int, default=16)
    serve.add_argument("--xtal", type=lambda x: int(x, 0), default=19_200_000)
    serve.add_argument("--ctun", type=lambda x: int(x, 0), default=0x07)
    serve.add_argument("--antcap", type=lambda x: int(x, 0), default=0)
    serve.add_argument("--lock-ms", type=int, default=5000)
    serve.add_argument("--flash-patch-addr", type=lambda x: int(x, 0), default=0x00004000)
    serve.add_argument("--flash-dab-addr", type=lambda x: int(x, 0), default=0x00092000)
    serve.add_argument("--flash-fmhd-addr", type=lambda x: int(x, 0), default=0x00006000)
    serve.add_argument("--flash-amhd-addr", type=lambda x: int(x, 0), default=0x0011E000)
    serve.add_argument("--volume", type=int, default=40, help="Initial analog volume 0-63")
    serve.add_argument("--mode", choices=MODE_CHOICES, default="dab", help="Startup mode")
    serve.add_argument("--record-device", default="default", help="ALSA capture device for recordings")

    boot = subparsers.add_parser("boot", help="Initialize the radio on the server.")
    boot.add_argument("--mode", choices=MODE_CHOICES)

    mode = subparsers.add_parser("mode", help="Switch source mode.")
    mode.add_argument("mode", choices=MODE_CHOICES)

    scan = subparsers.add_parser("scan", help="Scan stations for the active mode.")
    scan.add_argument("--mode", choices=MODE_CHOICES)
    scan.add_argument("--no-force", action="store_true", help="Reuse cached results if available")

    stations = subparsers.add_parser("stations", help="List known stations for a mode.")
    stations.add_argument("--mode", choices=MODE_CHOICES)
    stations.add_argument("--json", action="store_true", help="Print raw JSON")

    subparsers.add_parser("favorites", help="List favorite stations.")

    play = subparsers.add_parser("play", help="Play a station by index, label, or station_id.")
    play.add_argument("target")

    volume = subparsers.add_parser("volume", help="Set the volume or apply a delta.")
    volume.add_argument("value", help="Examples: 40, +4, -6")

    amp = subparsers.add_parser("amp", help="Turn the speaker amplifier on or off.")
    amp.add_argument("state", choices=["on", "off"])

    favorite = subparsers.add_parser("favorite", help="Toggle or set favorite on a station.")
    favorite.add_argument("target", help="Station index in current mode, or station_id")
    favorite.add_argument("--off", action="store_true", help="Remove from favorites instead of toggling")

    record = subparsers.add_parser("record", help="Start, stop, or toggle recording.")
    record.add_argument("action", choices=["start", "stop", "toggle"], nargs="?", default="toggle")

    flash = subparsers.add_parser("flash", help="Program firmware into SPI flash via the Si4689 and run a self-test.")
    flash.add_argument("mode", choices=MODE_CHOICES, nargs="?", default="dab")
    flash.add_argument("--no-self-test", action="store_true", help="Skip the flash boot self-test after programming")
    flash.add_argument("--json", action="store_true", help="Print raw JSON")

    recordings = subparsers.add_parser("recordings", help="List saved recordings.")
    recordings.add_argument("--json", action="store_true", help="Print raw JSON")

    status = subparsers.add_parser("status", help="Show current status.")
    status.add_argument("--json", action="store_true", help="Print raw JSON")

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .backend import RadioConfig
        from .server import run_server

        config = RadioConfig(
            patch_path=args.patch.resolve(),
            mini_patch_path=args.mini_patch.resolve(),
            dab_firmware_path=args.dab_fw.resolve(),
            fmhd_firmware_path=args.fmhd_fw.resolve(),
            amhd_firmware_path=args.amhd_fw.resolve(),
            dab_scan_file=args.dab_scan.resolve(),
            fm_scan_file=args.fm_scan.resolve(),
            hd_scan_file=args.hd_scan.resolve(),
            am_scan_file=args.am_scan.resolve(),
            am_hd_scan_file=args.am_hd_scan.resolve(),
            favorites_file=args.favorites_file.resolve(),
            recordings_dir=args.recordings_dir.resolve(),
            spi_bus=args.spi_bus,
            spi_dev=args.spi_dev,
            spi_speed_hz=args.spi_speed,
            flash_program_spi_hz=args.flash_program_spi_speed,
            rst_pin=args.rst_pin,
            amp_pin=None if args.disable_amp else args.amp_pin,
            amp_active_high=not args.amp_active_low,
            audio_out=args.audio_out,
            sample_rate=args.sample_rate,
            sample_size=args.sample_size,
            xtal_freq=args.xtal,
            ctun=args.ctun,
            antcap=args.antcap,
            lock_ms=args.lock_ms,
            flash_patch_addr=args.flash_patch_addr,
            flash_dab_addr=args.flash_dab_addr,
            flash_fmhd_addr=args.flash_fmhd_addr,
            flash_amhd_addr=args.flash_amhd_addr,
            default_volume=args.volume,
            default_mode=args.mode,
            record_device=args.record_device,
        )
        run_server(config=config, host=args.host, port=args.port, alias=args.alias)
        return

    if args.command == "boot":
        status = _request(args.url, "POST", "/api/boot", {"mode": args.mode, "force": False})
        _print_status(status)
        return

    if args.command == "mode":
        status = _request(args.url, "POST", "/api/mode", {"mode": args.mode})
        _print_status(status)
        return

    if args.command == "scan":
        if args.mode:
            _request(args.url, "POST", "/api/mode", {"mode": args.mode})
        data = _request(args.url, "POST", "/api/scan", {"force": not args.no_force}, timeout=900)
        print(f"{data.get('count', 0)} stations found.")
        for index, station in enumerate(data.get("stations", [])):
            print(_format_station_line(index, station))
        return

    if args.command == "stations":
        data = _request(args.url, "GET", f"/api/stations{'?mode=' + args.mode if args.mode else ''}")
        stations = data.get("stations", [])
        if args.json:
            print(json.dumps(stations, indent=2))
            return
        for index, station in enumerate(stations):
            print(_format_station_line(index, station))
        return

    if args.command == "favorites":
        data = _request(args.url, "GET", "/api/favorites")
        for index, station in enumerate(data.get("stations", [])):
            print(_format_station_line(index, station))
        return

    if args.command == "play":
        payload = _resolve_station_target(args.url, args.target)
        status = _request(args.url, "POST", "/api/play", payload)
        _print_status(status)
        return

    if args.command == "volume":
        token = args.value.strip()
        if token.startswith(("+", "-")):
            status = _request(args.url, "POST", "/api/volume", {"delta": int(token)})
        else:
            status = _request(args.url, "POST", "/api/volume", {"level": int(token)})
        _print_status(status)
        return

    if args.command == "amp":
        status = _request(args.url, "POST", "/api/amplifier", {"enabled": args.state == "on"})
        _print_status(status)
        return

    if args.command == "favorite":
        target = args.target.strip()
        station_id = target
        if target.isdigit():
            stations = _fetch_stations(args.url)
            index = int(target)
            if index < 0 or index >= len(stations):
                raise SystemExit(f"Station index {index} is out of range.")
            station_id = stations[index]["station_id"]
        result = _request(
            args.url,
            "POST",
            "/api/favorite",
            {"station_id": station_id, "favorite": False if args.off else None},
        )
        print(f"{result['station_id']}: {'favorite' if result['favorite'] else 'not favorite'}")
        return

    if args.command == "record":
        status = _request(args.url, "POST", "/api/record", {"action": args.action}, timeout=120)
        _print_status(status)
        return

    if args.command == "flash":
        report = _request(
            args.url,
            "POST",
            "/api/flash/program",
            {"mode": args.mode, "self_test": not args.no_self_test},
            timeout=900,
        )
        if args.json:
            print(json.dumps(report, indent=2))
            return
        _print_flash_report(report)
        return

    if args.command == "recordings":
        data = _request(args.url, "GET", "/api/recordings")
        recordings = data.get("recordings", [])
        if args.json:
            print(json.dumps(recordings, indent=2))
            return
        for item in recordings:
            print(f"{item.get('started_at')} | {item.get('station_label')} | {item.get('file_name')}")
        return

    if args.command == "status":
        status = _request(args.url, "GET", "/api/status")
        if args.json:
            print(json.dumps(status, indent=2))
            return
        _print_status(status)
        return

    parser.print_help()
    sys.exit(1)
