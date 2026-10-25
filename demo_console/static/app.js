"use strict";
// Everly demo console (the customer's AI companion at ACME Retail). Live mode: simulated website sign-in, then the chat streams agent events over a
// WebSocket. Replay mode (?replay=<storyboard>): the video renderer plays a recorded take on a presentation clock.

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
const initials = (n) => (n || "?").split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const color = (name) => css(`--a-${name}`) || css("--muted");
const fmtClock = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const REPLAY = new URLSearchParams(location.search).get("replay");

let META = null;
const AGENTS = {};        // name -> {label, sub, x, y}
const LAYOUT = { middle: 0 };
let S = null;             // signed-in session {id, customer, sly}
let ws = null;
const st = { calls: new Map(), used: new Set(), turn: 0, working: null, tick: null, sentAt: 0, answered: false,
             policy: [], caseView: null, lastTurn: null, recording: false, lastStatus: "", suggestions: [] };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}

// ------------------------------------------------------------------ start-up
try { const t = localStorage.getItem("ul-theme"); if (t) document.documentElement.dataset.theme = t; } catch (e) {}
$("themeBtn").onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("ul-theme", document.documentElement.dataset.theme); } catch (e) {}
  drawMap();
};
document.querySelectorAll("[data-mark]").forEach((el) => el.innerHTML = $("mark").innerHTML);

// "from ACME Retail": the retailer's first word as its wordmark (capitals, bold), the rest as plain text.
function fromRetailer(name) {
  const [first, ...rest] = String(name || "").split(" ");
  return first ? `from <b class="wordmark">${esc(first.toUpperCase())}</b>${rest.length ? " " + esc(rest.join(" ")) : ""}` : "";
}

async function init() {
  META = await api("/api/meta");
  $("brandName").textContent = META.brand;
  $("brandTag").textContent = META.tagline;
  document.title = META.retailer ? `${META.brand} from ${META.retailer} · Demo` : `${META.brand} · Demo`;
  if (META.retailer) {
    $("brandFrom").innerHTML = fromRetailer(META.retailer);
    $("brandFrom").hidden = false;
    $("topNote").textContent = "Simulated website sign-in for demos";
  }
  // Specialists stack on the right, 44px apart; the map's height and the front row's position follow their number.
  const specialists = META.agents.filter((a) => a.name !== "support_orchestrator");
  const mapHeight = Math.max(270, 4 + specialists.length * 44);
  const middle = Math.round((mapHeight - 38) / 2);
  $("map").setAttribute("viewBox", `0 0 770 ${mapHeight}`);
  AGENTS.customer = { label: "Customer", sub: "website chat", x: 10, y: middle };
  for (const a of META.agents) AGENTS[a.name] = { label: a.label, sub: a.sub, x: 195, y: middle };
  specialists.forEach((a, i) => Object.assign(AGENTS[a.name], { x: 400, y: 4 + i * 44 }));
  // Orbit's clock and new-in launches (docs/orbit_tick_plan.md): what triggers Orbit besides the customer
  AGENTS.triggers = { label: "Orbit's clock", sub: "ticks · new-in launches", x: 10, y: mapHeight - 42 };
  LAYOUT.middle = middle;  // the customer's row; with an AI assistant box the customer moves up (setAssistantBox)
  if (REPLAY) return runReplay(REPLAY);
  loadCustomers(true);
}

// ------------------------------------------------------------------ sign-in (simulated)
const pick = { kind: "all", query: "", offset: 0, list: [], total: 0, selected: null, notSigned: false };
let searchTimer = null;

async function loadCustomers(reset) {
  if (reset) { pick.offset = 0; pick.list = []; }
  try {
    const q = new URLSearchParams({ query: pick.query, kind: pick.kind, offset: pick.offset, limit: 60 });
    const res = await api(`/api/customers?${q}`);
    pick.list = pick.list.concat(res.customers); pick.total = res.total; pick.offset = pick.list.length;
    renderCards();
  } catch (e) { $("cards").innerHTML = `<div class="empty">Could not load customers: ${esc(e.message)}</div>`; }
}

function cardHtml(c) {
  const scn = c.scenarios.length
    ? c.scenarios.map((s) => `<div class="scn">${esc(s.id)} · ${esc(s.title)}</div><span class="cls ${s.cls}">${esc(s.outcome)}</span>`).join("")
    : `<div class="scn muted">${c.order_count} order(s)</div><span class="cls other">Free-form</span>`;
  return `<button class="card ${pick.selected && pick.selected.customer_id === c.customer_id ? "sel" : ""}" data-id="${esc(c.customer_id)}">
    <div class="name">${esc(c.name)} <span class="tier">${esc(c.tier)}</span></div>
    <div class="muted mono">${esc(c.customer_id)}</div>${scn}</button>`;
}

function renderCards() {
  $("cards").innerHTML = pick.list.length ? pick.list.map(cardHtml).join("") : `<div class="empty">No customers match.</div>`;
  $("cards").querySelectorAll(".card").forEach((el) => el.onclick = () => select(pick.list.find((c) => c.customer_id === el.dataset.id)));
  $("moreBtn").hidden = pick.list.length >= pick.total;
  $("moreBtn").textContent = `Show more (${pick.total - pick.list.length} left)`;
}

function select(c) {
  pick.selected = c; pick.notSigned = false; renderCards();
  $("signinEmpty").hidden = true; $("signinForm").hidden = false;
  $("sAvatar").textContent = initials(c.name); $("sName").textContent = c.name;
  $("sId").textContent = `${c.customer_id} · ${c.tier}`;
  let opts = `<label class="opt"><input type="radio" name="ch" value="web" checked><div><div class="t">Website, signed in
      <span class="tier">${esc(c.auth_level)}</span></div><div class="d">Questions answered; changes allowed by policy</div></div></label>`;
  if (META.not_signed_in) opts += `<label class="opt"><input type="radio" name="ch" value="none"><div><div class="t">Not signed in</div>
      <div class="d">Anonymous visitor: no account data may be shared</div></div></label>`;
  $("channels").innerHTML = opts;
  $("channels").querySelectorAll("input").forEach((r) => r.onchange = () => { pick.notSigned = r.value === "none"; preview(); });
  preview();
}

function preview() {
  const c = pick.selected;
  $("gwPreview").textContent = JSON.stringify(pick.notSigned ? { channel: "web_session" } : { channel: "web_session", identifier: c.identifier });
}

