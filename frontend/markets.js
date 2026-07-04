// markets.js — the Markets page: browse the live scan, trade by hand.
//
// Spectators see everything (list, prices, gates, book detail) but every
// Buy/Sell is locked -> spectator modal. A signed-in user trades into their
// OWN portfolio; an admin can pick any user portfolio from the selector.
// Buys and sells go through POST /api/trade, which routes into the exact
// same order-book fill simulation the bots use.

const $ = (id) => document.getElementById(id);
const money = (n) => (n < 0 ? "-$" : "$") + Math.abs(n).toFixed(2);
const signed = (n) => (n >= 0 ? "+" : "-") + "$" + Math.abs(n).toFixed(2);
const cls = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "muted");
const esc = (s) => (s || "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let MARKETS = [];
let PROFILES = [];
let TARGET = null;          // portfolio name manual trades go into

function canTrade() {
  if (!AUTH.user || !TARGET) return false;
  if (AUTH.user.role === "admin") return true;
  const p = PROFILES.find((x) => x.name === TARGET);
  return !!p && p.kind === "user" && p.owner_user_id === AUTH.user.id;
}
const guard = (fn) => (...a) => (canTrade() ? fn(...a) : showSpectatorModal());

// ---- target portfolio ----
async function loadProfilesAndTarget() {
  PROFILES = await (await fetch("/api/profiles")).json();
  const userPfs = PROFILES.filter((p) => p.kind === "user");
  if (AUTH.user) {
    const mine = userPfs.find((p) => p.owner_user_id === AUTH.user.id);
    TARGET = mine ? mine.name : (AUTH.user.role === "admin" && userPfs[0]
      ? userPfs[0].name : null);
    if (AUTH.user.role === "admin" && userPfs.length) {
      // admins may act on any user's portfolio — expose the choice
      $("targetRow").classList.remove("hidden");
      $("targetSelect").innerHTML = userPfs.map((p) =>
        `<option value="${esc(p.name)}"${p.name === TARGET ? " selected" : ""}>` +
        `${esc(p.name)} (${esc(p.owner || "user")})</option>`).join("");
      $("targetSelect").onchange = () => {
        TARGET = $("targetSelect").value;
        loadMine(); renderMarkets();
      };
    }
  }
  $("mineTitle").textContent = TARGET ? `Portfolio — ${TARGET}` : "Your portfolio";
}

async function loadMine() {
  const tb = $("minePosTable").querySelector("tbody");
  if (!TARGET) {
    $("mineSummary").textContent = "";
    tb.innerHTML = `<tr><td colspan="7" class="empty">${AUTH.user
      ? "No personal portfolio on this account."
      : "Spectating — sign in to trade into your own portfolio."}</td></tr>`;
    return;
  }
  try {
    const pf = await (await fetch(
      "/api/portfolio?profile=" + encodeURIComponent(TARGET))).json();
    $("mineSummary").textContent =
      `${money(pf.total_value)} total · ${money(pf.cash_balance)} cash · ` +
      `${pf.num_open_positions} open`;
    if (!pf.positions.length) {
      tb.innerHTML = `<tr><td colspan="7" class="empty">No open positions — buy something below.</td></tr>`;
      return;
    }
    tb.innerHTML = pf.positions.map((p) => `
      <tr>
        <td class="mkt" title="${esc(p.market_question)}">${esc(p.market_question)}</td>
        <td><span class="badge ${p.side}">${p.side}</span></td>
        <td class="num">${p.shares.toFixed(1)}</td>
        <td class="num">${p.avg_entry.toFixed(3)}</td>
        <td class="num">${p.current_price.toFixed(3)}</td>
        <td class="num ${cls(p.unrealized_pnl)}">${signed(p.unrealized_pnl)}</td>
        <td><button class="tiny-close${canTrade() ? "" : " locked"}"
             data-token="${p.token_id}" data-side="${p.side}">sell</button></td>
      </tr>`).join("");
    tb.querySelectorAll(".tiny-close").forEach((b) =>
      b.onclick = guard(async () => {
        b.textContent = "…";
        const r = await postTrade({ token_id: b.dataset.token,
                                    side: b.dataset.side, action: "sell" });
        note(r && r.error ? "✗ " + r.error
          : `✓ Sold — P&L ${signed(r.realized_pnl ?? (r[0] && r[0].realized_pnl) ?? 0)}`);
        loadMine();
      }));
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="7" class="empty">Could not load portfolio.</td></tr>`;
  }
}

// ---- live markets ----
async function loadMarkets() {
  note("Loading live markets…");
  try {
    const d = await (await fetch("/api/markets?limit=100")).json();
    MARKETS = d.markets || [];
    $("marketsCount").textContent =
      `${MARKETS.length} live · ranked by 24h volume`;
    note(`Bot baseline gates for reference: vol24h ≥ $${d.gates.min_volume24h.toLocaleString()}, ` +
         `favorite in [${d.gates.fav_low}–${d.gates.fav_high}]. ` +
         `Manual trades ignore them — only your cash balance and real book depth apply.`);
  } catch (e) {
    MARKETS = [];
    note("✗ Market scan failed — Polymarket may be unreachable.");
  }
  renderMarkets();
}

function renderMarkets() {
  const q = ($("marketSearch").value || "").toLowerCase();
  const onlyGates = $("onlyGates").checked;
  const tb = $("marketsTable").querySelector("tbody");
  const rows = MARKETS.filter((m) =>
    (!q || m.question.toLowerCase().includes(q)) &&
    (!onlyGates || (m.gate_volume && m.gate_band)));
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="7" class="empty">No markets match.</td></tr>`;
    return;
  }
  const lock = canTrade() ? "" : " locked";
  tb.innerHTML = rows.map((m, i) => {
    const gates = (m.gate_volume && m.gate_band)
      ? `<span class="gate-chip ok" title="Passes the bots' baseline volume + price-band gates (book depth/spread still checked at fill time)">✓ gates</span>`
      : `<span class="gate-chip" title="Outside the bots' baseline gates — you can still trade it">—</span>`;
    const tags = (m.tags || []).map((t) =>
      `<span class="tag-chip" title="Market class some bots exclude">${t.replace("_", " ")}</span>`).join("");
    return `
    <tr data-i="${i}">
      <td class="mkt mkt-wide" title="${esc(m.question)}">${esc(m.question)}${tags}</td>
      <td class="num">${m.yes_price.toFixed(3)}</td>
      <td class="num">${m.no_price.toFixed(3)}</td>
      <td class="num">$${Math.round(m.volume24hr).toLocaleString()}</td>
      <td>${gates}</td>
      <td><button class="btn btn-mini book-btn" data-yes="${m.yes_token}"
           data-no="${m.no_token}" title="Fetch the live order book: spread + walkable depth">book</button></td>
      <td><div class="trade-cell">
        <input type="number" min="1" step="1" value="10" title="USD to spend" class="amt" />
        <button class="btn btn-mini btn-buy-yes buy${lock}" data-side="YES"
          data-token="${m.yes_token}" title="Market-buy YES at the simulated book fill">Buy YES</button>
        <button class="btn btn-mini btn-buy-no buy${lock}" data-side="NO"
          data-token="${m.no_token}" title="Market-buy NO at the simulated book fill">Buy NO</button>
      </div></td>
    </tr>`;
  }).join("");

  const list = rows;  // captured for handlers
  tb.querySelectorAll(".buy").forEach((b) =>
    b.onclick = guard(async () => {
      const row = b.closest("tr");
      const amt = parseFloat(row.querySelector(".amt").value);
      const m = list[+row.dataset.i];
      if (!(amt > 0)) return note("✗ Enter a positive amount.");
      b.disabled = true; b.textContent = "…";
      const r = await postTrade({ token_id: b.dataset.token,
                                  side: b.dataset.side, action: "buy", amount: amt });
      b.disabled = false; b.textContent = "Buy " + b.dataset.side;
      note(r && r.error ? "✗ " + r.error
        : `✓ Bought ${r.shares} ${r.side} @ ${r.avg_price.toFixed(3)} — ` +
          `${money(r.total_cost)} into "${m.question.slice(0, 50)}"`);
      loadMine();
    }));

  tb.querySelectorAll(".book-btn").forEach((b) =>
    b.onclick = async () => {
      const row = b.closest("tr");
      const next = row.nextElementSibling;
      if (next && next.classList.contains("book-row")) { next.remove(); return; }
      b.textContent = "…";
      try {
        const [yes, no] = await Promise.all([
          (await fetch("/api/markets/book?token_id=" + b.dataset.yes)).json(),
          (await fetch("/api/markets/book?token_id=" + b.dataset.no)).json(),
        ]);
        const fmt = (s, x) => x.error
          ? `${s}: book unavailable`
          : `${s}: bid ${x.best_bid ?? "—"} / ask ${x.best_ask ?? "—"} · ` +
            `spread ${x.spread ?? "—"} · depth $${x.ask_depth_usd.toLocaleString()} asks / ` +
            `$${x.bid_depth_usd.toLocaleString()} bids`;
        row.insertAdjacentHTML("afterend",
          `<tr class="book-row"><td colspan="7"><div class="book-cols">
             <span>${fmt("YES", yes)}</span><span>${fmt("NO", no)}</span>
           </div></td></tr>`);
      } catch (e) { note("✗ Book fetch failed."); }
      b.textContent = "book";
    });
}

async function postTrade(body) {
  try {
    const r = await fetch("/api/trade", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, profile: TARGET }),
    });
    if (r.status === 403) { showSpectatorModal(); return { error: "spectator" }; }
    return await r.json();
  } catch (e) { return { error: "network error" }; }
}

function note(text) { $("marketsStatus").textContent = text; }

// ---- init ----
$("marketSearch").oninput = renderMarkets;
$("onlyGates").onchange = renderMarkets;
$("btnReloadMarkets").onclick = loadMarkets;

document.addEventListener("auth-ready", async () => {
  await loadProfilesAndTarget();
  loadMine();
  loadMarkets();
});
setInterval(loadMine, 15000);
