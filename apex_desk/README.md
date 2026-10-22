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
`desk/api.py` FastAPI | `desk/db.py` SQLite (`state/desk.db`) | `web/` dashboard | `docs/supabase_schema.sql` cloud schema | `tests/smoke_test.py`.

## Supabase + Netlify (next phase)
Executor and scanner stay on the Windows PC (they need MT5). Swap `desk/db.py` for a Postgres/Supabase adapter (schema in
`docs/supabase_schema.sql`), host `web/` on Netlify pointing at the API, and enforce the cut-off server-side (Edge Function or the
FastAPI service). Add Supabase Auth so only you and your manager can log in.

## Known limitations
- Sizing preview uses the signal close; actual size/stop are recomputed from the live ask at the open.
- No circuit breaker (the old one is disabled too). Equity is shown from the executor's last snapshot.
- The dashboard needs internet only for the chart library (unpkg CDN); vendor the file into `web/` for fully offline use.
- MT5 order path is reused unchanged but could not be exercised from the build sandbox: run `dry_run` with the DESK terminal logged in, then a 1-signal live test on the demo.
