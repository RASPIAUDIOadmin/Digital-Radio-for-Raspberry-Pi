const state = {
  status: null,
  stations: [],
  favorites: [],
  recordings: [],
  loadedMode: null,
  filter: "",
  pollingHandle: null,
  volumeDebounce: null,
};

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
  if (label) {
    button.dataset.originalLabel ||= button.textContent;
    button.textContent = busy ? label : button.dataset.originalLabel;
  }
}

function setError(message = "") {
  document.getElementById("errorLine").textContent = message;
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

function updateStatus(status) {
  state.status = status;
  const current = status.current_station || {};
  const signal = status.signal || {};
  const recording = status.recording || { active: false };

  document.getElementById("signalScore").textContent = signal.score ?? 0;
  document.getElementById("currentStation").textContent = current.label || "No station";
  document.getElementById("currentMode").textContent = status.mode_label || status.mode || "DAB";
  document.getElementById("firmwareLabel").textContent = "SPI host-load";
  document.getElementById("rssiValue").textContent = signal.rssi ?? "-";
  document.getElementById("snrValue").textContent = signal.snr ?? "-";
  document.getElementById("ficqValue").textContent = signal.fic_quality ?? "-";
  document.getElementById("cnrValue").textContent = signal.cnr ?? "-";
  document.getElementById("volumeLabel").textContent = `${status.volume ?? 0} / 63`;
  document.getElementById("volumeSlider").value = status.volume ?? 0;
  document.getElementById("bootState").textContent = status.booted
    ? `${status.mode_label} backend ready.`
    : "Backend is not initialized.";
  document.getElementById("audioOutLabel").textContent = `Audio out: ${status.audio_out || "both"}`;
  document.getElementById("scanMeta").textContent = status.last_scan_time
    ? `Last scan: ${formatTimestamp(status.last_scan_time)}`
    : "No scan yet";
  document.getElementById("stationCount").textContent = state.stations.length;
  document.getElementById("favoriteCount").textContent = state.favorites.length;
  document.getElementById("recordingCount").textContent = state.recordings.length;

  const ampButton = document.getElementById("ampButton");
  ampButton.textContent = status.amp_enabled ? "Amplifier on" : "Amplifier off";
  ampButton.classList.toggle("is-on", Boolean(status.amp_enabled));

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

  setError(status.last_error || "");
  updateDabMedia(status);
  renderModes();
  renderStations();
  renderFavorites();
  renderRecordings();
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

async function refreshStatus() {
  try {
    const status = await api("/api/status");
    const modeChanged = state.loadedMode !== status.mode;
    updateStatus(status);
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
    state.recordings = data.recordings || [];
    renderRecordings();
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
  try {
    const status = await api("/api/play", {
      method: "POST",
      body: JSON.stringify({ station_id: stationId }),
    });
    updateStatus(status);
    await refreshStations(status.mode);
    await refreshFavorites();
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

function wireEvents() {
  document.getElementById("scanButton").addEventListener("click", scanStations);
  document.getElementById("stopServerButton").addEventListener("click", stopServer);
  document.getElementById("volumeDownButton").addEventListener("click", () => updateVolume({ delta: -2 }));
  document.getElementById("volumeUpButton").addEventListener("click", () => updateVolume({ delta: 2 }));
  document.getElementById("ampButton").addEventListener("click", toggleAmplifier);
  document.getElementById("recordButton").addEventListener("click", toggleRecord);
  document.getElementById("volumeSlider").addEventListener("input", (event) => {
    clearTimeout(state.volumeDebounce);
    state.volumeDebounce = window.setTimeout(() => {
      updateVolume({ level: Number(event.target.value) });
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
  state.pollingHandle = window.setInterval(async () => {
    await refreshStatus();
    if (state.status?.recording?.active) {
      await refreshRecordings();
    }
  }, 3000);
}

window.addEventListener("DOMContentLoaded", init);
