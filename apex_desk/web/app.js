// apex_desk/web/app.js
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const fmt = (n, d = 2) => n == null || isNaN(n) ? "-" : Number(n).toLocaleString("en-GB", {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (n, d = 1) => n == null ? "-" : (n * 100).toFixed(d) + "%";
const usd = n => n == null ? "-" : "$" + fmt(n, 0);
const TRADABLE = ["A", "B", "W"];
const TIER_RANK = {A: 0, B: 1, W: 2, C: 3};
const BROKER_BLOCKED = ["close_only", "disabled", "short_only", "no_symbol"];      // symbols the broker will not open a long on
const state = {status: null, signals: [], filter: "tradable", setup: "", search: "", sort: "tier", cards: new Map(), charts: new Map(), tab: "signals", limit: 40, hover: null};
// Styling copied from the TradingView chart: EMA 10 red, 20 yellow, 50 purple, 200 white; Donchian(20) upper/lower blue, basis orange.
const LINES = {"EMA 10": "#ff5252", "EMA 20": "#ffeb3b", "EMA 50": "#9c27b0", "EMA 200": "#ffffff", "Donchian upper": "#2962ff", "Donchian lower": "#2962ff",
  "Donchian basis": "#ff6d00", "Breakout level (prior 20d high)": "#26a69a", "BB upper": "#5c6f9a", "BB mid": "#5c6f9a88", "BB lower": "#5c6f9a"};
const DASH = new Set(["Breakout level (prior 20d high)", "BB upper", "BB mid", "BB lower"]);
const DEFAULT_ON = new Set(["EMA 10", "EMA 20", "EMA 50", "EMA 200", "Donchian upper", "Donchian lower"]);
const THICK = new Set(["EMA 10", "EMA 20", "EMA 50", "EMA 200", "Donchian upper", "Donchian lower", "Donchian basis"]);
const LW15 = (window.devicePixelRatio || 1) >= 1.45 ? 1.5 : 2;      // lightweight-charts snaps widths to whole device pixels
const isTradable = s => TRADABLE.includes(s.tier) || !!s.watch;
const sv = () => state.tab === "signals" || state.tab === "watch";
let offsetMs = 0;
const LON = "Europe/London";
const P = (d, o) => Object.fromEntries(new Intl.DateTimeFormat("en-GB", {timeZone: LON, hourCycle: "h23", ...o}).formatToParts(d).map(x => [x.type, x.value]));
const fmtDT = iso => { const p = P(new Date(iso), {weekday: "short", day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit"}); return `${p.weekday} ${p.day}-${p.month}-${p.year} ${p.hour}:${p.minute}`; };
const fmtDate = iso => iso ? String(iso).slice(0, 10).split("-").reverse().join("-") : "";
const fmtTs = ts => ts ? `${fmtDate(ts)} ${String(ts).slice(11, 19)}` : "";
const dmy = fmtDate;
const todayISO = () => { const p = P(new Date(Date.now() + offsetMs), {year: "numeric", month: "2-digit", day: "2-digit"}); return `${p.year}-${p.month}-${p.day}`; };
const ageD = s => Math.max(0, Math.round((Date.parse(todayISO()) - Date.parse(s.signal_date)) / 864e5));
const ageTxt = s => { const n = ageD(s); return n === 0 ? "today" : n === 1 ? "1 day ago" : n + " days ago"; };
function tabRows() {
  const bd = state.status.board_date, board = new Set(state.signals.filter(s => !bd || s.signal_date >= bd).map(s => s.ticker));
  if (state.tab === "watch") return state.signals.filter(s => bd && s.signal_date < bd && isTradable(s) && !board.has(s.ticker));
  return state.signals.filter(s => !bd || s.signal_date >= bd);
}

async function api(path, opt) {
  if (window.DESK_CLOUD) return window.DESK_CLOUD.api(path, opt);
  const r = await fetch(path, opt && opt.body ? {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(opt.body)} : opt);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch (e) {} throw new Error(m); }
  return r.json();
}
function toast(msg, err) { const t = $("#toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : ""); clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.add("hidden"), 4500); }
const reviewer = () => $("#reviewer").value || "Nish";
const tierLabel = k => (state.status.tiers[k] || {}).label || "Info only";

// ---------- clock / countdown ring ----------
const CIRC = 2 * Math.PI * 52;
const STATES = ["s-ok", "s-warn", "s-crit", "s-locked", "s-open", "s-closed", "s-batch", "s-soon"];
const setState = (el, c) => { if (!el.classList.contains(c)) { el.classList.remove(...STATES); el.classList.add(c); } };
const dur = sec => { sec = Math.max(0, sec); const d = Math.floor(sec / 86400), t = [Math.floor(sec % 86400 / 3600), Math.floor(sec % 3600 / 60), sec % 60].map(x => String(x).padStart(2, "0")).join(":"); return d > 0 ? `${d}d ${t}` : t; };
let prevLocked = null;
function tick() {
  const s = state.status; if (!s) return;
  const now = new Date(Date.now() + offsetMs), n = +now;
  $("#clkLon").textContent = now.toLocaleTimeString("en-GB", {timeZone: "Europe/London"});
  $("#clkNy").textContent = now.toLocaleTimeString("en-GB", {timeZone: "America/New_York"});
  // 1) decision window: counts down only from the batch publication to the cut-off, then locks
  const cut = +new Date(s.cutoff_utc), rev = +new Date(s.review_open_utc), left = Math.floor((cut - n) / 1000), open = n >= rev && left > 0, box = $("#cdBox");
  if (open) {
    $("#cdTime").textContent = dur(left); $("#cdSub").textContent = `Cut-off ${fmtDT(s.cutoff_utc)} London`;
    setState(box, left < 900 ? "s-crit" : left < 7200 ? "s-warn" : "s-ok");
    $("#ringFg").style.strokeDashoffset = String(CIRC * (1 - Math.min(1, left * 1000 / Math.max(cut - rev, 1))));
  } else {
    setState(box, "s-locked"); $("#cdSub").textContent = `Reopens ${fmtDT(s.next_batch_utc)} London`;
    if (prevLocked === false) { box.classList.add("locking"); setTimeout(() => box.classList.remove("locking"), 2600); }
  }
  prevLocked = !open; state.windowOpen = open;
  // 2) US market
  const m = s.market || {}, mk = $("#tMarket");
  if (m.is_open && n < +new Date(m.close_utc)) {
    setState(mk, "s-open"); $("#mkTitle").textContent = "MARKET OPEN · CLOSES IN"; $("#mkTime").textContent = dur(Math.floor((+new Date(m.close_utc) - n) / 1000)); $("#mkSub").textContent = `Closes ${fmtDT(m.close_utc)} London`;
  } else if (!m.is_open && m.next_open_utc && n < +new Date(m.next_open_utc)) {
    setState(mk, "s-closed"); $("#mkTitle").textContent = "MARKET CLOSED · OPENS IN"; $("#mkTime").textContent = dur(Math.floor((+new Date(m.next_open_utc) - n) / 1000)); $("#mkSub").textContent = `Opens ${fmtDT(m.next_open_utc)} London`;
  } else { $("#mkTitle").textContent = "US MARKET"; $("#mkTime").textContent = "--:--:--"; $("#mkSub").textContent = "Updating market hours..."; }
  // 3) next signal batch
  const nb = +new Date(s.next_batch_utc), bl = Math.floor((nb - n) / 1000), bt = $("#tBatch");
  if (bl > 0) { setState(bt, bl < 600 ? "s-soon" : "s-batch"); $("#btTitle").textContent = "NEXT SIGNAL BATCH IN"; $("#btTime").textContent = dur(bl); $("#btSub").textContent = `Due ${fmtDT(s.next_batch_utc)} London`; }
  else { setState(bt, "s-soon"); $("#btTitle").textContent = "NEW BATCH DUE NOW"; $("#btTime").textContent = "00:00:00"; $("#btSub").textContent = "The scan is running; the board refreshes automatically"; }
}
document.querySelectorAll(".candles").forEach((c, k) => { for (let i = 0; i < 22; i++) { const e = document.createElement("i"); const r = Math.abs(Math.sin((i + 1) * (k + 3) * 12.9898) * 43758.5453) % 1; e.style.cssText = `--h:${(10 + r * 34).toFixed(0)}px;--t:${(5 + r * 6).toFixed(1)}s;--d:-${(r * 9).toFixed(1)}s`; c.appendChild(e); } });
setInterval(tick, 1000);

// ---------- hero / KPIs / dock ----------
function renderHero() {
  const s = state.status, c = s.counts, rev = c.trade + c.skip;
  $("#heroTitle").textContent = state.tab === "watch" ? `WATCHLIST FOR THE ${fmtDate(s.session_date)} US OPEN` : `REVIEW SETUPS FOR THE ${fmtDate(s.session_date)} US OPEN`;
  const nb = t => `<b class="nb">${t}</b>`;                                  // bold chunk that never breaks across lines (dates, times)
  const ln = t => `<span class="ln">${t}</span>`;                            // one sentence per line, so wrapping stays tidy
  $("#heroSub").innerHTML = state.tab === "watch"
    ? ln(`Older setups that were not traded stay here, tagged with their <b>signal date</b> and <b>last decision</b>.`) +
      ln(`Candles and numbers refresh after every daily scan.`) +
      ln(`Trade any of them at the next open on ${nb(s.open_london + " London")}, or leave them here.`)
    : ln(`Latest signals formed at the close of ${nb(fmtDate(s.board_date) || "n/a")}.`) +
      ln(`Orders go out at the US open on ${nb(s.open_london + " London")}.`) +
      ln(`Choose <b>Trade</b> or <b>Don't trade</b> for each setup, then confirm the batch before the cut-off.`) +
      ln(`Older setups are on the <b>Watchlist</b> tab.`);
  $("#revBar").style.width = (c.tradable ? rev / c.tradable * 100 : 0) + "%";
  $("#revText").textContent = `${rev} of ${c.tradable} tradable setups reviewed  ·  ${c.trade} Trade  ·  ${c.skip} Don't trade  ·  ${c.undecided} still to review`;
}
function renderKpis() {
  const s = state.status, c = s.counts, ex = s.exposure, b = s.batch, bt = c.by_tier;
  const gx = ex.gross_x + b.est_gross_x, cap = s.max_gross_x || 5;
  const k = [
    ["Tradable setups", c.tradable, `A ${bt.A || 0}  ·  B ${bt.B || 0}  ·  W ${bt.W || 0}  ·  Info only ${bt.C || 0}`, "", null],
    ["Confirmed trades", c.trade, `${usd(b.est_risk)} estimated risk (${fmt(b.est_risk_pct, 2)}% of equity)`, b.est_risk_pct > 20 ? "warn" : "", Math.min(100, b.est_risk_pct / 20 * 100)],
    ["Projected gross", fmt(gx, 2) + "x", `Now ${fmt(ex.gross_x, 2)}x  ·  cap ${cap}x`, gx > cap ? "bad" : gx > cap * .8 ? "warn" : "", Math.min(100, gx / cap * 100)],
    ["Open positions", `${ex.open_positions}/${s.max_positions}`, `${b.free_slots} slots free  ·  max ${s.max_per_ticker} per ticker`, "", ex.open_positions / s.max_positions * 100],
    ["Open stop risk", fmt(ex.open_risk_pct, 1) + "%", `${usd(ex.open_risk)} lost if every stop is hit`, ex.open_risk_pct > 30 ? "warn" : "", Math.min(100, ex.open_risk_pct / 40 * 100)],
    ["Equity", usd(s.equity), s.executor ? `${String(s.executor.broker).toUpperCase()} snapshot at ${s.executor.ts.slice(11, 16)} UTC` : "No broker snapshot yet", "", null],
  ];
  $("#kpis").innerHTML = k.map(([t, v, sub, cls, bar]) => `<div class="kpi ${cls}"><small>${t}</small><b>${v}</b><em>${sub}</em>${bar == null ? "" : `<div class="bar"><i style="width:${bar}%"></i></div>`}</div>`).join("");
}
function renderAction() {
  const s = state.status, b = s.batch, c = s.counts, queued = c.queued > 0 || c.executed > 0;
  $("#btnConfirm").textContent = queued ? `QUEUED FOR THE OPEN (${c.queued + c.executed})` : `TRADE ALL CONFIRMED (${b.count})`;
  $("#btnConfirm").disabled = queued || !b.count || !state.windowOpen;
  $("#btnUnlock").classList.toggle("hidden", !(c.queued > 0 && state.windowOpen));
  let t = `<b>${c.trade}</b> Trade, <b>${c.skip}</b> Don't trade, <b>${c.undecided}</b> still to review. `;
  if (queued) t += `<b>Batch locked.</b> Orders are placed at the US open (${s.open_london} London).`;
  else if (!state.windowOpen) t += `Review is locked. It reopens with the next signal batch (<span class="nb">${fmtDT(s.next_batch_utc)} London</span>).`;
  else t += `Estimated risk ${usd(b.est_risk)} (${fmt(b.est_risk_pct, 2)}% of equity), notional ${usd(b.est_notional)}.`;
  $("#actionSummary").innerHTML = t;
  const msgs = [];
  if (!s.last_scan) msgs.push(["bad", "No scan has run yet. Click Re-scan, or wait for the scheduled scan at 17:20 ET (22:20 London)."]);
  else if (s.data_late && s.data_late.late) {                                    // only when the batch is genuinely overdue
    msgs.push(["warn", `Signals for <b>${dmy(s.data_late.expected)}</b> are late: the price data has not refreshed yet (latest candles: ${dmy(s.data_late.have)}). The desk is retrying and will publish the new batch automatically.`]);
  }
  (b.warnings || []).forEach(w => msgs.push(["warn", w]));
  const bn = $("#banner"); bn.classList.toggle("hidden", !msgs.length);
  if (msgs.length) { bn.className = "banner " + msgs[0][0]; bn.innerHTML = msgs.map(m => m[1]).join("<br>"); }
}

// ---------- chips / legend / filters ----------
function renderChips() {
  const rows = tabRows(), tr = rows.filter(isTradable), bt = {}, T = state.status.tiers;
  rows.forEach(r => bt[r.tier] = (bt[r.tier] || 0) + 1);
  const c = {tradable: tr.length, undecided: tr.filter(x => x.decision === "none").length, trade: tr.filter(x => x.decision === "trade").length, skip: tr.filter(x => x.decision === "skip").length};
  const defs = [["tradable", "All tradable", c.tradable], ["A", "A", bt.A || 0, "var(--tA)", T.A.label], ["B", "B", bt.B || 0, "var(--tB)", T.B.label], ["W", "W", bt.W || 0, "var(--tW)", T.W.label],
    ["undec", "Still to review", c.undecided], ["trade", "Trade", c.trade], ["skip", "Don't trade", c.skip], ["C", "Info only", bt.C || 0]];
  $("#chips").innerHTML = defs.map(([k, l, n, col, tip]) => `<button class="chip ${state.filter === k ? "on" : ""}" data-k="${k}" ${tip ? `title="${tip}"` : ""}>${col ? `<b class="ltr" style="color:${col}">${k}</b>${tip}` : l}<i>${n}</i></button>`).join("");
  $("#chips").querySelectorAll(".chip").forEach(b => b.onclick = () => { state.filter = b.dataset.k; state.limit = 40; renderChips(); renderList(true); });
  const sel = $("#setupSel"); if (sel.options.length <= 1) Object.entries(state.status.setup_info).forEach(([k, v]) => sel.add(new Option(v.label, k)));
  $("#tierLegend").innerHTML = ["A", "B", "W"].map(k => `<span><b style="color:var(--t${k})">${k}</b> ${T[k].label}: ${T[k].backtest} (risk ×${T[k].risk_mult})</span>`).join("");
}
function visibleSignals() {
  let r = tabRows(); const f = state.filter;
  if (f === "tradable") r = r.filter(isTradable);
  else if (f === "undec") r = r.filter(s => isTradable(s) && s.decision === "none");
  else if (f === "trade") r = r.filter(s => isTradable(s) && s.decision === "trade");
  else if (f === "skip") r = r.filter(s => isTradable(s) && s.decision === "skip");
  else r = r.filter(s => s.tier === f);
  if (state.setup) r = r.filter(s => (s.setup_tags || s.setup || "").split("+").includes(state.setup));
  if (state.search) r = r.filter(s => s.ticker.includes(state.search.toUpperCase()));
  const dkey = state.tab === "watch" ? s => -Date.parse(s.signal_date) / 864e5 * 100 : () => 0;
  const key = {tier: s => dkey(s) * 1000 + TIER_RANK[s.tier] * 10 + (s.dist200 + 2), dist: s => s.dist200, ticker: s => s.ticker, risk: s => -s.est_risk}[state.sort];
  return r.sort((a, b) => key(a) > key(b) ? 1 : -1);
}

// ---------- cards ----------
function prevTag(s) {
  const bd = state.status.board_date; if (!bd || s.signal_date >= bd) return "";
  const l = s.last_decision, d = dmy(s.last_decision_date);
  if (l === "skip") return `<span class="badge b-warn">Skipped ${d}</span>`;
  if (l === "trade") return `<span class="badge b-warn">Marked trade ${d}, not queued</span>`;
  return `<span class="badge b-warn">Not reviewed</span>`;
}
const effRisk = s => s.risk_pct != null ? s.risk_pct : (state.status.risk_pct || 0.01) * (s.risk_mult || 1);
const defRisk = s => s.risk_pct_default != null ? s.risk_pct_default : (state.status.risk_pct || 0.01) * (s.risk_mult || 1);
function cardHtml(s) {
  const info = !isTradable(s), T = state.status.tiers[s.tier] || {}, SI = state.status.setup_info[s.setup] || {};
  const setupBadge = s.setup === "BRK" ? "Breakout" : (SI.label || s.setup);
  return `
  <div class="chead"><span class="tk">${s.ticker}</span>
    <span class="badge b-${s.tier}">${s.tier} ${info ? "info" : (T.label || "")}</span>
    <span class="badge b-s">${setupBadge}${s.setup_tags && s.setup_tags.includes("+") ? " +" : ""}</span>
    <span class="badge b-s">Stage ${s.stage} ${s.stage_name}</span>
    <span class="badge b-age" title="Signal formed at the close of ${s.signal_date}">Signal ${dmy(s.signal_date)} &middot; ${ageTxt(s)}</span>
    ${prevTag(s)}${s.watch && !TRADABLE.includes(s.tier) ? '<span class="badge b-star">&#9733; Starred</span>' : ""}<span class="badge b-bad hidden" data-f="bm"></span>
    <span class="right"><span class="rm" title="Tier risk multiple">Risk ×${s.risk_mult || 1}</span><span class="state" data-f="state"></span>
    <a href="https://www.tradingview.com/chart/?symbol=${s.ticker}" target="_blank" rel="noopener">OPEN IN TRADINGVIEW &#8599;</a></span></div>
  <div class="hint-bt">${info ? "Info only. Star it to make it tradable and keep it on the Watchlist." : (s.setup !== "BRK" && SI.backtest ? "Backtest: " + SI.backtest : "Tier " + s.tier + " backtest: " + (T.backtest || ""))}</div>
  <div class="metrics">
    <div><small>${s.sig_close && Math.abs(s.close / s.sig_close - 1) > 0.0005 ? "Last close" : "Close"}</small><b>$${fmt(s.close)}${s.sig_close && Math.abs(s.close / s.sig_close - 1) > 0.0005 ? ` <span class="${s.close >= s.sig_close ? "pos" : "neg"} sm">${pct(s.close / s.sig_close - 1)} vs signal $${fmt(s.sig_close)}</span>` : ""}</b></div>
    <div><small>vs EMA200</small><b class="${s.dist200 < 0 ? "neg" : "pos"}">${pct(s.dist200)}</b></div>
    <div><small>Stage 2 age</small><b>${s.days_since_s2 > 900 ? "n/a" : s.days_since_s2 + " bars"}</b></div>
    <div><small>Stop (ref)</small><b>$${fmt(s.stop_price)} <span class="muted">(${pct(s.stop_pct)})</span></b></div>
    <div><small>Size</small><b>${fmt(s.est_shares, 0)} sh</b></div>
    <div><small>Risk / Notional</small><b>${usd(s.est_risk)} / ${usd(s.est_notional)}</b></div>
  </div>
  <div class="metrics sub">
    <div><small>ATR14</small><b>${pct(s.atr_pct)}</b></div><div><small>Vol ratio</small><b>${fmt(s.volume_ratio)}</b></div>
    <div><small>RSI</small><b>${fmt(s.rsi, 0)}</b></div><div><small>Avg $ volume (20d)</small><b>${usd(s.avg_dollar_vol)}</b></div>
    <div><small>Open in same ticker</small><b>${s.open_same_ticker}/${state.status.max_per_ticker}</b></div><div><small>Signal / data as of</small><b>${fmtDate(s.signal_date)} / ${fmtDate(s.as_of || s.signal_date)}</b></div>
  </div>
  <div class="chart" data-f="chart"><div class="ph">Loading chart…</div></div>
  <div class="legend">${Object.keys(LINES).map(n => `<span class="lg ${DEFAULT_ON.has(n) ? "on" : ""}" data-n="${n}"><i style="background:${LINES[n]}"></i>${n}</span>`).join("")}</div>
  ${info ? `<div class="decide"><button class="star" data-f="star">&#9734; STAR TO TRADE</button><span class="muted">Starring keeps it on the Watchlist and lets you trade it with a manual risk (default ${pct((state.status.risk_pct || 0.01) * (state.status.manual_risk_mult || 0.5), 2)} of equity).</span></div>` :
  `<div class="decide"><button class="go" data-f="go">&#10004; TRADE</button><button class="no" data-f="no">&#10006; DON'T TRADE</button><input data-f="note" placeholder="Note (optional)">
    <div class="riskrow"><label>Risk per trade <input data-f="risk" type="number" step="0.05" min="0.05" max="${(state.status.max_risk_pct || 0.02) * 100}"> % of equity</label><span class="rdef" data-f="rdef"></span><button class="lnk" data-f="rreset">Reset to default</button></div></div>`}`;
}
function decorate(s, el) {
  el.className = "card t" + s.tier + (!isTradable(s) ? " info" : "") + (s.decision === "trade" ? " trade" : s.decision === "skip" ? " skip" : "");
  const st = el.querySelector('[data-f="state"]'), es = s.exec_status;
  if (es === "none") { st.className = "state" + (isTradable(s) && s.decision === "none" ? " await" : ""); st.textContent = isTradable(s) && s.decision === "none" ? "TO REVIEW" : ""; }
  else { st.className = "state " + es; st.textContent = es.toUpperCase() + (s.exec_detail && es !== "queued" ? " : " + s.exec_detail : ""); }
  if (isTradable(s)) {
    const go = el.querySelector('[data-f="go"]'), no = el.querySelector('[data-f="no"]'), n = el.querySelector('[data-f="note"]'), lock = !!s.locked_reason;
    go.classList.toggle("on", s.decision === "trade"); no.classList.toggle("on", s.decision === "skip");
    const bmode = BROKER_BLOCKED.includes(s.broker_mode) ? s.broker_mode : "", bm = el.querySelector('[data-f="bm"]');
    bm.classList.toggle("hidden", !bmode); bm.textContent = bmode ? "BROKER: " + bmode.replace(/_/g, " ").toUpperCase() : ""; bm.title = "The broker will not open new trades on this symbol. Execution would be rejected.";
    go.disabled = lock || (!!bmode && s.decision !== "trade"); no.disabled = n.disabled = lock;
    go.title = bmode && s.decision !== "trade" ? "Broker does not allow new trades on this symbol (" + bmode.replace(/_/g, " ") + ")" : (s.locked_reason || ""); no.title = s.locked_reason || "";
    if (document.activeElement !== n) n.value = s.note || "";
    const ri = el.querySelector('[data-f="risk"]'), rr = el.querySelector('[data-f="rreset"]'), eff = effRisk(s), df = defRisk(s), ovr = Math.abs(eff - df) > 1e-9;
    if (document.activeElement !== ri) ri.value = (eff * 100).toFixed(2);
    ri.disabled = lock; ri.classList.toggle("ovr", ovr); rr.disabled = lock; rr.classList.toggle("hidden", !ovr);
    el.querySelector('[data-f="rdef"]').textContent = ovr ? `default ${(df * 100).toFixed(2)}%` : `default (${(df * 100).toFixed(2)}%)`;
  }
}
async function decide(s, d, el) {
  try { await api(`/api/signals/${s.id}/decision`, {body: {decision: d, by: reviewer(), note: (el.querySelector('[data-f="note"]') || {}).value || ""}}); await refresh(); }
  catch (e) { toast(e.message, true); }
}
function buildCard(s) {
  const el = document.createElement("div"); el.dataset.id = s.id; el.innerHTML = cardHtml(s);
  el.onmouseenter = () => state.hover = {s, el}; el.onmouseleave = () => { if (state.hover && state.hover.el === el) state.hover = null; };
  if (!isTradable(s)) {
    el.querySelector('[data-f="star"]').onclick = async () => { try { await api(`/api/signals/${s.id}/watch`, {body: {by: reviewer()}}); toast(`${s.ticker} is starred: it is now tradable and stays on the Watchlist.`); await refresh(); } catch (e) { toast(e.message, true); } };
  }
  if (isTradable(s)) {
    const setRisk = async v => { try { await api(`/api/signals/${s.id}/risk`, {body: {risk_pct: v, by: reviewer()}}); await refresh(); } catch (e) { toast(e.message, true); await refresh(); } };
    el.querySelector('[data-f="risk"]').onchange = e => { const v = parseFloat(e.target.value) / 100; if (v > 0) setRisk(v); };
    el.querySelector('[data-f="rreset"]').onclick = () => setRisk(defRisk(s));
    el.querySelector('[data-f="go"]').onclick = () => decide(s, s.decision === "trade" ? "none" : "trade", el);
    el.querySelector('[data-f="no"]').onclick = () => decide(s, s.decision === "skip" ? "none" : "skip", el);
    el.querySelector('[data-f="note"]').onchange = () => { if (s.decision !== "none") decide(s, s.decision, el); };
  }
  decorate(s, el); io.observe(el); return el;
}
const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { io.unobserve(e.target); loadChart(e.target); } }), {rootMargin: "600px"});
async function loadChart(el) {
  const s = state.signals.find(x => String(x.id) === el.dataset.id); if (!s) return;
  const host = el.querySelector('[data-f="chart"]');
  try {
    const d = await api(`/api/chart/${s.ticker}?sid=${encodeURIComponent(s.sid || (s.signal_date + "|" + s.ticker))}`);
    host.innerHTML = "";
    const ch = LightweightCharts.createChart(host, {width: host.clientWidth, height: host.clientHeight, layout: {background: {type: "solid", color: "rgba(0,0,0,0)"}, textColor: "#93a3c3", fontFamily: "JetBrains Mono, Inter, monospace", fontSize: 11},
      grid: {vertLines: {color: "rgba(255,255,255,.04)"}, horzLines: {color: "rgba(255,255,255,.04)"}}, rightPriceScale: {borderColor: "rgba(255,255,255,.1)"},
      timeScale: {borderColor: "rgba(255,255,255,.1)", tickMarkFormatter: (t, type) => {   // t may be a "YYYY-MM-DD" string, a unix timestamp or a {year,month,day} object depending on the library path
        let y, m, d;
        if (typeof t === "string") [y, m, d] = t.slice(0, 10).split("-");
        else if (typeof t === "number") { const x = new Date(t * 1000); y = x.getUTCFullYear(); m = x.getUTCMonth() + 1; d = x.getUTCDate(); }
        else if (t) { y = t.year; m = t.month; d = t.day; }
        if (y == null) return "";
        m = String(m).padStart(2, "0"); d = String(d).padStart(2, "0");
        return type === 0 ? `${y}` : type === 1 ? `${m}-${y}` : `${d}-${m}`;
      }},
      localization: {dateFormat: "dd-MM-yyyy"}, crosshair: {mode: 0}});
    const cs = ch.addCandlestickSeries({upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false});
    cs.setData(d.candles);
    const prior = d.signals.filter(t => t < s.signal_date).slice(-3).map(t => ({time: t, position: "belowBar", color: "#5c6f9a", shape: "arrowUp", text: ""}));
    cs.setMarkers(prior.concat([{time: s.signal_date, position: "belowBar", color: s.tier === "W" ? "#7aa2ff" : "#22d39a", shape: "arrowUp", text: s.setup === "BRK" ? "BREAKOUT" : (state.status.setup_info[s.setup] || {}).label.toUpperCase()}]));
    cs.createPriceLine({price: s.stop_price, color: "#ff5d73", lineWidth: 1, lineStyle: 2, title: `Stop ${state.status.atr_multiplier}xATR`});
    cs.createPriceLine({price: s.sig_close || s.close, color: "#cfd8ee", lineWidth: 1, lineStyle: 3, title: "Signal close"});
    const vol = ch.addHistogramSeries({priceFormat: {type: "volume"}, priceScaleId: "vol"});
    ch.priceScale("vol").applyOptions({scaleMargins: {top: 0.82, bottom: 0}}); vol.setData(d.volume);
    const ser = {};
    Object.keys(LINES).forEach(n => {
      const l = ch.addLineSeries({color: LINES[n], lineWidth: THICK.has(n) ? LW15 : 1, lineStyle: DASH.has(n) ? 2 : 0, priceLineVisible: false, lastValueVisible: false, visible: DEFAULT_ON.has(n)});
      l.setData(d.lines[n] || []); ser[n] = l;
    });
    el.querySelectorAll(".lg").forEach(b => b.onclick = () => { const on = !b.classList.contains("on"); b.classList.toggle("on", on); ser[b.dataset.n].applyOptions({visible: on}); });
    const n = d.candles.length; ch.timeScale().setVisibleLogicalRange({from: Math.max(0, n - 130), to: n + 4});
    new ResizeObserver(() => ch.applyOptions({width: host.clientWidth})).observe(host);
    state.charts.set(s.id, ch);
  } catch (e) { host.innerHTML = `<div class="ph" style="animation:none">Chart unavailable: ${e.message}</div>`; }
}
function renderList(rebuild) {
  const vis = visibleSignals(), lim = vis.slice(0, state.limit), wrap = $("#cards"), ids = lim.map(s => String(s.id)).join(",");
  if (rebuild || wrap.dataset.ids !== ids) {
    state.charts.forEach(c => { try { c.remove(); } catch (e) {} }); state.charts.clear();
    wrap.innerHTML = ""; wrap.dataset.ids = ids;
    let last = null;
    lim.forEach(s => {
      if (state.tab === "watch" && s.signal_date !== last) {
        last = s.signal_date; const g = document.createElement("div"); g.className = "grp";
        g.innerHTML = `<b>Signal of ${dmy(s.signal_date)}</b><span>${ageTxt(s)} &middot; ${vis.filter(x => x.signal_date === last).length} setups</span>`; wrap.appendChild(g);
      }
      wrap.appendChild(buildCard(s));
    });
    if (vis.length > lim.length) { const m = document.createElement("div"); m.className = "empty"; m.style.gridColumn = "1/-1"; m.innerHTML = `<button class="btn">Show more (${vis.length - lim.length} left)</button>`; m.querySelector("button").onclick = () => { state.limit += 40; renderList(true); }; wrap.appendChild(m); }
  } else lim.forEach(s => { const el = wrap.querySelector(`[data-id="${s.id}"]`); if (el) { Object.assign(s, state.signals.find(x => x.id === s.id)); decorate(s, el); } });
  $("#empty").textContent = state.tab === "watch" ? "Watchlist is empty. Setups you do not trade stay here after the next scan." : "No signals in this view.";
  $("#empty").classList.toggle("hidden", lim.length > 0);
  wrap.className = "cards cols" + $("#cols").value;
}

// ---------- tables ----------
const T = (el, cols, rows, rowCls) => { el.innerHTML = `<thead><tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr class="${rowCls ? rowCls(r) : ""}">${cols.map(c => `<td>${c[1](r) ?? ""}</td>`).join("")}</tr>`).join("") || `<tr><td colspan="${cols.length}" class="muted">Nothing to show yet.</td></tr>`}</tbody>`; };
const tierOf = r => ["A", "B", "W", "C"].includes(r.tier) ? r.tier : "";
const tierCls = r => tierOf(r) ? "r" + tierOf(r) : "";                                   // coloured strip at the row start
const tkCell = r => `${tierOf(r) ? `<span class="badge b-${tierOf(r)} tg">${tierOf(r)}</span>` : ""}<b>${r.ticker}</b>`;   // tier tag + ticker
const pnlCell = (v, d = 0) => v == null ? "" : `<span style="color:var(--${v >= 0 ? "ok" : "bad"})">${v >= 0 ? "+" : "-"}$${Math.abs(v).toLocaleString("en-GB", {maximumFractionDigits: d})}</span>`;
const holdDays = r => r.exit_date && r.entry_date ? Math.round((new Date(r.exit_date) - new Date(r.entry_date)) / 864e5) : "";
const sortBy = (rows, ...keys) => [...(rows || [])].sort((a, b) => { for (const key of keys) { const d = key[0] === "-" ? -1 : 1, k = d < 0 ? key.slice(1) : key, x = a[k] ?? "", y = b[k] ?? ""; if (x < y) return -d; if (x > y) return d; } return 0; });   // ascending by default; "-key" = descending
async function loadTab(tab) {
  if (tab === "closed") {
    const c = sortBy(await api("/api/positions?status=closed"), "-exit_date", "-entry_date", "ticker");
    T($("#tblClosed"), [["Ticker", tkCell], ["Symbol", r => r.broker_symbol], ["Ticket", r => r.ticket], ["Entry date", r => fmtDate(r.entry_date)], ["Exit date", r => fmtDate(r.exit_date)], ["Days held", holdDays], ["Entry", r => fmt(r.entry_price, 4)], ["Exit", r => fmt(r.exit_price, 4)], ["Shares", r => fmt(r.shares, 0)], ["Exit reason", r => (r.exit_reason || "").replace(/_/g, " ")], ["P&L $", r => pnlCell(r.pnl_total)], ["R", r => r.pnl_r == null ? "" : `<span style="color:var(--${r.pnl_r >= 0 ? "ok" : "bad"})">${r.pnl_r >= 0 ? "+" : ""}${r.pnl_r.toFixed(2)}R</span>`]], c, tierCls);
  }
  if (tab === "positions") { const p = sortBy(await api("/api/positions"), "-entry_date", "ticker", "ticket"); T($("#tblPos"), [["Ticker", tkCell], ["Symbol", r => r.broker_symbol], ["Ticket", r => r.ticket], ["Entry date", r => fmtDate(r.entry_date)], ["Entry", r => fmt(r.entry_price, 4)], ["Stop", r => fmt(r.stop_price, 4)], ["Shares", r => fmt(r.shares, 0)], ["Lots", r => r.mt5_volume], ["Risk $", r => usd(r.shares * r.stop_distance)]], p, tierCls); }
  if (tab === "history") { const h = sortBy(await api("/api/history"), "-signal_date", "-entry_date", "ticker"); T($("#tblHist"), [["Signal date", r => fmtDate(r.signal_date)], ["Target session", r => fmtDate(r.entry_date)], ["Tier", r => `<span class="badge b-${r.tier}">${r.tier}</span>${r.watch ? ' <span class="badge b-star">&#9733;</span>' : ""}`], ["Ticker", r => `<b>${r.ticker}</b>`], ["Setup", r => r.setup], ["vs EMA200", r => pct(r.dist200)], ["Stage-2 age", r => r.days_since_s2], ["Decision", r => r.decision], ["Previous decision", r => r.last_decision && r.last_decision !== "none" ? `${r.last_decision} (${dmy(r.last_decision_date)})` : ""], ["Risk %", r => r.risk_pct ? (r.risk_pct * 100).toFixed(2) : ""], ["By", r => r.decided_by || ""], ["Execution", r => r.exec_status]], h, tierCls); }
  if (tab === "audit") { const a = sortBy(await api("/api/audit"), "-ts", "action", "-id"); T($("#tblAudit"), [["Time (UTC)", r => fmtTs(r.ts)], ["Actor", r => r.actor], ["Action", r => r.action], ["Detail", r => (r.detail || "").slice(0, 160)]], a); }
}
document.querySelectorAll(".tab").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === b)); state.tab = b.dataset.tab;
  ["signals", "positions", "closed", "history", "audit"].forEach(t => $("#view-" + t).classList.toggle("hidden", t === "signals" ? !sv() : t !== state.tab));
  if (sv() && state.status) { state.limit = 40; state.filter = "tradable"; renderHero(); renderChips(); renderList(true); } else loadTab(state.tab);
});

// ---------- batch + controls ----------
$("#btnConfirm").onclick = async () => {
  const p = await api("/api/batch/preview"), by = {};
  state.signals.filter(s => s.decision === "trade" && s.exec_status === "none" && isTradable(s)).forEach(s => by[s.tier] = (by[s.tier] || 0) + 1);
  $("#modalBody").innerHTML = `<div class="sumgrid"><div><small>Trades</small><b>${p.count}</b></div><div><small>Estimated risk</small><b>${usd(p.est_risk)}</b></div><div><small>Notional</small><b>${usd(p.est_notional)}</b></div></div>
    <p class="muted">By tier: ${Object.entries(by).map(([k, v]) => `${k} ${v}`).join("  ·  ") || "-"}  ·  Free slots: ${p.free_slots}  ·  Gross exposure after this batch: ${fmt(p.est_gross_x, 2)}x</p><p>${p.tickers.join(", ")}</p>` + (p.warnings || []).map(w => `<div class="warnbox">${w}</div>`).join("");
  $("#modal").classList.remove("hidden");
};
$("#mCancel").onclick = () => $("#modal").classList.add("hidden");
$("#mOk").onclick = async () => { try { const r = await api("/api/batch/confirm", {body: {by: reviewer()}}); toast(`Batch #${r.batch_id} is queued for the open: ${r.count} trades.`); } catch (e) { toast(e.message, true); } $("#modal").classList.add("hidden"); refresh(); };
$("#btnUnlock").onclick = async () => { try { const r = await api("/api/batch/unlock", {body: {by: reviewer()}}); toast(`Batch unlocked: ${r.unlocked} setups can be changed again.`); } catch (e) { toast(e.message, true); } refresh(); };
$("#btnScan").onclick = async () => { await api("/api/scan", {body: {}}); toast(window.DESK_CLOUD ? "Scan requested. The Work PC picks it up within a minute." : "Scan started. Results appear in about a minute."); };
$("#search").oninput = e => { state.search = e.target.value; renderList(true); };
$("#setupSel").onchange = e => { state.setup = e.target.value; renderList(true); };
$("#sort").onchange = e => { state.sort = e.target.value; renderList(true); };
$("#cols").onchange = () => renderList(true);
document.addEventListener("keydown", e => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName) || !state.hover || !isTradable(state.hover.s) || state.hover.s.locked_reason) return;
  const k = e.key.toLowerCase(), {s, el} = state.hover;
  if (k === "t") decide(s, "trade", el); else if (k === "d") decide(s, "skip", el); else if (k === "c") decide(s, "none", el);
});

