const state = {
  status: null,
  stations: [],
  favorites: [],
  recordings: [],
  recordingsSignature: "",
  loadedMode: null,
  filter: "",
  pollingHandle: null,
  volumeDebounce: null,
  scanProgressHandle: null,
  scanProgressWatcherHandle: null,
  scanProgressToken: 0,
  scanProgressSeenActive: false,
  scanProgressSyncing: false,
  backendScanActive: false,
  browserOutputPlaying: false,
  browserOutputStarting: false,
  browserOutputMessage: "",
};

function sleep(ms) {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await response.json();
  if (!payload.ok) {
    throw new Error(payload.error || "API error");
  }
  return payload.data;
}

async function apiWithTimeout(path, options = {}, timeoutMs = 2500) {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await api(path, {
      ...options,
      signal: controller.signal,
    });
  } finally {
    window.clearTimeout(timeout);
  }
}

function setBusy(button, busy, label) {
  if (!button) return;
  button.disabled = busy;
  if (busy && label) {
    button.dataset.originalLabel ||= button.textContent;
    button.textContent = label;
  } else if (!busy && button.dataset.originalLabel) {
    button.textContent = button.dataset.originalLabel;
    delete button.dataset.originalLabel;
  }
}

function setError(message = "") {
  document.getElementById("errorLine").textContent = message;
}

function sourceModeLabel(mode) {
  if (mode === "fmhd") return "FM / HD";
  if (mode === "amhd") return "AM / HD";
  if (mode === "dab") return "DAB";
  return String(mode || "radio").toUpperCase();
}

function setStatusMessage(message, badgeText = "Working", badgeClass = "status-ready") {
  document.getElementById("bootState").textContent = message;
  const badge = document.getElementById("statusBadge");
  badge.className = `status-badge ${badgeClass}`;
  badge.textContent = badgeText;
}

function setModeCardsDisabled(disabled) {
  document.querySelectorAll(".mode-card").forEach((button) => {
    button.disabled = Boolean(disabled);
  });
}

function scanProgressSummary(progress = {}) {
  const current = Number(progress.current || 0);
  const total = Number(progress.total || 0);
  const found = Number(progress.found || 0);
  const hdFound = Number(progress.hd_found || 0);
  const stage = String(progress.stage || "");
  const parts = [];
  if (stage === "hd_probe") {
    if (total > 0) parts.push(`HD probe ${Math.min(current, total)}/${total}`);
    parts.push(`FM ${found} found`);
    parts.push(`HD ${hdFound}`);
    return parts.join(" | ");
  }
  if (stage === "fm_scan") {
    if (total > 0) parts.push(`FM ${Math.min(current, total)}/${total}`);
    parts.push(`${found} found`);
    return parts.join(" | ");
  }
  if (stage === "am_hd_probe") {
    if (total > 0) parts.push(`AM HD probe ${Math.min(current, total)}/${total}`);
    parts.push(`AM ${found} found`);
    parts.push(`HD ${hdFound}`);
    return parts.join(" | ");
  }
  if (stage === "am_scan" || stage === "am_confirm") {
    if (total > 0) parts.push(`AM ${Math.min(current, total)}/${total}`);
    parts.push(`${found} found`);
    return parts.join(" | ");
  }
  if (stage === "dab_scan") {
    if (total > 0) parts.push(`DAB ${Math.min(current, total)}/${total}`);
    parts.push(`${found} found`);
    return parts.join(" | ");
  }
  if (hdFound > 0) parts.push(`HD ${hdFound}`);
  if (found > 0) parts.push(`${found} found`);
  if (total > 0) parts.push(`${Math.min(current, total)}/${total}`);
  return parts.join(" | ") || "Working";
}

function renderScanProgress(progress = {}, fallbackMode = state.status?.mode || "dab") {
  const mode = progress.mode || fallbackMode;
  const label = progress.message || `${sourceModeLabel(mode)} scan in progress.`;
  let percent = Number(progress.percent || 0);
  if (progress.active && percent <= 0) {
    percent = 1;
  }
  percent = Math.max(0, Math.min(100, Math.round(percent)));
  document.getElementById("scanProgressLabel").textContent = label;
  document.getElementById("scanProgressTime").textContent = scanProgressSummary(progress);
  document.getElementById("scanProgressFill").style.width = `${percent}%`;
  const track = document.getElementById("scanProgressTrack");
  track.setAttribute("aria-valuenow", String(percent));
  track.setAttribute("aria-label", `${sourceModeLabel(mode)} scan progress`);
}

async function refreshScanProgress(token, mode) {
  if (token !== state.scanProgressToken) return;
  try {
    const progress = await apiWithTimeout("/api/scan-progress", { cache: "no-store" }, 1800);
    if (token !== state.scanProgressToken) return;
    renderScanProgress(progress, mode);
  } catch (error) {
    if (token !== state.scanProgressToken) return;
    renderScanProgress(
      {
        active: true,
        mode,
        message: `Waiting for ${sourceModeLabel(mode)} scan progress...`,
        percent: 1,
      },
      mode,
    );
  }
}