$("search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => { pick.query = $("search").value; loadCustomers(true); }, 250); };
document.querySelectorAll("[data-kind]").forEach((b) => b.onclick = () => {
  pick.kind = b.dataset.kind; document.querySelectorAll("[data-kind]").forEach((x) => x.classList.toggle("on", x === b));
  loadCustomers(true);
});
$("moreBtn").onclick = () => loadCustomers(false);
$("signinBtn").onclick = async () => {
  try {
    const res = await api("/api/session", { method: "POST", body: JSON.stringify({ customer_id: pick.notSigned ? null : pick.selected.customer_id }) });
    openConsole(res);
  } catch (e) { alertNote(`Sign-in failed: ${e.message}`); }
};

function alertNote(text) { const n = document.createElement("div"); n.className = "note"; n.textContent = text; $("signinForm").appendChild(n); }

// ------------------------------------------------------------------ console
function openConsole(res) {
  S = { id: res.session_id, customer: res.customer, sly: res.sly_data_sent };
  $("login").hidden = true; $("console").hidden = false;
  for (const id of ["whoPill", "showAgentsBox", "newChat", "switchCust"]) $(id).hidden = false;
  $("recBtn").hidden = REPLAY || !META.recording;
  $("topNote").hidden = true;
  const c = S.customer;
  $("cAvatar").textContent = initials(c.name); $("cName").textContent = c.name;
  const scn = c.scenarios && c.scenarios.length ? " · " + c.scenarios.map((s) => s.id).join(", ") : "";
  $("cSub").textContent = S.sly.identifier ? `Website, signed in · ${c.auth_level} sign-in${scn}` : "Not signed in";
  $("whoText").textContent = `${c.name} · website`;
  const asks = [...new Set((c.scenarios || []).flatMap((s) => s.messages))];
  $("suggest").innerHTML = asks.map((q) => `<button type="button">${esc(q)}</button>`).join("");
  $("suggest").querySelectorAll("button").forEach((b) => b.onclick = () => { $("input").value = b.textContent; $("input").focus(); });
  clearConversation();
  showCTab("chat");
  $("ctabs").hidden = !c.customer_id;  // My Companion needs a signed-in customer
  if (!REPLAY) {
    loadThread();
    connect();
    inboxSeen = -1; loadInbox();
    MC = null; loadMC();
    Object.assign(bg, { seq: 0, aseq: 0, first: true, queue: [], lane: [] }); setAssistantBox([]); pollClock();
    clearInterval(inboxTimer); inboxTimer = setInterval(() => { loadInbox(); pollClock(); if (mcTab !== "chat") loadMC(); }, 5000);
  }
}

function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/session/${S.id}/chat`);
  ws.onmessage = (e) => handle(JSON.parse(e.data));
  ws.onclose = () => { if (S && ws && ws.dead !== true) addSys("Connection to the console closed. Sign in again to continue."); };
}

function clearConversation() {
  st.calls.clear(); st.used = new Set(); st.turn = 0; st.policy = []; st.caseView = null; st.lastTurn = null;
  st.suggestions = [];
  stopWorking();
  $("msgs").innerHTML = "";
  addSys(`New conversation · ${S.customer.name} · a fresh agent session`);
  $("timeline").innerHTML = `<div class="empty">Send a message to see the agents work.</div>`;
  renderCase(null); renderSession(); drawMap();
  $("sendBtn").disabled = false;
}

// The customer's continuous thread (companion plan P2): earlier conversations and updates above the new one, then
// the companion's greeting, written by code. The agents never get the thread, only the companion notes.
async function loadThread() {
  if (REPLAY || !S || !S.customer.customer_id) return;
  const sid = S.id;
  let t;
  try { t = await api(`/api/session/${sid}/thread`); } catch (e) { return; }
  if (!S || S.id !== sid || st.turn > 0) return;  // signed out, or the customer already wrote
  if (t.thread.length) {
    const box = document.createDocumentFragment();
    const head = document.createElement("div"); head.className = "sys"; head.textContent = `Earlier with ${t.name}`;
    box.appendChild(head);
    for (const m of t.thread) {
      const d = document.createElement("div");
      d.className = `bubble old ${m.role === "customer" ? "me" : m.role === "update" ? "bot upd" : "bot"}`;
      d.innerHTML = (m.role === "update" ? `<div class="upd-h">${({ guardian: "Update · follow-up", stylist: "New in · Personal Stylist" })[m.source] || "Update"}</div>` : "") + md(m.text);
      box.appendChild(d);
    }
    $("msgs").prepend(box);
  }
  if (t.greeting) addMsg("bot", t.greeting);
}

$("newChat").onclick = async () => {
  const res = await api(`/api/session/${S.id}/reset`, { method: "POST" });
  clearConversation(); savedTake(res.take); loadThread();
  if (st.recording) await api(`/api/session/${S.id}/record`, { method: "POST", body: JSON.stringify({ on: true }) });
};
$("switchCust").onclick = async () => {
  if (ws) { ws.dead = true; ws.close(); }
  const res = await api(`/api/session/${S.id}`, { method: "DELETE" }).catch(() => ({}));
  if (res.take) alertNote(`Saved take ${res.take.scenario || "manual"} / ${res.take.take_id} (${verdictText(res.take)})`);
  setRecording(false);
  clearInterval(inboxTimer);
  S = null; $("console").hidden = true; $("login").hidden = false;
  for (const id of ["whoPill", "showAgentsBox", "newChat", "switchCust", "recBtn"]) $(id).hidden = true;
  $("topNote").hidden = false;
};
$("recBtn").onclick = async () => {
  if (!S) return;
  const res = await api(`/api/session/${S.id}/record`, { method: "POST", body: JSON.stringify({ on: !st.recording }) });
  setRecording(res.recording);
  if (!res.recording) savedTake(res.take);
};
function setRecording(on) {
  st.recording = on; $("recBtn").classList.toggle("on", on); $("recText").textContent = on ? "Recording" : "Record";
}
function verdictText(take) { return take.verdict ? `${take.verdict.result}${take.verdict.reasons && take.verdict.reasons.length ? ": " + take.verdict.reasons[0] : ""}` : "unscored"; }
function savedTake(take) { if (take) addSys(`Saved take ${take.scenario || "manual"} / ${take.take_id} · ${verdictText(take)}`); }

$("showAgents").onchange = (e) => $("layout").classList.toggle("hide-agents", !e.target.checked);
$("showMap").onchange = (e) => $("mapBox").classList.toggle("collapsed", !e.target.checked);
document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => showTab(b.dataset.tab));
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  if (name === "inbox" && !REPLAY) loadInbox();
  if (name === "launches" && !REPLAY) loadLaunches();
}
$("showTools").onchange = () => document.querySelectorAll(".ev.tool").forEach((e) => e.hidden = !$("showTools").checked);
$("technical").onchange = () => document.querySelectorAll("[data-plain]").forEach((e) => {
  e.textContent = $("technical").checked ? e.dataset.tech : e.dataset.plain;
});

$("composer").onsubmit = (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text || !S || $("sendBtn").disabled) return;
  $("input").value = "";
  customerSends(text);
  ws.send(JSON.stringify({ text }));
};

function customerSends(text) {
  addMsg("me", text);
  st.turn += 1; st.answered = false; st.sentAt = performance.now();
  if ($("timeline").querySelector(".empty")) $("timeline").innerHTML = "";
  const row = document.createElement("div"); row.className = "ev turn"; row.textContent = `Message ${st.turn}: “${text}”`;
  $("timeline").appendChild(row);
  startWorking(); $("sendBtn").disabled = true;
}

function addMsg(who, text) {
  const d = document.createElement("div"); d.className = "bubble " + who; d.innerHTML = md(text);
  $("msgs").appendChild(d); $("msgs").scrollTop = 1e9; return d;
}
function addSys(text) { const d = document.createElement("div"); d.className = "sys"; d.textContent = text; $("msgs").appendChild(d); $("msgs").scrollTop = 1e9; }

function startWorking() {
  stopWorking();
  st.working = document.createElement("div"); st.working.className = "working";
  st.working.innerHTML = `<span class="spin"></span><span class="wt">Working on it…</span>`;
  $("msgs").appendChild(st.working); $("msgs").scrollTop = 1e9;
  st.lastStatus = "Working on it";
  if (!REPLAY) st.tick = setInterval(() => setStatus(st.lastStatus, (performance.now() - st.sentAt) / 1000), 1000);
}
function stopWorking() { if (st.tick) clearInterval(st.tick); st.tick = null; if (st.working) st.working.remove(); st.working = null; }
function setStatus(text, seconds) {
  st.lastStatus = text;
  if (st.working) st.working.querySelector(".wt").textContent = `${text}… (${Math.round(seconds)} s)`;
}

// ------------------------------------------------------------------ events
const label = (n) => (AGENTS[n] ? AGENTS[n].label : n);
const tag = (n) => AGENTS[n] ? `<span class="tag" style="background:${color(n)}">${esc(label(n))}</span>` : `<span class="tag tool mono">${esc(n)}</span>`;

function handle(ev, realT) {
  const t = realT ?? ev.t;
  switch (ev.kind) {
    case "delegate": case "agent_reply": case "tool_call":
      addRow(ev); drawMap(ev);
      setStatus(ev.kind === "tool_call" ? `${label(ev.from)} is using ${ev.to}` : ev.kind === "delegate" ? `${label(ev.to)} is working` : `${label(ev.from)} reported back`, t);
      break;
    case "tool_result":
      attachResult(ev); drawMap(ev);
      if (ev.from === "evaluate_policy" && ev.detail && ev.detail.result && typeof ev.detail.result === "object")
        st.policy.push(ev.detail.result);
      break;
    case "case": renderCase(ev.case); break;
    case "answer": stopWorking(); addMsg("bot", ev.text); st.answered = true; drawMap(ev); break;
    case "suggestions": st.suggestions.push(ev); suggestionRow(ev); renderSuggestions(ev); renderCase(null); break;
    case "error": stopWorking(); addMsg("err", ev.say); break;
    case "turn_end": turnEnd(ev); break;
    default: break;
  }
}

function addRow(ev) {
  const el = document.createElement("div");
  el.className = "ev" + (ev.kind === "tool_call" ? " tool" : "");
  const route = ev.kind === "tool_call" ? `${tag(ev.from)}<span class="arrow">uses</span>${tag(ev.to)}`
    : `${tag(ev.from)}<span class="arrow">${ev.kind === "agent_reply" ? "replies to" : "→"}</span>${tag(ev.to)}`;
  const tech = $("technical").checked;
  el.innerHTML = `<div class="t">${ev.t.toFixed(1)}s</div><div><div class="route">${route}</div>
    <div class="say" data-plain="${esc(ev.say)}" data-tech="${esc(ev.tech)}">${esc(tech ? ev.tech : ev.say)}</div>
    <div class="res" hidden></div>
    <details><summary>${ev.kind === "tool_call" ? "arguments and result" : "message"}</summary><pre class="mono">${esc(JSON.stringify(ev.detail, null, 2))}</pre></details></div>`;
  if (ev.kind === "tool_call") { st.calls.set(ev.id, { el, detail: ev.detail }); if (!$("showTools").checked) el.hidden = true; }
  $("timeline").appendChild(el); $("timeline").scrollTop = 1e9;
  return el;
}

function attachResult(ev) {
  const call = ev.call_id != null ? st.calls.get(ev.call_id) : null;
  if (!call) { addRow({ ...ev, kind: "tool_call", from: ev.to, to: ev.from }); return; }
  const res = call.el.querySelector(".res");
  res.hidden = false; res.classList.toggle("err", !!ev.error);
  res.dataset.plain = "→ " + ev.say; res.dataset.tech = "→ " + JSON.stringify(ev.detail.result).slice(0, 300);
  res.textContent = $("technical").checked ? res.dataset.tech : res.dataset.plain;
  call.detail = { ...call.detail, ...ev.detail };
  call.el.querySelector("pre").textContent = JSON.stringify(call.detail, null, 2);
  $("timeline").scrollTop = 1e9;
}

// ------------------------------------------------------------------ product suggestions (decided by code, not the model)
const money = (amount, currency) => currency === "USD" ? `$${Number(amount).toFixed(2)}` : `${Number(amount).toFixed(2)} ${esc(currency)}`;
const uniq = (xs) => [...new Set(xs.filter((x) => x != null && x !== ""))];

function productCard(i) {
  const colors = uniq(i.options.map((o) => o.color)), sizes = uniq(i.options.map((o) => o.size));
  return `<div class="pcard">
    <div class="pc-top"><span class="pc-cat">${esc(words(i.category))}</span><span class="pc-price">${money(i.list_price, i.currency)}</span></div>
    <div class="pc-title">${esc(i.title)}</div>
    <div class="pc-why">${esc(i.because)}</div>
    ${i.description ? `<div class="pc-desc">${esc(i.description)}</div>` : ""}
    <div class="pc-opts">${colors.map((c) => `<span class="pc-chip">${esc(c)}</span>`).join("")}
      ${sizes.length ? `<span class="pc-sizes">Sizes ${esc(sizes.join(", "))}</span>` : ""}
      ${i.in_your_usual_size ? `<span class="pc-yours">your size ${esc(i.in_your_usual_size)}</span>` : ""}</div>
    <div class="pc-link">View on the product page →</div></div>`;
}

function renderSuggestions(ev) {
  if (!ev.items || !ev.items.length) return;
  const d = document.createElement("div"); d.className = "recs";
  d.innerHTML = `<div class="recs-head">Suggested for you</div><div class="cards">${ev.items.map(productCard).join("")}</div>
    <div class="recs-note">Chosen by the store's rules from your purchases and what is in stock. Nothing is ordered here.</div>`;
  $("msgs").appendChild(d); $("msgs").scrollTop = 1e9;
}

