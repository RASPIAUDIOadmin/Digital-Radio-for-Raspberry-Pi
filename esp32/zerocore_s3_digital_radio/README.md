# ZeroCore S3 digital radio prototype

This PlatformIO application controls the RASPIAUDIO Digital Radio shield's
SI4689 from a ZeroCore S3 through the 40-pin header. It ports the radio boot
and tune sequence from the [earlier ESP32 prototype](https://github.com/RASPIAUDIOadmin/RM_Digital_Radio)
and drives the shield's analog jack and speaker amplifier. Control is through
the USB serial port at 115200 baud.

## Current status

- **FM works on the assembled hardware.** The SI4689 firmware loads, the FM
  scan finds stations, and tuning 101.10 MHz reported `valid=1`, RSSI 46 and
  SNR 16 dB on the test board. Audio through the shield was confirmed by
  listening.
- The DAB firmware loads and the tuner responds to Band III tune commands.
  No multiplex locked in the test location. DAB service listing and playback
  are not yet exposed by this serial application.
- HD Radio program selection is not yet exposed by this serial application.

This is a focused radio bring-up application. The Raspberry Pi Python backend
and web UI in the root of this repository are separate software.

## Hardware connections

The numbers in the first column are physical 40-pin header positions. These
ESP32 GPIO assignments come from the [ZeroCore S3 pinout](https://github.com/RASPIAUDIO/ZeroCore-S3/blob/main/docs/pinout.md);
the shield signals come from this repository's [header pinout](../../README.md#raspberry-pi-header-pinout).

| Header pin | Shield function | ZeroCore S3 GPIO |
| ---: | --- | ---: |
| 11 | Amplifier enable | 1 |
| 19 | SPI MOSI | 11 |
| 21 | SPI MISO | 13 |
| 22 | SI4689 reset | 39 |
| 23 | SPI clock | 12 |
| 24 | SPI CS0 | 10 |
| 16 | Radio interrupt, unused by this app | 41 |

The application enables analog audio on the SI4689. Its amplifier GPIO is
active high and starts off. USB-C provides power and the serial connection.

## Build

Install [PlatformIO](https://platformio.org/) and run from this directory:

```sh
pio run -e zerocore_s3
```

`embed_firmware.py` generates `src/firmware_images.cpp` from the four SI4689
images in `data/` before compilation. That generated file and `.pio/` are
ignored by Git. The target is an ESP32-S3 with 16 MB flash and 8 MB PSRAM.

## Flash while preserving the existing application

The tested board had this partition table: `app0` at `0x10000` and `app1` at
`0x7D0000`, each `0x7C0000` bytes. Verify the board's partition table before
using these addresses. The existing application was preserved in `app0` and
this radio application was written to `app1`:

```sh
esptool --port COM8 --baud 921600 write_flash 0x7D0000 .pio/build/zerocore_s3/firmware.bin
python <ESP_IDF_PATH>/components/app_update/otatool.py --port COM8 switch_ota_partition --slot 1
```

Replace `COM8` with the actual port. Power cycle the ZeroCore S3 after
flashing. On the tested board, the RTS reset issued by `esptool` left the
ESP32-S3 in download mode, while a USB power cycle started the application.

To select the preserved application again, use the same ESP-IDF tool with
`switch_ota_partition --slot 0`, then power cycle.

## Serial commands

| Command | Action |
| --- | --- |
| `help` | List commands |
| `f 101.1` | Tune FM to 101.10 MHz |
| `scan` | Scan FM from 87.5 to 108 MHz |
| `s` | Show current FM or DAB tuner status |
| `amp on`, `amp off` | Control the shield amplifier |
| `v 40` | Set SI4689 analog volume, range 0–63 |
| `mode dab`, `mode fm` | Load the selected SI4689 firmware |
| `d 8C` | Tune a DAB Band III multiplex |

FM is loaded at power-up. For speaker playback, tune a valid FM station and
send `amp on`. The jack uses the SI4689 analog output.