function startScanProgress(mode) {
  stopScanProgress({ hide: true });
  state.scanProgressToken += 1;
  const token = state.scanProgressToken;
  state.scanProgressSeenActive = true;
  state.backendScanActive = true;
  document.getElementById("scanProgress").hidden = false;
  renderScanProgress(
    {
      active: true,
      mode,
      message: `Starting ${sourceModeLabel(mode)} scan...`,
      percent: 1,
    },
    mode,
  );
  refreshScanProgress(token, mode);
  state.scanProgressHandle = window.setInterval(() => refreshScanProgress(token, mode), 500);
}

function stopScanProgress({ completed = false, failed = false, hide = false, message = "", count = null } = {}) {
  if (state.scanProgressHandle) {
    window.clearInterval(state.scanProgressHandle);
    state.scanProgressHandle = null;
  }
  if (hide) {
    document.getElementById("scanProgress").hidden = true;
    return;
  }
  const token = state.scanProgressToken;
  const progress = document.getElementById("scanProgress");
  if (completed) {
    const stationCount = count === null ? null : Number(count);
    renderScanProgress({
      active: false,
      mode: state.status?.mode,
      stage: "complete",
      message: message || "Scan complete.",
      current: Number.isFinite(stationCount) ? stationCount : 1,
      total: Number.isFinite(stationCount) ? stationCount : 1,
      found: Number.isFinite(stationCount) ? stationCount : 0,
      percent: 100,
    });
  } else if (failed) {
    renderScanProgress({
      active: false,
      mode: state.status?.mode,
      stage: "failed",
      message: message || "Scan failed.",
      percent: 100,
    });
  }
  window.setTimeout(() => {
    if (token === state.scanProgressToken) {
      progress.hidden = true;
    }
  }, completed ? 1800 : 3500);
}

async function refreshAfterBackendScan(progress = {}) {
  const mode = progress.mode || state.status?.mode;
  await refreshStatus();
  await refreshStations(mode);
  await refreshFavorites();
}

async function syncBackendScanProgress() {
  if (state.scanProgressSyncing) return;
  state.scanProgressSyncing = true;
  try {
    const progress = await api("/api/scan-progress", { cache: "no-store" });
    const active = Boolean(progress.active);
    const mode = progress.mode || state.status?.mode || "dab";
    const scanButton = document.getElementById("scanButton");

    if (active) {
      state.scanProgressSeenActive = true;
      state.backendScanActive = true;
      document.getElementById("scanProgress").hidden = false;
      renderScanProgress(progress, mode);
      setBusy(scanButton, true, "Scanning...");
      setModeCardsDisabled(true);
      document.getElementById("scanMeta").textContent = "Scanning...";
      setStatusMessage(progress.message || `Scanning ${sourceModeLabel(mode)} stations...`, "Scanning");
      return;
    }

    state.backendScanActive = false;
    if (!state.scanProgressSeenActive) return;
    state.scanProgressSeenActive = false;
    setBusy(scanButton, false);
    setModeCardsDisabled(false);
    renderScanProgress(progress, mode);
    if (progress.error) {
      setStatusMessage(`Scan failed for ${sourceModeLabel(mode)}.`, "Error", "status-idle");
      setError(progress.error);
    } else {
      setStatusMessage(`${sourceModeLabel(mode)} backend ready.`, "Ready");
      setError("");
      await refreshAfterBackendScan(progress);
    }
    window.setTimeout(() => {
      if (!state.backendScanActive) {
        document.getElementById("scanProgress").hidden = true;
      }
    }, progress.error ? 3500 : 1800);
  } catch (error) {
    if (state.backendScanActive) {
      setError(`Waiting for scan progress: ${error.message}`);
    }
  } finally {
    state.scanProgressSyncing = false;
  }
}

function startBackendScanWatcher() {
  if (state.scanProgressWatcherHandle) {
    window.clearInterval(state.scanProgressWatcherHandle);
  }
  state.scanProgressWatcherHandle = window.setInterval(syncBackendScanProgress, 1000);
}

function setBrowserPlayFallback(message) {
  state.browserOutputPlaying = false;
  state.browserOutputMessage = `Browser output is enabled, but playback is paused. Click Browser output again or allow audio playback in this browser. ${message}`;
  if (browserOutputSupported(state.status || {})) {
    updateAudioOutputUi(state.status || {});
  }
  setError(state.browserOutputMessage);
}

function formatFrequency(station) {
  const freq = Number(station.freq_khz || 0);
  if (station.band === "fm") {
    return `${(freq / 1000).toFixed(1)} MHz`;
  }
  return freq > 0 ? `${freq} kHz` : "freq ?";
}

