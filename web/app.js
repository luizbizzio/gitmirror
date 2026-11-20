"use strict";

const $ = (query) => document.querySelector(query);
const all = (query) => [...document.querySelectorAll(query)];
const escape = (value) => String(value ?? "").replace(/[&<>"']/g,
  (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character]);
const date = (stamp, fallback = "Unobserved") => Number.isFinite(stamp) ? new Date(stamp * 1000).toLocaleString() : fallback;
const size = (bytes) => {
  if (!Number.isFinite(bytes)) return "Unavailable";
  if (bytes < 1024) return `${bytes} B`;
  const group = Math.min(4, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** group).toFixed(1)} ${["B", "KiB", "MiB", "GiB", "TiB"][group]}`;
};
const duration = (run) => {
  const seconds = Math.max(0, Math.floor((run.ended ?? Date.now() / 1000) - run.started));
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
};
const knownStates = new Set(["succeeded", "failed", "blocked", "running", "queued", "interrupted", "checked", "verified", "unverified"]);
const pill = (state) => `<span class="pill ${knownStates.has(state) ? state : ""}">${escape(state || "Unobserved")}</span>`;
const empty = (message) => `<p class="empty">${escape(message)}</p>`;
const list = (pairs) => `<dl class="data">${pairs.map(([key, value]) => `<dt>${escape(key)}</dt><dd>${escape(value)}</dd>`).join("")}</dl>`;
const table = (labels, rows) => `<table><thead><tr>${labels.map((label) => `<th scope="col">${escape(label)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
const row = (...cells) => `<tr>${cells.map((value) => `<td>${value}</td>`).join("")}</tr>`;
const button = (action, value, label, disabled = false) => `<button class="link" type="button" data-action="${action}" data-value="${escape(value)}" ${disabled ? "disabled" : ""}>${escape(label)}</button>`;
const origins = {cli: "CLI", web: "Web", scheduled: "Scheduler", cron: "Cron"};
const pages = {overview: "Dashboard", repositories: "Repositories", destinations: "Destinations", backups: "Backups",
  activity: "Activity log", settings: "Settings"};

let csrf = "", view = "overview", snapshot = null, stream = null, refreshTimer = null;
let repositories = [], targetSignature = "", currentRun = "", restoreSelection = null, targets = [];
let loading = false, refreshPending = false, logRequest = 0, busy = false;
let previewMode = false, initialSetupData = null;
let securityEnrollment = false, securityRecovery = false;
let inventoryWarmupAttempted = false;
const mobileNavigation = window.matchMedia("(max-width: 760px)");
let compactNavigation = false;

function routeFromUrl() {
  const route = window.location.hash.slice(1);
  return Object.hasOwn(pages, route) ? route : "overview";
}

function updateNavigationControl() {
  const mobile = mobileNavigation.matches;
  const open = $("#shell").classList.contains("mobile-nav-open");
  $("#nav-toggle").setAttribute("aria-expanded", String(mobile ? open : !compactNavigation));
  $("#nav-toggle").setAttribute("aria-label", mobile
    ? (open ? "Close navigation" : "Open navigation")
    : (compactNavigation ? "Expand navigation" : "Collapse navigation"));
  $("#sidebar-backdrop").hidden = !mobile || !open;
}

function closeMobileNavigation() {
  $("#shell").classList.remove("mobile-nav-open");
  updateNavigationControl();
}

function initializeNavigation() {
  try { compactNavigation = window.localStorage.getItem("gitmirror-sidebar") === "compact"; } catch { /* optional */ }
  $("#shell").classList.toggle("nav-collapsed", compactNavigation && !mobileNavigation.matches);
  updateNavigationControl();
}

function toggleNavigation() {
  if (mobileNavigation.matches) {
    $("#shell").classList.toggle("mobile-nav-open");
  } else {
    compactNavigation = !compactNavigation;
    $("#shell").classList.toggle("nav-collapsed", compactNavigation);
    try { window.localStorage.setItem("gitmirror-sidebar", compactNavigation ? "compact" : "expanded"); }
    catch { /* storage may be disabled */ }
  }
  updateNavigationControl();
}

function notify(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
}

function connected(status) {
  $("#connection").textContent = status;
  $("#connection").classList.toggle("connected", status === "Web connected" || status === "Web online");
}

function updateSystemStatus(data) {
  const state = $("#system-state");
  const indicator = document.createElement("span");
  indicator.className = "status-dot";
  let title, description;
  if (data.web.writes_paused) {
    title = "Synchronization paused";
    description = "Managed maintenance is in progress";
  } else if (data.errors.length || data.backup.failed) {
    title = "Review recent errors";
    description = "Open Activity log or Backups for details";
  } else if (!data.inventory_at) {
    title = "Inventory pending";
    description = "No repository status has been measured yet";
  } else {
    title = "Monitoring ready";
    description = `${data.total_repositories} repositories in source inventory`;
    indicator.classList.add("ready");
  }
  state.replaceChildren(indicator, document.createTextNode(title));
  $("#system-detail").textContent = description;
}

function signOut() {
  csrf = "";
  stream?.close();
  stream = null;
  clearInterval(refreshTimer);
  refreshTimer = null;
  closeMobileNavigation();
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form").hidden = false;
  $("#first-password-form").hidden = true;
  $("#mfa-form").hidden = true;
  $("#login-label").textContent = "Dashboard password";
  $("#login-description").textContent = "Sign in to manage your mirrors and backups.";
  securityEnrollment = securityRecovery = false;
  for (const dialog of all("dialog[open]")) dialog.close();
  $("#login-password").focus();
}

async function api(path, { data, method = "GET" } = {}) {
  const headers = {Accept: "application/json"};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (csrf && method !== "GET") headers["X-CSRF-Token"] = csrf;
  let response;
  try {
    response = await fetch(path, {method, headers, credentials: "same-origin",
      body: data === undefined ? undefined : JSON.stringify(data)});
  } catch {
    throw new Error("Cannot reach the GitMirror dashboard.");
  }
  if (response.status === 401 && path !== "/api/login") {
    signOut();
    throw new Error("Sign in again to continue.");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `Request failed (${response.status}).`);
  return payload;
}

const post = (path, data) => api(path, {method: "POST", data});
function report(error) { notify(error.message, true); }

function openView(name, {updateUrl = true} = {}) {
  if (!Object.hasOwn(pages, name)) return;
  view = name;
  for (const item of all("#main-navigation [data-view]")) {
    if (item.dataset.view === view) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  for (const page of all(".content > section")) page.hidden = page.id !== `view-${view}`;
  $("#page-title").textContent = pages[view];
  $("#topbar-location").textContent = pages[view];
  document.title = `${pages[view]} | GitMirror`;
  if (updateUrl && window.location.hash !== `#${view}`) {
    window.history.pushState(null, "", `#${view}`);
  }
  closeMobileNavigation();
  if (previewMode) {
    renderPreviewView();
  } else {
    renderContext();
    refresh().catch(report);
  }
}

function renderContext() {
  if (!snapshot) return;
  const source = `${providerLabel(snapshot.source.provider)} / ${snapshot.source.owner}`;
  const context = {
    overview: source, repositories: source, destinations: `${snapshot.destinations.length} configured targets`,
    backups: `${snapshot.backup.configured_volumes} configured local volumes`,
    activity: "CLI, scheduler, cron and web operations", settings: "Read-only configuration",
  };
  $("#page-context").textContent = context[view];
}

function stat(label, value, note) {
  return `<div><dt>${escape(label)}</dt><dd>${escape(value)}</dd><small>${escape(note)}</small></div>`;
}

function renderActivityChart(runs = []) {
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const days = Array.from({length: 7}, (_, index) => {
    const day = new Date(today);
    day.setDate(today.getDate() - (6 - index));
    const end = new Date(day);
    end.setDate(day.getDate() + 1);
    const finished = runs.filter((run) => run.mode === "apply" && Number.isFinite(run.ended)
      && run.ended * 1000 >= day.getTime() && run.ended * 1000 < end.getTime());
    return {name: day.toLocaleDateString(undefined, {weekday: "short"}), successful: finished.filter(
      (run) => run.state === "succeeded").length, failed: finished.filter(
      (run) => run.state !== "succeeded").length};
  });
  const maximum = Math.max(1, ...days.map((day) => day.successful + day.failed));
  const total = days.reduce((sum, day) => sum + day.successful + day.failed, 0);
  $("#chart-summary").textContent = total ? `${total} completed run${total === 1 ? "" : "s"}` : "No completed runs";
  $("#activity-chart").innerHTML = days.map((day) => `<div class="chart-day"
    aria-label="${escape(day.name)}: ${day.successful} successful, ${day.failed} failed">
      <div class="chart-column"><span class="chart-bar failed-bar"></span>
      <span class="chart-bar success-bar"></span></div>
      <span class="chart-label">${escape(day.name)}</span></div>`).join("");
  all("#activity-chart .chart-day").forEach((node, index) => {
    node.querySelector(".success-bar").style.height = `${days[index].successful / maximum * 100}%`;
    node.querySelector(".failed-bar").style.height = `${days[index].failed / maximum * 100}%`;
  });
}

const storageColors = ["#6ab8e6", "#58b99b", "#e1b66d", "#a99ad9", "#df8a7e",
  "#87bdc4", "#a7bd70", "#8c9ebd"];

function chartPlaceholder(message) {
  return `<div class="storage-placeholder"><span class="storage-placeholder-icon" aria-hidden="true">◌</span>
    <p>${escape(message)}</p></div>`;
}

function renderStorageChart(chartId, noteId, records, options = {}) {
  const chart = $(`#${chartId}`);
  const note = $(`#${noteId}`);
  const items = Array.isArray(records) ? records : [];
  const known = items.filter((item) => Number.isSafeInteger(item.bytes) && item.bytes >= 0);
  const unknown = items.length - known.length;
  const positive = known.filter((item) => item.bytes > 0).sort((a, b) => b.bytes - a.bytes);
  const total = positive.reduce((sum, item) => sum + item.bytes, 0);
  note.textContent = options.source
    ? "Provider estimate. Git LFS and additional storage may not be included."
    : "Logical file sizes across local backups. Hardlinks may be counted more than once.";
  if (unknown) note.textContent += ` No size reported for ${unknown} repositor${unknown === 1 ? "y" : "ies"}.`;
  if (!positive.length || !total) {
    chart.innerHTML = chartPlaceholder(known.length ? "The measured size is 0 B." :
      options.emptyMessage || "No storage data yet.");
    return;
  }
  const slices = positive.slice(0, 7).map((item) => ({...item}));
  if (positive.length > 7) {
    slices.push({name: `Other (${positive.length - 7})`,
      bytes: positive.slice(7).reduce((sum, item) => sum + item.bytes, 0)});
  }
  chart.replaceChildren();
  const graphic = document.createElement("div");
  graphic.className = "donut-graphic";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 160 160");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `Repository storage breakdown, ${size(total)} in total`);
  const center = 80, radius = 60, circumference = 2 * Math.PI * radius;
  const ring = document.createElementNS(svg.namespaceURI, "circle");
  for (const [key, value] of Object.entries({cx:center, cy:center, r:radius,
    stroke:"#2d4150", "stroke-width":22, fill:"none"})) ring.setAttribute(key, value);
  svg.append(ring);
  let consumed = 0;
  slices.forEach((item, index) => {
    const segment = document.createElementNS(svg.namespaceURI, "circle");
    const portion = item.bytes / total * circumference;
    for (const [key, value] of Object.entries({cx:center, cy:center, r:radius, fill:"none",
      stroke:storageColors[index], "stroke-width":22,
      "stroke-dasharray":`${portion} ${circumference}`,
      "stroke-dashoffset": -consumed, transform:"rotate(-90 80 80)"})) segment.setAttribute(key, value);
    const title = document.createElementNS(svg.namespaceURI, "title");
    title.textContent = `${item.name}: ${size(item.bytes)}`;
    segment.append(title);
    svg.append(segment);
    consumed += portion;
  });
  graphic.append(svg);
  const summary = document.createElement("div");
  summary.className = "donut-center";
  summary.innerHTML = `<strong>${escape(size(total))}</strong><span>${known.length} repositories</span>`;
  graphic.append(summary);
  const legend = document.createElement("ol");
  legend.className = "donut-legend";
  slices.forEach((item, index) => {
    const row = document.createElement("li");
    const percent = (item.bytes / total * 100).toFixed(1);
    row.innerHTML = `<span class="legend-swatch"></span><span class="legend-name" title="${escape(item.name)}">${escape(item.name)}</span>
      <strong class="legend-value">${escape(size(item.bytes))}</strong>
      <span class="legend-percent">${percent}%</span>`;
    row.querySelector(".legend-swatch").style.backgroundColor = storageColors[index];
    legend.append(row);
  });
  chart.append(graphic, legend);
}

function emptyPage(title, description, action = "Open setup") {
  return `<article class="panel empty-panel"><div class="empty-icon" aria-hidden="true">⑂</div>
    <h2>${escape(title)}</h2><p>${escape(description)}</p>
    <button type="button" data-setup-open class="button primary">${escape(action)}</button></article>`;
}

function renderStorageEmpty() {
  renderStorageChart("source-storage-chart", "source-storage-note", [],
    {source: true, emptyMessage: "Connect your source to see how much space each repository uses."});
  renderStorageChart("backup-storage-chart", "backup-storage-note", [],
    {emptyMessage: "Add a local backup destination to see repository storage breakdowns."});
}

function renderPreviewView() {
  const messages = {
    overview: "System overview", repositories: "Source repository inventory",
    destinations: "Configured mirroring targets", backups: "Local backup storage",
    activity: "Synchronization and verification history", settings: "System configuration",
  };
  $("#page-context").textContent = messages[view];
  if (view === "overview") {
    $("#stats").innerHTML = stat("Repositories", "—", "No source connected")
      + stat("Destinations", "0", "No mirrors configured")
      + stat("Successful syncs", "—", "No executions yet")
      + stat("Local backups", "—", "No backup volume");
    renderStorageEmpty();
    $("#activity-panel").hidden = true;
    $("#overview-operations").hidden = true;
  } else if (view === "repositories") {
    $("#repository-workspace").hidden = true;
    $("#repository-onboarding").hidden = false;
    $("#inventory-time").textContent = "Source not configured";
    $("#repositories").innerHTML = empty("Connect a source account in Setup to view repositories.");
    $("#discover").disabled = true;
    $("#sync-submit").disabled = true;
    $("#actions-help").textContent = "Synchronization is unavailable until setup is complete.";
    $("#sync-targets").innerHTML = "<legend>Destinations</legend><p>None configured.</p>";
  } else if (view === "destinations") {
    $("#destinations").innerHTML = emptyPage("No destinations configured",
      "Add GitLab, Codeberg or local backup storage to receive your copies.");
  } else if (view === "backups") {
    $("#backups").innerHTML = emptyPage("No local backups yet",
      "Add a backup volume in Setup to keep recoverable repository generations.");
  } else if (view === "activity") {
    $("#activity").innerHTML = '<div class="empty-list-message"><strong>No activity recorded</strong><p>Runs and verification results appear here after the first synchronization.</p></div>';
    $("#log-panel").hidden = true;
  }
}

function enterPreview(data) {
  previewMode = true;
  initialSetupData = data;
  $("#shell").hidden = false;
  $("#login").hidden = true;
  $("#setup-banner").hidden = false;
  $("#settings-preview").hidden = false;
  $("#live-settings").hidden = true;
  $("#settings-editor").hidden = true;
  $("#logout").hidden = true;
  $("#refresh").disabled = true;
  $("#top-setup").textContent = "Setup";
  $("#system-state").innerHTML = '<span class="status-dot"></span> Setup required';
  $("#system-detail").textContent = "Sync is disabled until configuration";
  connected("Web online");
  initializeNavigation();
  openView(routeFromUrl(), {updateUrl: false});
}

function showInitialSetup() {
  if (!previewMode || !initialSetupData) return;
  if (!$("#setup").open) {
    openConfiguration(true, initialSetupData);
    $("#setup").showModal();
  }
}

function runName(run) {
  if (run.operation === "doctor") return "Verify connections";
  if (run.operation !== "sync") return run.operation === "restore" ? "Restore" : "Verify backup";
  return run.mode === "apply" ? "Synchronization" : "Dry-run";
}

function runItems(runs, none) {
  return runs.map((run) => `<div class="item"><div>
    ${button("log", run.id, runName(run))}<p>${escape(run.repository || "All repositories")} · ${escape(origins[run.origin] || run.origin)}</p>
    ${run.progress && run.state === "running" ?
      `<p>${escape(run.progress.repository)} · ${escape(run.progress.index)} / ${escape(run.progress.total)}</p>` : ""}
    </div>${pill(run.state)}</div>`).join("") || empty(none);
}

function renderOverview(data) {
  const completed = data.successful + data.failed;
  $("#stats").innerHTML = stat("Repositories", data.total_repositories ?? "—",
    data.inventory_at ? "Discovered at source" : "Inventory not loaded")
    + stat("Destinations", String(data.destinations.length), "Mirrors and local backups")
    + stat("Successful syncs", completed ? String(data.successful) : "—",
      completed ? `${data.failed} unsuccessful` : "No applied runs yet")
    + stat("Local backups", size(data.backup.logical_bytes),
      `${data.backup.available_volumes} available volumes`);
  renderStorageChart("source-storage-chart", "source-storage-note",
    data.repository_storage?.repositories, {source:true,
      emptyMessage: data.inventory_at ? "This provider did not report repository sizes."
        : "Read the source inventory to display repository sizes."});
  renderStorageChart("backup-storage-chart", "backup-storage-note",
    data.backup_storage?.repositories, {emptyMessage: data.backup.configured_volumes
      ? "No local repository backup data measured yet." : "Add a local backup volume to see repository sizes."});
  if (data.backup.available_volumes < data.backup.configured_volumes) {
    $("#backup-storage-note").textContent += " Some backup volumes could not be measured.";
  }
  const applied = data.runs.filter((run) => run.mode === "apply" && Number.isFinite(run.ended));
  $("#activity-panel").hidden = applied.length === 0;
  if (applied.length) renderActivityChart(data.runs);
  $("#overview-operations").hidden = !(data.active.length || data.runs.length || data.errors.length
    || data.backup.configured_volumes);
  $("#active-count").textContent = String(data.active.length);
  $("#active-list").innerHTML = runItems(data.active, "No active operations.");
  const health = data.backup;
  $("#health").innerHTML = !health.configured_volumes ? empty("No local volumes configured.")
    : list([["Volumes available", `${health.available_volumes} / ${health.configured_volumes}`],
      ["Current verified generations", health.verified], ["Unverified", health.unverified],
      ["Errors", health.failed]]);
  $("#recent").innerHTML = runItems(data.runs.slice(0, 5), "No activity recorded.");
  $("#errors").innerHTML = data.errors.map((problem) => `<div class="item"><div>
    <strong>${escape([problem.repository, problem.destination].filter(Boolean).join(" / ") || "Operation failed")}</strong>
    <p>${escape(problem.detail)}</p><small class="muted">${escape(date(problem.at))}</small></div></div>`).join("")
    || empty("No errors recorded.");
}

function retentionText(policy) {
  if (!policy) return "Keep all generations";
  const days = policy.days ? ` · keep at least ${policy.days} days` : "";
  return `Keep ${policy.keep}${days} · ${policy.automatic ? "automatic on CLI" : "manual cleanup"}`;
}

function renderDestinations(data) {
  $("#destinations").innerHTML = data.destinations.length
    ? data.destinations.map((destination) => `<article class="panel"><header>
    <h2>${escape(destination.id)}</h2><span class="pill">${escape(providerLabel(destination.provider))}</span></header>
    ${list(destination.provider === "local"
      ? [["Backup path", destination.path], ["Retention", retentionText(destination.retention)]]
      : [["Account", destination.owner ?? "Determined on login"], ["API", destination.api_url]])}</article>`).join("")
    : emptyPage("No destinations configured",
      "Configure a remote mirror or local backup location.", "Edit configuration");
}

function renderSettings(data) {
  $("#security-panel").hidden = !data.web.managed;
  $("#edit-configuration").hidden = !data.web.managed;
  $("#check-connections").hidden = !data.web.managed;
  $("#check-connections button").disabled = busy;
  $("#settings").innerHTML = list([["Source", `${providerLabel(data.source.provider)} / ${data.source.owner}`],
    ["Source API", data.source.api_url], ["Schedule interval", `${data.schedule_hours} hours`],
    ["Next observed run", date(data.next_run, "Unavailable")], ["Working path", data.storage.path],
    ["Web actions", data.web.writes_paused ? "Applied sync paused" : data.web.allow_actions ? "Enabled" : "Read-only"],
    ["Web restores", data.web.restore_enabled ? "Enabled" : "Disabled"],
    ["Access", data.web.secure ? "HTTPS secure session" : "HTTP local session"]])
    + (data.web.managed ? "" : empty("This dashboard reads the server’s configuration. Changes take effect after a dashboard restart."));
}

function renderSyncTargets() {
  const signature = snapshot.destinations.map((destination) => destination.id).join(",");
  if (signature !== targetSignature) {
    targetSignature = signature;
    $("#sync-targets").innerHTML = "<legend>Destinations · leave empty for all</legend>"
      + snapshot.destinations.map(({id}) => `<label class="check"><input type="checkbox" name="destination"
        value="${escape(id)}">${escape(id)}</label>`).join("");
  }
  $("#actions-help").textContent = snapshot.web.allow_actions ? (busy ? "A dashboard operation is active." :
    "Uses the existing GitMirror CLI and its locks.") : "Dashboard actions are disabled on this server.";
  $("#sync-submit").disabled = !snapshot.web.allow_actions || busy;
  syncMode();
}

function statusFor(repos) {
  if (repos.some((result) => ["failed", "blocked"].includes(result.state))) return "failed";
  if (repos.some((result) => result.state === "succeeded")) return "succeeded";
  return repos.length ? repos[0].state : "unobserved";
}

function renderRepositories() {
  $("#repository-workspace").hidden = false;
  $("#repository-onboarding").hidden = true;
  const query = $("#repo-search").value.trim().toLowerCase();
  const visibility = $("#repo-filter").value, status = $("#status-filter").value;
  const filtered = repositories.filter((repo) => repo.name.toLowerCase().includes(query)
    && (!visibility || repo.visibility === visibility)
    && (!status || statusFor(repo.destinations) === status));
  $("#repositories").innerHTML = filtered.length ? table(["Repository", "Source size", "Visibility", "Default branch", "Sync status", "Last backup"],
    filtered.map((repo) => row(button("details", repo.id, repo.name), escape(size(repo.size_bytes)),
      escape(repo.visibility), `<code>${escape(repo.default_branch || "Unconfirmed")}</code>`,
      pill(statusFor(repo.destinations)), escape(date(repo.last_backup)))))
    : empty(repositories.length ? "No repositories match your filters." : "No repositories loaded. Click Read source inventory to fetch them.");
}

function openRepository(id) {
  const repo = repositories.find((item) => String(item.id) === id);
  if (!repo) return;
  $("#details-title").textContent = repo.name;
  const destinations = snapshot.destinations.map((destination) => {
    const result = repo.destinations.find((item) => item.destination === destination.id);
    return row(escape(destination.id), pill(result?.state), escape(date(result?.at)), escape(result?.detail || ""));
  });
  $("#details-content").innerHTML = list([["Visibility", repo.visibility], ["Default branch", repo.default_branch || "Unconfirmed"],
    ["Reported source size", size(repo.size_bytes)], ["Last verified backup", date(repo.last_backup)]]) + `<div class="table-wrap">${table(["Destination", "Status", "Checked", "Detail"], destinations)}</div>`
    + button("prepare-sync", repo.id, "Prepare manual synchronization", !snapshot.web.allow_actions);
  $("#details-dialog").showModal();
}

function renderBackups(data) {
  const opened = new Set(all("#backups details[open]").map((element) => element.dataset.key));
  targets = [];
  $("#backups").innerHTML = data.destinations.map((volume) => `<article class="panel"><header><div><h2>${escape(volume.id)}</h2>
    <p class="mono">${escape(volume.path)}</p></div>${pill(volume.error ? "failed" : "Available")}</header>
    ${volume.error ? `<p class="error">${escape(volume.error)}</p>` : list([["Logical storage", size(volume.used)], ["Free space", size(volume.free)]])}
    ${volume.repositories.map((repo) => {
      const key = `${volume.id}/${repo.id}`;
      const generations = repo.generations.map((generation) => {
        const index = targets.push({repo: repo.id, name: repo.name, destinations: [volume.id], generation: generation.id}) - 1;
        return row(`<code>${escape(generation.id)}</code>${generation.current ? " · Current" : ""}`,
          pill(generation.integrity), escape(date(generation.verified)), escape(date(generation.created)),
          `${button("verify", index, "Verify", !snapshot.web.allow_actions || busy)} ·
           ${button("restore", index, "Restore", !snapshot.web.allow_actions || !snapshot.web.restore_enabled || busy)}`);
      });
      return `<details data-key="${escape(key)}" ${opened.has(key) ? "open" : ""}>
        <summary>${escape(repo.name)} <small class="muted">· ${size(repo.logical_bytes)} logical · ${repo.generations.length} generations</small></summary>
        ${repo.error ? `<p class="error">${escape(repo.error)}</p>` : ""}
        <div class="table-wrap">${table(["Generation", "Integrity", "Last check", "Created", "Actions"], generations)}</div>
        </details>`;
    }).join("") || empty("No backup generations observed.")}</article>`).join("") || emptyPage("No backup volumes yet", "Add a local destination to store recoverable Git backups.", "Edit configuration");
}

function renderActivity() {
  const filter = $("#activity-filter").value;
  const runs = snapshot.runs.filter((run) => !filter || filter === run.origin);
  $("#activity").innerHTML = runs.length ? table(["Started", "Operation", "Origin", "Repository", "Duration", "Result"],
    runs.map((run) => row(escape(date(run.started)), button("log", run.id, runName(run)), escape(origins[run.origin] || run.origin),
      escape(run.repository || "All"), escape(duration(run)), pill(run.state)))) : empty("No matching activity.");
}

async function loadLog() {
  if (!currentRun || view !== "activity") return;
  const sequence = ++logRequest;
  const result = await api(`/api/runs/${encodeURIComponent(currentRun)}/log`);
  if (sequence !== logRequest || view !== "activity") return;
  $("#log-panel").hidden = false;
  $("#log-status").outerHTML = pill(snapshot.runs.find((run) => run.id === result.id)?.state ?? result.state)
    .replace('<span ', '<span id="log-status" ');
  $("#log-progress").textContent = result.error || (result.progress ?
    `${result.progress.repository} · ${result.progress.index} of ${result.progress.total} repositories` : "");
  $("#log-results").innerHTML = result.results.length ? table(["Repository", "Target", "Result", "Detail"],
    result.results.map((item) => row(escape(item.repository || "Run"), escape(item.destination), pill(item.state), escape(item.detail)))) : "";
  const log = $("#log");
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 24;
  log.textContent = result.log || "No redacted output is available for this run.";
  if (atBottom) log.scrollTop = log.scrollHeight;
}

async function warmupInventory() {
  if (inventoryWarmupAttempted || !csrf || previewMode || !snapshot) return;
  const rows = snapshot.repository_storage?.repositories ?? [];
  if (snapshot.inventory_at && (!rows.length ||
      rows.some((repo) => Number.isSafeInteger(repo.bytes)))) return;
  inventoryWarmupAttempted = true;
  try {
    await post("/api/repositories/refresh", {});
    await refresh();
  } catch {
    if (view === "overview") {
      $("#source-storage-chart").innerHTML = chartPlaceholder(
        "Source inventory unavailable. Open Repositories to retry.");
    }
  }
}

async function refresh() {
  if (previewMode || !csrf) return;
  if (loading) { refreshPending = true; return; }
  loading = true;
  try {
    snapshot = await api("/api/overview");
    busy = snapshot.active.some((run) => run.origin === "web");
    updateSystemStatus(snapshot);
    renderContext();
    switch (view) {
      case "overview":
        renderOverview(snapshot);
        if (!inventoryWarmupAttempted) warmupInventory();
        break;
      case "destinations": renderDestinations(snapshot); break;
      case "settings": renderSettings(snapshot); if (snapshot.web.managed) await loadSecurity(); break;
      case "activity": renderActivity(); await loadLog(); break;
      case "backups": renderBackups(await api("/api/backups")); break;
      case "repositories": {
        const inventory = await api("/api/repositories");
        repositories = inventory.repositories || [];
        $("#inventory-time").textContent = `Inventory checked: ${date(inventory.checked)}`;
        renderRepositories();
        renderSyncTargets();
        break;
      }
    }
    connected("Web connected");
  } finally {
    loading = false;
    if (refreshPending) {
      refreshPending = false;
      refresh().catch(report);
    }
  }
}

function connect() {
  stream?.close();
  stream = new EventSource("/api/events");
  stream.onmessage = () => { if (!document.hidden) refresh().catch(report); };
  stream.onopen = () => connected("Web connected");
  stream.onerror = () => connected("Reconnecting");
  stream.addEventListener("expired", signOut);
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => { if (!document.hidden) refresh().catch(report); }, 60000);
}

