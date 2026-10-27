# APEX Desk (apex_desk/)

Human-in-the-loop signal desk. Each evening it scans the daily data, queues every breakout signal on a dashboard, and you
and your manager review the charts in the morning, mark each **Trade / Don't trade**, then press **Trade all confirmed**
before the cut-off. Only confirmed signals are executed, at the US open.

**Independent of the old system.** Nothing in `config/`, `prod/`, `core/`, `state/`, `scheduler.py` is modified: `apex_desk`
only *imports* the stage/indicator code and the MT5 order helpers (read-only reuse), reads the old scheduler's data cache
(read-only), and keeps its own database, logs, magic number and MT5 account. The old `scheduler.py` runs exactly as before.

## Daily timeline (London time, US standard/DST aligned weeks)
| When | What |
|---|---|
| 22:20 | Scan runs (17:20 ET, after the old cache refresh 16:45 and signals 17:05): finds the day's breakouts, tiers them, previews size/stop, queues them. Stage-9 exits for held tickers are queued. |
| Morning | Open the dashboard. Review each Tier A chart, set Trade / Don't trade, then **Trade all confirmed**. |
| **13:30** | **Cut-off** (60 min before the 14:30 open; auto-adjusts in the 1-2 week US/UK DST mismatch). Countdown on screen. Unreviewed signals are not traded. |
| 14:31 | Executor (09:31 ET) sends exits first, then the confirmed batch, deepest below EMA200 first. |

## Tiers and locked parameters (research/dislocation_study steps 08-11)
All three tiers are **tradable** after your manual review; tier decides the risk size. Tier C is info only.
| Tier | Setup | Rule | Risk | Backtest (3.5x ATR stop, no costs) | Count/day |
|---|---|---|---|---|---|
| **A Core breakout** | Stage 6/7 Donchian breakout | >= 25% below EMA200, Stage 2 within 40 bars | 1.0x (1% of equity) | +0.20R/trade, 36% win | ~1.3 |
| **B Extended breakout** | same breakout, more lenient | >= 15% below, Stage 2 within 60 bars (not A) | 0.5x | +0.06R/trade (marginal, use judgement) | ~2 |
| **W Warming up** | pre-breakout: **Near breakout** (within 3% of the 20d high, EMA10>EMA20), **EMA20 reclaim**, **EMA50 reclaim** | >= 25% below, Stage 2 within 60 bars, avg $ volume >= 500k | 0.5x | +0.16R to +0.23R/trade, ~35% win, positive in train/test/2026 | ~10-13 |
| C Info | every other breakout | greyed, not tradable | - | ~0 edge | - |

Expect roughly 13-16 tradable cards on an average day (up to 50+ on a busy one), sorted tier A first, deepest below EMA200 first; filter by tier or setup.
Warming-setup stats are trade-level (overlapping events not removed) and the liquidity floor is an untested sanity filter: treat W as a manual-conviction tier.
- **Stop:** entry - max(**3.5 x ATR14**, 0.5% of entry), native broker stop. Best in the 2x-3.5x band (+50.8% CAGR, -63% max DD, 38% win rate at A/3 per ticker, vs +37.9% / -71% at 3x).
- **Risk:** 1% of equity x tier multiplier, position value <= 20% of equity, max **75** positions, max **3** concurrent trades per ticker, gross <= 5x equity, price >= $2.
- **Exits:** Stage 9 (close < EMA10) at the next open, broker stop, 365-day time stop, same as the old system.
- Evidence: `research/dislocation_study/FINDINGS.md` and the Excel reports (`Tier_Ladder_Study.xlsx` for the tier table).

## Execution (reused from production)
`prod.execution.order_builder` / `order_executor` are called unchanged via `desk/brokers.py`: tick warm-up retries, lot-step
flooring, **volume clamped to the broker's volume_max** (a 5000-lot target on a 1000-lot-max symbol still executes 1000), native
stop attached, retcode handling, and the 5-minute `no_live_quote` retry window. Stops come from the live ask at the open.
Position records are keyed by ticket so a ticker can hold up to 3 trades at once.