function formatTimestamp(value) {
  if (!value) return "Unknown time";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function updateDabMedia(status) {
  const media = status.radio_media || status.dab_media || {};
  const current = status.current_station || {};
  const source = media.source || (status.mode === "dab" ? "dab" : current.hd_available ? "hd" : "none");
  const isDab = source === "dab";
  const isHd = source === "hd";
  const isRds = source === "rds";
  const isActive = isDab || isHd || isRds;
  const sourceLabel = isHd ? "HD Radio" : isDab ? "DAB" : isRds ? "FM RDS" : "Radio";
  const hasText = Boolean(media.text || media.artist || media.title || media.station_name);
  const hasArtwork = Boolean(media.artwork_url);
  const mediaTimestamp = media.artwork_updated_at || media.updated_at;

  document.getElementById("mediaArtist").textContent =
    media.artist || (!isRds && media.station_name) || (isRds ? "No artist in RDS data" : isActive ? "No artist yet" : "Metadata inactive");
  document.getElementById("mediaTitle").textContent = media.title || (isActive ? "No title yet" : "Metadata inactive");
  document.getElementById("mediaText").textContent = isActive
    ? media.text || media.station_name || `No ${sourceLabel} text received yet.`
    : "Tune a DAB, FM RDS or HD Radio station to read metadata.";
  document.getElementById("mediaUpdated").textContent = mediaTimestamp
    ? `Updated: ${formatTimestamp(mediaTimestamp)}`
    : (isActive ? "No metadata received yet." : "Radio metadata is inactive.");
  document.getElementById("mediaHint").textContent = isActive
    ? hasArtwork
      ? `${sourceLabel} artwork received from the current station.`
      : isHd
        ? "Waiting for HD Radio image data if this station broadcasts it."
        : isRds
          ? "FM RDS carries text metadata but no artwork."
          : "Waiting for slideshow image from the current DAB station."
    : "Text metadata is available on FM RDS, DAB and HD Radio when broadcast.";

  const statusPill = document.getElementById("mediaStatus");
  statusPill.textContent = !isActive
    ? "Inactive"
    : hasArtwork && hasText
      ? `${sourceLabel} art + text`
      : hasArtwork
        ? `${sourceLabel} art`
        : hasText
          ? `${sourceLabel} text`
          : "Waiting";

  const artwork = document.getElementById("mediaArtwork");
  const fallback = document.getElementById("mediaArtworkFallback");
  if (hasArtwork) {
    artwork.hidden = false;
    artwork.src = media.artwork_url;
    fallback.hidden = true;
  } else {
    artwork.hidden = true;
    artwork.removeAttribute("src");
    fallback.hidden = false;
    fallback.textContent = (media.program || (isRds ? "RDS" : isHd ? "HD" : current.label || "DAB")).slice(0, 4).toUpperCase();
  }
}

function renderModes() {
  const currentMode = state.status?.mode;
  document.querySelectorAll(".mode-card").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.mode === currentMode);
  });
}

function audioOutputLabel(mode) {
  if (mode === "analog") return "Analog";
  if (mode === "i2s") return "Browser";
  if (mode === "both") return "Both";
  return "Unknown";
}

function browserOutputSupported(status) {
  const audioOut = String(status?.audio_out || "").toLowerCase();
  return audioOut === "i2s" || audioOut === "both";
}

function clampVolumeLevel(value) {
  const level = Number(value);
  if (!Number.isFinite(level)) return 0;
  return Math.max(0, Math.min(63, Math.round(level)));
}

function browserVolumeFromLevel(level) {
  return clampVolumeLevel(level) / 63;
}

function updateVolumeReadout(level) {
  const normalized = clampVolumeLevel(level);
  document.getElementById("volumeLabel").textContent = `${normalized} / 63`;
  document.getElementById("volumeSlider").value = normalized;
}

function applyBrowserPlayerVolume(level, muted = state.status?.muted) {
  const player = document.getElementById("browserAudioPlayer");
  if (!player) return;
  player.volume = browserVolumeFromLevel(level);
  player.muted = Boolean(muted);
}

function streamUrlWithCacheBust(path) {
  const separator = String(path || "").includes("?") ? "&" : "?";
  return `${path}${separator}ts=${Date.now()}`;
}

function stopBrowserAudio({ clearSource = true } = {}) {
  const player = document.getElementById("browserAudioPlayer");
  if (!player) return;
  player.pause();
  player.loop = false;
  if (clearSource) {
    player.removeAttribute("src");
    player.load();
  }
  state.browserOutputPlaying = false;
  state.browserOutputMessage = "";
}

async function startBrowserAudio({ forceNewStream = false } = {}) {
  const player = document.getElementById("browserAudioPlayer");
  if (!player) return;
  const streamPath = state.status?.live_stream?.path || "/audio/live.wav";
  if (!state.status?.current_station) {
    setError("Tune a station before starting browser output.");
    return;
  }
  player.loop = false;
  applyBrowserPlayerVolume(state.status?.volume ?? 0, state.status?.muted);
  if (forceNewStream || !player.getAttribute("src")) {
    player.src = streamUrlWithCacheBust(streamPath);
    player.load();
  }
  try {
    const playback = player.play();
    await Promise.race([
      playback,
      sleep(15000).then(() => {
        throw new Error("Playback did not start automatically.");
      }),
    ]);
    await sleep(250);
    if (player.paused) {
      throw new Error("The browser kept the player paused.");
    }
    state.browserOutputPlaying = true;
    state.browserOutputMessage = "";
    setError("");
  } catch (error) {
    setBrowserPlayFallback(error.message);
  }
}

function scheduleBrowserPlaybackWatchdog() {
  const delays = [600, 1800, 3600];
  delays.forEach((delay, index) => {
    window.setTimeout(async () => {
      const player = document.getElementById("browserAudioPlayer");
      if (!player || !browserOutputSupported(state.status || {}) || !player.getAttribute("src") || !player.paused) {
        return;
      }
      try {
        await player.play();
        await sleep(250);
        if (!player.paused) {
          state.browserOutputPlaying = true;
          setError("");
          updateAudioOutputUi(state.status || {});
          return;
        }
      } catch (error) {
        if (index === delays.length - 1) {
          setBrowserPlayFallback(error.message);
        }
        return;
      }
      if (index === delays.length - 1) {
        setBrowserPlayFallback("Playback is still paused.");
      }
    }, delay);
  });
}

