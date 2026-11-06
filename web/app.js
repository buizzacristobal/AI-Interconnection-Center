"use strict";
/* AI Interconnection Center dashboard. Vanilla JS, no build step, no CDN. */

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const local = {
  get(k, d) { try { const v = localStorage.getItem("aic." + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("aic." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};
const HUMAN = "Human";
const STATUSES = [["todo", "To do"], ["doing", "Doing"], ["review", "Review"], ["done", "Done"]];

const S = {
  games: [], game: null, cfg: null, agents: {}, msgs: [], total: 0, lastLine: 0, searching: false,
  team: null, tasks: [], claims: [], tree: null, project: null, view: "chat",
  es: null, pollTimer: null, key: local.get("key", ""), connectFmt: "rules", seenErrors: {},
};

/* ---------------------------------------------------------------- api */

async function api(path, body) {
  const headers = { "X-AIC-Client": "web" };
  if (S.key) headers["X-AIC-Key"] = S.key;
  const opts = { headers, method: body === undefined ? "GET" : "POST" };
  if (body !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  if (res.status === 401) { askKey(); throw new Error("Access key required"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const e = new Error((data && data.error) || res.statusText);
    e.status = res.status; e.data = data; throw e;
  }
  return data;
}
const gq = () => "game=" + encodeURIComponent(S.game);

function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => t.classList.remove("show"), 2400);
}

async function copy(text, what = "Copied") {
  try { await navigator.clipboard.writeText(text); }
  catch {
    const ta = document.createElement("textarea");
    ta.value = text; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast(what);
}

/* ---------------------------------------------------------------- formatting */

function relTime(ts) {
  if (!ts) return "";
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (!t) return "";
  const s = (Date.now() - t) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  const d = new Date(t);
  if (s < 6 * 86400) return d.toLocaleDateString(undefined, { weekday: "short" }) + " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  return d.toLocaleDateString();
}
const fullTime = (ts) => (ts ? new Date(ts).toLocaleString() : "");

function agentInfo(name) {
  if (name === HUMAN) return { name, color: "", human: true };
  return S.agents[name] || { name, color: "#8a94a6" };
}

function avatar(name, cls = "") {
  const a = agentInfo(name);
  return `<div class="avatar ${a.human ? "human" : ""} ${cls}" style="--c:${a.color}">${esc(name[0] || "?")}</div>`;
}

function formatText(text) {
  const parts = String(text).split(/```/);
  return parts.map((seg, i) => {
    if (i % 2) {
      const body = seg.replace(/^[\w+-]*\n/, "");
      return `<pre><code>${esc(body)}</code></pre>`;
    }
    let h = esc(seg);
    h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    h = h.replace(/(https?:\/\/[^\s<"')]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');
    h = h.replace(/(^|[^\w@/.])@([A-Za-z][\w-]{1,23})/g, (m, pre, n) => {
      const canon = Object.keys(S.agents).concat(HUMAN).find((k) => k.toLowerCase() === n.toLowerCase());
      if (!canon && !/^(all|everyone)$/i.test(n)) return m;
      const c = canon ? agentInfo(canon).color : "";
      return `${pre}<span class="mention${canon === HUMAN ? " me" : ""}" style="--c:${c || "var(--accent)"}">@${esc(n)}</span>`;
    });
    h = h.replace(/\[([^\]\n<>]{2,60})\]/g, '<span class="tag">[$1]</span>');
    return h;
  }).join("");
}

const typeClass = (t) => "t-" + String(t || "").toLowerCase().replace(/\s+/g, "-");

/* ---------------------------------------------------------------- config & projects */

async function loadConfig() {
  S.cfg = await api("/api/config");
  S.agents = {};
  for (const a of S.cfg.agents) S.agents[a.name] = { ...a, hosted: !!(a.provider && a.model && !a.archived) };
  $("#version").textContent = "v" + S.cfg.version;
  const types = S.cfg.types || ["General"];
  $("#composerType").innerHTML = types.map((t) => `<option>${esc(t)}</option>`).join("");
  const hosted = S.cfg.agents.filter((a) => a.provider && a.model && !a.archived).map((a) => "@" + a.name);
  $("#composerHint").textContent = hosted.length
    ? `${hosted.join(", ")} answer${hosted.length === 1 ? "s" : ""} when mentioned · Enter to send, Shift+Enter for a new line`
    : "Enter to send · Shift+Enter for a new line · @Name to address an agent";
}

function activeAgents() { return S.cfg.agents.filter((a) => !a.archived); }

async function loadGames() {
  S.games = await api("/games");
  renderProjects();
}

function renderProjects() {
  const seen = local.get("seen", {});
  $("#projects").innerHTML = S.games.map((g) => {
    const unread = g.id !== S.game && g.last_line > (seen[g.id] || 0) && seen[g.id] !== undefined;
    return `<button class="proj ${g.id === S.game ? "active" : ""}" data-id="${esc(g.id)}" title="${esc(g.id)}">
      <span class="name">${esc(g.label)}</span>${unread ? '<span class="badge">new</span>' : `<span class="n">${g.messages}</span>`}</button>`;
  }).join("");
}

function markSeen() {
  const seen = local.get("seen", {});
  seen[S.game] = S.lastLine;
  for (const g of S.games) if (seen[g.id] === undefined) seen[g.id] = g.last_line;
  local.set("seen", seen);
}

async function selectGame(id) {
  if (!id) return;
  S.game = id;
  local.set("game", id);
  history.replaceState(null, "", "?game=" + encodeURIComponent(id));
  const g = S.games.find((x) => x.id === id) || { label: id };
  $("#projectTitle").textContent = g.label;
  document.title = g.label + " · AI Interconnection Center";
  $("#app").classList.remove("menu-on");
  S.msgs = []; S.lastLine = 0; S.tree = null;
  $("#search").value = ""; $("#agentFilter").value = ""; $("#typeFilter").value = "";
  renderProjects();
  await Promise.all([loadMessages(), loadTeam(), loadTasks(), loadClaims(), loadProject()]);
  markSeen();
  renderProjects();
  if (S.view !== "chat") showView(S.view);
  connectEvents();
}

/* ---------------------------------------------------------------- messages */

async function loadMessages() {
  const d = await api(`/messages?${gq()}&format=json&tail=150`);
  S.msgs = d.messages;
  S.total = d.total;
  S.lastLine = S.msgs.length ? S.msgs[S.msgs.length - 1].line : 0;
  S.searching = false;
  renderMessages(true);
  updateFilters();
}

async function fetchNew() {
  if (S.searching) return;
  const d = await api(`/messages?${gq()}&format=json&from_line=${S.lastLine + 1}`);
  if (!d.messages.length) return;
  const feed = $("#feed");
  const atBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 80;
  S.msgs.push(...d.messages);
  S.total += d.messages.length;
  S.lastLine = S.msgs[S.msgs.length - 1].line;
  $("#messages").insertAdjacentHTML("beforeend", d.messages.map((m, i) => msgHTML(m, S.msgs[S.msgs.length - d.messages.length + i - 1])).join(""));
  bindMore();
  if (atBottom || d.messages.some((m) => m.agent === HUMAN)) scrollBottom();
  else $("#jumpBtn").classList.remove("hidden");
  if (document.visibilityState === "visible") markSeen();
  updateFilters();
  const g = S.games.find((x) => x.id === S.game);
  if (g) { g.messages += d.messages.length; g.last_line = S.lastLine; renderProjects(); }
}

async function loadOlder() {
  const first = S.msgs[0];
  if (!first) return;
  const d = await api(`/messages?${gq()}&format=json&before_line=${first.line}&tail=100`);
  const feed = $("#feed");
  const prevH = feed.scrollHeight;
  S.msgs = d.messages.concat(S.msgs);
  renderMessages(false);
  feed.scrollTop = feed.scrollHeight - prevH;
}

async function runSearch() {
  const q = $("#search").value.trim(), agent = $("#agentFilter").value, type = $("#typeFilter").value;
  if (!q && !agent && !type) { if (S.searching) await loadMessages(); return; }
  const p = new URLSearchParams({ game: S.game, format: "json", tail: "200" });
  if (q) p.set("q", q);
  if (agent) p.set("agent", agent);
  if (type) p.set("type", type);
  const d = await api("/messages?" + p);
  S.searching = true;
  S.msgs = d.messages;
  renderMessages(true, d.total > d.count ? `Showing the newest ${d.count} of ${d.total} matches` : `${d.total} match${d.total === 1 ? "" : "es"}`);
}

function msgHTML(m, prev) {
  const a = agentInfo(m.agent);
  const ts = m.ts ? Date.parse(m.ts) : 0;
  const pts = prev && prev.ts ? Date.parse(prev.ts) : 0;
  let sep = "";
  if (ts && (!pts || new Date(ts).toDateString() !== new Date(pts).toDateString())) {
    sep = `<div class="daysep">${new Date(ts).toLocaleDateString(undefined, { weekday: "long", month: "short", day: "numeric" })}</div>`;
  }
  const cont = !sep && prev && prev.agent === m.agent && ts && pts && ts - pts < 5 * 60e3 && m.type === prev.type;
  const long = m.text.length > 1400 || m.text.split("\n").length > 22;
  const hosted = a.hosted ? '<span class="pill t-hosted" title="Hosted: answers through an AI provider">AI</span>' : "";
  const pill = m.type && m.type !== "General" ? `<span class="pill ${typeClass(m.type)}">${esc(m.type)}</span>` : "";
  return `${sep}<article class="msg ${cont ? "cont" : ""}" id="L${m.line}" style="--c:${a.color}">
    ${avatar(m.agent)}
    <div class="mbody">
      <div class="mhead"><span class="who">${esc(m.agent)}</span>${hosted}${pill}<span class="mtime" data-ts="${esc(m.ts || "")}" title="${esc(fullTime(m.ts))}">${relTime(m.ts)}</span><a class="mline" data-line="${m.line}" title="Copy a reference agents can fetch">L${m.line}</a></div>
      <div class="mtext ${long ? "clamp" : ""}">${formatText(m.text)}</div>
      ${long ? '<button class="more">Show more</button>' : ""}
    </div></article>`;
}

function renderMessages(toBottom, note) {
  const list = S.msgs;
  $("#messages").innerHTML = (note ? `<div class="daysep">${esc(note)}</div>` : "") +
    list.map((m, i) => msgHTML(m, list[i - 1])).join("");
  $("#empty").classList.toggle("hidden", list.length > 0);
  $("#empty").textContent = S.searching ? "No messages match." : "No messages yet. Connect an agent, or say hello.";
  $("#olderWrap").classList.toggle("hidden", S.searching || !list.length || list[0].line <= firstMessageLine());
  bindMore();
  if (toBottom) scrollBottom();
}

function firstMessageLine() {
  const g = S.games.find((x) => x.id === S.game);
  return S.total <= S.msgs.length || !g ? Infinity : 0;
}

function bindMore() {
  $$("#messages .more").forEach((b) => {
    b.onclick = () => {
      const t = b.previousElementSibling;
      t.classList.toggle("clamp");
      b.textContent = t.classList.contains("clamp") ? "Show more" : "Show less";
    };
  });
  $$("#messages .mline").forEach((a) => {
    a.onclick = () => copy(`L${a.dataset.line} in ${S.game}: GET /messages?game=${encodeURIComponent(S.game)}&from_line=${a.dataset.line}&limit=1`, "Reference copied");
  });
}

function scrollBottom() {
  const feed = $("#feed");
  requestAnimationFrame(() => { feed.scrollTop = feed.scrollHeight; });
  $("#jumpBtn").classList.add("hidden");
}

function updateFilters() {
  const agents = new Set(S.msgs.map((m) => m.agent));
  Object.keys(S.agents).forEach((n) => { if (!S.agents[n].archived) agents.add(n); });
  const af = $("#agentFilter"), tf = $("#typeFilter");
  const av = af.value, tv = tf.value;
  af.innerHTML = '<option value="">All agents</option>' + [...agents, HUMAN].filter((v, i, a) => a.indexOf(v) === i)
    .map((n) => `<option ${n === av ? "selected" : ""}>${esc(n)}</option>`).join("");
  const types = new Set(S.msgs.map((m) => m.type));
  (S.cfg.types || []).forEach((t) => types.add(t));
  tf.innerHTML = '<option value="">All types</option>' + [...types].map((t) => `<option ${t === tv ? "selected" : ""}>${esc(t)}</option>`).join("");
}

async function jumpToLine(line) {
  showView("chat");
  const el = document.getElementById("L" + line);
  if (el) {
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash");
    return;
  }
  const d = await api(`/messages?${gq()}&format=json&from_line=${line}&limit=1`);
  const m = d.messages[0];
  if (!m) return toast("Message not found");
  const dlg = document.createElement("dialog");
  dlg.innerHTML = `<div class="msg" style="--c:${agentInfo(m.agent).color}">${avatar(m.agent)}<div class="mbody"><div class="mhead"><span class="who">${esc(m.agent)}</span><span class="pill">${esc(m.type)}</span><span class="mtime">L${m.line} ${relTime(m.ts)}</span></div><div class="mtext">${formatText(m.text)}</div></div></div><div class="dialog-actions"><span class="grow"></span><button class="btn">Close</button></div>`;
  document.body.appendChild(dlg);
  dlg.querySelector("button").onclick = () => dlg.close();
  dlg.addEventListener("close", () => dlg.remove());
  dlg.showModal();
}

/* ---------------------------------------------------------------- composer */

const composer = $("#composerText");

function autosize() { composer.style.height = "auto"; composer.style.height = Math.min(composer.scrollHeight, 240) + "px"; }

async function send() {
  const text = composer.value.trim();
  if (!text) return;
  $("#composerErr").textContent = "";
  $("#sendBtn").disabled = true;
  try {
    const r = await api("/post", { game: S.game, agent: HUMAN, type: $("#composerType").value, text });
    composer.value = ""; autosize();
    $("#composerType").value = "General";
    if (r.triggered && r.triggered.length) toast(r.triggered.join(", ") + " is replying...");
    await fetchNew();
    scrollBottom();
  } catch (e) {
    $("#composerErr").textContent = e.message;
  } finally {
    $("#sendBtn").disabled = false;
    composer.focus();
  }
}

const mention = { open: false, items: [], sel: 0, start: 0 };

function mentionCandidates(prefix) {
  const p = prefix.toLowerCase();
  const list = activeAgents().map((a) => ({ name: a.name, color: a.color, note: a.provider && a.model ? "AI replies" : (a.role || "") }));
  list.push({ name: "all", color: "var(--accent)", note: "everyone" });
  return list.filter((a) => a.name.toLowerCase().startsWith(p)).slice(0, 8);
}

function updateMention() {
  const pos = composer.selectionStart;
  const before = composer.value.slice(0, pos);
  const m = before.match(/(^|\s)@([\w-]*)$/);
  if (!m) return closeMention();
  mention.start = pos - m[2].length;
  mention.items = mentionCandidates(m[2]);
  if (!mention.items.length) return closeMention();
  mention.sel = Math.min(mention.sel, mention.items.length - 1);
  mention.open = true;
  const menu = $("#mentionMenu");
  menu.innerHTML = mention.items.map((a, i) => `<button type="button" class="${i === mention.sel ? "sel" : ""}" data-i="${i}"><span class="sw" style="--c:${a.color}"></span>${esc(a.name)}<small>${esc(a.note)}</small></button>`).join("");
  menu.classList.remove("hidden");
  $$("button", menu).forEach((b) => { b.onmousedown = (e) => { e.preventDefault(); pickMention(+b.dataset.i); }; });
}

function pickMention(i) {
  const a = mention.items[i];
  if (!a) return;
  const v = composer.value, pos = composer.selectionStart;
  composer.value = v.slice(0, mention.start) + a.name + " " + v.slice(pos);
  const caret = mention.start + a.name.length + 1;
  composer.setSelectionRange(caret, caret);
  closeMention();
  composer.focus();
}

function closeMention() { mention.open = false; mention.sel = 0; $("#mentionMenu").classList.add("hidden"); }

/* ---------------------------------------------------------------- team & claims */

async function loadTeam() {
  S.team = await api(`/team?${gq()}`);
  renderTeam();
}
let teamTimer = null;
function loadTeamSoon() { clearTimeout(teamTimer); teamTimer = setTimeout(() => loadTeam().catch(() => {}), 600); }

function renderTeam() {
  if (!S.team) return;
  const now = S.team.now;
  const rows = S.team.agents.filter((a) => !a.archived).map((a) => {
    const ago = a.seen ? now - a.seen : Infinity;
    const lastTs = a.last_ts ? Date.parse(a.last_ts) / 1000 : 0;
    const recent = Math.min(ago, lastTs ? now - lastTs : Infinity);
    return { ...a, ago, recent };
  });
  rows.sort((x, y) => x.recent - y.recent || y.messages - x.messages);
  const active = rows.filter((a) => a.recent < Infinity || a.messages || a.hosted || a.name === HUMAN);
  const idle = rows.filter((a) => !active.includes(a));
  const thinking = [];
  $("#team").innerHTML = active.map((a) => {
    let cls = a.state === "error" ? "err" : a.ago < 180 ? "on" : a.ago < 1800 ? "away" : "";
    let st;
    if (a.state === "thinking") { st = "thinking..."; cls = "on"; thinking.push(a); }
    else if (a.state === "error") st = `<span class="st err">${esc(a.error)}</span>`;
    else if (a.doing) st = esc(a.doing);
    else if (a.ago < 180) st = "online";
    else if (a.last_ts) st = "posted " + relTime(a.last_ts);
    else if (a.messages) st = a.messages + " messages";
    else st = a.hosted ? "ready - @mention to ask" : "";
    const tasks = a.open_tasks ? ` · ${a.open_tasks} task${a.open_tasks > 1 ? "s" : ""}` : "";
    return `<div class="member ${cls}" title="${esc(a.role || "")}">${avatar(a.name)}<div class="info"><div class="nm">${esc(a.name)}${a.hosted ? '<span class="hosted-badge">AI</span>' : ""}</div>${st.startsWith("<span") ? st : `<div class="st">${st}${tasks}</div>`}</div></div>`;
  }).join("") + (idle.length ? `<p class="muted small">${idle.map((a) => esc(a.name)).join(", ")} not connected yet. <a href="#" id="railConnect">Connect</a></p>` : "");
  const rc = $("#railConnect");
  if (rc) rc.onclick = (e) => { e.preventDefault(); showView("connect"); };
  const feed = $("#feed");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
  $("#thinking").innerHTML = thinking.map((a) => `<div class="thinking" style="--c:${agentInfo(a.name).color}"><i></i><i></i><i></i>${esc(a.name)} is thinking...</div>`).join("");
  if (thinking.length && nearBottom && S.view === "chat") scrollBottom();
  for (const a of rows) {
    if (a.state === "error" && a.error && S.seenErrors[a.name] !== a.error) {
      S.seenErrors[a.name] = a.error;
      if (a.hosted) toast(`${a.name}: ${a.error.slice(0, 90)}`);
    }
  }
}

async function loadClaims() {
  const d = await api(`/claims?${gq()}`);
  S.claims = d.claims;
  renderClaims();
}

function renderClaims() {
  $("#claimCount").textContent = S.claims.length ? S.claims.length : "";
  $("#claims").innerHTML = S.claims.length ? S.claims.map((c) => {
    const left = Math.max(0, Math.round((Date.parse(c.expires_at) - Date.now()) / 60000));
    return `<div class="claim"><div class="paths">${c.paths.map(esc).join("<br>")}</div>
      <div class="meta"><span style="color:${agentInfo(c.agent).color};font-weight:600">${esc(c.agent)}</span><span>${left}m left</span><button data-id="${esc(c.id)}" title="Release this claim">release</button></div>
      ${c.note ? `<div class="muted">${esc(c.note)}</div>` : ""}</div>`;
  }).join("") : '<p class="muted small">Nobody is holding files. Agents claim paths before editing so they never collide.</p>';
  $$("#claims button").forEach((b) => {
    b.onclick = async () => {
      try { await api("/release", { game: S.game, agent: HUMAN, id: b.dataset.id }); await loadClaims(); }
      catch (e) { toast(e.message); }
    };
  });
}

/* ---------------------------------------------------------------- tasks */

async function loadTasks() {
  const d = await api(`/tasks?${gq()}`);
  S.tasks = d.tasks;
  const open = S.tasks.filter((t) => t.status !== "done").length;
  $("#taskCount").textContent = open || "";
  if (S.view === "tasks") renderBoard();
}

function renderBoard() {
  $("#board").innerHTML = STATUSES.map(([st, label]) => {
    const list = S.tasks.filter((t) => t.status === st);
    return `<div class="col" data-status="${st}"><div class="col-head">${label} <span class="n">${list.length}</span></div>
      ${list.map((t) => `<div class="task" draggable="true" data-id="${esc(t.id)}">
        <div class="tid">${esc(t.id)}</div><div class="ttl">${esc(t.title)}</div>
        ${t.paths.length ? `<div class="paths">${t.paths.map(esc).join(", ")}</div>` : ""}
        <div class="tfoot">${t.assignee ? `<span class="who" style="color:${agentInfo(t.assignee).color}">@${esc(t.assignee)}</span>` : '<span class="muted">unassigned</span>'}<span title="${esc(fullTime(t.updated_at))}">${relTime(t.updated_at)}</span></div>
      </div>`).join("")}</div>`;
  }).join("");
  $$("#board .task").forEach((el) => {
    el.onclick = () => openTask(S.tasks.find((t) => t.id === el.dataset.id));
    el.ondragstart = (e) => { e.dataTransfer.setData("text/plain", el.dataset.id); el.classList.add("dragging"); };
    el.ondragend = () => el.classList.remove("dragging");
  });
  $$("#board .col").forEach((col) => {
    col.ondragover = (e) => { e.preventDefault(); col.classList.add("drop"); };
    col.ondragleave = () => col.classList.remove("drop");
    col.ondrop = async (e) => {
      e.preventDefault(); col.classList.remove("drop");
      const id = e.dataTransfer.getData("text/plain");
      const t = S.tasks.find((x) => x.id === id);
      if (!t || t.status === col.dataset.status) return;
      t.status = col.dataset.status; renderBoard();
      try { await api("/tasks/update", { game: S.game, agent: HUMAN, id, status: col.dataset.status }); }
      catch (err) { toast(err.message); }
      loadTasks();
    };
  });
}

let editingTask = null;
function openTask(t) {
  editingTask = t || null;
  $("#taskDialogTitle").textContent = t ? `${t.id} · edit task` : "New task";
  $("#taskTitle").value = t ? t.title : "";
  $("#taskDetail").value = t ? t.detail : "";
  $("#taskAssignee").innerHTML = '<option value="">Unassigned</option>' + activeAgents().concat([{ name: HUMAN }])
    .map((a) => `<option ${t && t.assignee === a.name ? "selected" : ""}>${esc(a.name)}</option>`).join("");
  $("#taskStatus").value = t ? t.status : "todo";
  $("#taskPaths").value = t ? t.paths.join("\n") : "";
  $("#taskDelete").classList.toggle("hidden", !t);
  $("#taskErr").textContent = "";
  $("#taskDialog").showModal();
  $("#taskTitle").focus();
}

async function saveTask(e) {
  if (e.submitter && e.submitter.value === "cancel") return;
  e.preventDefault();
  const fields = { title: $("#taskTitle").value, detail: $("#taskDetail").value, assignee: $("#taskAssignee").value,
    status: $("#taskStatus").value, paths: $("#taskPaths").value };
  try {
    if (editingTask) await api("/tasks/update", { game: S.game, agent: HUMAN, id: editingTask.id, ...fields });
    else await api("/tasks", { game: S.game, agent: HUMAN, ...fields });
    $("#taskDialog").close();
    await loadTasks();
  } catch (err) { $("#taskErr").textContent = err.message; }
}

/* ---------------------------------------------------------------- tree */

async function loadTree() {
  S.tree = await api(`/tree?${gq()}`);
  $("#treeMeta").textContent = S.tree.nodes
    ? `v${S.tree.version} · ${S.tree.nodes} nodes · ~${S.tree.est_tokens} tokens · last edit by ${S.tree.updated_by || "?"} ${relTime(S.tree.updated_at)}`
    : "No tree yet. Agents create it with POST /tree, or write one with Edit JSON.";
  renderTree();
}

function renderTree() {
  const f = $("#treeFilter").value.trim().toLowerCase();
  const mark = (s) => (f ? esc(s).replace(new RegExp("(" + f.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig"), "<mark>$1</mark>") : esc(s));
  const matches = (n) => !f || [n.title, n.detail || "", ...(n.refs || [])].join(" ").toLowerCase().includes(f) || (n.children || []).some(matches);
  const node = (n, depth) => {
    if (!matches(n)) return "";
    const kids = (n.children || []).map((c) => node(c, depth + 1)).join("");
    return `<details ${f || depth < 1 ? "open" : ""} class="${kids ? "" : "leaf"}"><summary><span class="tt">${mark(n.title)}</span></summary>
      ${n.detail ? `<span class="td">${mark(n.detail)}</span>` : ""}
      ${(n.refs || []).length ? `<span class="tr">${n.refs.map((r) => `<code>${mark(r)}</code>`).join("")}</span>` : ""}${kids}</details>`;
  };
  const roots = (S.tree && S.tree.tree) || [];
  $("#tree").innerHTML = roots.length ? roots.map((n) => node(n, 0)).join("") : '<p class="muted">Empty.</p>';
}

/* ---------------------------------------------------------------- map */

async function loadMap() {
  const km = await api(`/kmap?${gq()}`);
  $("#mapMeta").textContent = `${km.messages} messages · ${km.nodes} nodes · ${km.edges} links. Click a concept to see what it touches.`;
  $("#mapGods").innerHTML = km.gods.map((g) => `<button class="chip" data-n="${esc(g.label)}">${esc(g.label)} <span class="muted">${g.msgs}</span></button>`).join("") || '<p class="muted">Not enough history yet.</p>';
  $("#mapComms").innerHTML = km.communities.slice(0, 8).map((c) => `<div class="comm"><b>${c.members.slice(0, 5).map(esc).join(", ")}</b>${c.agents.length ? `<div class="muted small">mostly ${c.agents.map(esc).join(", ")}</div>` : ""}</div>`).join("");
  $$("#mapGods .chip").forEach((b) => { b.onclick = () => explain(b.dataset.n); });
}

function refsHTML(refs) {
  return refs.map((r) => `<div class="ref" data-line="${r.line}">L${r.line} ${esc(r.agent)}: ${esc(r.snippet)}</div>`).join("");
}
function bindRefs() { $$("#mapResult .ref").forEach((el) => { el.onclick = () => jumpToLine(+el.dataset.line); }); }

async function explain(name) {
  try {
    const r = await api(`/kmap/explain?${gq()}&node=${encodeURIComponent(name)}`);
    $("#mapResult").innerHTML = `<div class="hit"><b>${esc(r.label)}</b> <span class="muted">${esc(r.kind)} · ${r.degree} links</span>
      <div class="chips" style="margin:6px 0">${r.connections.slice(0, 14).map((c) => `<button class="chip" data-n="${esc(c.label)}">${esc(c.label)}</button>`).join("")}</div>${refsHTML(r.refs)}</div>`;
    $$("#mapResult .chip").forEach((b) => { b.onclick = () => explain(b.dataset.n); });
    bindRefs();
  } catch (e) { $("#mapResult").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}

/* ---------------------------------------------------------------- connect */

async function loadConnect() {
  const sel = $("#connectAgent");
  const cur = sel.value || local.get("connectAgent", "");
  const external = activeAgents().filter((a) => !(a.provider && a.model));
  sel.innerHTML = external.map((a) => `<option ${a.name === cur ? "selected" : ""}>${esc(a.name)}</option>`).join("");
  const help = {
    rules: "Best for any agent that reads a rules file. It tells the agent to sync, claim, post change notices and release on its own.",
    mcp: "Best for Claude Code, Codex, Cursor, Cline and other MCP clients: the hub becomes native tools (aic_sync, aic_claim...).",
    cli: "For terminal agents or scripts. Same actions, no JSON quoting.",
  };
  $("#connectHelp").textContent = help[S.connectFmt];
  $$("#connectFmt button").forEach((b) => b.classList.toggle("active", b.dataset.fmt === S.connectFmt));
  if (!sel.value) { $("#connectText").textContent = "Add an agent first."; return; }
  local.set("connectAgent", sel.value);
  $("#connectText").textContent = await api(`/onboard?${gq()}&agent=${encodeURIComponent(sel.value)}&format=${S.connectFmt}`);
}

/* ---------------------------------------------------------------- project */

async function loadProject() {
  S.project = await api(`/project?${gq()}`);
  $("#projectMeta").textContent = S.project.rules ? "project rules set" : "";
}

/* ---------------------------------------------------------------- views */

function showView(v) {
  S.view = v;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === v));
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === "view-" + v));
  const run = { tasks: () => renderBoard(), tree: loadTree, map: loadMap, connect: loadConnect }[v];
  if (run) Promise.resolve(run()).catch((e) => toast(e.message));
  if (v === "chat") scrollBottom();
}

/* ---------------------------------------------------------------- live updates */

function setLive(mode) {
  $("#liveDot").className = "dot " + (mode === "live" ? "live" : mode === "poll" ? "poll" : "");
  $("#liveText").textContent = mode === "live" ? "live" : mode === "poll" ? "polling" : "reconnecting";
}

function connectEvents() {
  if (S.es) S.es.close();
  clearInterval(S.pollTimer);
  const url = `/events?${gq()}` + (S.key ? "&key=" + encodeURIComponent(S.key) : "");
  const es = new EventSource(url);
  S.es = es;
  let failures = 0;
  es.addEventListener("hello", () => { failures = 0; setLive("live"); });
  es.addEventListener("change", (ev) => onChange(JSON.parse(ev.data)));
  es.onerror = () => {
    setLive("off");
    if (++failures >= 3 || es.readyState === 2) {
      es.close();
      setLive("poll");
      S.pollTimer = setInterval(() => onChange(["messages", "team", "tasks", "claims"]), 4000);
    }
  };
}

async function onChange(what) {
  const jobs = [];
  if (what.includes("messages")) jobs.push(fetchNew());
  if (what.includes("team")) loadTeamSoon();
  if (what.includes("tasks")) jobs.push(loadTasks());
  if (what.includes("claims")) jobs.push(loadClaims());
  if (what.includes("tree") && S.view === "tree") jobs.push(loadTree());
  if (what.includes("project")) jobs.push(loadProject());
  if (what.includes("config")) jobs.push(loadConfig().then(() => { renderTeam(); if (S.view === "connect") loadConnect(); }));
  if (what.includes("games")) jobs.push(loadGames());
  if (what.includes("messages")) loadTeamSoon();
  await Promise.allSettled(jobs);
}

/* ---------------------------------------------------------------- settings */

function openSettings(tab = "agents") {
  renderAgentsPane();
  renderProviders();
  renderProjectPane();
  renderSecurity();
  settingsTab(tab);
  $("#settingsDialog").showModal();
}

function settingsTab(tab) {
  $$(".settings-nav [data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  $$(".pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === tab));
}

function providerOptions(selected) {
  const configured = S.cfg.presets.filter((p) => p.key_source || S.cfg.providers[p.id]);
  const opts = ['<option value="">External (connects itself)</option>'];
  for (const p of S.cfg.presets) {
    const ready = configured.includes(p);
    opts.push(`<option value="${p.id}" ${p.id === selected ? "selected" : ""}>${ready ? "" : "(no key) "}Hosted · ${esc(p.label)}</option>`);
  }
  return opts.join("");
}

function agentRow(a, isNew) {
  const preset = S.cfg.presets.find((p) => p.id === a.provider);
  const models = (preset && preset.models) || [];
  return `<div class="agent-row ${a.archived ? "archived" : ""}" data-name="${esc(a.name)}" data-new="${isNew ? 1 : ""}">
    <input type="color" value="${esc(a.color || "#8a94a6")}" data-k="color">
    ${isNew ? `<input data-k="name" placeholder="Name" value="${esc(a.name)}" maxlength="24">` : `<span class="nm">${esc(a.name)}</span>`}
    <input data-k="role" placeholder="Role, e.g. Build & QA" value="${esc(a.role || "")}" maxlength="120">
    <select data-k="provider">${providerOptions(a.provider)}</select>
    <input data-k="model" placeholder="${a.provider ? "model id" : "-"}" value="${esc(a.model || "")}" list="models-${esc(a.name)}" ${a.provider ? "" : "disabled"}>
    <datalist id="models-${esc(a.name)}">${models.map((m) => `<option value="${esc(m)}">`).join("")}</datalist>
    <button class="icon-btn" data-act="${a.archived ? "restore" : "archive"}" title="${a.archived ? "Restore" : "Archive (history is kept)"}">${a.archived ? "&#8634;" : "&#10005;"}</button>
    <div class="extra">
      ${a.provider ? `<textarea data-k="system" rows="2" placeholder="Extra instructions for this hosted agent (optional)">${esc(a.system || "")}</textarea>` : ""}
      ${S.cfg.auth.enabled && !isNew ? `<button class="btn sm" data-act="key">${a.has_key ? "New access key" : "Create access key"}</button>${a.has_key ? `<span class="muted small">key ${esc(a.key_hint)}</span>` : ""}` : ""}
    </div>
  </div>`;
}

function renderAgentsPane() {
  const list = S.cfg.agents.slice().sort((a, b) => a.archived - b.archived);
  $("#agentList").innerHTML = `<div class="agent-head"><span></span><span>Name</span><span>Role</span><span>Runs on</span><span>Model</span><span></span></div>` +
    list.map((a) => agentRow(a, false)).join("");
  bindAgentRows();
}

function bindAgentRows() {
  $$("#agentList .agent-row").forEach((row) => {
    const prov = $('[data-k="provider"]', row);
    prov.onchange = () => {
      const a = collectAgent(row);
      a.has_key = false;
      const fresh = document.createElement("div");
      fresh.innerHTML = agentRow(a, !!row.dataset.new);
      row.replaceWith(fresh.firstElementChild);
      bindAgentRows();
    };
    $$("[data-act]", row).forEach((b) => {
      b.onclick = async () => {
        if (b.dataset.act === "archive" || b.dataset.act === "restore") {
          if (row.dataset.new) { row.remove(); return; }
          row.classList.toggle("archived");
          b.dataset.act = row.classList.contains("archived") ? "restore" : "archive";
          b.innerHTML = row.classList.contains("archived") ? "&#8634;" : "&#10005;";
        } else if (b.dataset.act === "key") {
          try {
            const r = await api("/api/keys", { target: row.dataset.name });
            showIssued(`Access key for <b>${esc(row.dataset.name)}</b> (shown once - put it in that agent's setup as X-AIC-Key or AIC_KEY):`, r.key, "agents");
            await loadConfig();
          } catch (e) { $("#agentsErr").textContent = e.message; }
        }
      };
    });
  });
}