async function boot() {
  const session = await api("/api/session");
  csrf = session.csrf;
  previewMode = false;
  initialSetupData = null;
  inventoryWarmupAttempted = false;
  $("#login").hidden = true;
  $("#shell").hidden = false;
  $("#setup-banner").hidden = true;
  $("#settings-preview").hidden = true;
  $("#live-settings").hidden = false;
  $("#settings-editor").hidden = false;
  $("#logout").hidden = false;
  $("#refresh").disabled = false;
  $("#discover").disabled = false;
  $("#top-setup").textContent = "Settings";
  $("#system-state").innerHTML = '<span class="status-dot"></span> Loading status';
  $("#system-detail").textContent = "Checking source and destinations";
  initializeNavigation();
  openView(routeFromUrl(), {updateUrl: false});
  connect();
}

function syncMode() {
  const apply = $("#sync-mode").value === "apply";
  $("#sync-confirm-label").hidden = !apply;
  $("#sync-confirm").required = apply;
  $("#sync-required").textContent = `Type ${$("#sync-repo").value.trim() ? "SYNC" : "SYNC ALL"} to confirm`;
  $("#sync-submit").textContent = apply ? "Apply synchronization" : "Start dry-run";
}

async function afterStart(job) {
  currentRun = job.id;
  openView("activity");
  notify("Operation queued.");
}

