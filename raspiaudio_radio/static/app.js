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
  const media = status.dab_media || {};
  const isDab = status.mode === "dab";
  const current = status.current_station || {};
  const hasText = Boolean(media.text || media.artist || media.title);
  const hasArtwork = Boolean(media.artwork_url);
  const mediaTimestamp = media.artwork_updated_at || media.updated_at;

  document.getElementById("mediaArtist").textContent = media.artist || (isDab ? "No artist yet" : "DAB only");
  document.getElementById("mediaTitle").textContent = media.title || (isDab ? "No title yet" : "DAB only");
  document.getElementById("mediaText").textContent = isDab
    ? media.text || "No DAB text received yet."
    : "Switch to DAB and tune a station to read metadata.";
  document.getElementById("mediaUpdated").textContent = mediaTimestamp
    ? `Updated: ${formatTimestamp(mediaTimestamp)}`
    : (isDab ? "No metadata received yet." : "DAB metadata is inactive.");
  document.getElementById("mediaHint").textContent = isDab
    ? hasArtwork
      ? "Slideshow image received from the current DAB station."
      : "Waiting for slideshow image from the current DAB station."
    : "Artwork and DLS text are only available in DAB mode.";

  const statusPill = document.getElementById("mediaStatus");
  statusPill.textContent = !isDab ? "DAB only" : hasArtwork && hasText ? "Artwork + text" : hasArtwork ? "Artwork" : hasText ? "Live text" : "Waiting";

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
    fallback.textContent = (current.label || "DAB").slice(0, 3).toUpperCase();
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
    node.querySelector(".station-name").textContent = station.label;
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
    node.querySelector(".favorite-label").textContent = station.label;
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
  try {
    const status = await api("/api/mode", {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    updateStatus(status);
    await refreshStations(mode);
  } catch (error) {
    setError(error.message);
  }
}

async function scanStations() {
  const button = document.getElementById("scanButton");
  setBusy(button, true, "Scanning...");
  try {
    const data = await api("/api/scan", {
      method: "POST",
      body: JSON.stringify({ force: true }),
    });
    state.stations = data.stations || [];
    renderStations();
    await refreshStatus();
    await refreshFavorites();
  } catch (error) {
    setError(error.message);
  } finally {
    setBusy(button, false);
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
  await refreshAll();
  startPolling();
}

window.addEventListener("DOMContentLoaded", init);
