"use strict";
const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmtDate = (epoch) => epoch ? new Date(epoch * 1000).toLocaleString() : "Never";
const fmtBytes = (n) => !Number.isFinite(Number(n)) ? "Unknown" : (Number(n) < 1024 ? Number(n) + " B" : (Number(n) / (1024 ** Math.floor(Math.log(Number(n)) / Math.log(1024)))).toFixed(1) + " " + ["B","KiB","MiB","GiB","TiB"][Math.min(4,Math.floor(Math.log(Number(n))/Math.log(1024)))]);
let csrf = "", state = null, currentView = "overview", lastLog = "", lastRunId = "", loading = false, stream = null, timer = null;
const sections = {overview:["Overview","Monitor your mirrors and local backups."],repositories:["Repositories","Read-only source repository inventory."],destinations:["Destinations","Where your repository data is mirrored."],backups:["Backups","Local generations, health and free disk space."],activity:["Activity","A history of runs started through this dashboard."],settings:["Settings","Current runtime configuration. Secrets are hidden."]};
function notice(message, error=false){const n=$("#notice");n.hidden=false;n.classList.toggle("error",error);n.textContent=message;clearTimeout(notice.timer);notice.timer=setTimeout(()=>n.hidden=true,7000)}
async function request(path, options={}) {
  const headers = {"Accept":"application/json",...(options.body ? {"Content-Type":"application/json"}:{}),...(csrf ? {"X-CSRF-Token":csrf}:{})};
  let response;
  try{response=await fetch(path,{credentials:"same-origin",...options,headers:{...headers,...(options.headers||{})},body:options.body ? JSON.stringify(options.body) : undefined})}
  catch(error){throw new Error("Cannot connect to the GitMirror service.")}
  if(response.status===401 && path!=="/api/login"){showLogin();throw new Error("Session expired. Sign in again.")}
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.detail || "Request failed ("+response.status+")");
  return data;
}
function showLogin(){
  $("#login").hidden=false;$("#shell").hidden=true; if(stream){stream.close();stream=null} if(timer){clearInterval(timer);timer=null}
}
function showDashboard(){$("#login").hidden=true;$("#shell").hidden=false}
async function bootstrap(){
  try{const session=await request("/api/session");csrf=session.csrf;showDashboard();await refreshAll();connectEvents();if(!timer)timer=setInterval(()=>refreshAll(true).catch(()=>{}),15000)}
  catch(e){showLogin()}
}
function view(name){if(!sections[name])return;currentView=name;document.querySelectorAll(".nav-item").forEach(el=>el.classList.toggle("active",el.dataset.view===name));document.querySelectorAll(".view").forEach(el=>el.hidden=el.id!=="view-"+name);$("#page-title").textContent=sections[name][0];$("#section-name").textContent=sections[name][0];$("#page-subtitle").textContent=sections[name][1];}
function stat(label,value,note,icon){return `<div class="stat-card"><div class="stat-label">${esc(label)}<span class="stat-icon">${esc(icon)}</span></div><div class="stat-value">${esc(value)}</div><div class="stat-note">${esc(note)}</div></div>`}
function pill(status){const cls=status==="succeeded"?"ok":status==="failed"?"bad":status==="running"?"warn":"";return `<span class="pill ${cls}">${esc(status || "Unknown")}</span>`}
function renderOverview(data){
  const all=data.runs||[]; const completed=all.filter(x=>x.state==="succeeded").length; const failures=all.filter(x=>x.state==="failed").length;
  $("#stats").innerHTML=stat("Source provider",(data.source.provider||"").toUpperCase(),data.source.owner||"Configured","⑂")+
    stat("Destinations",data.destinations.length,"Configured endpoints","◇")+
    stat("Successful web runs",completed,failures+" failed in recent history","✓")+
    stat("Disk free",fmtBytes(data.storage.free),"Working storage volume","▤");
  const recent=all.slice(0,5);
  $("#recent-runs").innerHTML=recent.length?recent.map(run=>`<div class="run-row"><div><div class="run-name">${esc(run.mode==="apply"?"Sync applied":"Dry-run")} · ${esc(run.repository||"All repositories")}</div><div class="run-meta">${esc(fmtDate(run.started))} · ${esc((run.destinations||[]).join(", ")||"All targets")}</div></div>${pill(run.state)}</div>`).join(""):'<div class="empty-state">No web-initiated runs yet.</div>';
  const busy=Boolean(data.active);$("#engine-state").className="pill "+(busy?"warn":"ok");$("#engine-state").textContent=busy?"Running":"Idle";
  $("#live-status").className="pill "+(data.latest?data.latest.state==="succeeded"?"ok":data.latest.state==="failed"?"bad":"warn":"");
  $("#live-status").textContent=data.latest?data.latest.state:"No run";
  $("#run-button").disabled=!data.web.allow_actions||busy;
  $("#actions-help").textContent=data.web.allow_actions?"Web actions are enabled. GitMirror uses the existing CLI lock.":"Web actions are disabled. Start the server with --allow-actions.";
  $("#sync-target").innerHTML='<option value="">All configured destinations</option>'+data.destinations.map(d=>`<option value="${esc(d.id)}">${esc(d.id)} (${esc(d.provider)})</option>`).join("");
}
function renderDestinations(data){$("#destinations-list").innerHTML=data.destinations.map(d=>`<article class="panel"><div class="panel-head"><div><h2>${esc(d.id)}</h2><p>${esc(d.provider.toUpperCase())} destination</p></div><span class="badge">Configured</span></div><dl class="mini-data"><dt>Provider</dt><dd>${esc(d.provider)}</dd><dt>Account</dt><dd>${esc(d.owner||"Local storage")}</dd><dt>Location</dt><dd>${esc(d.path||d.api_url||"Managed by provider")}</dd></dl></article>`).join("")}
function renderSettings(data){$("#settings-list").innerHTML=`<dl class="mini-data"><dt>Source</dt><dd>${esc(data.source.provider)} / ${esc(data.source.owner)}</dd><dt>Schedule</dt><dd>Every ${esc(data.schedule_hours)} hours (CLI scheduler)</dd><dt>Working path</dt><dd>${esc(data.storage.path)}</dd><dt>Web actions</dt><dd>${data.web.allow_actions?"Enabled":"Disabled"}</dd><dt>Web security</dt><dd>Password login, signed session, CSRF protection</dd><dt>Destinations</dt><dd>${esc(data.destinations.map(d=>d.id).join(", "))}</dd></dl>`}
function renderActivity(data){const runs=data.runs||[];$("#activity-table").innerHTML=runs.length?`<table class="data-table"><thead><tr><th>Started</th><th>Mode</th><th>Repository</th><th>Result</th><th>Output</th></tr></thead><tbody>${runs.map(r=>`<tr><td>${esc(fmtDate(r.started))}</td><td>${esc(r.mode)}</td><td>${esc(r.repository||"All")}</td><td>${pill(r.state)}</td><td><button class="text-button" data-log="${esc(r.id)}">View log</button></td></tr>`).join("")}</tbody></table>`:'<div class="empty-state">No jobs have been started from the dashboard.</div>'}
async function refreshAll(quiet=false){
  if(loading)return; loading=true;
  try{const data=await request("/api/overview");state=data;renderOverview(data);renderDestinations(data);renderSettings(data);renderActivity(data);$("#time-now").textContent=new Date().toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"});if(data.latest?.id && (lastRunId!==data.latest.id||data.active))await loadLog(data.latest.id,true);if(currentView==="backups")await loadBackups();$("#heartbeat").innerHTML='<span class="online-dot"></span> Connected';}
  catch(e){$("#heartbeat").textContent="Disconnected";if(!quiet)notice(e.message,true)}
  finally{loading=false}
}
async function loadLog(id,quiet=false){try{const d=await request("/api/runs/"+encodeURIComponent(id)+"/log");const pre=$("#live-log");if(d.log!==lastLog||lastRunId!==id){const atBottom=pre.scrollTop+pre.clientHeight>=pre.scrollHeight-45;pre.textContent=d.log||"Job has not produced output yet.";if(atBottom||lastRunId!==id)pre.scrollTop=pre.scrollHeight;lastLog=d.log;lastRunId=id}}catch(e){if(!quiet)notice(e.message,true)}}
async function loadBackups(){
  const data=await request("/api/backups");
  $("#backups-list").innerHTML=data.destinations.length?data.destinations.map(d=>`<article class="panel"><div class="panel-head"><div><h2>${esc(d.id)}</h2><p>${esc(d.path)}</p></div>${pill(d.error?"failed":"succeeded")}</div><dl class="mini-data"><dt>Status</dt><dd>${esc(d.error||"Volume available")}</dd><dt>Repositories</dt><dd>${esc(d.repositories)}</dd><dt>Generations</dt><dd>${esc(d.generations)}</dd><dt>Free space</dt><dd>${esc(fmtBytes(d.free))}</dd></dl></article>`).join(""):'<div class="empty-state">No local destinations configured.</div>';
}
async function loadRepositories(){
  $("#load-repositories").disabled=true;$("#repositories-list").textContent="Reading source repositories...";
  try{const data=await request("/api/repositories");$("#repositories-list").innerHTML=data.repositories.length?`<table class="data-table"><thead><tr><th>Name</th><th>Default branch</th><th>Visibility</th></tr></thead><tbody>${data.repositories.map(r=>`<tr><td><strong>${esc(r.name)}</strong></td><td><code>${esc(r.default_branch||"Unknown")}</code></td><td>${esc(r.visibility)}</td></tr>`).join("")}</tbody></table>`:'<div class="empty-state">No repositories returned.</div>';}
  catch(e){$("#repositories-list").textContent=e.message}
  finally{$("#load-repositories").disabled=false}
}
function connectEvents(){if(stream)stream.close();stream=new EventSource("/api/events");stream.onmessage=()=>{if(!loading)refreshAll(true).catch(()=>{})};stream.onerror=()=>{$("#heartbeat").textContent="Reconnecting..."}}
document.addEventListener("DOMContentLoaded",()=>{
  document.querySelectorAll(".nav-item").forEach(el=>el.addEventListener("click",()=>{view(el.dataset.view);if(el.dataset.view==="backups")loadBackups().catch(e=>notice(e.message,true))}));
  document.querySelectorAll("[data-go]").forEach(el=>el.addEventListener("click",()=>view(el.dataset.go)));
  $("#refresh-button").addEventListener("click",()=>refreshAll());
  $("#load-repositories").addEventListener("click",loadRepositories);
  $("#login-form").addEventListener("submit",async event=>{event.preventDefault();$("#login-error").textContent="";try{await request("/api/login",{method:"POST",body:{password:$("#login-password").value}});$("#login-password").value="";await bootstrap()}catch(e){$("#login-error").textContent=e.message}});
  $("#logout").addEventListener("click",async()=>{try{await request("/api/logout",{method:"POST",body:{}})}catch(_){}csrf="";showLogin()});
  $("#sync-mode").addEventListener("change",()=>{$("#sync-confirm-wrap").hidden=$("#sync-mode").value!=="apply";$("#run-button").textContent=$("#sync-mode").value==="apply"?"Apply synchronization":"Start dry-run";$("#sync-confirm").value=""});
  $("#sync-form").addEventListener("submit",async event=>{event.preventDefault();const repo=$("#sync-repo").value.trim(),apply=$("#sync-mode").value==="apply";const confirm=$("#sync-confirm").value.trim();const destinations=$("#sync-target").value?[$("#sync-target").value]:[];try{const result=await request("/api/sync",{method:"POST",body:{repo,apply,confirmation:confirm,destinations}});notice("Job "+result.id+" started.");await refreshAll()}catch(e){notice(e.message,true)}});
  $("#activity-table").addEventListener("click",event=>{const el=event.target.closest("[data-log]");if(el){view("overview");loadLog(el.dataset.log).catch(e=>notice(e.message,true))}});
  bootstrap();
});
