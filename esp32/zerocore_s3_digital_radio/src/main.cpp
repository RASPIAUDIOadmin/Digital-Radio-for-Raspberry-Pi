#include <Arduino.h>
#include <SPI.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include "firmware_images.h"
#include "radio_pins.h"

constexpr uint8_t PIN_AMP = RADIO_PIN_AMP;
constexpr uint8_t PIN_MOSI = RADIO_PIN_MOSI;
constexpr uint8_t PIN_MISO = RADIO_PIN_MISO;
constexpr uint8_t PIN_RESET = RADIO_PIN_RESET;
constexpr uint8_t PIN_SCK = RADIO_PIN_SCK;
constexpr uint8_t PIN_CS = RADIO_PIN_CS;

constexpr uint32_t XTAL_HZ = 19200000;
constexpr uint8_t CMD_POWER_UP = 0x01;
constexpr uint8_t CMD_HOST_LOAD = 0x04;
constexpr uint8_t CMD_LOAD_INIT = 0x06;
constexpr uint8_t CMD_BOOT = 0x07;
constexpr uint8_t CMD_SET_PROPERTY = 0x13;
constexpr uint8_t CMD_GET_DIGITAL_SERVICE_LIST = 0x80;
constexpr uint8_t CMD_START_DIGITAL_SERVICE = 0x81;
constexpr uint8_t CMD_STOP_DIGITAL_SERVICE = 0x82;
constexpr uint8_t CMD_FM_TUNE_FREQ = 0x30;
constexpr uint8_t CMD_FM_RSQ_STATUS = 0x32;
constexpr uint8_t CMD_DAB_TUNE_FREQ = 0xB0;
constexpr uint8_t CMD_DAB_DIGRAD_STATUS = 0xB2;
constexpr uint8_t CMD_DAB_GET_EVENT_STATUS = 0xB3;
constexpr uint8_t CMD_DAB_SET_FREQ_LIST = 0xB8;

constexpr uint16_t PROP_PIN_CONFIG_ENABLE = 0x0800;
constexpr uint16_t PROP_AUDIO_ANALOG_VOLUME = 0x0300;
constexpr uint16_t PROP_AUDIO_MUTE = 0x0301;
constexpr uint16_t PROP_FM_TUNE_FE_VARM = 0x1710;
constexpr uint16_t PROP_FM_TUNE_FE_VARB = 0x1711;
constexpr uint16_t PROP_FM_TUNE_FE_CFG = 0x1712;

struct DabChannel { const char *name; uint32_t khz; };
struct DabService { uint32_t serviceId; uint16_t componentId; String label; };
constexpr DabChannel DAB_CHANNELS[] = {
    {"5A",174928}, {"5B",176640}, {"5C",178352}, {"5D",180064},
    {"6A",181936}, {"6B",183648}, {"6C",185360}, {"6D",187072},
    {"7A",188928}, {"7B",190640}, {"7C",192352}, {"7D",194064},
    {"8A",195936}, {"8B",197648}, {"8C",199360}, {"8D",201072},
    {"9A",202928}, {"9B",204640}, {"9C",206352}, {"9D",208064},
    {"10A",209936}, {"10B",211648}, {"10C",213360}, {"10D",215072},
    {"10N",210096}, {"11A",216928}, {"11B",218640}, {"11C",220352},
    {"11D",222064}, {"11N",217088}, {"12A",223936}, {"12B",225648},
    {"12C",227360}, {"12D",229072}, {"12N",224096}, {"13A",230784},
    {"13B",232496}, {"13C",234208}, {"13D",235776}, {"13E",237488},
    {"13F",239200},
};
constexpr size_t DAB_CHANNEL_COUNT = sizeof(DAB_CHANNELS) / sizeof(DAB_CHANNELS[0]);

enum class Mode { FM, DAB };
Mode currentMode = Mode::FM;
bool radioReady = false;
bool ampEnabled = false;
uint8_t volume = 40;
String commandLine;
std::vector<DabService> dabServices;
int selectedService = -1;
SPISettings commandSpi(2000000, MSBFIRST, SPI_MODE0);
SPISettings loadSpi(4000000, MSBFIRST, SPI_MODE0);

void selectRadio(bool fast = false) {
  SPI.beginTransaction(fast ? loadSpi : commandSpi);
  digitalWrite(PIN_CS, LOW);
}

