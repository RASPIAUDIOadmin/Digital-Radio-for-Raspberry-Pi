#pragma once

#include <Arduino.h>
#include <stdint.h>

struct DigiStatus {
  bool ficError = false;
  bool acq = false;
  bool valid = false;
  int8_t rssi = 0;
  uint8_t snr = 0;
  uint8_t ficQuality = 0;
  uint8_t cnr = 0;
  uint32_t tuneFreqHz = 0;
  uint8_t tuneIndex = 0;
};

struct DabService {
  uint32_t serviceId = 0;
  uint32_t componentId = 0;
  String label;
  uint8_t charset = 0;
  uint8_t freqIndex = 0;
  uint32_t freqKHz = 0;
};
