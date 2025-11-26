#include <Arduino.h>
#include <FS.h>
#include <LittleFS.h>
#include <SPI.h>
#include <vector>

#include "radio_types.h"

// ---------------------------------------------------------------------------
// Pinout (ESP32)
// ---------------------------------------------------------------------------
constexpr uint8_t PIN_MOSI = 13;
constexpr uint8_t PIN_MISO = 19;
constexpr uint8_t PIN_SCK = 14;
constexpr uint8_t PIN_CS = 15;
constexpr int8_t PIN_RST = 12;

// ---------------------------------------------------------------------------
// Files stored in LittleFS
// ---------------------------------------------------------------------------
const char* PATH_PATCH = "/rom00_patch.016.bin";
const char* PATH_FW = "/dab_radio_6_0_8.bin";
const char* PATH_SCAN = "/scan.tsv";

// ---------------------------------------------------------------------------
// Si468x command and property IDs (subset)
// ---------------------------------------------------------------------------
constexpr uint8_t CMD_POWER_UP = 0x01;
constexpr uint8_t CMD_GET_PART_INFO = 0x02;
constexpr uint8_t CMD_HOST_LOAD = 0x04;
constexpr uint8_t CMD_LOAD_INIT = 0x06;
constexpr uint8_t CMD_BOOT = 0x07;
constexpr uint8_t CMD_SET_PROPERTY = 0x13;
constexpr uint8_t CMD_READ_OFFSET = 0x10;
constexpr uint8_t CMD_GET_DIGITAL_SERVICE_LIST = 0x80;
constexpr uint8_t CMD_START_DIGITAL_SERVICE = 0x81;
constexpr uint8_t CMD_STOP_DIGITAL_SERVICE = 0x82;
constexpr uint8_t CMD_DAB_TUNE_FREQ = 0xB0;
constexpr uint8_t CMD_DAB_DIGRAD_STATUS = 0xB2;
constexpr uint8_t CMD_DAB_GET_EVENT_STATUS = 0xB3;
constexpr uint8_t CMD_DAB_SET_FREQ_LIST = 0xB8;

constexpr uint16_t PROP_PIN_CONFIG_ENABLE = 0x0800;
constexpr uint16_t PROP_DIGITAL_IO_OUTPUT_SELECT = 0x0200;
constexpr uint16_t PROP_DIGITAL_IO_OUTPUT_SAMPLE_RATE = 0x0201;
constexpr uint16_t PROP_DIGITAL_IO_OUTPUT_FORMAT = 0x0202;
constexpr uint16_t PROP_AUDIO_ANALOG_VOLUME = 0x0300;
constexpr uint16_t PROP_AUDIO_MUTE = 0x0301;
constexpr uint16_t PROP_DAB_TUNE_FE_VARM = 0x1710;
constexpr uint16_t PROP_DAB_TUNE_FE_VARB = 0x1711;
constexpr uint16_t PROP_DAB_TUNE_FE_CFG = 0x1712;
constexpr uint16_t PROP_DAB_EVENT_INTERRUPT_SOURCE = 0xB300;
constexpr uint16_t PROP_DAB_VALID_RSSI_THRESHOLD = 0xB201;

// ---------------------------------------------------------------------------
// DAB Band III table
// ---------------------------------------------------------------------------
struct DabChannel {
  const char* label;
  uint32_t freqKHz;
};

const DabChannel DAB_BAND_III[] = {
    {"5A", 174928}, {"5B", 176640}, {"5C", 178352}, {"5D", 180064}, {"6A", 181936},
    {"6B", 183648}, {"6C", 185360}, {"6D", 187072}, {"7A", 188928}, {"7B", 190640},
    {"7C", 192352}, {"7D", 194064}, {"8A", 195936}, {"8B", 197648}, {"8C", 199360},
    {"8D", 201072}, {"9A", 202928}, {"9B", 204640}, {"9C", 206352}, {"9D", 208064},
    {"10A", 209936}, {"10B", 211648}, {"10C", 213360}, {"10D", 215072}, {"10N", 210096},
    {"11A", 216928}, {"11B", 218640}, {"11C", 220352}, {"11D", 222064}, {"11N", 217088},
    {"12A", 223936}, {"12B", 225648}, {"12C", 227360}, {"12D", 229072}, {"12N", 224096},
    {"13A", 230784}, {"13B", 232496}, {"13C", 234208}, {"13D", 235776}, {"13E", 237488},
    {"13F", 239200},
};
constexpr size_t DAB_BAND_SIZE = sizeof(DAB_BAND_III) / sizeof(DAB_BAND_III[0]);

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
static inline int8_t signedByte(uint8_t v) {
  return (v & 0x80U) ? static_cast<int8_t>(v - 0x100) : static_cast<int8_t>(v);
}