void deselectRadio() {
  digitalWrite(PIN_CS, HIGH);
  SPI.endTransaction();
}

uint8_t readStatus() {
  selectRadio();
  SPI.transfer(0);
  uint8_t status = SPI.transfer(0);
  deselectRadio();
  return status;
}

bool waitCts(uint32_t timeoutMs = 1000, bool checkError = true) {
  const uint32_t start = millis();
  uint8_t status = 0;
  do {
    status = readStatus();
    if (status & 0x80) {
      if (checkError && (status & 0x40)) {
        Serial.printf("SI4689 ERR_CMD status=0x%02X\n", status);
        return false;
      }
      return true;
    }
    delay(1);
  } while (millis() - start < timeoutMs);
  Serial.printf("SI4689 CTS timeout, last status=0x%02X\n", status);
  return false;
}

bool sendCommand(const uint8_t *bytes, size_t size, uint32_t timeoutMs = 1000) {
  if (!waitCts(timeoutMs, false)) return false;
  selectRadio();
  for (size_t i = 0; i < size; ++i) SPI.transfer(bytes[i]);
  deselectRadio();
  return waitCts(timeoutMs, true);
}

bool readReply(uint8_t *result, size_t size) {
  if (!waitCts()) return false;
  selectRadio();
  SPI.transfer(0);
  for (size_t i = 0; i < size; ++i) result[i] = SPI.transfer(0);
  deselectRadio();
  return true;
}

bool setProperty(uint16_t prop, uint16_t value) {
  const uint8_t cmd[] = {CMD_SET_PROPERTY, 0,
      static_cast<uint8_t>(prop), static_cast<uint8_t>(prop >> 8),
      static_cast<uint8_t>(value), static_cast<uint8_t>(value >> 8)};
  return sendCommand(cmd, sizeof(cmd));
}

bool loadImage(const char *label, const uint8_t *image, uint32_t size) {
  Serial.printf("Loading %s: %lu bytes\n", label, static_cast<unsigned long>(size));
  uint8_t packet[4 + 252] = {CMD_HOST_LOAD, 0, 0, 0};
  const uint32_t started = millis();
  for (uint32_t offset = 0; offset < size; offset += 252) {
    const size_t count = min(static_cast<uint32_t>(252), size - offset);
    memcpy(packet + 4, image + offset, count);
    if (!waitCts(1000, false)) return false;
    selectRadio(true);
    SPI.transferBytes(packet, nullptr, count + 4);
    deselectRadio();
    if (!waitCts()) {
      Serial.printf("Image failed at byte %lu\n", static_cast<unsigned long>(offset));
      return false;
    }
    if ((offset & 0x3fff) == 0) yield();
  }
  Serial.printf("Loaded in %lu ms\n", static_cast<unsigned long>(millis() - started));
  return true;
}

