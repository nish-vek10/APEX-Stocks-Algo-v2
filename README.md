# APEX — Production Algo System

**Long-only breakout US equity strategy — exact production replication of the ALGO-Stocks backtest (PF 2.26, E[R] 0.63).**
Broker-agnostic execution: IG Group (default, live) and MT5 (IC Markets demo, in testing).

> This file is the operational blueprint — every script, every path, every command, in the order you actually run them. (SYSTEM_GUIDE.md is deprecated as of 2026-08-13 — everything it covered, including Telegram alerts, now lives here.)

---

## Table of Contents

- [1. What This System Does](#1-what-this-system-does)
- [2. Architecture](#2-architecture)
- [3. Data Sources — What Feeds What](#3-data-sources--what-feeds-what)
- [4. First-Time Setup](#4-first-time-setup)
- [5. Daily Operating Pipeline](#5-daily-operating-pipeline)
- [6. Script Reference — Every Tool, What It Does, When to Run It](#6-script-reference--every-tool-what-it-does-when-to-run-it)
- [7. Config File Reference](#7-config-file-reference)
- [8. Environment Variables (.env)](#8-environment-variables-env)
- [9. Broker Setup](#9-broker-setup)
  - [9.1 IG Group (default)](#91-ig-group-default)
  - [9.2 MT5 / IC Markets (legacy, in testing)](#92-mt5--ic-markets-legacy-in-testing)
- [10. Universe Construction](#10-universe-construction)
- [11. TwelveData Cache](#11-twelvedata-cache)
- [12. Strategy Logic Summary](#12-strategy-logic-summary)
- [13. Risk Modes](#13-risk-modes)
- [14. Spider Gate](#14-spider-gate)
- [15. Paper vs Live](#15-paper-vs-live)
- [16. State Files](#16-state-files)
- [17. Transferring This Project to Another Machine](#17-transferring-this-project-to-another-machine)
- [18. Known Gaps / Open Items](#18-known-gaps--open-items)
- [19. Telegram Alert Reference — Every Message Type, When It Fires, Examples](#19-telegram-alert-reference--every-message-type-when-it-fires-examples)

---

## 1. What This System Does

Scans a ~2,900-ticker US equity universe daily, detects Stage 7 ("Breakout Confirmed") signals using the exact stage-classifier logic from the ALGO-Stocks backtest, sizes positions via a configurable risk model, and executes next-day through either IG Group or MT5. Everything upstream of execution (data, indicators, stage classification, signal rules) is broker-agnostic — swapping `broker: "ig"` for `broker: "mt5"` in `production.yaml` changes nothing about how signals are generated.

---

## 2. Architecture

```
APEX-Stocks_algo-v2/
|-- config/                          # All configuration (YAML)
|   |-- production.yaml              # broker, environment, universe (2,910 tickers), entry/exit, portfolio caps
|   |-- risk.yaml                    # Active risk mode + all 5 sizing modes
|   |-- indicators.yaml              # EMA, BB, Donchian, ATR, Volume, MACD, RSI params
|   |-- stages.yaml                  # Stage 1-9 classifier thresholds
|   |-- spiders.yaml                 # 10 sector spider gate configs (currently disabled)
|   |-- ig_epic_map.yaml             # Ticker -> IG epic mapping (102 mapped)
|   `-- mt5_symbol_map.yaml          # Ticker -> MT5 broker symbol mapping (IC Markets, in progress)
|
|-- core/                            # Strategy logic (broker-independent, never touch per-broker)
|   |-- features/technicals/pipeline.py   # apply_indicators(): all indicators in one pass
|   |-- stages/stage_classifier.py        # Stages 1-9 state machine (exact backtest replica)
|   |-- filters/spider_gate.py            # SpiderGate: macro permission layer
|   `-- utils/
|       |-- config_loader.py         # load_*_config(), resolve_ig_credentials(), resolve_mt5_credentials()
|       `-- logging.py               # setup_logger()
|
|-- prod/                            # Production execution layer
|   |-- orchestrator.py              # APEXOrchestrator — master broker-dispatching coordinator
|   |-- data/
|   |   |-- twelvedata_fetcher.py    # PRIMARY data source (cache-based + live fallback)
|   |   |-- ig_fetcher.py            # yfinance — emergency fallback only if TWELVEDATA_API_KEY unset
|   |   |-- mt5_fetcher.py           # MT5 live data (legacy path, used only if broker=mt5 data path selected)
|   |   `-- universe.py              # build_epic_map(), build_symbol_map(), get_ticker_sector(), etc.
|   |-- execution/
|   |   |-- ig_connector.py          # IGConnector: IG REST session lifecycle
|   |   |-- ig_order_builder.py      # build_entry_request_ig(), build_close_request_ig()
|   |   |-- ig_order_executor.py     # send_order_ig() (paper guard + confirm polling)
|   |   |-- mt5_connector.py         # MT5Connector: MT5 terminal session lifecycle
|   |   |-- order_builder.py         # build_entry_request(), build_close_request() (MT5)
|   |   `-- order_executor.py        # send_order() MT5 with retry
|   |-- signals/signal_generator.py  # SignalGenerator: EOD Stage 7 detection
|   |-- risk/
|   |   |-- position_sizer.py        # compute_position_size(): all 5 sizing modes
|   |   `-- circuit_breaker.py       # CircuitBreaker: daily/weekly DD + streak halt
|   |-- positions/
|   |   |-- position_manager.py      # PositionManager: open/close/track
|   |   `-- portfolio_tracker.py     # PortfolioTracker: trade log + R-multiple
|   |-- state/state_manager.py       # StateManager: run state persistence
|   `-- monitoring/
|       |-- logger.py                # RunLogger: structured JSONL event log
|       `-- alert.py                 # send_alert(): Telegram
|
|-- tools/                           # Standalone operational scripts (see §6 for full table)
|-- state/                           # Runtime state (gitignored, see §16)
|-- data/raw/prices_daily/twelvedata/  # TwelveData OHLCV cache (gitignored, see §11)
|-- logs/                            # Run logs (auto-created)
|-- .env                             # Credentials (never commit)
`-- run_prod.py                      # Main entry point
```

---

## 3. Data Sources — What Feeds What

This is the part most likely to silently drift from the backtest if touched carelessly. Current wiring:

| Purpose | Source | Script | Notes |
|---|---|---|---|
| Universe construction (which 2,910 tickers) | Finviz Elite | `tools/build_scan_universe.py` | Exact replica of ALGO-Stocks `04_apply_universe_filters.py` — USA + cap≥$300M + REIT exclusion (4-rule engine) |
| Historical OHLCV for signal generation | TwelveData (cached) | `tools/build_td_cache.py` → `prod/data/twelvedata_fetcher.py` | Exact backtest data source parity. Falls back to live TwelveData per-ticker on cache miss, then yfinance only if `TWELVEDATA_API_KEY` unset entirely |
| Live bid/ask at execution time | Broker (IG or MT5) | `ig_connector.py` / MT5 terminal | Never used for signal generation, only for fill price + stop recompute |
| Order execution | Broker (IG or MT5) | `ig_order_executor.py` / `order_executor.py` | Only fires on tickers with a broker symbol mapped (`ig_epic_map.yaml` / `mt5_symbol_map.yaml`) |

**Why this matters:** the whole point of this system is exact backtest replication. If historical OHLCV ever silently falls back to yfinance (e.g. `TWELVEDATA_API_KEY` missing), signals will deviate from backtest — `orchestrator.py` logs a warning when this happens, watch for it.

---

## 4. First-Time Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in: IG_*, TWELVEDATA_API_KEY, FINVIZ_EXPORT_URL, TELEGRAM_* (see §8)
```

Then run the pipeline in order — see [§5](#5-daily-operating-pipeline).

---

## 5. Daily Operating Pipeline

This is the actual sequence, start to finish, with exact commands.

### One-time / periodic (universe + cache refresh)

```bash
# 1. Rebuild the scan universe from Finviz (dry run first, always review the diff)
python tools/build_scan_universe.py
python tools/build_scan_universe.py --apply

# 2. Refresh the TwelveData OHLCV cache (~6hrs for ~2,900 tickers at 8 credits/min —
#    resumable, safe to leave running, run right after EOD close)
python tools/build_td_cache.py
```

### Daily (after cache is current)

```bash
# 3. EOD signal generation (after US market close, 17:05 ET)
python run_prod.py --mode signals

# 4. AM execution (after US market open, 09:31 ET next day)
python run_prod.py --mode execution

# Anytime: check status
python run_prod.py --mode status
```

**Scheduling is NYSE-local, DST-aware.** `scheduler.py`'s cron jobs run against an `America/New_York` (not UTC) `BlockingScheduler`, so 17:05 ET / 09:31 ET stay fixed to those NYSE-local clock times year-round — no manual UTC math, and no risk of drifting when US and UK daylight saving shift on different dates (US DST ends first Sunday of Nov, UK BST ends last Sunday of Oct). `config/production.yaml`'s `scheduling:` block is documentation only; the actual trigger lives in `scheduler.py` (env-overridable via `APEX_SIGNAL_HOUR/MIN`, `APEX_EXEC_HOUR/MIN`, all ET).

**NYSE market-hours safety guard (added 2026-08-13).** `_is_nyse_regular_session()` in `prod/orchestrator.py` independently checks `America/New_York` local time (09:30–16:00 ET, Mon–Fri, DST-safe via `zoneinfo`) before allowing any new entries in `run_execution()` — a backstop behind `scheduler.py`'s cron in case of a misfire or a late catch-up run outside regular hours. It does **not** yet account for NYSE market holidays (e.g. Thanksgiving, Christmas) — see §18. Exits are never gated by this check; closing risk should never be delayed by a session-hours guard.

### Testing / debugging

```bash
python tools/debug_stages.py      # shows current stage + indicator values for all tickers, no orders
python run_prod.py --mode full    # signals + execution back-to-back, for testing only
```

---

## 6. Script Reference — Every Tool, What It Does, When to Run It

### `tools/` — standalone operational scripts

| Script | Purpose | When to run |
|---|---|---|
| `build_scan_universe.py` | Rebuilds the ~2,910-ticker universe from Finviz Elite (USA, cap≥$300M, REIT-excluded, exact backtest rule engine). Full re-derive, not merge. Writes `config/production.yaml` | Periodically (weekly/monthly) to keep universe current with cap/listing changes. `--apply` to write |
| `build_td_cache.py` | Builds/refreshes local TwelveData parquet cache for the whole universe. Resumable, skips fresh tickers | After every `build_scan_universe.py --apply`, and daily before signal generation if running live |
| `debug_stages.py` | Prints current stage + indicator values for every ticker, no state writes, no orders | Any time — safe sanity check |
| `ig_epic_inspector.py` | Discovers correct IG epic strings for your account/region. `--search TICKER`, `--validate-all`, `--list-open-positions` | Once per broker account setup, or when adding new tickers to `ig_epic_map.yaml` |
| `mt5_symbol_inspector.py` | Connects to a live MT5 terminal, discovers broker symbol names/suffixes/contract specs/filling modes by guessing suffixes against OUR ticker list. `--from-universe` runs it against the full 2,910-ticker list instead of the small built-in sample | Quick one-off check. For the authoritative mapping workflow use the two tools below instead |
| `mt5_full_stock_catalogue.py` | Connects to MT5, pulls IC Markets' ENTIRE symbol universe (no guessing), filters to stock CFDs, exports the ground-truth catalogue | Needs a live MT5 connection. Run once, then periodically (e.g. monthly) to catch new/removed broker symbols |
| `build_mt5_symbol_map.py` | Maps `production.yaml`'s current universe against the cached catalogue CSV, writes `config/mt5_symbol_map.yaml`. Pure local file matching, no MT5 connection needed | Run after every `build_scan_universe.py --apply` — instant, no need to reconnect to MT5 |
| `export_epic_catalogue.py` | Dumps full IG market details for every epic in `ig_epic_map.yaml` to CSV | Occasional audit of epic mapping |
| `export_full_universe.py` | Discovers all undated 24h cash CFD shares on an IG account via systematic search sweeps | One-off broker discovery, IG DEMO |
| `export_ig_all_epics.py` | Pulls every searchable instrument on an IG account, no filtering, all asset classes | One-off full catalogue pull, IG DEMO |
| `export_ig_shares_live.py` | Same as above but SHARES-only, against the LIVE IG account (`IG_LIVE_*` credentials) | One-off, IG LIVE only |
| `debug_ig_nav.py` | Diagnostic — inspects raw IG navigation API response | Debugging IG connectivity only |

### `prod/` — production runtime (imported by `run_prod.py`, not run directly)

| Module | Role |
|---|---|
| `orchestrator.py` | `APEXOrchestrator` — coordinates data fetch → signals → gate → sizing → execution → position management. Broker selected from `production.yaml` |
| `data/twelvedata_fetcher.py` | `fetch_universe_from_cache()` (primary path), `fetch_universe_twelvedata()` (live), `fetch_ticker_twelvedata()` |
| `data/ig_fetcher.py` | yfinance fallback (`fetch_universe_ig()`), IG live bid/ask (`get_live_bid_ask()`) |
| `data/mt5_fetcher.py` | MT5 live OHLCV + bid/ask (legacy data path if MT5 selected for data too) |
| `data/universe.py` | Ticker↔broker-symbol mapping helpers, sector lookup |
| `execution/ig_*.py` | IG session, order building, order execution |
| `execution/mt5_connector.py`, `order_builder.py`, `order_executor.py` | MT5 session, order building, order execution |
| `signals/signal_generator.py` | Stage 7 detection, exact backtest signal rules |
| `risk/position_sizer.py`, `circuit_breaker.py` | Sizing math, drawdown/streak halts |
| `positions/position_manager.py`, `portfolio_tracker.py` | Open/close tracking, trade log, R-multiples |
| `state/state_manager.py` | Pending signal persistence |
| `monitoring/logger.py`, `alert.py` | JSONL event log, Telegram alerts |

### Top level

| Script | Purpose |
|---|---|
| `run_prod.py` | Main entry point. `--mode signals\|execution\|full\|status` |
| `scheduler.py` | Cron-style scheduler wrapper for `run_prod.py` (EOD/AM automation) |

---

## 7. Config File Reference

| File | Controls |
|---|---|
| `config/production.yaml` | `environment` (paper/live), `broker` (ig/mt5), universe ticker list + lookback, entry/exit rules, stop config, portfolio caps, circuit breakers, spider gate toggle, scheduling times |
| `config/risk.yaml` | Active sizing mode + params for all 5 modes, gate risk multipliers |
| `config/indicators.yaml` | EMA/BB/Donchian/ATR/Volume/MACD/RSI parameters |
| `config/stages.yaml` | Stage 1-9 classifier thresholds |
| `config/spiders.yaml` | 10 sector spider gate composite configs |
| `config/ig_epic_map.yaml` | Ticker → IG epic string (102 currently mapped — tickers without an entry generate signals but never execute) |
| `config/mt5_symbol_map.yaml` | Ticker → MT5 broker symbol string (populate via `mt5_symbol_inspector.py`) |

**Note on `production.yaml`:** this file previously had a duplicated `entry`/`exit`/`stop`/`spider_gate` block (a copy-paste leftover). YAML silently keeps the *last* occurrence of a duplicate key, so it was quietly running on the second block's values without erroring. This has been cleaned up — there is now exactly one instance of each key. If editing this file, verify there's still only one of each top-level key after saving:
```bash
python -c "import yaml; yaml.safe_load(open('config/production.yaml'))"
```

---

## 8. Environment Variables (.env)

```bash
# IG Group — DEMO (default broker)
IG_IDENTIFIER=
IG_PASSWORD=
IG_API_KEY=
IG_ACCOUNT_ID=
IG_ACC_TYPE=DEMO                # DEMO | LIVE

# IG Group — LIVE (manager's account, used only by export_ig_shares_live.py)
IG_LIVE_IDENTIFIER=
IG_LIVE_PASSWORD=
IG_LIVE_API_KEY=
IG_LIVE_ACCOUNT_ID=

# MT5 (only needed if broker: "mt5" in production.yaml)
MT5_LOGIN=
MT5_PASSWORD=
MT5_SERVER=                     # e.g. ICMarketsSC-Demo
MT5_TERMINAL_PATH=              # path to terminal64.exe, only needed for a non-default install location

# Finviz Elite — universe construction
FINVIZ_EXPORT_URL=https://elite.finviz.com/export/screener?v=111&auth=YOUR_TOKEN
FINVIZ_EXPORT_TAG=v111_all
HTTP_TIMEOUT=60

# TwelveData — OHLCV data (exact backtest parity source)
TWELVEDATA_API_KEY=
TD_INTERVAL=1day
TD_TIMEZONE=UTC
TD_CREDITS_PER_MIN=8            # your plan's rate limit — drives cache build time
TD_BATCH_SIZE=8
TD_OUTPUTSIZE=5000
TD_MIN_ROWS_OK=950               # coverage gate — below this, ticker marked ok_short_history
TD_START_DATE=                   # backtest-only, unused by build_td_cache.py (rolling window instead)
TD_END_DATE=
TD_EXPECTED_LAST_DATE=
TD_SMOKE_N=0
TD_SMOKE_TICKERS=
TD_RETRY_SLEEP_SEC=1.0

# Telegram alerts
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Never commit `.env` — it's in `.gitignore`.

---

## 9. Broker Setup

### 9.1 IG Group (default)

1. IG → My IG → Manage account → API keys → Create API key. Note identifier, password, API key, account ID.
2. Set `IG_*` vars in `.env`.
3. Discover epics: `python tools/ig_epic_inspector.py --search AAPL` or `--validate-all`. Copy correct epics into `config/ig_epic_map.yaml`.
4. `production.yaml`: `broker: "ig"`, `ig.acc_type: "DEMO"` until validated.

IG epic format (UK/EU CFD accounts): `CS.D.AAPL.CFD.IP` (daily funded) or `CS.D.AAPL.CASH.IP` (undated/rolling DFB).

**IG mode uses:** TwelveData cache for historical OHLCV (§3), IG REST API for live bid/ask at execution time only, IG OTC positions for entries/exits.

### 9.2 MT5 / IC Markets (legacy, in testing)

Verified against the live IC Markets demo (`ICMarketsSC-Demo`) on 2026-08-13 via the catalogue-based workflow below (`mt5_full_stock_catalogue.py` + `build_mt5_symbol_map.py`): **2,550 / 2,910 universe tickers mapped** (87.6%) against an IC Markets catalogue of 4,525 unique base tickers — meaningfully better than IG's 102/2,910. 360 tickers remain unmapped (scan-only). Full results: `tools/output/symbol_specs_latest.xlsx`.

**Regex fix (2026-08-13):** the original catalogue/mapping scripts used a regex that failed to parse IC Markets' `-24` suffixed symbols (24/5 extended-hours variants, e.g. `WDC.NAS-24`), silently dropping `base_ticker` for 5,417/7,275 catalogue rows (74%) and undercounting coverage as 1,106/2,910 (38%), all IOC filling. Fixed via a non-greedy base-ticker capture with an optional trailing `-24` group in both `tools/mt5_full_stock_catalogue.py` and `tools/build_mt5_symbol_map.py`; the latter now re-derives `base_ticker` directly from `mt5_symbol` rather than trusting the CSV's precomputed column, and ranks `-24` variants below standard-hours listings when both exist for a ticker.

Of the 2,550 mapped tickers, 1,103 map to a standard-hours listing and **1,447 map only via the `-24` extended-hours (24/5) variant** — i.e. for those 1,447 tickers IC Markets does not offer a standard-hours stock CFD, only the 24/5 extended-hours one. Worth confirming spread/liquidity characteristics for these with IC Markets before relying on them for live execution — extended-hours instruments can behave differently (wider spreads, lower liquidity) than standard listings.

Everything not found remains scan-only (signals generate, no execution) — same design as IG. GOOGL (Alphabet Class A) and EA (Electronic Arts) are confirmed genuinely absent from IC Markets' catalogue via exhaustive description-text search, not a mapping artifact — IC Markets only offers GOOG (Alphabet Class C), a different share class, in place of GOOGL.

**Recommended workflow (catalogue-based, not suffix-guessing):**

1. Open MT5 terminal, log in to the IC Markets demo account (custom install path supported via `MT5_TERMINAL_PATH` in `.env`).
2. Set `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `MT5_TERMINAL_PATH` in `.env`.
3. Pull IC Markets' full stock CFD catalogue (needs live MT5 connection, ground-truth — not guessed suffixes):
   ```bash
   python tools/mt5_full_stock_catalogue.py
   ```
   Outputs to `tools/output/`: `mt5_stock_catalogue_latest.csv/.xlsx` (every stock CFD IC Markets offers), `mt5_path_breakdown_latest.csv` (diagnostic — symbol counts by broker category, to sanity-check the stock-CFD filter caught everything).
4. Map the current universe against that catalogue (pure local matching, no MT5 connection):
   ```bash
   python tools/build_mt5_symbol_map.py --apply
   ```
   Re-run this step (not step 3) after every `build_scan_universe.py --apply` — it's instant. Only re-run step 3 when IC Markets' own symbol offering may have changed.
5. `production.yaml`: `broker: "mt5"` when ready to switch.

Earlier suffix-guessing run (`mt5_symbol_inspector.py --from-universe`) found 1,106/2,910 against our own ticker list — the catalogue-based approach above beats that decisively (2,550/2,910), since it doesn't depend on guessing the right suffix pattern per ticker and, after the regex fix above, correctly picks up `-24` extended-hours variants that the suffix guesser and the buggy catalogue parser both missed.

**MT5 data source fixed (2026-08-13).** `_fetch_data_mt5()` in `orchestrator.py` previously pulled historical OHLCV live from the MT5 terminal for signal/indicator calculation — a backtest-parity deviation flagged in earlier revisions of this README. It now calls the same `fetch_universe_from_cache()` TwelveData path used by IG mode (§3). **MT5 itself is now used only for:** live tick price at order time, native broker-side SL on the order, and lot-size resolution — never for signal generation. This closes the gap previously documented here and in §18.

**MT5 lot-sizing fix (2026-08-13).** Position sizing (`prod/risk/position_sizer.py`) computes a broker-agnostic "shares" figure (dollars-at-risk ÷ stop-distance) that's correct as-is for IG (1 unit = 1 share). For MT5 this was previously passed straight through as the raw order `volume` with zero regard for the broker's actual `contract_size` / `volume_step` / `volume_min` per symbol — meaning real risk-per-trade could silently diverge from the configured 1% target by an arbitrary amount. Fixed via a new `resolve_mt5_volume()` in `prod/execution/order_builder.py` that queries `mt5.symbol_info()` live, converts target shares to a valid lot size (floored to `volume_step`, clamped to `volume_min`/`volume_max`, contract-size-aware), skips the trade if it rounds below `volume_min`, and logs a warning if lot-step rounding pushes realized risk more than 15% away from the 1% target. `PositionManager.open_position()` gained an `mt5_volume` field so the raw broker lot size (needed only for the order itself) is tracked separately from `actual_shares` (volume × contract_size, used for correct $ P&L math) — see §16.

**Not yet done:** MT5 execution — including this lot-sizing fix and the Stage 9 exit logic (§12) — has not been run end-to-end against a live MT5 session. IG DEMO remains the only broker path validated for order placement/tracking. Validate the corrected lot-sizing math and Stage 9 exit queuing on MT5 DEMO before going live on `broker: "mt5"`.

---

## 10. Universe Construction

Full detail in [§3](#3-data-sources--what-feeds-what) and [§6](#6-script-reference--every-tool-what-it-does-when-to-run-it). Short version: `tools/build_scan_universe.py` pulls Finviz Elite's full USA export, filters to market cap ≥ $300M, excludes REITs via the same 4-rule engine as the ALGO-Stocks backtest (`sector == "Real Estate"`; industry contains `"REIT"` / `"REIT -"` / `"Real Estate"`), and fully re-derives the universe each run (not an incremental merge with whatever was there before). Expect ~2,800-2,900 tickers — this replaced a stale 528-ticker legacy list that predated exact backtest-parity work.

Execution only ever fires on tickers with a broker symbol mapped (`ig_epic_map.yaml` / `mt5_symbol_map.yaml`). Everything else generates signals and logs them, by design, but never places an order.

---

## 11. TwelveData Cache

`tools/build_td_cache.py` builds a local parquet cache at:
```
data/raw/prices_daily/twelvedata/parquets/{TICKER}.parquet
data/raw/prices_daily/twelvedata/meta/{TICKER}.meta.json
```
Same batching/rate-limit method as the ALGO-Stocks backtest fetch script, adapted for a rolling window (`outputsize`, always latest N days) instead of a frozen historical date range — production needs current data every day, not a backtest snapshot.

**Rate limit reality:** at `TD_CREDITS_PER_MIN=8` and ~2,900 tickers, a full refresh takes ~6 hours. This is a TwelveData plan limit, not fixable in code. Run it right after EOD close (`scheduling.signal_time_et: 17:05 ET`) — there's a 16h15m gap before next-day execution (`09:31 ET`), comfortably fits. Both times are NYSE-local and DST-aware (see Daily Operating Pipeline above). Upgrading the TwelveData plan is the only way to shorten this. It's resumable and idempotent — safe to interrupt and rerun; already-fresh tickers are skipped at zero credit cost.

Smoke test before a full run:
```bash
python tools/build_td_cache.py --tickers AAPL,MSFT,NVDA
```

---

## 12. Strategy Logic Summary

### Signal Generation (EOD)
1. Fetch D1 OHLCV (TwelveData cache, `lookback_days` bars per ticker — default 300)
2. Apply indicators: EMA 10/20/50/100/200, BB(20, 2σ), Donchian(20), ATR(14), Volume(20), MACD, RSI
3. Classify each bar into Stage 1-9 (expanding-window state machine)
4. **Stage 2 Dislocation** prerequisite: once seen, never resets (`state/stage2_memory.json`)
5. Signal fires on **Stage 7 (Breakout Confirmed)** detection, with Stage 2 present in history
6. Persisted to `state/run_state.json` for next-day execution

| Stage | Name | Primary Condition |
|---|---|---|
| 1 | Downtrend | EMA10 < EMA20 < EMA50 |
| 2 | Dislocation | Price ≤ BB lower OR price ≤ EMA50 × 0.98 |
| 3 | Accumulation | Volume surge > 1.15× |
| 4 | Base Building | Price near Donchian upper |
| 5 | Pre-Breakout | EMA10 > EMA20 |
| 6 | Breakout Attempt | Price ≥ Donchian upper + volume surge |
| 7 | **Breakout Confirmed** | EMA stack + volume surge + close > Donchian |
| 8 | Extension | Price near BB upper |
| 9 | Exhaustion | RSI ≥ 70 + volume climax |

### Execution (Next-Day AM)
1. NYSE market-hours guard (`_is_nyse_regular_session()`, 09:30–16:00 ET Mon–Fri) — blocks new entries outside regular session; see §5. No holiday calendar yet (§18)
2. `max_open_positions` cap — hard-enforced, blocks new entries once hit (see §13/§18)
3. Spider gate check (currently disabled — see §14)
4. Circuit breaker check — halts if daily DD > 3%, weekly DD > 7%, or 5 consecutive losses
5. Entry at live broker ask price
6. Stop: `entry_price - ATR(14) × 2.0`, floor at 0.5% of entry
7. Position size from active risk mode × gate multiplier (MT5: converted to a broker-valid lot size, see §9.2/§13)

### Exit Hierarchy

Matches ALGO-Stocks `backtest/engine.py` priority order exactly: gap/stop-hit, then Stage 9 fade, then time stop.

1. **Stop hit / gap-protection** → close immediately. Enforced two ways: native broker-side SL sent with every order, plus a daily backstop poll in production for gap-through scenarios.
2. **Stage 9 (In-Zone Fading) detected** → exit next open. `_check_stage9_exits()` computes each open position's current stage every EOD signal run (reusing the already-fetched TwelveData universe, no extra fetch) and queues Stage-9 positions for exit at the next AM open — matching backtest's "signal at EOD close, exit at next open" timing. Implemented 2026-08-13; previously configured `true` in `production.yaml`'s `exit:` block but not actually implemented (a real backtest-parity gap, now closed — see §18).
3. **Time stop** — `time_stop_days: 365`, confirmed against the actual validated backtest snapshot (`universe_baseline_v1_20260224_2310`, PF 2.26 / E[R] 0.63), not the generic code fallback of 60 days (a red herring, ruled out 2026-08-13).

**No take-profit exists, by design.** The validated backtest has zero fixed take-profit logic anywhere — exits are stop-loss, Stage 9 fade, or time-stop only. MT5 orders intentionally send `tp: 0.0`. Confirmed directly against the ALGO-Stocks backtest engine before touching any exit code, specifically to avoid introducing new, unvalidated strategy behavior.

---

## 13. Risk Modes

Set `active_mode` in `config/risk.yaml`:

| Mode | Description | Key Param |
|---|---|---|
| `equity_pct` | Risk % of equity per trade | `risk_pct_per_trade: 0.01` |
| `fixed_dollar_risk` | Fixed $ risked per trade | `risk_dollars: 200` |
| `fixed_lots` | Fixed shares always | `lots: 10` |
| `atr_dynamic` | Volatility-adjusted size | `vol_target_pct: 0.01` |
| `volume_based` | Liquidity-aware sizing | `adv_participation_pct: 0.01` |

Gate multiplier always applied on top.

**MT5 lot sizing (fixed 2026-08-13).** All 5 modes above compute a broker-agnostic "shares" figure — correct as-is for IG, where 1 unit = 1 share. For MT5, that figure now goes through `resolve_mt5_volume()` (`prod/execution/order_builder.py`) before an order is sent: it queries `mt5.symbol_info()` for the symbol's live `contract_size`/`volume_step`/`volume_min`/`volume_max`, floors the target shares to a valid lot size, skips the trade if it rounds below `volume_min`, and logs a warning if lot-step rounding pushes realized risk more than 15% from the configured 1% target. Previously the raw shares figure was sent straight through as MT5 `volume` with no broker-symbol awareness, so real MT5 risk-per-trade could silently diverge from target — see §9.2 and §18.

---

## 14. Spider Gate

10 sector composites (XLK, XLF, XLV, XLE, XLY, XLP, XLI, XLB, XLU, XLRE) — macro permission layer that blocks/scales positions by sector health.

**Currently `enabled: false`** in `production.yaml` — the required input file (`data/cleaned/spiders_daily/gate/spider_gate_daily.parquet`) hasn't been built yet on this system. When disabled, all trades permitted regardless of macro state. Re-enable once that file exists and is validated — see [§18](#18-known-gaps--open-items).

---

## 15. Paper vs Live

**`environment: "paper"` is the safety guard.**
- Paper: logs all actions, simulates fills, never calls the broker's order endpoint
- Live: prompts for `CONFIRM` at startup before any orders placed

Broker demo account (IG `acc_type: DEMO` / MT5 demo login) is additional safety — real API calls but no real money.

Switch to live only after:
1. Paper mode validated (signals generating, sizing correct)
2. Broker DEMO validated (orders executing, positions tracking)
3. `environment: "live"` set, and broker set to LIVE credentials

---

## 16. State Files

All state in `state/` is auto-created on first run. **Gitignored — does not transfer via git clone, see §17.**

| File | Purpose | Reset safe? |
|---|---|---|
| `stage2_memory.json` | Stage 2 per-ticker history | **NEVER reset** — design locked, expanding window |
| `positions.json` | Open positions | Reset = lose position tracking |
| `trade_log.json` | Full trade history | Safe to archive |
| `circuit_breaker.json` | CB state + streak | Can reset after manual review |
| `run_state.json` | Pending signals/orders, incl. `pending_stage9_exits` | Safe to clear if stale (worst case: lose a queued Stage 9 exit or pending signal, regenerates next EOD run) |

**Position state note (IG mode):** `positions.json` uses `mt5_ticket`/`mt5_symbol` field names (legacy naming preserved). In IG mode these store `ig_deal_id`/`ig_epic`. Do not rename — breaks state file compatibility.

**New fields (2026-08-13):** `positions.json` entries for MT5 positions now carry `mt5_volume` (the raw broker lot size actually sent with the order, from `PositionManager.open_position()`) alongside `actual_shares` (`mt5_volume × contract_size`, used for correct $ P&L math) — the two are tracked separately because MT5's own `volume` field is lot-size, not shares. `run_state.json` gained `pending_stage9_exits`, the queue `_check_stage9_exits()` writes at EOD for positions to be closed at the next AM open (§12).

---

## 17. Transferring This Project to Another Machine

`git clone` gets code + tracked config only. The following are gitignored and must be handled separately:

| Item | Action |
|---|---|
| `.env` | Copy the file directly — never retype credentials by hand |
| `state/stage2_memory.json` | **Copy exactly** — expanding-window history, non-regenerable. Losing it means Stage 7 signals are wrong/missing until history rebuilds naturally over time |
| `state/run_state.json` | Optional — worst case, lose one pending signal, regenerates next EOD run |
| `data/raw/prices_daily/twelvedata/` | Don't transfer — rebuild fresh via `tools/build_td_cache.py` on the new machine |
| `data/cleaned/` | Don't transfer — currently empty pending spider gate build |

---

## 18. Known Gaps / Open Items

- **Spider gate not built.** `data/cleaned/spiders_daily/gate/spider_gate_daily.parquet` doesn't exist. Gate is disabled (`production.yaml`) until this is built and validated.
- **RESOLVED (2026-08-13): MT5 data path now wired to TwelveData cache.** *(Supersedes the "MT5 data path not yet wired to TwelveData cache" gap previously listed here.)* `orchestrator._fetch_data_mt5()` now calls `fetch_universe_from_cache()`, the same TwelveData path as IG mode, instead of pulling live MT5 bars. MT5 is now used only for live tick price at order time, native broker-side SL, and lot-size resolution — never for signal generation. See §9.2.
- **Fixed (2026-08-13): MT5 risk sizing bug.** Position sizing previously passed the broker-agnostic "shares" figure straight through as raw MT5 `volume` with no regard for the symbol's `contract_size`/`volume_step`/`volume_min`, so real risk-per-trade could silently diverge from the configured 1% target. New `resolve_mt5_volume()` (`prod/execution/order_builder.py`) converts to a valid, broker-aware lot size and warns if rounding pushes realized risk >15% from target. See §9.2/§13. Not yet validated against a live MT5 session.
- **Changed (2026-08-13): `max_open_positions` raised 5 → 100** in `config/production.yaml`, hard-enforced (blocks new entries once hit, not just a log warning) in `prod/orchestrator.py::run_execution()`.
- **Added (2026-08-13): NYSE market-hours safety guard.** `_is_nyse_regular_session()` in `orchestrator.py` blocks new entries outside 09:30–16:00 ET Mon–Fri as a backstop behind `scheduler.py`'s cron. **Does not yet account for NYSE market holidays** (e.g. Thanksgiving, Christmas half-days) — a misfire/catch-up run on a holiday could still be blocked incorrectly or, if the holiday check is added later, needs its own validation. See §5.
- **Implemented (2026-08-13): Stage 9 fade exit and gap-protection exit.** Both were configured `true` in `production.yaml`'s `exit:` block but not actually implemented in code — a real backtest-parity gap. `_check_stage9_exits()` now computes stage per open position every EOD run and queues Stage-9 exits for next open, matching backtest exit-priority ordering (gap/stop → Stage 9 → time stop). See §12.
- **Fixed (2026-08-13): Telegram alerting gaps.** `alert_order_sent()` was called with the wrong number of arguments (latent bug, would have raised `TypeError` on the first live order) — fixed. `alert_order_rejected()` existed but was never wired up — now fires on every failed entry/exit order with reason/retcode. `alert_position_closed()` existed but was never wired up — now fires on every position close with exit price, $ P&L, R-multiple, reason. `alert_signal_found()` was already working correctly and is unchanged.
- **MT5 execution untested end-to-end** against IC Markets demo — IG DEMO remains the only broker path currently validated for order placement/tracking. This now specifically includes validating the new lot-sizing fix (`resolve_mt5_volume()`) and Stage 9 exit queuing on MT5, not just basic order placement.
- **IC Markets symbol coverage verified (2026-08-13):** 2,550/2,910 mapped (87.6%), 360 unmapped (scan-only), `config/mt5_symbol_map.yaml` populated. Supersedes an earlier 1,106/2,910 (38%) figure that was wrong due to a regex bug in `mt5_full_stock_catalogue.py`/`build_mt5_symbol_map.py` that failed to parse IC Markets' `-24` extended-hours suffixed symbols, silently dropping `base_ticker` for 74% of catalogue rows — fixed, see §9.2.
- **1,447 of the 2,550 mapped tickers only have a `-24` (24/5 extended-hours) listing** at IC Markets, not a standard-hours one. Worth checking with IC Markets on spread/liquidity characteristics for these symbols before relying on them for live execution — extended-hours instruments can behave differently (wider spreads, lower liquidity) than standard listings.
- **EA and GOOGL confirmed genuinely unavailable at IC Markets** (not a mapping bug) — confirmed by exhaustive description-text search. GOOGL specifically because IC Markets only offers GOOG (Alphabet Class C), a different share class from GOOGL (Class A).
- **Fixed (2026-08-13): scheduler was on a hardcoded UTC offset, not NYSE local time.** `scheduler.py` previously fired at fixed UTC clock times (13:31/21:05 UTC) that only happened to line up with NYSE open/close because both US and UK were on summer DST — it would have drifted out of sync at each DST boundary and during the US/UK DST-mismatch weeks (US ends first Sun of Nov, UK ends last Sun of Oct). Now runs an `America/New_York`-timezone `BlockingScheduler` (09:31/17:05 ET), which auto-adjusts across DST with no code changes. See §5 and `scheduler.py` docstring.
- **`SYSTEM_GUIDE.md` deprecated (2026-08-13).** It described the old 20-ticker/yfinance/IG-only design and had drifted badly out of sync (wrong universe size, wrong data source, wrong spider_gate state). Replaced with a short pointer to this README; do not use it for current instructions.
- **Fixed (2026-08-13): `--reset-circuit-breaker` CLI flag didn't exist.** The circuit-breaker Telegram alert (§19) told operators to run `python run_prod.py --reset-circuit-breaker`, but `run_prod.py` had no such flag — it would have failed with an argparse error at the exact moment someone needed it most. Now implemented: clears the halt via `CircuitBreaker.reset_halt()` and exits (no signals/execution run).
- **Fixed (2026-08-13): two Telegram message accuracy bugs.** (1) `alert_signal_found()` hardcoded "(Breakout Confirmed)" for every signal regardless of actual stage — wrong for Stage 6 ("Breakout") signals. Now uses the signal's real `stage_name`. (2) `alert_order_rejected()` always said "no position opened" in its footer, which is backwards when it fires for a failed EXIT (the position is still open, not absent). Now takes an `is_exit` flag and shows the correct footer for each case. See §19.
- **Four alert functions exist in `alert.py` but are not currently called anywhere:** `alert_spider_gate_block`, `alert_run_summary`, `alert_startup`, `alert_connection_failed`. Spider gate is disabled so that one is moot for now, but `alert_connection_failed` in particular is a real blind spot — if MT5/IG login fails at startup, nothing is sent to Telegram (the failure only surfaces as a crashed process / local log entry, or a generic scheduler-level retry alert with no broker-specific detail). See §19 for what this means for current coverage.

---

## 19. Telegram Alert Reference — Every Message Type, When It Fires, Examples

All logic lives in `prod/monitoring/alert.py`. Every alert is written to the local JSONL log (`logs/run_YYYYMMDD.jsonl`, via `RunLogger` — see §16) regardless of Telegram delivery; Telegram is a second channel, not the source of truth. If `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` aren't set in `.env`, Telegram sends are silently skipped and only the local log is written. Setup: create a bot via **@BotFather**, start a chat with it, fetch `https://api.telegram.org/bot<TOKEN>/getUpdates` to get the chat ID, add both to `.env`.

Below: every message type, the exact event that triggers it, whether it's currently wired into the code (fires today) or defined-but-unused, and a realistic example with the exact Telegram text (HTML formatting as sent — `<b>bold</b>`, `<code>code</code>`).

### 🟢 Currently firing — you will receive these

**1. Signal found** — `alert_signal_found()` — fires once per ticker, every EOD signal run (`run_prod.py --mode signals`), for every fresh Stage 6/7 transition detected.

```
📡 APEX SIGNAL
Ticker:  NVDA
Stage:   7 (Breakout Confirmed)
Date:    2026-08-13
Action:  Execute next AM open
```

**2. Order sent (entry filled)** — `alert_order_sent()` — fires when a new position is successfully opened (execution run, broker confirmed fill).

```
✅ APEX ORDER SENT 🟢 LIVE
──────────────────────
Ticker:     NVDA  (NVDA.NAS)
Time:       2026-08-14 13:31:05 UTC
Entry:      $142.85
Size:       350 shares
Stop:       $138.20  ($4.65 below entry)
Risk $:     $1,627.50
Deal ID:    2140558812
──────────────────────
```

**3. Order rejected — failed entry** — `alert_order_rejected(..., is_exit=False)` — fires when a broker-side entry order fails (invalid volume, no money, trade disabled, requote past deviation, etc.) or when MT5 lot-sizing resolves to a size below the symbol's `volume_min` (see §9.2/§13).

```
❌ APEX ORDER REJECTED
──────────────────────
Ticker:  WDC  (WDC.NAS-24)
Reason:  mt5_volume_below_volume_min
Mode:    LIVE
──────────────────────
⚠️ Review logs — no position opened.
```

**4. Order rejected — failed exit** — `alert_order_rejected(..., is_exit=True)` — fires when a stop/Stage-9/time-stop exit order fails to fill. Footer is deliberately different from #3: the position is still open and needs attention, not absent.

```
❌ APEX ORDER REJECTED
──────────────────────
Ticker:  PLTR  (PLTR.NAS)
Reason:  exit failed (stop_hit): TRADE_DISABLED
Mode:    LIVE
──────────────────────
⚠️ Exit FAILED — position is still OPEN. Manual review/close required.
```

**5. Position closed** — `alert_position_closed()` — fires whenever an open position is closed, for any exit reason (`stop_gap`, `stop_hit`, `stage9_exit`, `time_stop`).

```
🟢 APEX POSITION CLOSED
──────────────────────
Ticker:   NVDA  (NVDA.NAS)
Time:     2026-09-02 16:31:12 UTC
Exit:     $151.40
P&L:      +$3,027.50  (+1.86R)
Reason:   stage9_exit
──────────────────────
```
(🔴 instead of 🟢 when P&L is negative.)

**6. Circuit breaker tripped** — `alert_circuit_breaker()` — fires when daily drawdown ≥3%, weekly drawdown ≥7%, or 5 consecutive losing trades is breached. **Halts ALL new entries** until manually cleared.

```
🚨 APEX CIRCUIT BREAKER TRIPPED
──────────────────────
Reason: daily_drawdown_3.42%
──────────────────────
Daily DD:   3.42%
Weekly DD:  1.15%
Loss streak: 2
──────────────────────
⛔ All execution halted. Review and run:
python run_prod.py --reset-circuit-breaker
```

**7. Operational error** — `alert_error()` — fires on any unhandled exception in the entry loop or exit loop (network blip, malformed data, unexpected broker response, etc.). `critical=True` escalates the emoji/level but nothing currently sets it (all current call sites use the default `critical=False`).

```
⚠️ APEX ERROR
──────────────────────
Ticker:  AMD
Error:   Connection timeout after 10000ms
──────────────────────
Check: logs/run_*.jsonl
```

### ⚪ Defined but NOT currently wired up — you will NOT receive these yet

These functions exist in `alert.py`, fully built, but nothing in the codebase calls them. Listed here so it's explicit what's *not* covered, rather than assuming silence means "nothing happened."

**8. Spider gate blocked** — `alert_spider_gate_block()` — would fire when the sector macro gate blocks a trade. Moot right now since spider gate is disabled (§14) — nothing to block.
```
🕷️ SPIDER GATE BLOCKED
Ticker:  XOM
Sector:  XLE
Reason:  sector_regime_bearish
```

**9. Run summary** — `alert_run_summary()` — would fire once at the end of each signals/execution run with aggregate counts. Currently only written to the local log (`log_run_summary`), not Telegram.
```
📊 APEX RUN SUMMARY
──────────────────────
Mode:       EXECUTION [LIVE]
Time:       2026-08-14 13:35 UTC
Signals:    4
Orders:     3
Closed:     1
Equity:     $102,430.50
──────────────────────
```

**10. Startup notification** — `alert_startup()` — would fire when the orchestrator initializes. Currently silent — no confirmation reaches Telegram that a scheduled run even started.
```
🚀 APEX STARTED
──────────────────────
Mode:    EXECUTION
Env:     LIVE
Broker:  MT5
Equity:  $102,430.50
Time:    2026-08-14 13:31 UTC
──────────────────────
```

**11. Connection failed** — `alert_connection_failed()` — would fire if MT5/IG login fails at startup. **This is the most important gap of the four** — right now a broker auth failure crashes the process locally with no Telegram signal at all (beyond scheduler.py's generic retry alert, which has no broker/credential detail).
```
🔴 APEX CONNECTION FAILED
──────────────────────
Broker:  MT5
Error:   MT5 login failed: (10004, 'Invalid account')
──────────────────────
⛔ Run aborted. Check credentials in .env
```

**For manager reporting:** the honest summary is "7 of 11 alert types are live — every signal, every fill, every rejection (entry or exit), every close, every circuit-breaker trip, and every operational error reaches Telegram today. The 4 that don't are lower-priority (run summaries, startup pings) except connection-failure alerting, which is worth prioritizing next since it's currently a genuine blind spot for unattended runs."