$("#nav-toggle").addEventListener("click", toggleNavigation);
$("#sidebar-backdrop").addEventListener("click", closeMobileNavigation);
window.addEventListener("popstate", () => {
  if (!$("#shell").hidden) openView(routeFromUrl(), {updateUrl: false});
});
window.addEventListener("hashchange", () => {
  if (!$("#shell").hidden && routeFromUrl() !== view) openView(routeFromUrl(), {updateUrl: false});
});
mobileNavigation.addEventListener("change", () => {
  $("#shell").classList.toggle("nav-collapsed", compactNavigation && !mobileNavigation.matches);
  closeMobileNavigation();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeMobileNavigation();
});

document.addEventListener("click", (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.view || target.dataset.go) openView(target.dataset.view || target.dataset.go);
  if (target.hasAttribute("data-setup-open")) {
    if (previewMode) showInitialSetup();
    else {
      openView("settings");
      if (snapshot?.web?.managed) $("#edit-configuration").click();
    }
  }
  if (target.dataset.close) $(`#${target.dataset.close}`).close();
  const value = target.dataset.value;
  switch (target.dataset.action) {
    case "log": currentRun = value; openView("activity"); break;
    case "details": openRepository(value); break;
    case "prepare-sync": {
      $("#sync-repo").value = repositories.find((repo) => String(repo.id) === value).name;
      $("#details-dialog").close();
      $("#sync-mode").focus();
      $("#sync-form").scrollIntoView({block: "nearest"});
      syncMode();
      break;
    }
    case "verify": post("/api/backups/verify", targets[Number(value)]).then(afterStart).catch(report); break;
    case "restore":
      restoreSelection = targets[Number(value)];
      $("#restore-summary").textContent = `${restoreSelection.name} · ${restoreSelection.generation}`;
      $("#restore-error").textContent = "";
      $("#restore-form").reset();
      $("#restore-dialog").showModal();
      break;
  }
});

