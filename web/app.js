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
const pages = {overview: "Overview", repositories: "Repositories", destinations: "Destinations", backups: "Backups",
  activity: "Activity", settings: "Settings"};

let csrf = "", view = "overview", snapshot = null, stream = null, refreshTimer = null;
let repositories = [], targetSignature = "", currentRun = "", restoreSelection = null, targets = [];
let loading = false, refreshPending = false, logRequest = 0, busy = false;

function notify(message, error = false) {
  const node = $("#notice");
  node.textContent = message;
  node.classList.toggle("error", error);
  node.hidden = false;
}

function connected(status) {
  $("#connection").textContent = status;
  $("#connection").classList.toggle("connected", status === "Live");
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

function runName(run) {
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
  $("#settings").innerHTML = list([["Source", `${data.source.provider} / ${data.source.owner}`],
    ["Source API", data.source.api_url], ["Schedule interval", `${data.schedule_hours} hours`],
    ["Next observed run", date(data.next_run, "Unavailable")], ["Working path", data.storage.path],
    ["Web actions", data.web.allow_actions ? "Enabled" : "Read-only"],
    ["Web restores", data.web.restore_enabled ? "Enabled" : "Disabled"],
    ["Access", data.web.secure ? "HTTPS secure session" : "HTTP local session"]])
    + empty("This dashboard reads the server’s configuration. Changes take effect after a dashboard restart.");
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
  if (!csrf) return;
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
  $("#login").hidden = true;
  $("#shell").hidden = false;
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
boot().catch(signOut);