function i2sSetupText(setup) {
  const configPath = setup?.config_path || "/boot/firmware/config.txt";
  const lines = (setup?.required_lines || []).join(" and ");
  if (!setup?.arecord_available) {
    return "Browser output and recording need arecord. Install alsa-utils, then reload this page.";
  }
  if (setup?.config_ready && !setup?.installed) {
    return `The I2S overlay is already in ${configPath}, but the capture device is not active yet. Reboot the Raspberry Pi.`;
  }
  return `Add ${lines} to ${configPath}. This modifies the boot config, enables start with the system, and requires a reboot.`;
}

function spiSetupText(setup) {
  const configPath = setup?.config_path || "/boot/firmware/config.txt";
  const lines = (setup?.required_lines || []).join(" and ") || "dtparam=spi=on";
  if (setup?.config_ready && !setup?.enabled) {
    return `SPI is already enabled in ${configPath}, but /dev/spidev0.* is not active yet. Reboot the Raspberry Pi.`;
  }
  return `Add ${lines} to ${configPath}. This modifies the boot config and requires a Raspberry Pi reboot.`;
}

function updateSpiSetupUi(status) {
  const setup = status?.spi_setup || {};
  const card = document.getElementById("spiSetupCard");
  const text = document.getElementById("spiSetupText");
  const button = document.getElementById("spiInstallButton");
  if (!card || !text || !button) return;
  const showSetup = Boolean(setup && !setup.enabled);
  card.hidden = !showSetup;
  if (!showSetup) return;
  text.textContent = setup.message ? `${setup.message} ${spiSetupText(setup)}` : spiSetupText(setup);
  button.disabled = setup.config_ready || setup.install_available === false;
  button.textContent = setup.config_ready ? "Reboot required" : "Enable SPI";
}

function updateI2sSetupUi(liveStream) {
  const setup = liveStream?.i2s_setup || {};
  const card = document.getElementById("i2sSetupCard");
  const text = document.getElementById("i2sSetupText");
  const button = document.getElementById("i2sInstallButton");
  if (!card || !text || !button) return;
  const showSetup = Boolean(liveStream && !liveStream.supported);
  card.hidden = !showSetup;
  if (!showSetup) return;
  text.textContent = i2sSetupText(setup);
  button.disabled = setup.config_ready || setup.install_available === false;
  button.textContent = setup.config_ready ? "Reboot required" : "Install I2S capture config";
}

function updateAudioOutputUi(status) {
  const audioOut = String(status.audio_out || "both").toLowerCase();
  const outputState = document.getElementById("browserOutputState");
  const analogButton = document.getElementById("analogOutputButton");
  const browserButton = document.getElementById("browserOutputButton");
  const hint = document.getElementById("browserOutputHint");
  const player = document.getElementById("browserAudioPlayer");
  const liveStream = status.live_stream || {};
  const browserReady = Boolean(liveStream.supported && liveStream.ready);
  const browserHardware = browserOutputSupported(status);
  updateI2sSetupUi(liveStream);

  outputState.textContent = audioOutputLabel(audioOut);
  analogButton.classList.toggle("is-on", audioOut === "analog");
  browserButton.classList.toggle("is-on", browserHardware);
  browserButton.disabled = !liveStream.supported;
  player.classList.toggle("is-ready", browserHardware);

  if (audioOut === "analog" && !state.browserOutputStarting) {
    stopBrowserAudio();
  }

  if (!liveStream.supported) {
    hint.textContent = liveStream.i2s_setup?.message || "Browser output needs the Raspberry Pi I2S capture device and arecord.";
  } else if (!browserReady) {
    hint.textContent = "Tune a station, then choose Browser output to listen from this page.";
  } else if (browserHardware) {
    hint.textContent = state.browserOutputPlaying
      ? "Browser output is playing the local PCM WAV stream from the SI4689 I2S capture."
      : "Browser output is enabled. If playback is paused, click Browser output again.";
  } else {
    hint.textContent = "Analog output uses the onboard DAC, jack and amplifier.";
  }
}

