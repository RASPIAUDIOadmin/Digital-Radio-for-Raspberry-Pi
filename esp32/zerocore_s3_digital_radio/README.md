# ESP32-S3 digital radio prototype

This PlatformIO application controls the RASPIAUDIO Digital Radio shield's
SI4689 from a ZeroCore S3 through the 40-pin header, or from a wired ESP32-S3
development board. It ports the radio boot and tune sequence from the
[earlier ESP32 prototype](https://github.com/RASPIAUDIOadmin/RM_Digital_Radio)
and drives the shield's analog jack and speaker amplifier. Control is through
the USB serial port at 115200 baud; an optional
[local web version](WEB.md) adds a page for a phone or computer over Wi-Fi or
the device's own hotspot.

## Current status

- **FM works on the assembled hardware.** The SI4689 firmware loads, the FM
  scan finds stations, and tuning 101.10 MHz reported `valid=1`, RSSI 46 and
  SNR 16 dB on the test board. Audio through the shield was confirmed by
  listening.
- The generic ESP32-S3 build compiles, but its wiring and serial controls have
  not been tested on a separate development board.
- The DAB firmware loads; a Band III scan found valid multiplexes on 8C and
  11B. The `services` command listed 13 audio services on 11B, and `play 1`
  started JAZZ RADIO. Audio from the shield amplifier was captured with a
  UMIK-1 microphone. Reception and service availability depend on location.
- HD Radio program selection is not yet exposed by this serial application.

This is a focused radio bring-up application. The Raspberry Pi Python backend
and web UI in the root of this repository are separate software.

The [CoreZero Digital Radio browser installer](https://apps.raspiaudio.com/#device=ZeroCoreS3&application=DigitalRadio)
offers a factory image for the ZeroCore S3. It replaces existing ESP32 firmware
and settings. Use the manual flash steps below when preserving another
application in `app0` matters.

## Wiring the 40-pin shield header

Header numbers below are **physical positions on the shield's Raspberry Pi
connector**, counted from its pin 1 marker. GPIO numbers are **ESP32-S3 GPIO
numbers**, not Raspberry Pi BCM numbers. The default GPIO assignments follow
the [ZeroCore S3 pinout](https://github.com/RASPIAUDIO/ZeroCore-S3/blob/main/docs/pinout.md).
The shield signals are listed in this repository's
[complete header pinout](../../README.md#raspberry-pi-header-pinout).

| Shield physical pin | Shield signal | Default ESP32-S3 GPIO | Build flag | Direction |
| ---: | --- | ---: | --- | --- |
| 19 | SPI MOSI | 11 | `RADIO_PIN_MOSI` | ESP32-S3 to shield |
| 21 | SPI MISO | 13 | `RADIO_PIN_MISO` | Shield to ESP32-S3 |
| 23 | SPI clock | 12 | `RADIO_PIN_SCK` | ESP32-S3 to shield |
| 24 | SPI CS0 / `SSBSI` | 10 | `RADIO_PIN_CS` | ESP32-S3 to shield, active low |
| 22 | SI4689 reset / `RST` | 39 | `RADIO_PIN_RESET` | ESP32-S3 to shield, active low |
| 11 | Amplifier enable / `ENABLE_AMPLI` | 1 | `RADIO_PIN_AMP` | ESP32-S3 to shield, active high |
| 2 **or** 4 | 5 V supply | 5 V supply, **not a GPIO** | — | Supply to shield |
| 6, 9, 14, 20, 25, 30, 34 **or** 39 | Ground | ESP32-S3 GND | — | Common ground |

The six signal wires, one 5 V connection and one common ground are the minimum
for this application. Use a suitable 5 V supply for the shield and amplifier.
Keep all GPIO at **3.3 V logic**; never connect the shield's 5 V header pin to
an ESP32-S3 GPIO. When powering a generic board and shield separately, connect
their grounds and check the power path before connecting two 5 V sources.

Shield pin **16** carries the SI4689 interrupt (GPIO41 on ZeroCore S3), but
this application polls the radio, so no interrupt wire is needed. Pins 12, 35
and 38 carry I2S audio, and pins 29, 31 and 33 carry navigation controls; this
application does not use them. Audio comes from the shield's analog jack or
speaker amplifier, so no I2S wiring is needed for playback.

The application enables analog audio on the SI4689. Its amplifier GPIO is
active high and starts off. On ZeroCore S3, USB-C provides power and the serial
connection. For a generic board, check that its power path can supply the
shield before using its USB connection as the sole power source.

### Changing GPIOs for a generic ESP32-S3

The defaults are in [`src/radio_pins.h`](src/radio_pins.h). They can be
overridden in `platformio.ini` without changing the source. For example, if
you wire the shield's amplifier enable (physical pin 11) to GPIO4 instead of
GPIO1, add these lines under `[env:generic_s3]`:

```ini
build_flags =
    ${env:zerocore_s3.build_flags}
    -DRADIO_PIN_AMP=4
```

The other five signals retain the defaults from the table. To move any of
them, add its build flag with the actual GPIO number. Choose GPIOs exposed and
free on your specific ESP32-S3 board; avoid pins reserved for onboard
flash/PSRAM, USB or boot strapping. The `pins` serial command prints the
mapping compiled into the firmware; it cannot discover the physical wiring.

## Build

Install [PlatformIO](https://platformio.org/) and run from this directory:

```sh
pio run -e zerocore_s3
```

Use `pio run -e zerocore_s3_web` for the hotspot web version, or
`pio run -e generic_s3_web` for the generic 8 MB ESP32-S3 starting point.
See [web controls](WEB.md) for connection instructions and features. All web
builds retain the USB serial commands below.

`embed_firmware.py` generates `src/firmware_images.cpp` from the four SI4689
images in `data/` before compilation. That generated file and `.pio/` are
ignored by Git. `zerocore_s3` targets the tested ZeroCore S3 partition layout
with 16 MB flash. For an 8 MB generic ESP32-S3 development board, use
`pio run -e generic_s3`; this environment uses PlatformIO's `default_8MB.csv`
partition table and does not require PSRAM. Adjust its flash size, partitions
and upload settings to match your actual board. On a generic board, PlatformIO
can flash the complete firmware with `pio run -e generic_s3 -t upload`; this
overwrites the board's current application and partition table.

## Flash while preserving the existing application

The tested board originally had `app0` at `0x10000` and `app1` at `0x7D0000`,
each `0x7C0000` bytes. The last 64 KB of `app0` were verified empty, then
reserved as `radio_cfg` at `0x7C0000`, reducing `app0` to `0x7B0000` bytes
without moving either application. Verify the board's partition table and
that this region is empty before using these addresses on another board. The
existing application remains in `app0`; flash the revised
partition table once, then the radio application in `app1`:

```sh
esptool --port COM8 --baud 921600 write_flash 0x8000 .pio/build/zerocore_s3_web/partitions.bin
esptool --port COM8 --baud 921600 write_flash 0x7D0000 .pio/build/zerocore_s3_web/firmware.bin
python <ESP_IDF_PATH>/components/app_update/otatool.py --port COM8 switch_ota_partition --slot 1
```

For the serial build, replace `zerocore_s3_web` in the firmware path with
`zerocore_s3`. Do not write either image at `0x7D0000` on a generic board
unless its actual partition table has the same `app1` address and size.

Replace `COM8` with the actual port. If the firmware does not start after
flashing, power cycle the ZeroCore S3. During early tests, the RTS reset from
`esptool` occasionally left the board in download mode; later flashes restarted
normally.

To select the preserved application again, use the same ESP-IDF tool with
`switch_ota_partition --slot 0`, then power cycle.

## Serial commands

Send one command per line at **115200 baud**. `set` is the primary command
for changing a setting. FM is loaded at power-up; select a mode before tuning
in the other mode.

| Command | Action |
| --- | --- |
| `help` | List commands |
| `wifi` (web build) | Show the current Wi-Fi mode, local address and network details |
| `wifi set <ssid> <password>` (web build) | Save local network credentials in device flash and connect |
| `wifi retry` (web build) | Retry the saved local network without entering its password again |
| `wifi reboot` (web build) | Restart the ESP32-S3 without cycling USB power |
| `wifi clear` (web build) | Erase saved local network credentials and start the hotspot |
| `pins` | Print the GPIO numbers compiled into this firmware and shield header destinations |
| `set mode fm` / `set mode dab` | Load FM or DAB firmware; resets the amplifier to off |
| `set fm 101.10` | Tune FM to 101.10 MHz (87.5–108.0 MHz) |
| `set dab 11B` | Tune a DAB Band III multiplex |
| `services` | List audio services on the tuned DAB multiplex |
| `play 1` | Start the listed DAB service numbered 1 |
| `set volume 40` | Set SI4689 analog volume (integer 0–63) |
| `set amp on` / `set amp off` | Enable or disable the shield's speaker amplifier |
| `status` | Show current FM or DAB tuner status |
| `scan` | Scan FM from 87.5 to 108 MHz |
| `mode fm`, `mode dab`, `f 101.10`, `d 8C`, `v 40`, `amp on`, `amp off`, `s` | Earlier short forms, still supported |

Example FM speaker session:

```text
pins
set fm 101.10
status
set volume 40
set amp on
```

Tune a valid local FM station; `101.10` was only the frequency used on the
test board. The jack uses the SI4689 analog output and does not need the
amplifier GPIO.

Example DAB speaker session after a scan identifies a valid local multiplex:

```text
set mode dab
set dab 11B
services
set volume 40
play 1
set amp on
```

Wait a few seconds after tuning before `services` so the service list can
arrive. The service numbers come from the most recent `services` response and
are cleared when switching modes or tuning another multiplex. `play` starts
the selected audio service; it does not turn on the speaker amplifier.
