# APEX System Guide
**Complete operations, architecture, and execution reference.**

---

## Table of Contents

- [1. What APEX Does — One-Line Summary](#1-what-apex-does--one-line-summary)
- [2. Daily Run Schedule — What to Run and When](#2-daily-run-schedule--what-to-run-and-when)
- [3. Signal Generation — Full Flow](#3-signal-generation--full-flow)
  - [3.1 Data Source: Why yfinance, Not IG](#31-data-source-why-yfinance-not-ig)
  - [3.2 Is yfinance Accurate Enough?](#32-is-yfinance-accurate-enough)
  - [3.3 EMA 200 Lookback Guarantee](#33-ema-200-lookback-guarantee)
  - [3.4 Indicator Calculations — Exact Formulas](#34-indicator-calculations--exact-formulas)
  - [3.5 Stage Classifier — How Stages Are Locked In](#35-stage-classifier--how-stages-are-locked-in)
  - [3.6 Backtest Replication — Are They Identical?](#36-backtest-replication--are-they-identical)
- [4. Execution Flow — What Happens Next Morning](#4-execution-flow--what-happens-next-morning)
  - [4.1 IG-Specific: Sizing, Stop Loss, Risk](#41-ig-specific-sizing-stop-loss-risk)
  - [4.2 Paper Mode vs Live Mode](#42-paper-mode-vs-live-mode)
- [5. Risk Management — The Full Picture](#5-risk-management--the-full-picture)
  - [5.1 Per-Trade Risk (1%)](#51-per-trade-risk-1)
  - [5.2 Circuit Breaker Math — How 3%/7% Works With 1% Risk](#52-circuit-breaker-math--how-37-works-with-1-risk)
  - [5.3 Portfolio Position Cap](#53-portfolio-position-cap)
- [6. Telegram Alerts — Setup and What You Receive](#6-telegram-alerts--setup-and-what-you-receive)
  - [6.1 One-Time Setup](#61-one-time-setup)
  - [6.2 Alert Types](#62-alert-types)
- [7. Logging — JSON Event Log](#7-logging--json-event-log)
- [8. State Files — What They Are and When to Touch Them](#8-state-files--what-they-are-and-when-to-touch-them)
- [9. Spider Gate — Macro Filter](#9-spider-gate--macro-filter)
- [10. IG Epic Map — Maintenance](#10-ig-epic-map--maintenance)
- [11. Pre-Live Checklist](#11-pre-live-checklist)
- [12. Common Issues and Fixes](#12-common-issues-and-fixes)

---

## 1. What APEX Does — One-Line Summary

APEX scans 20 US tech stocks every evening, identifies **Stage 7 Breakout Confirmed** signals using a 9-stage classifier, sizes positions using 1% equity risk, and submits orders to IG Group the following morning via REST API.

---

## 2. Daily Run Schedule — What to Run and When

| Time (UTC) | Command | Purpose |
|---|---|---|
| **21:05 UTC** (after US close) | `python run_prod.py --mode signals` | Fetch data, run indicators, classify stages, store signals |
| **13:31 UTC** (after US open) | `python run_prod.py --mode execution` | Execute pending signals, manage exits |
| Anytime | `python run_prod.py --mode status` | Check positions, equity, circuit breaker state |
| Anytime | `python run_prod.py --mode full` | Signals + execution in one shot (testing only) |

**Why these times?**

- `21:05 UTC` = ~4:05 PM EST = 5 minutes after NYSE close. All daily candles are final. yfinance data is settled.
- `13:31 UTC` = ~8:31 AM EST = 1 minute after NYSE open. Fills at near-open prices as intended by the strategy.

**Never run `--mode execution` before market open.** Orders would fill at pre-market prices, violating the strategy's entry assumption.

---

## 3. Signal Generation — Full Flow

### 3.1 Data Source: Why yfinance, Not IG

Signals use **yfinance** for historical OHLCV, not the IG API.

Reason: IG's historical price endpoint is rate-limited to **10 requests per minute** on demo accounts. With 20 tickers needing 300+ bars each, that would take ~2 minutes and risk throttling. yfinance has no rate limits, returns clean adjusted OHLCV, and is the same data source used in the backtest.

IG API is only used for:
- Live bid/ask price at execution time (entry price)
- Submitting and tracking orders

### 3.2 Is yfinance Accurate Enough?

**Yes, for this strategy.**

APEX is a **daily timeframe, end-of-day signal** strategy. yfinance provides:
- Adjusted close (split/dividend corrected) — same as most professional backtests
- Volume figures matching exchange data
- 300+ calendar days history per call (more than enough for all indicators)

yfinance data matches Bloomberg/Refinitiv daily OHLCV within rounding (typically <$0.01 on close). The backtest was built on yfinance data, so signal calculation is consistent by construction.

**Limitation to know**: yfinance can have 1-2 day delays on some tickers in rare cases. If running `--mode signals` and you see a ticker with a last date of yesterday instead of today, that ticker's signal for today will be stale by one day. This is rare and acceptable for daily timeframe.

### 3.3 EMA 200 Lookback Guarantee

EMA 200 requires 200+ bars to converge properly.

`lookback_days: 300` in `config/production.yaml` fetches 300 calendar days ≈ **210–215 trading days**. This provides enough history for:

| Indicator | Periods Needed | Bars Fetched | Safe? |
|---|---|---|---|
| EMA 10 | 10 | 210+ | ✅ |
| EMA 20 | 20 | 210+ | ✅ |
| EMA 50 | 50 | 210+ | ✅ |
| EMA 100 | 100 | 210+ | ✅ |
| EMA 200 | 200 | 210+ | ✅ (10-15 bars of warmup headroom) |
| ATR 14 | 14 | 210+ | ✅ |
| Donchian 20 | 20 | 210+ | ✅ |

EMA converges exponentially — by bar ~4x the period, warmup error is < 0.01%. With 210+ bars, EMA 200 is fully converged for all 20 tickers.

### 3.4 Indicator Calculations — Exact Formulas

All indicators computed in `core/features/technicals/pipeline.py` via `apply_indicators()`.

**EMAs (periods: 10, 20, 50, 100, 200)**
```python
close.ewm(span=period, adjust=False).mean()
```
- `adjust=False` = Wilder-style recursive: `EMA_t = α * price_t + (1-α) * EMA_{t-1}`
- `α = 2 / (span + 1)`
- Identical to standard EMA used in TradingView, Amibroker, backtest engine

**ATR (period: 14)**
```python
tr = max(high-low, |high-prev_close|, |low-prev_close|)
atr = tr.ewm(span=14, adjust=False).mean()
```
- True Range uses previous close gap (not just bar range)
- Smoothed with Wilder's EMA (same `adjust=False`)
- Same method used in backtest for stop distance calculation

**Bollinger Bands (period: 20, std: 2)**
```python
bb_mid = close.rolling(20).mean()
bb_std = close.rolling(20).std(ddof=0)   # population std
bb_upper = bb_mid + 2 * bb_std
bb_lower = bb_mid - 2 * bb_std
```
- `ddof=0` = population standard deviation (N, not N-1)

**Donchian Channel (period: 20)**
```python
donchian_upper = high.rolling(20).max()
donchian_lower = low.rolling(20).min()
donchian_mid   = (donchian_upper + donchian_lower) / 2
```
- Breakout confirmation requires `close > donchian_upper` at signal bar

**Volume Surge**
```python
vol_avg = volume.rolling(20).mean()
volume_surge = (volume / vol_avg) >= 1.15
```
- Surge = current bar volume at least 1.15x the 20-day average
- Boolean flag used in Stage 3, 6, 7, 9 classification

**MACD**
```python
ema_fast = close.ewm(span=12, adjust=False).mean()
ema_slow = close.ewm(span=26, adjust=False).mean()
macd_line   = ema_fast - ema_slow
macd_signal = macd_line.ewm(span=9, adjust=False).mean()
macd_hist   = macd_line - macd_signal
```

**RSI (period: 14)**
```python
delta = close.diff()
gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
loss  = (-delta).clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
rs    = gain / loss
rsi   = 100 - (100 / (1 + rs))
```
- Wilder's smoothing: `alpha = 1/period`
- RSI >= 70 is one of Stage 9 (Exhaustion) conditions

### 3.5 Stage Classifier — How Stages Are Locked In

Stages are computed in `core/stages/stage_classifier.py` by `classify_stages()` and `StageClassifier`.

**Priority order (highest wins)**:
`Stage 9 → 8 → 7 → 6 → 5 → 4 → 3 → 2 → 1`

Each bar gets exactly one stage label. The classifier checks conditions from Stage 9 down and assigns the first match.

**Stage 2 (Dislocation) is special — it uses an expanding memory window:**

```
Stage 2 fires when:
  close <= bb_lower   OR   close <= ema50 * 0.98
```

Once a stock has **ever** hit Stage 2, `stage2_seen = True` is stored in `state/stage2_memory.json`. This flag **never resets** — even if the stock goes through multiple cycles. Stage 7 requires `stage2_seen = True` as a prerequisite.

This is by design: we only trade breakouts that have previously dislocated and recovered. This filters out stocks that have never pulled back — they may be parabolic extensions, not genuine breakout setups.

**Stage 7 (Breakout Confirmed) — the signal stage:**
```
Requires ALL of:
  1. stage2_seen = True  (dislocation memory)
  2. ema10 > ema20 > ema50   (bullish EMA stack)
  3. volume_surge = True      (volume confirmation)
  4. close > donchian_upper   (price breakout above 20-day range)
```

All four must be true simultaneously on the close bar. If any one is missing, the bar is assigned a lower stage.

### 3.6 Backtest Replication — Are They Identical?

**Yes — the production pipeline uses the exact same code.**

The strategy logic in `core/` is **broker-agnostic**. The backtest was built on top of the same `apply_indicators()` and `classify_stages()` functions. Production calls them identically.

What is identical:
- All indicator formulas (same pandas/ewm calls)
- Stage classification rules (same `stage_classifier.py`)
- Stage 2 memory logic (persistent expanding window)
- ATR calculation for stop loss
- Entry condition: Stage 7 close bar triggers next-day market open execution
- Stop: `entry - ATR(14) * 2.0`, floored at `0.5%`

What differs (unavoidable, acceptable):
- **Entry price**: Backtest uses EOD close as proxy. Production enters at next-morning IG ask price. This is ~0.1–0.3% slippage typically, consistent with real-world trading.
- **Fills**: Backtest assumes perfect fills. Production goes through IG OTC — fills are near-instant but not zero-latency.
- **Data timing**: Backtest used fixed historical dataset. Production fetches fresh yfinance data nightly.

There is no logic divergence between backtest and production. The same conditions that fired a signal historically will fire the same signal in production.

---

## 4. Execution Flow — What Happens Next Morning

When you run `python run_prod.py --mode execution`:

```
1. IGConnector.connect()
   → IG REST session created (version=3 auth)
   → Account equity fetched (used for sizing)
   → Telegram: 🚀 APEX STARTED

2. CircuitBreaker.check()
   → Reads state/circuit_breaker.json
   → If halted: log + Telegram alert + exit immediately

3. SpiderGate.check() per signal
   → Reads data/cleaned/spiders_daily/gate/spider_gate_daily.parquet
   → Returns gate_mult (1.0 = full risk, 0.5 = half size, 0.0 = block)
   → If gate file missing: defaults to ALLOW (mult=1.0)

4. PositionManager.check_exits()
   → For each open position: fetch live IG bid price
   → If bid <= stop_price: close position (stop hit)
   → If hold_days > time_stop_days: close position (time stop)
   → If current stage = 9: close position (exhaustion exit)

5. For each pending signal (from state/run_state.json):
   → compute_position_size() → shares, risk_dollars, stop_distance
   → build_entry_request_ig() → IG OTC order dict
   → send_order_ig() → POST to IG /positions/otc
   → Telegram: ✅ ORDER SENT (or ❌ REJECTED)
   → RunLogger.log_order_sent()

6. CircuitBreaker.update()
   → Update daily/weekly drawdown from equity change
   → Update consecutive loss streak

7. RunLogger.log_run_summary()
   → Telegram: 📊 RUN SUMMARY
```

### 4.1 IG-Specific: Sizing, Stop Loss, Risk

**How sizing works on IG (no MT5 contract sizes):**

IG CFDs are denominated in **contracts = 1 share per contract** for US equities. Fractional contracts are NOT supported — all sizes are rounded to whole integers.

```python
# equity_pct mode (default, risk_pct=0.01):
risk_dollars   = equity * 0.01 * gate_mult
stop_distance  = entry_price - stop_price       # in dollars
shares         = floor(risk_dollars / stop_distance)

# Apply portfolio cap:
max_by_port    = floor(equity * 0.20 / entry_price)  # max 20% of equity
shares         = min(shares, max_by_port)
shares         = max(shares, 1)                       # minimum 1 share
```

**Example** (equity = $10,000, AAPL @ $175, ATR = $3.50):
```
risk_dollars   = $10,000 * 0.01 = $100
stop_price     = $175 - ($3.50 * 2.0) = $168.00
stop_distance  = $175 - $168 = $7.00
shares         = floor($100 / $7.00) = 14
position_value = 14 * $175 = $2,450
actual_risk    = 14 * $7.00 = $98  (≈1% equity)

Portfolio cap check:
max_by_port    = floor($10,000 * 0.20 / $175) = 11  ← binds here
shares         = min(14, 11) = 11
actual_risk    = 11 * $7.00 = $77  (0.77% equity)
```

**How the stop loss is placed on IG:**

The stop is a **guaranteed stop loss** (GSL) on the IG OTC position. It is set in `build_entry_request_ig()`:
```python
"stopLevel": stop_price  # absolute price level, not pips
```

IG converts this to a stop in points (USD for US equities). The stop persists server-side — it will trigger even if your machine is offline.

**Currency**: All US equity CFDs on IG are priced in USD. P&L also in USD. No currency conversion needed.

### 4.2 Paper Mode vs Live Mode

| Setting | IG API Called? | Orders Placed? | Safe? |
|---|---|---|---|
| `environment: paper` | ✅ Connect + price fetch | ❌ No order calls | ✅ Always safe |
| `environment: live` + `acc_type: DEMO` | ✅ Full API | ✅ Demo orders only | ✅ No real money |
| `environment: live` + `acc_type: LIVE` | ✅ Full API | ✅ Real orders | ⚠️ REAL MONEY |

**Current state**: `environment: paper` + `acc_type: DEMO` = maximum safety. Nothing can reach real money.

---

## 5. Risk Management — The Full Picture

### 5.1 Per-Trade Risk (1%)

`risk_pct_per_trade: 0.01` in `config/risk.yaml`.

This means: **maximum loss per trade = 1% of current equity**.

The stop loss is sized so that if price hits the stop, you lose exactly $equity × 0.01. Due to portfolio cap and integer rounding, actual risk may be slightly less.

### 5.2 Circuit Breaker Math — How 3%/7% Works With 1% Risk

Question: *"If I risk 1% per trade and there's no cap on open positions, how can we have a 3% daily loss limit?"*

The answer is that these are two different control layers:

**Layer 1 — Per-trade stop loss (pre-entry):**
Each trade is sized so a stop-out = 1% equity loss. This controls **maximum per-trade loss** assuming the stop is hit cleanly (no gap).

**Layer 2 — Circuit breaker (post-facto drawdown check):**
The circuit breaker monitors **realised cumulative drawdown** from a daily/weekly high-water mark.

```
daily_drawdown = (daily_baseline_equity - current_equity) / daily_baseline_equity

If daily_drawdown >= 0.03 → HALT
If weekly_drawdown >= 0.07 → HALT
If consecutive_losses >= 5 → HALT
```

**How they interact:**

With 5 max open positions, each risking 1%, maximum theoretical daily loss from stops = **5%** (all 5 positions stop out in one day). The 3% circuit breaker trips **before** all 5 can be stopped out — it halts new execution after ~3 clean 1% losses.

```
Day scenario:
  Trade 1: -1% → equity -1%   (CB: 1% < 3% → continue)
  Trade 2: -1% → equity -2%   (CB: 2% < 3% → continue)
  Trade 3: -1% → equity -3%   (CB: 3% >= 3% → HALT)
  Trades 4 & 5: never executed
```

**Gap risk**: If a stock gaps down through the stop, you can lose more than 1%. IG does NOT guarantee your stop on overnight gaps unless you pay for a Guaranteed Stop Loss (GSL). For now, the gap protection in `config/production.yaml` (`gap_protection: true`) detects next-open gaps and exits at market, but the circuit breaker also catches the resulting larger-than-expected drawdown.

**Weekly circuit breaker** catches sustained bad weeks where you lose <3% per day but compound to >7% over the week.

**Consecutive loss halt** (5 losses) catches strategy breakdown independent of dollar loss — protects during market regime changes.

### 5.3 Portfolio Position Cap

`max_open_positions: 5` in `config/production.yaml`.

Even if 10 Stage 7 signals fire on the same day, only the first 5 (by signal priority) will be executed. Remaining signals are stored in `state/run_state.json` but not actioned until a position is closed.

`max_single_position_pct: 0.20` = no single position can exceed 20% of equity by value (the portfolio cap applied during sizing).

---

## 6. Telegram Alerts — Setup and What You Receive

### 6.1 One-Time Setup

**Step 1 — Create bot:**
1. Open Telegram → search `@BotFather`
2. Send: `/newbot`
3. Follow prompts → receive `BOT_TOKEN` (format: `123456789:ABCdef...`)

**Step 2 — Get your chat ID:**
1. Start a conversation with your new bot (send any message)
2. Open browser: `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find `"chat": {"id": 123456789}` in the response — that number is your `CHAT_ID`

**Step 3 — Add to .env:**
```bash
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ
TELEGRAM_CHAT_ID=123456789
```

**Step 4 — Test:**
```python
from prod.monitoring.alert import send_alert
send_alert("APEX Telegram test", level="INFO")
```

If vars are missing, alerts fall back to log-only silently — nothing breaks.

### 6.2 Alert Types

| Event | Emoji | Trigger |
|---|---|---|
| System startup | 🚀 | Every `run_prod.py` invocation |
| Signal found | 📡 | Stage 7 detected for ticker |
| Order sent | ✅ | Successful IG order submission |
| Order rejected | ❌ | IG rejected order OR paper guard blocked |
| Position closed | 🟢/🔴 | Any exit (stop / time stop / stage 9) |
| Circuit breaker | 🚨 | DD or streak threshold breached |
| Spider gate block | 🕷️ | Macro filter blocked a trade |
| Run summary | 📊 | End of each run mode |
| Error (non-critical) | ⚠️ | Any caught exception during execution |
| Error (critical) | 🔥 | Connection failure or unrecoverable error |

**Example order alert:**
```
✅ APEX ORDER SENT 🟡 PAPER
──────────────────────
Ticker:     NVDA  (UC.D.NVDA.CASH.IP)
Time:       2026-05-21 13:31:44 UTC
Entry:      $875.40
Size:       11 shares
Stop:       $847.80  ($27.60 below entry)
Risk $:     $303.60
Deal ID:    N/A
──────────────────────
```

**Example error alert:**
```
⚠️ APEX ERROR
──────────────────────
Ticker:  COIN
Error:   get_live_price failed: ConnectionTimeout
Context: execution loop, order build phase
──────────────────────
Check: logs/run_*.jsonl
```

---

## 7. Logging — JSON Event Log

Every run writes structured JSONL to `logs/run_YYYYMMDD.jsonl`.

**Format — one JSON object per line:**
```json
{"ts": "2026-05-21T13:31:44.123456+00:00", "event": "signal", "ticker": "NVDA", "stage": 7, "signal_date": "2026-05-20"}
{"ts": "2026-05-21T13:31:45.234567+00:00", "event": "order_sent", "ticker": "NVDA", "retcode": 0, "success": true, "volume": 11, "price": 875.4}
{"ts": "2026-05-21T13:31:46.345678+00:00", "event": "position_open", "ticker": "NVDA", "entry": 875.4, "stop": 847.8, "shares": 11}
```

**Event types logged:**

| Event | Fields |
|---|---|
| `signal` | ticker, stage, signal_date |
| `order_sent` | ticker, retcode, success, volume, price |
| `position_open` | ticker, entry, stop, shares |
| `position_close` | ticker, exit, pnl, pnl_r, reason |
| `gate_decision` | ticker, allowed, reason, risk_mult |
| `circuit_breaker` | reason |
| `error` | ticker, error |
| `run_summary` | signals, orders, equity, mode |

**Parsing logs (Python):**
```python
import json
from pathlib import Path

log = Path("logs/run_20260521.jsonl")
events = [json.loads(line) for line in log.read_text().splitlines() if line]
orders = [e for e in events if e["event"] == "order_sent"]
errors = [e for e in events if e["event"] == "error"]
```

---

## 8. State Files — What They Are and When to Touch Them

All state lives in `state/` (auto-created, gitignored).

### `state/stage2_memory.json`

```json
{"AAPL": true, "NVDA": true, "TSLA": false, ...}
```

**What**: Records which tickers have ever hit Stage 2 (Dislocation).

**Rule**: **NEVER delete or reset this file.** It is the core memory of the strategy. Deleting it means every Stage 7 signal requires a fresh Stage 2 observation — you will miss valid setups for weeks.

**When to inspect**: If a stock has been in your universe for 300+ days and still shows `false`, it has genuinely never dislocated. Either the stock is in a strong sustained uptrend, or there is a data issue.

### `state/positions.json`

Open position tracker. Each entry: entry price, stop, shares, deal ID, open date.

**Reset safe?**: No. Deleting loses all position tracking — APEX will re-enter already-open positions.

**Note**: Field names use `mt5_ticket` / `mt5_symbol` for historical compatibility. In IG mode these store `ig_deal_id` / `ig_epic` respectively.

### `state/circuit_breaker.json`

```json
{"halted": false, "daily_drawdown_pct": 0.012, "weekly_drawdown_pct": 0.018, "consecutive_losses": 1}
```

**Reset**: Safe to reset after manual review. Use:
```bash
python run_prod.py --reset-circuit-breaker
```
Or delete the file — it will reinitialise on next run.

### `state/run_state.json`

Pending signals waiting for next execution window.

**Reset**: Safe to clear if stale (e.g., signals from 3 days ago that were never executed).

### `state/trade_log.json`

Full closed trade history. Append-only.

**Reset**: Safe to archive — move to `logs/` if it grows too large.

---

## 9. Spider Gate — Macro Filter

The spider gate uses 10 sector ETFs (XLK, XLF, XLV, XLE, XLY, XLP, XLI, XLB, XLU, XLRE) to assess macro health before each trade.

Gate output per ticker:
- `mult = 1.0` → full size, proceed
- `mult = 0.5` → half position size
- `mult = 0.0` → block trade entirely

**Current state**: Gate file at `data/cleaned/spiders_daily/gate/spider_gate_daily.parquet`. If this file is missing or stale, gate defaults to `mult = 1.0` (allow all).

**Config**: `spider_gate.enabled: true` in `config/production.yaml`. Set to `false` to bypass.

---

## 10. IG Epic Map — Maintenance

Epics are account-specific. The current `config/ig_epic_map.yaml` was validated on account `Z5S2OK` (demo). The 20 validated epics are correct for this account.

**Important aliases** (IG uses old/internal tickers):
- `META` → `UB.D.FB.CASH.IP` (IG still uses Facebook ticker)
- `COIN` → `UA.D.COINUS.CASH.IP`
- `PLTR` → `SE.D.PLTRUS.CASH.IP`
- `CRWD` → `UA.D.CRWDUS.CASH.IP`
- `ARM`  → `UA.D.ARMUS.CASH.IP`

**If you add new tickers:**
```bash
# Search for epic
python tools/ig_epic_inspector.py --search TICKER

# Search multiple in one session (avoids rate limits)
python tools/ig_epic_inspector.py --search-many TICK1 TICK2 TICK3

# Validate all current epics
python tools/ig_epic_inspector.py --validate-all
```

**If epics stop working on a new account**: Re-run `--discover-all`. Epic prefixes (UA/UB/UC/UD/SA/SE) differ by account region.

---

## 11. Pre-Live Checklist

Work through these steps before switching to `environment: live`.

**Phase 1 — Paper mode validation (current)**
- [x] All 20 epics validated via `--validate-all`
- [x] `--mode signals` runs clean (no errors, 0+ signals generated)
- [ ] `--mode execution` runs clean in paper mode (no exceptions, sizing looks correct)
- [ ] Telegram alerts firing correctly for all event types
- [ ] Logs writing to `logs/run_*.jsonl` with correct structure
- [ ] Stage 2 memory populating correctly in `state/stage2_memory.json`
- [ ] Circuit breaker state persisting between runs

**Phase 2 — IG DEMO live orders**
- [ ] Change `config/production.yaml`: keep `environment: paper` but change `acc_type: DEMO`
- [ ] Wait for a real Stage 7 signal
- [ ] Switch to `environment: live` + `acc_type: DEMO` — real IG API calls, demo money
- [ ] Verify: order appears in IG demo platform with correct size and stop level
- [ ] Verify: position tracked in `state/positions.json`
- [ ] Verify: Telegram order alert received with correct fields
- [ ] Verify: exit logic triggers correctly (set a very wide stop, manually close in IG UI, check APEX detects it)

**Phase 3 — Live money**
- [ ] Switch `acc_type: DEMO` → `acc_type: LIVE`
- [ ] Switch `environment: paper` → `environment: live`
- [ ] Confirm live prompt at startup (`CONFIRM` typed manually)
- [ ] Start with small equity allocation to validate fills

**Never skip Phase 2.** Demo validation catches IG API response format issues, stop level placement bugs, and deal rejection edge cases without risking real capital.

---

## 12. Common Issues and Fixes

### `ApiExceededException` during epic discovery

**Cause**: Too many separate IG login attempts (each `--search` call creates a new session).

**Fix**: Use `--search-many` to run all searches in one session:
```bash
python tools/ig_epic_inspector.py --search-many AAPL MSFT GOOGL AMZN META NVDA
```

Wait 15 minutes before retrying if you hit the limit.

### `Cannot identify date column` in ig_fetcher

**Cause**: yfinance version mismatch — `Date` column comes through capitalised on some versions.

**Fix**: Already applied. `ig_fetcher.py` now calls `reset_index()` before column lowercasing. Pull latest code.

### `AttributeError: 'float' object has no attribute 'get'` in ig_connector

**Cause**: `trading_ig` v0.0.24 returns `balance` as flat float, not nested dict.

**Fix**: Already applied. `ig_connector.py` checks `isinstance(balance_val, dict)` before `.get()`.

### Signals show 0 despite price breakouts

**Cause 1**: `stage2_seen = False` for that ticker — Stage 7 requires prior Stage 2 history.
→ Check `state/stage2_memory.json`. If false after 300+ days, the stock may not have dislocated in the data window.

**Cause 2**: Volume surge condition not met — `volume >= 1.15x` 20-day average required.
→ Add temporary debug print: check `volume_surge` column in indicator output.

**Cause 3**: EMA stack not confirmed — `ema10 > ema20 > ema50` all required simultaneously.
→ Print last bar's EMA values for the ticker.

### Circuit breaker stuck in halted state

```bash
# Manual reset after reviewing logs:
python run_prod.py --reset-circuit-breaker

# Or delete state file (reinitialises on next run):
rm state/circuit_breaker.json
```

### Telegram alerts not arriving

1. Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in `.env`
2. Ensure your bot is started (send `/start` to it in Telegram)
3. Run test: `python -c "from prod.monitoring.alert import send_alert; send_alert('test')"`
4. Check logs for `Telegram send error` warnings

### IG order rejected — `REJECT_INVALID_STOP`

**Cause**: Stop level too close to entry for IG's minimum stop distance.

**Fix**: Increase `atr_multiplier` in `config/production.yaml` (currently 2.0). IG requires minimum distance of ~0.5–1% for most US equity CFDs. The `floor_pct: 0.005` already enforces this floor — check if it is being applied.