function showFirstPassword() {
  $("#shell").hidden = true;
  $("#login").hidden = false;
  $("#login-form").hidden = true;
  $("#mfa-form").hidden = true;
  $("#first-password-form").hidden = false;
  $("#first-password").focus();
}

function showSecondFactor() {
  $("#login-form").hidden = true;
  $("#first-password-form").hidden = true;
  $("#mfa-form").hidden = false;
  $("#mfa-code").value = "";
  $("#mfa-code").focus();
}

async function finishLogin() {
  const status = await api("/api/session");
  csrf = status.csrf;
  if (status.first_login) {
    showFirstPassword();
    return;
  }
  // A completed setup may have restarted the server since the login page was opened.
  const response = await fetch("/api/setup", {credentials: "same-origin"});
  if (response.ok) {
    const setup = await response.json();
    if (setup.required) {
      enterPreview(setup);
      return;
    }
  }
  initialSetupData = null;
  await boot();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const control = event.currentTarget.querySelector("button");
  control.disabled = true;
  $("#login-error").textContent = "";
  try {
    const result = await post("/api/login", {password: $("#login-password").value});
    $("#login-password").value = "";
    if (result.mfa_required) showSecondFactor();
    else await finishLogin();
  } catch (error) { $("#login-error").textContent = error.message; }
  finally { control.disabled = false; }
});