class Si468xRadio {
 public:
  bool begin();
  bool reset();
  bool powerUp(uint32_t xtalHz, uint8_t ctun = 0x07, uint8_t ibias = 0x28, uint8_t ibiasRun = 0x18);
  bool loadPatchAndFirmware(const char* patchPath, const char* fwPath);
  bool configureAudio(bool useI2S, bool i2sMaster, uint32_t sampleRate, uint8_t sampleSizeBits);
  bool configureDabFrontend();
  bool setVolume(uint8_t level);
  bool setDabFreqList(const std::vector<uint32_t>& freqsKHz);
  bool dabTune(uint8_t freqIndex, uint16_t antcap = 0);
  DigiStatus dabDigradStatus();
  bool dabGetEventStatus(bool ack, bool clrAudio, uint8_t& evByte, uint8_t& audioStatus);
  bool getAudioServices(std::vector<DabService>& out);
  bool startDigitalService(uint32_t serviceId, uint32_t componentId);
  bool stopDigitalService(uint32_t serviceId, uint32_t componentId);
  bool setProperty(uint16_t propId, uint16_t value);

 private:
  SPISettings settings_{1'000'000, MSBFIRST, SPI_MODE0};

  void select() {
    SPI.beginTransaction(settings_);
    digitalWrite(PIN_CS, LOW);
  }

  void deselect() {
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
  }

  uint8_t readStatus();
  bool waitCts(uint32_t timeoutMs = 1000);
  bool sendCommand(const std::vector<uint8_t>& data);
  std::vector<uint8_t> readReply(size_t length);
  bool sendLoadInit();
  bool boot();
  bool hostLoadFile(const char* path);
  bool getServiceListPayload(std::vector<uint8_t>& payload);
  bool readServiceListSegment(uint16_t offset, uint8_t length, std::vector<uint8_t>& out);
};

bool Si468xRadio::begin() {
  SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
  pinMode(PIN_CS, OUTPUT);
  digitalWrite(PIN_CS, HIGH);
  pinMode(PIN_RST, OUTPUT);
  digitalWrite(PIN_RST, LOW);
  return true;
}

bool Si468xRadio::reset() {
  digitalWrite(PIN_RST, LOW);
  delay(10);
  digitalWrite(PIN_RST, HIGH);
  delay(200);
  return true;
}

uint8_t Si468xRadio::readStatus() {
  select();
  SPI.transfer(0x00);
  uint8_t status = SPI.transfer(0x00);
  deselect();
  return status;
}

bool Si468xRadio::waitCts(uint32_t timeoutMs) {
  uint32_t deadline = millis() + timeoutMs;
  while (millis() < deadline) {
    uint8_t status = readStatus();
    if (status & 0x80U) {
      if (status & 0x40U) {
        return false;
      }
      return true;
    }
    delay(1);
  }
  return false;
}

bool Si468xRadio::sendCommand(const std::vector<uint8_t>& data) {
  if (!waitCts()) {
    return false;
  }
  select();
  for (uint8_t b : data) {
    SPI.transfer(b);
  }
  deselect();
  return waitCts();
}

std::vector<uint8_t> Si468xRadio::readReply(size_t length) {
  std::vector<uint8_t> out(length, 0);
  select();
  SPI.transfer(0x00);
  for (size_t i = 0; i < length; ++i) {
    out[i] = SPI.transfer(0x00);
  }
  deselect();
  return out;
}

bool Si468xRadio::powerUp(uint32_t xtalHz, uint8_t ctun, uint8_t ibias, uint8_t ibiasRun) {
  std::vector<uint8_t> cmd(16, 0);
  cmd[0] = CMD_POWER_UP;
  cmd[2] = (1U << 4) | 0x07U;  // clk_mode=1, tr_size=7
  cmd[3] = ibias & 0x7FU;
  cmd[4] = static_cast<uint8_t>(xtalHz & 0xFF);
  cmd[5] = static_cast<uint8_t>((xtalHz >> 8) & 0xFF);
  cmd[6] = static_cast<uint8_t>((xtalHz >> 16) & 0xFF);
  cmd[7] = static_cast<uint8_t>((xtalHz >> 24) & 0xFF);
  cmd[8] = ctun & 0x3FU;
  cmd[9] = 0x10;
  cmd[13] = ibiasRun & 0x7FU;
  return sendCommand(cmd);
}

bool Si468xRadio::sendLoadInit() {
  return sendCommand({CMD_LOAD_INIT, 0x00});
}

bool Si468xRadio::boot() {
  return sendCommand({CMD_BOOT, 0x00});
}

bool Si468xRadio::hostLoadFile(const char* path) {
  File f = LittleFS.open(path, "r");
  if (!f) {
    Serial.printf("Cannot open %s\n", path);
    return false;
  }
  std::vector<uint8_t> buf(4 + 252, 0);
  buf[0] = CMD_HOST_LOAD;
  while (true) {
    size_t n = f.read(buf.data() + 4, 252);
    if (n == 0) {
      break;
    }
    buf[1] = buf[2] = buf[3] = 0x00;
    std::vector<uint8_t> frame(buf.begin(), buf.begin() + 4 + n);
    if (!sendCommand(frame)) {
      f.close();
      return false;
    }
  }
  f.close();
  return true;
}

