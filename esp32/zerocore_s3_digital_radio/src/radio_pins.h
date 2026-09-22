#pragma once

// Defaults follow the ZeroCore S3 40-pin header. Override any value with a
// -DRADIO_PIN_*=<GPIO> build flag when wiring a different ESP32-S3 board.
// These are ESP32 GPIO numbers, never physical header or Raspberry Pi BCM numbers.
#ifndef RADIO_PIN_AMP
#define RADIO_PIN_AMP 1    // Shield header pin 11; active high
#endif
#ifndef RADIO_PIN_MOSI
#define RADIO_PIN_MOSI 11  // Shield header pin 19
#endif
#ifndef RADIO_PIN_MISO
#define RADIO_PIN_MISO 13  // Shield header pin 21
#endif
#ifndef RADIO_PIN_RESET
#define RADIO_PIN_RESET 39 // Shield header pin 22; active low
#endif
#ifndef RADIO_PIN_SCK
#define RADIO_PIN_SCK 12   // Shield header pin 23
#endif
#ifndef RADIO_PIN_CS
#define RADIO_PIN_CS 10    // Shield header pin 24; active low
#endif