function suggestionRow(ev) {
  const el = document.createElement("div"); el.className = "ev";
  el.innerHTML = `<div class="t">${ev.t.toFixed(1)}s</div><div><div class="route">${tag("support_orchestrator")}<span class="arrow">gets</span><span class="tag tool mono">recommendation engine</span></div>
    <div class="say">${esc(ev.say)}</div></div>`;
  $("timeline").appendChild(el); $("timeline").scrollTop = 1e9;
}

function turnEnd(ev) {
  stopWorking();
  if (!st.answered && ev.answer) addMsg("bot", ev.answer);
  st.lastTurn = ev;
  const cost = ev.cost || {};
  const view = st.caseView;
  const resolved = view ? view.issues.filter((i) => ["executed_verified", "resolved_info_only"].includes(i.status)).length : 0;
  const executed = view ? view.issues.filter((i) => i.outcome && i.outcome.outcome === "executed").length : 0;
  const people = view ? view.issues.filter((i) => ["queued_for_review", "handed_off"].includes(i.status)).length : 0;
  const m = document.createElement("div"); m.className = "meta";
  m.innerHTML = [cost.llm_calls != null ? `${cost.llm_calls} LLM calls` : null, `${Math.round(ev.t)} s`,
    resolved ? `${resolved} issue(s) resolved` : null, executed ? `${executed} action(s) executed` : null,
    people ? `${people} with a person` : null].filter(Boolean).map((x) => `<span class="pill">${x}</span>`).join("")
    + `<button type="button">See how the agents got here →</button>`;
  m.querySelector("button").onclick = () => { $("showAgents").checked = true; $("layout").classList.remove("hide-agents"); showTab("activity"); };
  $("msgs").appendChild(m); $("msgs").scrollTop = 1e9;
  renderSession(ev); drawMap();
  showWatchStarted(ev.watching);
  loadMC();  // a turn may have changed what the companion knows, follows or remembers
  $("sendBtn").disabled = false;
}

// ------------------------------------------------------------------ network map (SVG)
const W = 150, H = 38;
const SYSTEM_NODES = new Set(["guardian", "triggers"]);  // code only, no model: drawn dashed
const LEFT = new Set(["customer", "assistant", "triggers"]);  // what talks to Orbit from the left
// opts.background: Orbit's ticks and launches (not the customer's conversation: nothing is marked as used).
// opts.extra: a temporary dashed link with a label, e.g. {from: "guardian", to: "customer", label: "update"}.
function drawMap(ev, opts = {}) {
  if (!AGENTS.customer) return;
  const active = new Set(); let edge = null, toolOn = null;
  if (ev && ["delegate", "agent_reply"].includes(ev.kind)) { active.add(ev.from); active.add(ev.to); edge = [ev.from, ev.to].sort().join("|"); }
  if (ev && ev.kind === "tool_call") { active.add(ev.from); toolOn = [ev.from, ev.to]; }
  if (ev && ev.kind === "tool_result") { active.add(ev.to); toolOn = [ev.to, ev.from]; }
  if (ev && ev.kind === "answer") { active.add("support_orchestrator"); active.add("customer"); edge = "customer|support_orchestrator"; }
  if (ev && !opts.background) [ev.from, ev.to].forEach((a) => AGENTS[a] && st.used.add(a));
  if (st.turn) st.used.add("customer");
  const links = [...[...LEFT].filter((k) => AGENTS[k]).map((k) => [k, "support_orchestrator"]),
    ...Object.keys(AGENTS).filter((k) => !LEFT.has(k) && k !== "support_orchestrator").map((k) => ["support_orchestrator", k])];
  let out = "";
  for (const [a, b] of links) {
    const A = AGENTS[a], B = AGENTS[b], on = edge === [a, b].sort().join("|");
    const x1 = A.x + W, y1 = A.y + H / 2, x2 = B.x - 4, y2 = B.y + H / 2, mx = (x1 + x2) / 2;
    const who = ev && on ? (AGENTS[ev.from] ? ev.from : "support_orchestrator") : null;
    out += `<path class="edge ${on ? "active" : ""}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" ${on ? `style="stroke:${color(who)}"` : ""}/>`;
  }
  if (opts.extra && AGENTS[opts.extra.from] && AGENTS[opts.extra.to]) {
    const A = AGENTS[opts.extra.from], B = AGENTS[opts.extra.to], col = color(opts.extra.from);
    active.add(opts.extra.from); active.add(opts.extra.to);
    // left to right like the links; right to left below the boxes in the middle
    const ltr = A.x < B.x, x1 = ltr ? A.x + W : A.x - 4, y1 = A.y + H / 2, x2 = ltr ? B.x - 4 : B.x + W, y2 = B.y + H / 2;
    const low = Math.max(y1, y2) + 46, d = ltr ? `M${x1},${y1} C${(x1 + x2) / 2},${y1} ${(x1 + x2) / 2},${y2} ${x2},${y2}`
      : `M${x1},${y1} C${x1 - 90},${low} ${x2 + 90},${low} ${x2},${y2}`;
    out += `<path class="edge extra" d="${d}" style="stroke:${col}"/>`;
    const lx = ltr ? B.x - 8 : (x1 + x2) / 2, ly = ltr ? B.y - 4 : low - 8;
    out += `<text class="extra-label" x="${lx}" y="${ly}" text-anchor="${ltr ? "end" : "middle"}" style="fill:${col}">${esc(opts.extra.label)}</text>`;
  }
  for (const [k, a] of Object.entries(AGENTS)) {
    const on = active.has(k), was = st.used.has(k), col = color(k);
    out += `<g class="node ${on ? "active" : ""} ${SYSTEM_NODES.has(k) ? "system" : ""}" transform="translate(${a.x},${a.y})">
      <rect width="${W}" height="${H}" rx="8" style="${on || was ? `stroke:${col}` : ""};${on ? `fill:color-mix(in srgb, ${col} 14%, ${css("--panel")})` : ""}"/>
      <circle cx="12" cy="${H / 2}" r="4.5" fill="${col}" opacity="${on || was ? 1 : 0.35}"/>
      <text x="22" y="16">${esc(a.label)}</text><text class="sub" x="22" y="30">${esc(a.sub)}</text></g>`;
    if (toolOn && toolOn[0] === k) {
      const orch = k === "support_orchestrator";
      const bx = orch ? a.x : a.x + W + 10, by = orch ? a.y + H + 6 : a.y + 11;
      // 10.5px monospace is about 6.4px per character, plus the gear and padding
      out += `<g transform="translate(${bx},${by})"><rect width="${toolOn[1].length * 6.4 + 28}" height="17" rx="4" fill="${col}"/>
        <text class="toolbadge mono" x="7" y="12" fill="#fff">⚙ ${esc(toolOn[1])}</text></g>`;
    }
  }
  $("map").innerHTML = out;
}