bool Si468xRadio::loadPatchAndFirmware(const char* patchPath, const char* fwPath) {
  if (!sendLoadInit()) {
    return false;
  }
  if (!hostLoadFile(patchPath)) {
    return false;
  }
  delay(4);
  if (!sendLoadInit()) {
    return false;
  }
  if (!hostLoadFile(fwPath)) {
    return false;
  }
  return boot();
}

bool Si468xRadio::setProperty(uint16_t propId, uint16_t value) {
  std::vector<uint8_t> cmd = {
      CMD_SET_PROPERTY,
      0x00,
      static_cast<uint8_t>(propId & 0xFF),
      static_cast<uint8_t>((propId >> 8) & 0xFF),
      static_cast<uint8_t>(value & 0xFF),
      static_cast<uint8_t>((value >> 8) & 0xFF),
  };
  return sendCommand(cmd);
}

bool Si468xRadio::configureAudio(bool useI2S, bool i2sMaster, uint32_t sampleRate, uint8_t sampleSizeBits) {
  uint16_t pinCfg = 0x8001;  // DACOUTEN + keep defaults
  if (useI2S) {
    pinCfg |= 0x0002;
  }
  if (!setProperty(PROP_PIN_CONFIG_ENABLE, pinCfg)) {
    return false;
  }
  if (useI2S) {
    uint16_t outputSel = i2sMaster ? 0x8000 : 0x0000;
    if (!setProperty(PROP_DIGITAL_IO_OUTPUT_SELECT, outputSel)) {
      return false;
    }
    if (!setProperty(PROP_DIGITAL_IO_OUTPUT_SAMPLE_RATE, static_cast<uint16_t>(sampleRate))) {
      return false;
    }
    uint16_t fmt = static_cast<uint16_t>((sampleSizeBits & 0x3F) << 8);
    if (!setProperty(PROP_DIGITAL_IO_OUTPUT_FORMAT, fmt)) {
      return false;
    }
  }
  return true;
}

bool Si468xRadio::configureDabFrontend() {
  return setProperty(PROP_DAB_TUNE_FE_VARM, 0xFD12) &&
         setProperty(PROP_DAB_TUNE_FE_VARB, 0x009B) &&
         setProperty(PROP_DAB_TUNE_FE_CFG, 0x0000) &&
         setProperty(PROP_DAB_EVENT_INTERRUPT_SOURCE, 0x00C1) &&
         setProperty(PROP_DAB_VALID_RSSI_THRESHOLD, 6);
}

bool Si468xRadio::setVolume(uint8_t level) {
  level = (level > 63) ? 63 : level;
  return setProperty(PROP_AUDIO_ANALOG_VOLUME, level);
}

bool Si468xRadio::setDabFreqList(const std::vector<uint32_t>& freqsKHz) {
  if (freqsKHz.empty() || freqsKHz.size() > 75) {
    return false;
  }
  std::vector<uint8_t> cmd;
  cmd.reserve(4 + freqsKHz.size() * 4);
  cmd.push_back(CMD_DAB_SET_FREQ_LIST);
  cmd.push_back(static_cast<uint8_t>(freqsKHz.size() & 0xFF));
  cmd.push_back(0x00);
  cmd.push_back(0x00);
  for (uint32_t f : freqsKHz) {
    cmd.push_back(static_cast<uint8_t>(f & 0xFF));
    cmd.push_back(static_cast<uint8_t>((f >> 8) & 0xFF));
    cmd.push_back(static_cast<uint8_t>((f >> 16) & 0xFF));
    cmd.push_back(static_cast<uint8_t>((f >> 24) & 0xFF));
  }
  return sendCommand(cmd);
}

bool Si468xRadio::dabTune(uint8_t freqIndex, uint16_t antcap) {
  std::vector<uint8_t> cmd = {
      CMD_DAB_TUNE_FREQ,
      0x00,
      freqIndex,
      0x00,
      static_cast<uint8_t>(antcap & 0xFF),
      static_cast<uint8_t>((antcap >> 8) & 0xFF),
  };
  return sendCommand(cmd);
}

DigiStatus Si468xRadio::dabDigradStatus() {
  DigiStatus out;
  sendCommand({CMD_DAB_DIGRAD_STATUS, 0x00});
  auto reply = readReply(0x28);
  if (reply.size() < 0x28) {
    return out;
  }
  out.ficError = reply[5] & 0x08;
  out.acq = reply[5] & 0x04;
  out.valid = reply[5] & 0x01;
  out.rssi = signedByte(reply[6]);
  out.snr = reply[7];
  out.ficQuality = reply[8];
  out.cnr = reply[9];
  out.tuneFreqHz = static_cast<uint32_t>(reply[12]) | (static_cast<uint32_t>(reply[13]) << 8) |
                   (static_cast<uint32_t>(reply[14]) << 16) | (static_cast<uint32_t>(reply[15]) << 24);
  out.tuneIndex = reply[16];
  return out;
}

