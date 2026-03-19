# Raspberry Pi control for Si468x (SPI/I2C host-load + optional flash boot)

This project is a **local radio receiver** controller for the Silicon Labs Si468x (Si4689) family.
It does **not** use the internet. It talks directly to the chip over **SPI or I2C**, loads the ROM patch + firmware, and plays broadcast radio.

Supported broadcast standards depend on the **firmware image** you load:
- **DAB/DAB+** (used by this script by default)
- **FM**
- **AM**
- **HD Radio** (different firmware required)

The script can **host-load** the firmware (RAM) or **boot from external SPI flash** if your module supports it.

## Scripts

| File | Description |
|------|-------------|
| `legacy/dab_radio.py` | Original script (SPI only) |
| `legacy/dab_radio_i2c_fixed.py` | Legacy robust I2C + SPI helper |
| `legacy/dab_radio_i2c_safe2.py` | Low-level Si468x helper reused by the current backend |
| `radio.py` | Easy CLI for the local backend |
| `raspiaudio_radio/` | Shared SPI backend + browser UI |

Firmware images now live in `firmwares/`.
Legacy standalone Python scripts now live in `legacy/`.

The new `radio.py` path matches the current shield bring-up:
- **SPI only**
- **Host-load only**
- **DAB / FM / HD Radio / AM / AM HD**
- **Analog + I2S audio out**
- **Amplifier enable on GPIO17**
- **Favorites + browser UI + local recording list**

---

## Quick start

### New local CLI + web UI (recommended for daily use)

Start the backend on the Raspberry Pi:

```bash
python radio.py serve --host 0.0.0.0 --port 8686
```

Then from another shell on the Pi:

```bash
python radio.py boot --mode dab
python radio.py scan --mode fm
python radio.py stations --mode fm
python radio.py play 0
python radio.py volume +2
python radio.py amp off
python radio.py record start
python radio.py recordings
```

Open the browser UI on:

```text
http://<raspberry-pi-ip>:8686/
```

### SPI mode (default)

```bash
python legacy/dab_radio_i2c_fixed.py
```

Default behavior:
- **SPI control** is used by default.
- Loads `firmwares/rom00_patch.016.bin` then `firmwares/dab_radio_6_0_9.bin`.
- Tunes DAB channel **5A** (index 0), reads the service list, and starts audio.
- Uses **analog audio** by default (I2S optional).

### I2C mode

```bash
python legacy/dab_radio_i2c_fixed.py --i2c --i2c-bus 1 --i2c-addr 0x64
```

Before running, verify the chip is visible:
```bash
sudo i2cdetect -y 1
# Should show 64 at address 0x64
```

---

## How it works (modes)

### 1) Host-load (RAM) – default
The patch + firmware are sent over SPI/I2C at boot and run from RAM.

Pros: Reliable, always works  
Cons: Slower boot (~seconds)

### 2) Flash boot (NVSPI)
The firmware is stored in external SPI flash and loaded by the chip on boot.

Pros: Fast boot  
Cons: Requires correct flash image + patch + hardware support

---

## Wiring / Pinout (Raspberry Pi)

### Control interface (SPI host, default)
Connect the Pi to the **Si468x control SPI** (SSBSI/SDIO/SCLK).

- 3V3: Pi pin 17
- GND: Pi pin 6/9/25/39
- **RSTB**: Pi pin 22 (BCM 25, configurable with `--rst-pin`)
- **INTB**: optional, configurable with `--int-pin` (leave unset to poll)
- **SPI MOSI**: Pi pin 19
- **SPI MISO**: Pi pin 21
- **SPI SCLK**: Pi pin 23
- **SPI CE0**: Pi pin 24 (SSBSI)
- **AMP ENABLE**: Pi pin 11 (BCM 17) — set HIGH to enable the onboard amplifier

Enable SPI in `raspi-config`.

For the new `radio.py` backend the expected defaults are:
- `RSTB = BCM25`
- `AMP_EN = BCM17`
- `SPI bus/device = 0/0`
- `Host-load firmware from local files`