**Quote retries (no quote, `null_result`)** - `.NYSE-24` symbols often have no quote right at the open, so the executor retries instead of failing:
| Phase | Interval | Until | Config (`conf/desk.yaml`) |
|---|---|---|---|
| 1 | 30 s | 5 min after the run starts | `no_quote_retry_interval_sec`, `no_quote_retry_window_sec` |
| 2 | 120 s | open + 30 min (15:00 London) | `no_quote_extended_interval_sec`, `no_quote_extended_minutes_after_open` |

After 15:00 London a signal with still no quote is marked `failed` (`no_live_quote (no quote until 30 min after the open)`) with an alert.
Fills during retries use the live ask at that moment for size, stop and risk. The desk never falls back to the end-of-day close.

**MT5 order comment (tier tag)** - entry: `DESK_<tier>-<signal id>`, e.g. `DESK_A-4567`; exit: `DESK_EXIT_<tier>-<signal id>`. The number is the desk's signal id (traceable to the History tab and `state/desk.db`, never reused), max 31 chars (MT5 limit). Visible in the MT5 history export Comment column. Trades placed before this change carry `DESK<id>`.

**Broker trade mode (close-only)** - the broker can set a symbol to Close only / Disabled / Short only; new entries then fail with retcode `10044 (Only position closing is allowed)` (seen on `CCB.NAS-24`, 26-10-2026). The desk reads each tradable setup's MT5 symbol trade mode after every scan, in every 30-min snapshot and again right before each order (`Executor.check_broker_modes`, stored in `signals.broker_mode`, synced to Supabase). Such cards show a red **BROKER: CLOSE ONLY** tag, the Trade button is disabled, `set_decision` / `force-queue` refuse, and the executor fails them fast as `broker_close_only` with no retry. Info-only and skipped setups are unaffected.

**Symbol resolution** - the symbol map (`config/mt5_symbol_map.yaml`) is static, so new listings (e.g. `BETR`) are missing from it. `MT5Broker.symbol_for` falls back to probing the broker for `<TICKER>.NAS-24`, `.NYSE-24`, `.NAS`, `.NYSE` (first hit wins, cached for the session, logged as `Symbol auto-resolved`). Only a ticker that exists under none of these shows `BROKER: NO SYMBOL`.

