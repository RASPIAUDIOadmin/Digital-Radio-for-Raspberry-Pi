# Raspiaudio Digital Radio for Raspberry Pi

![Raspiaudio Web UI](pic/webui.png)

This project turns a Raspberry Pi and a SI4689-based shield into a fully local digital radio.

The Raspiaudio Digital Radio Shield for Raspberry Pi is a compact all-in-one radio board that brings AM, FM, DAB, DAB+, and HD Radio support to Raspberry Pi boards with a 40-pin header. It combines a local Web UI, CLI control, analog audio output, I2S digital audio, a built-in 1 x 5 W amplifier, a switchable onboard speaker output, and an onboard 3-way navigation control with up, down, and push actions for standalone menu-based projects.

*HD Radio is subject to licensing. Please verify that you are legally allowed to use it in your country and for your intended application.*

<p align="center">
  <img src="pic/Digital%20radio%20Pi%20Raspiaudio%20front.png" alt="Raspiaudio digital radio front" width="46%" />
  <img src="pic/Digital%20radio%20Pi%20Raspiaudio%20back.png" alt="Raspiaudio digital radio back" width="46%" />
</p>

<p align="center">
  <img src="pic/Digital%20radio%20Pi%20Raspiaudio%20situation.png" alt="Raspiaudio digital radio in use" width="46%" />
  <img src="pic/Digital%20radio%20Pi%20Raspiaudio%20situation%20%282%29.png" alt="Raspiaudio digital radio setup view" width="46%" />
</p>

The focus is simple:
- no internet required to listen to radio
- resilient local control
- browser-based Web UI for daily use
- CLI access for automation, scripting, and custom applications

The whole project is open source:
- GitHub: [RASPIAUDIOadmin/DAB_RADIO](https://github.com/RASPIAUDIOadmin/DAB_RADIO)

## Main features

- DAB / DAB+
- FM
- HD Radio
- AM
- AM HD
- local Web UI to scan, browse, tune, change volume, manage favorites, and handle recordings
- CLI to control the backend from the terminal or integrate the radio into your own software
- analog audio output on the shield
- I2S audio path for digital capture and recording
- built-in `1 x 5 W` amplifier
- switchable onboard speaker output
- onboard 3-way navigation button: up, down, and push
- audio jack output
- screwless passive speaker output for an external speaker
  - `4 ohm` recommended
  - `8 ohm` supported
- SMA antenna connector for the included antenna or a different external antenna
- AM antenna balun for impedance matching
- AM loop antenna connection support
- amplifier enable on `GPIO17`
- local recordings list in the browser

## Why this project

Unlike an internet radio product, this setup does not depend on network streaming to play stations.

That makes it useful when you want:
- a radio that still works without internet access
- direct access to terrestrial broadcast bands
- a local control API you can reuse in your own program
- a Raspberry Pi based platform that is easy to extend

## Web UI first

The recommended workflow is the local Web UI.

It gives you:
- source mode selection
- station scan
- station selection
- favorites
- amplifier on / off
- volume control
- recording controls
- recordings browser
- a simple radio workflow directly from a browser on the local network

Start the server on the Raspberry Pi:

```bash
python radio.py serve --host 0.0.0.0 --port 8686
```

Then open:

```text
http://<raspberry-pi-ip>:8686/
```

## CLI mode

The CLI uses the same backend as the Web UI.

That means you can control the radio manually from the terminal, or use the commands as a base for your own scripts and applications.

Examples:

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

## Repository layout

- `radio.py`
  entry point for the CLI
- `raspiaudio_radio/`
  shared backend, HTTP server, and Web UI
- `firmwares/`
  firmware and patch files used by the radio backend
- `legacy/`
  older low-level scripts kept for reference

## Current backend scope

The current radio backend is intentionally kept simple:
- SPI control
- local firmware host-load
- Web UI + CLI on the same backend
- station playback
- favorites
- local browser control
- I2S recording workflow

## Hardware notes

Current shield-oriented defaults:
- `RSTB = BCM25`
- `AMP_EN = BCM17`
- `SPI bus/device = 0/0`
- local firmware files loaded from `firmwares/`
- onboard navigation input with `up / down / push`
- onboard speaker output with dedicated on / off control
- SMA antenna connector
- AM impedance-matching balun
- passive speaker connector

## Dependencies

On Raspberry Pi OS:

```bash
sudo apt install python3-spidev python3-rpi.gpio python3-smbus2
```

## Open source and reusable

This repository is not only a radio player.

It is also a base to:
- build your own radio application
- integrate the SI4689 shield into a custom Raspberry Pi project
- create your own UI on top of the CLI or HTTP backend
- experiment with local digital radio features without depending on cloud services

## Inputs / Outputs

- Power input:
  - `5V` on Raspberry Pi header pins `2` and `4`
  - `GND` on pins `6, 9, 14, 20, 25, 30, 34, 39`
- SPI radio interface:
  - `MOSI` on `GPIO10` / pin `19`
  - `MISO` on `GPIO9` / pin `21`
  - `SPICLK` on `GPIO11` / pin `23`
  - `SSBSI` on `GPIO8` / pin `24`
- Radio control:
  - `INT` on `GPIO23` / pin `16`
  - `SI4689 RST` on `GPIO25` / pin `22`
  - `ENABLE_AMPLI` on `GPIO17` / pin `11`
- I2S digital audio:
  - `I2S BCK` on `GPIO18` / pin `12`
  - `I2S LRCK` on `GPIO19` / pin `35`
  - `I2S DOUT` on `GPIO20` / pin `38`
- Navigation button:
  - `Switch CW` on `GPIO5` / pin `29`
  - `Switch PUSH` on `GPIO6` / pin `31`
  - `Switch CCW` on `GPIO13` / pin `33`
- RF and audio connections on the shield:
  - `SMA` antenna connector
  - AM loop antenna input
  - AM impedance-matching balun
  - audio jack output
  - onboard speaker output with on / off switch
  - passive external speaker output via screwless connector
