#!/usr/bin/env bash
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
BUILD_DIR="${BUILD_DIR:-"$SCRIPT_DIR/build"}"
DTC="${DTC:-dtc}"
EEPMAKE="${EEPMAKE:-eepmake}"

mkdir -p "$BUILD_DIR"

"$DTC" -@ -H epapr -O dtb \
  -o "$BUILD_DIR/raspiaudio-digital-radio.dtbo" \
  "$SCRIPT_DIR/raspiaudio-digital-radio-overlay.dts"

"$EEPMAKE" \
  "$SCRIPT_DIR/raspiaudio-digital-radio-hatplus.txt" \
  "$BUILD_DIR/raspiaudio-digital-radio-hatplus.eep"

"$EEPMAKE" -v1 \
  "$SCRIPT_DIR/raspiaudio-digital-radio-v1.txt" \
  "$BUILD_DIR/raspiaudio-digital-radio-v1.eep" \
  "$BUILD_DIR/raspiaudio-digital-radio.dtbo"

printf '%s\n' "Generated EEPROM files in $BUILD_DIR"