**KPI boxes** - *Confirmed trades* and *Projected gross* count every setup marked Trade that is still pending **or already queued** for this open (risk = sum of each card's risk, notional added to the current gross); executed/failed ones drop out. *Open in same ticker* on each card counts the open positions of that ticker (also in the cloud view).

**MT5 lock** - MT5's Python API is process-global, so `MT5_LOCK` (in `executor.py`) serialises the executor and the snapshot job.
While the executor retries (up to 30 min) the half-hourly snapshot is skipped ("Snapshot skipped" in the log); equity on the dashboard may lag. `null_result` triggers an automatic MT5 reconnect.

**Recovery** - `python -m desk requeue` puts failed signals (`null_result`, IPC, `no_live_quote`) back to `queued`; then `python -m desk execute --force` while the market is open.

## Setup (PowerShell, from `...\APEX-Stocks_algo-v2\apex_desk`)
```
pip install -r requirements.txt
copy .env.example .env      # fill DESK_MT5_* for the NEW demo account (not the old account)
notepad conf\desk.yaml      # mode: dry_run (default) or live
```
Run three things (two windows are enough):
```
.\scripts\start_dashboard.ps1     # http://127.0.0.1:8000
.\scripts\start_scheduler.ps1     # 17:20 ET scan, 09:31 ET execution, snapshots
.\scripts\scan_now.ps1            # optional manual scan; -Asof 2026-04-15 replays a past day
```
Test everything offline first: `python tests\smoke_test.py`.

## Modes
- `dry_run` (default): orders are simulated (real quotes if the DESK MT5 terminal is logged in, otherwise last close). Safe.
- `live`: real orders to the account in `.env`. Needs `MetaTrader5` and the DESK terminal path. Use only on the new **demo** account first.

## Dashboard
Countdown to cut-off (London) | KPI strip (reviewed, confirmed risk, projected gross, positions, equity) | one card per signal
with candles, EMA 10/20/50/200, Donchian 20, Bollinger and volume, signal marker and stop line, toggles per indicator |
Trade / Don't trade buttons + note | **Trade all confirmed** (summary + warnings, locked after confirm, **Unlock** until cut-off) |
tabs for open positions, history and a full audit log (who decided what, when). Charts are drawn with TradingView's open-source
Lightweight Charts from the same data the algo used, so what you see is exactly what produced the signal. An "Open in TradingView" button opens the live chart.

## Layout
`conf/desk.yaml` all settings | `desk/engine.py` scan + charts | `desk/rules.py` decisions, batch, cut-off | `desk/executor.py` + `brokers.py` MT5 path |
`desk/api.py` FastAPI | `desk/sync.py` Supabase relay | `web/cloud.js` Supabase adapter | `netlify.toml` | `desk/db.py` SQLite (`state/desk.db`) | `web/` dashboard | `docs/supabase_schema.sql` cloud schema | `tests/smoke_test.py`.

## Signals, Watchlist and risk override (v2)
- **Signals tab** = the latest scan batch. It stays on screen until the next scan (about 22:20 London) produces newer signals, so the board is never empty.
- **Watchlist tab** = every older, unexecuted A/B/W setup (and any starred info setup), grouped by signal date and tagged `Skipped <date>`, `Marked trade <date>, not queued` or `Not reviewed`.
  Each session rollover moves them to the new session, resets the decision and remembers the last one. Candles, indicators, close, ATR, stop and size refresh after every daily scan
  (cache is end-of-day data). They have the same Trade / Don't trade buttons: trade any of them at the next open, on any day. Rolling stops after `watchlist.max_age_days` (45); History keeps everything.
- **Star (Watch)** on an info-only (tier C) card makes it tradable with a default manual risk of 0.5%.
- **Risk override**: each tradable card has a "Risk per trade" box (default = 1% x tier multiple: A 1.0%, B/W/starred 0.5%). Edit it up to `risk.max_risk_pct_override` (2%); the size preview updates and the executor uses it. Highlighted amber when overridden, with a reset link. Enforced server-side in Supabase too.
- **Charts** match the TradingView set-up: EMA 10 red, 20 yellow, 50 purple, 200 white; Donchian(20) upper/lower blue (basis orange, optional), width 1.5 (snapped to whole device pixels by the chart library). Default on: EMAs + Donchian upper/lower. Optional toggle: algo breakout level (prior 20-day high). Donchian fill between the bands is not drawn.
- Cloud upgrade needs `docs/supabase_migration_v2.sql` (run before the Work PC runs the new code).

## Dashboard timers and look (v3)
- Three glass timer cards share one layout (title / countdown / date and time, London time, DD-MM-YYYY): **Decision window** (large), **Market open/close** and **Next signal batch** (small).
- The decision window runs only from the batch publication (17:20 ET scan, 22:20 London) to the 13:30 London cut-off. After the cut-off it locks (red padlock, ring closes, "REVIEW LOCKED") and reopens with the next batch. Decisions and batch actions are refused while locked.
- Colours: green (plenty of time) > amber (under 2 h) > red pulse (under 15 min) > red lock; market green while open, orange while closed; batch cyan. Slow candle strips animate behind each card.
- Market hours use the regular 09:30-16:00 ET session (early-close days are not modelled).
- All dates display as DD-MM-YYYY; titles and tab headers are upper case; fonts Space Grotesk, Inter, JetBrains Mono (Google Fonts, needs internet).

## Positions, Closed and History tabs (v4)
- **Positions** = open trades. **Closed** = finished trades (exit date, days held, exit price, reason, P&L $, R). A position moves from Positions to Closed when the Stage-9 / time-stop exit fills or MT5 closes it (stop hit); the executor's `reconcile` detects it at the next run (09:31 ET). Positions are never deleted.
- Sort order, newest first on all four tabs: Positions by entry date, Closed by exit date, History by signal date (each then ticker A-Z), Audit by time (then action A-Z). The tabs load the latest 100 audit / 300-500 history rows.
- Every row in Positions, Closed and History starts with a tier tag (A / B / W / C) in the tier colour (A green, B amber, W blue, C grey) and a matching coloured strip at the row start.
- Positions store the tier at entry (`positions.tier`, Supabase `desk_positions.tier`; run `docs/supabase_migration_v3.sql` once in the Supabase SQL editor before pulling on the Work PC).

## Cloud architecture (live)
```
Work PC (MT5 + data cache)                    Supabase (project apexstocks-desk)             Netlify (apexstocks-desk.netlify.app)
 scan 17:20 ET -> SQLite state/desk.db  --sync-->  desk_signals / desk_charts / desk_status  <--  web/ (static, supabase-js, login)
 executor 09:31 ET  <--decisions, batch--  RPC: desk_set_decision / confirm_batch / unlock_batch (cut-off enforced server-side)
```
- Local SQLite stays the trading source of truth. Supabase is the UI datastore and relay (`desk/sync.py`, every 60 s, service key, Work PC only).
- Browser uses the public anon key and signed-in users only. Tables are read-only via RLS; writes go through SECURITY DEFINER functions that refuse after `cutoff_at`.
- Decisions: last writer wins by `decided_at`. Batch queue/unlock: the cloud is authoritative until the open. Execution results flow back to the cloud.
- Charts upload for tiers A/B/W only. Pill shows OFFLINE if the Work PC has not synced for 10+ minutes.
- Netlify builds from GitHub (Base dir `apex_desk`, `netlify.toml`) with env vars `SUPABASE_URL` and `SUPABASE_ANON_KEY`; rebuilds only when `web/` changes.
- Full setup and test runbook: `docs/SETUP_CLOUD.md`.

## Environment (`apex_desk/.env`, never committed)
`DESK_MT5_LOGIN / PASSWORD / SERVER / TERMINAL_PATH` (new demo account, portable terminal e.g. `C:\MT5_DESK\terminal64.exe`) |
`DESK_SUPABASE_URL`, `DESK_SUPABASE_SERVICE_KEY` (secret, Work PC only) | `DESK_MODE=dry_run|live` and `DESK_SYNC_ENABLED=true`
override `conf/desk.yaml`, so `git pull` never conflicts | optional `TELEGRAM_*` (leave blank to keep alerts apart from the old system).

## Work PC runbook (runs beside the old scheduler, which is never touched)
1. `cd ...\APEX-Stocks-Algo_v2` then `git pull origin main`; `cd apex_desk`; `pip install -r requirements.txt`.
2. Portable MT5 second terminal logged in to the new demo account; fill `.env`.
3. Tests: `python -m desk status` | `scan` (must report `stale_cache: false`; no `--allow-stale`) | `sync` | `snapshot` (MT5 block shows the NEW account).
4. Run `python -m desk scheduler` in its own window (scan 17:20 ET, execute 09:31 ET, snapshot 30 min, sync 60 s). Keep the PC awake.
   Scan resilience: a `scan_startup` job runs 20 s after the scheduler starts; if the cache is stale the scan retries every `scan_retry_min` (5) minutes for up to `scan_retry_max_hours` (15), then syncs.
   The old scheduler must refresh the cache first (16:45 ET, `tools/build_td_cache.py`); if the old scheduler is down the desk keeps retrying but the data stays stale. Manual fix: run `python tools\build_td_cache.py` in the old project, then `python -m desk scan`.
   After any `git pull`, restart the scheduler (Ctrl+C, then `python -m desk scheduler`).
   **Quiet by default (stale vs late)** - data is only considered stale when it is older than the newest trading day whose 17:20 ET scan time has passed (`clock.expected_signal_date`; on a Monday during market hours Friday's data is current, so nothing is flagged and no cache build starts). Stale scan retries are not written to the Audit tab and do not overwrite the last good batch. The dashboard banner, one audit row (`scan_late`) and one alert appear only when the batch is more than `timing.late_grace_min` (45) minutes overdue. A successful scan writes one audit row; errors always show.
   **Self-sufficient cache refresh (`desk/cachebuild.py`)** - the desk no longer depends on the old scheduler being up:
   - `job_cache` runs Mon-Fri at `timing.cache_time_et` (16:50 ET, 21:50 London; Friday's close included) and runs `tools/build_td_cache.py` from the old repo root (read-only reuse, same cache files). If the old scheduler's refresh is still running it waits (up to 45 min) and then tops up; fresh tickers cost zero credits.
   - Any stale scan (startup, 17:20 ET or a retry) triggers a build and an immediate rescan, at most once per 30 min (`cache_build.min_gap_min`). This covers a restart on Monday morning with Friday's data missing.
   - Each build is audit-logged (`cache_build`), a failure sends an alert. Optional `cache_build:` block in `conf/desk.yaml` overrides `enabled`, `script`, `min_gap_min`, `timeout_min`, `wait_external_min`.
5. Daily: review on Netlify before 13:30 London, press Trade all confirmed; paper (dry_run) or real demo orders at 09:31 ET.
6. Go `DESK_MODE=live` only after one clean dry-run day; restart the scheduler; watch the first fills.
Isolation: own SQLite DB, own magic number (20261001), own MT5 account, read-only use of the old price cache. Do not run a second scheduler on another PC (same account would double up orders).

## Test-only tools
- `python -m desk scan --as-session` relabels stale signals as today's session (never use on the Work PC). `--asof YYYY-MM-DD` replays a past date.
- `python -m desk execute --force` runs the executor outside the open (dry_run only).
- `python -m desk force-queue CCB [TICKER ...] [--risk 0.5]` MANUAL OVERRIDE: queues the ticker's latest signal for TODAY's open after the cut-off (info-only setups are starred automatically; `--risk` is % of equity, max 2). Refused on non-trading days or after the close. Audit-logged as `force_queue`. The scheduler's 09:31 ET run executes it; if the open has already passed, run `python -m desk execute` once.
- `python -m desk requeue` returns retryable failed signals to `queued` (see Execution).
- `DESK_FAKE_NOW=2026-10-23T09:00:00+00:00` freezes the server clock to test timers/lock states (PowerShell: `$env:DESK_FAKE_NOW=...`).
- `python -m desk sync [--loop]` one cloud cycle / continuous relay. `scripts/build_netlify.py <URL> <ANON_KEY>` builds a manual Netlify bundle.

## Known limitations
- Sizing preview uses the signal close; actual size/stop are recomputed from the live ask at the open.
- No circuit breaker (the old one is disabled too). Equity is shown from the executor's last snapshot.
- The dashboard needs internet only for the chart library (unpkg CDN); vendor the file into `web/` for fully offline use.
- MT5 dry-run and live demo fills verified (23-10-2026: 9 of 11 filled; the shared-connection clash that failed the rest is fixed by `MT5_LOCK`). The two-phase quote retry is verified with a simulated broker only; first real run is the next open.
- The cloud sync owns decisions and batch state: use the Netlify site for decisions while sync is on.
- The "review not open yet" lock is enforced by the UI and local API only; Supabase enforces the cut-off.
- Early-close days (half sessions) are not modelled in the timers or cut-off.
- Dashboard auto-refreshes every 15 s on all tabs (Signals, Watchlist, Positions, History, Audit).
- Unfilled orders show as `queued` during retries (no separate "waiting for quote" state).
- Gross cap 5x = total position value / equity; new trades are skipped once it would be exceeded.
