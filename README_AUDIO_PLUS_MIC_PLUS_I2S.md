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
before rebooting. The important rule is to keep only one active I2S audio
profile for these pins.

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
device with the native ALSA loopback tool:

```bash
cd ~/Digital-Radio-for-Raspberry-Pi
alsaloop \
  -C hw:CARD=radioi2soutput,DEV=0 \
  -P hw:CARD=radioi2soutput,DEV=1 \
  -f S16_LE \
  -r 48000 \
  -c 2
```

The SI4689 analog volume does not change the raw I2S level. The direct
`alsaloop` command above routes full-scale PCM.

If the output level is too high and the playback HAT does not expose an ALSA
mixer control, use an ALSA `route` PCM for native software attenuation. This
example creates a 10% playback route:

```bash
cat > ~/radio-i2s-asound.conf <<'EOF'
</usr/share/alsa/alsa.conf>

pcm.radio_i2s_10pct {
    type route
    slave.pcm "hw:CARD=radioi2soutput,DEV=1"
    slave.channels 2
    ttable.0.0 0.1
    ttable.1.1 0.1
}
EOF

ALSA_CONFIG_PATH=~/radio-i2s-asound.conf alsaloop \
  -C hw:CARD=radioi2soutput,DEV=0 \
  -P radio_i2s_10pct \
  -f S16_LE \
  -r 48000 \
  -c 2
```

Change `ttable.0.0` and `ttable.1.1` to another value if you need a different
level, for example `0.5` for 50%.

If your playback HAT exposes a hardware mixer control, you can use that instead
with `amixer scontrols` and `amixer sset`.

If ALSA shows different device numbers, change `-C` to the capture device shown
by `arecord -l`, and `-P` to the playback device shown by `aplay -l`.

## Stop the route

Press `Ctrl+C` in the route terminal.

If the route was started in the background:

```bash
pkill -f "alsaloop .*radioi2soutput"
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