bool Si468xRadio::dabGetEventStatus(bool ack, bool clrAudio, uint8_t& evByte, uint8_t& audioStatus) {
  uint8_t flags = (ack ? 0x01 : 0x00) | (clrAudio ? 0x02 : 0x00);
  if (!sendCommand({CMD_DAB_GET_EVENT_STATUS, flags})) {
    return false;
  }
  auto reply = readReply(9);
  if (reply.size() < 9) {
    return false;
  }
  evByte = reply[5];
  audioStatus = reply[8];
  return true;
}

bool Si468xRadio::getServiceListPayload(std::vector<uint8_t>& payload) {
  if (!sendCommand({CMD_GET_DIGITAL_SERVICE_LIST, 0x00})) {
    return false;
  }
  auto header = readReply(6);
  if (header.size() < 6) {
    return false;
  }
  uint16_t totalSize = static_cast<uint16_t>(header[4]) | (static_cast<uint16_t>(header[5]) << 8);
  if (totalSize == 0) {
    payload.clear();
    return true;
  }
  auto full = readReply(6 + totalSize);
  if (full.size() >= 6 + totalSize) {
    payload.assign(full.begin() + 6, full.end());
  } else {
    payload.clear();
  }
  if (payload.size() >= totalSize) {
    return true;
  }
  payload.clear();
  uint16_t offset = 0;
  while (offset < totalSize) {
    uint8_t chunk = static_cast<uint8_t>(std::min<uint16_t>(252, totalSize - offset));
    std::vector<uint8_t> seg;
    if (!readServiceListSegment(offset, chunk, seg)) {
      return false;
    }
    payload.insert(payload.end(), seg.begin(), seg.end());
    offset = static_cast<uint16_t>(offset + chunk);
  }
  return payload.size() >= totalSize;
}

bool Si468xRadio::readServiceListSegment(uint16_t offset, uint8_t length, std::vector<uint8_t>& out) {
  std::vector<uint8_t> cmd = {
      CMD_READ_OFFSET,
      0x00,
      static_cast<uint8_t>(offset & 0xFF),
      static_cast<uint8_t>((offset >> 8) & 0xFF),
  };
  if (!sendCommand(cmd)) {
    return false;
  }
  auto reply = readReply(4 + length);
  if (reply.size() < 4 + length) {
    return false;
  }
  out.assign(reply.begin() + 4, reply.end());
  return true;
}

bool Si468xRadio::getAudioServices(std::vector<DabService>& out) {
  out.clear();
  std::vector<uint8_t> payload;
  if (!getServiceListPayload(payload)) {
    return false;
  }
  if (payload.size() < 6) {
    return true;
  }
  uint16_t serviceCount = static_cast<uint16_t>(payload[2]) | (static_cast<uint16_t>(payload[3]) << 8);
  size_t offset = 6;
  for (uint16_t i = 0; i < serviceCount; ++i) {
    if (offset + 24 > payload.size()) {
      break;
    }
    uint32_t sid = static_cast<uint32_t>(payload[offset]) |
                   (static_cast<uint32_t>(payload[offset + 1]) << 8) |
                   (static_cast<uint32_t>(payload[offset + 2]) << 16) |
                   (static_cast<uint32_t>(payload[offset + 3]) << 24);
    uint8_t info1 = payload[offset + 4];
    uint8_t info2 = payload[offset + 5];
    uint8_t info3 = payload[offset + 6];
    char labelBuf[17] = {0};
    memcpy(labelBuf, payload.data() + offset + 8, 16);
    String label = String(labelBuf);
    uint8_t numComponents = info2 & 0x0F;
    offset += 24;

    for (uint8_t c = 0; c < numComponents; ++c) {
      if (offset + 4 > payload.size()) {
        break;
      }
      uint16_t compId = static_cast<uint16_t>(payload[offset]) | (static_cast<uint16_t>(payload[offset + 1]) << 8);
      uint8_t compInfo = payload[offset + 2];
      uint8_t tmid = static_cast<uint8_t>((compId >> 14) & 0x03);
      uint8_t caflag = compInfo & 0x01;
      if (tmid == 0 && caflag == 0 && (info1 & 0x01) == 0) {
        DabService svc;
        svc.serviceId = sid;
        svc.componentId = compId;
        svc.label = label.length() ? label : String("SID 0x") + String(sid, HEX);
        svc.charset = info3 & 0x0F;
        out.push_back(svc);
      }
      offset += 4;
    }
  }
  return true;
}

bool Si468xRadio::startDigitalService(uint32_t serviceId, uint32_t componentId) {
  std::vector<uint8_t> cmd = {
      CMD_START_DIGITAL_SERVICE,
      0x00,
      0x00,
      0x00,
      static_cast<uint8_t>(serviceId & 0xFF),
      static_cast<uint8_t>((serviceId >> 8) & 0xFF),
      static_cast<uint8_t>((serviceId >> 16) & 0xFF),
      static_cast<uint8_t>((serviceId >> 24) & 0xFF),
      static_cast<uint8_t>(componentId & 0xFF),
      static_cast<uint8_t>((componentId >> 8) & 0xFF),
      static_cast<uint8_t>((componentId >> 16) & 0xFF),
      static_cast<uint8_t>((componentId >> 24) & 0xFF),
  };
  return sendCommand(cmd);
}

