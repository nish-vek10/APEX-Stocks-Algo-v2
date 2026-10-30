# APEX Desk: handover (state at 27-10-2026, end of phase 1)

Purpose: paste or attach this in a new chat to continue. Phase 2 must not disturb anything listed here (see "Phase 2 guardrails").

## 1. What exists
Human-in-the-loop signal desk for the APEX stock-breakout strategy. Nightly scan builds signals; reviewers mark Trade / Don't trade on a web dashboard before the cut-off; the executor places only confirmed trades at the US open on MT5 (IC Markets demo, account 53110911, symbols `.NAS/.NYSE/-24`). The old production system (`scheduler.py`, repo root) is untouched and keeps running in parallel for manual comparison.

| Piece | Where |
|---|---|
| Main PC (development) | `C:\Users\ravil\PycharmProjects\APEX-Stocks_algo-v2` (git, `apex_desk/` is the desk) |
| Work PC (runs the algo, data cache, MT5) | `C:\Users\AdminCC\PycharmProjects\APEX-Stocks-Algo_v2\apex_desk` |
| GitHub | `nish-vek10/APEX-Stocks-Algo-v2`, branch `main` |
| Supabase project | `apexstocks-desk` (UI datastore + relay, RLS, SECURITY DEFINER RPCs) |
| Netlify site | `apexstocks-desk.netlify.app` (static `web/`, base dir `apex_desk`, env `SUPABASE_URL`, `SUPABASE_ANON_KEY`; rebuilds only when `web/`, `scripts/build_netlify.py` or `netlify.toml` change) |
| Secrets | `apex_desk/.env` on the Work PC only (`DESK_MT5_*`, `DESK_SUPABASE_URL`, `DESK_SUPABASE_SERVICE_KEY`, `DESK_MODE=live`, `DESK_SYNC_ENABLED=true`). Never paste them in chat. |

Deploy flow: Main PC `git add apex_desk` / commit / `git push origin main`; Work PC `git pull origin main`, Ctrl+C the scheduler window, `python -m desk scheduler`. `web/config.js` must stay `window.DESK_CONFIG = {};` in git.