function updateStatus(status, { preserveError = false } = {}) {
  state.status = status;
  const current = status.current_station || {};
  const signal = status.signal || {};
  const recording = status.recording || { active: false };
  const oled = status.oled || {};
  const oledRequested = oled.requested ?? oled.enabled;
  const systemService = status.system_service || {};

  document.getElementById("signalScore").textContent = signal.score ?? 0;
  document.getElementById("currentStation").textContent = current.label || "No station";
  document.getElementById("currentMode").textContent = status.mode_label || status.mode || "DAB";
  document.getElementById("firmwareLabel").textContent = "SPI host-load";
  document.getElementById("rssiValue").textContent = signal.rssi ?? "-";
  document.getElementById("snrValue").textContent = signal.snr ?? "-";
  document.getElementById("ficqValue").textContent = signal.fic_quality ?? "-";
  document.getElementById("cnrValue").textContent = signal.cnr ?? "-";
  updateVolumeReadout(status.volume ?? 0);
  applyBrowserPlayerVolume(status.volume ?? 0, status.muted);
  updateSpiSetupUi(status);
  document.getElementById("bootState").textContent = status.booted
    ? `${status.mode_label} backend ready.`
    : "Backend is not initialized.";
  document.getElementById("audioOutLabel").textContent = `Audio out: ${audioOutputLabel(status.audio_out || "both")}`;
  updateAudioOutputUi(status);
  document.getElementById("scanMeta").textContent = status.last_scan_time
    ? `Last scan: ${formatTimestamp(status.last_scan_time)}`
    : "No scan yet";
  document.getElementById("stationCount").textContent = state.stations.length;
  document.getElementById("favoriteCount").textContent = state.favorites.length;
  document.getElementById("recordingCount").textContent = status.recordings_count ?? state.recordings.length;
  document.getElementById("oledToggle").checked = Boolean(oledRequested);
  document.getElementById("oledStatusText").textContent = oledRequested
    ? oled.error
      ? `Screen requested on I2C bus ${oled.i2c_bus}. Last OLED error: ${oled.error}`
      : `Screen enabled on I2C bus ${oled.i2c_bus}, address 0x${Number(oled.i2c_addr || 0).toString(16)}.`
    : `Screen disabled. I2C bus ${oled.i2c_bus} is free for other accessories.`;
  document.getElementById("systemAutostartToggle").checked = Boolean(systemService.enabled);
  document.getElementById("systemAutostartText").textContent = systemService.error
    ? `Autostart status for ${systemService.service || "service"} is unavailable: ${systemService.error}`
    : systemService.enabled
      ? `${systemService.service || "raspiaudio-radio.service"} is enabled for the next Raspberry Pi boot.`
      : `${systemService.service || "raspiaudio-radio.service"} is disabled for the next Raspberry Pi boot.`;

  const ampButton = document.getElementById("ampButton");
  ampButton.textContent = status.amp_enabled ? "Amplifier on" : "Amplifier off";
  ampButton.classList.toggle("is-on", Boolean(status.amp_enabled));

  const muteButton = document.getElementById("muteButton");
  muteButton.textContent = status.muted ? "Muted" : "Mute";
  muteButton.classList.toggle("is-on", Boolean(status.muted));

  const recordButton = document.getElementById("recordButton");
  recordButton.textContent = recording.active ? "STOP" : "REC";
  recordButton.title = recording.active ? "Stop recording" : "Start recording";
  recordButton.setAttribute("aria-label", recording.active ? "Stop recording" : "Start recording");
  recordButton.classList.toggle("is-recording", Boolean(recording.active));

  const badge = document.getElementById("statusBadge");
  badge.className = "status-badge";
  if (!status.booted) {
    badge.classList.add("status-idle");
    badge.textContent = "Idle";
  } else if (recording.active) {
    badge.classList.add("status-live");
    badge.textContent = "Recording";
  } else if (current.label) {
    badge.classList.add("status-live");
    badge.textContent = "Live";
  } else {
    badge.classList.add("status-ready");
    badge.textContent = "Ready";
  }

  if (!preserveError) {
    setError(state.browserOutputMessage || status.last_error || "");
  }
  if (current.station_id) {
    state.stations = state.stations.map((station) =>
      station.station_id === current.station_id ? { ...station, ...current } : station
    );
  }
  updateDabMedia(status);
  renderModes();
  renderStations();
  renderFavorites();
}

function renderStations() {
  const list = document.getElementById("stationList");
  const template = document.getElementById("stationTemplate");
  list.innerHTML = "";

  const filter = state.filter.trim().toLowerCase();
  const filtered = state.stations.filter((station) => {
    if (!filter) return true;
    return `${station.label} ${station.freq_khz || ""} ${station.mode_label || ""}`
      .toLowerCase()
      .includes(filter);
  });

  document.getElementById("stationCount").textContent = filtered.length;

  if (!filtered.length) {
    const empty = document.createElement("div");
    empty.className = "station-empty";
    empty.textContent = "No station loaded for this mode. Run a scan.";
    list.appendChild(empty);
    return;
  }

  filtered.forEach((station) => {
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.toggle("is-active", Boolean(station.is_current));
    renderStationName(node.querySelector(".station-name"), station);
    node.querySelector(".station-meta").textContent = `${formatFrequency(station)} | ${station.mode_label}`;

    const playButton = node.querySelector(".station-main");
    playButton.addEventListener("click", () => playStation(station.station_id));

    const favoriteButton = node.querySelector(".station-favorite");
    favoriteButton.textContent = station.favorite ? "Saved" : "Save";
    favoriteButton.classList.toggle("is-favorite", Boolean(station.favorite));
    favoriteButton.addEventListener("click", async (event) => {
      event.stopPropagation();
      await toggleFavorite(station.station_id, !station.favorite);
    });

    list.appendChild(node);
  });
}

function renderStationName(target, station) {
  target.textContent = station.label;
  if (!station.hd_available) return;
  const badge = document.createElement("span");
  badge.className = "station-badge station-badge-hd";
  badge.textContent = station.program_label || "HD";
  target.appendChild(badge);
}

function renderFavorites() {
  const list = document.getElementById("favoriteList");
  const template = document.getElementById("favoriteTemplate");
  list.innerHTML = "";

  if (!state.favorites.length) {
    const empty = document.createElement("div");
    empty.className = "station-empty";
    empty.textContent = "Favorite stations will appear here.";
    list.appendChild(empty);
    return;
  }

  state.favorites.forEach((station) => {
    const node = template.content.firstElementChild.cloneNode(true);
    renderStationName(node.querySelector(".favorite-label"), station);
    node.querySelector(".favorite-mode").textContent = `${station.mode_label} | ${formatFrequency(station)}`;
    node.addEventListener("click", () => playStation(station.station_id));
    list.appendChild(node);
  });
}

