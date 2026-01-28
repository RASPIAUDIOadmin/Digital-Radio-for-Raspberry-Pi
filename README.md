# Raspberry Pi control for Si468x (SPI/I2C host-load + optional flash boot)

This project is a **local radio receiver** controller for the Silicon Labs/Si46xx family.  
It does **not** use the internet. It talks directly to the chip over **SPI or I2C**, loads the ROM patch + firmware, and plays broadcast radio.

Supported broadcast standards depend on the **firmware image** you load:
- **DAB/DAB+** (used by this script by default)
- **FM**
- **AM**
- **HD Radio** (different firmware required)

The script can **host-load** the firmware (RAM) or **boot from external SPI flash** if your module supports it.

---

## Quick start (simple mode, no arguments)

From the folder that contains `dab_radio.py`:

```bash
python dab_radio.py
```

Default behavior:
- **SPI control** is used by default.
- Loads `rom00_patch.016.bin` then `dab_radio_6_0_9.bin` from the **same folder**.
- Tunes DAB channel **5A** (index 0), reads the service list, and starts audio.
- Uses **analog audio** by default (I2S optional).

If you want I2C instead:
```bash
python dab_radio.py --i2c
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

Enable SPI in `raspi-config`.

### Control interface (I2C)
Connect I2C only if your board is configured for I2C control (SMODE = I2C).

- **SDA**: Pi pin 3 (GPIO2)
- **SCL**: Pi pin 5 (GPIO3)
- Default address: **0x64** (7-bit)

The script enables internal pull-ups on GPIO2/3, but **external pull-ups are still recommended**.

### External SPI flash (NVSPI)
This is **not** the same bus as the control SPI.

The flash connects to **NVSCLK / NVMOSI / NVMISO / SSBNV** on the Si468x.
Do **not** connect the Pi as another SPI master on those lines unless you isolate the bus.

---

## Audio output

### Analog (default)
Uses the Si468x internal DAC.

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

### DAB/DAB+ (host-load)
```bash
python dab_radio.py --freq 10C
```

### Full DAB scan
```bash
python dab_radio.py --scan
```

### List services only
```bash
python dab_radio.py --list-only
```

### FM tune
```bash
python dab_radio.py --fm-freq 99.5
```

### FM scan
```bash
python dab_radio.py --fm-scan
```

---

## Flash programming + boot

### 1) Program flash (via Si468x)
```bash
python dab_radio.py \
  --flash-program --flash-program-only \
  --flash-program-image dab_radio_6_0_9.bin \
  --flash-program-patch rom00_patch_mini.003.bin \
  --flash-addr 0x00092000
```

### 2) Boot from flash
Use the **full patch** for boot:
```bash
python dab_radio.py \
  --flash-boot \
  --flash-addr 0x00092000 \
  --patch rom00_patch.016.bin
```

### 3) Program + boot in one run
```bash
python dab_radio.py \
  --flash-program \
  --flash-program-image dab_radio_6_0_9.bin \
  --flash-program-patch rom00_patch_mini.003.bin \
  --flash-addr 0x00092000 \
  --flash-boot
```

---

## Known issues / limitations

1) **Flash boot fails with status=0xC0**  
   - Usually means the firmware in flash is **not flash-bootable** or the patch is incompatible.  
   - Many Si468x images are **host-load only**.  

2) **Flash programming appears to work, but boot still fails**  
   - The device can accept the write commands, yet the image is not valid for flash boot.  

3) **Two SPI masters on the same flash**  
   - If the Pi and Si468x both drive the same flash bus, this will break.  
   - Only one master is allowed (or use a mux/isolator).

4) **I2C detection issues**  
   - Requires correct SMODE pin configuration.  
   - Weak internal pull-ups are not enough for reliable operation.

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

## Troubleshooting helpers

Optional helper scripts in this folder:
- `flash_check.py` – checks whether the flash is accessible  
- `flash_boot_test.py` / `flash_boot_final.py` – tests boot sequences  

---

## Dependencies

On Raspberry Pi OS:
```bash
sudo apt install python3-spidev python3-rpi.gpio python3-smbus2
```

---

## Summary

This project lets you listen to **broadcast radio without internet** on a Raspberry Pi.  
Use **SPI host-load** for reliable DAB+ operation, and only attempt **flash boot** if you have the correct flash-bootable firmware and patch.
