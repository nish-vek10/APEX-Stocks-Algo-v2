// apex_desk/web/cloud.js
// Supabase adapter: replaces the local FastAPI with Supabase (auth + RLS reads + RPC writes). Loaded only when config.js sets SUPABASE_URL.
(() => {
"use strict";
const CFG = window.DESK_CONFIG || {};
if (!CFG.SUPABASE_URL || !CFG.SUPABASE_ANON_KEY || !window.supabase) return;
const sb = window.supabase.createClient(CFG.SUPABASE_URL, CFG.SUPABASE_ANON_KEY);
const TRAD = ["A", "B", "W"];
let session = null, cache = {status: null, signals: [], sessDate: null};
const ok = r => { if (r.error) throw new Error(r.error.message); return r.data; };

function loginGate() {
  return new Promise(resolve => {
    const box = document.createElement("div");
    box.style.cssText = "position:fixed;inset:0;z-index:99;display:grid;place-items:center;background:#0b0f19;font-family:Inter,system-ui,sans-serif";
    box.innerHTML = `<form style="width:320px;padding:28px;border:1px solid #232b40;border-radius:14px;background:#121829;color:#e6ebf5;display:grid;gap:12px">
      <h2 style="margin:0;font-size:18px">APEX Desk</h2><small style="color:#8b97b5">Sign in to review signals</small>
      <input id="lgE" type="email" placeholder="Email" required style="padding:10px;border-radius:8px;border:1px solid #2b3550;background:#0b0f19;color:#e6ebf5">
      <input id="lgP" type="password" placeholder="Password" required style="padding:10px;border-radius:8px;border:1px solid #2b3550;background:#0b0f19;color:#e6ebf5">
      <button style="padding:10px;border:0;border-radius:8px;background:#3b82f6;color:#fff;font-weight:600;cursor:pointer">Sign in</button><small id="lgM" style="color:#ff6b81"></small></form>`;
    document.body.appendChild(box);
    box.querySelector("form").onsubmit = async e => {
      e.preventDefault();
      const {error} = await sb.auth.signInWithPassword({email: box.querySelector("#lgE").value, password: box.querySelector("#lgP").value});
      if (error) { box.querySelector("#lgM").textContent = error.message; return; }
      box.remove(); resolve();
    };
  });
}

async function status() {
  const d = ok(await sb.from("desk_status").select("payload,updated_at").eq("id", 1).maybeSingle());
  if (!d) throw new Error("No data yet. Start the Work PC scheduler, or run: python -m desk sync --loop");
  const s = d.payload; s.now_utc = new Date().toISOString();
  s.synced_at = d.updated_at; s.stale_min = Math.round((Date.now() - new Date(d.updated_at)) / 60000);
  s.mode = (s.stale_min > 10 ? "OFFLINE " : "") + s.mode;
  return s;
}
function lock(r) {
  if (!TRAD.includes(r.tier) && !r.watch) return "Info only: star it to make it tradable";
  if (r.exec_status !== "none") return `Signal already ${r.exec_status}`;
  if (Date.now() >= new Date(r.cutoff_at)) return "Cut-off passed: decisions are closed for this session";
  if (cache.status && Date.now() < new Date(cache.status.review_open_utc)) return "Review is locked until the next signal batch is published";
  return "";
}
async function signals(sess) {
  const rows = ok(await sb.from("desk_signals").select("*").eq("entry_date", sess).order("tier").order("dist200").limit(5000));
  rows.forEach(r => { r.id = r.sid; r.open_same_ticker = 0; r.locked_reason = lock(r); });
  cache.signals = rows; cache.sessDate = sess; return rows;
}
function preview() {
  const st = cache.status || {}, ex = st.exposure || {}, eq = st.equity || 1;
  const sg = cache.signals.filter(s => (TRAD.includes(s.tier) || s.watch) && s.decision === "trade" && s.exec_status === "none");
  const risk = sg.reduce((a, s) => a + (s.est_risk || 0), 0), nt = sg.reduce((a, s) => a + (s.est_notional || 0), 0);
  const slots = (st.max_positions || 75) - (ex.open_positions || 0), w = [];
  if (sg.length > slots) w.push(`${sg.length} confirmed > ${slots} free slots: the deepest-below-EMA200 signals fill first, the rest are skipped.`);
  if (st.max_gross_x && ((ex.notional || 0) + nt) / eq > st.max_gross_x) w.push(`Projected gross exposure above the ${st.max_gross_x}x cap: the executor stops adding at the cap.`);
  return {count: sg.length, est_risk: risk, est_risk_pct: risk / eq * 100, est_notional: nt, est_gross_x: nt / eq, free_slots: slots, equity: eq, warnings: w, tickers: sg.map(s => s.ticker)};
}
function counts(rows) {
  const a = rows.filter(r => TRAD.includes(r.tier) || r.watch), n = f => a.filter(f).length, by = {};
  rows.forEach(r => by[r.tier] = (by[r.tier] || 0) + 1);
  return {tradable: a.length, by_tier: by, trade: n(r => r.decision === "trade"), skip: n(r => r.decision === "skip"), undecided: n(r => r.decision === "none"), queued: n(r => r.exec_status === "queued"), executed: n(r => r.exec_status === "executed")};
}
async function api(path, opt) {
  const body = opt && opt.body, u = new URL(path, "http://x"), p = u.pathname;
  if (p === "/api/status") { const s = await status(); cache.status = s; const rows = await signals(s.session_date); s.counts = counts(rows); s.batch = preview(); return s; }
  if (p === "/api/signals") return cache.sessDate === u.searchParams.get("session") ? cache.signals : signals(u.searchParams.get("session"));
  let m;
  if ((m = p.match(/^\/api\/chart\/(.+)$/))) { const d = ok(await sb.from("desk_charts").select("payload").eq("sid", u.searchParams.get("sid")).maybeSingle()); if (!d) throw new Error("Chart not synced (info-only tier or still uploading)"); return d.payload; }
  if ((m = p.match(/^\/api\/signals\/(.+)\/decision$/))) return ok(await sb.rpc("desk_set_decision", {p_sid: decodeURIComponent(m[1]), p_decision: body.decision, p_by: body.by, p_note: body.note || ""}));
  if ((m = p.match(/^\/api\/signals\/(.+)\/risk$/))) return ok(await sb.rpc("desk_set_risk", {p_sid: decodeURIComponent(m[1]), p_risk_pct: body.risk_pct, p_by: body.by}));
  if ((m = p.match(/^\/api\/signals\/(.+)\/watch$/))) return ok(await sb.rpc("desk_set_watch", {p_sid: decodeURIComponent(m[1]), p_by: body.by}));
  if (p === "/api/batch/preview") return preview();
  if (p === "/api/batch/confirm") return ok(await sb.rpc("desk_confirm_batch", {p_entry_date: cache.sessDate, p_by: body.by}));
  if (p === "/api/batch/unlock") return {unlocked: ok(await sb.rpc("desk_unlock_batch", {p_entry_date: cache.sessDate, p_by: body.by}))};
  if (p === "/api/scan") { ok(await sb.rpc("desk_request_scan", {p_by: "web"})); return {started: true}; }
  if (p === "/api/positions") { const st = u.searchParams.get("status") === "closed" ? "closed" : "open"; return ok(await sb.from("desk_positions").select("*").eq("status", st).order(st === "closed" ? "exit_date" : "entry_date", {ascending: false}).limit(500)); }
  if (p === "/api/history") return ok(await sb.from("desk_signals").select("signal_date,entry_date,ticker,tier,setup,decision,decided_by,exec_status,dist200,days_since_s2,watch,last_decision,last_decision_date,risk_pct").or("tier.in.(A,B,W),watch.eq.true").order("signal_date", {ascending: false}).limit(500));
  if (p === "/api/audit") return ok(await sb.from("desk_audit").select("*").order("id", {ascending: false}).limit(100));
  throw new Error("Unsupported in cloud mode: " + p);
}
window.DESK_CLOUD = {api};
window.DESK_READY = sb.auth.getSession().then(({data}) => { session = data.session; return session ? null : loginGate(); });
})();