$("#first-password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const password = $("#first-password").value;
  $("#first-password-error").textContent = "";
  if (password !== $("#first-confirm").value) {
    $("#first-password-error").textContent = "Passwords do not match.";
    return;
  }
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await post("/api/setup/password", {password});
    $("#first-password").value = $("#first-confirm").value = "";
    await finishLogin();
  } catch (error) { $("#first-password-error").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#mfa-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  $("#mfa-error").textContent = "";
  try {
    await post("/api/login/2fa", {code: $("#mfa-code").value.trim()});
    $("#mfa-code").value = "";
    await finishLogin();
  } catch (error) { $("#mfa-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("#mfa-back").addEventListener("click", () => {
  $("#mfa-form").hidden = true;
  $("#login-form").hidden = false;
  $("#login-password").focus();
});

$("#logout").addEventListener("click", () => post("/api/logout", {}).then(signOut).catch(report));
$("#refresh").addEventListener("click", () => refresh().catch(report));
$("#discover").addEventListener("click", async () => {
  $("#discover").disabled = true;
  try {
    await post("/api/repositories/refresh", {});
    await refresh();
  } catch (error) { report(error); }
  finally { $("#discover").disabled = false; }
});
$("#sync-repo").addEventListener("input", syncMode);
$("#sync-mode").addEventListener("change", () => { $("#sync-confirm").value = ""; syncMode(); });
$("#repo-search").addEventListener("input", renderRepositories);
$("#repo-filter").addEventListener("change", renderRepositories);
$("#status-filter").addEventListener("change", renderRepositories);
$("#activity-filter").addEventListener("change", renderActivity);

$("#sync-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#sync-submit").disabled = true;
  try {
    const destinations = all("#sync-targets input:checked").map((field) => field.value);
    const job = await post("/api/sync", {repo: $("#sync-repo").value.trim(), apply: $("#sync-mode").value === "apply",
      confirmation: $("#sync-confirm").value.trim(), destinations});
    await afterStart(job);
  } catch (error) { report(error); renderSyncTargets(); }
});
$("#restore-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button");
  submit.disabled = true;
  try {
    const job = await post("/api/backups/restore", {...restoreSelection, output: $("#restore-name").value.trim(),
      confirmation: $("#restore-confirm").value.trim(), include_archive: $("#restore-archive").checked});
    $("#restore-dialog").close();
    await afterStart(job);
  } catch (error) { $("#restore-error").textContent = error.message; }
  finally { submit.disabled = false; }
});
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) refresh().catch(report);
});

const providerLabels = {github: "GitHub", gitlab: "GitLab", codeberg: "Codeberg", gitea: "Gitea", local: "Local backup"};
const providerLabel = (id) => Object.hasOwn(providerLabels, id) ? providerLabels[id] : id;

const accountCards = new WeakMap();
const oauthPath = "/api/managed/oauth";
const accountDate = (value) => value ? new Date(value * 1000).toLocaleString() : "—";