### Control interface (I2C)
Connect I2C only if your board is configured for I2C control (SMODE = I2C).

- **SDA**: Pi pin 3 (GPIO2)
- **SCL**: Pi pin 5 (GPIO3)
- Default address: **0x64** (7-bit)

**External pull-ups (4.7kΩ to 3.3V) are recommended** on SDA and SCL.

> **WARNING**: Do NOT use `GPIO.setup()` on GPIO 2 or 3 (I2C SDA/SCL). These pins are managed by the Linux I2C kernel driver. Any RPi.GPIO operation on them (setup, cleanup, bit-banging) will corrupt the I2C bus and make the chip disappear from `i2cdetect` until reboot. The legacy helper under `legacy/dab_radio_i2c_fixed.py` handles this correctly.

### External SPI flash (NVSPI)
This is **not** the same bus as the control SPI.

The flash connects to **NVSCLK / NVMOSI / NVMISO / SSBNV** on the Si468x.
Do **not** connect the Pi as another SPI master on those lines unless you isolate the bus.

---

## Audio output

### Analog + I2S (recommended for the new web UI)
The local backend now defaults to `both`, so the shield keeps analog audio while also exposing I2S for recording/capture.

```
--audio-out both
```

### Analog only
Uses the Si468x internal DAC only.

```
--audio-out analog
```

### I2S
Si468x drives I2S in master mode by default.

```
--audio-out i2s
```

I2S pins (Si468x → Pi/DAC):
- DCLK (BCLK)
- DFS (LRCLK)
- DOUT

If you want the Pi to be I2S master:
```
--i2s-slave
```

---

## Common commands

All examples below use `legacy/dab_radio_i2c_fixed.py`. Add `--i2c --i2c-bus 1 --i2c-addr 0x64` for I2C mode.

### DAB/DAB+ (host-load)
```bash
python legacy/dab_radio_i2c_fixed.py --freq 10C
```

### Full DAB scan
```bash
python legacy/dab_radio_i2c_fixed.py --scan
```

### List services only
```bash
python legacy/dab_radio_i2c_fixed.py --list-only
```

### FM tune
```bash
python legacy/dab_radio_i2c_fixed.py --fm-freq 99.5
```

### FM scan
```bash
python legacy/dab_radio_i2c_fixed.py --fm-scan
```

### I2C example (DAB on channel 5A)
```bash
python legacy/dab_radio_i2c_fixed.py --i2c --i2c-bus 1 --i2c-addr 0x64 --freq 5A
```

---

## Flash programming + boot

Flash programming uses `CMD_FLASH_LOAD` (0x05) with the magic header bytes `0xFE 0xC0 0xDE`. The **mini patch** (`rom00_patch_mini.003.bin`) must be used during programming because the full patch blocks flash access.

### 1) Program flash (via Si468x)
```bash
python legacy/dab_radio_i2c_fixed.py \
  --flash-program --flash-program-only \
  --flash-program-image dab_radio_6_0_9.bin \
  --flash-program-patch rom00_patch_mini.003.bin \
  --flash-addr 0x00092000
```

### 2) Boot from flash
Use the **full patch** for boot:
```bash
python legacy/dab_radio_i2c_fixed.py \
  --flash-boot \
  --flash-addr 0x00092000 \
  --patch firmwares/rom00_patch.016.bin
```

### 3) Program + boot in one run
```bash
python legacy/dab_radio_i2c_fixed.py \
  --flash-program \
  --flash-program-image dab_radio_6_0_9.bin \
  --flash-program-patch rom00_patch_mini.003.bin \
  --flash-addr 0x00092000 \
  --flash-boot
```

> **Current status**: Flash programming completes successfully (499,516 bytes written), but **flash boot fails with status=0xC0**. The firmware `dab_radio_6_0_9.bin` is **HOST-LOAD only** and cannot boot from flash. A flash-bootable firmware image from Skyworks/Silicon Labs would be needed for flash boot to work. **Host-load mode works reliably** on both SPI and I2C (71 DAB services detected in testing).

---

## Known issues / limitations

