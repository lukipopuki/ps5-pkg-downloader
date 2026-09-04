/* PS5 Patch Downloader - WebUI
 *
 * Talks to the same JSON API that any other tool can use; nothing here is
 * privileged. Downloads are polled instead of streamed so the page survives
 * proxies and sleeping tabs without a reconnect dance.
 */
"use strict";

const state = {
  title: null,
  settings: {},
  pollTimer: null,
  busy: new Set(),
};

const $ = (id) => document.getElementById(id);

// --- helpers ---------------------------------------------------------------

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 204) return null;
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = { detail: text }; }
  }
  if (!response.ok) {
    throw new Error((payload && payload.detail) || `HTTP ${response.status}`);
  }
  return payload;
}

function formatBytes(value) {
  if (value === null || value === undefined || value < 0) return "–";
  if (value === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const index = Math.min(units.length - 1, Math.floor(Math.log10(value) / 3));
  const scaled = value / Math.pow(1000, index);
  return `${scaled.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

function formatSpeed(bytesPerSecond) {
  if (!bytesPerSecond) return "–";
  return `${formatBytes(bytesPerSecond)}/s`;
}

function formatEta(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  return `${hours} h ${minutes % 60} min`;
}

function setStatus(element, message, kind = "") {
  if (!message) {
    element.hidden = true;
    element.textContent = "";
    return;
  }
  element.hidden = false;
  element.className = `status ${kind}`.trim();
  element.textContent = message;
}

function text(node, value) { node.textContent = value === null || value === undefined ? "" : String(value); }

// --- search ----------------------------------------------------------------

async function runSearch(refresh = false) {
  const query = $("search-input").value.trim();
  if (!query) return;
  const status = $("search-status");
  const list = $("search-results");
  setStatus(status, "Searching…");
  list.hidden = true;
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}&refresh=${refresh}`);
    list.innerHTML = "";
    if (!data.results.length) {
      setStatus(status, data.hint || "No results.", "error");
      return;
    }
    setStatus(status, data.cached ? "Results from cache." : "");
    for (const item of data.results) {
      const li = document.createElement("li");
      li.tabIndex = 0;
      if (item.icon_url) {
        const img = document.createElement("img");
        img.src = item.icon_url;
        img.alt = "";
        img.loading = "lazy";
        li.appendChild(img);
      }
      const wrap = document.createElement("div");
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = item.name || item.title_id;
      const sub = document.createElement("div");
      sub.className = "mono muted";
      sub.textContent = [item.title_id, item.region].filter(Boolean).join(" · ");
      wrap.append(name, sub);
      li.appendChild(wrap);
      const open = () => loadTitle(item.title_id);
      li.addEventListener("click", open);
      li.addEventListener("keydown", (event) => { if (event.key === "Enter") open(); });
      list.appendChild(li);
    }
    list.hidden = false;
    if (data.results.length === 1) loadTitle(data.results[0].title_id);
  } catch (error) {
    setStatus(status, error.message, "error");
  }
}

// --- title view ------------------------------------------------------------

async function loadTitle(titleId, refresh = false) {
  const panel = $("title-panel");
  try {
    const data = await api(`/api/title/${encodeURIComponent(titleId)}?refresh=${refresh}`);
    state.title = data;
    panel.hidden = false;
    renderTitle(data);
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    setStatus($("search-status"), error.message, "error");
  }
}

function renderTitle(data) {
  const title = data.title;
  text($("title-name"), title.name || title.title_id);
  text($("title-ids"), [title.title_id, title.content_id].filter(Boolean).join("  ·  "));
  text($("title-extra"), [title.publisher, title.region, title.last_updated && `updated ${title.last_updated}`]
    .filter(Boolean).join("  ·  "));
  const icon = $("title-icon");
  if (title.icon_url) { icon.src = title.icon_url; icon.hidden = false; } else { icon.hidden = true; }

  const warning = $("title-warning");
  if (data.warning) {
    warning.hidden = false;
    warning.textContent = `Showing cached data: ${data.warning}`;
  } else {
    warning.hidden = true;
  }
  $("versionxml-input").value = title.version_file_uri || "";

  renderUpdates(data.updates || []);
  renderDlc(data.additional_content || []);
}

