# Raspiaudio Digital Radio for Raspberry Pi

![Raspiaudio Web UI](pic/webui.png)

This project turns a Raspberry Pi and a SI4689-based shield into a fully local digital radio.

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
