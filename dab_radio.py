#!/usr/bin/env python3
"""
Minimal Raspberry Pi controller for Si468x in SPI host-load mode (no NVM flash).
Loads the ROM00 patch and DAB firmware, configures I2S output, tunes a channel,
reads the service list, and starts an audio service.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import spidev  # type: ignore
    import RPi.GPIO as GPIO  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    spidev = None
    GPIO = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

# ---------------------------------------------------------------------------
# Si468x command constants (subset needed for DAB bring-up)
# ---------------------------------------------------------------------------
CMD_POWER_UP = 0x01
CMD_HOST_LOAD = 0x04
CMD_LOAD_INIT = 0x06
CMD_BOOT = 0x07
CMD_SET_PROPERTY = 0x13

CMD_GET_PART_INFO = 0x02

CMD_DAB_TUNE_FREQ = 0xB0
CMD_DAB_DIGRAD_STATUS = 0xB2
CMD_DAB_GET_EVENT_STATUS = 0xB3
CMD_DAB_SET_FREQ_LIST = 0xB8
CMD_GET_DIGITAL_SERVICE_LIST = 0x80
CMD_START_DIGITAL_SERVICE = 0x81
CMD_STOP_DIGITAL_SERVICE = 0x82
CMD_READ_OFFSET = 0x10

# Property IDs
PROP_PIN_CONFIG_ENABLE = 0x0800
PROP_DIGITAL_IO_OUTPUT_SELECT = 0x0200
PROP_DIGITAL_IO_OUTPUT_SAMPLE_RATE = 0x0201
PROP_DIGITAL_IO_OUTPUT_FORMAT = 0x0202
PROP_AUDIO_ANALOG_VOLUME = 0x0300
PROP_DAB_TUNE_FE_VARM = 0x1710
PROP_DAB_TUNE_FE_VARB = 0x1711
PROP_DAB_TUNE_FE_CFG = 0x1712
PROP_DAB_EVENT_INTERRUPT_SOURCE = 0xB300
PROP_DAB_VALID_RSSI_THRESHOLD = 0xB201

# ---------------------------------------------------------------------------
# DAB Band III frequency list (index -> (label, freq_khz))
# Order matches standard Band III channel ordering; index 0 == 5A.
# ---------------------------------------------------------------------------
DAB_BAND_III: List[Tuple[str, int]] = [
    ("5A", 174_928),
    ("5B", 176_640),
    ("5C", 178_352),
    ("5D", 180_064),
    ("6A", 181_936),
    ("6B", 183_648),
    ("6C", 185_360),
    ("6D", 187_072),
    ("7A", 188_928),
    ("7B", 190_640),
    ("7C", 192_352),
    ("7D", 194_064),
    ("8A", 195_936),
    ("8B", 197_648),
    ("8C", 199_360),
    ("8D", 201_072),
    ("9A", 202_928),
    ("9B", 204_640),
    ("9C", 206_352),
    ("9D", 208_064),
    ("10A", 209_936),
    ("10B", 211_648),
    ("10C", 213_360),
    ("10D", 215_072),
    ("10N", 210_096),
    ("11A", 216_928),
    ("11B", 218_640),
    ("11C", 220_352),
    ("11D", 222_064),
    ("11N", 217_088),
    ("12A", 223_936),
    ("12B", 225_648),
    ("12C", 227_360),
    ("12D", 229_072),
    ("12N", 224_096),
    ("13A", 230_784),
    ("13B", 232_496),
    ("13C", 234_208),
    ("13D", 235_776),
    ("13E", 237_488),
    ("13F", 239_200),
]
LABEL_TO_INDEX: Dict[str, int] = {label: idx for idx, (label, _) in enumerate(DAB_BAND_III)}

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def _signed_byte(value: int) -> int:
    return value - 256 if value & 0x80 else value


def _require_pi_modules() -> None:
    if _IMPORT_ERROR is not None:
        raise RuntimeError(
            "spidev and RPi.GPIO are required on the Raspberry Pi. "
            "Import failed with: %s" % _IMPORT_ERROR
        )


class Si468xDabRadio:
    def __init__(
        self,
        spi_bus: int,
        spi_device: int,
        spi_speed_hz: int,
        rst_pin: int,
        int_pin: Optional[int],
    ) -> None:
        _require_pi_modules()
        self.spi = spidev.SpiDev()
        self.spi.open(spi_bus, spi_device)
        self.spi.max_speed_hz = spi_speed_hz
        self.spi.mode = 0
        self.spi.bits_per_word = 8
        self.spi.lsbfirst = False

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(rst_pin, GPIO.OUT, initial=GPIO.LOW)
        if int_pin is not None:
            GPIO.setup(int_pin, GPIO.IN)
        self.rst_pin = rst_pin
        self.int_pin = int_pin

    # ------------------------------------------------------------------
    # Low-level SPI helpers
    # ------------------------------------------------------------------
    def _read_reply(self, length: int) -> List[int]:
        # First byte is 0x00 per SI468x SPI read protocol.
        rx = self.spi.xfer2([0x00] + [0x00] * length)
        return rx[1:]

    def _wait_cts(self, timeout: float = 1.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self._read_reply(1)[0]
            if status & 0x80:  # CTS bit
                if status & 0x40:
                    raise RuntimeError(f"SI468x reported command error (status=0x{status:02X})")
                return
            time.sleep(0.001)
        raise TimeoutError("CTS timeout waiting for SI468x")

    def _write_command(self, data: List[int]) -> None:
        self._wait_cts()
        self.spi.xfer2(data)
        self._wait_cts()

    # ------------------------------------------------------------------
    # Boot / load
    # ------------------------------------------------------------------
    def reset(self) -> None:
        GPIO.output(self.rst_pin, GPIO.LOW)
        time.sleep(0.01)
        GPIO.output(self.rst_pin, GPIO.HIGH)
        time.sleep(0.2)

    def power_up(
        self,
        xtal_freq: int = 19_200_000,
        clk_mode: int = 1,
        tr_size: int = 0x07,
        ibias: int = 0x28,
        ctun: int = 0x07,
        ibias_run: int = 0x18,
    ) -> None:
        cmd = [0x00] * 16
        cmd[0] = CMD_POWER_UP
        cmd[1] |= (0 & 0x1) << 7  # CTSIEN disabled
        cmd[2] |= (clk_mode & 0x03) << 4
        cmd[2] |= tr_size & 0x0F
        cmd[3] = ibias & 0x7F
        cmd[4:8] = list(xtal_freq.to_bytes(4, "little"))
        cmd[8] = ctun & 0x3F
        cmd[9] = 0x10  # required for ROM00 parts
        cmd[13] = ibias_run & 0x7F
        self._write_command(cmd)

    def _send_load_init(self) -> None:
        self._write_command([CMD_LOAD_INIT, 0x00])

    def _boot(self) -> None:
        self._write_command([CMD_BOOT, 0x00])

    def _host_load_file(self, image_path: Path, chunk_size: int = 252) -> None:
        with image_path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                payload = [CMD_HOST_LOAD, 0x00, 0x00, 0x00] + list(chunk)
                self._write_command(payload)

    def load_patch_and_firmware(self, patch_path: Path, firmware_path: Path) -> None:
        self._send_load_init()
        self._host_load_file(patch_path)
        time.sleep(0.004)
        self._send_load_init()
        self._host_load_file(firmware_path)
        self._boot()

    # ------------------------------------------------------------------
    # Properties and configuration
    # ------------------------------------------------------------------
    def set_property(self, prop_id: int, value: int) -> None:
        cmd = [
            CMD_SET_PROPERTY,
            0x00,
            prop_id & 0xFF,
            (prop_id >> 8) & 0xFF,
            value & 0xFF,
            (value >> 8) & 0xFF,
        ]
        self._write_command(cmd)

    def configure_audio(
        self,
        mode: str = "analog",
        master: bool = True,
        sample_rate: int = 48_000,
        sample_size: int = 16,
    ) -> None:
        """
        mode: "analog" enables DAC only, "i2s" enables I2S (DAC remains on by default).
        """
        pin_cfg = 0x8001  # DACOUTEN + keep defaults
        if mode == "i2s":
            pin_cfg |= 0x0002  # I2SOUTEN
        self.set_property(PROP_PIN_CONFIG_ENABLE, pin_cfg)

        if mode == "i2s":
            output_select = 0x8000 if master else 0x0000
            self.set_property(PROP_DIGITAL_IO_OUTPUT_SELECT, output_select)
            self.set_property(PROP_DIGITAL_IO_OUTPUT_SAMPLE_RATE, sample_rate)
            fmt_value = (sample_size & 0x3F) << 8  # sample_size bits, I2S framing = 0
            self.set_property(PROP_DIGITAL_IO_OUTPUT_FORMAT, fmt_value)

    def configure_dab_frontend(self) -> None:
        # Calibration values pulled from Platform_F380_Module (FRONT_END_BOOST)
        self.set_property(PROP_DAB_TUNE_FE_VARM, 0xFD12)
        self.set_property(PROP_DAB_TUNE_FE_VARB, 0x009B)
        self.set_property(PROP_DAB_TUNE_FE_CFG, 0x0000)
        # Interrupts: RECFG, RECFGWRN, SRVLIST
        self.set_property(PROP_DAB_EVENT_INTERRUPT_SOURCE, 0x00C1)
        self.set_property(PROP_DAB_VALID_RSSI_THRESHOLD, 6)

    def set_volume(self, level: int) -> int:
        """Set analog volume 0-63; returns clamped level."""
        level = max(0, min(63, level))
        self.set_property(PROP_AUDIO_ANALOG_VOLUME, level)
        return level

    def set_dab_freq_list(self, freqs_khz: List[int], extend_range: bool = False) -> None:
        # Build DAB_SET_FREQ_LIST: [cmd, num_freqs, tune_limit, pad] + freqs (u32 LE)
        num = len(freqs_khz)
        if num == 0:
            raise ValueError("Frequency list empty")
        if num > 75:
            raise ValueError("Frequency list too long (max 75)")
        enable_ext_tune_limit = 1 if extend_range else 0
        cmd = [CMD_DAB_SET_FREQ_LIST, num & 0xFF, enable_ext_tune_limit & 0x01, 0x00]
        for f in freqs_khz:
            cmd.extend(list(int(f).to_bytes(4, "little")))
        self._write_command(cmd)

    # ------------------------------------------------------------------
    # DAB control
    # ------------------------------------------------------------------
    def dab_tune(self, freq_index: int, antcap: int = 0) -> None:
        cmd = [
            CMD_DAB_TUNE_FREQ,
            0x00,  # injection auto
            freq_index & 0xFF,
            0x00,
            antcap & 0xFF,
            (antcap >> 8) & 0xFF,
        ]
        self._write_command(cmd)

    def dab_digrad_status(self) -> Dict[str, int]:
        self._write_command([CMD_DAB_DIGRAD_STATUS, 0x00])
        reply = self._read_reply(0x28)
        return {
            "fic_error": bool(reply[5] & 0x08),
            "acq": bool(reply[5] & 0x04),
            "valid": bool(reply[5] & 0x01),
            "rssi": _signed_byte(reply[6]),
            "snr": reply[7],
            "fic_quality": reply[8],
            "cnr": reply[9],
            "tune_freq_hz": int.from_bytes(reply[12:16], "little"),
            "tune_index": reply[16],
        }

    def dab_get_event_status(self, ack: bool = False, clr_audio: bool = False) -> Dict[str, bool]:
        flags = (0x01 if ack else 0x00) | (0x02 if clr_audio else 0x00)
        self._write_command([CMD_DAB_GET_EVENT_STATUS, flags])
        reply = self._read_reply(9)
        return {
            "svrlist": bool(reply[5] & 0x01),
            "freqinfo": bool(reply[5] & 0x02),
            "audio": bool(reply[5] & 0x20),
            "mute_engaged": bool(reply[8] & 0x08),
            "blk_error": bool(reply[8] & 0x02),
            "blk_loss": bool(reply[8] & 0x01),
        }

    def _get_service_list_payload(self) -> bytes:
        self._write_command([CMD_GET_DIGITAL_SERVICE_LIST, 0x00])  # audio service type
        header = self._read_reply(6)
        total_size = int.from_bytes(header[4:6], "little")
        if total_size == 0:
            return b""
        # One more read to pull the full payload (header + payload)
        full = self._read_reply(6 + total_size)
        return bytes(full[6:])

    def _read_service_list_segment(self, offset: int, length: int) -> bytes:
        cmd = [CMD_READ_OFFSET, 0x00, offset & 0xFF, (offset >> 8) & 0xFF]
        self._write_command(cmd)
        reply = self._read_reply(4 + length)
        return bytes(reply[4:])

    def get_audio_services(self) -> List[Dict[str, object]]:
        payload = self._get_service_list_payload()
        if not payload:
            return []

        # Fallback to segmented reads if needed
        total_size = int.from_bytes(payload[4:6], "little") if len(payload) >= 6 else len(payload)
        if total_size > len(payload):
            # Re-fetch using READ_OFFSET in 252-byte chunks
            segments: List[bytes] = []
            offset = 0
            while offset < total_size:
                chunk_len = min(252, total_size - offset)
                segments.append(self._read_service_list_segment(offset, chunk_len))
                offset += chunk_len
            payload = b"".join(segments)

        services: List[Dict[str, object]] = []
        offset = 0
        service_count = int.from_bytes(payload[2:4], "little") if len(payload) >= 4 else 0
        offset = 6  # start of first service element

        for _ in range(service_count):
            if offset + 24 > len(payload):
                break
            sid = int.from_bytes(payload[offset : offset + 4], "little")
            info1 = payload[offset + 4]
            info2 = payload[offset + 5]
            info3 = payload[offset + 6]
            label_bytes = payload[offset + 8 : offset + 24]
            label = label_bytes.split(b"\x00", 1)[0].decode("latin-1", errors="ignore").strip()
            num_components = info2 & 0x0F
            offset += 24

            for _ in range(num_components):
                if offset + 4 > len(payload):
                    break
                comp_id = int.from_bytes(payload[offset : offset + 2], "little")
                comp_info = payload[offset + 2]
                tmid = (comp_id >> 14) & 0x03
                caflag = comp_info & 0x01
                if tmid == 0 and caflag == 0 and (info1 & 0x01) == 0:
                    services.append(
                        {
                            "service_id": sid,
                            "component_id": comp_id,
                            "label": label or f"SID 0x{sid:08X}",
                            "charset": info3 & 0x0F,
                        }
                    )
                offset += 4
        return services

    def start_digital_service(self, service_id: int, component_id: int) -> None:
        cmd = [
            CMD_START_DIGITAL_SERVICE,
            0x00,  # audio
            0x00,
            0x00,
            *list(service_id.to_bytes(4, "little")),
            *list(component_id.to_bytes(4, "little")),
        ]
        self._write_command(cmd)

    def stop_digital_service(self, service_id: int, component_id: int) -> None:
        cmd = [
            CMD_STOP_DIGITAL_SERVICE,
            0x00,  # audio
            0x00,
            0x00,
            *list(service_id.to_bytes(4, "little")),
            *list(component_id.to_bytes(4, "little")),
        ]
        self._write_command(cmd)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------
    def close(self) -> None:
        try:
            self.spi.close()
        finally:
            try:
                GPIO.cleanup()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).resolve().parent.parent
    default_patch = "./rom00_patch.016.bin"
    default_fw = "./dab_radio_6_0_9.bin"

    parser = argparse.ArgumentParser(description="Play DAB via Si468x on Raspberry Pi (SPI host load).")
    parser.add_argument("--patch", type=Path, default=default_patch, help="Path to rom00 patch image")
    parser.add_argument("--firmware", type=Path, default=default_fw, help="Path to dab_radio firmware image")
    parser.add_argument("--freq", type=str, help="DAB channel label (e.g. 5A, 10C)")
    parser.add_argument("--freq-index", type=int, help="Frequency index override (0-based)")
    parser.add_argument("--service-id", type=lambda x: int(x, 0), help="Service ID to start (hex or int)")
    parser.add_argument(
        "--service-index",
        type=int,
        default=0,
        help="Use nth audio service from the list (default: 0 / first)",
    )
    parser.add_argument("--list-only", action="store_true", help="Only list services after tuning")
    parser.add_argument("--spi-bus", type=int, default=0)
    parser.add_argument("--spi-device", type=int, default=0)
    parser.add_argument("--spi-speed", type=int, default=1_000_000, help="SPI speed in Hz (default 1 MHz)")
    parser.add_argument("--rst-pin", type=int, default=23, help="GPIO (BCM) for RSTB (default 23 / physical 16)")
    parser.add_argument("--int-pin", type=int, default=None, help="GPIO (BCM) for INTB; leave unset to poll")
    parser.add_argument(
        "--audio-out",
        choices=["analog", "i2s"],
        default="analog",
        help="Select audio output path (analog DAC or I2S). Default: analog",
    )
    parser.add_argument("--i2s-master", action="store_true", default=True, help="Si468x drives BCLK/LRCLK (default)")
    parser.add_argument("--i2s-slave", dest="i2s_master", action="store_false", help="Pi drives I2S clocks")
    parser.add_argument("--sample-rate", type=int, default=48_000)
    parser.add_argument("--sample-size", type=int, default=16)
    parser.add_argument(
        "--xtal", type=lambda x: int(x, 0), default=19_200_000, help="XTAL frequency in Hz (default 19.2 MHz)"
    )
    parser.add_argument(
        "--ctun", type=lambda x: int(x, 0), default=0x07, help="XTAL tuning word (default 0x07 from module ref)"
    )
    parser.add_argument("--antcap", type=lambda x: int(x, 0), default=0, help="ANTCAP value for DAB_TUNE (0=auto)")
    parser.add_argument(
        "--skip-set-freqlist",
        action="store_true",
        help="Do not push a frequency list; use current list stored in the chip (not recommended).",
    )
    parser.add_argument(
        "--freq-list-khz",
        type=str,
        help="Comma-separated list of DAB freqs in kHz to push as the frequency list (index is position).",
    )
    parser.add_argument(
        "--lock-ms",
        type=int,
        default=5000,
        help="How long to wait for DAB lock before failing (ms, default 5000)",
    )
    parser.add_argument(
        "--status-interval-ms",
        type=int,
        default=500,
        help="How often to print digrad status while waiting for lock (ms, default 500)",
    )
    parser.add_argument("--scan", action="store_true", help="Scan all frequencies in the list before choosing a service")
    parser.add_argument("--force-scan", action="store_true", help="Ignore saved full_scan.txt and rescan now")
    return parser.parse_args()


def resolve_freq_index(args: argparse.Namespace) -> int:
    if args.freq_index is not None:
        return args.freq_index
    if args.freq:
        label = args.freq.upper()
        if label not in LABEL_TO_INDEX:
            raise SystemExit(f"Unknown DAB channel label '{args.freq}'. Known labels: {', '.join(LABEL_TO_INDEX)}")
        return LABEL_TO_INDEX[label]
    # Default to 5A
    return 0


def load_scan_file(path: Path) -> Optional[List[Dict[str, object]]]:
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()
        if lines and lines[0].startswith("Automatically generated"):
            json_text = "\n".join(lines[1:])
        else:
            json_text = text
        data = json.loads(json_text)
        return data if isinstance(data, list) else None
    except Exception:
        return None


def save_scan_file(path: Path, services: List[Dict[str, object]]) -> None:
    payload = []
    for svc in services:
        payload.append(
            {
                "service_id": svc.get("service_id"),
                "component_id": svc.get("component_id"),
                "label": svc.get("label"),
                "freq_index": svc.get("freq_index"),
                "freq_khz": svc.get("freq_khz"),
            }
        )
    json_text = json.dumps(payload, indent=2)
    path.write_text("Automatically generated and machine read file, do not change!\n" + json_text, encoding="utf-8")


def main() -> None:
    args = parse_args()
    freq_index = resolve_freq_index(args)
    band_freqs = [f for _, f in DAB_BAND_III]
    scan_file = Path(__file__).resolve().with_name("full_scan.txt")

    patch_path = args.patch
    firmware_path = args.firmware
    if not patch_path.exists():
        raise SystemExit(f"Patch image not found: {patch_path}")
    if not firmware_path.exists():
        raise SystemExit(f"Firmware image not found: {firmware_path}")

    radio = Si468xDabRadio(
        spi_bus=args.spi_bus,
        spi_device=args.spi_device,
        spi_speed_hz=args.spi_speed,
        rst_pin=args.rst_pin,
        int_pin=args.int_pin,
    )

    try:
        print("Resetting SI468x...")
        radio.reset()
        print(f"Powering up ROM... (xtal={args.xtal} ctun=0x{args.ctun:02X})")
        radio.power_up(xtal_freq=args.xtal, ctun=args.ctun)
        print("Loading patch and firmware (this takes a few seconds)...")
        radio.load_patch_and_firmware(patch_path, firmware_path)
        print("Configuring I2S and DAB frontend...")
        radio.configure_audio(
            mode=args.audio_out,
            master=args.i2s_master,
            sample_rate=args.sample_rate,
            sample_size=args.sample_size,
        )
        radio.configure_dab_frontend()

        # Determine startup frequency list
        loaded_services = None
        if not args.force_scan:
            loaded_services = load_scan_file(scan_file)

        if loaded_services:
            # Build frequency list from saved services (unique, sorted)
            freqs_from_file = []
            for svc in loaded_services:
                fk = svc.get("freq_khz")
                if isinstance(fk, (int, float)) and int(fk) not in freqs_from_file:
                    freqs_from_file.append(int(fk))
            if freqs_from_file:
                band_freqs = freqs_from_file
            print(f"Loaded {len(loaded_services)} services from {scan_file}.")
        else:
            print("No valid full_scan.txt found. Will run full scan.")

        if not args.skip_set_freqlist:
            if args.freq_list_khz:
                user_freqs = []
                for token in args.freq_list_khz.split(","):
                    token = token.strip()
                    if not token:
                        continue
                    user_freqs.append(int(token))
                if not user_freqs:
                    raise SystemExit("Provided --freq-list-khz is empty after parsing")
                print(f"Setting custom DAB frequency list ({len(user_freqs)} entries)...")
                radio.set_dab_freq_list(user_freqs)
                band_freqs = user_freqs
            else:
                print(f"Setting frequency list ({len(band_freqs)} entries)...")
                radio.set_dab_freq_list(band_freqs)

        # Map freq_khz to new freq_index for all services
        freq_map = {freq: idx for idx, freq in enumerate(band_freqs)}

        def tune_and_wait(idx: int, lock_ms_override: Optional[int] = None) -> Optional[Dict[str, int]]:
            label = f"idx {idx}"
            freq_khz = band_freqs[idx] if idx < len(band_freqs) else None
            print(f"Tuning DAB channel index {idx} ({label}) freq={freq_khz} kHz ...")
            try:
                radio.dab_tune(idx, antcap=args.antcap)
            except RuntimeError as err:
                print(f"DAB_TUNE_FREQ failed: {err}")
                return None
            lock_ms = lock_ms_override if lock_ms_override is not None else args.lock_ms
            deadline = time.time() + (lock_ms / 1000.0)
            next_status_print = time.time()
            while time.time() < deadline:
                status = radio.dab_digrad_status()
                now = time.time()
                if status["valid"]:
                    return status
                if now >= next_status_print:
                    print(
                        f"  waiting lock... RSSI={status['rssi']} SNR={status['snr']} "
                        f"FICQ={status['fic_quality']} ACQ={status['acq']} VALID={status['valid']}"
                    )
                    next_status_print = now + max(args.status_interval_ms / 1000.0, 0.05)
                time.sleep(0.05)
            return None

        def grab_services() -> List[Dict[str, object]]:
            # Wait for service list to be ready
            for _ in range(50):
                ev = radio.dab_get_event_status(ack=False)
                if ev["svrlist"]:
                    radio.dab_get_event_status(ack=True)
                    break
                time.sleep(0.1)
            return radio.get_audio_services()

        def full_scan() -> List[Dict[str, object]]:
            all_services: List[Dict[str, object]] = []
            print("Starting full scan...")
            for idx in range(len(band_freqs)):
                status = tune_and_wait(idx)
                if status is None:
                    continue
                svc_list = grab_services()
                for svc in svc_list:
                    svc["freq_index"] = idx
                    svc["freq_khz"] = band_freqs[idx] if idx < len(band_freqs) else None
                all_services.extend(svc_list)
            return all_services

        def ensure_services() -> List[Dict[str, object]]:
            nonlocal freq_index, band_freqs, loaded_services
            if loaded_services and not args.force_scan:
                # Ensure the frequency list aligns with stored indices
                services = []
                for svc in loaded_services:
                    svc_copy = dict(svc)
                    fk = svc_copy.get("freq_khz")
                    if isinstance(fk, (int, float)) and int(fk) in freq_map:
                        svc_copy["freq_index"] = freq_map[int(fk)]
                    services.append(svc_copy)
                return services
            services = full_scan()
            if not services:
                print("No services found during scan.")
                return []
            save_scan_file(scan_file, services)
            print(f"Scan complete. Saved {len(services)} services to {scan_file}.")
            return services

        services = ensure_services()
        if not services:
            return

        if args.list_only:
            return

        # Sort services by label for display
        services = sorted(services, key=lambda s: s.get("label", ""))
        current_service: Optional[Dict[str, object]] = None

        def start_service(service: Dict[str, object]) -> None:
            nonlocal freq_index
            target_idx = int(service.get("freq_index", freq_index))
            if target_idx != freq_index:
                status = tune_and_wait(target_idx, lock_ms_override=max(args.lock_ms, 8000))
                if status is None:
                    print("Failed to lock to target frequency; service start aborted.")
                    return
                freq_index = target_idx
            else:
                status = tune_and_wait(target_idx, lock_ms_override=max(args.lock_ms, 8000))
                if status is None:
                    print("Failed to lock to target frequency; service start aborted.")
                    return
            # Check ACQ/VALID again just before starting service
            if not status.get("valid", 0) or not status.get("acq", 0):
                status = radio.dab_digrad_status()
                if not status.get("valid", 0) or not status.get("acq", 0):
                    print("Channel not valid/acquired; service start aborted.")
                    return
            # Stop previous service if any
            nonlocal current_service
            if current_service:
                try:
                    radio.stop_digital_service(
                        int(current_service["service_id"]), int(current_service["component_id"])
                    )
                except Exception:
                    pass
            print(
                f"Starting service '{service['label']}' SID=0x{service['service_id']:08X} "
                f"COMP=0x{service['component_id']:04X}"
            )
            try:
                radio.start_digital_service(int(service["service_id"]), int(service["component_id"]))
            except RuntimeError as err:
                print(f"START_DIGITAL_SERVICE failed: {err}")
                return
            current_service = service
            if args.audio_out == "analog":
                print("Analog audio active on SI468x DAC outputs. (+/- to change volume, q to quit)")
            else:
                print("I2S audio active on SI468x DCLK/DFS/DOUT pins. (+/- to change volume, q to quit)")

        current_volume = radio.set_volume(40)
        print(f"Initial volume set to {current_volume}/63.")

        # If a specific service is requested, start it immediately
        if args.service_id is not None:
            matches = [s for s in services if s["service_id"] == args.service_id]
            if not matches:
                raise SystemExit(f"Service ID 0x{args.service_id:08X} not found in ensemble")
            start_service(matches[0])
        else:
            # Default to first service
            start_service(services[0])

        def print_menu() -> None:
            print("\nCommands: number=<index> | name substring | + / - volume | o toggle audio out | r rescan | q quit")
            print("Stations:")
            for idx, svc in enumerate(services):
                fi = svc.get("freq_index", -1)
                fk = svc.get("freq_khz", 0)
                print(
                    f"  [{idx}] {svc.get('label','')}  SID=0x{svc['service_id']:08X} "
                    f"COMP=0x{svc['component_id']:04X}  FreqIdx={fi} ({fk} kHz)"
                )

        print_menu()
        while True:
            try:
                cmd = input("radio> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if cmd == "":
                continue
            if cmd.lower() == "q":
                print("Leaving radio playing. Bye.")
                break
            if cmd.lower() == "r":
                services = ensure_services()
                services = sorted(services, key=lambda s: s.get("label", ""))
                print("Rescan complete.")
                print_menu()
                continue
            if cmd == "+":
                current_volume = radio.set_volume(current_volume + 2)
                print(f"Volume {current_volume}/63")
                continue
            if cmd == "-":
                current_volume = radio.set_volume(current_volume - 2)
                print(f"Volume {current_volume}/63")
                continue
            if cmd.lower() == "o":
                args.audio_out = "i2s" if args.audio_out == "analog" else "analog"
                radio.configure_audio(
                    mode=args.audio_out,
                    master=args.i2s_master,
                    sample_rate=args.sample_rate,
                    sample_size=args.sample_size,
                )
                print(f"Audio output switched to {args.audio_out}.")
                continue

            # Selection by index or substring
            selected: Optional[Dict[str, object]] = None
            if cmd.isdigit():
                idx = int(cmd)
                if 0 <= idx < len(services):
                    selected = services[idx]
            else:
                for svc in services:
                    if cmd.lower() in str(svc.get("label", "")).lower():
                        selected = svc
                        break
            if selected:
                start_service(selected)
            else:
                print("Unknown command/selection.")
    finally:
        radio.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
