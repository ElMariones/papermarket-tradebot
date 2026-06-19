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

let equityData = [];

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
  $("openPos").textContent = `${pf.num_open_positions} / ${pf.starting_balance ? 10 : 10}`;
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
  tb.innerHTML = t.map((r) => `
    <tr>
      <td>${ts(r.executed_at)}</td>
      <td><span class="badge ${r.action}">${r.action}</span></td>
      <td><span class="badge ${r.side}">${r.side}</span></td>
      <td class="mkt" title="${esc(r.market_question)}">${esc(r.market_question)}</td>
      <td class="num">${r.shares.toFixed(1)}</td>
      <td class="num">${r.price.toFixed(3)}</td>
      <td class="num">${money(r.total_cost)}</td>
      <td class="reason-cell" title="${esc(r.reasoning)}">${esc(r.reasoning)}</td>
    </tr>`).join("");
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
async function loadSettings() {
  const s = await api("/api/settings");
  const f = $("settingsForm");
  f.innerHTML = SETTING_FIELDS.map(([k, label, step]) => `
    <div class="setting">
      <label for="set_${k}">${label}</label>
      <input id="set_${k}" type="number" step="${step}" value="${s[k]}" />
    </div>`).join("");
}
$("btnSaveSettings").onclick = async () => {
  const body = {};
  SETTING_FIELDS.forEach(([k]) => { body[k] = parseFloat($("set_" + k).value); });
  await api("/api/settings", "POST", body);
  const m = $("settingsMsg"); m.textContent = "✓ Parameters saved.";
  setTimeout(() => (m.textContent = ""), 2500);
};

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
document.querySelectorAll(".chip").forEach((c) =>
  c.onclick = async () => { await api("/api/add-funds", "POST", { amount: +c.dataset.amt }); refresh(); });

// ---- TABS ----
document.querySelectorAll(".tab").forEach((t) =>
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    const isTrades = t.dataset.tab === "trades";
    $("tradesPane").classList.toggle("hidden", !isTrades);
    $("decisionsPane").classList.toggle("hidden", isTrades);
  });

// ---- INIT + POLL ----
function tickSlow() { loadTrades(); loadDecisions(); loadEquity(); }
refresh(); loadSettings(); tickSlow();
setInterval(refresh, 4000);
setInterval(tickSlow, 8000);
window.addEventListener("resize", drawEquity);
