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

function notify(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
}

function connected(status) {
  $("#connection").textContent = status;
  $("#connection").classList.toggle("connected", status === "Live" || status === "Interface online");
}

function signOut() {
  csrf = "";
  stream?.close();
  stream = null;
  clearInterval(refreshTimer);
  refreshTimer = null;
  $("#shell").hidden = true;
  $("#login").hidden = false;
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

function openView(name) {
  if (!Object.hasOwn(pages, name)) return;
  view = name;
  for (const item of all("nav [data-view]")) {
    if (item.dataset.view === view) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  }
  for (const page of all(".content > section")) page.hidden = page.id !== `view-${view}`;
  $("#page-title").textContent = pages[view];
  $("#topbar-location").textContent = pages[view];
  if (previewMode) {
    renderPreviewView();
    return;
  }
  renderContext();
  refresh().catch(report);
}

function renderContext() {
  if (!snapshot) return;
  const source = `${snapshot.source.provider} / ${snapshot.source.owner}`;
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

function renderPreviewView() {
  const messages = {
    overview: "System overview", repositories: "Source repository inventory",
    destinations: "Configured mirroring targets", backups: "Local backup storage",
    activity: "Synchronization and verification history", settings: "System configuration",
  };
  $("#page-context").textContent = messages[view];
  if (view === "overview") {
    $("#stats").innerHTML = stat("Repositories", "—", "Source not connected")
      + stat("Successful syncs", "—", "No operations yet")
      + stat("Last successful sync", "—", "Scheduling is off")
      + stat("Backup storage", "—", "No volume configured");
    renderActivityChart();
    $("#active-count").textContent = "0";
    $("#active-list").innerHTML = empty("No operations. Finish setup to connect your repositories.");
    $("#health").innerHTML = empty("No backup destinations configured.");
    $("#errors").innerHTML = empty("No errors recorded.");
    $("#recent").innerHTML = empty("No synchronization history.");
  } else if (view === "repositories") {
    $("#inventory-time").textContent = "Source not configured";
    $("#repositories").innerHTML = empty("Connect a source account in Setup to view repositories.");
    $("#discover").disabled = true;
    $("#sync-submit").disabled = true;
    $("#actions-help").textContent = "Synchronization is unavailable until setup is complete.";
    $("#sync-targets").innerHTML = "<legend>Destinations</legend><p>None configured.</p>";
  } else if (view === "destinations") {
    $("#destinations").innerHTML = '<article class="panel"><header><h2>Destinations</h2></header>'
      + empty("No remote mirrors or local backup volumes configured.") + '</article>';
  } else if (view === "backups") {
    $("#backups").innerHTML = '<article class="panel"><header><h2>Backup storage</h2></header>'
      + empty("No backup generations. Add a local destination during Setup.") + '</article>';
  } else if (view === "activity") {
    $("#activity").innerHTML = empty("No activity recorded. GitMirror has not been configured.");
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
  connected("Interface online");
  openView("overview");
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
  renderActivityChart(data.runs);
  $("#stats").innerHTML = stat("Repositories", data.total_repositories ?? "Unobserved", `${data.destinations.length} configured destinations`)
    + stat("Applied synchronizations", data.successful + data.failed ? `${data.successful} succeeded` : "Unobserved",
      data.successful + data.failed ? `${data.failed} failed` : "No completed applied sync observed")
    + stat("Last successful sync", date(data.last_success), `Next scheduled: ${date(data.next_run, "Unavailable")}`)
    + stat("Working disk free", size(data.storage.free), `Local backups: ${size(data.backup.logical_bytes)} logical`);
  $("#active-count").textContent = String(data.active.length);
  $("#active-list").innerHTML = runItems(data.active, "No active operations observed.");
  const health = data.backup;
  $("#health").innerHTML = !health.configured_volumes ? empty("No local volumes configured.")
    : list([["Volumes available", `${health.available_volumes} / ${health.configured_volumes}`],
      ["Current verified generations", health.verified], ["Unverified", health.unverified],
      ["Errors", health.failed]]) + (health.verified + health.failed + health.unverified === 0
      ? empty("No current generations observed.") : "");
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
  $("#destinations").innerHTML = data.destinations.map((destination) => `<article class="panel"><header>
    <h2>${escape(destination.id)}</h2><span class="pill">${escape(destination.provider)}</span></header>
    ${list(destination.provider === "local" ? [["Backup path", destination.path], ["Retention", retentionText(destination.retention)]]
      : [["Account", destination.owner ?? "Determined on login"], ["API", destination.api_url]])}</article>`).join("");
}

function renderSettings(data) {
  $("#edit-configuration").hidden = !data.web.managed;
  $("#check-connections").hidden = !data.web.managed;
  $("#check-connections button").disabled = busy;
  $("#settings").innerHTML = list([["Source", `${data.source.provider} / ${data.source.owner}`],
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
  const query = $("#repo-search").value.trim().toLowerCase();
  const visibility = $("#repo-filter").value, status = $("#status-filter").value;
  const filtered = repositories.filter((repo) => repo.name.toLowerCase().includes(query)
    && (!visibility || repo.visibility === visibility)
    && (!status || statusFor(repo.destinations) === status));
  $("#repositories").innerHTML = filtered.length ? table(["Repository", "Visibility", "Default branch", "Sync status", "Last verified backup"],
    filtered.map((repo) => row(button("details", repo.id, repo.name), escape(repo.visibility),
      `<code>${escape(repo.default_branch || "Unconfirmed")}</code>`, pill(statusFor(repo.destinations)), escape(date(repo.last_backup)))))
    : empty(repositories.length ? "No repositories match your filters." : "No source inventory recorded. Read it from the source to populate this view.");
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
    ["Last verified backup", date(repo.last_backup)]]) + `<div class="table-wrap">${table(["Destination", "Status", "Checked", "Detail"], destinations)}</div>`
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
        <summary>${escape(repo.name)} <small class="muted">· ${repo.generations.length} generations</small></summary>
        ${repo.error ? `<p class="error">${escape(repo.error)}</p>` : ""}
        <div class="table-wrap">${table(["Generation", "Integrity", "Last check", "Created", "Actions"], generations)}</div>
        </details>`;
    }).join("") || empty("No backup generations observed.")}</article>`).join("") || empty("No local volumes configured.");
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

async function refresh() {
  if (previewMode || !csrf) return;
  if (loading) { refreshPending = true; return; }
  loading = true;
  try {
    snapshot = await api("/api/overview");
    busy = snapshot.active.some((run) => run.origin === "web");
    renderContext();
    switch (view) {
      case "overview": renderOverview(snapshot); break;
      case "destinations": renderDestinations(snapshot); break;
      case "settings": renderSettings(snapshot); break;
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
    connected("Live");
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
  stream.onopen = () => connected("Live");
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
  $("#system-state").innerHTML = '<span class="status-dot ready"></span> Connected';
  $("#system-detail").textContent = "Dashboard authenticated";
  openView("overview");
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

document.addEventListener("click", (event) => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.view || target.dataset.go) openView(target.dataset.view || target.dataset.go);
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

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const control = event.currentTarget.querySelector("button");
  control.disabled = true;
  $("#login-error").textContent = "";
  try {
    await post("/api/login", {password: $("#login-password").value});
    $("#login-password").value = "";
    await boot();
  } catch (error) { $("#login-error").textContent = error.message; }
  finally { control.disabled = false; }
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

function providerInput(spec = {}, destination = false) {
  const fieldset = document.createElement("fieldset");
  const choices = destination ? ["github", "gitlab", "codeberg", "local"] : ["github", "gitlab", "codeberg"];
  fieldset.innerHTML = `
    ${destination ? '<div class="target-head"><button type="button" class="link remove-target">Remove</button></div>' : ""}
    <div class="action-form">
      ${destination ? '<label>Destination ID<input name="id" pattern="[a-z][a-z0-9_\\-]{0,62}" maxlength="63" required></label>' : ""}
      <label>Provider<select name="provider">${choices.map((p) => `<option>${p}</option>`).join("")}</select></label>
    </div>
    <div class="action-form remote-section">
      <label>Account<input name="owner" maxlength="100" autocomplete="off"></label>
      <label>API URL (optional)<input name="api_url" type="url" autocomplete="off" placeholder="Provider default"></label>
      <label>API token<input name="token" type="password" autocomplete="new-password" maxlength="4096"></label>
    </div>
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
  function syncFields() {
    const local = control("provider").value === "local";
    const localFields = fieldset.querySelector(".local-section");
    const remoteFields = fieldset.querySelector(".remote-section");
    localFields.hidden = !local;
    remoteFields.hidden = local;
    for (const input of localFields.querySelectorAll("input")) input.disabled = !local;
    for (const input of remoteFields.querySelectorAll("input")) input.disabled = local;
    control("owner").required = !local;
    control("keep").required = local && (control("days").value !== "" || control("automatic").checked);
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
  form.querySelector(".setup-code").hidden = !initial;
  form.querySelector(".current-password").hidden = initial;
  form.elements.code.required = initial;
  form.elements.password.required = initial;
  form.elements.hours.value = data.schedule?.interval_hours ?? 12;
  form.elements.enabled.checked = data.schedule?.enabled ?? false;
  form.elements.repositories.value = (data.repositories ?? []).join("\n");
  form.elements.known_hosts.value = data.known_hosts ?? "";
  form.querySelector("[data-public-key]").textContent = data.public_key;
  form.querySelector(".source-fields").append(providerInput(data.source));
  const destinations = form.querySelector(".target-fields");
  const addTarget = (dest) => destinations.append(providerInput(dest, true));
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
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = form.querySelector("button[type=submit]");
    const error = form.querySelector("[data-config-error]");
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
    enterPreview(data);
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
startDashboard().catch(signOut);