bool bootRadio(Mode mode) {
  radioReady = false;
  dabServices.clear();
  selectedService = -1;
  digitalWrite(PIN_AMP, LOW);
  ampEnabled = false;
  digitalWrite(PIN_RESET, LOW);
  delay(10);
  digitalWrite(PIN_RESET, HIGH);
  delay(200);
  Serial.printf("Reset released; SPI status=0x%02X\n", readStatus());

  uint8_t powerUp[16] = {CMD_POWER_UP};
  powerUp[2] = 0x17; // 19.2 MHz crystal, clock mode 1, tr_size 7
  powerUp[3] = 0x28;
  powerUp[4] = static_cast<uint8_t>(XTAL_HZ);
  powerUp[5] = static_cast<uint8_t>(XTAL_HZ >> 8);
  powerUp[6] = static_cast<uint8_t>(XTAL_HZ >> 16);
  powerUp[7] = static_cast<uint8_t>(XTAL_HZ >> 24);
  powerUp[8] = 0x07;
  powerUp[9] = 0x10;
  powerUp[13] = 0x18;
  if (!sendCommand(powerUp, sizeof(powerUp))) return false;

  const uint8_t loadInit[] = {CMD_LOAD_INIT, 0};
  if (!sendCommand(loadInit, sizeof(loadInit))) return false;
  if (mode == Mode::FM) {
    if (!loadImage("FM patch", fm_patch, fm_patch_size)) return false;
  } else {
    if (!loadImage("DAB patch", dab_patch, dab_patch_size)) return false;
  }
  delay(4);
  if (!sendCommand(loadInit, sizeof(loadInit))) return false;
  if (mode == Mode::FM) {
    if (!loadImage("FM/HD firmware", fm_firmware, fm_firmware_size)) return false;
  } else {
    if (!loadImage("DAB firmware", dab_firmware, dab_firmware_size)) return false;
  }
  const uint8_t boot[] = {CMD_BOOT, 0};
  if (!sendCommand(boot, sizeof(boot))) return false;

  // Analog DAC output goes directly to the shield's jack and onboard amp.
  if (!setProperty(PROP_PIN_CONFIG_ENABLE, 0x8001)) return false;
  if (!setProperty(PROP_FM_TUNE_FE_VARM, 0xFD12)) return false;
  if (!setProperty(PROP_FM_TUNE_FE_VARB, 0x009B)) return false;
  if (!setProperty(PROP_FM_TUNE_FE_CFG, 0)) return false;
  if (mode == Mode::FM) {
    if (!setProperty(0x3100, 8750)) return false;
    if (!setProperty(0x3101, 10800)) return false;
    if (!setProperty(0x3102, 10)) return false;
    if (!setProperty(0x3202, 18)) return false;
    if (!setProperty(0x3204, 6)) return false;
    if (!setProperty(0x3205, 127)) return false;
  } else {
    if (!setProperty(0xB300, 0x00C1)) return false;
    if (!setProperty(0xB201, 6)) return false;
    uint8_t freqList[4 + DAB_CHANNEL_COUNT * 4] = {CMD_DAB_SET_FREQ_LIST,
        static_cast<uint8_t>(DAB_CHANNEL_COUNT), 0, 0};
    for (size_t i = 0; i < DAB_CHANNEL_COUNT; ++i) {
      const uint32_t khz = DAB_CHANNELS[i].khz;
      for (uint8_t byte = 0; byte < 4; ++byte)
        freqList[4 + i * 4 + byte] = static_cast<uint8_t>(khz >> (8 * byte));
    }
    if (!sendCommand(freqList, sizeof(freqList))) return false;
  }
  if (!setProperty(PROP_AUDIO_ANALOG_VOLUME, volume)) return false;
  if (!setProperty(PROP_AUDIO_MUTE, 0)) return false;
  currentMode = mode;
  radioReady = true;
  Serial.printf("READY %s; analog volume %u; amp off\n", mode == Mode::FM ? "FM/HD" : "DAB", volume);
  return true;
}

bool tuneFm(uint16_t freq10kHz) {
  if (!radioReady || currentMode != Mode::FM || freq10kHz < 8750 || freq10kHz > 10800) return false;
  const uint8_t cmd[] = {CMD_FM_TUNE_FREQ, 0,
      static_cast<uint8_t>(freq10kHz), static_cast<uint8_t>(freq10kHz >> 8), 0, 0, 0};
  if (!sendCommand(cmd, sizeof(cmd))) return false;
  Serial.printf("FM tuned %u.%02u MHz\n", freq10kHz / 100, freq10kHz % 100);
  return true;
}

bool printFmStatus() {
  const uint8_t cmd[] = {CMD_FM_RSQ_STATUS, 0x04};
  if (!sendCommand(cmd, sizeof(cmd))) return false;
  uint8_t reply[23];
  if (!readReply(reply, sizeof(reply))) return false;
  const uint16_t freq = reply[6] | (reply[7] << 8);
  Serial.printf("FM status: valid=%u afc=%u hd=%u freq=%u.%02u MHz rssi=%d snr=%d dB\n",
      reply[5] & 1, (reply[5] >> 1) & 1, (reply[5] >> 5) & 1,
      freq / 100, freq % 100, static_cast<int8_t>(reply[9]), static_cast<int8_t>(reply[10]));
  return true;
}

bool tuneDab(size_t index) {
  if (!radioReady || currentMode != Mode::DAB || index >= DAB_CHANNEL_COUNT) return false;
  dabServices.clear();
  selectedService = -1;
  const uint8_t cmd[] = {CMD_DAB_TUNE_FREQ, 0, static_cast<uint8_t>(index), 0, 0, 0};
  if (!sendCommand(cmd, sizeof(cmd))) return false;
  Serial.printf("DAB tuned %s (%lu kHz)\n", DAB_CHANNELS[index].name,
      static_cast<unsigned long>(DAB_CHANNELS[index].khz));
  return true;
}

