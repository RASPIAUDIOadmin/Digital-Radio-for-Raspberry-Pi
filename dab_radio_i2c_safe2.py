#!/usr/bin/env python3
"""
Minimal Raspberry Pi controller for Si468x in I2C or SPI host-load mode (optional flash boot).
Loads the ROM00 patch, boots the DAB firmware (host-load or flash-load), configures
I2S output, tunes a channel, reads the service list, and starts an audio service.
"""
from __future__ import annotations

import argparse
import json
import select
import sys
import time
import termios
import tty
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    from smbus2 import SMBus, i2c_msg  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    SMBus = None
    i2c_msg = None
    _I2C_IMPORT_ERROR = exc
else:
    _I2C_IMPORT_ERROR = None

try:
    import spidev  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    spidev = None
    _SPI_IMPORT_ERROR = exc
else:
    _SPI_IMPORT_ERROR = None

try:
    import RPi.GPIO as GPIO  # type: ignore
except ImportError as exc:  # pragma: no cover - only relevant on the Pi
    GPIO = None
    _GPIO_IMPORT_ERROR = exc
else:
    _GPIO_IMPORT_ERROR = None

# ---------------------------------------------------------------------------
# Si468x command constants (subset needed for DAB bring-up)
# ---------------------------------------------------------------------------
CMD_POWER_UP = 0x01
CMD_HOST_LOAD = 0x04
CMD_FLASH_LOAD = 0x05
CMD_LOAD_INIT = 0x06
CMD_BOOT = 0x07
CMD_SET_PROPERTY = 0x13
CMD_GET_PROPERTY = 0x14

CMD_GET_PART_INFO = 0x02

CMD_DAB_TUNE_FREQ = 0xB0
CMD_DAB_DIGRAD_STATUS = 0xB2
CMD_DAB_GET_EVENT_STATUS = 0xB3
CMD_DAB_SET_FREQ_LIST = 0xB8
CMD_GET_DIGITAL_SERVICE_LIST = 0x80
CMD_START_DIGITAL_SERVICE = 0x81
CMD_STOP_DIGITAL_SERVICE = 0x82
CMD_READ_OFFSET = 0x10
CMD_FM_TUNE_FREQ = 0x30
CMD_FM_SEEK_START = 0x31
CMD_FM_RSQ_STATUS = 0x32

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

# Default NVM flash addresses (from _RECOMMENDED_FLASH_ADDRESSES.txt)
# FLASH_MINI flow loads mini patch from host, then full patch + firmware from flash.
FLASH_ADDR_PATCH_UPDATE = 0x00002000
FLASH_ADDR_PATCH_FULL = 0x00004000
FLASH_ADDR_DAB = 0x00092000
FLASH_SECTOR_SIZE = 0x1000
FLASH_WRITE_BLOCK = 224

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

FM_BAND_DEFAULT_MIN_KHZ = 87_500
FM_BAND_DEFAULT_MAX_KHZ = 108_000
FM_BAND_DEFAULT_STEP_KHZ = 100

# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
def _signed_byte(value: int) -> int:
    return value - 256 if value & 0x80 else value


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(value)))


def _reception_score(status: Dict[str, int]) -> int:
    ficq = _clamp_int(status.get("fic_quality", 0), 0, 100)
    cnr = _clamp_int(status.get("cnr", 0), 0, 30)
    cnr_score = _clamp_int(cnr * 10, 0, 100)
    rssi = _clamp_int(status.get("rssi", -120), -120, 20)
    rssi_score = _clamp_int(int((rssi + 120) * (100 / 140)), 0, 100)
    return _clamp_int(int(round(ficq * 0.5 + cnr_score * 0.35 + rssi_score * 0.15)), 0, 100)