1) **Flash boot fails with status=0xC0**
   - The firmware `dab_radio_6_0_9.bin` is **HOST-LOAD only** and cannot boot from flash.
   - Flash programming completes successfully (bytes are written), but the chip rejects the image at boot.
   - A flash-bootable firmware from Skyworks would be required.
   - **Workaround**: Use host-load mode, which works reliably on both SPI and I2C.

2) **ROM patch types matter**
   - `rom00_patch.016.bin` (full patch): Required for boot, but **blocks SPI flash access** — cannot be used for flash programming.
   - `rom00_patch_mini.003.bin` (mini patch): Allows flash access — must be used for flash programming.

3) **Two SPI masters on the same flash**
   - If the Pi and Si468x both drive the same flash bus, this will break.
   - Only one master is allowed (or use a mux/isolator).

4) **I2C bus corruption from RPi.GPIO**
   - `GPIO.setup()` on GPIO 2/3 (I2C SDA/SCL) corrupts the bus — chip disappears from `i2cdetect` until reboot.
   - `GPIO.cleanup()` without args resets ALL pins including I2C — must only cleanup specific pins (e.g. the reset pin).
   - Bit-banging GPIO 2/3 for "bus recovery" destroys the kernel I2C driver state.
   - All three issues are fixed in `legacy/dab_radio_i2c_fixed.py`.

5) **I2C NACKs during POWER_UP**
   - After the POWER_UP command, the Si468x goes offline while the crystal oscillator starts.
   - All I2C transactions return NACK (errno 110) for several hundred milliseconds.
   - `legacy/dab_radio_i2c_fixed.py` handles this with NACK-tolerant CTS polling and extended timeouts.

---

## FM / AM / HD Radio firmware

The Si468x family can support **FM, AM, DAB/DAB+ and HD Radio**, but **each mode needs a specific firmware image**.

Examples from the SDK:
- `dab_radio_6_0_9.bin` → DAB/DAB+  
- `fmhd_radio_5_0_4.bin` → FM + HD  
- `amhd_radio_2_0_11.bin` → AM + HD  

This script currently exposes **DAB+ and FM** workflows.  
AM/HD require different firmware and additional command support.

---

## Troubleshooting

### I2C chip not detected after script crash
If the chip disappears from `i2cdetect -y 1` after a failed run:
```bash
sudo reboot
```
This is caused by GPIO 2/3 corruption. Use `legacy/dab_radio_i2c_fixed.py` which avoids this issue.

### Manual reset via pinctrl
```bash
pinctrl set 25 op dl   # pull RST low
pinctrl set 25 op dh   # release RST high
sleep 1
sudo i2cdetect -y 1    # verify chip at 0x64
```

### Helper scripts
- `flash_check.py` – checks whether the flash is accessible
- `flash_boot_test.py` / `flash_boot_final.py` – tests boot sequences
- `read_flash.py` – reads back flash content for verification  

---

## Dependencies

On Raspberry Pi OS:
```bash
sudo apt install python3-spidev python3-rpi.gpio python3-smbus2
```

---

## I2C implementation details

The I2C protocol for Si468x follows the reference C code from the SDK (`si468x_bus.c`):

1. **Command write**: Single I2C write transaction with the command bytes
2. **Read reply**: Send `RD_REPLY` (0x00) as a write, then a separate read transaction (500µs gap between)
3. **CTS polling**: Read status byte repeatedly until bit 7 (CTS) is set, with 1ms interval

Key timing in `legacy/dab_radio_i2c_fixed.py`:
- Reset pulse: 100ms low, then 1s wait for I2C slave to come up
- POWER_UP: Send command, wait 500ms for crystal, then poll CTS with 5s timeout
- Inter-transaction gap: 500µs between write and read

---

## Summary

This project lets you listen to **broadcast radio without internet** on a Raspberry Pi.
Use **`legacy/dab_radio_i2c_fixed.py`** for SPI/I2C experiments.
Host-load mode is reliable and tested (71 DAB services). Flash boot requires a flash-bootable firmware image from Skyworks/Silicon Labs.