function collectAgent(row) {
  const get = (k) => { const el = $(`[data-k="${k}"]`, row); return el ? el.value.trim() : ""; };
  const existing = S.cfg.agents.find((a) => a.name === row.dataset.name) || {};
  return {
    name: row.dataset.new ? get("name") : row.dataset.name,
    color: get("color"), role: get("role"), provider: get("provider"),
    model: get("provider") ? get("model") : "", system: $('[data-k="system"]', row) ? get("system") : (existing.system || ""),
    archived: row.classList.contains("archived"), has_key: existing.has_key, key_hint: existing.key_hint,
  };
}

async function saveAgents() {
  $("#agentsErr").textContent = "";
  const agents = $$("#agentList .agent-row").map(collectAgent).filter((a) => a.name);
  for (const a of agents) if (a.provider && !a.model) { $("#agentsErr").textContent = `${a.name}: choose a model for the hosted provider.`; return; }
  try {
    await api("/api/config", { agents: agents.map(({ has_key, key_hint, ...a }) => a) });
    await loadConfig();
    renderAgentsPane();
    renderTeam();
    toast("Agents saved");
  } catch (e) { $("#agentsErr").textContent = e.message; }
}

function renderProviders() {
  $("#providerList").innerHTML = S.cfg.presets.map((p) => {
    const st = S.cfg.providers[p.id] || {};
    const src = p.key_source;
    const status = src === "saved" ? `<span class="status-ok">Key saved ${esc(st.key_hint || "")}</span>`
      : src && src.startsWith("env:") ? `<span class="status-ok">Using $${esc(src.slice(4))}</span>`
      : src === "keyless" ? '<span class="status-ok">No key needed</span>' : '<span class="status-off">Not set up</span>';
    const user = S.cfg.agents.find((a) => a.provider === p.id && a.model);
    const model = (user && user.model) || p.models[0] || "";
    const needsUrl = p.id === "custom" || p.id === "ollama";
    return `<div class="prov" data-id="${p.id}">
      <div class="prov-head"><b>${esc(p.label)}</b>${status}</div>
      <div class="row">
        ${p.keyless ? "" : `<input type="password" data-k="key" placeholder="${src === "saved" ? "Replace key..." : "Paste API key"}" autocomplete="off">`}
        <input data-k="url" placeholder="${esc(p.base_url || "https://.../v1")}" value="${esc(st.base_url || "")}" class="${needsUrl ? "" : "hidden"}" title="Base URL">
        <button class="btn sm primary" data-act="save">Save</button>
        ${src === "saved" ? '<button class="btn sm ghost" data-act="clear">Remove key</button>' : ""}
        ${needsUrl ? "" : '<button class="btn sm ghost" data-act="url">Base URL</button>'}
      </div>
      <div class="row"><input data-k="model" placeholder="model to test, e.g. ${esc(p.models[0] || "model-id")}" value="${esc(model)}"><button class="btn sm" data-act="test">Test connection</button></div>
      <div class="test-out muted"></div>
    </div>`;
  }).join("");
  $$("#providerList .prov").forEach((card) => {
    const id = card.dataset.id;
    const out = $(".test-out", card);
    $$("[data-act]", card).forEach((b) => {
      b.onclick = async () => {
        out.className = "test-out muted";
        try {
          if (b.dataset.act === "url") { $('[data-k="url"]', card).classList.toggle("hidden"); return; }
          if (b.dataset.act === "test") {
            out.textContent = "Calling the provider...";
            const r = await api("/api/providers/test", { provider: id, model: $('[data-k="model"]', card).value.trim() });
            out.className = "test-out status-ok"; out.textContent = `Works: "${r.reply}" in ${r.ms} ms`;
            return;
          }
          const patch = {};
          if (b.dataset.act === "clear") patch.api_key = "__clear__";
          else {
            const k = $('[data-k="key"]', card);
            if (k && k.value.trim()) patch.api_key = k.value.trim();
            patch.base_url = $('[data-k="url"]', card).value.trim();
          }
          await api("/api/config", { providers: { [id]: patch } });
          await loadConfig();
          renderProviders();
          toast("Saved");
        } catch (e) { out.className = "test-out err"; out.textContent = e.message; }
      };
    });
  });
}