bool Si468xRadio::stopDigitalService(uint32_t serviceId, uint32_t componentId) {
  std::vector<uint8_t> cmd = {
      CMD_STOP_DIGITAL_SERVICE,
      0x00,
      0x00,
      0x00,
      static_cast<uint8_t>(serviceId & 0xFF),
      static_cast<uint8_t>((serviceId >> 8) & 0xFF),
      static_cast<uint8_t>((serviceId >> 16) & 0xFF),
      static_cast<uint8_t>((serviceId >> 24) & 0xFF),
      static_cast<uint8_t>(componentId & 0xFF),
      static_cast<uint8_t>((componentId >> 8) & 0xFF),
      static_cast<uint8_t>((componentId >> 16) & 0xFF),
      static_cast<uint8_t>((componentId >> 24) & 0xFF),
  };
  return sendCommand(cmd);
}

// ---------------------------------------------------------------------------
// Simple demo: scan Band III and start the first audio service found
// ---------------------------------------------------------------------------
Si468xRadio radio;
constexpr uint32_t XTAL_FREQ_HZ = 19'200'000;
constexpr uint8_t CTUN_WORD = 0x07;
constexpr bool USE_I2S = false;     // true to enable I2S out instead of analog DAC
constexpr bool I2S_MASTER = true;   // Si468x drives BCLK/LRCLK when true
constexpr uint32_t SAMPLE_RATE = 48'000;
constexpr uint8_t SAMPLE_SIZE_BITS = 16;

std::vector<DabService> g_services;
int g_currentService = -1;
uint8_t g_volume = 40;
std::vector<uint32_t> g_freqList;
bool g_useI2S = USE_I2S;
std::vector<uint16_t> g_antcapList = {0x00, 0x20, 0x40, 0x60};

static void ensureFreqList() {
  if (!g_freqList.empty()) {
    return;
  }
  g_freqList.reserve(DAB_BAND_SIZE);
  for (const auto& ch : DAB_BAND_III) {
    g_freqList.push_back(ch.freqKHz);
  }
}

static bool reinitRadio() {
  Serial.println("Reinit radio...");
  ensureFreqList();
  radio.reset();
  if (!radio.powerUp(XTAL_FREQ_HZ, CTUN_WORD)) {
    Serial.println("POWER_UP failed");
    return false;
  }
  if (!radio.loadPatchAndFirmware(PATH_PATCH, PATH_FW)) {
    Serial.println("Host load failed");
    return false;
  }
  if (!radio.configureAudio(g_useI2S, I2S_MASTER, SAMPLE_RATE, SAMPLE_SIZE_BITS)) {
    Serial.println("Audio config failed");
    return false;
  }
  Serial.printf("Audio mode: %s (sample %u Hz, %u bits)\n",
                g_useI2S ? "I2S" : "DAC", SAMPLE_RATE, SAMPLE_SIZE_BITS);
  radio.configureDabFrontend();
  radio.setVolume(g_volume);
  radio.setProperty(PROP_AUDIO_MUTE, 0);  // unmute defensively
  radio.setDabFreqList(g_freqList);
  return true;
}

static bool waitForServiceList() {
  uint8_t ev = 0, aud = 0;
  for (uint8_t i = 0; i < 50; ++i) {
    if (!radio.dabGetEventStatus(false, false, ev, aud)) {
      return false;
    }
    if (ev & 0x01U) {
      radio.dabGetEventStatus(true, false, ev, aud);
      return true;
    }
    delay(100);
  }
  return false;
}

static bool waitForLock(uint32_t lockMs, uint32_t statusIntervalMs, DigiStatus& outStatus) {
  uint32_t nextPrint = millis();
  uint32_t deadline = millis() + lockMs;
  while (millis() < deadline) {
    outStatus = radio.dabDigradStatus();
    if (outStatus.valid) {
      return true;
    }
    if (millis() >= nextPrint) {
      Serial.printf("  waiting lock... RSSI=%d SNR=%u FICQ=%u ACQ=%u VALID=%u\n", outStatus.rssi, outStatus.snr,
                    outStatus.ficQuality, outStatus.acq, outStatus.valid);
      nextPrint = millis() + statusIntervalMs;
    }
    delay(50);
  }
  return false;
}

static bool tuneWithAntcap(uint8_t freqIndex, DigiStatus& statusOut, uint16_t& usedAntcap) {
  for (uint16_t ant : g_antcapList) {
    if (!radio.dabTune(freqIndex, ant)) {
      continue;
    }
    if (waitForLock(8000, 400, statusOut)) {
      usedAntcap = ant;
      return true;
    }
  }
  return false;
}

