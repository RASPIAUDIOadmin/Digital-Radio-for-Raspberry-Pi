# Play Digital Radio through Audio+ or MIC+ I2S output

Use this setup when the Digital Radio Shield receives DAB/FM/HD Radio, but the
audio must come out of another Raspberry Pi I2S audio HAT such as Audio+ or
MIC+.

The audio path is:

```text
Digital Radio Shield SI4689 -> Raspberry Pi I2S capture -> Raspberry Pi I2S playback -> Audio+/MIC+
```

Do not enable two independent audio overlays at the same time. For example, do
not combine `dtoverlay=adau7002-simple` with an Audio+, HiFiBerry, or MIC+
overlay. They all try to own the Raspberry Pi sound card. Use the combined
profile below instead.

## Install the combined profile

Install the required tools and compile the overlay:

```bash
sudo apt install -y device-tree-compiler alsa-utils
cd ~/Digital-Radio-for-Raspberry-Pi/eeprom
dtc -@ -I dts -O dtb -o /tmp/raspiaudio-digital-radio-i2s-output.dtbo raspiaudio-digital-radio-i2s-output-overlay.dts
sudo cp /tmp/raspiaudio-digital-radio-i2s-output.dtbo /boot/firmware/overlays/
```

Edit `/boot/firmware/config.txt` and keep one audio overlay only:

```ini
dtparam=spi=on
dtparam=i2s=on
dtoverlay=raspiaudio-digital-radio-i2s-output
```

If the Raspberry Pi already has an old MIC+ / XVF profile installed, disable it
for this test so it does not create another ALSA card on the same I2S pins:

```bash
systemctl --user disable --now pi-ai-mic-vocalfusion-pipewire-16k.service 2>/dev/null || true
systemctl --user disable --now pi-ai-mic-vocalfusion-pipewire-48k.service 2>/dev/null || true
sudo systemctl disable --now pi-ai-mic-rpi-48k-doa-spi-boot.service 2>/dev/null || true
```

Reboot:

```bash
sudo reboot
```

## Check ALSA

After reboot, check the combined ALSA card:

```bash
aplay -l
arecord -l
```

Expected result: one card named `radio_i2s_output`, with a capture device and a
playback device. On the tested Raspberry Pi 5 setup, ALSA exposed:

```text
capture:  hw:CARD=radioi2soutput,DEV=0
playback: hw:CARD=radioi2soutput,DEV=1
```

## Start the radio server

Start the radio server with I2S output enabled:

```bash
cd ~/Digital-Radio-for-Raspberry-Pi
sudo python3 radio.py serve --port 8686 --audio-out i2s
```

Open the web UI and select a station.

## Route I2S to the audio HAT

In another terminal, route the live radio I2S capture to the I2S playback
device:

```bash
cd ~/Digital-Radio-for-Raspberry-Pi
python3 tools/i2s_route.py --gain 0.5
```

Use `--gain` as the I2S software volume. The SI4689 analog volume does not
change the raw I2S level.

If the output saturates, use a lower gain:

```bash
python3 tools/i2s_route.py --gain 0.1
```

If ALSA shows different device numbers, pass them explicitly:

```bash
python3 tools/i2s_route.py \
  --capture hw:CARD=radioi2soutput,DEV=0 \
  --playback hw:CARD=radioi2soutput,DEV=1 \
  --gain 0.5
```

## Stop the route

Press `Ctrl+C` in the route terminal.

If the route was started in the background:

```bash
pkill -f "tools/i2s_route.py"
```

## Troubleshooting

If there is no sound, first confirm that the radio server is tuned to a station
and that the I2S capture contains real samples:

```bash
timeout 5 arecord -D hw:CARD=radioi2soutput,DEV=0 -f S16_LE -r 48000 -c 2 /tmp/radio-test.wav
ls -lh /tmp/radio-test.wav
```

If the Audio+ or MIC+ card is not visible, check that `/boot/firmware/config.txt`
does not contain another audio overlay that conflicts with
`raspiaudio-digital-radio-i2s-output`.
