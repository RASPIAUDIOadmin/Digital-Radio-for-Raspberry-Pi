# Raspberry Pi DAB control for Si468x (SPI, host-load)

Python helper to load the Si468x ROM00 patch and DAB firmware over SPI (no external flash), configure I2S output, tune a DAB ensemble, list services, and start audio.

## Wiring (uGreen DAB board → Raspberry Pi)

- 3V3: Pi pin 17  
- GND: Pi pins 6/9/25/39  
- RSTB: Pi pin 16 (BCM 23, configurable via `--rst-pin`)  
- INTB: Pi pin 22 (BCM 25, optional, script polls by default)  
- MOSI/MISO/SCLK/CE0: Pi pins 19/21/23/24  
- I2S: DCLK pin 12, DFS pin 35, DOUT pin 38 (SI468x drives audio)  

Enable SPI in `raspi-config`. For audio, keep the Si468x as I2S **master** (default in the script); feed DCLK/DFS/DOUT into your DAC or configure the Pi as an I2S slave input if you want ALSA capture.

## Dependencies

- `spidev`, `RPi.GPIO` (preinstalled on Raspberry Pi OS, otherwise `sudo apt install python3-spidev python3-rpi.gpio`)

## Running

```bash
cd /path/to/Si46xx_SDK_1_9_12
python3 raspi_dab/dab_radio.py \
  --freq 5A \
  --patch Si46xx_Firmware_Images/rom00_patch.016.bin \
  --firmware Si46xx_Firmware_Images/dab_radio_6_0_9.bin
```

What it does:
1) Resets/power-ups the Si468x ROM  
2) Host-loads `rom00_patch.016.bin` then `dab_radio_6_0_9.bin`  
3) Sets I2S to 48 kHz, 16-bit, Si468x as master, and applies the DAB front-end calibration used by the module example  
4) Tunes the requested DAB channel (default 5A), waits for lock, pulls the service list, prints it, and starts the first audio service (or `--list-only` to skip start)

Useful flags:
- `--freq 10C` or `--freq-index N` to pick a channel  
- `--service-id 0x1234` or `--service-index N` to pick which service to start  
- `--i2s-slave` if you want the Pi to drive BCLK/LRCLK instead  
- `--spi-speed` (default 1 MHz)  
- `--scan` or `--force-scan` to run a full Band III scan and save results to `full_scan.txt`

Full scan and station cache:
- Run a full scan explicitly:  
  ```bash
  python3 raspi_dab/dab_radio.py --scan --xtal 19200000 --ctun 0x07 --audio-out analog
  ```
- Results are cached in `full_scan.txt` (same folder as the script). On next run, the script loads this file and skips scanning.  
- If tuning fails or the file looks corrupted after a crash, simply delete `full_scan.txt` and rerun with `--scan` (or `--force-scan`) to rebuild it.

If you want to stream audio through the Pi, configure an I2S input overlay (e.g., `dtoverlay=i2s-slave`) and route DCLK/DFS/DOUT to that interface, then capture/play with `arecord`/`aplay` at 48 kHz, 16-bit.
