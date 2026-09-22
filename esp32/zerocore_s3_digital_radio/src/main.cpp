#include <Arduino.h>
#include <SPI.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include <vector>
#include "firmware_images.h"
#include "radio_pins.h"
#ifdef RADIO_WEB_UI
#include <WiFi.h>
#include <WebServer.h>
#include <Preferences.h>
#include <ESPmDNS.h>
#ifdef RADIO_FLASH_WIFI_STORE
#include <esp_partition.h>
#include <esp_spi_flash.h>
#endif
#include "web_page.h"
#ifndef RADIO_AP_PASSWORD
#define RADIO_AP_PASSWORD "raspiaudio"
#endif
#endif

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
uint16_t currentFmFreq = 0;
int currentDabIndex = -1;
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
  currentFmFreq = 0;
  currentDabIndex = -1;
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
  currentFmFreq = freq10kHz;
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
  currentDabIndex = static_cast<int>(index);
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
#ifdef RADIO_WEB_UI
  Serial.println("  wifi | wifi retry | wifi set <ssid> <password> | wifi clear | wifi reboot");
#endif
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

#ifdef RADIO_WEB_UI
void printWebNetworkStatus();
bool saveWebWifiCredentials(const String &ssid, const String &password);
bool clearWebWifiCredentials();
void scheduleWebWifiSwitch(bool useSaved);
#endif