// ------------------------------------------------------------------ Orbit's clock and launches (background lane)
// Ticks and launches are Orbit's own requests, not the customer's conversation: they play on the map only between
// turns and are listed under it, never in the timeline. Code decides every step; no model is called.
// The signed-in customer's own AI assistants (assistant gateway, docs/assistant_lane_plan.md) are listed there too:
// an ask_support turn plays once it has finished (assistant -> Orbit -> agents -> assistant); status reads are listed
// only (no agents).
const bg = { seq: 0, aseq: 0, first: true, playing: false, queue: [], lane: [], orbit: null };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const SOURCE_AGENT = { guardian: "guardian", stylist: "stylist" };

async function pollClock() {
  if (!S || REPLAY) return;
  const sid = S.id;
  let v;
  try { v = await api(`/api/session/${sid}/clock?after=${bg.seq}&after_assistant=${bg.aseq}`); } catch (e) { return; }
  if (!S || S.id !== sid) return;
  bg.orbit = v;
  for (const r of v.requests) {
    bg.seq = Math.max(bg.seq, r.seq);
    bg.lane.unshift({ ...r, id: `orbit-${r.seq}` });
    if (!bg.first) bg.queue.push(r);  // what happened before sign-in is listed, not played
  }
  bg.aseq = Math.max(bg.aseq, v.assistant_seq || 0);
  for (const a of v.assistant || []) {  // running, then done/failed with the same activity_id: the row is replaced
    const row = { ...a, event: "assistant", id: a.activity_id, at: a.time };
    const i = bg.lane.findIndex((x) => x.id === row.id);
    if (i >= 0) bg.lane.splice(i, 1);
    bg.lane.unshift(row);
    if (!bg.first && a.tool === "ask_support" && a.status !== "running" && (a.events || []).length) bg.queue.push(row);
  }
  bg.first = false;
  bg.lane = bg.lane.sort((x, y) => y.at - x.at).slice(0, 5);
  if (setAssistantBox(v.assistants || [])) drawMap();
  renderLane();
  playBackground();
}

// The customer's connected AI assistant on the left of the map (the most recent one), between them and the clock.
function setAssistantBox(names) {
  const had = Boolean(AGENTS.assistant);
  if (!names.length || !S) {
    delete AGENTS.assistant;
    if (AGENTS.customer) AGENTS.customer.y = LAYOUT.middle;
    return had;
  }
  const name = names[0].length > 12 ? names[0].slice(0, 11) + "…" : names[0];
  const first = (S.customer.name || "").split(" ")[0];
  AGENTS.assistant = { label: `${name} · AI assistant`, sub: `${first}'s · MCP · read-only`, x: 10, y: LAYOUT.middle + 20 };
  AGENTS.customer.y = LAYOUT.middle - 70;
  return !had;
}

async function playBackground() {
  if (bg.playing) return;
  bg.playing = true;
  while (bg.queue.length) {
    const r = bg.queue.shift();
    if (st.working || bg.queue.length > 2) continue;  // a customer turn is on the map, or too far behind: list only
    if (r.event === "assistant") { await playAssistant(r); continue; }
    drawMap({ kind: "delegate", from: "triggers", to: "support_orchestrator" }, { background: true });
    await sleep(500);
    for (const ev of r.events) {
      if (st.working) break;
      drawMap(ev, { background: true });
      await sleep(ev.kind === "tool_call" ? 650 : 450);
    }
    for (const source of r.for_you) {  // the signed-in customer got a message from this request
      if (st.working || !SOURCE_AGENT[source]) continue;
      drawMap(null, { background: true, extra: { from: SOURCE_AGENT[source], to: "customer",
        label: source === "stylist" ? "new-in pick" : "update" } });
      await sleep(1400);
    }
    if (!st.working) drawMap();
  }
  bg.playing = false;
}

async function playAssistant(r) {  // an ask_support turn, compressed: assistant -> Orbit -> agents -> assistant
  if (!AGENTS.assistant) return;
  drawMap(null, { background: true, extra: { from: "assistant", to: "support_orchestrator", label: "ask_support" } });
  await sleep(900);
  const step = Math.max(80, Math.min(300, 6000 / r.events.length));  // a whole turn in about 6 s at most
  for (const ev of r.events) {
    if (st.working) return;
    drawMap(ev, { background: true });
    await sleep(ev.kind === "tool_call" ? step * 1.5 : step);
  }
  if (st.working) return;
  drawMap({ kind: "agent_reply", from: "support_orchestrator", to: "assistant" }, { background: true });
  await sleep(1100);
  if (!st.working) drawMap();
}

function assistantText(r) {
  const who = r.client_name || "The assistant";
  if (r.tool !== "ask_support") return `${who} read the ${r.tool === "get_order_status" ? "order" : "case"} status (no agents)`;
  const q = `“${(r.question || "").length > 60 ? r.question.slice(0, 59) + "…" : r.question}”`;
  if (r.status === "running") return `${who} is asking ${q} …`;
  const path = ["Orbit", ...(r.agents || []).map(label)].join(" → ");
  return `${who} asked ${q} · ${path} · ${r.status === "done" ? "answered" : "not finished"}`;
}

function laneText(r) {
  if (r.event === "assistant") return assistantText(r);
  if (r.error) return `not done: ${r.error}`;
  const s = r.summary || {}, g = s.guardian, a = s.stylist;
  const n = (x) => (Array.isArray(x) ? x.length : x || 0);
  if (r.event === "launch") return typeof a === "object" && a ? `Aura launched ${a.title || a.product_id}: ${a.invited} invited of ${a.candidates} with Stylist Mode on` : String(a);
  const gt = typeof g === "object" && g ? `Guardian ${n(g.checked)} checked, ${n(g.notices)} update(s), ${n(g.handoffs)} hand-off(s)` : `Guardian: ${g}`;
  const at = typeof a === "object" && a ? `Aura ${n(a.sent)} held pick(s) sent` : `Aura: ${a}`;
  return `${gt} · ${at}`;
}

function renderLane() {
  const o = bg.orbit || {};
  const first = esc(S.customer.name.split(" ")[0]);
  const kind = { launch: "Launch", assistant: "Assistant" };
  const rows = bg.lane.map((r) => `<div class="bg-row ${r.error || r.status === "failed" ? "err" : ""} ${r.event === "assistant" ? "asst" : ""}"><span class="mono muted">${esc(new Date(r.at * 1000).toLocaleTimeString())}</span>
      <b>${kind[r.event] || "Tick"}</b><span>${esc(laneText(r))}</span>${(r.for_you || []).length ? `<span class="pill">sent to ${first}</span>` : ""}${r.refused_change ? `<span class="pill ro" title="A change asked through an assistant is refused: the customer confirms it in the app or website">read-only</span>` : ""}</div>`).join("");
  const helpers = (o.assistants || []).length ? ` and ${first}'s AI assistants` : "";
  $("bgLane").innerHTML = `<div class="bg-h">In the background · Orbit's clock (${o.every_s ? `a tick every ${o.every_s} s` : "off"}, code only)${helpers}</div>${rows || `<div class="muted">No tick yet.</div>`}`;
}