static std::vector<DabService> fullScan() {
  std::vector<DabService> all;
  g_freqList.clear();
  g_freqList.reserve(DAB_BAND_SIZE);
  for (const auto& ch : DAB_BAND_III) {
    g_freqList.push_back(ch.freqKHz);
  }
  radio.setDabFreqList(g_freqList);

  Serial.println("Starting full DAB scan...");
  for (uint8_t idx = 0; idx < DAB_BAND_SIZE; ++idx) {
    Serial.printf("Tuning %s (%lu kHz)...\n", DAB_BAND_III[idx].label, DAB_BAND_III[idx].freqKHz);
    DigiStatus status;
    uint16_t usedAntcap = 0;
    if (!tuneWithAntcap(idx, status, usedAntcap)) {
      Serial.println("  no lock");
      continue;
    }
    if (!waitForServiceList()) {
      Serial.println("  service list not ready");
      continue;
    }
    std::vector<DabService> list;
    if (!radio.getAudioServices(list)) {
      Serial.println("  failed to read services");
      continue;
    }
    for (auto& svc : list) {
      svc.freqIndex = idx;
      svc.freqKHz = DAB_BAND_III[idx].freqKHz;
      all.push_back(svc);
      Serial.printf("  found service: %s SID=0x%08lX COMP=0x%04lX\n", svc.label.c_str(), svc.serviceId,
                    static_cast<unsigned long>(svc.componentId));
    }
  }
  return all;
}

static String sanitizeLabel(const String& in) {
  String out = in;
  out.trim();
  out.replace("\t", " ");
  return out;
}

static bool saveServices(const std::vector<DabService>& list, const char* path) {
  File f = LittleFS.open(path, "w");
  if (!f) {
    return false;
  }
  f.println("# sid_hex\tcomp_hex\tfreq_index\tfreq_khz\tlabel");
  for (const auto& svc : list) {
    String line;
    line.reserve(64);
    line += String(svc.serviceId, HEX);
    line += '\t';
    line += String(svc.componentId, HEX);
    line += '\t';
    line += String(svc.freqIndex);
    line += '\t';
    line += String(svc.freqKHz);
    line += '\t';
    line += sanitizeLabel(svc.label);
    f.println(line);
  }
  f.close();
  return true;
}

static bool loadServices(std::vector<DabService>& out, const char* path) {
  out.clear();
  File f = LittleFS.open(path, "r");
  if (!f) {
    return false;
  }
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() == 0 || line.startsWith("#")) {
      continue;
    }
    int p1 = line.indexOf('\t');
    int p2 = line.indexOf('\t', p1 + 1);
    int p3 = line.indexOf('\t', p2 + 1);
    int p4 = line.indexOf('\t', p3 + 1);
    if (p1 <= 0 || p2 <= p1 || p3 <= p2 || p4 <= p3) {
      continue;
    }
    String sidStr = line.substring(0, p1);
    String compStr = line.substring(p1 + 1, p2);
    String fiStr = line.substring(p2 + 1, p3);
    String fkStr = line.substring(p3 + 1, p4);
    String labelStr = line.substring(p4 + 1);

    char* endPtr = nullptr;
    uint32_t sid = strtoul(sidStr.c_str(), &endPtr, 16);
    uint32_t comp = strtoul(compStr.c_str(), &endPtr, 16);
    uint8_t fi = static_cast<uint8_t>(atoi(fiStr.c_str()));
    uint32_t fk = static_cast<uint32_t>(strtoul(fkStr.c_str(), &endPtr, 10));

    DabService svc;
    svc.serviceId = sid;
    svc.componentId = comp;
    svc.freqIndex = fi;
    svc.freqKHz = fk;
    svc.label = sanitizeLabel(labelStr);
    out.push_back(svc);
  }
  f.close();
  if (out.empty()) {
    return false;
  }
  // Rebuild frequency list from saved services and re-map indices
  g_freqList.clear();
  for (const auto& svc : out) {
    if (std::find(g_freqList.begin(), g_freqList.end(), svc.freqKHz) == g_freqList.end()) {
      g_freqList.push_back(svc.freqKHz);
    }
  }
  // Ensure chip freq list matches cache
  radio.setDabFreqList(g_freqList);
  // Remap freqIndex based on current list ordering
  for (auto& svc : out) {
    auto it = std::find(g_freqList.begin(), g_freqList.end(), svc.freqKHz);
    svc.freqIndex = (it == g_freqList.end()) ? 0 : static_cast<uint8_t>(std::distance(g_freqList.begin(), it));
  }
  return true;
}

static void printMenu() {
  Serial.println("\nCommandes:");
  Serial.println("  l          : liste des stations");
  Serial.println("  <index>    : jouer station #index");
  Serial.println("  + / -      : volume +/- 2");
  Serial.println("  v<0-63>    : volume direct");
  Serial.println("  s          : statut RF (digrad)");
  Serial.println("  a          : statut audio (event + mute)");
  Serial.println("  o          : basculer DAC/I2S et reconfigurer");
  Serial.println("  r          : refaire un scan complet");
  Serial.println("  m          : afficher mode audio courant");
  Serial.println("  u          : forcer unmute (AUDIO_MUTE=0)");
  Serial.println("  h          : afficher ce menu");
}