## 2. Daily timeline (America/New_York based; London shown for UTC+0 winter / UTC+1 summer-mismatch weeks)
| ET | Job |
|---|---|
| 16:50 | `job_cache`: runs `tools/build_td_cache.py` (waits for the old scheduler's refresh, tops up; Mon-Fri) |
| 17:20 | `job_scan`: scan, retry every 5 min while stale (max 15 h), stale scan triggers a cache build (max one per 30 min) |
| 17:20 to 09:30 -60 min | Review window (cut-off 12:30 London now; 13:30 London in summer) |
| 09:31 | `job_execute`: confirmed trades, retry for no-quote symbols (30 s for 5 min, then 120 s until open + 30 min) |
| every 30 min | snapshot (equity, reconcile broker-closed positions, broker trade-mode check) |
| every 60 s | sync to Supabase |

## 3. Strategy rules (locked, from research/dislocation_study steps 08-11)
Tiers: **A** core breakout (>=25% below EMA200, Stage 2 within 40 bars, risk x1), **B** extended breakout (>=15% below, within 60 bars, x0.5), **W** warming up (near-breakout, EMA20/EMA50 reclaim, >=25% below, avg $ volume >= 500k, x0.5), **C** info only (can be starred to trade at 0.5%). Stop = entry - max(3.5 x ATR14, 0.5%). Risk 1% x tier multiple, per-trade override up to 2%. Max 75 positions, max 3 per ticker, position value <= 20% of equity, gross <= 5x equity, **no minimum price** (changed 27-10; was $2, the research stats used the $2 floor). Broker volume cap: order sent at the cap, risks less. Exits: Stage 9 (close < EMA10) at the next open, native broker stop, 365-day time stop.

## 4. Dashboard (tabs)
Signals (latest batch) | Watchlist (older unexecuted setups, last decision tag, refreshed daily, rolls to each new session, 45 days) | **Executed** (filled + failed per session: newest first, executed before failed, A-Z; entry/stop/exit lines, shaded P&L vs entry) | Positions | Closed | History | Audit. All table tabs newest first then A-Z. Tier tag + coloured strip on Positions/Closed/History. Three glass timers (decision window with lock animation, market open/close, next batch). Red BROKER tag + disabled Trade button when every symbol variant is close-only/disabled. KPI boxes: confirmed trades/projected gross include queued trades. Banner only when a batch is >45 min overdue. 15 s polling re-renders only on change.

**Daily PnL (added 30-10-2026):** KPI card 7 + modal of all close-of-business days; Positions rows expand into per-day decks (close, running P&L). `desk/pnl.py` snapshots daily at 17:10 ET (also after each scan and at scheduler start), backfills history on first run, idempotent. Supabase `desk_pnl_daily` / `desk_pnl_pos` (`supabase_migration_v5.sql`). CLI `python -m desk pnl`; test `tests/pnl_test.py`.

## 5. Execution details
- MT5 comment: entry `DESK_<tier>-<signal id>` (e.g. `DESK_W-4567`), exit `DESK_EXIT_<tier>-<signal id>`. Magic number 20261001 (separate from old system).
- Symbol: map `config/mt5_symbol_map.yaml` first, auto-resolve unmapped tickers; per attempt tries all variants (`.NAS-24`, `.NYSE-24`, `.NAS`, `.NYSE`) and uses the first that is open to buy and has a live quote.
- `MT5_LOCK` serialises executor, snapshot and broker checks (MT5 python API is process-global).
- Observed: first fills come about 4.5 min after the open (`10018 market closed` padding in `prod/execution/order_executor.py`); some `.NYSE-24` feeds have no quotes at all; some symbols are close-only (BLDR, OI, OLN, VRRM, CCB seen).

## 6. CLI (run from `apex_desk/`)
`python -m desk scheduler | scan [--as-session --asof D --allow-stale] | execute [--force] | sync [--loop] | snapshot | status | requeue | force-queue TICKER [--risk PCT] | serve`.
`force-queue` is a manual override for missed windows (audit-logged, market hours only). `DESK_FAKE_NOW=<ISO UTC>` freezes the clock for tests. Smoke test: `python tests\smoke_test.py`.

## 7. Supabase migrations (run in the SQL editor, in order, all idempotent)
`docs/supabase_schema.sql` (base) then `supabase_migration_v2.sql` (3 blocks), `supabase_migration_v3.sql` (`desk_positions.tier`, `desk_signals.broker_mode`), `supabase_migration_v4.sql` (`desk_positions.sid`), `supabase_migration_v5.sql` (`desk_pnl_daily`, `desk_pnl_pos`). Confirm v4 was run before relying on the Executed tab in the cloud view.

## 8. Current state (27-10-2026)
Open on the demo account: TREE, FISV (27-10), HTZ (27-10, manual, 0.5%) plus 9 earlier positions (FLO, ROL, DKS, PRCT, PODD, NKE, Z, FIS, TREE from 23-10). KRMN, UTI, TPB did not fill (no quote on `.NYSE-24`; the variant fallback was added afterwards and is untested live). CCB blocked (close-only).

## 9. Known limitations / open items
1. Early-close days are not modelled (timers, cut-off).
2. Supabase RPCs enforce only the cut-off; "review not open yet" and broker close-only are UI + local API only.
3. Variant fallback, extended retry and close-only checks are tested with simulated brokers only; first real run is the next open with such symbols.
4. Penny stocks are now allowed (stats in the tier table were measured with the $2 floor): unvalidated.
5. Old system (not part of the desk): its live account had `10019 No money` / `10044` rejections; its 16:45 ET cache refresh was unreliable (window closed on 22-10 and 23-10). The desk now refreshes the cache itself.
6. Donchian fill between bands not drawn; chart line width snaps to whole device pixels.
7. Older, separate backlog (other repos/tasks): exposure/stop-risk caps in the old system, execution priority, housekeeping fixes (APH exclude, VCYT-style exit retry, RYZ stale re-fire, cost tracking), spider-gate backtest comparison, live-readiness report.

## 10. Phase 2 guardrails
- Do not edit `apex_desk/`, `config/`, `prod/`, `core/`, `scheduler.py` for phase 2 exploration. Work in a new folder (e.g. `research/phase2/`) or a new branch; read the desk DB (`apex_desk/state/desk.db`) and the price cache read-only.
- Any change that must touch the desk goes through a small, separately tested commit and the deploy flow above; keep the old scheduler running.
- Research outputs follow the usual standard: neat Excel metrics, README/summary suitable for managers and investors, script path as the first comment line.
- Keep the Work PC scheduler window running (PC awake, DESK MT5 terminal logged in) while researching.

## 11. Suggested opening message for the next chat
"Phase 2 of the APEX Desk project. Read `apex_desk/docs/HANDOVER.md` and `apex_desk/README.md` first. Do not change apex_desk or the old scheduler; work in a separate folder. Goal of phase 2: <your outline>. Start with research."