function renderProjectPane() {
  $("#projLabel").value = S.project.label;
  $("#projRules").value = S.project.rules;
  $("#projErr").textContent = "";
  $("#dangerZone").classList.toggle("hidden", S.project.default);
  $("#delName").textContent = S.game;
  $("#delConfirm").value = "";
  $("#delBtn").disabled = true;
  $("#delErr").textContent = "";
}

function renderSecurity() {
  $("#authToggle").checked = S.cfg.auth.enabled;
  $("#maxChars").value = S.cfg.settings.max_message_chars;
  $("#authInfo").textContent = S.cfg.auth.enabled
    ? "Keys are on. Create a key per agent under Agents. This browser uses the admin key."
    : "Keys are off.";
  $("#issuedKey").classList.add("hidden");
}

function showIssued(label, key, pane) {
  const box = document.createElement("div");
  box.className = "key-box";
  box.innerHTML = `${label}<code>${esc(key)}</code><button class="btn sm">Copy</button>`;
  box.querySelector("button").onclick = () => copy(key);
  const target = pane === "agents" ? $("#agentList") : $("#issuedKey");
  if (pane === "agents") target.prepend(box);
  else { target.innerHTML = ""; target.appendChild(box); target.classList.remove("hidden"); }
}

async function saveSecurity() {
  try {
    const enable = $("#authToggle").checked;
    const r = await api("/api/config", { auth: { enabled: enable }, settings: { max_message_chars: +$("#maxChars").value } });
    if (r.issued && r.issued.admin_key) {
      S.key = r.issued.admin_key; local.set("key", S.key);
      connectEvents();
    }
    await loadConfig();
    renderSecurity();
    if (r.issued && r.issued.admin_key) showIssued("Admin key - saved in this browser. Keep a copy; it is also in .aic/config.json:", r.issued.admin_key, "security");
    toast("Saved");
  } catch (e) { toast(e.message); }
}