static void printServices(const std::vector<DabService>& list) {
  Serial.println("Stations:");
  for (size_t i = 0; i < list.size(); ++i) {
    const auto& s = list[i];
    Serial.printf("  [%u] %s  SID=0x%08lX COMP=0x%04lX FreqIdx=%u (%lu kHz)\n",
                  static_cast<unsigned>(i), s.label.c_str(),
                  static_cast<unsigned long>(s.serviceId),
                  static_cast<unsigned long>(s.componentId),
                  s.freqIndex, static_cast<unsigned long>(s.freqKHz));
  }
}

static void logStatus(const DigiStatus& st, const char* prefix) {
  Serial.printf("%s RSSI=%d SNR=%u FICQ=%u CNR=%u ACQ=%u VALID=%u tuneIdx=%u\n",
                prefix, st.rssi, st.snr, st.ficQuality, st.cnr, st.acq, st.valid, st.tuneIndex);
}

static void logEventStatus(bool ack) {
  uint8_t ev = 0, aud = 0;
  if (!radio.dabGetEventStatus(ack, false, ev, aud)) {
    Serial.println("GET_EVENT_STATUS failed");
    return;
  }
  Serial.printf("Event: svrlist=%u freqinfo=%u audio=%u mute=%u blkerr=%u blkloss=%u (ev=0x%02X aud=0x%02X)\n",
                static_cast<unsigned>(ev & 0x01), static_cast<unsigned>((ev >> 1) & 0x01),
                static_cast<unsigned>((ev >> 5) & 0x01), static_cast<unsigned>((aud >> 3) & 0x01),
                static_cast<unsigned>((aud >> 1) & 0x01), static_cast<unsigned>(aud & 0x01),
                ev, aud);
}

static bool waitAudioReady(uint32_t timeoutMs = 4000, bool kickAudio = false) {
  uint32_t deadline = millis() + timeoutMs;
  uint8_t failCount = 0;
  if (kickAudio) {
    uint8_t ev = 0, aud = 0;
    // One-time clear-audio to unmute/start the audio path if latched
    radio.dabGetEventStatus(true, true, ev, aud);
    Serial.printf("Kick audio: ev=0x%02X aud=0x%02X\n", ev, aud);
  }
  while (millis() < deadline) {
    uint8_t ev = 0, aud = 0;
    if (!radio.dabGetEventStatus(true, false, ev, aud)) {
      Serial.println("GET_EVENT_STATUS failed");
      if (++failCount >= 3) {
        return false;
      }
      delay(100);
      continue;
    }
    bool audio = (ev >> 5) & 0x01;
    bool mute = (aud >> 3) & 0x01;
    bool blkerr = (aud >> 1) & 0x01;
    bool blkloss = aud & 0x01;
    Serial.printf("Audio wait: audio=%u mute=%u blkerr=%u blkloss=%u ev=0x%02X aud=0x%02X\n",
                  audio, mute, blkerr, blkloss, ev, aud);
    if (audio && !mute && !blkerr && !blkloss) {
      return true;
    }
    delay(150);
  }
  return false;
}

static bool startServiceByIndex(int idx) {
  if (idx < 0 || static_cast<size_t>(idx) >= g_services.size()) {
    Serial.println("Index hors plage.");
    return false;
  }
  const auto& svc = g_services[static_cast<size_t>(idx)];
  Serial.printf("Selection: [%d] %s (freqIdx=%u freq=%lu kHz, audio=%s)\n", idx, svc.label.c_str(), svc.freqIndex,
                static_cast<unsigned long>(svc.freqKHz), g_useI2S ? "I2S" : "DAC");
  ensureFreqList();
  DigiStatus st;
  uint16_t usedAntcap = 0;
  if (!tuneWithAntcap(svc.freqIndex, st, usedAntcap)) {
    Serial.println("DAB_TUNE_FREQ a echoue, tentative de reinit radio...");
    if (!reinitRadio() || !tuneWithAntcap(svc.freqIndex, st, usedAntcap)) {
      Serial.println("Echec DAB_TUNE_FREQ");
      return false;
    }
  }
  Serial.printf("ANTCAP utilise: 0x%02X\n", usedAntcap & 0xFF);
  logStatus(st, "Lock:");
  if (g_currentService >= 0) {
    const auto& prev = g_services[static_cast<size_t>(g_currentService)];
    radio.stopDigitalService(prev.serviceId, prev.componentId);
  }
  radio.startDigitalService(svc.serviceId, svc.componentId);
  radio.setProperty(PROP_AUDIO_MUTE, 0);  // defensive unmute
  delay(50);
  logStatus(radio.dabDigradStatus(), "Post-start status:");
  logEventStatus(false);  // just show audio/mute flags without clearing audio
  if (!waitAudioReady(6000, true)) {
    Serial.println("Audio not ready (mute/blkloss?) after start. Retente start...");
    radio.stopDigitalService(svc.serviceId, svc.componentId);
    uint8_t ev = 0, aud = 0;
    radio.dabGetEventStatus(true, true, ev, aud);
    delay(50);
    radio.startDigitalService(svc.serviceId, svc.componentId);
    radio.setProperty(PROP_AUDIO_MUTE, 0);
    if (!waitAudioReady(6000, true)) {
      Serial.println("Audio toujours muet apres restart.");
    }
  }
  g_currentService = idx;
  return true;
}

