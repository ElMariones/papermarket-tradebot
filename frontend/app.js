// TradeBOT dashboard — vanilla JS, polls the stdlib API.
const $ = (id) => document.getElementById(id);
const api = async (path, method = "GET", body) => {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  return r.json();
};
const money = (n) => (n < 0 ? "-$" : "$") + Math.abs(n).toFixed(2);
const signed = (n) => (n >= 0 ? "+" : "-") + "$" + Math.abs(n).toFixed(2);
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "muted");
const esc = (s) => (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const ts = (s) => (s ? s.slice(0, 19).replace("T", " ") : "—");

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
  ["stop_loss_pct", "Stop loss", 0.05],
  ["min_trade_usd", "Min trade $", 1],
];
const CAP_MIN = { max_concurrent_positions: 1, markets_per_scan: 10 };

let equityData = [];
let SETTINGS = {};

async function refresh() {
  try {
    const s = await api("/api/summary");
    renderHeadline(s);
    renderAgent(s.agent);
    renderPositions(s.portfolio.positions);
    $("lastUpdate").textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (e) { /* transient */ }
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
  $("drawdown").textContent = `DD ${pf.drawdown_pct.toFixed(1)}% · start ${money(pf.starting_balance)}`;
  $("posCount").textContent = pf.num_open_positions + " open";
}
function setVal(id, n, signedFmt) {
  const el = $(id);
  el.textContent = signedFmt ? signed(n) : money(n);
  el.className = "card-value " + cls(n);
}

function renderAgent(a) {
  const pill = $("agentStatus");
  pill.textContent = "● " + a.status;
  pill.className = "status-pill status-" + a.status;
  $("agentMeta").textContent = a.cycles ? `${a.cycles} cycles · ${a.last_message || ""}` : "";
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
      <td>${ts(r.executed_at)}</td>
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
      <td>${ts(r.ts)}</td>
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
      <td>${ts(r.ts)}</td>
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
function drawEquity() {
  const cv = $("equityChart"); const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth, H = 220;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d"); ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const pad = { l: 56, r: 12, t: 12, b: 22 };
  const data = equityData;
  if (data.length < 2) {
    ctx.fillStyle = "#56657a"; ctx.font = "12px monospace";
    ctx.fillText("Collecting equity data… run a few agent cycles.", pad.l, H / 2);
    return;
  }
  const vals = data.map((d) => d.total_value);
  let min = Math.min(...vals), max = Math.max(...vals);
  const span = max - min || 1; min -= span * 0.1; max += span * 0.1;
  const x = (i) => pad.l + (i / (data.length - 1)) * (W - pad.l - pad.r);
  const y = (v) => pad.t + (1 - (v - min) / (max - min)) * (H - pad.t - pad.b);
  const start = data[0].total_value;

  // grid + y labels
  ctx.strokeStyle = "#1e2836"; ctx.fillStyle = "#56657a"; ctx.font = "10px monospace";
  ctx.textAlign = "right";
  for (let g = 0; g <= 4; g++) {
    const v = min + (g / 4) * (max - min); const yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
    ctx.fillText("$" + v.toFixed(0), pad.l - 6, yy + 3);
  }
  // baseline (starting value)
  if (start >= min && start <= max) {
    ctx.strokeStyle = "#3a4658"; ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(pad.l, y(start)); ctx.lineTo(W - pad.r, y(start)); ctx.stroke();
    ctx.setLineDash([]);
  }
  const last = vals[vals.length - 1];
  const up = last >= start;
  const color = up ? "#1ec27a" : "#ff5470";
  // area fill
  const grad = ctx.createLinearGradient(0, pad.t, 0, H - pad.b);
  grad.addColorStop(0, up ? "rgba(30,194,122,.25)" : "rgba(255,84,112,.25)");
  grad.addColorStop(1, "rgba(0,0,0,0)");
  ctx.beginPath(); ctx.moveTo(x(0), y(vals[0]));
  data.forEach((d, i) => ctx.lineTo(x(i), y(d.total_value)));
  ctx.lineTo(x(data.length - 1), H - pad.b); ctx.lineTo(x(0), H - pad.b); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill();
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
  $("btnCycle").textContent = "Cycle ▸";
  refresh(); loadTrades(); loadDecisions(); loadEquity();
};

// ---- FUNDS ----
$("btnFund").onclick = async () => {
  const amt = parseFloat($("fundAmount").value);
  if (amt > 0) { await api("/api/add-funds", "POST", { amount: amt }); refresh(); }
};
document.querySelectorAll(".chip[data-amt]").forEach((c) =>
  c.onclick = async () => { await api("/api/add-funds", "POST", { amount: +c.dataset.amt }); refresh(); });

// ---- TABS ----
const PANES = { trades: "tradesPane", decisions: "decisionsPane", reports: "reportsPane" };
document.querySelectorAll(".tab").forEach((t) =>
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    Object.entries(PANES).forEach(([k, id]) => $(id).classList.toggle("hidden", t.dataset.tab !== k));
    if (t.dataset.tab === "reports") loadReports();
  });

// ---- INIT + POLL ----
function tickSlow() { loadTrades(); loadDecisions(); loadEquity(); loadReports(); }
refresh(); loadSettings(); tickSlow();
setInterval(refresh, 4000);
setInterval(tickSlow, 8000);
window.addEventListener("resize", drawEquity);