function accountCard(fieldset, spec, destination, setupData) {
  const card = fieldset.querySelector(".account-connection");
  const control = (name) => fieldset.querySelector('[name="' + name + '"]');
  const element = (name) => card.querySelector("[data-account-" + name + "]");
  const [provider, owner, apiUrl, token] = ["provider", "owner", "api_url", "token"].map(control);
  const [connect, disconnect, check, manual, browser, device, launch, cancel, copy, permissions] =
    ["connect", "disconnect", "check", "manual", "browser", "device", "launch", "cancel", "copy", "permissions"].map(element);
  const context = () => ({provider: provider.value, api_url: apiUrl.value.trim() || null,
    owner: owner.value.trim(), role: destination ? "destination" : "source", code: setupData()});
  const states = {connected: "Connected", refresh_due: "Renewal due", disconnected: "Disconnected",
    reconnect_required: "Reconnect required"};
  let connection = spec.connection;
  let manualMode = !connection && Boolean(spec.owner);
  let options, flow, busy = false;

  function render() {
    const remote = provider.value !== "local";
    const active = Boolean(connection && !manualMode);
    const grant = flow && ["starting", "awaiting", "working"].includes(flow.status);
    card.hidden = !remote;
    token.closest("label").hidden = !manualMode;
    token.disabled = !remote || !manualMode;
    provider.disabled = active || busy || !!grant;
    apiUrl.readOnly = owner.readOnly = active || busy || !!grant;
    element("name").textContent = providerLabel(provider.value);
    element("status").textContent = active ? states[connection.status] ?? "Check required" :
      manualMode ? "Manual API token" : "Account not connected";
    element("metadata").hidden = !active;
    if (active) {
      element("account").textContent = connection.account;
      element("expiry").textContent = accountDate(connection.expires_at);
      element("checked").textContent = accountDate(connection.last_checked);
    }
    connect.textContent = active ? "Reconnect" : "Connect account";
    connect.disabled = busy || !!grant || location.protocol !== "https:";
    disconnect.hidden = !active || connection.status === "disconnected";
    check.hidden = !active || !["connected", "refresh_due"].includes(connection.status);
    for (const button of [disconnect, check, manual]) button.disabled = busy || !!grant;
    manual.textContent = manualMode ? "Use browser authorization" : "Configure manually (API token)";
    const permissionsUrl = options?.permissions_url ?? (active ? connection.permissions_url : null);
    permissions.hidden = !permissionsUrl;
    if (permissionsUrl) permissions.href = permissionsUrl;
    const callback = options?.callback || (provider.value !== "github" && location.protocol === "https:" ?
      location.origin + oauthPath + "/" + provider.value + "/callback" : null);
    element("callback").hidden = !callback;
    element("redirect").textContent = callback || "";
    browser.hidden = !!grant || !options?.methods?.includes("browser");
    device.hidden = !!grant || !options?.methods?.includes("device");
    browser.disabled = device.disabled = busy;
    element("consent").hidden = !!grant || !options?.full_access;
    element("grant").hidden = !grant;
    launch.hidden = !flow?.url;
    if (flow?.url) {
      launch.href = flow.url;
      launch.textContent = flow.method === "device" ? "Open provider verification page" : "Open provider authorization page";
    }
    element("code").textContent = flow?.user_code || "";
    copy.hidden = !flow?.user_code || !navigator.clipboard;
    element("deadline").textContent = flow?.expires_at ? "Approval expires " + accountDate(flow.expires_at) : "";
    cancel.disabled = busy;
  }

  async function action(task) {
    busy = true;
    element("message").textContent = "";
    render();
    try { await task(); }
    catch (error) { element("message").textContent = error.message; }
    finally { busy = false; render(); }
  }

  async function monitor(identifier) {
    try {
      while (fieldset.isConnected && flow?.flow === identifier) {
        await new Promise((resolve) => setTimeout(resolve, 1800));
        if (!fieldset.isConnected || flow?.flow !== identifier) return;
        const result = await post(oauthPath + "/flow/" + identifier + "/status");
        if (flow?.flow !== identifier) return;
        flow = result;
        if (result.status === "connected") {
          connection = result.connection;
          owner.value = connection.account;
          apiUrl.value = connection.api_url;
          token.value = "";
          manualMode = false;
          options = flow = null;
          element("message").textContent = "Provider API access checked. Complete SSH setup for Git transfers.";
          render();
          return;
        }
        if (result.status === "failed" || result.status === "cancelled") {
          flow = null;
          element("message").textContent = result.message;
          render();
          return;
        }
        render();
      }
    } catch (error) {
      if (flow?.flow !== identifier) return;
      flow = null;
      element("message").textContent = error.message;
      render();
    }
  }

  connect.addEventListener("click", () => action(async () => {
    options = null;
    try { options = await post(oauthPath + "/options", context()); }
    finally {
      if (!options?.methods?.length) element("callback").open = true;
    }
    const message = !options.methods.length ? "Register the exact HTTPS callback displayed below." :
      provider.value === "github" ? "Install the GitHub App on this account and grant repository permissions." :
      options.scopes ? "Requested API permissions " + options.scopes : "Review the provider’s access request.";
    element("message").textContent = message;
  }));
  function start(method) {
    action(async () => {
      flow = await post(oauthPath + "/start", {...context(), method,
        ...(connection && !manualMode ? {connection: connection.id} : {}),
        full_access_confirmed: element("confirm").checked});
      monitor(flow.flow);
    });
  }
  browser.addEventListener("click", () => start("browser"));
  device.addEventListener("click", () => start("device"));
  cancel.addEventListener("click", () => action(async () => {
    const id = flow.flow;
    const result = await post(oauthPath + "/flow/" + id + "/cancel");
    flow = null;
    element("message").textContent = result.message || "Approval already completed. Check the account connection.";
    if (result.connection) {
      connection = result.connection;
      owner.value = connection.account;
      apiUrl.value = connection.api_url;
      token.value = "";
      manualMode = false;
    }
  }));
  copy.addEventListener("click", () => action(async () => {
    await navigator.clipboard.writeText(flow.user_code);
    element("message").textContent = "User code copied.";
  }));
  disconnect.addEventListener("click", () => {
    if (!window.confirm("Disconnect this account? Syncs using this connection will stop until it is reconnected.")) return;
    action(async () => {
      const result = await post(oauthPath + "/connection/" + connection.id + "/disconnect", {code: setupData()});
      connection = result.connection;
      element("message").textContent = result.message;
    });
  });
  check.addEventListener("click", () => action(async () => {
    const result = await post(oauthPath + "/connection/" + connection.id + "/check", {code: setupData()});
    connection = result.connection;
    element("message").textContent = result.message;
  }));
  manual.addEventListener("click", () => {
    manualMode = !manualMode;
    if (!manualMode) {
      token.value = "";
      if (connection) {
        provider.value = connection.provider;
        apiUrl.value = connection.api_url;
        owner.value = connection.account;
      }
    }
    options = null;
    element("message").textContent = manualMode ? "Enter an API token, or keep your existing manual credential if it applies." : "";
    render();
  });
  for (const input of [provider, owner, apiUrl]) input.addEventListener("input", () => {
    options = null;
    element("confirm").checked = false;
    element("message").textContent = "";
    render();
  });
  if (location.protocol !== "https:") element("message").textContent = "Browser authorization requires HTTPS.";
  accountCards.set(fieldset, {render, connection: () => manualMode ? null : connection,
    method: () => manualMode ? "token" : "oauth", pending: () => busy || Boolean(flow)});
  render();
}

function providerInput(spec = {}, destination = false, setupData) {
  const fieldset = document.createElement("fieldset");
  const choices = Object.entries(providerLabels).filter(([id]) => destination || id !== "local");
  fieldset.innerHTML = `
    ${destination ? '<div class="target-head"><button type="button" class="link remove-target">Remove</button></div>' : ""}
    <div class="action-form">
      ${destination ? '<label>Destination ID<input name="id" pattern="[a-z][a-z0-9_\\-]{0,62}" maxlength="63" required></label>' : ""}
      <label>Provider<select name="provider">${choices.map(([id, label]) => `<option value="${id}">${label}</option>`).join("")}</select></label>
    </div>
    <div class="action-form remote-section">
      <label>Account<input name="owner" maxlength="100" autocomplete="off"></label>
      <label><span data-api-label>API URL (optional)</span><input name="api_url" type="url" autocomplete="off" placeholder="Provider default"></label>
      <label>API token<input name="token" type="password" autocomplete="new-password" maxlength="4096"></label>
    </div>
    <section class="account-connection" aria-label="Provider account">
      <div class="account-heading"><strong data-account-name></strong><span class="muted" data-account-status></span></div>
      <dl class="data" data-account-metadata hidden>
        <dt>Account</dt><dd data-account-account></dd>
        <dt>Token expiry</dt><dd data-account-expiry></dd>
        <dt>Last successful authorization check</dt><dd data-account-checked></dd>
      </dl>
      <div class="account-actions">
        <button type="button" class="button secondary" data-account-connect>Connect account</button>
        <button type="button" class="link" data-account-check hidden>Check access</button>
        <button type="button" class="link" data-account-disconnect hidden>Disconnect</button>
        <a data-account-permissions target="_blank" rel="noopener noreferrer" hidden>Manage permissions</a>
        <button type="button" class="link" data-account-manual>Configure manually (API token)</button>
      </div>
      <details data-account-callback hidden><summary>App registration and callback URI</summary><code data-account-redirect></code></details>
      <label class="check" data-account-consent hidden><input type="checkbox" data-account-confirm>
        I approve Codeberg OAuth’s account-wide access.</label>
      <div class="account-actions">
        <button type="button" class="button secondary" data-account-device hidden>Start device approval</button>
        <button type="button" class="button secondary" data-account-browser hidden>Start browser approval</button>
      </div>
      <div data-account-grant hidden>
        <div class="account-actions">
          <a data-account-launch target="_blank" rel="noopener noreferrer"></a>
          <code data-account-code></code><button type="button" class="link" data-account-copy>Copy code</button>
          <button type="button" class="link" data-account-cancel>Cancel approval</button>
        </div>
        <small data-account-deadline></small>
      </div>
      <div class="muted" role="status" aria-live="polite" data-account-message></div>
    </section>
    <div class="action-form local-section">
      <label>Backup directory<input name="path" placeholder="Use managed backup directory"></label>
      <label>Minimum generations to keep<input name="keep" type="number" min="1" step="1" placeholder="Keep all"></label>
      <label>Minimum age in days<input name="days" type="number" min="1" step="1" placeholder="Optional"></label>
      <label class="check"><input name="automatic" type="checkbox">Apply retention on scheduled syncs</label>
    </div>`;
  const control = (name) => fieldset.querySelector(`[name="${name}"]`);
  control("provider").value = spec.provider ?? (destination ? "local" : "github");
  for (const key of ["id", "owner", "api_url", "path"]) {
    if (control(key)) control(key).value = spec[key] ?? "";
  }
  if (spec.retention) {
    control("keep").value = spec.retention.keep ?? "";
    control("days").value = spec.retention.days ?? "";
    control("automatic").checked = spec.retention.automatic;
  }
  accountCard(fieldset, spec, destination, setupData);
  function syncFields() {
    const local = control("provider").value === "local";
    const localFields = fieldset.querySelector(".local-section");
    const remoteFields = fieldset.querySelector(".remote-section");
    localFields.hidden = !local;
    remoteFields.hidden = local;
    for (const input of localFields.querySelectorAll("input")) input.disabled = !local;
    for (const input of remoteFields.querySelectorAll("input")) input.disabled = local;
    control("owner").required = !local;
    const gitea = control("provider").value === "gitea";
    control("api_url").required = gitea;
    control("api_url").placeholder = gitea ? "https://git.example.com/api/v1" : "Provider default";
    fieldset.querySelector("[data-api-label]").textContent = gitea ? "Gitea API URL" : "API URL (optional)";
    control("keep").required = local && (control("days").value !== "" || control("automatic").checked);
    accountCards.get(fieldset).render();
  }
  fieldset.addEventListener("input", syncFields);
  fieldset.addEventListener("change", syncFields);
  fieldset.querySelector(".remove-target")?.addEventListener("click", () => fieldset.remove());
  syncFields();
  return fieldset;
}

