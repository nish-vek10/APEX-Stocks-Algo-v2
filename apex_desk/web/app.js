// apex_desk/web/app.js
(() => {
"use strict";
const $ = (s, r = document) => r.querySelector(s);
const fmt = (n, d = 2) => n == null || isNaN(n) ? "-" : Number(n).toLocaleString("en-GB", {minimumFractionDigits: d, maximumFractionDigits: d});
const pct = (n, d = 1) => n == null ? "-" : (n * 100).toFixed(d) + "%";
const usd = n => n == null ? "-" : "$" + fmt(n, 0);
const TRADABLE = ["A", "B", "W"];
const TIER_RANK = {A: 0, B: 1, W: 2, C: 3};
const state = {status: null, signals: [], filter: "tradable", setup: "", search: "", sort: "tier", cards: new Map(), charts: new Map(), tab: "signals", limit: 40, hover: null};
const LINES = {"EMA 10": "#f6c945", "EMA 20": "#ff9f43", "EMA 50": "#a66bff", "EMA 200": "#ff4d6d", "Donchian 20 high": "#2bd9a5", "Donchian 20 low": "#2bd9a577", "BB upper": "#5c6f9a", "BB mid": "#5c6f9a88", "BB lower": "#5c6f9a"};
const DASH = new Set(["Donchian 20 low", "BB upper", "BB mid", "BB lower"]);
const DEFAULT_ON = new Set(["EMA 10", "EMA 20", "EMA 50", "EMA 200", "Donchian 20 high"]);
const isTradable = s => TRADABLE.includes(s.tier);

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
let offsetMs = 0;
const CIRC = 2 * Math.PI * 52;
function tick() {
  const s = state.status; if (!s) return;
  const now = new Date(Date.now() + offsetMs);
  $("#clkLon").textContent = now.toLocaleTimeString("en-GB", {timeZone: "Europe/London"});
  $("#clkNy").textContent = now.toLocaleTimeString("en-GB", {timeZone: "America/New_York"});
  const left = Math.floor((new Date(s.cutoff_utc) - now) / 1000), box = $("#cdBox");
  if (left > 0) {
    const h = Math.floor(left / 3600), m = Math.floor(left % 3600 / 60), sec = left % 60;
    $("#cdTime").textContent = [h, m, sec].map(x => String(x).padStart(2, "0")).join(":");
    $("#cdLabel").textContent = "CUT-OFF IN";
    $("#cdSub").textContent = `${s.cutoff_london} London`;
    box.className = "ring-card" + (left < 600 ? " crit" : left < 1800 ? " warn" : "");
    $("#ringFg").style.strokeDashoffset = String(CIRC * (1 - Math.min(1, left / (12 * 3600))));
  } else {
    $("#cdTime").textContent = "CLOSED"; $("#cdLabel").textContent = "DECISIONS LOCKED"; $("#cdSub").textContent = `cut-off was ${s.cutoff_london}`;
    box.className = "ring-card closed"; $("#ringFg").style.strokeDashoffset = String(CIRC);
  }
  state.windowOpen = left > 0;
}
setInterval(tick, 1000);

// ---------- hero / KPIs / dock ----------
function renderHero() {
  const s = state.status, c = s.counts, rev = c.trade + c.skip;
  $("#heroTitle").textContent = `Session ${s.session_date}: review the setups`;
  $("#heroSub").textContent = `Signals formed at the close of ${s.last_scan ? s.last_scan.signal_date : "n/a"}, orders at the US open ${s.open_london} London. Pick Trade or Don't trade on each, then confirm the batch before the cut-off.`;
  $("#revBar").style.width = (c.tradable ? rev / c.tradable * 100 : 0) + "%";
  $("#revText").textContent = `${rev} of ${c.tradable} tradable setups reviewed  |  ${c.trade} trade  |  ${c.skip} don't trade  |  ${c.undecided} to review`;
}
function renderKpis() {
  const s = state.status, c = s.counts, ex = s.exposure, b = s.batch, bt = c.by_tier;
  const gx = ex.gross_x + b.est_gross_x, cap = s.max_gross_x || 5;
  const k = [
    ["Tradable setups", c.tradable, `A ${bt.A || 0}  |  B ${bt.B || 0}  |  W ${bt.W || 0}  |  info ${bt.C || 0}`, "", null],
    ["Confirmed trades", c.trade, `${usd(b.est_risk)} est. risk  (${fmt(b.est_risk_pct, 2)}% of equity)`, b.est_risk_pct > 20 ? "warn" : "", Math.min(100, b.est_risk_pct / 20 * 100)],
    ["Projected gross", fmt(gx, 2) + "x", `now ${fmt(ex.gross_x, 2)}x  |  cap ${cap}x`, gx > cap ? "bad" : gx > cap * .8 ? "warn" : "", Math.min(100, gx / cap * 100)],
    ["Open positions", `${ex.open_positions}/${s.max_positions}`, `${b.free_slots} slots free  |  max ${s.max_per_ticker} per ticker`, "", ex.open_positions / s.max_positions * 100],
    ["Open stop-risk", fmt(ex.open_risk_pct, 1) + "%", `${usd(ex.open_risk)} if every stop hits`, ex.open_risk_pct > 30 ? "warn" : "", Math.min(100, ex.open_risk_pct / 40 * 100)],
    ["Equity", usd(s.equity), s.executor ? `${s.executor.broker} snapshot ${s.executor.ts.slice(11, 16)}Z` : "no executor snapshot yet", "", null],
  ];
  $("#kpis").innerHTML = k.map(([t, v, sub, cls, bar]) => `<div class="kpi ${cls}"><small>${t}</small><b>${v}</b><em>${sub}</em>${bar == null ? "" : `<div class="bar"><i style="width:${bar}%"></i></div>`}</div>`).join("");
}
function renderAction() {
  const s = state.status, b = s.batch, c = s.counts, queued = c.queued > 0 || c.executed > 0;
  $("#btnConfirm").textContent = queued ? `QUEUED FOR THE OPEN (${c.queued + c.executed})` : `TRADE ALL CONFIRMED (${b.count})`;
  $("#btnConfirm").disabled = queued || !b.count || !state.windowOpen;
  $("#btnUnlock").classList.toggle("hidden", !(c.queued > 0 && state.windowOpen));
  let t = `<b>${c.trade}</b> Trade, <b>${c.skip}</b> Don't trade, <b>${c.undecided}</b> to review. `;
  if (queued) t += `<b>Batch locked.</b> Orders fire at the US open (${s.open_london} London).`;
  else if (!state.windowOpen) t += "Cut-off passed: unreviewed setups will not be traded.";
  else t += `Est. risk ${usd(b.est_risk)} (${fmt(b.est_risk_pct, 2)}% of equity), notional ${usd(b.est_notional)}.`;
  $("#actionSummary").innerHTML = t;
  const msgs = [];
  if (!s.last_scan) msgs.push(["bad", "No scan has run yet. Click Re-scan, or wait for the scheduled 17:20 ET scan."]);
  else if (s.last_scan.stale_cache) msgs.push(["bad", `Data is stale: last bars are ${s.last_scan.signal_date}. Check the old scheduler's cache refresh before trusting these.`]);
  if (!state.windowOpen) msgs.push(["warn", "Decision window closed for this session."]);
  (b.warnings || []).forEach(w => msgs.push(["warn", w]));
  const bn = $("#banner"); bn.classList.toggle("hidden", !msgs.length);
  if (msgs.length) { bn.className = "banner " + msgs[0][0]; bn.innerHTML = msgs.map(m => m[1]).join("<br>"); }
}

// ---------- chips / legend / filters ----------
function renderChips() {
  const c = state.status.counts, bt = c.by_tier, T = state.status.tiers;
  const defs = [["tradable", "All tradable", c.tradable], ["A", "A", bt.A || 0, "var(--tA)", T.A.label], ["B", "B", bt.B || 0, "var(--tB)", T.B.label], ["W", "W", bt.W || 0, "var(--tW)", T.W.label],
    ["undec", "To review", c.undecided], ["trade", "Trade", c.trade], ["skip", "Don't trade", c.skip], ["C", "Info only", bt.C || 0]];
  $("#chips").innerHTML = defs.map(([k, l, n, col, tip]) => `<button class="chip ${state.filter === k ? "on" : ""}" data-k="${k}" ${tip ? `title="${tip}"` : ""}>${col ? `<b class="ltr" style="color:${col}">${k}</b>${tip}` : l}<i>${n}</i></button>`).join("");
  $("#chips").querySelectorAll(".chip").forEach(b => b.onclick = () => { state.filter = b.dataset.k; state.limit = 40; renderChips(); renderList(true); });
  const sel = $("#setupSel"); if (sel.options.length <= 1) Object.entries(state.status.setup_info).forEach(([k, v]) => sel.add(new Option(v.label, k)));
  $("#tierLegend").innerHTML = ["A", "B", "W"].map(k => `<span><b style="color:var(--t${k})">${k}</b> ${T[k].label}: ${T[k].backtest} (risk x${T[k].risk_mult})</span>`).join("");
}
function visibleSignals() {
  let r = state.signals.slice(); const f = state.filter;
  if (f === "tradable") r = r.filter(isTradable);
  else if (f === "undec") r = r.filter(s => isTradable(s) && s.decision === "none");
  else if (f === "trade") r = r.filter(s => isTradable(s) && s.decision === "trade");
  else if (f === "skip") r = r.filter(s => isTradable(s) && s.decision === "skip");
  else r = r.filter(s => s.tier === f);
  if (state.setup) r = r.filter(s => (s.setup_tags || s.setup || "").split("+").includes(state.setup));
  if (state.search) r = r.filter(s => s.ticker.includes(state.search.toUpperCase()));
  const key = {tier: s => TIER_RANK[s.tier] * 10 + (s.dist200 + 2), dist: s => s.dist200, ticker: s => s.ticker, risk: s => -s.est_risk}[state.sort];
  return r.sort((a, b) => key(a) > key(b) ? 1 : -1);
}

// ---------- cards ----------
function cardHtml(s) {
  const info = !isTradable(s), T = state.status.tiers[s.tier] || {}, SI = state.status.setup_info[s.setup] || {};
  const setupBadge = s.setup === "BRK" ? "Breakout" : (SI.label || s.setup);
  return `
  <div class="chead"><span class="tk">${s.ticker}</span>
    <span class="badge b-${s.tier}">${s.tier} ${info ? "info" : (T.label || "")}</span>
    <span class="badge b-s">${setupBadge}${s.setup_tags && s.setup_tags.includes("+") ? " +" : ""}</span>
    <span class="badge b-s">Stage ${s.stage} ${s.stage_name}</span>
    <span class="right"><span class="rm">risk x${s.risk_mult || 1}</span><span class="state" data-f="state"></span>
    <a href="https://www.tradingview.com/chart/?symbol=${s.ticker}" target="_blank" rel="noopener">TradingView &#8599;</a></span></div>
  <div class="hint-bt">${info ? "Info only: not tradable from this desk." : (s.setup !== "BRK" && SI.backtest ? "Backtest: " + SI.backtest : "Tier " + s.tier + " backtest: " + (T.backtest || ""))}</div>
  <div class="metrics">
    <div><small>Close</small><b>$${fmt(s.close)}</b></div>
    <div><small>vs EMA200</small><b class="${s.dist200 < 0 ? "neg" : "pos"}">${pct(s.dist200)}</b></div>
    <div><small>Stage 2 age</small><b>${s.days_since_s2 > 900 ? "n/a" : s.days_since_s2 + " bars"}</b></div>
    <div><small>Stop (ref)</small><b>$${fmt(s.stop_price)} <span class="muted">(${pct(s.stop_pct)})</span></b></div>
    <div><small>Size</small><b>${fmt(s.est_shares, 0)} sh</b></div>
    <div><small>Risk / notional</small><b>${usd(s.est_risk)} / ${usd(s.est_notional)}</b></div>
  </div>
  <div class="metrics sub">
    <div><small>ATR14</small><b>${pct(s.atr_pct)}</b></div><div><small>Vol ratio</small><b>${fmt(s.volume_ratio)}</b></div>
    <div><small>RSI</small><b>${fmt(s.rsi, 0)}</b></div><div><small>Avg $ vol 20d</small><b>${usd(s.avg_dollar_vol)}</b></div>
    <div><small>Open same ticker</small><b>${s.open_same_ticker}/${state.status.max_per_ticker}</b></div><div><small>Signal / entry</small><b>${s.signal_date.slice(5)} / ${s.entry_date.slice(5)}</b></div>
  </div>
  <div class="chart" data-f="chart"><div class="ph">Loading chart...</div></div>
  <div class="legend">${Object.keys(LINES).map(n => `<span class="lg ${DEFAULT_ON.has(n) ? "on" : ""}" data-n="${n}"><i style="background:${LINES[n]}"></i>${n}</span>`).join("")}</div>
  ${info ? "" : `<div class="decide"><button class="go" data-f="go">&#10004; TRADE</button><button class="no" data-f="no">&#10006; DON'T TRADE</button><input data-f="note" placeholder="Note (optional)"></div>`}`;
}
function decorate(s, el) {
  el.className = "card t" + s.tier + (!isTradable(s) ? " info" : "") + (s.decision === "trade" ? " trade" : s.decision === "skip" ? " skip" : "");
  const st = el.querySelector('[data-f="state"]'), es = s.exec_status;
  if (es === "none") { st.className = "state" + (isTradable(s) && s.decision === "none" ? " await" : ""); st.textContent = isTradable(s) && s.decision === "none" ? "TO REVIEW" : ""; }
  else { st.className = "state " + es; st.textContent = es.toUpperCase() + (s.exec_detail && es !== "queued" ? " : " + s.exec_detail : ""); }
  if (isTradable(s)) {
    const go = el.querySelector('[data-f="go"]'), no = el.querySelector('[data-f="no"]'), n = el.querySelector('[data-f="note"]'), lock = !!s.locked_reason;
    go.classList.toggle("on", s.decision === "trade"); no.classList.toggle("on", s.decision === "skip");
    go.disabled = no.disabled = n.disabled = lock; go.title = no.title = s.locked_reason || "";
    if (document.activeElement !== n) n.value = s.note || "";
  }
}
async function decide(s, d, el) {
  try { await api(`/api/signals/${s.id}/decision`, {body: {decision: d, by: reviewer(), note: (el.querySelector('[data-f="note"]') || {}).value || ""}}); await refresh(); }
  catch (e) { toast(e.message, true); }
}
function buildCard(s) {
  const el = document.createElement("div"); el.dataset.id = s.id; el.innerHTML = cardHtml(s);
  el.onmouseenter = () => state.hover = {s, el}; el.onmouseleave = () => { if (state.hover && state.hover.el === el) state.hover = null; };
  if (isTradable(s)) {
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
    const d = await api(`/api/chart/${s.ticker}?asof=${s.signal_date}`);
    host.innerHTML = "";
    const ch = LightweightCharts.createChart(host, {width: host.clientWidth, height: host.clientHeight, layout: {background: {color: "#0f1624"}, textColor: "#8696b4", fontFamily: "Inter, Segoe UI, sans-serif"},
      grid: {vertLines: {color: "#162036"}, horzLines: {color: "#162036"}}, rightPriceScale: {borderColor: "#1e2a42"}, timeScale: {borderColor: "#1e2a42"}, crosshair: {mode: 0}});
    const cs = ch.addCandlestickSeries({upColor: "#26a69a", downColor: "#ef5350", wickUpColor: "#26a69a", wickDownColor: "#ef5350", borderVisible: false});
    cs.setData(d.candles);
    const prior = d.signals.filter(t => t < s.signal_date).slice(-3).map(t => ({time: t, position: "belowBar", color: "#5c6f9a", shape: "arrowUp", text: ""}));
    cs.setMarkers(prior.concat([{time: s.signal_date, position: "belowBar", color: s.tier === "W" ? "#7aa2ff" : "#22d39a", shape: "arrowUp", text: s.setup === "BRK" ? "BREAKOUT" : (state.status.setup_info[s.setup] || {}).label.toUpperCase()}]));
    cs.createPriceLine({price: s.stop_price, color: "#ff5d73", lineWidth: 1, lineStyle: 2, title: `Stop ${state.status.atr_multiplier}xATR`});
    cs.createPriceLine({price: s.close, color: "#cfd8ee", lineWidth: 1, lineStyle: 3, title: "Signal close"});
    const vol = ch.addHistogramSeries({priceFormat: {type: "volume"}, priceScaleId: "vol"});
    ch.priceScale("vol").applyOptions({scaleMargins: {top: 0.82, bottom: 0}}); vol.setData(d.volume);
    const ser = {};
    Object.keys(LINES).forEach(n => {
      const l = ch.addLineSeries({color: LINES[n], lineWidth: n === "EMA 200" ? 2 : 1, lineStyle: DASH.has(n) ? 2 : 0, priceLineVisible: false, lastValueVisible: false, visible: DEFAULT_ON.has(n)});
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
    wrap.innerHTML = ""; wrap.dataset.ids = ids; lim.forEach(s => wrap.appendChild(buildCard(s)));
    if (vis.length > lim.length) { const m = document.createElement("div"); m.className = "empty"; m.style.gridColumn = "1/-1"; m.innerHTML = `<button class="btn">Show more (${vis.length - lim.length} left)</button>`; m.querySelector("button").onclick = () => { state.limit += 40; renderList(true); }; wrap.appendChild(m); }
  } else lim.forEach(s => { const el = wrap.querySelector(`[data-id="${s.id}"]`); if (el) { Object.assign(s, state.signals.find(x => x.id === s.id)); decorate(s, el); } });
  $("#empty").classList.toggle("hidden", lim.length > 0);
  wrap.className = "cards cols" + $("#cols").value;
}

// ---------- tables ----------
const T = (el, cols, rows) => { el.innerHTML = `<thead><tr>${cols.map(c => `<th>${c[0]}</th>`).join("")}</tr></thead><tbody>${rows.map(r => `<tr>${cols.map(c => `<td>${c[1](r) ?? ""}</td>`).join("")}</tr>`).join("") || `<tr><td colspan="${cols.length}" class="muted">Nothing yet</td></tr>`}</tbody>`; };
async function loadTab(tab) {
  if (tab === "positions") { const p = await api("/api/positions"); T($("#tblPos"), [["Ticker", r => `<b>${r.ticker}</b>`], ["Symbol", r => r.broker_symbol], ["Ticket", r => r.ticket], ["Entry date", r => r.entry_date], ["Entry", r => fmt(r.entry_price, 4)], ["Stop", r => fmt(r.stop_price, 4)], ["Shares", r => fmt(r.shares, 0)], ["Lots", r => r.mt5_volume], ["Risk $", r => usd(r.shares * r.stop_distance)]], p); }
  if (tab === "history") { const h = await api("/api/history"); T($("#tblHist"), [["Signal", r => r.signal_date], ["Entry", r => r.entry_date], ["Ticker", r => `<b>${r.ticker}</b>`], ["Tier", r => r.tier], ["Setup", r => r.setup], ["vs EMA200", r => pct(r.dist200)], ["Stage-2 age", r => r.days_since_s2], ["Decision", r => r.decision], ["By", r => r.decided_by || ""], ["Execution", r => r.exec_status]], h); }
  if (tab === "audit") { const a = await api("/api/audit"); T($("#tblAudit"), [["Time (UTC)", r => r.ts.replace("T", " ").slice(0, 19)], ["Actor", r => r.actor], ["Action", r => r.action], ["Detail", r => (r.detail || "").slice(0, 160)]], a); }
}
document.querySelectorAll(".tab").forEach(b => b.onclick = () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === b)); state.tab = b.dataset.tab;
  ["signals", "positions", "history", "audit"].forEach(t => $("#view-" + t).classList.toggle("hidden", t !== state.tab)); loadTab(state.tab);
});

// ---------- batch + controls ----------
$("#btnConfirm").onclick = async () => {
  const p = await api("/api/batch/preview"), by = {};
  state.signals.filter(s => s.decision === "trade" && s.exec_status === "none" && isTradable(s)).forEach(s => by[s.tier] = (by[s.tier] || 0) + 1);
  $("#modalBody").innerHTML = `<div class="sumgrid"><div><small>Trades</small><b>${p.count}</b></div><div><small>Est. risk</small><b>${usd(p.est_risk)}</b></div><div><small>Notional</small><b>${usd(p.est_notional)}</b></div></div>
    <p class="muted">By tier: ${Object.entries(by).map(([k, v]) => `${k} ${v}`).join("  |  ") || "-"}  |  free slots ${p.free_slots}  |  gross after batch ${fmt(p.est_gross_x, 2)}x</p><p>${p.tickers.join(", ")}</p>` + (p.warnings || []).map(w => `<div class="warnbox">${w}</div>`).join("");
  $("#modal").classList.remove("hidden");
};
$("#mCancel").onclick = () => $("#modal").classList.add("hidden");
$("#mOk").onclick = async () => { try { const r = await api("/api/batch/confirm", {body: {by: reviewer()}}); toast(`Batch #${r.batch_id} queued: ${r.count} trades`); } catch (e) { toast(e.message, true); } $("#modal").classList.add("hidden"); refresh(); };
$("#btnUnlock").onclick = async () => { try { const r = await api("/api/batch/unlock", {body: {by: reviewer()}}); toast(`Unlocked ${r.unlocked} setups`); } catch (e) { toast(e.message, true); } refresh(); };
$("#btnScan").onclick = async () => { await api("/api/scan", {body: {}}); toast(window.DESK_CLOUD ? "Scan requested: Work PC picks it up within a minute" : "Scan started (about 3 minutes)"); };
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
    tick(); renderHero(); renderKpis(); renderChips(); renderList(false); renderAction();
  } catch (e) { toast("Cannot reach the desk API: " + e.message, true); }
}
(window.DESK_READY || Promise.resolve()).then(() => { refresh(); setInterval(() => { if (state.tab === "signals") refresh(); }, 15000); });
})();
