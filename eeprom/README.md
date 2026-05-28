# Raspiaudio Digital Radio EEPROM profile

This folder contains draft Raspberry Pi HAT EEPROM profiles for automatic boot-time configuration.

The target overlay is `raspiaudio-digital-radio`. It enables:

- SPI0 CE0 for SI4689 control
- Raspberry Pi I2S clock producer mode
- an ALSA capture card named `si4689_i2s` for recording and local streaming

## Recommended development path

For immediate plug-and-play testing, use the legacy HAT v1 profile because it embeds the compiled `.dtbo` into the EEPROM image:

```bash
sudo apt install git cmake device-tree-compiler i2c-tools
git clone https://github.com/raspberrypi/utils.git
cd utils/eeptools
cmake .
make
```

Build the overlay and EEPROM images:

```bash
cd /path/to/Digital-Radio-for-Raspberry-Pi/eeprom
EEPMAKE=/path/to/utils/eeptools/eepmake bash ./build-eeprom.sh
```

Flash it to a standard 24C32 HAT EEPROM at address `0x50`:

```bash
sudo dtoverlay i2c-gpio i2c_gpio_sda=0 i2c_gpio_scl=1 bus=9
i2cdetect -y 9
sudo /path/to/utils/eeptools/eepflash.sh -w -t=24c32 -a=50 -f=build/raspiaudio-digital-radio-v1.eep
```

Use the HAT+ profile when the overlay is installed as `/boot/firmware/overlays/raspiaudio-digital-radio.dtbo` or accepted upstream into Raspberry Pi OS:

```bash
sudo cp build/raspiaudio-digital-radio.dtbo /boot/firmware/overlays/
sudo /path/to/utils/eeptools/eepflash.sh -w -t=24c32 -a=50 -f=build/raspiaudio-digital-radio-hatplus.eep
```

## Production data to confirm

- final `product_id`
- final `product_ver`
- exact product/vendor strings
- whether the board back-powers the Raspberry Pi
- EEPROM address/type and write-protect control
- whether to ship legacy embedded overlay, HAT+ overlay-name profile, or both