function renderRecordings() {
  const list = document.getElementById("recordingList");
  const template = document.getElementById("recordingTemplate");
  list.innerHTML = "";

  if (!state.recordings.length) {
    const empty = document.createElement("div");
    empty.className = "station-empty";
    empty.textContent = "No recording yet.";
    list.appendChild(empty);
    return;
  }

  state.recordings.forEach((recording) => {
    const node = template.content.firstElementChild.cloneNode(true);
    node.classList.toggle("is-recording", Boolean(recording.active));
    node.querySelector(".recording-station").textContent = recording.station_label || recording.file_name;
    node.querySelector(".recording-meta").textContent =
      `${formatTimestamp(recording.started_at)} | ${recording.mode || "audio"}${recording.active ? " | recording" : ""}`;
    const link = node.querySelector(".recording-link");
    link.href = recording.url;
    const player = node.querySelector(".recording-player");
    player.src = recording.url;
    list.appendChild(node);
  });
}

function recordingsSignature(recordings) {
  return JSON.stringify(
    (recordings || []).map((recording) => ({
      file_name: recording.file_name,
      active: Boolean(recording.active),
      started_at: recording.started_at,
      url: recording.url,
    })),
  );
}

async function refreshStatus(options = {}) {
  try {
    const status = await api("/api/status");
    const modeChanged = state.loadedMode !== status.mode;
    updateStatus(status, options);
    if (modeChanged) {
      await refreshStations(status.mode);
    }
  } catch (error) {
    setError(error.message);
  }
}

async function refreshStations(mode = state.status?.mode) {
  try {
    const data = await api(`/api/stations${mode ? `?mode=${mode}` : ""}`);
    state.stations = data.stations || [];
    state.loadedMode = mode || null;
    renderStations();
  } catch (error) {
    setError(error.message);
  }
}

async function refreshFavorites() {
  try {
    const data = await api("/api/favorites");
    state.favorites = data.stations || [];
    renderFavorites();
  } catch (error) {
    setError(error.message);
  }
}

async function refreshRecordings() {
  try {
    const data = await api("/api/recordings");
    const recordings = data.recordings || [];
    const signature = recordingsSignature(recordings);
    state.recordings = recordings;
    if (signature !== state.recordingsSignature) {
      state.recordingsSignature = signature;
      renderRecordings();
    }
    document.getElementById("recordingCount").textContent = recordings.length;
  } catch (error) {
    setError(error.message);
  }
}

async function refreshAll() {
  await refreshStatus();
  await refreshFavorites();
  await refreshRecordings();
}

async function setMode(mode) {
  const scanButton = document.getElementById("scanButton");
  setModeCardsDisabled(true);
  setBusy(scanButton, true, "Loading...");
  setStatusMessage(`Loading ${sourceModeLabel(mode)} firmware...`, "Loading");
  setError("");
  try {
    const status = await api("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    updateStatus(status);
    await refreshStations(mode);
  } catch (error) {
    setStatusMessage(`Failed to load ${sourceModeLabel(mode)} firmware.`, "Error", "status-idle");
    setError(error.message);
  } finally {
    setBusy(scanButton, false);
    setModeCardsDisabled(false);
  }
}

async function scanStations() {
  const button = document.getElementById("scanButton");
  setBusy(button, true, "Scanning...");
  setModeCardsDisabled(true);
  const scanMode = state.status?.mode || "dab";
  const label = sourceModeLabel(scanMode);
  setStatusMessage(`Scanning ${label} stations. Loading firmware if needed...`, "Scanning");
  document.getElementById("scanMeta").textContent = "Scanning...";
  startScanProgress(scanMode);
  setError("");
  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ force: true }),
    });
    state.stations = data.stations || [];
    renderStations();
    await refreshStatus();
    await refreshFavorites();
    stopScanProgress({
      completed: true,
      message: `Scan complete: ${data.count || 0} stations found.`,
      count: data.count || 0,
    });
  } catch (error) {
    setStatusMessage(`Scan failed for ${label}.`, "Error", "status-idle");
    stopScanProgress({ failed: true, message: `Scan failed for ${label}.` });
    setError(error.message);
  } finally {
    setBusy(button, false);
    setModeCardsDisabled(false);
  }
}

async function playStation(stationId) {
  const keepBrowserOutput = browserOutputSupported(state.status || {});
  try {
    if (keepBrowserOutput) {
      stopBrowserAudio();
    }
    const status = await api("/api/play", {
      method: "POST",
      body: JSON.stringify({ station_id: stationId }),
    });
    updateStatus(status);
    await refreshStations(status.mode);
    await refreshFavorites();
    if (keepBrowserOutput) {
      const outputStatus = await api("/api/audio-output", {
        method: "POST",
        body: JSON.stringify({ mode: "browser" }),
      });
      updateStatus(outputStatus, { preserveError: true });
      await startBrowserAudio({ forceNewStream: true });
      await refreshStatus({ preserveError: true });
    }
  } catch (error) {
    setError(error.message);
  }
}