bool printDabStatus() {
  const uint8_t cmd[] = {CMD_DAB_DIGRAD_STATUS, 0};
  if (!sendCommand(cmd, sizeof(cmd))) return false;
  uint8_t reply[40];
  if (!readReply(reply, sizeof(reply))) return false;
  const uint32_t khz = reply[12] | (reply[13] << 8) | (reply[14] << 16) | (reply[15] << 24);
  Serial.printf("DAB status: acq=%u valid=%u rssi=%d snr=%u fic_quality=%u freq=%lu kHz\n",
      (reply[5] >> 2) & 1, reply[5] & 1, static_cast<int8_t>(reply[6]),
      reply[7], reply[8], static_cast<unsigned long>(khz));
  return true;
}

bool getDabEventStatus(bool acknowledge, uint8_t &events, uint8_t &audioStatus) {
  const uint8_t cmd[] = {CMD_DAB_GET_EVENT_STATUS, static_cast<uint8_t>(acknowledge ? 1 : 0)};
  uint8_t reply[9];
  if (!sendCommand(cmd, sizeof(cmd)) || !readReply(reply, sizeof(reply))) return false;
  events = reply[5];
  audioStatus = reply[8];
  return true;
}

void printDabServices();

bool loadDabServices() {
  if (!radioReady || currentMode != Mode::DAB) return false;
  dabServices.clear();
  selectedService = -1;
  uint8_t events = 0, audioStatus = 0;
  bool listReady = false;
  const uint32_t start = millis();
  do {
    if (!getDabEventStatus(false, events, audioStatus)) return false;
    if (events & 1) {
      listReady = true;
      if (!getDabEventStatus(true, events, audioStatus)) return false;
      break;
    }
    delay(100);
  } while (millis() - start < 5000);
  if (!listReady) {
    Serial.println("DAB service-list event not ready");
    return false;
  }

  const uint8_t cmd[] = {CMD_GET_DIGITAL_SERVICE_LIST, 0};
  uint8_t header[6];
  if (!sendCommand(cmd, sizeof(cmd)) || !readReply(header, sizeof(header))) return false;
  const uint16_t totalSize = header[4] | (static_cast<uint16_t>(header[5]) << 8);
  if (totalSize < 6 || totalSize > 4096) {
    Serial.printf("Invalid DAB service-list size: %u\n", totalSize);
    return false;
  }
  std::vector<uint8_t> reply(6 + totalSize);
  if (!readReply(reply.data(), reply.size())) return false;
  const uint8_t *payload = reply.data() + 6;
  const uint16_t count = payload[2] | (static_cast<uint16_t>(payload[3]) << 8);
  size_t offset = 6;
  for (uint16_t i = 0; i < count; ++i) {
    if (offset + 24 > totalSize) break;
    const uint32_t sid = static_cast<uint32_t>(payload[offset]) |
        (static_cast<uint32_t>(payload[offset + 1]) << 8) |
        (static_cast<uint32_t>(payload[offset + 2]) << 16) |
        (static_cast<uint32_t>(payload[offset + 3]) << 24);
    const uint8_t info1 = payload[offset + 4];
    const uint8_t components = payload[offset + 5] & 0x0F;
    char label[17] = {0};
    memcpy(label, payload + offset + 8, 16);
    offset += 24;
    for (uint8_t j = 0; j < components; ++j) {
      if (offset + 4 > totalSize) {
        Serial.println("Truncated DAB service list");
        return false;
      }
      const uint16_t componentId = payload[offset] | (static_cast<uint16_t>(payload[offset + 1]) << 8);
      const uint8_t tmid = (componentId >> 14) & 3;
      const bool conditionalAccess = payload[offset + 2] & 1;
      if (tmid == 0 && !conditionalAccess && !(info1 & 1)) {
        DabService service{sid, componentId, String(label)};
        service.label.trim();
        dabServices.push_back(service);
      }
      offset += 4;
    }
  }
  printDabServices();
  return true;
}