function compatibilityBadge(entry) {
  const span = document.createElement("span");
  if (entry.compatible === true) {
    span.className = "badge ok";
    span.textContent = "compatible";
  } else if (entry.compatible === false) {
    span.className = "badge err";
    span.textContent = "incompatible";
  } else {
    span.className = "badge warn";
    span.textContent = "unknown";
  }
  return span;
}

function downloadButton(payload, label = "Download") {
  const button = document.createElement("button");
  button.className = "primary small-btn";
  button.textContent = label;
  button.addEventListener("click", async () => {
    button.disabled = true;
    const original = button.textContent;
    button.textContent = "Starting…";
    try {
      await api("/api/download", { method: "POST", body: JSON.stringify(payload) });
      button.textContent = "Queued";
      await refreshDownloads();
    } catch (error) {
      button.disabled = false;
      button.textContent = original;
      window.alert(error.message);
    }
  });
  return button;
}

function renderUpdates(updates) {
  const body = $("updates-body");
  body.innerHTML = "";
  $("updates-empty").hidden = updates.length > 0;
  for (const update of updates) {
    const row = document.createElement("tr");

    const version = document.createElement("td");
    version.className = "mono";
    version.textContent = update.content_ver;
    if (update.is_latest) {
      const tag = document.createElement("span");
      tag.className = "badge ok";
      tag.style.marginLeft = "8px";
      tag.textContent = "latest";
      version.appendChild(tag);
    }

    const firmware = document.createElement("td");
    firmware.className = "mono";
    firmware.textContent = update.required_firmware || "–";

    const size = document.createElement("td");
    size.textContent = formatBytes(update.file_size);

    const date = document.createElement("td");
    date.className = "muted small";
    date.textContent = update.import_date || "–";

    const status = document.createElement("td");
    status.appendChild(compatibilityBadge(update));

    const actions = document.createElement("td");
    actions.className = "actions";
    const button = downloadButton({
      title_id: state.title.title.title_id,
      content_ver: update.content_ver,
      kind: "app",
      ignore_firmware: update.compatible === false,
    });
    if (update.compatible === false) {
      button.className = "ghost small-btn";
      button.title = "This update requires newer firmware than configured";
      button.textContent = "Download anyway";
    }
    actions.appendChild(button);

    row.append(version, firmware, size, date, status, actions);
    body.appendChild(row);
  }
}

function renderDlc(items) {
  const body = $("dlc-body");
  body.innerHTML = "";
  $("dlc-empty").hidden = items.length > 0;
  for (const item of items) {
    const row = document.createElement("tr");
    const name = document.createElement("td");
    name.textContent = item.name || item.content_id;
    const version = document.createElement("td");
    version.className = "mono";
    version.textContent = item.content_ver || "–";
    const firmware = document.createElement("td");
    firmware.className = "mono";
    firmware.textContent = item.required_firmware || "–";
    const size = document.createElement("td");
    size.textContent = formatBytes(item.file_size);
    const status = document.createElement("td");
    status.appendChild(compatibilityBadge(item));
    const actions = document.createElement("td");
    actions.className = "actions";
    actions.appendChild(downloadButton({
      title_id: state.title.title.title_id,
      content_ver: item.content_ver,
      content_id: item.content_id,
      kind: "ac",
      ignore_firmware: item.compatible === false,
    }));
    row.append(name, version, firmware, size, status, actions);
    body.appendChild(row);
  }
}

// --- downloads -------------------------------------------------------------

function statusClass(status) {
  if (status === "completed") return "done";
  if (status === "error") return "err";
  if (status === "paused" || status === "queued") return "paused";
  return "";
}

async function action(id, path, method = "POST") {
  if (state.busy.has(id)) return;
  state.busy.add(id);
  try {
    await api(path, { method });
    await refreshDownloads();
  } catch (error) {
    window.alert(error.message);
  } finally {
    state.busy.delete(id);
  }
}