void handleCommand(String line) {
  line.trim();
  if (!line.length()) return;
  String lower = line;
  lower.toLowerCase();
  if (lower == "help" || lower == "?") { printHelp(); return; }
  if (lower == "pins") { printPins(); return; }
#ifdef RADIO_WEB_UI
  if (lower == "wifi") {
    printWebNetworkStatus();
    return;
  }
  if (lower == "wifi retry") {
    Serial.println("Retrying saved WiFi credentials");
    scheduleWebWifiSwitch(true);
    return;
  }
  if (lower == "wifi reboot") {
    Serial.println("Rebooting");
    Serial.flush();
    ESP.restart();
    return;
  }
  if (lower.startsWith("wifi set ")) {
    const int separator = line.indexOf(' ', 9);
    if (separator < 0) { Serial.println("Use wifi set <ssid> <password>"); return; }
    const String ssid = line.substring(9, separator);
    String password = line.substring(separator + 1);
    password.trim();
    if (!saveWebWifiCredentials(ssid, password)) {
      Serial.println("WiFi credentials rejected or device storage failed"); return;
    }
    Serial.println("WiFi credentials saved on device; connecting (password hidden)");
    scheduleWebWifiSwitch(true);
    return;
  }
  if (lower == "wifi clear") {
    if (!clearWebWifiCredentials()) { Serial.println("WiFi storage erase failed"); return; }
    Serial.println("WiFi credentials erased; starting hotspot");
    scheduleWebWifiSwitch(false);
    return;
  }
#endif
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

#ifdef RADIO_WEB_UI
struct WebMetrics {
  bool ok = false;
  bool valid = false;
  bool acquired = false;
  int rssi = 0;
  int snr = 0;
  int ficQuality = 0;
};
struct FmScanResult { uint16_t frequency; int8_t rssi; int8_t snr; };
struct DabScanResult { uint8_t index; int8_t rssi; uint8_t ficQuality; };
enum class WebScanMode { None, FM, DAB };

WebServer webServer(80);
String hotspotName;
String mdnsName;
String savedWifiSsid;
String savedWifiPassword;
enum class NetworkMode { None, Connecting, Station, Hotspot };
NetworkMode networkMode = NetworkMode::None;
bool webServerStarted = false;
bool wifiSwitchPending = false;
bool wifiSwitchUseSaved = false;
uint32_t wifiSwitchAt = 0;
uint32_t wifiConnectAt = 0;
uint32_t wifiLostAt = 0;
std::vector<FmScanResult> fmScanResults;
std::vector<DabScanResult> dabScanResults;
WebScanMode webScanMode = WebScanMode::None;
size_t webScanIndex = 0;
size_t webScanTotal = 0;
bool webScanWaiting = false;
uint32_t webScanTuneAt = 0;
uint32_t webScanLastCheck = 0;

#ifdef RADIO_FLASH_WIFI_STORE
// The tested ZeroCore S3 NVS partition loses new entries across restarts.
// A dedicated radio_cfg data partition stores device-local Wi-Fi settings.
struct FlashWifiSettings {
  uint32_t magic;
  uint8_t version;
  uint8_t ssidLength;
  uint8_t passwordLength;
  uint8_t reserved;
  char ssid[33];
  char password[64];
  uint32_t checksum;
};
constexpr uint32_t WIFI_SETTINGS_MAGIC = 0x52445746;
constexpr size_t WIFI_SETTINGS_SECTOR_SIZE = 4096;
static_assert(sizeof(FlashWifiSettings) < WIFI_SETTINGS_SECTOR_SIZE, "WiFi settings exceed flash sector");

const esp_partition_t *wifiSettingsPartition() {
  const esp_partition_t *partition = esp_partition_find_first(
      ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_ANY, "radio_cfg");
  return partition && partition->size >= WIFI_SETTINGS_SECTOR_SIZE ? partition : nullptr;
}

uint32_t wifiSettingsChecksum(const FlashWifiSettings &settings) {
  const uint8_t *bytes = reinterpret_cast<const uint8_t *>(&settings);
  uint32_t hash = 2166136261u;
  for (size_t i = 0; i < sizeof(settings) - sizeof(settings.checksum); ++i)
    hash = (hash ^ bytes[i]) * 16777619u;
  return hash;
}

bool readFlashWifiSettings(const esp_partition_t *partition, FlashWifiSettings &settings);

bool loadWebWifiCredentials() {
  const esp_partition_t *partition = wifiSettingsPartition();
  if (!partition) return false;
  FlashWifiSettings settings{};
  if (!readFlashWifiSettings(partition, settings)) return false;
  if (settings.magic != WIFI_SETTINGS_MAGIC || settings.version != 1 ||
      settings.ssidLength < 1 || settings.ssidLength > 32 ||
      settings.passwordLength < 8 || settings.passwordLength > 63 ||
      settings.ssid[settings.ssidLength] != '\0' ||
      settings.password[settings.passwordLength] != '\0' ||
      settings.checksum != wifiSettingsChecksum(settings)) return false;
  savedWifiSsid = settings.ssid;
  savedWifiPassword = settings.password;
  return true;
}

bool readFlashWifiSettings(const esp_partition_t *partition, FlashWifiSettings &settings) {
  const void *mapped = nullptr;
  spi_flash_mmap_handle_t mapping = 0;
  const esp_err_t error = esp_partition_mmap(partition, 0, sizeof(settings),
      SPI_FLASH_MMAP_DATA, &mapped, &mapping);
  if (error != ESP_OK) return false;
  memcpy(&settings, mapped, sizeof(settings));
  spi_flash_munmap(mapping);
  return true;
}

bool saveFlashWebWifiCredentials(const String &ssid, const String &password) {
  const esp_partition_t *partition = wifiSettingsPartition();
  if (!partition) { Serial.println("radio_cfg partition not found"); return false; }
  FlashWifiSettings settings{};
  settings.magic = WIFI_SETTINGS_MAGIC;
  settings.version = 1;
  settings.ssidLength = ssid.length();
  settings.passwordLength = password.length();
  memcpy(settings.ssid, ssid.c_str(), ssid.length());
  memcpy(settings.password, password.c_str(), password.length());
  settings.checksum = wifiSettingsChecksum(settings);
  const size_t offset = 0;
  esp_err_t error = esp_partition_erase_range(partition, offset, WIFI_SETTINGS_SECTOR_SIZE);
  if (error != ESP_OK) { Serial.printf("radio_cfg erase failed: %s\n", esp_err_to_name(error)); return false; }
  error = esp_partition_write(partition, offset, &settings, sizeof(settings));
  if (error != ESP_OK) { Serial.printf("radio_cfg write failed: %s\n", esp_err_to_name(error)); return false; }
  FlashWifiSettings verification{};
  if (!readFlashWifiSettings(partition, verification)) {
    Serial.println("radio_cfg mapped read failed"); return false;
  }
  if (verification.checksum != settings.checksum ||
      memcmp(&verification, &settings, sizeof(settings)) != 0) {
    const uint8_t *actual = reinterpret_cast<const uint8_t *>(&verification);
    const uint8_t *expected = reinterpret_cast<const uint8_t *>(&settings);
    size_t mismatch = 0;
    while (mismatch < sizeof(settings) && actual[mismatch] == expected[mismatch]) ++mismatch;
    Serial.printf("radio_cfg verification failed at byte %u, checksum %s\n",
        static_cast<unsigned>(mismatch),
        verification.checksum == settings.checksum ? "matches" : "differs");
    return false;
  }
  return true;
}

bool clearFlashWebWifiCredentials() {
  const esp_partition_t *partition = wifiSettingsPartition();
  return partition && esp_partition_erase_range(partition, 0,
      WIFI_SETTINGS_SECTOR_SIZE) == ESP_OK;
}
#endif

bool saveWebWifiCredentials(const String &ssid, const String &password) {
  if (ssid.length() < 1 || ssid.length() > 32 ||
      password.length() < 8 || password.length() > 63) return false;
#ifdef RADIO_FLASH_WIFI_STORE
  if (!saveFlashWebWifiCredentials(ssid, password)) return false;
#else
  Preferences prefs;
  if (!prefs.begin("radio_wifi", false)) return false;
  const bool written = prefs.putString("ssid", ssid) == ssid.length() &&
      prefs.putString("pass", password) == password.length();
  if (!written) prefs.clear();
  prefs.end();
  if (!written) return false;
#endif
  savedWifiSsid = ssid;
  savedWifiPassword = password;
  return true;
}

bool clearWebWifiCredentials() {
#ifdef RADIO_FLASH_WIFI_STORE
  if (!clearFlashWebWifiCredentials()) return false;
#else
  Preferences prefs;
  if (!prefs.begin("radio_wifi", false)) return false;
  const bool cleared = prefs.clear();
  prefs.end();
  if (!cleared) return false;
#endif
  savedWifiSsid = "";
  savedWifiPassword = "";
  return true;
}

void scheduleWebWifiSwitch(bool useSaved) {
  wifiSwitchPending = true;
  wifiSwitchUseSaved = useSaved;
  wifiSwitchAt = millis() + 800;
}

void stopWebNetwork() {
  if (webServerStarted) {
    webServer.stop();
    webServerStarted = false;
  }
  MDNS.end();
}

void startWebHotspot() {
  stopWebNetwork();
  WiFi.mode(WIFI_AP);
  const IPAddress address(192, 168, 4, 1);
  WiFi.softAPConfig(address, address, IPAddress(255, 255, 255, 0));
  if (!WiFi.softAP(hotspotName.c_str(), RADIO_AP_PASSWORD)) {
    networkMode = NetworkMode::None;
    Serial.println("Hotspot start failed");
    return;
  }
  networkMode = NetworkMode::Hotspot;
  webServer.begin();
  webServerStarted = true;
  Serial.printf("Hotspot %s | password %s | http://%s/\n", hotspotName.c_str(),
      RADIO_AP_PASSWORD, WiFi.softAPIP().toString().c_str());
}

void startWebStation() {
  if (!savedWifiSsid.length()) { startWebHotspot(); return; }
  stopWebNetwork();
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  WiFi.begin(savedWifiSsid.c_str(), savedWifiPassword.c_str());
  networkMode = NetworkMode::Connecting;
  wifiConnectAt = millis();
  Serial.printf("Connecting to WiFi %s (password hidden)\n", savedWifiSsid.c_str());
}

void maintainWebNetwork() {
  if (wifiSwitchPending && static_cast<int32_t>(millis() - wifiSwitchAt) >= 0) {
    wifiSwitchPending = false;
    if (wifiSwitchUseSaved) startWebStation();
    else startWebHotspot();
  }
  if (networkMode == NetworkMode::Connecting) {
    if (WiFi.status() == WL_CONNECTED) {
      networkMode = NetworkMode::Station;
      wifiLostAt = 0;
      webServer.begin();
      webServerStarted = true;
      if (!MDNS.begin(mdnsName.c_str())) Serial.println("mDNS start failed; use the IP address");
      Serial.printf("WiFi connected: %s | http://%s/ | http://%s.local/ | RSSI %d dBm\n",
          savedWifiSsid.c_str(), WiFi.localIP().toString().c_str(), mdnsName.c_str(),
          WiFi.RSSI());
    } else if (millis() - wifiConnectAt >= 30000) {
      Serial.println("WiFi connection timed out; falling back to hotspot");
      startWebHotspot();
    }
  } else if (networkMode == NetworkMode::Station) {
    if (WiFi.status() == WL_CONNECTED) wifiLostAt = 0;
    else if (!wifiLostAt) wifiLostAt = millis();
    else if (millis() - wifiLostAt >= 10000) {
      Serial.println("WiFi disconnected; falling back to hotspot");
      startWebHotspot();
    }
  }
}

void printWebNetworkStatus() {
  if (networkMode == NetworkMode::Station && WiFi.status() == WL_CONNECTED) {
    Serial.printf("WiFi STA %s | http://%s/ | http://%s.local/ (password hidden)\n",
        savedWifiSsid.c_str(), WiFi.localIP().toString().c_str(), mdnsName.c_str());
  } else if (networkMode == NetworkMode::Connecting) {
    Serial.printf("Connecting to WiFi %s (password hidden); hotspot returns after timeout\n",
        savedWifiSsid.c_str());
  } else if (networkMode == NetworkMode::Hotspot) {
    Serial.printf("Hotspot %s | password %s | http://%s/ | clients %u\n",
        hotspotName.c_str(), RADIO_AP_PASSWORD, WiFi.softAPIP().toString().c_str(),
        WiFi.softAPgetStationNum());
  } else Serial.println("WiFi is not ready");
}

WebMetrics readWebMetrics() {
  WebMetrics result;
  if (!radioReady) return result;
  if (currentMode == Mode::FM) {
    const uint8_t cmd[] = {CMD_FM_RSQ_STATUS, 0x04};
    uint8_t reply[23];
    if (!sendCommand(cmd, sizeof(cmd)) || !readReply(reply, sizeof(reply))) return result;
    result.valid = reply[5] & 1;
    result.acquired = result.valid;
    result.rssi = static_cast<int8_t>(reply[9]);
    result.snr = static_cast<int8_t>(reply[10]);
  } else {
    const uint8_t cmd[] = {CMD_DAB_DIGRAD_STATUS, 0};
    uint8_t reply[40];
    if (!sendCommand(cmd, sizeof(cmd)) || !readReply(reply, sizeof(reply))) return result;
    result.acquired = reply[5] & 4;
    result.valid = reply[5] & 1;
    result.rssi = static_cast<int8_t>(reply[6]);
    result.snr = static_cast<int8_t>(reply[7]);
    result.ficQuality = reply[8];
  }
  result.ok = true;
  return result;
}

String jsonQuoted(const String &value) {
  String output = "\"";
  for (size_t i = 0; i < value.length(); ++i) {
    const uint8_t c = static_cast<uint8_t>(value[i]);
    if (c == '"' || c == '\\') { output += '\\'; output += static_cast<char>(c); }
    else if (c < 0x20) output += ' ';
    else if (c >= 0x80) {
      // SI4689 labels are usually Latin-1; convert their bytes to UTF-8.
      output += static_cast<char>(0xC0 | (c >> 6));
      output += static_cast<char>(0x80 | (c & 0x3F));
    } else output += static_cast<char>(c);
  }
  output += '"';
  return output;
}

void sendWebError(int code, const char *reason) {
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(code, "application/json; charset=utf-8",
      String("{\"ok\":false,\"error\":") + jsonQuoted(reason) + "}");
}

void sendWebOk() {
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(200, "application/json; charset=utf-8", "{\"ok\":true}");
}

bool parseLongExact(const String &input, long &value) {
  if (!input.length()) return false;
  char *end = nullptr;
  value = strtol(input.c_str(), &end, 10);
  return end != input.c_str() && *end == '\0';
}

void startWebScan() {
  webScanMode = currentMode == Mode::FM ? WebScanMode::FM : WebScanMode::DAB;
  webScanIndex = 0;
  webScanTotal = currentMode == Mode::FM ? 206 : DAB_CHANNEL_COUNT;
  webScanWaiting = false;
  if (currentMode == Mode::FM) fmScanResults.clear();
  else dabScanResults.clear();
}

void processWebScan() {
  if (webScanMode == WebScanMode::None) return;
  if (!radioReady || (webScanMode == WebScanMode::FM && currentMode != Mode::FM) ||
      (webScanMode == WebScanMode::DAB && currentMode != Mode::DAB)) {
    webScanMode = WebScanMode::None;
    return;
  }
  if (webScanIndex >= webScanTotal) {
    webScanMode = WebScanMode::None;
    Serial.println("Web scan complete");
    return;
  }
  if (!webScanWaiting) {
    const bool tuned = webScanMode == WebScanMode::FM
        ? tuneFm(static_cast<uint16_t>(8750 + webScanIndex * 10)) : tuneDab(webScanIndex);
    if (!tuned) { webScanMode = WebScanMode::None; return; }
    webScanTuneAt = millis();
    webScanLastCheck = 0;
    webScanWaiting = true;
    return;
  }
  const uint32_t elapsed = millis() - webScanTuneAt;
  if (webScanMode == WebScanMode::FM) {
    if (elapsed < 80) return;
    const WebMetrics metrics = readWebMetrics();
    if (metrics.ok && (metrics.valid || (metrics.rssi >= 18 && metrics.snr >= 6))) {
      fmScanResults.push_back({static_cast<uint16_t>(8750 + webScanIndex * 10),
          static_cast<int8_t>(metrics.rssi), static_cast<int8_t>(metrics.snr)});
    }
    ++webScanIndex;
    webScanWaiting = false;
    return;
  }
  if (elapsed < 700 || millis() - webScanLastCheck < 250) return;
  webScanLastCheck = millis();
  const WebMetrics metrics = readWebMetrics();
  if (metrics.ok && metrics.valid && metrics.acquired && metrics.ficQuality > 0) {
    dabScanResults.push_back({static_cast<uint8_t>(webScanIndex),
        static_cast<int8_t>(metrics.rssi), static_cast<uint8_t>(metrics.ficQuality)});
    ++webScanIndex;
    webScanWaiting = false;
  } else if (elapsed >= 4500) {
    ++webScanIndex;
    webScanWaiting = false;
  }
}

void sendWebStatus() {
  const WebMetrics metrics = readWebMetrics();
  String json;
  json.reserve(3500);
  json += "{\"ok\":true,\"ready\":";
  json += radioReady ? "true" : "false";
  json += ",\"mode\":\"";
  json += currentMode == Mode::FM ? "fm" : "dab";
  json += "\",\"volume\":";
  json += volume;
  json += ",\"amp\":";
  json += ampEnabled ? "true" : "false";
  json += ",\"valid\":";
  json += metrics.valid ? "true" : "false";
  json += ",\"rssi\":";
  json += metrics.ok ? String(metrics.rssi) : "null";
  json += ",\"snr\":";
  json += metrics.ok ? String(metrics.snr) : "null";
  json += ",\"fic_quality\":";
  json += metrics.ok && currentMode == Mode::DAB ? String(metrics.ficQuality) : "null";
  json += ",\"frequency\":";
  json += currentMode == Mode::FM && currentFmFreq ? String(currentFmFreq / 100.0f, 2) : "null";
  json += ",\"channel\":";
  json += currentMode == Mode::DAB && currentDabIndex >= 0
      ? jsonQuoted(DAB_CHANNELS[currentDabIndex].name) : "null";
  json += ",\"station\":";
  json += currentMode == Mode::DAB && selectedService >= 0 &&
      static_cast<size_t>(selectedService) < dabServices.size()
      ? jsonQuoted(dabServices[selectedService].label) : "null";
  json += ",\"scan\":{\"active\":";
  json += webScanMode != WebScanMode::None ? "true" : "false";
  json += ",\"current\":";
  json += static_cast<unsigned>(webScanIndex + (webScanWaiting ? 1 : 0));
  json += ",\"total\":";
  json += static_cast<unsigned>(webScanTotal);
  json += ",\"found\":";
  json += static_cast<unsigned>(currentMode == Mode::FM ? fmScanResults.size() : dabScanResults.size());
  json += "},\"wifi\":{\"mode\":";
  json += jsonQuoted(networkMode == NetworkMode::Station ? "sta" :
      networkMode == NetworkMode::Hotspot ? "ap" : "connecting");
  json += ",\"ssid\":";
  json += jsonQuoted(networkMode == NetworkMode::Hotspot ? hotspotName : savedWifiSsid);
  json += ",\"configured_ssid\":";
  json += jsonQuoted(savedWifiSsid);
  json += ",\"ip\":";
  json += jsonQuoted(networkMode == NetworkMode::Station ? WiFi.localIP().toString() :
      networkMode == NetworkMode::Hotspot ? WiFi.softAPIP().toString() : "");
  json += ",\"clients\":";
  json += networkMode == NetworkMode::Hotspot ? WiFi.softAPgetStationNum() : 0;
  json += "},\"fm_stations\":[";
  for (size_t i = 0; i < fmScanResults.size(); ++i) {
    if (i) json += ',';
    const FmScanResult &station = fmScanResults[i];
    json += "{\"frequency\":" + String(station.frequency / 100.0f, 2) +
        ",\"rssi\":" + String(station.rssi) + ",\"snr\":" + String(station.snr) + "}";
  }
  json += "],\"services\":[";
  for (size_t i = 0; i < dabServices.size(); ++i) {
    if (i) json += ',';
    json += "{\"index\":" + String(static_cast<unsigned>(i)) +
        ",\"label\":" + jsonQuoted(dabServices[i].label) + "}";
  }
  json += "],\"multiplexes\":[";
  for (size_t i = 0; i < dabScanResults.size(); ++i) {
    if (i) json += ',';
    const DabScanResult &mux = dabScanResults[i];
    json += "{\"channel\":" + jsonQuoted(DAB_CHANNELS[mux.index].name) +
        ",\"rssi\":" + String(mux.rssi) +
        ",\"fic_quality\":" + String(mux.ficQuality) + "}";
  }
  json += "]}";
  webServer.sendHeader("Cache-Control", "no-store");
  webServer.send(200, "application/json; charset=utf-8", json);
}

void setupWeb() {
  if (strlen(RADIO_AP_PASSWORD) < 8) {
    Serial.println("Hotspot password must be at least 8 characters");
    return;
  }
  char suffix[7];
  snprintf(suffix, sizeof(suffix), "%06lX",
      static_cast<unsigned long>(ESP.getEfuseMac() & 0xFFFFFF));
  hotspotName = String("RASPIAUDIO-Radio-") + suffix;
  mdnsName = String("raspiaudio-radio-") + suffix;
  mdnsName.toLowerCase();
  #ifdef RADIO_FLASH_WIFI_STORE
  if (loadWebWifiCredentials()) {
    Serial.printf("Saved WiFi in flash: SSID %s, password length %u\n",
        savedWifiSsid.c_str(), static_cast<unsigned>(savedWifiPassword.length()));
  } else {
    Serial.println("No saved WiFi in flash");
  }
  #else
  Preferences prefs;
  if (prefs.begin("radio_wifi", true)) {
    savedWifiSsid = prefs.getString("ssid", "");
    savedWifiPassword = prefs.getString("pass", "");
    prefs.end();
    Serial.printf("Saved WiFi in NVS: SSID %s, password length %u\n",
        savedWifiSsid.c_str(), static_cast<unsigned>(savedWifiPassword.length()));
  } else {
    Serial.println("Saved WiFi NVS open failed");
  }
  #endif
  WiFi.persistent(false);
  WiFi.onEvent([](arduino_event_id_t event, arduino_event_info_t info) {
    if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED &&
        networkMode != NetworkMode::Hotspot) {
      Serial.printf("WiFi disconnected, reason %u\n", info.wifi_sta_disconnected.reason);
    }
  }, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  webServer.on("/", HTTP_GET, []() {
    webServer.send_P(200, "text/html; charset=utf-8", RADIO_WEB_PAGE);
  });
  webServer.on("/api/channels", HTTP_GET, []() {
    String json = "{\"ok\":true,\"channels\":[";
    for (size_t i = 0; i < DAB_CHANNEL_COUNT; ++i) {
      if (i) json += ',';
      json += jsonQuoted(DAB_CHANNELS[i].name);
    }
    json += "]}";
    webServer.send(200, "application/json; charset=utf-8", json);
  });
  webServer.on("/api/status", HTTP_GET, sendWebStatus);
  webServer.on("/api/mode", HTTP_POST, []() {
    if (webScanMode != WebScanMode::None) { sendWebError(409, "Attendez la fin du scan"); return; }
    const String mode = webServer.arg("mode");
    if (mode != "fm" && mode != "dab") { sendWebError(400, "Mode inconnu"); return; }
    if (!bootRadio(mode == "fm" ? Mode::FM : Mode::DAB)) {
      sendWebError(503, "Chargement du firmware radio échoué"); return;
    }
    sendWebOk();
  });
  webServer.on("/api/tune", HTTP_POST, []() {
    if (webScanMode != WebScanMode::None) { sendWebError(409, "Attendez la fin du scan"); return; }
    if (!radioReady) { sendWebError(503, "Radio indisponible"); return; }
    if (currentMode == Mode::FM) {
      const String input = webServer.arg("frequency");
      char *end = nullptr;
      const float mhz = strtof(input.c_str(), &end);
      if (end == input.c_str() || *end != '\0' || !isfinite(mhz) || mhz < 87.5f || mhz > 108.0f) {
        sendWebError(400, "Fréquence FM attendue entre 87,5 et 108 MHz"); return;
      }
      if (!tuneFm(static_cast<uint16_t>(lroundf(mhz * 100)))) {
        sendWebError(503, "Réglage FM échoué"); return;
      }
    } else {
      String channel = webServer.arg("channel");
      channel.toUpperCase();
      size_t index = 0;
      while (index < DAB_CHANNEL_COUNT && channel != DAB_CHANNELS[index].name) ++index;
      if (index == DAB_CHANNEL_COUNT) { sendWebError(400, "Canal DAB inconnu"); return; }
      if (!tuneDab(index)) { sendWebError(503, "Réglage DAB échoué"); return; }
    }
    sendWebOk();
  });
  webServer.on("/api/services", HTTP_POST, []() {
    if (webScanMode != WebScanMode::None) { sendWebError(409, "Attendez la fin du scan"); return; }
    if (!radioReady || currentMode != Mode::DAB || currentDabIndex < 0) {
      sendWebError(400, "Réglez d'abord un canal DAB"); return;
    }
    if (dabServices.empty() && !loadDabServices()) {
      sendWebError(503, "Liste des services indisponible, réessayez dans quelques secondes"); return;
    }
    sendWebOk();
  });
  webServer.on("/api/play", HTTP_POST, []() {
    if (webScanMode != WebScanMode::None) { sendWebError(409, "Attendez la fin du scan"); return; }
    long index = -1;
    if (!parseLongExact(webServer.arg("index"), index) || index < 0 ||
        static_cast<size_t>(index) >= dabServices.size()) {
      sendWebError(400, "Service DAB inconnu"); return;
    }
    if (!playDabService(static_cast<size_t>(index))) {
      sendWebError(503, "Lecture DAB échouée"); return;
    }
    sendWebOk();
  });
  webServer.on("/api/volume", HTTP_POST, []() {
    long requested = -1;
    if (!parseLongExact(webServer.arg("value"), requested) || requested < 0 || requested > 63) {
      sendWebError(400, "Volume attendu entre 0 et 63"); return;
    }
    if (!radioReady || !setProperty(PROP_AUDIO_ANALOG_VOLUME, requested)) {
      sendWebError(503, "Réglage du volume échoué"); return;
    }
    volume = static_cast<uint8_t>(requested);
    sendWebOk();
  });
  webServer.on("/api/amp", HTTP_POST, []() {
    if (!radioReady) { sendWebError(503, "Radio indisponible"); return; }
    const String value = webServer.arg("on");
    if (value != "0" && value != "1") { sendWebError(400, "État ampli inconnu"); return; }
    ampEnabled = value == "1";
    digitalWrite(PIN_AMP, ampEnabled ? HIGH : LOW);
    sendWebOk();
  });
  webServer.on("/api/scan", HTTP_POST, []() {
    if (!radioReady) { sendWebError(503, "Radio indisponible"); return; }
    if (webScanMode != WebScanMode::None) { sendWebError(409, "Scan déjà en cours"); return; }
    startWebScan();
    sendWebOk();
  });
  webServer.on("/api/wifi", HTTP_POST, []() {
    if (networkMode != NetworkMode::Hotspot) {
      sendWebError(403, "Configuration WiFi disponible depuis le hotspot uniquement"); return;
    }
    if (!saveWebWifiCredentials(webServer.arg("ssid"), webServer.arg("password"))) {
      sendWebError(400, "SSID (1-32 caractères) et mot de passe (8-63 caractères) requis"); return;
    }
    sendWebOk();
    scheduleWebWifiSwitch(true);
  });
  webServer.on("/api/wifi/clear", HTTP_POST, []() {
    if (networkMode != NetworkMode::Hotspot) {
      sendWebError(403, "Configuration WiFi disponible depuis le hotspot uniquement"); return;
    }
    if (!clearWebWifiCredentials()) { sendWebError(503, "Effacement des identifiants échoué"); return; }
    sendWebOk();
    scheduleWebWifiSwitch(false);
  });
  webServer.on("/api/wifi/retry", HTTP_POST, []() {
    if (networkMode != NetworkMode::Hotspot || !savedWifiSsid.length()) {
      sendWebError(403, "Aucun réseau enregistré à réessayer depuis le hotspot"); return;
    }
    sendWebOk();
    scheduleWebWifiSwitch(true);
  });
  webServer.onNotFound([]() { sendWebError(404, "Page introuvable"); });
  startWebHotspot();
  if (savedWifiSsid.length() && savedWifiPassword.length()) scheduleWebWifiSwitch(true);
}
#endif

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
#ifdef RADIO_WEB_UI
  setupWeb();
#endif
}

void loop() {
  while (Serial.available()) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r' || c == '\n') {
      if (commandLine.length()) handleCommand(commandLine);
      commandLine = "";
    } else if (commandLine.length() < 128) {
      commandLine += c;
    }
  }
#ifdef RADIO_WEB_UI
  maintainWebNetwork();
  if (webServerStarted) webServer.handleClient();
  processWebScan();
#endif
  delay(1);
}