async function showWatchStarted(n) {  // end of a customer turn: the gateway asked the Guardian to follow issues up
  if (!n || !AGENTS.guardian) return;
  drawMap(null, { background: true, extra: { from: "support_orchestrator", to: "guardian", label: `start watch (${n})` } });
  await sleep(1800);
  if (!st.working && !bg.playing) drawMap();
}

// ------------------------------------------------------------------ case and session panels
const STATUS = { open: ["open", "info"], investigating: ["investigating", "info"], proposed: ["fix proposed", "warn"],
  awaiting_customer: ["waiting for the customer", "info"],
  executed_verified: ["fixed and verified", "good"], resolved_info_only: ["answered", "good"],
  queued_for_review: ["waiting for a person", "warn"], handed_off: ["with a person", "warn"] };
const OUTCOME = { resolved: ["resolved without a person", "good"], closed_with_handoff: ["closed, with a person", "warn"],
  open: ["in progress", "info"], no_issues: ["no issues yet", "info"] };
const badge = (map, key, text) => `<span class="st ${(map[key] || [key, ""])[1]}">${esc(text || (map[key] || [key])[0])}</span>`;
const words = (s) => String(s ?? "").replace(/_/g, " ");

function renderCase(view) {
  st.caseView = view || st.caseView;
  view = st.caseView;
  if (!view) { $("casebox").innerHTML = `<div class="empty">No case yet. ${esc(label("intake_identity"))} opens one on the first message.</div>`; return; }
  const issues = view.issues.map((i) => {
    const prop = i.proposal ? `<div class="muted mono">${esc(i.proposal.action)} → ${esc(i.proposal.target_id)}</div>` : "";
    // shopping issues are answered by the recommendation engine, not by a specialist agent
    const who = i.domain === "shopping" ? `<span class="tag" style="background:var(--accent)">Suggestions</span>` : tag(i.domain);
    return `<tr><td>${esc(i.issue_id.split("-").pop())}</td><td>${who}</td><td>${esc(i.summary)}${prop}</td><td>${badge(STATUS, i.status)}</td></tr>`;
  }).join("");
  const actions = view.issues.filter((i) => i.outcome && i.outcome.outcome && i.proposal).map((i) => `<tr><td class="mono">${esc(i.proposal ? i.proposal.action : "")}</td>
    <td class="mono">${esc(i.proposal ? i.proposal.target_id : "")}</td><td>${i.outcome.amount != null ? "$" + Number(i.outcome.amount).toFixed(2) : "–"}</td>
    <td>${badge({ executed: ["executed", "good"], queued: ["queued for a person", "warn"], denied: ["refused", "bad"] }, i.outcome.outcome, words(i.outcome.outcome))}</td>
    <td class="mono">${esc(i.outcome.reference || "–")}</td></tr>`).join("");
  const policy = st.policy.map((p) => `<tr><td class="mono">${esc(p.action)}</td><td>${badge({ ALLOW: ["ALLOW", "good"], DENY: ["DENY", "bad"], ESCALATE: ["ESCALATE", "warn"] }, p.recommendation)}</td>
    <td>${esc(words(p.reason))}</td></tr>`).join("");
  const handoffs = view.handoffs.map((h) => `<tr><td class="mono">${esc(h.handoff_id)}</td><td>${esc(words(h.reason))}</td><td>${esc(words(h.status))}</td></tr>`).join("");
  const suggested = st.suggestions.map((s) => s.items.length
    ? s.items.map((i) => `<tr><td>${esc(i.title)}</td><td>${esc(words(i.reason))}</td><td>${money(i.list_price, i.currency)}</td></tr>`).join("")
    : `<tr><td colspan="3" class="muted">None offered: ${esc(words(s.reason || s.status))}</td></tr>`).join("");
  const outcome = (view.summary || {}).outcome;
  $("casebox").innerHTML = `<div class="kv"><div class="muted">Case</div><div class="mono">${esc(view.case_id)}</div>
      <div class="muted">Identity</div><div>${badge({ verified: ["verified", "good"], gateway_authenticated: ["verified", "good"] }, view.identity_status, words(view.identity_status))}</div>
      <div class="muted">Outcome</div><div>${badge(OUTCOME, outcome)}</div></div>
    <h4>Issues</h4><table><tr><th>#</th><th>Specialist</th><th>Issue</th><th>Status</th></tr>${issues || `<tr><td colspan="4" class="muted">None yet.</td></tr>`}</table>
    <h4>Policy decisions</h4>${policy ? `<table><tr><th>Action</th><th>Decision</th><th>Why</th></tr>${policy}</table>` : `<div class="muted">None yet.</div>`}
    <h4>Actions</h4>${actions ? `<table><tr><th>Action</th><th>Target</th><th>Amount</th><th>Outcome</th><th>Reference</th></tr>${actions}</table>` : `<div class="muted">None yet.</div>`}
    ${suggested ? `<h4>Product suggestions</h4><table><tr><th>Product</th><th>Why (engine)</th><th>Price</th></tr>${suggested}</table>` : ""}
    ${handoffs ? `<h4>With a person</h4><table><tr><th>Hand-off</th><th>Reason</th><th>Status</th></tr>${handoffs}</table>` : ""}`;
}

function renderSession(turn) {
  if (!S) return;
  const cost = (turn && turn.cost) || {};
  $("sessionbox").innerHTML = `<h4 style="margin-top:0">Sent by the website gateway (sly_data)</h4><pre class="sbox mono">${esc(JSON.stringify(S.sly, null, 2))}</pre>
    <div class="note">Built by the demo console from the simulated sign-in. The browser never sends sly_data, and the model never sees it.</div>
    <h4>Kept by the gateway after the turn</h4>${turn ? `<pre class="sbox mono">${esc(JSON.stringify(turn.sly_data_after, null, 2))}</pre>
      <div class="note">The network returns only <span class="mono">case_id</span> (allow.to_upstream); it is sent back with the next message.</div>` : `<div class="muted">After the first answer.</div>`}
    <h4>Identity</h4><div>${st.caseView ? `Resolved in code: <b>${esc(words(st.caseView.identity_status))}</b>` : `<span class="muted">After the first answer.</span>`}</div>
    <h4>Last turn</h4>${turn ? `<div class="kv"><div class="muted">LLM calls</div><div>${cost.llm_calls ?? "?"}</div>
      <div class="muted">Tokens</div><div>${cost.tokens != null ? Number(cost.tokens).toLocaleString() : "?"}</div>
      <div class="muted">Time</div><div>${Math.round(turn.t)} s</div></div>` : `<div class="muted">–</div>`}
    <h4>Setup</h4><div class="kv"><div class="muted">Network</div><div class="mono">${esc(META.network)}</div>
      <div class="muted">Customer data</div><div>${esc(META.backend)}</div><div class="muted">Model</div><div class="mono">${esc(META.model)}</div></div>`;
}

// ------------------------------------------------------------------ inbox and Resolution Guardian (E3)
// The inbox shows what the customer receives (the outbox as a channel gateway delivers it). The simulator buttons
// make business events happen; which ones are possible for a watch is decided by the server.
const WATCH_STATE = { watching: ["first check pending", "info"], waiting_on_business: ["waiting on ACME Retail", "info"],
  waiting_on_customer: ["waiting on the customer", "info"], escalated: ["with a person", "warn"],
  resolved: ["resolved · update waiting", "good"], closed: ["closed", "good"] };
const fmtSim = (ts) => ts ? new Date(ts).toLocaleString([], { weekday: "short", day: "numeric", month: "short",
  hour: "2-digit", minute: "2-digit", timeZone: "UTC" }) + " UTC" : "–";
let inboxSeen = -1, inboxTimer = null;

async function loadInbox() {
  if (!S || !S.id) return;
  try { renderInbox(await api(`/api/session/${S.id}/inbox`)); } catch (e) { /* the next poll tries again */ }
}