function renderDownloads(jobs) {
  const list = $("downloads-list");
  $("downloads-empty").hidden = jobs.length > 0;
  list.innerHTML = "";
  const active = jobs.filter((job) => job.status === "running").length;
  text($("downloads-summary"), jobs.length
    ? `${active} running · ${jobs.length} total`
    : "");

  for (const job of jobs) {
    const card = document.createElement("div");
    card.className = "download";

    const head = document.createElement("div");
    head.className = "download-head";
    const name = document.createElement("div");
    name.className = "download-name";
    name.textContent = job.title_name || job.title_id || "Package";
    const sub = document.createElement("div");
    sub.className = "download-sub mono";
    sub.textContent = [job.title_id, job.content_ver, job.status].filter(Boolean).join(" · ");
    head.append(name, sub);

    const bar = document.createElement("div");
    bar.className = `bar ${statusClass(job.status)}`.trim();
    const fill = document.createElement("span");
    fill.style.width = `${Math.max(0, Math.min(100, job.progress))}%`;
    bar.appendChild(fill);

    const stats = document.createElement("div");
    stats.className = "download-stats";
    const parts = [
      `${job.progress.toFixed(1)} %`,
      `${formatBytes(job.downloaded)} / ${formatBytes(job.total_size)}`,
    ];
    if (job.status === "running") {
      parts.push(formatSpeed(job.speed_bps), `ETA ${formatEta(job.eta_seconds)}`);
    }
    if (job.required_firmware) parts.push(`FW ${job.required_firmware}`);
    for (const part of parts) {
      const span = document.createElement("span");
      span.textContent = part;
      stats.appendChild(span);
    }

    const actions = document.createElement("div");
    actions.className = "download-actions";
    const add = (label, handler, className = "ghost small-btn") => {
      const button = document.createElement("button");
      button.className = className;
      button.textContent = label;
      button.addEventListener("click", handler);
      actions.appendChild(button);
    };

    if (job.status === "running" || job.status === "queued") {
      add("Pause", () => action(job.id, `/api/download/${job.id}/pause`));
    }
    if (job.status === "paused" || job.status === "cancelled") {
      add("Resume", () => action(job.id, `/api/download/${job.id}/resume`), "primary small-btn");
    }
    if (job.status === "error") {
      add("Retry", () => action(job.id, `/api/download/${job.id}/retry`), "primary small-btn");
      add("Restart from scratch", () => action(job.id, `/api/download/${job.id}/retry?from_scratch=true`));
    }
    if (job.status !== "completed") {
      add("Cancel", () => {
        if (window.confirm("Cancel this download and delete its partial file?")) {
          action(job.id, `/api/download/${job.id}`, "DELETE");
        }
      }, "danger small-btn");
    } else {
      add("Remove from list", () => action(job.id, `/api/download/${job.id}?delete_files=false`, "DELETE"));
    }

    card.append(head, bar, stats, actions);

    if (job.error) {
      const error = document.createElement("div");
      error.className = "download-error";
      error.textContent = job.error;
      card.appendChild(error);
    }
    if (job.status === "completed" && job.output_path) {
      const path = document.createElement("div");
      path.className = "mono muted small";
      path.textContent = job.output_path;
      card.appendChild(path);
    }
    list.appendChild(card);
  }
}

async function refreshDownloads() {
  try {
    const data = await api("/api/downloads");
    renderDownloads(data.downloads);
  } catch (error) {
    console.warn("download poll failed", error);
  }
}

// --- settings --------------------------------------------------------------

async function loadSettings() {
  state.settings = await api("/api/settings");
  const badge = $("fw-badge");
  badge.textContent = state.settings.max_firmware ? `FW ≤ ${state.settings.max_firmware}` : "FW filter off";
  $("set-firmware").value = state.settings.max_firmware || "";
  $("set-ttl").value = state.settings.cache_ttl_hours;
  $("set-concurrency").value = state.settings.max_concurrent_downloads;
  $("set-bandwidth").value = state.settings.max_bandwidth_mbps;
  text($("settings-paths"),
    `Downloads: ${state.settings.download_dir} · Config: ${state.settings.config_dir} · ` +
    `Rules: ${state.settings.rules_file}`);
}