async function toggleFavorite(stationId, favorite) {
  try {
    await api("/api/favorite", {
      method: "POST",
      body: JSON.stringify({ station_id: stationId, favorite }),
    });
    await refreshStations(state.status?.mode);
    await refreshFavorites();
    await refreshStatus();
  } catch (error) {
    setError(error.message);
  }
}

async function updateVolume(payload) {
  if (payload && Object.prototype.hasOwnProperty.call(payload, "level")) {
    updateVolumeReadout(payload.level);
    applyBrowserPlayerVolume(payload.level, state.status?.muted);
  }
  try {
    const status = await api("/api/volume", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    updateStatus(status);
  } catch (error) {
    setError(error.message);
  }
}

async function toggleAmplifier() {
  try {
    const enabled = !state.status?.amp_enabled;
    const status = await api("/api/amplifier", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    updateStatus(status);
  } catch (error) {
    setError(error.message);
  }
}

async function toggleMute() {
  try {
    const status = await api("/api/mute", {
      method: "POST",
      body: JSON.stringify({}),
    });
    updateStatus(status);
  } catch (error) {
    setError(error.message);
  }
}

async function setAudioOutput(mode) {
  const analogButton = document.getElementById("analogOutputButton");
  const browserButton = document.getElementById("browserOutputButton");
  const activeButton = mode === "analog" ? analogButton : browserButton;
  setBusy(activeButton, true, mode === "analog" ? "Switching..." : "Starting...");
  try {
    if (mode === "browser") {
      state.browserOutputStarting = true;
      await startBrowserAudio({ forceNewStream: true });
      const player = document.getElementById("browserAudioPlayer");
      if (player?.paused) {
        setBrowserPlayFallback("The browser kept the player paused.");
      }
      await refreshStatus({ preserveError: true });
      scheduleBrowserPlaybackWatchdog();
      return;
    }
    if (mode === "analog") {
      state.browserOutputStarting = false;
      stopBrowserAudio();
      await sleep(120);
    }
    const status = await api("/api/audio-output", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    updateStatus(status, { preserveError: mode === "browser" });
    if (mode === "analog") {
      await sleep(150);
      await refreshStatus();
    }
  } catch (error) {
    if (mode === "browser") {
      stopBrowserAudio();
    }
    setError(error.message);
    await refreshStatus();
  } finally {
    if (mode === "browser") {
      state.browserOutputStarting = false;
    }
    setBusy(activeButton, false);
  }
}

async function toggleRecord() {
  try {
    const action = state.status?.recording?.active ? "stop" : "start";
    const status = await api("/api/record", {
      method: "POST",
      body: JSON.stringify({ action }),
    });
    updateStatus(status);
    await refreshRecordings();
  } catch (error) {
    setError(error.message);
  }
}

async function setOledEnabled(enabled) {
  const toggle = document.getElementById("oledToggle");
  toggle.disabled = true;
  try {
    const status = await api("/api/oled", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    updateStatus(status);
  } catch (error) {
    setError(error.message);
    await refreshStatus();
  } finally {
    toggle.disabled = false;
  }
}

async function setSystemAutostart(enabled) {
  const toggle = document.getElementById("systemAutostartToggle");
  toggle.disabled = true;
  try {
    const status = await api("/api/system-autostart", {
      method: "POST",
      body: JSON.stringify({ enabled }),
    });
    updateStatus(status);
  } catch (error) {
    setError(error.message);
    await refreshStatus();
  } finally {
    toggle.disabled = false;
  }
}

async function installSpiConfig() {
  const button = document.getElementById("spiInstallButton");
  const setup = state.status?.spi_setup || {};
  const configPath = setup.config_path || "/boot/firmware/config.txt";
  const confirmed = window.confirm(
    `This will add dtparam=spi=on to ${configPath} and require a Raspberry Pi reboot. Continue?`,
  );
  if (!confirmed) return;
  setBusy(button, true, "Enabling...");
  try {
    const result = await api("/api/spi/install", {
      method: "POST",
      body: JSON.stringify({ confirm: true }),
    });
    if (result.status) {
      updateStatus(result.status);
    } else {
      await refreshStatus();
    }
    const changed = Boolean(result.config?.changed);
    setError(
      changed
        ? "SPI config added. Reboot the Raspberry Pi to activate /dev/spidev0.* and radio control."
        : "SPI config is already present. Reboot the Raspberry Pi if /dev/spidev0.* is still missing.",
    );
  } catch (error) {
    setError(error.message);
    await refreshStatus();
  } finally {
    setBusy(button, false);
  }
}

async function installI2sConfig() {
  const button = document.getElementById("i2sInstallButton");
  const setup = state.status?.live_stream?.i2s_setup || {};
  const configPath = setup.config_path || "/boot/firmware/config.txt";
  const confirmed = window.confirm(
    `This will modify ${configPath}, enable raspiaudio-radio.service at boot, and require a Raspberry Pi reboot. Continue?`,
  );
  if (!confirmed) return;
  setBusy(button, true, "Installing...");
  try {
    const result = await api("/api/i2s/install", {
      method: "POST",
      body: JSON.stringify({ confirm: true, enable_autostart: true }),
    });
    if (result.status) {
      updateStatus(result.status);
    } else {
      await refreshStatus();
    }
    const changed = Boolean(result.config?.changed);
    const autostartError = result.autostart?.error ? ` Autostart warning: ${result.autostart.error}` : "";
    setError(
      changed
        ? `I2S capture config added. Reboot the Raspberry Pi to activate browser output and recording.${autostartError}`
        : `I2S capture config is already present. Reboot the Raspberry Pi if the device is still missing.${autostartError}`,
    );
  } catch (error) {
    setError(error.message);
    await refreshStatus();
  } finally {
    setBusy(button, false);
  }
}

async function stopServer() {
  const button = document.getElementById("stopServerButton");
  setBusy(button, true, "Stopping...");
  try {
    await api("/api/server/stop", {
      method: "POST",
      body: JSON.stringify({}),
    });
    if (state.pollingHandle) {
      window.clearInterval(state.pollingHandle);
      state.pollingHandle = null;
    }
    const badge = document.getElementById("statusBadge");
    badge.className = "status-badge status-idle";
    badge.textContent = "Stopped";
    document.getElementById("bootState").textContent = "Server stopped. Reload the page after restarting the service.";
    setError("Server shutdown requested.");
  } catch (error) {
    setError(error.message);
    setBusy(button, false);
  }
}

function startPolling() {
  if (state.pollingHandle) {
    window.clearInterval(state.pollingHandle);
  }
  state.pollingHandle = window.setInterval(async () => {
    await syncBackendScanProgress();
    if (state.backendScanActive) return;
    await refreshStatus();
    if (state.status?.recording?.active) {
      await refreshRecordings();
    }
  }, 3000);
}

async function waitForServer(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(1200);
    try {
      const status = await api("/api/status");
      updateStatus(status);
      await refreshStations(status.mode);
      await refreshFavorites();
      await refreshRecordings();
      return;
    } catch (error) {
      // Retry until the server is back.
    }
  }
  throw new Error("Server restart requested, but the UI could not reconnect within 30 seconds.");
}

async function restartServer() {
  const button = document.getElementById("restartServerButton");
  setBusy(button, true, "Restarting...");
  if (state.pollingHandle) {
    window.clearInterval(state.pollingHandle);
    state.pollingHandle = null;
  }
  try {
    try {
      await api("/api/server/restart", {
        method: "POST",
        body: JSON.stringify({}),
      });
    } catch (error) {
      setError(`Restart requested. Waiting for the service to come back. ${error.message}`);
    }
    const badge = document.getElementById("statusBadge");
    badge.className = "status-badge status-ready";
    badge.textContent = "Restarting";
    document.getElementById("bootState").textContent = "Server restart requested. Reconnecting...";
    await waitForServer();
    startPolling();
    setError("Server restarted.");
  } catch (error) {
    setError(error.message);
  } finally {
    setBusy(button, false);
  }
}

function wireEvents() {
  document.getElementById("scanButton").addEventListener("click", scanStations);
  document.getElementById("restartServerButton").addEventListener("click", restartServer);
  document.getElementById("stopServerButton").addEventListener("click", stopServer);
  document.getElementById("volumeDownButton").addEventListener("click", () => updateVolume({ delta: -2 }));
  document.getElementById("volumeUpButton").addEventListener("click", () => updateVolume({ delta: 2 }));
  document.getElementById("muteButton").addEventListener("click", toggleMute);
  document.getElementById("ampButton").addEventListener("click", toggleAmplifier);
  document.getElementById("recordButton").addEventListener("click", toggleRecord);
  document.getElementById("analogOutputButton").addEventListener("click", () => setAudioOutput("analog"));
  document.getElementById("browserOutputButton").addEventListener("click", () => setAudioOutput("browser"));
  document.getElementById("spiInstallButton")?.addEventListener("click", installSpiConfig);
  document.getElementById("i2sInstallButton").addEventListener("click", installI2sConfig);
  document.getElementById("browserAudioPlayer").addEventListener("play", () => {
    state.browserOutputPlaying = true;
    updateAudioOutputUi(state.status || {});
  });
  document.getElementById("browserAudioPlayer").addEventListener("pause", () => {
    state.browserOutputPlaying = false;
    updateAudioOutputUi(state.status || {});
  });
  document.getElementById("browserAudioPlayer").addEventListener("error", () => {
    state.browserOutputPlaying = false;
    setError("Browser audio stream failed. Check the I2S capture overlay and arecord.");
  });
  document.getElementById("oledToggle").addEventListener("change", (event) => {
    setOledEnabled(Boolean(event.target.checked));
  });
  document.getElementById("systemAutostartToggle").addEventListener("change", (event) => {
    setSystemAutostart(Boolean(event.target.checked));
  });
  document.getElementById("volumeSlider").addEventListener("input", (event) => {
    const level = Number(event.target.value);
    updateVolumeReadout(level);
    applyBrowserPlayerVolume(level, state.status?.muted);
    clearTimeout(state.volumeDebounce);
    state.volumeDebounce = window.setTimeout(() => {
      updateVolume({ level });
    }, 120);
  });
  document.getElementById("stationSearch").addEventListener("input", (event) => {
    state.filter = event.target.value || "";
    renderStations();
  });
  document.querySelectorAll(".mode-card").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.mode));
  });
}

async function init() {
  wireEvents();
  startBackendScanWatcher();
  await syncBackendScanProgress();
  if (!state.backendScanActive) {
    await refreshAll();
  }
  startPolling();
}

window.addEventListener("DOMContentLoaded", init);