function readProvider(fieldset) {
  const control = (name) => fieldset.querySelector(`[name="${name}"]`);
  const clean = {provider: control("provider").value};
  if (control("id")) clean.id = control("id").value.trim();
  if (clean.provider === "local") {
    clean.path = control("path").value.trim() || null;
    if (control("keep").value) {
      clean.retention = {keep: Number(control("keep").value), days: Number(control("days").value) || null,
        automatic: control("automatic").checked};
    }
  } else {
    clean.owner = control("owner").value.trim();
    clean.api_url = control("api_url").value.trim() || null;
    clean.token = control("token").value.trim();
    const card = accountCards.get(fieldset);
    const connection = card.connection();
    clean.auth_method = card.method();
    if (connection) clean.oauth_id = connection.id;
  }
  return clean;
}

async function waitForConfiguration(revision) {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch("/api/setup", {credentials: "same-origin"});
      if (response.ok && (await response.json()).revision === revision) return;
    } catch {
      // The listener briefly restarts when a new validated configuration is published.
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("Configuration was saved. Reload this page when the server is available.");
}

function openConfiguration(initial, data) {
  const wrapper = initial ? $("#setup-editor") : $("#settings-editor");
  wrapper.replaceChildren($("#configuration-template").content.cloneNode(true));
  const form = wrapper.querySelector("form");
  let showWizardStep = null;
  const sectionNames = initial ? ["Source", "Destinations", "SSH keys", "Schedule"] :
    ["Administrator", "Source", "Destinations", "SSH keys", "Schedule"];
  const sections = [...form.querySelectorAll(":scope > article.panel")].slice(initial ? 1 : 0);
  form.querySelector(".admin-settings").hidden = initial;
  const index = document.createElement("nav");
  index.className = "configuration-nav";
  index.setAttribute("aria-label", "Configuration sections");
  for (const [position, section] of sections.entries()) {
    section.classList.add("configuration-section");
    const link = document.createElement("button");
    link.type = "button";
    link.className = "configuration-jump";
    link.textContent = sectionNames[position];
    link.addEventListener("click", () => {
      if (initial && showWizardStep) { showWizardStep(position); return; }
      for (const item of index.querySelectorAll("button")) item.removeAttribute("aria-current");
      link.setAttribute("aria-current", "step");
      section.scrollIntoView({behavior: "smooth", block: "start"});
    });
    if (position === 0) link.setAttribute("aria-current", "step");
    index.append(link);
  }
  form.prepend(index);
  form.querySelector(".setup-code").hidden = true;
  form.querySelector(".current-password").hidden = initial;
  form.querySelector(".new-password").hidden = initial;
  form.elements.code.required = false;
  form.elements.password.required = false;
  form.elements.hours.value = data.schedule?.interval_hours ?? 12;
  form.elements.enabled.checked = data.schedule?.enabled ?? false;
  form.elements.repositories.value = (data.repositories ?? []).join("\n");
  form.elements.known_hosts.value = data.known_hosts ?? "";
  form.querySelector("[data-public-key]").textContent = data.public_key;
  const setupCode = () => form.elements.code.value.trim();
  const sourceCard = providerInput(data.source, false, setupCode);
  form.querySelector(".source-fields").append(sourceCard);
  const destinations = form.querySelector(".target-fields");
  const addTarget = (dest) => destinations.append(providerInput(dest, true, setupCode));
  for (const dest of data.destinations ?? [{id: "local", provider: "local"}]) addTarget(dest);
  form.querySelector(".add-target").addEventListener("click", () => addTarget({}));
  form.querySelector(".verify-host").addEventListener("click", async (event) => {
    const trigger = event.currentTarget;
    const feedback = form.querySelector("[data-host-status]");
    trigger.disabled = true;
    try {
      const result = await post("/api/managed/ssh-host", {code: form.elements.code.value.trim(),
        host: form.elements.ssh_host.value.trim(), port: Number(form.elements.ssh_port.value),
        fingerprint: form.elements.ssh_fingerprint.value.trim()});
      const entries = form.elements.known_hosts.value.split("\n").map((line) => line.trim()).filter(Boolean);
      form.elements.known_hosts.value = [...new Set([...entries, result.known_host])].join("\n");
      feedback.textContent = "Host key verified and added.";
    } catch (failure) { feedback.textContent = failure.message; }
    finally { trigger.disabled = false; }
  });
  if (initial) {
    form.classList.add("setup-wizard");
    form.noValidate = true;
    const review = document.createElement("section");
    review.className = "wizard-review panel";
    review.hidden = true;
    review.innerHTML = '<header><h2>Review configuration</h2></header>' +
      '<p class="muted">Check your choices before activating GitMirror. Secrets are never displayed here.</p>' +
      '<dl class="data wizard-review-data"></dl>';
    const footer = form.querySelector(":scope > .form-footer");
    form.insertBefore(review, footer);
    const controls = document.createElement("div");
    controls.className = "wizard-controls";
    controls.innerHTML = '<span class="wizard-progress" role="status" aria-live="polite"></span>' +
      '<div class="wizard-buttons"><button type="button" class="button secondary wizard-previous">Back</button>' +
      '<button type="button" class="button primary wizard-next">Continue</button></div>';
    form.insertBefore(controls, footer);
    const reviewLink = document.createElement("button");
    reviewLink.type = "button";
    reviewLink.className = "configuration-jump";
    reviewLink.textContent = "Review";
    reviewLink.addEventListener("click", () => {
      if (showWizardStep) showWizardStep(sections.length);
    });
    index.append(reviewLink);
    let current = 0, furthest = 0;
    const choices = [...index.querySelectorAll("button")];
    const validate = (section) => {
      for (const element of section.querySelectorAll("input, select, textarea")) {
        if (element.disabled || element.closest("[hidden]")) continue;
        if (!element.checkValidity()) {
          element.reportValidity();
          return false;
        }
      }
      return true;
    };
    showWizardStep = (requested) => {
      if (requested > furthest || requested < 0 || requested > sections.length) return;
      current = requested;
      sections.forEach((section, i) => { section.hidden = i !== current; });
      review.hidden = current !== sections.length;
      footer.classList.toggle("wizard-footer-visible", current === sections.length);
      controls.querySelector(".wizard-previous").disabled = current === 0;
      controls.querySelector(".wizard-next").hidden = current === sections.length;
      controls.querySelector(".wizard-progress").textContent =
        `Step ${current + 1} of ${sections.length + 1}`;
      choices.forEach((choice, i) => {
        if (i === current) choice.setAttribute("aria-current", "step");
        else choice.removeAttribute("aria-current");
        choice.disabled = i > furthest;
      });
      if (current === sections.length) {
        const values = [
          ...[sourceCard, ...destinations.querySelectorAll("fieldset")].map((card, i) => {
            const spec = readProvider(card);
            const connection = accountCards.get(card).connection();
            const label = i ? "Destination " + (spec.id || i) : "Source";
            if (spec.provider === "local") return [label, providerLabel(spec.provider)];
            return [label, providerLabel(spec.provider) + " · " + spec.owner + " · " +
              (connection ? connection.status === "connected" ? "API authorized" : "API check required" : "Manual API token")];
          }),
          ["SSH public key", form.elements.ssh_key_added.checked ? "Installation confirmed by administrator" : "Install the public key before syncing"],
          ["SSH host keys", form.elements.known_hosts.value.trim() ? "Entries supplied. SSH authentication still needs a connection check." : "Verification required"],
          ["Schedule interval", `${form.elements.hours.value} hours`],
          ["Automatic synchronization", form.elements.enabled.checked ? "Enabled" : "Disabled"],
        ];
        const summary = review.querySelector(".wizard-review-data");
        summary.replaceChildren();
        for (const [label, value] of values) {
          const dt = document.createElement("dt"), dd = document.createElement("dd");
          dt.textContent = label; dd.textContent = value;
          summary.append(dt, dd);
        }
      }
      $("#setup").scrollTop = 0;
    };
    controls.querySelector(".wizard-previous").addEventListener("click", () => showWizardStep(current - 1));
    controls.querySelector(".wizard-next").addEventListener("click", () => {
      if (current < sections.length && validate(sections[current])) {
        furthest = Math.max(furthest, current + 1);
        showWizardStep(current + 1);
      }
    });
    form.addEventListener("submit", (event) => {
      // Pressing Enter on an earlier step must not bypass the review screen.
      if (current !== sections.length) {
        event.preventDefault();
        event.stopImmediatePropagation();
        if (validate(sections[current])) {
          furthest = Math.max(furthest, current + 1);
          showWizardStep(current + 1);
        }
        return;
      }
      for (let i = 0; i < sections.length; i++) {
        sections[i].hidden = false;
        const valid = validate(sections[i]);
        sections[i].hidden = true;
        if (!valid) {
          event.preventDefault();
          event.stopImmediatePropagation();
          furthest = Math.max(furthest, i);
          showWizardStep(i);
          const target = sections[i].querySelector(":invalid");
          target?.reportValidity();
          return;
        }
      }
    }, {capture: true});
    showWizardStep(0);
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("button[type=submit]");
    const error = form.querySelector("[data-config-error]");
    if ([sourceCard, ...destinations.querySelectorAll("fieldset")].some((card) => accountCards.get(card).pending())) {
      error.textContent = "Finish or cancel provider approval before saving.";
      return;
    }
    submit.disabled = true;
    error.textContent = "";
    const configuration = {
      code: form.elements.code.value.trim(),
      password: form.elements.password.value,
      current_password: form.elements.current_password.value,
      revision: data.revision,
      source: readProvider(form.querySelector(".source-fields fieldset")),
      destinations: [...destinations.querySelectorAll("fieldset")].map(readProvider),
      known_hosts: form.elements.known_hosts.value,
      repositories: form.elements.repositories.value.split("\n").map((name) => name.trim()).filter(Boolean),
      schedule: {enabled: form.elements.enabled.checked, interval_hours: Number(form.elements.hours.value)},
    };
    try {
      const saved = await post(initial ? "/api/setup" : "/api/managed/settings", configuration);
      error.textContent = "Saved. Reloading the dashboard…";
      stream?.close();
      await waitForConfiguration(saved.revision);
      wrapper.replaceChildren();
      if ($("#setup").open) $("#setup").close();
      // The managed service reloads its validated configuration after sending the response.
      signOut();
      $("#login-error").textContent = "Configuration saved. Sign in with the admin password.";
    } catch (failure) {
      error.textContent = failure.message;
      submit.disabled = false;
    }
  });
  if (!initial) wrapper.scrollIntoView({block: "start"});
}