async function saveSettings() {
  const status = $("settings-status");
  try {
    await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify({
        max_firmware: $("set-firmware").value.trim(),
        cache_ttl_hours: Number($("set-ttl").value),
        max_concurrent_downloads: Number($("set-concurrency").value),
        max_bandwidth_mbps: Number($("set-bandwidth").value),
      }),
    });
    await loadSettings();
    setStatus(status, "Saved.", "ok");
    if (state.title) loadTitle(state.title.title.title_id);
  } catch (error) {
    setStatus(status, error.message, "error");
  }
}

// --- wiring ----------------------------------------------------------------

function wire() {
  $("search-form").addEventListener("submit", (event) => { event.preventDefault(); runSearch(false); });
  $("btn-refresh-search").addEventListener("click", () => runSearch(true));
  $("btn-title-refresh").addEventListener("click", () => {
    if (state.title) loadTitle(state.title.title.title_id, true);
  });

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => {
      for (const other of document.querySelectorAll(".tab")) other.classList.toggle("active", other === tab);
      for (const body of document.querySelectorAll(".tab-body")) {
        body.hidden = body.dataset.body !== tab.dataset.tab;
      }
    });
  }

  $("btn-versionxml").addEventListener("click", async () => {
    const status = $("versionxml-status");
    const url = $("versionxml-input").value.trim();
    if (!url || !state.title) return;
    setStatus(status, "Checking…");
    try {
      const data = await api(`/api/title/${state.title.title.title_id}/version-xml`, {
        method: "POST",
        body: JSON.stringify({ url }),
      });
      setStatus(status, `Registered. Sony lists ${data.packages.length} package(s) for this title.`, "ok");
      loadTitle(state.title.title.title_id, true);
    } catch (error) {
      setStatus(status, error.message, "error");
    }
  });

  $("btn-manual").addEventListener("click", async () => {
    const status = $("manual-status");
    const payload = {
      manifest_url: $("manual-url").value.trim(),
      title_id: $("manual-title").value.trim(),
      content_ver: $("manual-version").value.trim(),
    };
    if (!payload.manifest_url) { setStatus(status, "A manifest URL is required.", "error"); return; }
    setStatus(status, "Queuing…");
    try {
      await api("/api/download", { method: "POST", body: JSON.stringify(payload) });
      setStatus(status, "Queued.", "ok");
      await refreshDownloads();
    } catch (error) {
      setStatus(status, error.message, "error");
    }
  });

  $("btn-settings").addEventListener("click", () => {
    setStatus($("settings-status"), "");
    $("settings-dialog").showModal();
  });
  $("btn-save-settings").addEventListener("click", saveSettings);
  $("btn-clear-cache").addEventListener("click", async () => {
    try {
      await api("/api/cache/refresh", { method: "POST" });
      setStatus($("settings-status"), "Metadata cache cleared.", "ok");
    } catch (error) {
      setStatus($("settings-status"), error.message, "error");
    }
  });
  $("btn-reload-rules").addEventListener("click", async () => {
    try {
      const data = await api("/api/rules/reload", { method: "POST" });
      setStatus($("settings-status"), `Rules reloaded from ${data.path}.`, "ok");
    } catch (error) {
      setStatus($("settings-status"), error.message, "error");
    }
  });
}

async function main() {
  wire();
  await loadSettings().catch((error) => console.warn(error));
  await refreshDownloads();
  state.pollTimer = window.setInterval(refreshDownloads, 1500);
  const params = new URLSearchParams(window.location.search);
  const initial = params.get("title");
  if (initial) {
    $("search-input").value = initial;
    loadTitle(initial);
  }
}

document.addEventListener("DOMContentLoaded", main);