bool sendServiceCommand(uint8_t opcode, const DabService &service) {
  const uint32_t sid = service.serviceId;
  const uint32_t component = service.componentId;
  const uint8_t cmd[] = {opcode, 0, 0, 0,
      static_cast<uint8_t>(sid), static_cast<uint8_t>(sid >> 8),
      static_cast<uint8_t>(sid >> 16), static_cast<uint8_t>(sid >> 24),
      static_cast<uint8_t>(component), static_cast<uint8_t>(component >> 8),
      static_cast<uint8_t>(component >> 16), static_cast<uint8_t>(component >> 24)};
  return sendCommand(cmd, sizeof(cmd));
}

bool playDabService(size_t index) {
  if (!radioReady || currentMode != Mode::DAB || index >= dabServices.size()) return false;
  if (selectedService >= 0) {
    if (!sendServiceCommand(CMD_STOP_DIGITAL_SERVICE, dabServices[selectedService])) return false;
  }
  if (!sendServiceCommand(CMD_START_DIGITAL_SERVICE, dabServices[index])) return false;
  if (!setProperty(PROP_AUDIO_MUTE, 0)) return false;
  selectedService = static_cast<int>(index);
  Serial.printf("DAB playing %u: %s\n", static_cast<unsigned>(index), dabServices[index].label.c_str());
  delay(1500);
  uint8_t events = 0, audioStatus = 0;
  if (getDabEventStatus(false, events, audioStatus)) {
    Serial.printf("DAB audio status=0x%02X mute=%u block_error=%u block_loss=%u\n",
        audioStatus, (audioStatus >> 3) & 1, (audioStatus >> 1) & 1, audioStatus & 1);
  }
  return true;
}

void printDabServices() {
  Serial.printf("DAB audio services: %u\n", static_cast<unsigned>(dabServices.size()));
  for (size_t i = 0; i < dabServices.size(); ++i) {
    const DabService &service = dabServices[i];
    Serial.printf("  %u: %s SID=0x%08lX COMP=0x%04X\n", static_cast<unsigned>(i),
        service.label.c_str(), static_cast<unsigned long>(service.serviceId), service.componentId);
  }
}

void printHelp() {
  Serial.println("Commands (115200 baud, newline terminated):");
  Serial.println("  set mode fm|dab       Load FM or DAB firmware");
  Serial.println("  set fm <MHz>          Tune FM, e.g. set fm 101.10");
  Serial.println("  set dab <channel>     Tune DAB Band III, e.g. set dab 8C");
  Serial.println("  set volume <0..63>    Set analog volume");
  Serial.println("  set amp on|off        Control speaker amplifier");
  Serial.println("  services | play <n>   List and play a DAB audio service");
  Serial.println("  status | scan | pins | help");
  Serial.println("  Legacy aliases: mode, f, d, v, amp, s");
}

void printPins() {
  Serial.printf("Shield 40-pin header: 19 MOSI <- GPIO%u, 21 MISO -> GPIO%u, "
      "23 SCK <- GPIO%u, 24 CS <- GPIO%u\n", PIN_MOSI, PIN_MISO, PIN_SCK, PIN_CS);
  Serial.printf("Shield 40-pin header: 22 RESET <- GPIO%u, 11 AMP <- GPIO%u\n",
      PIN_RESET, PIN_AMP);
  Serial.println("Also connect shield pin 2 or 4 to 5V and a shield GND pin to ESP32 GND.");
  Serial.println("Pin 16 INT is unused; analog audio comes from the shield jack/amplifier.");
}