function askKey() {
  const d = $("#keyDialog");
  if (!d.open) d.showModal();
}

/* ---------------------------------------------------------------- theme */

function applyTheme() {
  const t = local.get("theme", "auto");
  if (t === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = t;
  $("#themeLabel").textContent = "Theme: " + t;
}

/* ---------------------------------------------------------------- wiring */

function bind() {
  $("#projects").onclick = (e) => { const b = e.target.closest(".proj"); if (b) selectGame(b.dataset.id); };
  $("#newProjectBtn").onclick = () => { $("#newProjectForm").classList.toggle("hidden"); $("#newProjectTitle").focus(); };
  $("#newProjectCancel").onclick = () => $("#newProjectForm").classList.add("hidden");
  $("#newProjectForm").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/new_game", { title: $("#newProjectTitle").value, agent: HUMAN });
      $("#newProjectForm").classList.add("hidden"); $("#newProjectTitle").value = ""; $("#newProjectErr").textContent = "";
      await loadGames();
      await selectGame(r.id);
      if (r.created) { showView("connect"); toast("Project created - now connect your agents"); }
    } catch (err) { $("#newProjectErr").textContent = err.message; }
  };
  $$("#tabs button").forEach((b) => { b.onclick = () => showView(b.dataset.view); });
  $("#menuBtn").onclick = () => $("#app").classList.toggle("menu-on");
  $("#railBtn").onclick = () => {
    if (matchMedia("(max-width: 1180px)").matches) $("#app").classList.toggle("rail-on");
    else { $("#app").classList.toggle("rail-off"); local.set("railOff", $("#app").classList.contains("rail-off")); }
  };
  $("#themeBtn").onclick = () => {
    const order = ["auto", "light", "dark"];
    local.set("theme", order[(order.indexOf(local.get("theme", "auto")) + 1) % 3]);
    applyTheme();
  };

  // chat
  let searchTimer;
  $("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(() => runSearch().catch((e) => toast(e.message)), 250); };
  $("#agentFilter").onchange = $("#typeFilter").onchange = () => runSearch().catch((e) => toast(e.message));
  $("#olderBtn").onclick = () => loadOlder().catch((e) => toast(e.message));
  $("#jumpBtn").onclick = scrollBottom;
  $("#feed").onscroll = () => {
    const f = $("#feed");
    if (f.scrollHeight - f.scrollTop - f.clientHeight < 80) $("#jumpBtn").classList.add("hidden");
  };
  $("#briefBtn").onclick = async () => copy(await api(`/brief?${gq()}&format=text`), "AI brief copied - paste it into any AI");
  $("#composer").onsubmit = (e) => { e.preventDefault(); send(); };
  composer.oninput = () => { autosize(); updateMention(); };
  composer.onkeydown = (e) => {
    if (mention.open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        mention.sel = (mention.sel + (e.key === "ArrowDown" ? 1 : -1) + mention.items.length) % mention.items.length;
        updateMention(); return;
      }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pickMention(mention.sel); return; }
      if (e.key === "Escape") { closeMention(); return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  };
  composer.onblur = () => setTimeout(closeMention, 150);

  // tasks
  $("#newTaskBtn").onclick = () => openTask(null);
  $("#taskForm").onsubmit = saveTask;
  $("#taskDelete").onclick = async () => {
    if (!editingTask || !confirm(`Delete ${editingTask.id}?`)) return;
    try { await api("/tasks/update", { game: S.game, agent: HUMAN, id: editingTask.id, delete: true }); $("#taskDialog").close(); loadTasks(); }
    catch (e) { $("#taskErr").textContent = e.message; }
  };

  // tree
  $("#treeFilter").oninput = renderTree;
  $("#treeExpand").onclick = () => {
    const all = $$("#tree details");
    const open = all.some((d) => !d.open);
    all.forEach((d) => { d.open = open; });
    $("#treeExpand").textContent = open ? "Collapse all" : "Expand all";
  };
  $("#treeCopy").onclick = async () => copy(await api(`/tree?${gq()}&format=text`), "Tree copied");
  $("#treeEditBtn").onclick = () => {
    $("#treeJson").value = JSON.stringify((S.tree && S.tree.tree) || [{ title: "Systems", detail: "", children: [] }], null, 2);
    $("#treeErr").textContent = "";
    $("#treeEditor").classList.remove("hidden"); $("#tree").classList.add("hidden");
  };
  $("#treeCancel").onclick = () => { $("#treeEditor").classList.add("hidden"); $("#tree").classList.remove("hidden"); };
  $("#treeSave").onclick = async () => {
    let roots;
    try { roots = JSON.parse($("#treeJson").value); } catch (e) { $("#treeErr").textContent = "Not valid JSON: " + e.message; return; }
    try {
      await api("/tree", { game: S.game, agent: HUMAN, tree: roots, expected_version: S.tree ? S.tree.version : 0 });
      $("#treeCancel").onclick(); await loadTree(); toast("Tree saved");
    } catch (e) { $("#treeErr").textContent = e.message; }
  };

  // map
  $("#mapAsk").onsubmit = async (e) => {
    e.preventDefault();
    const r = await api(`/kmap/query?${gq()}&q=${encodeURIComponent($("#mapQ").value)}`);
    $("#mapResult").innerHTML = (r.hits.map((h) => `<div class="hit"><b>${esc(h.label)}</b> <span class="muted">${h.degree} links</span>${refsHTML(h.refs)}</div>`).join("") +
      (r.messages.length ? `<div class="hit"><b>Messages</b>${refsHTML(r.messages)}</div>` : "")) || '<p class="muted">Nothing found.</p>';
    bindRefs();
  };
  $("#mapPath").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api(`/kmap/path?${gq()}&a=${encodeURIComponent($("#mapA").value)}&b=${encodeURIComponent($("#mapB").value)}`);
      $("#mapResult").innerHTML = `<div class="hit">${r.path.map((h) => `${esc(h.from)} <span class="muted">&rarr; ${esc(h.rel)} &rarr;</span> ${esc(h.to)}`).join("<br>")}</div>`;
    } catch (err) { $("#mapResult").innerHTML = `<p class="err">${esc(err.message)}</p>`; }
  };

  // connect
  $("#connectAgent").onchange = () => loadConnect();
  $$("#connectFmt button").forEach((b) => { b.onclick = () => { S.connectFmt = b.dataset.fmt; loadConnect(); }; });
  $("#connectCopy").onclick = () => copy($("#connectText").textContent, "Setup copied - paste it into the agent");
  $("#connectAddAgent").onclick = () => { openSettings("agents"); $("#addAgentBtn").click(); };

  // settings
  $("#settingsBtn").onclick = () => openSettings();
  $("#settingsClose").onclick = () => $("#settingsDialog").close();
  $$(".settings-nav [data-tab]").forEach((b) => { b.onclick = () => settingsTab(b.dataset.tab); });
  $("#addAgentBtn").onclick = () => {
    const wrap = document.createElement("div");
    wrap.innerHTML = agentRow({ name: "", color: "#" + Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, "0"), provider: "", model: "" }, true);
    $("#agentList").appendChild(wrap.firstElementChild);
    bindAgentRows();
    $("#agentList .agent-row:last-child [data-k=name]").focus();
  };
  $("#saveAgents").onclick = saveAgents;
  $$("[data-tpl]").forEach((b) => {
    b.onclick = () => {
      const tpl = S.project.templates[b.dataset.tpl];
      const cur = $("#projRules").value.trim();
      $("#projRules").value = cur ? cur + "\n" + tpl : tpl;
    };
  });
  $("#saveProject").onclick = async () => {
    try {
      await api("/project", { game: S.game, agent: HUMAN, label: $("#projLabel").value, rules: $("#projRules").value });
      await Promise.all([loadProject(), loadGames()]);
      $("#projectTitle").textContent = S.project.label;
      toast("Project saved");
    } catch (e) { $("#projErr").textContent = e.message; }
  };
  $("#delConfirm").oninput = () => { $("#delBtn").disabled = $("#delConfirm").value !== S.game; };
  $("#delBtn").onclick = async () => {
    try {
      await api("/delete_game", { game: S.game, confirm_understand: true, confirm_name: $("#delConfirm").value, confirm_irreversible: "DELETE" });
      $("#settingsDialog").close();
      await loadGames();
      await selectGame(S.games[0] && S.games[0].id);
      toast("Project moved to .trash/");
    } catch (e) { $("#delErr").textContent = e.message; }
  };
  $("#saveSecurity").onclick = saveSecurity;
  $("#keyForm").onsubmit = () => { S.key = $("#keyInput").value.trim(); local.set("key", S.key); location.reload(); };

  // keyboard
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { e.preventDefault(); showView("chat"); $("#search").focus(); }
    if (e.key === "Escape") { $("#app").classList.remove("menu-on", "rail-on"); }
  });
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") markSeen(); });
  setInterval(() => { renderTeam(); renderClaims(); $$(".mtime[data-ts]").forEach((el) => { if (el.dataset.ts) el.textContent = relTime(el.dataset.ts); }); }, 30000);
  setInterval(() => loadGames().catch(() => {}), 30000);
}

async function init() {
  applyTheme();
  if (local.get("railOff", false)) $("#app").classList.add("rail-off");
  bind();
  try {
    await loadConfig();
    await loadGames();
    const want = new URLSearchParams(location.search).get("game") || local.get("game", "");
    const g = S.games.find((x) => x.id === want) || S.games[0];
    if (!g) { $("#projectTitle").textContent = "No projects yet - create one"; return; }
    await selectGame(g.id);
    composer.focus();
  } catch (e) {
    if (e.message !== "Access key required") $("#projectTitle").textContent = "Could not reach the hub: " + e.message;
  }
}

init();
