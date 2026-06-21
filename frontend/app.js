// TradeBOT dashboard — vanilla JS, polls the stdlib API.
const $ = (id) => document.getElementById(id);

// Which of the four independent bots the dashboard is currently driving. Every
// API call is scoped to this profile (GET via ?profile=, POST via body.profile)
// so each bot's money, strategy, trades and logs stay fully separate.
let CURRENT_PROFILE = localStorage.getItem("tradebot-profile") || "Kaladin";

const api = async (path, method = "GET", body) => {
  let url = path;
  if (method === "GET") {
    url += (path.includes("?") ? "&" : "?") + "profile=" + encodeURIComponent(CURRENT_PROFILE);
  } else {
    body = { ...(body || {}), profile: CURRENT_PROFILE };
  }
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (method !== "GET") opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  return r.json();
};
const money = (n) => (n < 0 ? "-$" : "$") + Math.abs(n).toFixed(2);
const signed = (n) => (n >= 0 ? "+" : "-") + "$" + Math.abs(n).toFixed(2);
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "muted");
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
// Timestamps from the API are UTC ISO strings; render them in the viewer's
// LOCAL timezone (e.g. Madrid shows 17:30, not the 15:30 UTC value).
const ts = (s) => {
  if (!s) return "—";
  const d = new Date(s);
  if (isNaN(d.getTime())) return s;
  return d.toLocaleString([], {
    month: "short", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
};
const clockFmt = (d) => d.toLocaleTimeString([], { hour12: false });

// ---- SETTINGS metadata (labels + step) ----
const SETTING_FIELDS = [
  ["scan_interval_sec", "Scan interval (s)", 1],
  ["markets_per_scan", "Markets / scan", 1],
  ["risk_per_trade_pct", "Risk per trade", 0.01],
  ["max_concurrent_positions", "Max positions", 1],
  ["confidence_threshold", "Min confidence", 0.05],
  ["fav_low", "Favorite low", 0.01],
  ["fav_high", "Favorite high", 0.01],
  ["longshot_thresh", "Longshot <", 0.01],
  ["max_entry_price", "Max entry price", 0.01],
  ["min_volume24h", "Min 24h volume", 100],
  ["min_book_usd", "Min book $", 10],
  ["max_spread", "Max spread", 0.01],
  ["take_profit_pct", "Take profit", 0.05],
  ["stop_loss_pct", "Stop loss %", 0.05],
  ["stop_loss_price", "Stop price floor", 0.05],
  ["reentry_cooldown_min", "Re-entry cooldown (m)", 15],
  ["min_trade_usd", "Min trade $", 1],
];
const CAP_MIN = { max_concurrent_positions: 1, markets_per_scan: 10 };

let equityData = [];
let SETTINGS = {};
let activeTab = "trades";

async function refresh() {
  try {
    const s = await api("/api/summary");
    renderHeadline(s);
    renderAgent(s.agent);
    renderPositions(s.portfolio.positions);
    $("lastUpdate").textContent = "Updated " + clockFmt(new Date()) +
      (s.agent.cycles ? " · " + s.agent.cycles + " cycles" : "");
  } catch (e) { /* transient */ }
}

// Live local-time clock in the top bar.
function tickClock() {
  $("localClock").textContent = clockFmt(new Date());
  try {
    const z = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
    $("clockZone").textContent = z.split("/").pop().replace(/_/g, " ");
  } catch (e) { $("clockZone").textContent = "local"; }
}

function renderHeadline(s) {
  const pf = s.portfolio;
  const totalPnl = s.realized_pnl + s.unrealized_pnl;
  $("totalValue").textContent = money(pf.total_value);
  const tp = $("totalPnl");
  tp.textContent = `${signed(totalPnl)}  (${(totalPnl / pf.starting_balance * 100).toFixed(2)}%)`;
  tp.className = "card-delta " + cls(totalPnl);
  setVal("realizedPnl", s.realized_pnl, true);
  setVal("unrealizedPnl", s.unrealized_pnl, true);
  $("cash").textContent = money(pf.cash_balance);
  $("winRate").textContent = s.win_rate.toFixed(1) + "%";
  $("tradeCount").textContent = `${s.closed_trades} closed / ${s.total_trades} trades`;
  const maxPos = SETTINGS.max_concurrent_positions || pf.num_open_positions;
  $("openPos").textContent = `${pf.num_open_positions} / ${maxPos}`;
  $("drawdown").textContent = `DD ${pf.drawdown_pct.toFixed(1)}% · base ${money(pf.starting_balance)}`;
  $("posCount").textContent = pf.num_open_positions + " open";
  // exposure: share of equity deployed into positions
  const tv = pf.total_value || 1;
  const pct = Math.max(0, Math.min(100, pf.positions_value / tv * 100));
  $("expBar").style.width = pct.toFixed(1) + "%";
  $("expLabel").textContent =
    `${pct.toFixed(0)}% deployed · ${money(pf.positions_value)} in ${pf.num_open_positions}`;
}
function setVal(id, n, signedFmt) {
  const el = $(id);
  el.textContent = signedFmt ? signed(n) : money(n);
  el.className = "card-value " + cls(n);
}

function renderAgent(a) {
  const pill = $("agentStatus");
  pill.textContent = a.status;
  pill.className = "status-pill status-" + a.status;
  pill.title = (a.cycles ? a.cycles + " cycles" : "idle") +
    (a.last_message ? " · " + a.last_message : "");
}

function renderPositions(positions) {
  const tb = $("posTable").querySelector("tbody");
  if (!positions.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty">No open positions.</td></tr>`;
    return;
  }
  tb.innerHTML = positions.map((p) => `
    <tr>
      <td class="mkt" title="${esc(p.market_question)}">${esc(p.market_question)}</td>
      <td><span class="badge ${p.side}">${p.side}</span></td>
      <td class="num">${p.shares.toFixed(1)}</td>
      <td class="num">${p.avg_entry.toFixed(3)}</td>
      <td class="num">${p.current_price.toFixed(3)}</td>
      <td class="num">${money(p.value)}</td>
      <td class="num ${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}</td>
      <td><button class="tiny-close" data-token="${p.token_id}" data-side="${p.side}">close</button></td>
    </tr>`).join("");
  tb.querySelectorAll(".tiny-close").forEach((b) =>
    b.onclick = async () => {
      b.textContent = "…";
      await api("/api/close", "POST", { token_id: b.dataset.token, side: b.dataset.side });
      refresh(); loadTrades();
    });
}

async function loadTrades() {
  const t = await api("/api/trades?limit=200");
  const tb = $("tradesTable").querySelector("tbody");
  if (!t.length) { tb.innerHTML = `<tr><td colspan="8" class="empty">No trades yet.</td></tr>`; return; }
  tb.innerHTML = t.map((r) => {
    // Color the proceeds of a SELL by realized P&L: green if we made money,
    // red if we lost. BUYs are neutral (cash out, no realized result yet).
    const isSell = r.action === "SELL" && r.entry_avg != null;
    const pnl = isSell ? (r.price - r.entry_avg) * r.shares : null;
    const procCell = isSell
      ? `<td class="num ${cls(pnl)}" title="Realized P&L ${signed(pnl)} (sold @ ${r.price.toFixed(3)} vs entry ${r.entry_avg.toFixed(3)})">${money(r.total_cost)} <span class="pnl-tag">${signed(pnl)}</span></td>`
      : `<td class="num">${money(r.total_cost)}</td>`;
    return `
    <tr>
      <td class="t-time">${ts(r.executed_at)}</td>
      <td><span class="badge ${r.action}">${r.action}</span></td>
      <td><span class="badge ${r.side}">${r.side}</span></td>
      <td class="mkt" title="${esc(r.market_question)}">${esc(r.market_question)}</td>
      <td class="num">${r.shares.toFixed(1)}</td>
      <td class="num">${r.price.toFixed(3)}</td>
      ${procCell}
      <td class="reason-cell" title="${esc(r.reasoning)}">${esc(r.reasoning)}</td>
    </tr>`;
  }).join("");
}

async function loadDecisions() {
  const d = await api("/api/decisions?limit=120");
  const tb = $("decisionsTable").querySelector("tbody");
  if (!d.length) { tb.innerHTML = `<tr><td colspan="6" class="empty">No decisions logged.</td></tr>`; return; }
  tb.innerHTML = d.map((r) => `
    <tr>
      <td class="t-time">${ts(r.ts)}</td>
      <td><span class="badge ${r.signal}">${r.signal}</span></td>
      <td>${r.acted ? "✓" : "—"}</td>
      <td class="num">${r.confidence != null ? r.confidence.toFixed(2) : "—"}</td>
      <td class="mkt" title="${esc(r.market_question)}">${esc(r.market_question)}</td>
      <td class="reason-cell" title="${esc(r.reasoning)}">${esc(r.reasoning)}</td>
    </tr>`).join("");
}

async function loadReports() {
  const d = await api("/api/reports?limit=168");
  const tb = $("reportsTable").querySelector("tbody");
  if (!d.length) {
    tb.innerHTML = `<tr><td colspan="8" class="empty">No hourly reports yet — one is saved automatically every hour (and to the server logs).</td></tr>`;
    return;
  }
  tb.innerHTML = d.map((r) => `
    <tr>
      <td class="t-time">${ts(r.ts)}</td>
      <td class="num">${money(r.total_value)}</td>
      <td class="num ${cls(r.pnl_pct)}">${r.pnl_pct.toFixed(2)}%</td>
      <td class="num ${cls(r.realized_pnl)}">${signed(r.realized_pnl)}</td>
      <td class="num ${cls(r.unrealized_pnl)}">${signed(r.unrealized_pnl)}</td>
      <td class="num">${r.num_positions}</td>
      <td class="num">${r.win_rate.toFixed(0)}%</td>
      <td class="num">${r.trades_last_hour}</td>
    </tr>`).join("");
}

// ---- EQUITY CHART (hand-rolled canvas) ----
async function loadEquity() {
  equityData = await api("/api/equity");
  drawEquity();
}
function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function drawEquity() {
  const cv = $("equityChart"); const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 232;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  // theme-aware palette (follows the active light/dark variables)
  const C = {
    grid: cssVar("--grid", "#1a212a"),
    faint: cssVar("--text-faint", "#5a6776"),
    accent: cssVar("--accent", "#e3a838"),
    up: cssVar("--up", "#27c08a"),
    down: cssVar("--down", "#f0506a"),
  };
  const pad = { l: 56, r: 12, t: 12, b: 22 };
  const data = equityData;
  if (data.length < 2) {
    ctx.fillStyle = C.faint; ctx.font = "12px monospace";
    ctx.fillText("Collecting equity data — run a few agent cycles.", pad.l, H / 2);
    return;
  }
  const vals = data.map((d) => d.total_value);
  let min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1; min -= span * 0.1; max += span * 0.1;
  const x = (i) => pad.l + (i / (data.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / (max - min)) * (H - pad.t - pad.b);
  const start = data[0].total_value;

  // grid + y labels
  ctx.strokeStyle = C.grid; ctx.fillStyle = C.faint; ctx.font = "10px monospace";
  ctx.textAlign = "right";
  for (let g = 0; g <= 4; g++) {
    const v = min + (g / 4) * (max - min); const yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
    ctx.fillText("$" + v.toFixed(0), pad.l - 6, yy + 3);
  }
  // baseline (starting / deposited basis) in amber
  if (start >= min && start <= max) {
    ctx.strokeStyle = C.accent; ctx.globalAlpha = .55; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, y(start)); ctx.lineTo(W - pad.r, y(start)); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
  }
  const last = vals[vals.length - 1];
  const up = last >= start;
  const color = up ? C.up : C.down;
  // area fill (fade the P&L color via alpha so it follows the theme)
  ctx.save();
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  data.forEach((d, i) => ctx.lineTo(x(i), y(d.total_value)));
  ctx.lineTo(x(data.length - 1), H - pad.b); ctx.lineTo(x(0), H - pad.b); ctx.closePath();
  ctx.globalAlpha = .20; ctx.fillStyle = color; ctx.fill();
  ctx.restore();
  // line
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  data.forEach((d, i) => ctx.lineTo(x(i), y(d.total_value)));
  ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
  // last point
  ctx.fillStyle = color; ctx.beginPath();
  ctx.arc(x(data.length - 1), y(last), 3.5, 0, 7); ctx.fill();
  $("equityRange").textContent =
    `${data.length} pts · ${ts(data[0].ts)} → now`;
}

// ---- SETTINGS ----
function renderCapacity() {
  if ($("capMaxPos")) $("capMaxPos").textContent = SETTINGS.max_concurrent_positions ?? "—";
  if ($("capScan")) $("capScan").textContent = SETTINGS.markets_per_scan ?? "—";
}
async function loadSettings() {
  const s = await api("/api/settings");
  SETTINGS = s;
  const f = $("settingsForm");
  f.innerHTML = SETTING_FIELDS.map(([k, label, step]) => `
    <div class="setting">
      <label for="set_${k}">${label}</label>
      <input id="set_${k}" type="number" step="${step}" value="${s[k]}" />
    </div>`).join("");
  renderCapacity();
}
$("btnSaveSettings").onclick = async () => {
  const body = {};
  SETTING_FIELDS.forEach(([k]) => { body[k] = parseFloat($("set_" + k).value); });
  const s = await api("/api/settings", "POST", body);
  SETTINGS = s; renderCapacity();
  const m = $("settingsMsg"); m.textContent = "✓ Parameters saved.";
  setTimeout(() => (m.textContent = ""), 2500);
};

// ---- POSITION CAPACITY STEPPERS ----
async function applyCap(key, value) {
  const min = CAP_MIN[key] || 1;
  value = Math.max(min, Math.round(value));
  const s = await api("/api/settings", "POST", { [key]: value });
  SETTINGS = s; renderCapacity();
  if ($("set_" + key)) $("set_" + key).value = SETTINGS[key]; // keep form in sync
  const m = $("capMsg");
  m.textContent = `✓ ${key === "markets_per_scan" ? "Markets/scan" : "Max positions"} set to ${SETTINGS[key]}.`;
  setTimeout(() => (m.textContent = ""), 2500);
  refresh();
}
document.querySelectorAll(".step").forEach((b) =>
  b.onclick = () => applyCap(b.dataset.key, (SETTINGS[b.dataset.key] || 0) + (+b.dataset.delta)));
document.querySelectorAll(".set-cap").forEach((c) =>
  c.onclick = () => applyCap(c.dataset.key, +c.dataset.val));

// ---- AGENT CONTROLS ----
const ctl = (path) => async () => { await api(path, "POST"); refresh(); };
$("btnStart").onclick = ctl("/api/agent/start");
$("btnPause").onclick = ctl("/api/agent/pause");
$("btnStop").onclick = ctl("/api/agent/stop");
$("btnCycle").onclick = async () => {
  $("btnCycle").textContent = "running…";
  await api("/api/agent/cycle", "POST");
  $("btnCycle").textContent = "Cycle";
  refresh(); loadTrades(); loadDecisions(); loadEquity();
};

// ---- FUNDS (deposit / withdraw) ----
function fundMsg(text, ok) {
  const m = $("fundMsg");
  if (!m) return;
  m.textContent = text;
  m.style.color = ok ? "var(--up)" : "var(--down)";
  setTimeout(() => (m.textContent = ""), 3500);
}
async function moveFunds(endpoint, amount, verb) {
  if (!(amount > 0)) return;
  const r = await api(endpoint, "POST", { amount });
  if (r && r.error) fundMsg("✗ " + r.error, false);
  else fundMsg(`✓ ${verb} $${amount.toFixed(2)}.`, true);
  refresh(); loadProfiles();
}
$("btnFund").onclick = () => moveFunds("/api/add-funds", parseFloat($("fundAmount").value), "Deposited");
$("btnWithdraw").onclick = () => moveFunds("/api/withdraw-funds", parseFloat($("fundAmount").value), "Withdrew");
document.querySelectorAll(".chip[data-amt]").forEach((c) =>
  c.onclick = () => moveFunds("/api/add-funds", +c.dataset.amt, "Deposited"));

// ---- RESET (with confirmation) ----
$("btnReset").onclick = async () => {
  const bal = parseFloat($("resetBalance").value) || 200;
  const ok = confirm(
    `Reset ${CURRENT_PROFILE}?\n\nThis permanently wipes ${CURRENT_PROFILE}'s positions, ` +
    `trades, equity history, decisions and hourly reports, and restarts its balance at ` +
    `$${bal.toFixed(0)}.\n\nOnly ${CURRENT_PROFILE} is affected — the other bots are ` +
    `untouched. Its strategy parameters are kept. This cannot be undone.`);
  if (!ok) return;
  const b = $("btnReset"); b.textContent = "Resetting…"; b.disabled = true;
  try {
    const r = await api("/api/reset", "POST", { confirm: true, balance: bal });
    const m = $("resetMsg");
    if (r.error) { m.textContent = "✗ " + r.error; m.style.color = "var(--down)"; }
    else { m.textContent = `✓ Reset to $${bal.toFixed(0)}.`; m.style.color = "var(--up)"; }
    setTimeout(() => (m.textContent = ""), 4000);
  } finally {
    b.textContent = "Reset all"; b.disabled = false;
    refresh(); loadSettings(); loadTrades(); loadDecisions(); loadEquity(); loadReports();
  }
};

// ---- TABS ----
const PANES = { trades: "tradesPane", decisions: "decisionsPane", reports: "reportsPane" };
document.querySelectorAll(".tab").forEach((t) =>
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    activeTab = t.dataset.tab;
    Object.entries(PANES).forEach(([k, id]) => $(id).classList.toggle("hidden", t.dataset.tab !== k));
    if (t.dataset.tab === "reports") loadReports();
  });

// ---- EXPORT ----
// Full snapshot: navigating to the endpoint triggers a file download (the
// server sets Content-Disposition). The browser reuses the session's auth.
$("btnExport").onclick = () => {
  window.location.href = "/api/export?profile=" + encodeURIComponent(CURRENT_PROFILE);
};

// CSV of whatever tab is showing — fetched fresh, built client-side.
function toCsv(rows, cols) {
  const esc = (v) => {
    const s = v == null ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const head = cols.map((c) => c[0]).join(",");
  const body = rows.map((r) => cols.map((c) => esc(c[1](r))).join(",")).join("\n");
  return head + "\n" + body;
}
function download(name, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const CSV_SPECS = {
  trades: ["trades?limit=100000", [
    ["time_utc", (r) => r.executed_at], ["action", (r) => r.action], ["side", (r) => r.side],
    ["market", (r) => r.market_question], ["shares", (r) => r.shares], ["price", (r) => r.price],
    ["cost_or_proceeds", (r) => r.total_cost], ["entry_avg", (r) => r.entry_avg],
    ["realized_pnl", (r) => (r.action === "SELL" && r.entry_avg != null ? ((r.price - r.entry_avg) * r.shares).toFixed(4) : "")],
    ["rationale", (r) => r.reasoning]]],
  decisions: ["decisions?limit=100000", [
    ["time_utc", (r) => r.ts], ["signal", (r) => r.signal], ["acted", (r) => r.acted],
    ["confidence", (r) => r.confidence], ["market", (r) => r.market_question], ["reasoning", (r) => r.reasoning]]],
  reports: ["reports?limit=100000", [
    ["time_utc", (r) => r.ts], ["total_value", (r) => r.total_value], ["pnl_pct", (r) => r.pnl_pct],
    ["realized_pnl", (r) => r.realized_pnl], ["unrealized_pnl", (r) => r.unrealized_pnl],
    ["positions", (r) => r.num_positions], ["win_rate", (r) => r.win_rate],
    ["trades_last_hour", (r) => r.trades_last_hour]]],
};
$("btnExportCsv").onclick = async () => {
  const spec = CSV_SPECS[activeTab]; if (!spec) return;
  const rows = await api("/api/" + spec[0]);
  const stamp = new Date().toISOString().slice(0, 19).replace(/[:T-]/g, "");
  download(`tradebot-${activeTab}-${stamp}.csv`, toCsv(rows, spec[1]), "text/csv");
};

// ---- THEME TOGGLE ----
function syncThemeButton() {
  const light = document.documentElement.getAttribute("data-theme") === "light";
  // show the icon for the mode you'd switch TO: sun while dark, moon while light
  if ($("btnTheme")) $("btnTheme").textContent = light ? "☾" : "☀";
}
syncThemeButton();
$("btnTheme").onclick = () => {
  const light = document.documentElement.getAttribute("data-theme") === "light";
  if (light) document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", "light");
  try { localStorage.setItem("tradebot-theme", light ? "dark" : "light"); } catch (e) {}
  syncThemeButton();
  drawEquity();  // repaint the canvas with the new palette
};

// ---- PROFILE SWITCH (4 independent bots) ----
function renderProfileSwitch(profiles) {
  const names = profiles.map((p) => p.name);
  if (!names.includes(CURRENT_PROFILE)) CURRENT_PROFILE = names[0] || "Kaladin";
  const nav = $("profileSwitch");
  nav.innerHTML = profiles.map((p) => {
    const active = p.name === CURRENT_PROFILE ? " active" : "";
    const pnl = (p.pnl_pct == null) ? "—" : `${p.pnl_pct >= 0 ? "+" : ""}${p.pnl_pct.toFixed(2)}%`;
    const pnlCls = p.pnl_pct == null ? "" : (p.pnl_pct > 0 ? " pos" : p.pnl_pct < 0 ? " neg" : "");
    return `<button class="profile-btn${active}" data-profile="${p.name}" title="${esc(p.blurb)}">
      <span class="pf-name"><i class="pf-dot ${p.status}"></i>${p.name}</span>
      <span class="pf-pnl${pnlCls}">${pnl}</span>
    </button>`;
  }).join("");
  nav.querySelectorAll(".profile-btn").forEach((b) =>
    b.onclick = () => switchProfile(b.dataset.profile));
}
async function loadProfiles() {
  try { renderProfileSwitch(await api("/api/profiles")); } catch (e) { /* transient */ }
}
function switchProfile(name) {
  if (name === CURRENT_PROFILE) return;
  CURRENT_PROFILE = name;
  try { localStorage.setItem("tradebot-profile", name); } catch (e) {}
  // reload every panel for the newly selected bot
  loadProfiles(); refresh(); loadSettings();
  loadTrades(); loadDecisions(); loadEquity(); loadReports();
}

// ---- INIT + POLL ----
function tickSlow() { loadTrades(); loadDecisions(); loadEquity(); loadReports(); }
tickClock(); loadProfiles(); refresh(); loadSettings(); tickSlow();
setInterval(tickClock, 1000);
setInterval(() => { refresh(); loadProfiles(); }, 4000);
setInterval(tickSlow, 8000);
window.addEventListener("resize", drawEquity);