// ---------- refresh loop ----------
async function refresh() {
  try {
    const t0 = Date.now(); const s = await api("/api/status"); offsetMs = new Date(s.now_utc) - (t0 + Date.now()) / 2;
    state.status = s; state.signals = await api("/api/signals?session=" + s.session_date);
    $("#modePill").textContent = s.mode === "live" ? "LIVE ORDERS" : "DRY RUN"; $("#modePill").className = "pill" + (s.mode === "live" ? " live" : "");
    $("#brandName").textContent = s.name.toUpperCase();
    const sel = $("#reviewer"); if (!sel.options.length) { s.reviewers.forEach(n => sel.add(new Option(n, n))); sel.value = localStorage.getItem("desk_reviewer") || s.reviewers[0]; sel.onchange = () => localStorage.setItem("desk_reviewer", sel.value); }
    const bdv = state.status.board_date, bset = new Set(state.signals.filter(x => !bdv || x.signal_date >= bdv).map(x => x.ticker));
    $("#tbSig").textContent = state.signals.filter(x => !bdv || x.signal_date >= bdv).filter(isTradable).length;
    $("#tbWatch").textContent = state.signals.filter(x => bdv && x.signal_date < bdv && isTradable(x) && !bset.has(x.ticker)).length;
    tick(); renderHero(); renderKpis(); renderChips(); renderList(false); renderAction();
  } catch (e) { toast("Cannot load the desk data: " + e.message, true); }
}
(window.DESK_READY || Promise.resolve()).then(() => { refresh(); setInterval(async () => { await refresh(); if (!sv()) loadTab(state.tab); }, 15000); });
})();