def _format_reception_bar(status: Dict[str, int], width: int = 12) -> str:
    value = _reception_score(status)
    filled = int(round((value / 100) * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + f"] {value:3d}%"


def _format_fm_bar(status: Dict[str, int], width: int = 12) -> str:
    snr = _clamp_int(status.get("snr", 0), 0, 50)
    snr_score = _clamp_int(int((snr / 50) * 100), 0, 100)
    rssi = max(0, int(status.get("rssi", 0)))
    rssi_score = _clamp_int(int((rssi / 60) * 100), 0, 100)
    value = _clamp_int(int(round(snr_score * 0.6 + rssi_score * 0.4)), 0, 100)
    filled = int(round((value / 100) * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + f"] {value:3d}%"


def _mhz_or_khz_to_khz(value: float) -> int:
    return int(round(value * 1000.0)) if value < 1000.0 else int(round(value))


def _crc32_update(crc: int, data: bytes) -> int:
    c = crc
    for b in data:
        c ^= b
        for _ in range(8):
            if c & 1:
                c = (c >> 1) ^ 0xEDB88320
            else:
                c >>= 1
    return c


def _require_pi_modules(use_spi: bool) -> None:
    if _GPIO_IMPORT_ERROR is not None:
        raise RuntimeError(
            "RPi.GPIO is required on the Raspberry Pi. "
            "Import failed with: %s" % _GPIO_IMPORT_ERROR
        )
    if use_spi:
        if _SPI_IMPORT_ERROR is not None:
            raise RuntimeError(
                "spidev is required for SPI control. "
                "Import failed with: %s" % _SPI_IMPORT_ERROR
            )
    else:
        if _I2C_IMPORT_ERROR is not None:
            raise RuntimeError(
                "smbus2 is required for I2C control. "
                "Import failed with: %s" % _I2C_IMPORT_ERROR
            )


class Si468xDabRadio:
    def __init__(
        self,
        i2c_bus: int,
        i2c_addr: int,
        rst_pin: int,
        int_pin: Optional[int],
        use_spi: bool,
        spi_bus: int,
        spi_dev: int,
        spi_speed_hz: int,
        rst_initial_high: bool = False,
    ) -> None:
        _require_pi_modules(use_spi=use_spi)
        self.use_spi = use_spi
        self.bus = None
        self.spi = None
        self.i2c_addr = i2c_addr
        self.i2c_bus = i2c_bus
        self._i2c_delay_s = 0.0005
        self.reset_post_ms = 200
        if use_spi:
            if spidev is None:
                raise RuntimeError("spidev is required for SPI control")
            self.spi = spidev.SpiDev()
            self.spi.open(spi_bus, spi_dev)
            self.spi.max_speed_hz = int(spi_speed_hz)
            self.spi.mode = 0
            self.spi.bits_per_word = 8
        else:
            if SMBus is None or i2c_msg is None:
                raise RuntimeError("smbus2 is required for I2C control")
            self.bus = SMBus(i2c_bus)

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        # IMPORTANT: Do NOT touch GPIO 2/3 (I2C SDA/SCL). They are controlled by
        # the Linux I2C driver; reconfiguring them here can break the bus.
        rst_level = GPIO.HIGH if rst_initial_high else GPIO.LOW
        GPIO.setup(rst_pin, GPIO.OUT, initial=rst_level)
        if int_pin is not None:
            GPIO.setup(int_pin, GPIO.IN)
        self.rst_pin = rst_pin
        self.int_pin = int_pin

    # ------------------------------------------------------------------
    # Low-level SPI helpers
    # ------------------------------------------------------------------
    def _i2c_recover_bus(self) -> None:
        if self.use_spi or self.i2c_bus != 1 or GPIO is None:
            return
        # Do NOT bit-bang GPIO2/3 (I2C SDA/SCL). Let the kernel driver handle recovery.
        try:
            if self.bus is not None:
                self.bus.close()
        except Exception:
            pass
        try:
            if SMBus is not None:
                self.bus = SMBus(self.i2c_bus)
        except Exception:
            pass

    def _i2c_write(self, data: List[int]) -> None:
        if i2c_msg is None or self.bus is None:
            raise RuntimeError("smbus2 i2c_msg required for I2C writes")
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self.bus.i2c_rdwr(i2c_msg.write(self.i2c_addr, data))
                if self._i2c_delay_s:
                    time.sleep(self._i2c_delay_s)
                return
            except OSError as exc:
                last_err = exc
                if attempt == 0:
                    self._i2c_recover_bus()
                    time.sleep(0.005)
                    continue
                raise
        if last_err is not None:
            raise last_err

    def _i2c_read(self, length: int) -> List[int]:
        if i2c_msg is None or self.bus is None:
            raise RuntimeError("smbus2 i2c_msg required for I2C reads")
        last_err: Optional[Exception] = None
        for attempt in range(2):
            try:
                self.bus.i2c_rdwr(i2c_msg.write(self.i2c_addr, [0x00]))
                if self._i2c_delay_s:
                    time.sleep(self._i2c_delay_s)
                read = i2c_msg.read(self.i2c_addr, length)
                self.bus.i2c_rdwr(read)
                return list(read)
            except OSError as exc:
                last_err = exc
                if attempt == 0:
                    self._i2c_recover_bus()
                    time.sleep(0.005)
                    continue
                raise
        if last_err is not None:
            raise last_err
        return []

    def _read_reply(self, length: int) -> List[int]:
        if self.use_spi:
            if self.spi is None:
                raise RuntimeError("SPI not initialized")
            resp = self.spi.xfer2([0x00] + [0x00] * length)
            return resp[1:]
        return self._i2c_read(length)

    def _wait_cts(self, timeout: float = 1.0, allow_cmd_error: bool = False) -> None:
        deadline = time.time() + timeout
        last_err: Optional[Exception] = None
        while time.time() < deadline:
            try:
                status = self._read_reply(1)[0]
            except OSError as exc:
                last_err = exc
                time.sleep(0.005)
                continue
            if status & 0x80:  # CTS bit
                if status & 0x40:
                    if allow_cmd_error:
                        # SDK behavior (HAL writeCommand): allow a pending command error
                        # before sending a new command, so that the next command can clear it.
                        return
                    raise RuntimeError(f"SI468x reported command error (status=0x{status:02X})")
                return
            time.sleep(0.001)
        if last_err is not None:
            raise TimeoutError(f"CTS timeout waiting for SI468x (last error: {last_err})")
        raise TimeoutError("CTS timeout waiting for SI468x")

    def _write_command(
        self,
        data: List[int],
        timeout: float = 1.0,
        skip_cts_before: bool = False,
        allow_cmd_error_after: bool = False,
    ) -> None:
        if not skip_cts_before:
            self._wait_cts(timeout=timeout, allow_cmd_error=True)
        if self.use_spi:
            if self.spi is None:
                raise RuntimeError("SPI not initialized")
            self.spi.xfer2(data)
        else:
            self._i2c_write(data)
        self._wait_cts(timeout=timeout, allow_cmd_error=allow_cmd_error_after)

    # ------------------------------------------------------------------
    # Boot / load
    # ------------------------------------------------------------------
    def reset(self, hold_low_ms: int = 10, post_ms: Optional[int] = None) -> None:
        if post_ms is None:
            post_ms = self.reset_post_ms
        GPIO.output(self.rst_pin, GPIO.LOW)
        time.sleep(max(0.0, hold_low_ms) / 1000.0)
        GPIO.output(self.rst_pin, GPIO.HIGH)
        time.sleep(max(0.0, post_ms) / 1000.0)

    def power_up(
        self,
        xtal_freq: int = 19_200_000,
        clk_mode: int = 1,
        tr_size: int = 0x07,
        ibias: int = 0x28,
        ctun: int = 0x07,
        ibias_run: int = 0x18,
        retries: int = 1,
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
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            try:
                self._write_command(cmd, skip_cts_before=not self.use_spi)
                return
            except (OSError, TimeoutError) as exc:
                if attempt + 1 >= attempts:
                    raise
                self._i2c_recover_bus()
                time.sleep(0.05)

    def _send_load_init(self, allow_cmd_error: bool = False) -> None:
        self._write_command([CMD_LOAD_INIT, 0x00], allow_cmd_error_after=allow_cmd_error)

    def _boot(self, allow_cmd_error: bool = False) -> None:
        self._write_command([CMD_BOOT, 0x00], allow_cmd_error_after=allow_cmd_error)

    def _host_load_file(self, image_path: Path, chunk_size: int = 32, allow_cmd_error: bool = False) -> None:
        with image_path.open("rb") as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                payload = [CMD_HOST_LOAD, 0x00, 0x00, 0x00] + list(chunk)
                self._write_command(payload, allow_cmd_error_after=allow_cmd_error)

    def load_patch_and_firmware(
        self,
        patch_path: Path,
        firmware_path: Path,
        allow_cmd_error: bool = False,
    ) -> None:
        self._send_load_init(allow_cmd_error=allow_cmd_error)
        self._host_load_file(patch_path, allow_cmd_error=allow_cmd_error)
        time.sleep(0.004)
        self._send_load_init(allow_cmd_error=allow_cmd_error)
        self._host_load_file(firmware_path, allow_cmd_error=allow_cmd_error)
        self._boot(allow_cmd_error=allow_cmd_error)

    def load_patch_only(self, patch_path: Path, allow_cmd_error: bool = False) -> None:
        self._send_load_init(allow_cmd_error=allow_cmd_error)
        self._host_load_file(patch_path, allow_cmd_error=allow_cmd_error)
        time.sleep(0.004)

    def flash_load(self, start_addr: int) -> None:
        """
        Load firmware from external NVM flash.

        Try multiple command formats if the first one fails.
        """
        # Method 1: Standard 12-byte command (most common)
        try:
            cmd = [0x00] * 12
            cmd[0] = CMD_FLASH_LOAD
            cmd[4:8] = list(int(start_addr).to_bytes(4, "little"))
            self._write_command(cmd, timeout=5.0)
            print(f"[DEBUG] Flash load successful (method 1: standard 12-byte)")
            return
        except Exception as e1:
            print(f"[DEBUG] Flash load method 1 failed: {e1}")

        # Method 2: 8-byte command variant
        try:
            cmd = [0x00] * 8
            cmd[0] = CMD_FLASH_LOAD
            cmd[4:8] = list(int(start_addr).to_bytes(4, "little"))
            self._write_command(cmd, timeout=5.0)
            print(f"[DEBUG] Flash load successful (method 2: 8-byte variant)")
            return
        except Exception as e2:
            print(f"[DEBUG] Flash load method 2 failed: {e2}")

        # Method 3: With explicit length parameter (bytes 8-11)
        try:
            cmd = [0x00] * 12
            cmd[0] = CMD_FLASH_LOAD
            cmd[4:8] = list(int(start_addr).to_bytes(4, "little"))
            # Set length to 0 for "load all"
            cmd[8:12] = [0x00, 0x00, 0x00, 0x00]
            self._write_command(cmd, timeout=5.0)
            print(f"[DEBUG] Flash load successful (method 3: with length)")
            return
        except Exception as e3:
            print(f"[DEBUG] Flash load method 3 failed: {e3}")

        # If all methods fail
        raise RuntimeError(
            f"Failed to load firmware from flash address 0x{start_addr:08X}. "
            f"All command formats were rejected. "
            f"Please verify:\n"
            f"  1. Firmware was correctly programmed to flash\n"
            f"  2. Correct flash address (try 0x00040000 or 0x00092000)\n"
            f"  3. Correct patch is loaded (use full patch, not mini)\n"
            f"  4. Flash chip is properly connected and powered"
        )

    def flash_load_strict(self, start_addr: int, timeout: float = 5.0, allow_cmd_error: bool = False) -> None:
        """
        SDK strict FLASH_LOAD command format (12 bytes only).
        """
        cmd = [0x00] * 12
        cmd[0] = CMD_FLASH_LOAD
        cmd[4:8] = list(int(start_addr).to_bytes(4, "little"))
        self._write_command(cmd, timeout=timeout, allow_cmd_error_after=allow_cmd_error)

    def flash_load_and_boot(self, start_addr: int, allow_cmd_error: bool = False) -> None:
        self._send_load_init(allow_cmd_error=allow_cmd_error)
        self.flash_load_strict(start_addr, allow_cmd_error=allow_cmd_error)
        self._boot(allow_cmd_error=allow_cmd_error)

    def adjust_nvmspi_rate(self, rate_khz: int, allow_cmd_error: bool = False) -> None:
        """
        Optional SDK raw command to adjust external NVM SPI clock.
        Equivalent to Firmware_API_Manager.c::_adjust_NVMSPI_rate().
        """
        rate = int(rate_khz)
        if rate <= 0:
            return
        if rate > 0xFFFF:
            raise ValueError("NVM SPI rate must be <= 65535 kHz")
        cmd = [CMD_FLASH_LOAD, 0x10, 0x00, 0x00, 0x01, 0x00, rate & 0xFF, (rate >> 8) & 0xFF]
        self._write_command(cmd, timeout=2.0, allow_cmd_error_after=allow_cmd_error)

    def flash_load_mini_and_boot(
        self,
        patch_addr: int,
        firmware_addr: int,
        full_patch_wait_ms: int = 4,
        nvmspi_rate_khz: int = 0,
        allow_cmd_error: bool = False,
    ) -> None:
        """
        SDK OPTION__BOOT_FROM_FLASH_MINI flow:
        LOAD_INIT -> FLASH_LOAD(full patch) -> LOAD_INIT -> FLASH_LOAD(firmware) -> BOOT
        """
        self._send_load_init(allow_cmd_error=allow_cmd_error)
        self.flash_load_strict(patch_addr, allow_cmd_error=allow_cmd_error)
        if full_patch_wait_ms > 0:
            time.sleep(full_patch_wait_ms / 1000.0)
        if nvmspi_rate_khz > 0:
            self.adjust_nvmspi_rate(nvmspi_rate_khz, allow_cmd_error=allow_cmd_error)
        self._send_load_init(allow_cmd_error=allow_cmd_error)
        self.flash_load_strict(firmware_addr, allow_cmd_error=allow_cmd_error)
        self._boot(allow_cmd_error=allow_cmd_error)

    def flash_enter_program_mode(self) -> None:
        """
        FIXED: Enter flash programming mode.

        The original command [0xB2, 0x55, 0x55] was incorrect because:
        - 0xB2 is CMD_DAB_DIGRAD_STATUS, causing a command conflict
        - Flash operations should use CMD_FLASH_LOAD (0x05) with magic bytes

        This method tries multiple known unlock sequences in order.
        """
        # Try method 1: CMD_FLASH_LOAD with 0xFF prefix + magic unlock
        try:
            self._write_command([CMD_FLASH_LOAD, 0xFF, 0x55, 0x55, 0x00, 0x00, 0x00, 0x00], timeout=3.0)
            print("[DEBUG] Flash program mode entered (method 1: CMD_FLASH_LOAD + 0xFF5555)")
            return
        except Exception as e1:
            print(f"[DEBUG] Method 1 failed: {e1}")

        # Try method 2: Extended magic sequence
        try:
            self._write_command([CMD_FLASH_LOAD, 0x55, 0x55, 0xAA, 0xAA, 0x00, 0x00, 0x00], timeout=3.0)
            print("[DEBUG] Flash program mode entered (method 2: CMD_FLASH_LOAD + 5555AAAA)")
            return
        except Exception as e2:
            print(f"[DEBUG] Method 2 failed: {e2}")

        # Try method 3: Simple 2-byte sequence
        try:
            self._write_command([CMD_FLASH_LOAD, 0x55, 0x55], timeout=3.0)
            print("[DEBUG] Flash program mode entered (method 3: CMD_FLASH_LOAD + 5555)")
            return
        except Exception as e3:
            print(f"[DEBUG] Method 3 failed: {e3}")

        # Try method 4: No explicit command (patch may already enable mode)
        try:
            print("[DEBUG] Attempting to proceed without explicit enter command (patch may enable mode)")
            time.sleep(0.1)
            return
        except Exception as e4:
            print(f"[DEBUG] Method 4 failed: {e4}")

        # If all methods fail, raise an error with helpful diagnostics
        raise RuntimeError(
            "Failed to enter flash programming mode. "
            "All known command sequences were rejected by Si468x. "
            "Please check:\n"
            "  1. Correct flash programming patch is loaded (rom00_patch_mini.003.bin)\n"
            "  2. Flash CS pin is properly configured if using external flash\n"
            "  3. Si468x firmware version supports flash programming\n"
            "  4. Consult Si468x programming guide for your specific chip revision"
        )

    def flash_erase_sector(self, start_addr: int) -> None:
        cmd = [
            CMD_FLASH_LOAD,
            0xFE,
            0xC0,
            0xDE,
            *list(int(start_addr).to_bytes(4, "little")),
        ]
        self._write_command(cmd, timeout=3.0)

    def flash_write_block(self, start_addr: int, data: bytes) -> None:
        if not data or len(data) > FLASH_WRITE_BLOCK:
            raise ValueError("Flash write block length invalid")
        addr_len = start_addr.to_bytes(4, "little") + len(data).to_bytes(4, "little")
        crc = 0xFFFFFFFF
        crc = _crc32_update(crc, addr_len)
        crc = _crc32_update(crc, data)
        crc ^= 0xFFFFFFFF
        cmd = [
            CMD_FLASH_LOAD,
            0xF3,
            0x0C,
            0xED,
            *list(crc.to_bytes(4, "little")),
            *list(addr_len),
            *list(data),
        ]
        self._write_command(cmd, timeout=2.0)

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

    def get_property(self, prop_id: int) -> int:
        cmd = [CMD_GET_PROPERTY, 0x00, prop_id & 0xFF, (prop_id >> 8) & 0xFF]
        self._write_command(cmd)
        reply = self._read_reply(4)
        return reply[-2] | (reply[-1] << 8)

    def configure_audio(
        self,
        mode: str = "analog",
        master: bool = True,
        sample_rate: int = 48_000,
        sample_size: int = 16,
    ) -> None:
        """
        mode: "analog" enables DAC only, "i2s" enables I2S (DAC off to avoid overriding I2S).
        """
        # PROP 0x0800 PIN_CONFIG_ENABLE: bit1=I2SOUTEN, bit0=DACOUTEN
        pin_cfg = 0x8000  # keep defaults, INTB enabled
        if mode == "analog":
            pin_cfg |= 0x0001  # DAC only
        elif mode == "i2s":
            pin_cfg |= 0x0002  # I2S only (leave DAC disabled to honor I2S path)
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

    # ------------------------------------------------------------------
    # FM control
    # ------------------------------------------------------------------
    def fm_tune(
        self,
        freq_khz: int,
        antcap: int = 0,
        tune_mode: int = 0,
        injection: int = 0,
        dir_tune: int = 0,
    ) -> None:
        freq_10khz = int(round(freq_khz / 10))
        arg1 = ((dir_tune & 0x01) << 5) | ((tune_mode & 0x03) << 2) | (injection & 0x03)
        cmd = [
            CMD_FM_TUNE_FREQ,
            arg1,
            freq_10khz & 0xFF,
            (freq_10khz >> 8) & 0xFF,
            antcap & 0xFF,
            (antcap >> 8) & 0xFF,
            0x00,
        ]
        self._write_command(cmd)

    def fm_rsq_status(self, attune: bool = True, stcack: bool = False) -> Dict[str, int]:
        flags = (0x04 if attune else 0x00) | (0x01 if stcack else 0x00)
        self._write_command([CMD_FM_RSQ_STATUS, flags])
        reply = self._read_reply(23)
        readfreq_10khz = int.from_bytes(reply[6:8], "little")
        return {
            "valid": bool(reply[5] & 0x01),
            "rssi": _signed_byte(reply[9]),
            "snr": _signed_byte(reply[10]),
            "freqoff": _signed_byte(reply[8]),
            "freq_10khz": readfreq_10khz,
            "freq_khz": readfreq_10khz * 10,
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
            if self.bus is not None:
                self.bus.close()
            if self.spi is not None:
                self.spi.close()
        finally:
            try:
                GPIO.cleanup(self.rst_pin)
                if self.int_pin is not None:
                    GPIO.cleanup(self.int_pin)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Flash boot helpers
# ---------------------------------------------------------------------------
# Optional GPIO gate for external flash CS or mux.
def _make_flash_cs(
    pin: Optional[int],
    active_high: bool,
    hold_ms: int,
) -> Optional[Callable[[bool], None]]:
    if pin is None:
        return None
    if GPIO is None:
        raise RuntimeError("--flash-cs-pin requires RPi.GPIO")
    active_level = GPIO.HIGH if active_high else GPIO.LOW
    inactive_level = GPIO.LOW if active_high else GPIO.HIGH
    GPIO.setup(pin, GPIO.OUT, initial=inactive_level)

    def _set(active: bool) -> None:
        GPIO.output(pin, active_level if active else inactive_level)
        if hold_ms > 0:
            time.sleep(hold_ms / 1000.0)

    return _set


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------
def parse_args() -> argparse.Namespace:
    default_patch = "./rom00_patch.016.bin"
    default_fw = "./dab_radio_6_0_9.bin"

    parser = argparse.ArgumentParser(description="Play DAB via Si468x on Raspberry Pi (I2C or SPI host load).")
    parser.add_argument("--patch", type=Path, default=default_patch, help="Path to rom00 patch image")
    parser.add_argument("--firmware", type=Path, default=default_fw, help="Path to dab_radio firmware image")
    parser.add_argument(
        "--flash-boot",
        action="store_true",
        help=(
            "Boot using SDK FLASH_MINI flow: host-load mini patch, then load full patch + firmware "
            "from external NVM flash."
        ),
    )
    parser.add_argument(
        "--flash-program",
        action="store_true",
        help="Program full patch and firmware into external NVM flash before booting.",
    )
    parser.add_argument(
        "--flash-program-image",
        type=Path,
        default=None,
        help="Firmware image to program into NVM flash at --flash-addr (default: --firmware).",
    )
    parser.add_argument(
        "--flash-program-patch",
        type=Path,
        default=None,
        help=(
            "Bootstrap patch loaded from host before flash programming commands "
            "(default: --flash-mini-patch if available)."
        ),
    )
    parser.add_argument(
        "--flash-program-patch-image",
        type=Path,
        default=None,
        help="Full patch image to store in flash at --flash-patch-addr (default: --patch).",
    )
    parser.add_argument(
        "--flash-program-only",
        action="store_true",
        help="Exit after flash programming (no boot).",
    )
    parser.add_argument(
        "--flash-self-test",
        action="store_true",
        help=(
            "Run a flash boot self-test (FLASH_MINI then FLASH_FULL fallback) and exit. "
            "Useful to validate that programmed images are actually bootable."
        ),
    )
    parser.add_argument(
        "--flash-addr",
        type=lambda x: int(x, 0),
        default=FLASH_ADDR_DAB,
        help="Flash start address for DAB firmware image (default: 0x00092000).",
    )
    parser.add_argument(
        "--flash-patch-addr",
        type=lambda x: int(x, 0),
        default=FLASH_ADDR_PATCH_FULL,
        help="Flash start address for full patch image (default: 0x00004000).",
    )
    parser.add_argument(
        "--flash-mini-patch",
        type=Path,
        default=Path("./rom00_patch_mini.003.bin"),
        help="Mini patch image used for FLASH_MINI host-load step (default: ./rom00_patch_mini.003.bin).",
    )
    parser.add_argument(
        "--flash-mini-patch-wait-ms",
        type=int,
        default=4,
        help="Extra wait after host-loading mini patch before FLASH_LOAD (default: 4 ms SDK).",
    )
    parser.add_argument(
        "--flash-full-patch-wait-ms",
        type=int,
        default=4,
        help="Wait after FLASH_LOAD(full patch) before loading firmware (default: 4 ms SDK).",
    )
    parser.add_argument(
        "--nvmspi-rate-khz",
        type=int,
        default=0,
        help=(
            "Optional NVM SPI clock in kHz via raw FLASH_LOAD 0x10 command "
            "(0 disables, SDK-equivalent behavior)."
        ),
    )
    parser.add_argument(
        "--flash-enter-mode-before-load",
        action="store_true",
        help=(
            "Legacy option for non-SDK experiments. Ignored for SDK-compliant flash boot paths "
            "(FLASH_MINI/FLASH_FULL)."
        ),
    )
    parser.add_argument(
        "--flash-cs-pin",
        type=int,
        default=None,
        help="GPIO (BCM) used to select external flash during Si468x flash ops.",
    )
    parser.add_argument(
        "--flash-cs-active-high",
        action="store_true",
        help="Treat --flash-cs-pin as active-high (default active-low).",
    )
    parser.add_argument(
        "--flash-cs-hold-ms",
        type=int,
        default=1,
        help="Delay after toggling flash CS in ms (default 1).",
    )
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
    parser.add_argument("--i2c-bus", type=int, default=1, help="I2C bus number (default 1)")
    parser.add_argument(
        "--i2c-addr",
        type=lambda x: int(x, 0),
        default=0x64,
        help="I2C address (7-bit) for Si468x (default 0x64)",
    )
    parser.add_argument(
        "--skip-reset",
        action="store_true",
        help="Do not toggle RSTB (useful if interface mode is latched by pins).",
    )
    parser.add_argument(
        "--reset-delay-ms",
        type=int,
        default=200,
        help="Delay after RSTB goes high (ms, default 200).",
    )
    parser.add_argument(
        "--i2c-retries",
        type=int,
        default=3,
        help="Retries for I2C POWER_UP write on timeout (default 3).",
    )
    parser.add_argument(
        "--refresh-services-on-tune",
        action="store_true",
        default=True,
        help="After tuning, refresh service list from the current ensemble (default).",
    )
    parser.add_argument(
        "--no-refresh-services-on-tune",
        dest="refresh_services_on_tune",
        action="store_false",
        help="Do not refresh the service list after tuning.",
    )
    parser.add_argument(
        "--no-force-reset-on-error",
        dest="force_reset_on_error",
        action="store_false",
        default=True,
        help="Do not force a reset during recovery (default is to reset).",
    )
    parser.add_argument(
        "--spi",
        action="store_true",
        default=True,
        help="Use SPI for Si468x control (default).",
    )
    parser.add_argument(
        "--i2c",
        dest="spi",
        action="store_false",
        help="Use I2C instead of SPI for Si468x control.",
    )
    parser.add_argument("--spi-bus", type=int, default=0, help="SPI bus number (default 0)")
    parser.add_argument("--spi-dev", type=int, default=0, help="SPI device number (default 0)")
    parser.add_argument("--spi-speed", type=int, default=30_000_000, help="SPI speed in Hz (default 30000000)")
    parser.add_argument("--rst-pin", type=int, default=25, help="GPIO (BCM) for RSTB (default 25 / physical 22)")
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
    parser.add_argument("--fm-freq", type=float, help="FM frequency to tune (MHz or kHz)")
    parser.add_argument("--fm-scan", action="store_true", help="Scan the FM band for stations")
    parser.add_argument("--fm-min", type=float, default=FM_BAND_DEFAULT_MIN_KHZ, help="FM min (MHz or kHz)")
    parser.add_argument("--fm-max", type=float, default=FM_BAND_DEFAULT_MAX_KHZ, help="FM max (MHz or kHz)")
    parser.add_argument("--fm-step", type=float, default=FM_BAND_DEFAULT_STEP_KHZ, help="FM step (kHz)")
    parser.add_argument("--fm-snr-min", type=int, default=0, help="FM scan SNR threshold (default 0)")
    parser.add_argument("--fm-rssi-min", type=int, default=0, help="FM scan RSSI threshold (default 0)")
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
    fm_requested = args.fm_scan or args.fm_freq is not None
    flash_fw_addr = args.flash_addr
    flash_patch_addr = args.flash_patch_addr
    flash_boot_requested = args.flash_boot
    flash_program_requested = args.flash_program
    flash_program_only = args.flash_program_only
    flash_self_test_requested = args.flash_self_test
    flash_program_image = args.flash_program_image or firmware_path
    flash_program_patch_image = args.flash_program_patch_image or patch_path
    flash_mini_patch = args.flash_mini_patch
    if args.flash_program_patch is not None:
        flash_program_loader_patch = args.flash_program_patch
    else:
        flash_program_loader_patch = flash_mini_patch if flash_mini_patch.exists() else patch_path
    flash_boot_loader_patch = flash_mini_patch if flash_mini_patch.exists() else patch_path
    if not patch_path.exists():
        raise SystemExit(f"Patch image not found: {patch_path}")
    if not firmware_path.exists():
        raise SystemExit(f"Firmware image not found: {firmware_path}")
    if flash_boot_requested and not flash_boot_loader_patch.exists():
        raise SystemExit(f"Flash boot mini patch not found: {flash_boot_loader_patch}")
    if flash_program_requested:
        if not flash_program_image.exists():
            raise SystemExit(f"Flash program image not found: {flash_program_image}")
        if not flash_program_patch_image.exists():
            raise SystemExit(f"Flash program patch image not found: {flash_program_patch_image}")
        if not flash_program_loader_patch.exists():
            raise SystemExit(f"Flash program loader patch not found: {flash_program_loader_patch}")
        print(f"Flash loader patch: {flash_program_loader_patch}")

    radio = Si468xDabRadio(
        i2c_bus=args.i2c_bus,
        i2c_addr=args.i2c_addr,
        rst_pin=args.rst_pin,
        int_pin=args.int_pin,
        use_spi=args.spi,
        spi_bus=args.spi_bus,
        spi_dev=args.spi_dev,
        spi_speed_hz=args.spi_speed,
        rst_initial_high=args.skip_reset,
    )
    radio.reset_post_ms = max(0, int(args.reset_delay_ms))
    if args.skip_reset:
        time.sleep(radio.reset_post_ms / 1000.0)

    flash_cs = _make_flash_cs(args.flash_cs_pin, args.flash_cs_active_high, args.flash_cs_hold_ms)
    if flash_cs:
        level = "high" if args.flash_cs_active_high else "low"
        print(f"Flash CS GPIO configured on BCM {args.flash_cs_pin} (active {level}).")

    flash_boot_mode_active: Optional[str] = None

    def do_reset(force: bool = False) -> None:
        if args.skip_reset and not force:
            return
        radio.reset()

    def do_power_up() -> None:
        retries = args.i2c_retries if not args.spi else 1
        radio.power_up(xtal_freq=args.xtal, ctun=args.ctun, retries=retries)

    def flash_boot_mini() -> None:
        # First try the user-selected patch slot, then the two SDK defaults.
        patch_candidates: List[int] = []
        for candidate in (int(flash_patch_addr), FLASH_ADDR_PATCH_FULL, FLASH_ADDR_PATCH_UPDATE):
            if candidate not in patch_candidates:
                patch_candidates.append(candidate)

        last_exc: Optional[Exception] = None
        for attempt_idx, patch_addr_candidate in enumerate(patch_candidates):
            if attempt_idx > 0:
                print(
                    f"[FLASH_MINI] retry from clean reset with patch@0x{patch_addr_candidate:08X}..."
                )
                do_reset(force=True)
                do_power_up()

            if flash_cs:
                flash_cs(True)
            try:
                # SDK OPTION__BOOT_FROM_FLASH_MINI:
                # host-load mini patch, then flash-load full patch + firmware.
                radio.load_patch_only(flash_boot_loader_patch, allow_cmd_error=True)
                if args.flash_mini_patch_wait_ms > 0:
                    time.sleep(args.flash_mini_patch_wait_ms / 1000.0)
                if args.flash_enter_mode_before_load:
                    print(
                        "[FLASH_MINI] ignoring --flash-enter-mode-before-load for SDK compliance."
                    )
                radio.flash_load_mini_and_boot(
                    patch_addr_candidate,
                    flash_fw_addr,
                    full_patch_wait_ms=args.flash_full_patch_wait_ms,
                    nvmspi_rate_khz=args.nvmspi_rate_khz,
                    allow_cmd_error=True,
                )
                return
            except Exception as exc:
                last_exc = exc
                print(
                    f"[FLASH_MINI] failed with patch@0x{patch_addr_candidate:08X}: {exc}"
                )
            finally:
                if flash_cs:
                    flash_cs(False)

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("FLASH_MINI failed without a captured exception")

    def flash_boot_full() -> None:
        # SDK OPTION__BOOT_FROM_FLASH_FULL equivalent:
        # host-load full patch, then flash-load firmware image.
        if flash_cs:
            flash_cs(True)
        try:
            radio.load_patch_only(patch_path, allow_cmd_error=True)
            if args.flash_full_patch_wait_ms > 0:
                time.sleep(args.flash_full_patch_wait_ms / 1000.0)
            if args.flash_enter_mode_before_load:
                print(
                    "[FLASH_FULL] ignoring --flash-enter-mode-before-load for SDK compliance."
                )
            if args.nvmspi_rate_khz > 0:
                radio.adjust_nvmspi_rate(args.nvmspi_rate_khz, allow_cmd_error=True)
            radio.flash_load_and_boot(flash_fw_addr, allow_cmd_error=True)
        finally:
            if flash_cs:
                flash_cs(False)

    def flash_program_image_at(image_path: Path, start_addr: int, label: str) -> None:
        image_size = image_path.stat().st_size
        sectors = (image_size + FLASH_SECTOR_SIZE - 1) // FLASH_SECTOR_SIZE
        print(f"Programming {label} @0x{start_addr:08X} ({image_size} bytes, {sectors} sectors)...")
        for i in range(sectors):
            addr = start_addr + (i * FLASH_SECTOR_SIZE)
            radio.flash_erase_sector(addr)
            if (i + 1) % 16 == 0 or i == sectors - 1:
                print(f"  erase sector {i + 1}/{sectors} @0x{addr:08X}")
        written = 0
        with image_path.open("rb") as handle:
            while True:
                chunk = handle.read(FLASH_WRITE_BLOCK)
                if not chunk:
                    break
                radio.flash_write_block(start_addr + written, chunk)
                written += len(chunk)
                if written % (FLASH_WRITE_BLOCK * 64) == 0 or written == image_size:
                    print(f"  wrote {written}/{image_size} bytes ({label})")

    def flash_program() -> None:
        saved_spi_speed = None
        if radio.use_spi and radio.spi is not None:
            saved_spi_speed = radio.spi.max_speed_hz
            radio.spi.max_speed_hz = min(int(saved_spi_speed), 1_000_000)
        if flash_cs:
            flash_cs(True)
        try:
            time.sleep(0.05)
            radio.flash_enter_program_mode()
            flash_program_image_at(flash_program_patch_image, flash_patch_addr, "full patch")
            flash_program_image_at(flash_program_image, flash_fw_addr, "firmware")
        finally:
            if flash_cs:
                flash_cs(False)
            if saved_spi_speed is not None and radio.spi is not None:
                radio.spi.max_speed_hz = saved_spi_speed

    def run_flash_self_test() -> bool:
        nonlocal flash_boot_mode_active
        print(
            "[SELFTEST] Verifying flash bootability "
            f"(mini={flash_boot_loader_patch}, patch@0x{flash_patch_addr:08X}, fw@0x{flash_fw_addr:08X})..."
        )

        # Try SDK FLASH_MINI first.
        try:
            do_reset(force=True)
            do_power_up()
            flash_boot_mini()
            radio.dab_digrad_status()
            flash_boot_mode_active = "mini"
            print("[SELFTEST] PASS: FLASH_MINI boot succeeded and DAB status is readable.")
            return True
        except Exception as exc:
            print(f"[SELFTEST] FLASH_MINI failed: {exc}")

        # Then try SDK FLASH_FULL equivalent.
        try:
            do_reset(force=True)
            do_power_up()
            flash_boot_full()
            radio.dab_digrad_status()
            flash_boot_mode_active = "full"
            print("[SELFTEST] PASS: FLASH_FULL fallback boot succeeded and DAB status is readable.")
            return True
        except Exception as exc:
            print(f"[SELFTEST] FLASH_FULL failed: {exc}")

        flash_boot_mode_active = None
        print("[SELFTEST] FAIL: no flash boot path succeeded.")
        return False

    # Helper to recover the radio after a command error
    def recover_radio(reason: str) -> bool:
        nonlocal flash_boot_mode_active
        print(f"[RECOVER] Reinitializing radio after error: {reason}")
        try:
            do_reset(force=args.force_reset_on_error)
            do_power_up()
            if flash_boot_mode_active == "mini":
                try:
                    flash_boot_mini()
                except Exception as exc:
                    print(f"[RECOVER] Flash-mini boot failed, trying flash-full: {exc}")
                    try:
                        do_reset(force=True)
                        do_power_up()
                        flash_boot_full()
                        flash_boot_mode_active = "full"
                    except Exception as exc2:
                        print(f"[RECOVER] Flash-full boot failed, falling back to host load: {exc2}")
                        radio.load_patch_and_firmware(patch_path, firmware_path)
                        flash_boot_mode_active = None
            elif flash_boot_mode_active == "full":
                try:
                    flash_boot_full()
                except Exception as exc:
                    print(f"[RECOVER] Flash-full boot failed, falling back to host load: {exc}")
                    radio.load_patch_and_firmware(patch_path, firmware_path)
                    flash_boot_mode_active = None
            else:
                radio.load_patch_and_firmware(patch_path, firmware_path)
            radio.configure_audio(
                mode=args.audio_out,
                master=args.i2s_master,
                sample_rate=args.sample_rate,
                sample_size=args.sample_size,
            )
            radio.configure_dab_frontend()
            radio.set_dab_freq_list(band_freqs)
            return True
        except Exception as exc:  # pragma: no cover
            print(f"[RECOVER] Failed to reinitialize radio: {exc}")
            return False

    try:
        if flash_program_requested:
            print("Flash programming requested (via Si468x)...")
            do_reset()
            do_power_up()
            print(f"Loading flash-program bootstrap patch: {flash_program_loader_patch}")
            radio.load_patch_only(flash_program_loader_patch)
            flash_program()
            if flash_program_only and not flash_self_test_requested:
                return
            do_reset()
        if flash_self_test_requested:
            if not run_flash_self_test():
                raise SystemExit("Flash self-test failed.")
            return
        print("Resetting SI468x...")
        do_reset()
        print(f"Powering up ROM... (xtal={args.xtal} ctun=0x{args.ctun:02X})")
        do_power_up()
        if flash_boot_requested:
            print(
                "Booting from flash (FLASH_MINI): "
                f"mini patch={flash_boot_loader_patch}, patch@0x{flash_patch_addr:08X}, "
                f"fw@0x{flash_fw_addr:08X}"
            )
            try:
                flash_boot_mini()
                # Verify DAB firmware responds
                radio.dab_digrad_status()
                flash_boot_mode_active = "mini"
                print("Flash-mini boot successful.")
            except Exception as exc:
                print(f"Flash-mini boot failed: {exc}")
                print(
                    "Trying flash-full fallback (host-load full patch + flash-load firmware)..."
                )
                try:
                    do_reset(force=True)
                    do_power_up()
                    flash_boot_full()
                    radio.dab_digrad_status()
                    flash_boot_mode_active = "full"
                    print("Flash-full fallback boot successful.")
                except Exception as exc2:
                    print(f"Flash-full fallback failed: {exc2}")
                    print("Falling back to host-load firmware...")
                    do_reset()
                    do_power_up()
                    radio.load_patch_and_firmware(patch_path, firmware_path)
                    flash_boot_mode_active = None
        else:
            print("Loading patch and firmware (this takes a few seconds)...")
            radio.load_patch_and_firmware(patch_path, firmware_path)
        print("Configuring audio output...")
        radio.configure_audio(
            mode=args.audio_out,
            master=args.i2s_master,
            sample_rate=args.sample_rate,
            sample_size=args.sample_size,
        )

        if fm_requested:
            fm_min_khz = _mhz_or_khz_to_khz(float(args.fm_min))
            fm_max_khz = _mhz_or_khz_to_khz(float(args.fm_max))
            fm_step_khz = max(10, int(round(float(args.fm_step))))
            fm_freq_khz = _mhz_or_khz_to_khz(args.fm_freq) if args.fm_freq is not None else None
            fm_snr_min = int(args.fm_snr_min)
            fm_rssi_min = int(args.fm_rssi_min)
            fm_cmd_error_hint_shown = False

            if fm_min_khz >= fm_max_khz:
                raise SystemExit("FM band limits invalid (min >= max)")

            def fm_tune_and_status(freq_khz: int) -> Optional[Dict[str, int]]:
                nonlocal fm_cmd_error_hint_shown
                try:
                    radio.fm_tune(freq_khz)
                except RuntimeError as err:
                    print(f"FM_TUNE_FREQ failed: {err}")
                    if not fm_cmd_error_hint_shown:
                        print(
                            "FM command rejected by firmware. "
                            "Make sure your firmware build includes FM support."
                        )
                        fm_cmd_error_hint_shown = True
                    return None
                time.sleep(0.06)
                return radio.fm_rsq_status(attune=True)

            def fm_scan() -> List[Dict[str, int]]:
                stations: List[Dict[str, int]] = []
                total = ((fm_max_khz - fm_min_khz) // fm_step_khz) + 1
                print(
                    f"Scanning FM {fm_min_khz/1000:.1f}-{fm_max_khz/1000:.1f} MHz "
                    f"(step {fm_step_khz} kHz, {total} steps)..."
                )
                for idx, freq_khz in enumerate(range(fm_min_khz, fm_max_khz + 1, fm_step_khz)):
                    status = fm_tune_and_status(freq_khz)
                    if status is None:
                        print("FM commands not supported by this firmware image.")
                        break
                    if status["valid"] and status["snr"] >= fm_snr_min and status["rssi"] >= fm_rssi_min:
                        stations.append(
                            {
                                "freq_khz": freq_khz,
                                "rssi": status["rssi"],
                                "snr": status["snr"],
                            }
                        )
                        print(
                            f"  found {freq_khz/1000:.1f} MHz "
                            f"RSSI={status['rssi']} SNR={status['snr']}"
                        )
                    if idx % 50 == 0 and idx:
                        print(f"  progress {idx}/{total}")
                return stations

            stations: List[Dict[str, int]] = []
            if args.fm_scan or fm_freq_khz is None:
                stations = fm_scan()

            current_freq = fm_freq_khz
            if current_freq is None and stations:
                current_freq = stations[0]["freq_khz"]
            if current_freq is None:
                current_freq = fm_min_khz

            status = fm_tune_and_status(current_freq)
            if status is None:
                return
            print(f"FM tuned to {current_freq/1000:.1f} MHz")
            current_volume = radio.set_volume(40)
            print(f"Initial volume set to {current_volume}/63.")

            def print_menu_fm() -> None:
                print(
                    "\nCommands: <index> | f<freq MHz> | + / - volume | s status | l list | r rescan | q quit"
                )
                if stations:
                    print("Stations:")
                    for idx, st in enumerate(stations):
                        print(
                            f"  [{idx}] {st['freq_khz']/1000:.1f} MHz  "
                            f"RSSI={st['rssi']} SNR={st['snr']}"
                        )

            def print_status_fm() -> None:
                st = radio.fm_rsq_status(attune=True)
                gauge = _format_fm_bar(st)
                print(
                    f"FM {st['freq_khz']/1000:.1f} MHz RSSI={st['rssi']} "
                    f"SNR={st['snr']} {gauge} VALID={st['valid']}"
                )

            def parse_freq_cmd(text: str) -> Optional[int]:
                cleaned = text.strip().lower()
                if cleaned.startswith("f"):
                    cleaned = cleaned[1:]
                if not cleaned:
                    return None
                try:
                    value = float(cleaned)
                except ValueError:
                    return None
                return _mhz_or_khz_to_khz(value)

            print_menu_fm()
            print_status_fm()
            next_status = time.time() + 1.0
            fd = sys.stdin.fileno()
            old_tty = termios.tcgetattr(fd)
            input_buf = ""
            try:
                tty.setcbreak(fd)
                sys.stdout.write("radio> ")
                sys.stdout.flush()
                while True:
                    timeout = max(0.0, next_status - time.time())
                    ready, _, _ = select.select([sys.stdin], [], [], timeout)
                    if ready:
                        ch = sys.stdin.read(1)
                        if ch in ("\n", "\r"):
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                            cmd = input_buf.strip()
                            input_buf = ""
                        elif ch in ("\x7f", "\b"):
                            if input_buf:
                                input_buf = input_buf[:-1]
                                sys.stdout.write("\b \b")
                                sys.stdout.flush()
                            continue
                        else:
                            input_buf += ch
                            sys.stdout.write(ch)
                            sys.stdout.flush()
                            continue
                    else:
                        sys.stdout.write("\n")
                        print_status_fm()
                        sys.stdout.write("radio> " + input_buf)
                        sys.stdout.flush()
                        next_status = time.time() + 1.0
                        continue

                    next_status = time.time() + 1.0
                    if cmd == "":
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue
                    if cmd.lower() == "q":
                        print("Leaving radio playing. Bye.")
                        break
                    if cmd.lower() == "r":
                        stations = fm_scan()
                        print("Rescan complete.")
                        print_menu_fm()
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue
                    if cmd.lower() == "l":
                        print_menu_fm()
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue
                    if cmd and set(cmd) == {"+"}:
                        current_volume = radio.set_volume(current_volume + (2 * len(cmd)))
                        print(f"Volume {current_volume}/63")
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue
                    if cmd and set(cmd) == {"-"}:
                        current_volume = radio.set_volume(current_volume - (2 * len(cmd)))
                        print(f"Volume {current_volume}/63")
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue
                    if cmd.lower() == "s":
                        print_status_fm()
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue

                    tuned = False
                    if stations and cmd.isdigit():
                        idx = int(cmd)
                        if 0 <= idx < len(stations):
                            current_freq = stations[idx]["freq_khz"]
                            tuned = True
                    if not tuned:
                        freq_cmd = parse_freq_cmd(cmd)
                        if freq_cmd:
                            current_freq = freq_cmd
                            tuned = True
                    if tuned and current_freq is not None:
                        status = fm_tune_and_status(current_freq)
                        if status is not None:
                            print(f"Tuned to {current_freq/1000:.1f} MHz")
                        sys.stdout.write("radio> ")
                        sys.stdout.flush()
                        continue

                    print("Unknown command.")
                    print_menu_fm()
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_tty)
            return

        print("Configuring DAB frontend...")
        radio.configure_dab_frontend()
        if args.audio_out == "analog":
            radio.set_property(PROP_AUDIO_ANALOG_VOLUME, 0x003F)
            vol = radio.get_property(PROP_AUDIO_ANALOG_VOLUME)
            pin_cfg = radio.get_property(PROP_PIN_CONFIG_ENABLE)
            dac_on = "on" if (pin_cfg & 0x0001) else "off"
            print(f"Analog volume=0x{vol:04X} PIN_CFG=0x{pin_cfg:04X} DAC={dac_on}")

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
            for attempt in range(2):
                try:
                    radio.dab_tune(idx, antcap=args.antcap)
                    break
                except RuntimeError as err:
                    print(f"DAB_TUNE_FREQ failed: {err}")
                    if not recover_radio("tune failure"):
                        return None
                    if attempt == 1:
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
                    gauge = _format_reception_bar(status)
                    print(
                        f"  waiting lock... RSSI={status['rssi']} SNR={status['snr']} "
                        f"FICQ={status['fic_quality']} {gauge} ACQ={status['acq']} VALID={status['valid']}"
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
            if args.refresh_services_on_tune:
                refreshed = grab_services()
                if refreshed:
                    match = next(
                        (s for s in refreshed if s.get("service_id") == service.get("service_id")),
                        None,
                    )
                    if match is None:
                        print("Service not found in current ensemble; aborting.")
                        return
                    service = match
            # Check ACQ/VALID + minimal metrics again just before starting service
            status = radio.dab_digrad_status()
            if not status.get("valid", 0) or not status.get("acq", 0):
                print("Channel not valid/acquired; service start aborted.")
                return
            # Optional soft thresholds to avoid weak/false locks
            if status.get("fic_quality", 0) == 0 or status.get("snr", 0) == 0:
                print(
                    f"Weak lock (SNR={status.get('snr',0)} FICQ={status.get('fic_quality',0)}); "
                    "service start aborted."
                )
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
            for attempt in range(2):
                try:
                    radio.start_digital_service(int(service["service_id"]), int(service["component_id"]))
                    break
                except RuntimeError as err:
                    print(f"START_DIGITAL_SERVICE failed: {err}")
                    if not recover_radio("start service failure"):
                        return
                    if attempt == 1:
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
            print(
                "\nCommands: number=<index> | name substring | + / - volume | s status | o toggle audio out | "
                "r rescan | q quit"
            )
            print("Stations:")
            for idx, svc in enumerate(services):
                fi = svc.get("freq_index", -1)
                fk = svc.get("freq_khz", 0)
                print(
                    f"  [{idx}] {svc.get('label','')}  SID=0x{svc['service_id']:08X} "
                    f"COMP=0x{svc['component_id']:04X}  FreqIdx={fi} ({fk} kHz)"
                )

        def print_status_line() -> None:
            status = radio.dab_digrad_status()
            gauge = _format_reception_bar(status)
            print(
                f"Status: RSSI={status['rssi']} SNR={status['snr']} "
                f"FICQ={status['fic_quality']} {gauge} CNR={status['cnr']} "
                f"ACQ={status['acq']} VALID={status['valid']} tuneIdx={status['tune_index']}"
            )

        print_menu()
        print_status_line()
        next_status = time.time() + 1.0
        fd = sys.stdin.fileno()
        old_tty = termios.tcgetattr(fd)
        input_buf = ""
        try:
            tty.setcbreak(fd)
            sys.stdout.write("radio> ")
            sys.stdout.flush()
            while True:
                timeout = max(0.0, next_status - time.time())
                ready, _, _ = select.select([sys.stdin], [], [], timeout)
                if ready:
                    ch = sys.stdin.read(1)
                    if ch in ("\n", "\r"):
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        cmd = input_buf.strip()
                        input_buf = ""
                    elif ch in ("\x7f", "\b"):
                        if input_buf:
                            input_buf = input_buf[:-1]
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                        continue
                    else:
                        input_buf += ch
                        sys.stdout.write(ch)
                        sys.stdout.flush()
                        continue
                else:
                    sys.stdout.write("\n")
                    print_status_line()
                    sys.stdout.write("radio> " + input_buf)
                    sys.stdout.flush()
                    next_status = time.time() + 1.0
                    continue

                next_status = time.time() + 1.0
                if cmd == "":
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
                    continue
                if cmd.lower() == "q":
                    print("Leaving radio playing. Bye.")
                    break
                if cmd.lower() == "r":
                    services = ensure_services()
                    services = sorted(services, key=lambda s: s.get("label", ""))
                    print("Rescan complete.")
                    print_menu()
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
                    continue
                if cmd and set(cmd) == {"+"}:
                    current_volume = radio.set_volume(current_volume + (2 * len(cmd)))
                    print(f"Volume {current_volume}/63")
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
                    continue
                if cmd and set(cmd) == {"-"}:
                    current_volume = radio.set_volume(current_volume - (2 * len(cmd)))
                    print(f"Volume {current_volume}/63")
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
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
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
                    continue
                if cmd.lower() == "s":
                    status = radio.dab_digrad_status()
                    gauge = _format_reception_bar(status)
                    print(
                        f"Status: RSSI={status['rssi']} SNR={status['snr']} "
                        f"FICQ={status['fic_quality']} {gauge} CNR={status['cnr']} "
                        f"ACQ={status['acq']} VALID={status['valid']} tuneIdx={status['tune_index']}"
                    )
                    next_status = time.time() + 1.0
                    sys.stdout.write("radio> ")
                    sys.stdout.flush()
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
                    print_menu()
                sys.stdout.write("radio> ")
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_tty)
    finally:
        radio.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(1)