void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  String lower = line;
  lower.toLowerCase();
  if (lower == "help" || lower == "?") { printHelp(); return; }
  if (lower == "pins") { printPins(); return; }
  if (lower.startsWith("set ")) {
    lower.remove(0, 4);
    lower.trim();
  }
  if (lower.startsWith("f ")) lower = "fm " + lower.substring(2);
  if (lower.startsWith("d ")) lower = "dab " + lower.substring(2);
  if (lower.startsWith("v ")) lower = "volume " + lower.substring(2);
  if (lower == "mode fm" || lower == "mode dab") {
    Serial.println(bootRadio(lower.endsWith("fm") ? Mode::FM : Mode::DAB) ? "Mode ready" : "Mode boot failed");
    return;
  }
  if (lower.startsWith("amp ")) {
    const String value = lower.substring(4);
    if (value != "on" && value != "off") { Serial.println("Use set amp on|off"); return; }
    ampEnabled = value == "on";
    digitalWrite(PIN_AMP, ampEnabled ? HIGH : LOW);
    Serial.printf("Amplifier %s\n", ampEnabled ? "on" : "off");
    return;
  }
  if (lower.startsWith("volume ")) {
    const String value = lower.substring(7);
    char *end = nullptr;
    const long requested = strtol(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != '\0' || requested < 0 || requested > 63) {
      Serial.println("Volume must be an integer from 0 to 63"); return;
    }
    volume = static_cast<uint8_t>(requested);
    Serial.println(radioReady && setProperty(PROP_AUDIO_ANALOG_VOLUME, volume) ? "Volume set" : "Volume failed");
    return;
  }
  if (!radioReady) { Serial.println("Radio not ready"); return; }
  if (lower == "services") {
    if (!dabServices.empty()) printDabServices();
    else if (!loadDabServices()) Serial.println("DAB service list failed");
    return;
  }
  if (lower.startsWith("play ")) {
    const String value = lower.substring(5);
    char *end = nullptr;
    const long index = strtol(value.c_str(), &end, 10);
    if (end == value.c_str() || *end != '\0' || index < 0 ||
        static_cast<size_t>(index) >= dabServices.size()) {
      Serial.println("Invalid service number; use services first"); return;
    }
    if (!playDabService(static_cast<size_t>(index))) Serial.println("DAB play failed");
    return;
  }
  if (lower == "s" || lower == "status" || lower == "get status") {
    Serial.println((currentMode == Mode::FM ? printFmStatus() : printDabStatus()) ? "Status OK" : "Status failed");
    return;
  }
  if (lower.startsWith("fm ")) {
    const String value = lower.substring(3);
    char *end = nullptr;
    const float mhz = strtof(value.c_str(), &end);
    if (end == value.c_str() || *end != '\0' || !isfinite(mhz) || mhz < 87.5f || mhz > 108.0f) {
      Serial.println("FM frequency must be 87.5..108.0 MHz"); return;
    }
    const uint16_t freq10kHz = static_cast<uint16_t>(lroundf(mhz * 100));
    if (!tuneFm(freq10kHz)) { Serial.println("FM tune failed"); return; }
    delay(500);
    printFmStatus();
    return;
  }
  if (lower.startsWith("dab ")) {
    String channel = lower.substring(4);
    channel.trim(); channel.toUpperCase();
    for (size_t i = 0; i < DAB_CHANNEL_COUNT; ++i) {
      if (channel == DAB_CHANNELS[i].name) {
        if (!tuneDab(i)) { Serial.println("DAB tune failed"); return; }
        delay(1200);
        printDabStatus();
        return;
      }
    }
    Serial.println("Unknown DAB channel");
    return;
  }
  if (lower == "scan") {
    if (currentMode != Mode::FM) { Serial.println("Use mode fm first"); return; }
    for (uint16_t freq = 8750; freq <= 10800; freq += 10) {
      if (!tuneFm(freq)) break;
      delay(75);
      const uint8_t cmd[] = {CMD_FM_RSQ_STATUS, 0x04};
      uint8_t reply[23];
      if (sendCommand(cmd, sizeof(cmd)) && readReply(reply, sizeof(reply)) &&
          ((reply[5] & 1) || (static_cast<int8_t>(reply[9]) >= 18 && static_cast<int8_t>(reply[10]) >= 6))) {
        Serial.printf("FOUND %u.%02u MHz rssi=%d snr=%d\n", freq / 100, freq % 100,
            static_cast<int8_t>(reply[9]), static_cast<int8_t>(reply[10]));
      }
      yield();
    }
    Serial.println("Scan complete");
    return;
  }
  printHelp();
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println("\nZeroCore S3 / Raspiaudio Digital Radio bring-up");
  pinMode(PIN_AMP, OUTPUT);
  digitalWrite(PIN_AMP, LOW);
  pinMode(PIN_RESET, OUTPUT);
  digitalWrite(PIN_RESET, LOW);
  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
  printHelp();
  Serial.println(bootRadio(Mode::FM) ? "BOOT OK" : "BOOT FAILED");
}

void loop() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r' || c == '\n') {
      if (commandLine.length()) handleCommand(commandLine);
      commandLine = "";
    } else if (commandLine.length() < 96) {
      commandLine += c;
    }
  }
  delay(1);
}