function renderInbox(box) {
  if (inboxSeen < 0 || $("tab-inbox").classList.contains("on")) inboxSeen = box.messages.length;
  const unseen = box.messages.length - inboxSeen;
  $("inboxCount").hidden = unseen <= 0; $("inboxCount").textContent = unseen;
  const o = box.orbit || {};
  const orbitNote = o.every_s ? `<div class="note">Orbit checks the follow-ups: a tick every ${o.every_s} s${o.last_at
    ? `, last at ${esc(new Date(o.last_at * 1000).toLocaleTimeString())}` : ""}${o.last_error ? ` (not done: ${esc(o.last_error)})` : ""}.
      The buttons below move the simulated time (what the data and the Guardian see) or make something happen; Orbit
      ticks at once.</div>` : "";
  const sim = box.simulator ? `${orbitNote}<div class="simbar"><span class="clockv">Simulated time · ${esc(fmtSim(box.clock))}</span>
      <span class="muted">Advance</span><button class="btn" data-adv="6">+6 h</button><button class="btn" data-adv="24">+1 day</button>
      <button class="btn" data-adv="72">+3 days</button></div>`
    : `<div class="note">The event simulator is off (start the demo with scripts\\demo.ps1): nothing will happen to watched issues.</div>`;
  const watches = box.watches.map((w) => `<div class="watch"><div class="row"><b class="mono">${esc(w.issue_id.split("-").pop())}</b>
      <span class="mono muted">${esc(w.case_id)}</span>${badge(WATCH_STATE, w.state)}<span class="muted">waiting for ${esc(w.waiting_for)}</span></div>
      <div class="muted">Last checked ${esc(fmtSim(w.last_checked_at))} · updates sent ${w.updates_sent ?? 0}${w.closed_reason ? " · " + esc(words(w.closed_reason)) : ""}</div>
      ${box.simulator && w.events.length ? `<div class="acts">${w.events.map((e) =>
        `<button class="btn" data-watch="${esc(w.watch_id)}" data-ev="${esc(e.event)}">${esc(e.label)}</button>`).join("")}</div>` : ""}</div>`).join("");
  const msgs = box.messages.slice().reverse().map((m) => `<div class="imsg"><div class="meta2">${({ guardian: "Resolution Guardian", stylist: "Personal Stylist" })[m.source] || "Support team"}
      · ${esc(m.clock ? fmtSim(m.clock) : (m.at || ""))}</div>${esc(m.message)}</div>`).join("");
  const held = box.held.map((m) => `<div class="imsg held"><div class="meta2">Waiting to be sent · ${esc(words(m.why))}</div>${esc(m.message)}</div>`).join("");
  $("inboxbox").innerHTML = `${sim}
    <h4>Messages to ${esc(S.customer.name)}</h4>${held}${msgs || `<div class="muted">No messages yet.</div>`}
    <label class="switch" style="margin-top:8px;display:inline-flex;gap:6px"><input type="checkbox" id="updatesOn" ${box.updates_on ? "checked" : ""}> Proactive updates</label>
    <h4>Watched by the Resolution Guardian</h4>${watches || `<div class="muted">Nothing yet. After a turn, issues still in
      progress (a refund, a parcel, a review) are watched until the outcome is confirmed.</div>`}`;
  $("inboxbox").querySelectorAll("[data-adv]").forEach((b) => b.onclick = () => guardianCall("advance", { hours: Number(b.dataset.adv) }));
  $("inboxbox").querySelectorAll("[data-ev]").forEach((b) => b.onclick = () => guardianCall("event", { watch_id: b.dataset.watch, event: b.dataset.ev }));
  $("updatesOn").onchange = (e) => guardianCall("updates", { on: e.target.checked });
}

async function guardianCall(path, body) {
  $("inboxbox").querySelectorAll("button, input").forEach((b) => b.disabled = true);
  try { renderInbox(await api(`/api/session/${S.id}/guardian/${path}`, { method: "POST", body: JSON.stringify(body) })); }
  catch (e) { addSys(`Simulator: ${e.message}`); loadInbox(); }
  pollClock();  // the tick it caused, on the map
}

// ------------------------------------------------------------------ My Companion (companion plan P7)
// What the signed-in customer sees and controls. Every value comes from the server (the companion store, the
// Guardian, the Stylist and the data); clicks here are the customer's own actions in their account.
let MC = null, mcTab = "chat", mcBusy = false;
const KIND_WORDS = { likes: "Likes", avoids: "Avoids", size: "Size", occasion: "Occasion" };
const FAMILY_SAID = { dark_neutral: "dark neutrals", light_neutral: "light neutrals", earth: "earth tones", blue: "blues",
  green: "greens", warm: "warm colours" };
const CONSENT_SAID = { service_updates: "Proactive updates", stylist_mode: "Personal Stylist Mode", marketing: "News and offers" };

document.querySelectorAll("#ctabs button").forEach((b) => b.onclick = () => showCTab(b.dataset.ctab));
function showCTab(name) {
  mcTab = name;
  document.querySelectorAll("#ctabs button").forEach((x) => x.classList.toggle("on", x.dataset.ctab === name));
  document.querySelectorAll(".ctab").forEach((t) => t.classList.toggle("on", t.id === "ctab-" + name));
  if (name !== "chat") loadMC();
}

async function loadMC() {
  if (REPLAY || !S || !S.customer.customer_id || mcBusy) return;
  try { renderMC(await api(`/api/session/${S.id}/my-companion`)); } catch (e) { /* the next refresh tries again */ }
}

// errorAt: the id of the element to show an error under (looked up after re-rendering, which replaces it).
async function mcCall(path, opts, errorAt) {
  mcBusy = true;
  document.querySelectorAll(".mc button, .mc input, .mc select").forEach((x) => x.disabled = true);
  try { renderMC(await api(`/api/session/${S.id}/my-companion${path}`, opts)); }
  catch (e) {
    renderMC(MC);
    const at = errorAt && $(errorAt);
    if (at) at.insertAdjacentHTML("afterend", `<div class="mc-err">${esc(e.message)}</div>`);
  }
  finally { mcBusy = false; document.querySelectorAll(".mc button, .mc input, .mc select").forEach((x) => x.disabled = false); }
}
const post = (body) => ({ method: "POST", body: JSON.stringify(body) });

function renderMC(v) {
  if (!v) return;
  MC = v;
  renderUpdatesTab(v); renderStylistTab(v); renderKnowTab(v); renderSettingsTab(v);
}

function renderUpdatesTab(v) {
  const name = v.companion.name;
  const followed = v.updates.followed.map((w) => `<div class="mc-item"><div class="row"><b>${esc(w.what)}</b>
      ${badge(WATCH_STATE, w.state)}</div>
      <div class="muted">Case ${esc(w.case_id)} · ${w.state === "closed" ? "done" : "waiting for " + esc(w.waiting_for)}
      · last checked ${esc(fmtSim(w.last_checked_at))}</div></div>`).join("");
  const msgs = v.updates.messages.map((m) => `<div class="imsg"><div class="meta2">${({ guardian: "Update", stylist: "New in · Personal Stylist" })[m.source] || "From the support team"}
      · ${esc(m.clock ? fmtSim(m.clock) : (m.at || ""))}</div>${esc(m.text)}</div>`).join("");
  const on = v.companion.consents.service_updates.granted;
  $("mcUpdates").innerHTML = `<p class="lead">${esc(name)} keeps following your requests after the chat ends and tells you
      when something changes${on ? "" : " (proactive updates are off: switch them on in Settings)"}.</p>
    <h4>Being followed</h4>${followed || `<div class="muted">Nothing is being followed right now.</div>`}
    <h4>Messages</h4>${msgs || `<div class="muted">No messages yet.</div>`}`;
}

function renderStylistTab(v) {
  const s = v.stylist;
  const picks = s.picks === null ? `<div class="mc-err">Picks cannot be shown right now.</div>`
    : s.picks.map((p) => `<div class="pick"><div class="pc-top"><span><span class="pc-cat">${esc(p.category)}</span><span class="mc-src"> · new in · sent ${esc(p.sent_on)}</span></span>
        <span class="pc-price">${money(p.list_price, p.currency)}</span></div>
        <div class="pc-title">${esc(p.title)}</div><div class="pc-desc">${esc(p.description)}</div>
        <div class="mc-src" style="margin-top:6px">Why ${esc(v.companion.name)} picked it</div>
        <ul>${p.picked_because.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
        <div class="pc-opts">In stock in ${esc(p.in_stock_now.size)}: ${p.in_stock_now.colours.map((c) => `<span class="pc-chip">${esc(c)}</span>`).join("")}</div>
        <div class="acts">${p.your_feedback === "not_for_me" ? `<span class="st warn">You said: not for me</span>`
          : `<button class="btn small" data-nfm="${esc(p.product_id)}">Not for me</button>`}
          <span class="muted">Nothing is ordered from here: it's on the product page.</span></div></div>`).join("");
  $("mcStylist").innerHTML = `<label class="toggle"><input type="checkbox" id="mcStylistOn" ${s.on ? "checked" : ""}>
      <div><div class="t">Personal Stylist Mode</div><div class="d">When something new launches, ${esc(v.companion.name)} checks
      it against what you keep, your sizes and what you told it you like. You hear about it only when it really suits
      you and goes with something you own: at most ${v.limits.invites_per_week} a week, never while a request of yours is
      with a colleague. Switch it off at any time.</div></div></label>
    <h4>Your picks</h4>${picks || `<div class="muted">${s.on ? "No picks yet: you'll see them here when something new really suits you."
      : "Stylist Mode is off, so no picks are sent."}</div>`}`;
  $("mcStylistOn").onchange = (e) => mcCall("/consent", post({ consent: "stylist_mode", granted: e.target.checked }));
  $("mcStylist").querySelectorAll("[data-nfm]").forEach((b) => b.onclick = () =>
    mcCall("/pick-feedback", post({ product_id: b.dataset.nfm, kind: "not_for_me" })));
}