async function loadSecurity() {
  if (securityEnrollment || securityRecovery) return;
  const state = await api("/api/security");
  $("#security-state").textContent = state.enabled ? "Enabled" : "Disabled";
  $("#security-start").hidden = state.enabled;
  $("#security-disable").hidden = !state.enabled;
  $("#security-enroll").hidden = true;
  $("#security-recovery").hidden = true;
}

$("#security-start").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  $("#security-error").textContent = "";
  try {
    const result = await post("/api/security/totp/start", {password: $("#security-start-password").value});
    $("#security-start-password").value = "";
    $("#security-secret").textContent = result.secret;
    securityEnrollment = true;
    $("#security-start").hidden = true;
    $("#security-enroll").hidden = false;
    $("#security-state").textContent = "Waiting for confirmation";
  } catch (error) { $("#security-error").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#security-confirm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  $("#security-error").textContent = "";
  try {
    const result = await post("/api/security/totp/confirm", {code: $("#security-confirm-code").value.trim()});
    $("#security-confirm-code").value = "";
    $("#security-secret").textContent = "";
    securityEnrollment = false;
    securityRecovery = true;
    $("#security-enroll").hidden = true;
    $("#security-recovery").hidden = false;
    $("#security-codes").textContent = result.recovery_codes.join("\n");
    $("#security-state").textContent = "Enabled";
  } catch (error) { $("#security-error").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#security-copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("#security-secret").textContent);
    $("#security-error").textContent = "Setup key copied.";
  } catch {
    $("#security-error").textContent = "Copy unavailable. Select the setup key above.";
  }
});

$("#security-cancel").addEventListener("click", async () => {
  try {
    await post("/api/security/totp/cancel", {});
    $("#security-secret").textContent = "";
    securityEnrollment = false;
    await loadSecurity();
  } catch (error) { $("#security-error").textContent = error.message; }
});

$("#security-recovery-done").addEventListener("click", async () => {
  $("#security-codes").textContent = "";
  securityRecovery = false;
  await loadSecurity().catch(report);
});

$("#security-disable").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  $("#security-error").textContent = "";
  try {
    await post("/api/security/totp/disable", {password: $("#security-disable-password").value,
      code: $("#security-disable-code").value.trim()});
    $("#security-disable-password").value = $("#security-disable-code").value = "";
    await loadSecurity();
  } catch (error) { $("#security-error").textContent = error.message; }
  finally { button.disabled = false; }
});

async function startDashboard() {
  const response = await fetch("/api/setup", {credentials: "same-origin"});
  // Standalone dashboards have no managed setup endpoint.
  if (response.status === 404) {
    await boot();
    return;
  }
  if (!response.ok) throw new Error("The GitMirror server is unavailable.");
  const data = await response.json();
  if (data.required) {
    initialSetupData = data;
    try {
      const current = await api("/api/session");
      csrf = current.csrf;
      if (current.first_login) showFirstPassword();
      else enterPreview(data);
    } catch {
      signOut();
      $("#login-label").textContent = data.password_ready ? "Dashboard password" : "Temporary password";
      $("#login-description").textContent = data.password_ready
        ? "Sign in to continue the initial configuration."
        : "Enter the temporary password printed by the GitMirror installer.";
    }
    return;
  }
  await boot();
}

$("#top-setup").addEventListener("click", () => {
  if (previewMode) showInitialSetup();
  else openView("settings");
});
$("#banner-setup").addEventListener("click", showInitialSetup);
$("#settings-start").addEventListener("click", showInitialSetup);
$("#setup-close").addEventListener("click", () => $("#setup").close());

$("#edit-configuration").addEventListener("click", () => api("/api/managed/settings")
  .then((data) => openConfiguration(false, data)).catch(report));
$("#check-connections").addEventListener("submit", async (event) => {
  event.preventDefault();
  const trigger = event.currentTarget.querySelector("button");
  trigger.disabled = true;
  try {
    const job = await post("/api/managed/check", {repo: $("#check-repository").value.trim(), ssh: $("#check-ssh").checked});
    await afterStart(job);
  } catch (failure) {
    report(failure);
    trigger.disabled = false;
  }
});
const oauthReturn = location.hash;
if (["#oauth-connected", "#oauth-failed"].includes(oauthReturn)) history.replaceState(null, "", "/");
startDashboard().then(() => {
  if (oauthReturn === "#oauth-connected") notify("Account authorized. Return to your configuration tab.");
  if (oauthReturn === "#oauth-failed") notify("Authorization failed or expired. Return to your configuration tab.", true);
}).catch(signOut);