static void handleSerial() {
  if (!Serial.available()) {
    return;
  }
  String cmd = Serial.readStringUntil('\n');
  cmd.trim();
  if (cmd.length() == 0) {
    return;
  }
  if (cmd == "h") {
    printMenu();
    return;
  }
  if (cmd == "l") {
    printServices(g_services);
    return;
  }
  if (cmd == "r") {
    Serial.println("Rescan en cours...");
    g_services = fullScan();
    saveServices(g_services, PATH_SCAN);
    printServices(g_services);
    return;
  }
  if (cmd == "s") {
    logStatus(radio.dabDigradStatus(), "Status:");
    return;
  }
  if (cmd == "a") {
    logEventStatus(true);
    return;
  }
  if (cmd == "m") {
    Serial.printf("Audio mode courant: %s\n", g_useI2S ? "I2S" : "DAC");
    return;
  }
  if (cmd == "o") {
    g_useI2S = !g_useI2S;
    Serial.printf("Bascule audio vers %s...\n", g_useI2S ? "I2S" : "DAC");
    if (!radio.configureAudio(g_useI2S, I2S_MASTER, SAMPLE_RATE, SAMPLE_SIZE_BITS)) {
      Serial.println("Echec configureAudio");
    } else {
      Serial.printf("Audio mode: %s (sample %u Hz, %u bits)\n",
                    g_useI2S ? "I2S" : "DAC", SAMPLE_RATE, SAMPLE_SIZE_BITS);
    }
    return;
  }
  if (cmd == "u") {
    radio.setProperty(PROP_AUDIO_MUTE, 0);
    Serial.println("AUDIO_MUTE=0 envoye.");
    return;
  }
  if (cmd == "o") {
    g_useI2S = !g_useI2S;
    Serial.printf("Bascule audio vers %s...\n", g_useI2S ? "I2S" : "DAC");
    if (!radio.configureAudio(g_useI2S, I2S_MASTER, SAMPLE_RATE, SAMPLE_SIZE_BITS)) {
      Serial.println("Echec configureAudio");
    } else {
      Serial.printf("Audio mode: %s (sample %u Hz, %u bits)\n",
                    g_useI2S ? "I2S" : "DAC", SAMPLE_RATE, SAMPLE_SIZE_BITS);
    }
    return;
  }
  if (cmd == "+") {
    g_volume = (g_volume >= 61) ? 63 : static_cast<uint8_t>(g_volume + 2);
    radio.setVolume(g_volume);
    Serial.printf("Volume %u/63\n", g_volume);
    return;
  }
  if (cmd == "-") {
    g_volume = (g_volume <= 2) ? 0 : static_cast<uint8_t>(g_volume - 2);
    radio.setVolume(g_volume);
    Serial.printf("Volume %u/63\n", g_volume);
    return;
  }
  if (cmd.startsWith("v")) {
    int v = cmd.substring(1).toInt();
    if (v < 0) v = 0;
    if (v > 63) v = 63;
    g_volume = static_cast<uint8_t>(v);
    radio.setVolume(g_volume);
    Serial.printf("Volume %u/63\n", g_volume);
    return;
  }
  if (cmd.charAt(0) >= '0' && cmd.charAt(0) <= '9') {
    int idx = cmd.toInt();
    startServiceByIndex(idx);
    return;
  }
  Serial.println("Commande inconnue (h pour aide).");
}

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("\nSi468x DAB (ESP32) bring-up");

  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS mount failed");
    return;
  }
  radio.begin();
  radio.reset();
  Serial.println("Powering up ROM...");
  if (!radio.powerUp(XTAL_FREQ_HZ, CTUN_WORD)) {
    Serial.println("POWER_UP failed");
    return;
  }
  Serial.println("Loading patch + firmware...");
  if (!radio.loadPatchAndFirmware(PATH_PATCH, PATH_FW)) {
    Serial.println("Host load failed");
    return;
  }
  Serial.println("Configuring audio + frontend...");
  if (!radio.configureAudio(g_useI2S, I2S_MASTER, SAMPLE_RATE, SAMPLE_SIZE_BITS)) {
    Serial.println("Audio config failed");
    return;
  }
  radio.configureDabFrontend();
  radio.setVolume(g_volume);

  if (loadServices(g_services, PATH_SCAN)) {
    Serial.printf("Stations chargees depuis %s (%u entrees)\n", PATH_SCAN, static_cast<unsigned>(g_services.size()));
  } else {
    Serial.println("Aucun cache; lancement d'un scan...");
    g_services = fullScan();
    saveServices(g_services, PATH_SCAN);
  }

  if (g_services.empty()) {
    Serial.println("No services found.");
  } else {
    printServices(g_services);
    Serial.printf("Demarrage '%s'...\n", g_services[0].label.c_str());
    startServiceByIndex(0);
    Serial.printf("Audio actif sur les sorties SI468x (%s).\n", g_useI2S ? "I2S" : "DAC");
  }
  printMenu();
}

void loop() {
  handleSerial();

}