function renderKnowTab(v) {
  const p = v.profile, src = (k) => `<span class="mc-src">From ${esc((p.sources || {})[k] || "your records")}</span>`;
  const c = p.customer || {};
  const facts = [];
  if (p.customer) facts.push(["About you", `${esc(c.first_name || "")}${c.tier ? ` · ${esc(c.tier)} tier` : ""}${c.points != null ? ` · ${Number(c.points).toLocaleString()} points` : ""}${c.member_since ? ` · member since ${esc(c.member_since)}` : ""}`, "customer"]);
  if (p.sizes && Object.keys(p.sizes).length) facts.push(["Usual sizes", Object.entries(p.sizes).map(([k, s]) => `${esc(k)} ${esc(s)}`).join(", "), "sizes"]);
  if (p.colours && p.colours.length) facts.push(["Favourite colours", esc(p.colours.join(", ")), "colours"]);
  if (p.styles && p.styles.length) facts.push(["Styles you keep", esc(p.styles.join(", ")), "styles"]);
  if (p.colour_families && p.colour_families.length) facts.push(["Colours you wear", esc(p.colour_families.map((f) => FAMILY_SAID[f] || f).join(" and ")), "colour_families"]);
  if (p.categories && p.categories.length) facts.push(["What you keep most", esc(p.categories.map((x) => `${x.category} (${x.kept})`).join(", ")), "categories"]);
  if (p.price) facts.push(["Usual price", `${money(p.price.typical, p.price.currency)} (from ${money(p.price.min, p.price.currency)} to ${money(p.price.max, p.price.currency)})`, "price"]);
  if (p.returns && p.returns.count) facts.push(["Returns", esc(Object.entries(p.returns.reasons).map(([r, n]) => `${words(r)} ${n}`).join(", ")), "returns"]);
  const prefs = (p.preferences || []).map((m) => `<div class="mc-pref"><span class="k">${esc(KIND_WORDS[m.kind] || m.kind)}</span>
      <span class="v">${esc(m.value)} <span class="mc-src">· you told us ${esc(m.since)}</span></span>
      <button class="btn small" data-del="${esc(m.memory_id)}">Delete</button></div>`).join("");
  const recall = (p.recall || []).map((r) => `<li>${esc(r.text)}</li>`).join("");
  $("mcKnow").innerHTML = `<p class="lead">Everything ${esc(v.companion.name)} knows about you, and where it comes from. What it
      works out from your orders is never guessed; what you told it is kept only because you said yes.</p>
    <h4>From your account and orders</h4>${facts.map(([k, val, s]) => `<div class="mc-pref"><span class="k">${esc(k)}</span>
      <span class="v">${val}<br>${src(s)}</span></div>`).join("") || `<div class="muted">Nothing yet.</div>`}
    ${(p.unavailable || []).length ? `<div class="note">Not available right now: ${esc(p.unavailable.join(", "))}.</div>` : ""}
    <h4>What you told ${esc(v.companion.name)}</h4>${prefs || `<div class="muted">Nothing yet. In the chat, ask ${esc(v.companion.name)} to remember something, or add it here.</div>`}
    <form class="mc-form" id="mcAdd"><select id="mcKind">${v.memory_kinds.map((k) => `<option value="${k}">${esc(KIND_WORDS[k] || k)}</option>`).join("")}</select>
      <input id="mcValue" maxlength="60" placeholder="e.g. navy and earth tones · footwear 10"><button class="btn small primary">Add</button></form>
    <h4>Earlier requests</h4>${recall ? `<ul class="mc-list">${recall}</ul>` : `<div class="muted">None remembered.</div>`}
    <h4>Your data</h4><div class="mc-form"><a class="btn small" href="/api/session/${S.id}/my-companion/export" download>Download my companion data</a>
      <button class="btn small danger" id="mcReset">Reset my companion</button></div>
    <div class="mc-src" style="margin-top:6px">Reset forgets the name you gave it, everything you told it and your earlier requests. Your
      settings stay as you set them; your orders are not affected.</div>`;
  $("mcKnow").querySelectorAll("[data-del]").forEach((b) => b.onclick = () =>
    mcCall(`/memory/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }, "mcKnow"));
  $("mcAdd").onsubmit = (e) => { e.preventDefault();
    mcCall("/memory", post({ kind: $("mcKind").value, value: $("mcValue").value }), "mcAdd"); };
  armed($("mcReset"), "Click again to reset", () => mcCall("/reset", { method: "POST" }, "mcKnow"));
}

// A two-step button instead of a browser confirm dialog: the first click arms it for 4 seconds.
function armed(btn, prompt, action) {
  const label = btn.textContent;
  btn.onclick = () => {
    if (btn.classList.contains("armed")) { btn.classList.remove("armed"); btn.textContent = label; action(); return; }
    btn.classList.add("armed"); btn.textContent = prompt;
    setTimeout(() => { btn.classList.remove("armed"); btn.textContent = label; }, 4000);
  };
}

function renderSettingsTab(v) {
  const c = v.companion.consents;
  const toggle = (key, desc) => `<label class="toggle"><input type="checkbox" data-consent="${key}" ${c[key].granted ? "checked" : ""}>
      <div><div class="t">${CONSENT_SAID[key]}</div><div class="d">${desc}${c[key].set_by === "customer"
        ? ` · set by you ${esc((c[key].at || "").slice(0, 10))}` : " · default"}</div></div></label>`;
  const [qs, qe] = v.limits.quiet_hours;
  const assistants = v.assistants.map((g) => `<div class="mc-pref"><span class="v"><b>${esc(g.client_name)}</b>
      <span class="mc-src">· can ${esc(g.scopes.map(words).join(", "))} · connected ${esc(new Date(g.created_at * 1000).toISOString().slice(0, 10))}</span></span>
      <button class="btn small danger" data-revoke="${esc(g.grant_id)}">Disconnect</button></div>`).join("");
  const history = v.consent_history.slice(0, 6).map((h) => `<li>${esc(CONSENT_SAID[h.consent] || h.consent)} ${h.granted ? "on" : "off"}
      · ${esc(h.at.slice(0, 16).replace("T", " "))} · ${esc(h.channel)}</li>`).join("");
  $("mcSettings").innerHTML = `<h4>Your companion</h4><form class="mc-form" id="mcName"><input id="mcNameIn" maxlength="24"
      value="${esc(v.companion.named_by_customer ? v.companion.name : "")}" placeholder="Everly"><button class="btn small primary">Save name</button></form>
    <div class="mc-src" style="margin-top:4px">Leave empty to use Everly.</div>
    <h4>Messages from ${esc(v.companion.name)}</h4>
    ${toggle("service_updates", "Updates on requests it is following: a refund going through, a parcel moving.")}
    ${toggle("stylist_mode", "New-in picks that really suit you (see Stylist).")}
    ${toggle("marketing", "General news and offers. Not used in this demo.")}
    <div class="mc-src" style="margin-top:6px">Never between ${qs}:00 and ${qe}:00, and at most ${v.limits.per_day} a day in total;
      updates on your requests always come first.</div>
    <h4>Your own AI assistant</h4>${assistants || `<div class="muted">No assistant is connected. You can connect one (e.g. Claude) to
      read your orders and requests; it can never change anything.</div>`}
    ${history ? `<h4>Recent choices</h4><ul class="mc-list mc-src">${history}</ul>` : ""}`;
  $("mcSettings").querySelectorAll("[data-consent]").forEach((x) => x.onchange = () =>
    mcCall("/consent", post({ consent: x.dataset.consent, granted: x.checked }), "mcSettings"));
  $("mcName").onsubmit = (e) => { e.preventDefault(); mcCall("/name", post({ name: $("mcNameIn").value }), "mcName"); };
  $("mcSettings").querySelectorAll("[data-revoke]").forEach((b) =>
    armed(b, "Click again", () => mcCall(`/assistants/${encodeURIComponent(b.dataset.revoke)}/revoke`, { method: "POST" }, "mcSettings")));
}

// ------------------------------------------------------------------ Launch panel (business side, behind the scenes)
const OUTCOME_SAID = { invite: ["invited", "good"], below_threshold: ["below 80", "info"], gated: ["not eligible", ""],
  no_owned_item: ["nothing of theirs to name", "info"], data_unavailable: ["data unavailable", "bad"] };

async function loadLaunches(after) {
  try { renderLaunches(after || await api("/api/launches")); }
  catch (e) { $("launchbox").innerHTML = `<div class="mc-err">${esc(e.message)}</div>`; }
}

function renderLaunches(v) {
  const rows = v.launches.map((l) => {
    const s = l.summary;
    const gated = s ? Object.entries(s.gated || {}).map(([g, n]) => `${words(g)} ${n}`).join(", ") : "";
    return `<div class="launch"><div class="row"><b>${esc(l.title)}</b><span class="muted">${money(l.list_price, "USD")}</span>
        ${s ? badge({ x: ["launched", "good"] }, "x") : `<button class="btn small primary" data-launch="${esc(l.product_id)}">Launch now</button>`}</div>
      <div class="muted">${esc(l.headline)} · planned ${esc(fmtSim(l.planned_at))}${s ? ` · launched ${esc(fmtSim(s.launched_at))}` : ""}</div>
      ${s ? `<div style="margin-top:4px">${s.candidates} with Stylist Mode on · ${s.invited.length} invited · ${s.below_threshold} below
        ${v.min_affinity}${gated ? ` · not eligible: ${esc(gated)}` : ""}
        <button class="btn small" data-scores="${esc(l.product_id)}">Scores</button></div><div id="sc-${esc(l.product_id)}"></div>` : ""}</div>`;
  }).join("");
  $("launchbox").innerHTML = `<div class="simbar"><span class="clockv">Clock · ${esc(fmtSim(v.clock))}</span>
      <span class="muted">${v.opted_in} customer(s) with Personal Stylist Mode on</span></div>
    <div class="note">The merchandiser's view. Launching scores every customer who switched Personal Stylist Mode on (in My
      Companion) and sends an invite only at ${v.min_affinity} or more with an item they own to name. Code decides; no model.</div>
    ${rows || `<div class="muted">Nothing staged.</div>`}`;
  $("launchbox").querySelectorAll("[data-launch]").forEach((b) => b.onclick = async () => {
    b.disabled = true; b.textContent = "Launching…";
    try { await loadLaunches(await api(`/api/launches/${encodeURIComponent(b.dataset.launch)}`, { method: "POST" })); loadInbox(); loadMC(); pollClock(); }
    catch (e) { b.disabled = false; b.textContent = "Launch now"; b.insertAdjacentHTML("afterend", `<span class="mc-err"> ${esc(e.message)}</span>`); }
  });
  $("launchbox").querySelectorAll("[data-scores]").forEach((b) => b.onclick = () => showScores(b.dataset.scores));
}

async function showScores(pid) {
  const box = $("sc-" + pid);
  if (box.innerHTML) { box.innerHTML = ""; return; }
  const v = await api(`/api/launches/${encodeURIComponent(pid)}/scores?limit=15`);
  const rows = v.scores.map((r) => `<tr><td class="mono">${esc(r.customer_id)}${S && r.customer_id === S.customer.customer_id ? " (signed in)" : ""}</td>
      <td><b>${r.score ?? "–"}</b></td><td>${badge(OUTCOME_SAID, r.outcome)}${r.gates.length ? ` <span class="muted">${esc(r.gates.map(words).join(", "))}</span>` : ""}</td>
      <td class="pts">${Object.entries(r.points).map(([k, p]) => `${k} ${p}`).join(" · ")}${r.penalty ? ` · −${r.penalty} feedback` : ""}</td>
      <td>${esc(r.owned_item || "–")}</td><td>${r.invite ? esc(words(r.invite.status) + (r.invite.held_reason ? ` (${words(r.invite.held_reason)})` : "")) : "–"}</td></tr>`).join("");
  box.innerHTML = `<table class="scores" style="margin-top:6px"><tr><th>Customer</th><th>Score</th><th>Outcome</th><th>Points</th><th>Goes with</th><th>Invite</th></tr>${rows}</table>
    <div class="mc-src">Best ${v.scores.length} of ${v.total}. Every score is stored with its breakdown.</div>`;
}

// ------------------------------------------------------------------ replay (video renderer)
async function runReplay(name) {
  const sb = await api(`/api/storyboard/${encodeURIComponent(name)}`);
  if (sb.theme) document.documentElement.dataset.theme = sb.theme;
  $("clock").hidden = !sb.clock;
  document.body.classList.add("replay");
  let realTotal = 0, realBase = 0;
  const start = performance.now();
  const shown = () => (performance.now() - start) / 1000;
  const clock = () => { $("clock").textContent = `real time ${fmtClock(realTotal)} · shown ${fmtClock(shown())}`; };
  const timer = setInterval(clock, 250);
  const handlers = {
    card: (s) => showCard(s.card, s.dur),
    signin: (s) => replaySignIn(sb, s.dur),
    type: (s) => { realBase = realTotal; replayType(s.text, s.dur); },
    event: (s) => {
      if (s.skipped) skipChip(s.skipped);
      realTotal = realBase + (s.event.t || 0);
      handle(s.event);
    },
    caption: (s) => flash($("caption"), s.text, s.dur),
    subtitle: (s) => flash($("subtitle"), s.text, s.dur),
    tab: (s) => showTab(s.tab),
    end: () => { clearInterval(timer); clock(); window.__replayDone = true; },
  };
  window.__replayStartedAt = Date.now();
  for (const step of sb.steps) setTimeout(() => (handlers[step.kind] || (() => {}))(step), step.at * 1000);
}

function showCard(card, dur) {
  const mark = `<div class="logo">${$("mark").innerHTML}</div>`;
  const from = META.retailer ? `<div class="from">${fromRetailer(META.retailer)}</div>` : "";
  const brand = `${mark}<div class="b">${esc(card.brand || META.brand)}</div>${from}<div class="tg">${esc(card.tagline || META.tagline)}</div>`;
  const list = (xs) => xs && xs.length ? `<ul>${xs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const stats = (xs) => xs && xs.length ? `<div class="stats">${xs.map((x) => `<span>${esc(x)}</span>`).join("")}</div>` : "";
  $("cardbox").innerHTML = brand + (card.headline ? `<div class="h">${esc(card.headline)}</div>` : "")
    + (card.subline ? `<div class="s">${esc(card.subline)}</div>` : "") + list(card.lines) + stats(card.stats);
  $("overlay").classList.add("show");
  setTimeout(() => $("overlay").classList.remove("show"), Math.max(0, dur * 1000 - 500));
}

function replaySignIn(sb, dur) {
  pick.list = [sb.customer]; pick.total = 1; pick.selected = null;
  $("search").value = sb.customer.name; renderCards();
  setTimeout(() => select(sb.customer), dur * 300);
  setTimeout(() => openConsole({ session_id: null, customer: sb.customer, sly_data_sent: sb.sly_data_sent }), dur * 850);
}

function replayType(text, dur) {
  const n = text.length, step = (dur * 0.8 * 1000) / Math.max(n, 1);
  $("input").value = "";
  for (let i = 1; i <= n; i++) setTimeout(() => { $("input").value = text.slice(0, i); }, i * step);
  setTimeout(() => { $("input").value = ""; customerSends(text); }, dur * 1000 - 50);
}

function skipChip(seconds) {
  const c = document.createElement("div"); c.className = "skipchip"; c.textContent = `⏩ ${Math.round(seconds)} s of model thinking skipped`;
  if (st.working) $("msgs").insertBefore(c, st.working); else $("msgs").appendChild(c);
  const r = document.createElement("div"); r.className = "ev skip"; r.textContent = `⏩ ${Math.round(seconds)} s skipped`;
  $("timeline").appendChild(r);
  setTimeout(() => c.remove(), 1600);
}

function flash(el, text, dur) {
  el.textContent = text; el.classList.add("show");
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), dur * 1000);
}

init().catch((e) => { document.body.insertAdjacentHTML("beforeend", `<div class="note" style="margin:16px">Start-up failed: ${esc(e.message)}</div>`); });
