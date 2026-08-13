# APEX — Production Algo System

**Long-only breakout US equity strategy — IG Group API execution (broker-agnostic).**
Exact production replication of the ALGO-Stocks backtest. Supports IG Group (default) and MT5 (legacy).

> 📖 For the full operations manual, indicator details, circuit breaker math, Telegram setup, and pre-live checklist — see **[SYSTEM_GUIDE.md](SYSTEM_GUIDE.md)**

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Quick Start — IG Mode](#quick-start----ig-mode-default)
- [Broker Configuration](#broker-configuration)
- [IG Epic Map](#ig-epic-map-configig_epic_mapyaml)
- [Strategy Logic](#strategy-logic)
- [Risk Modes](#risk-modes)
- [Spider Gate](#spider-gate)
- [Paper vs Live](#paper-vs-live)
- [State Files](#state-files)
- [Environment Variables](#environment-variables-env)

---

## Architecture Overview

```
APEX-Stocks_algo-v2/
|-- config/                       # All configuration (YAML)
|   |-- production.yaml           # broker, environment, universe, entry/exit, portfolio caps
|   |-- risk.yaml                 # Active risk mode + all 5 sizing modes
|   |-- indicators.yaml           # EMA, BB, Donchian, ATR, Volume, MACD, RSI params
|   |-- stages.yaml               # Stage 1-9 classifier thresholds
|   |-- spiders.yaml              # 10 sector spider gate configs
|   |-- ig_epic_map.yaml          # Ticker -> IG epic mapping  [NEW]
|   `-- mt5_symbol_map.yaml       # Ticker -> MT5 broker symbol mapping (legacy)
|
|-- core/                         # Strategy logic (broker-independent)
|   |-- features/technicals/
|   |   `-- pipeline.py           # apply_indicators(): all indicators in one pass
|   |-- stages/
|   |   `-- stage_classifier.py   # Stages 1-9 state machine + StageClassifier (persistent)
|   |-- filters/
|   |   `-- spider_gate.py        # SpiderGate: macro permission layer
|   `-- utils/
|       |-- config_loader.py      # load_*_config(), resolve_ig_credentials()
|       `-- logging.py            # setup_logger()
|
|-- prod/                         # Production execution layer
|   |-- execution/
|   |   |-- ig_connector.py       # IGConnector: IG REST session lifecycle  [NEW]
|   |   |-- ig_order_builder.py   # build_entry_request_ig(), build_close_request_ig()  [NEW]
|   |   |-- ig_order_executor.py  # send_order_ig() (paper guard + confirm polling)  [NEW]
|   |   |-- mt5_connector.py      # MT5Connector: legacy MT5 session lifecycle
|   |   |-- order_builder.py      # build_entry_request(), build_close_request() (MT5)
|   |   `-- order_executor.py     # send_order() MT5 with retry
|   |-- data/
|   |   |-- ig_fetcher.py         # fetch_universe_ig() via yfinance  [NEW]
|   |   |-- mt5_fetcher.py        # fetch_ohlcv(), fetch_universe() (MT5)
|   |   `-- universe.py           # build_epic_map(), build_symbol_map(), helpers
|   |-- signals/
|   |   `-- signal_generator.py   # SignalGenerator: EOD Stage 7 detection
|   |-- risk/
|   |   |-- position_sizer.py     # compute_position_size(): all 5 modes
|   |   `-- circuit_breaker.py    # CircuitBreaker: daily/weekly DD + streak halt
|   |-- positions/
|   |   |-- position_manager.py   # PositionManager: open/close/track
|   |   `-- portfolio_tracker.py  # PortfolioTracker: trade log + R-multiple
|   |-- state/
|   |   `-- state_manager.py      # StateManager: run state persistence
|   |-- monitoring/
|   |   |-- logger.py             # RunLogger: structured JSONL event log
|   |   `-- alert.py              # send_alert(): extend for Telegram/email
|   `-- orchestrator.py           # APEXOrchestrator: broker-dispatching master coordinator
|
|-- tools/
|   |-- ig_epic_inspector.py      # Discover/validate IG epics for your account  [NEW]
|   `-- mt5_symbol_inspector.py   # Discover broker symbols + filling modes (MT5)
|
|-- state/                        # Runtime state (auto-created, gitignored)
|   |-- stage2_memory.json        # Stage 2 expanding-window memory (NEVER resets)
|   |-- positions.json            # Open positions
|   |-- trade_log.json            # Closed trade history
|   |-- run_state.json            # Pending signals/orders
|   `-- circuit_breaker.json      # Circuit breaker state
|
|-- logs/                         # Run logs (auto-created)
|-- data/                         # OHLCV + spider gate data
|-- .env                          # Credentials (never commit)
`-- run_prod.py                   # Main entry point
```

---

## Quick Start — IG Mode (Default)

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Get IG API credentials
- Log in to IG: My IG -> Manage account -> API keys -> Create API key
- Note: identifier (username), password, API key, account ID

### 3. Set credentials in .env
```bash
cp .env.example .env
# Edit .env:
# IG_IDENTIFIER=your_username
# IG_PASSWORD=your_password
# IG_API_KEY=your_api_key
# IG_ACCOUNT_ID=your_account_id
# IG_ACC_TYPE=DEMO   (start with DEMO, switch to LIVE when ready)
```

### 4. Discover your IG epics
```bash
python tools/ig_epic_inspector.py --search AAPL
python tools/ig_epic_inspector.py --validate-all
```
Copy correct epics into `config/ig_epic_map.yaml`.

### 5. Configure
Edit `config/production.yaml`:
```yaml
environment: "paper"   # keep paper until validated
broker: "ig"           # already default
ig:
  acc_type: "DEMO"     # DEMO first
```

### 6. Run
```bash
# Check status
python run_prod.py --mode status

# EOD (after US market close ~21:05 UTC): generate signals
python run_prod.py --mode signals

# AM (after US market open ~13:31 UTC): execute + manage exits
python run_prod.py --mode execution

# Full cycle (for testing only)
python run_prod.py --mode full
```

---

## Broker Configuration

Set in `config/production.yaml`:

```yaml
broker: "ig"    # "ig" (default) | "mt5" (legacy)
environment: "paper"   # "paper" | "live"
```

**IG mode** uses:
- yfinance for historical OHLCV (no rate limits, 300+ day lookback)
- IG REST API for live bid/ask prices at execution time
- IG OTC positions for all entries and exits

**MT5 mode** (legacy, unchanged):
- MT5 for all data and execution
- Requires Windows + MetaTrader5 package

---

## IG Epic Map (`config/ig_epic_map.yaml`)

IG epics are account-region specific. Standard format for UK/EU CFD accounts:

```
CS.D.AAPL.CFD.IP   -- daily funded CFD
CS.D.AAPL.CASH.IP  -- undated/rolling DFB
```

Use the inspector to find correct epics for your account:
```bash
python tools/ig_epic_inspector.py --search AAPL
python tools/ig_epic_inspector.py --validate-all   # checks all 20 tickers
python tools/ig_epic_inspector.py --list-open-positions
```

---

## Strategy Logic

### Signal Generation (EOD)
1. Fetch D1 OHLCV via yfinance (`lookback_days` bars per ticker)
2. Apply all indicators: EMA 10/20/50/100/200, BB(20,2sigma), Donchian(20), ATR(14), Volume(20), MACD, RSI
3. Classify each bar into Stage 1-9 using expanding-window state machine
4. **Stage 2 Dislocation** prerequisite: once seen, never resets (persisted in `state/stage2_memory.json`)
5. Signal fires when **Stage 7 (Breakout Confirmed)** detected AND Stage 2 exists in history
6. Signal persisted to `state/run_state.json` for next-day execution

### Stage Definitions
| Stage | Name                   | Primary Condition                              |
|-------|------------------------|------------------------------------------------|
| 1     | Downtrend              | EMA10 < EMA20 < EMA50                          |
| 2     | Dislocation            | Price <= BB lower OR price <= EMA50 x 0.98     |
| 3     | Accumulation           | Volume surge > 1.15x                           |
| 4     | Base Building          | Price near Donchian upper                      |
| 5     | Pre-Breakout           | EMA10 > EMA20                                  |
| 6     | Breakout Attempt       | Price >= Donchian upper + volume surge         |
| 7     | **Breakout Confirmed** | EMA stack + volume surge + close > Donchian    |
| 8     | Extension              | Price near BB upper                            |
| 9     | Exhaustion             | RSI >= 70 + volume climax                      |

### Execution (Next-Day AM)
1. Spider gate check per sector -- blocks or scales risk if macro unfavourable
2. Circuit breaker check -- halts if daily DD > 3%, weekly DD > 7%, or 5 consecutive losses
3. Entry at live IG ask price
4. Stop: `entry_price - ATR(14) x 2.0`, floor at `0.5%` of entry
5. Position size from active risk mode x gate multiplier

### Exit Hierarchy
1. **Stop hit**: current bid <= stop_price -> close immediately
2. **Time stop**: held > `time_stop_days` (default 10) -> exit
3. **Stage 9**: exhaustion detected -> exit next open

---

## Risk Modes

Set `active_mode` in `config/risk.yaml`:

| Mode               | Description                      | Key Param                      |
|--------------------|----------------------------------|--------------------------------|
| `equity_pct`       | Risk % of equity per trade       | `risk_pct_per_trade: 0.01`     |
| `fixed_dollar_risk`| Fixed $ risked per trade         | `risk_dollars: 200`            |
| `fixed_lots`       | Fixed shares always              | `lots: 10`                     |
| `atr_dynamic`      | Volatility-adjusted size         | `vol_target_pct: 0.01`         |
| `volume_based`     | Liquidity-aware sizing           | `adv_participation_pct: 0.01`  |

Gate multiplier always applied on top.

---

## Spider Gate

10 sector composites (XLK, XLF, XLV, XLE, XLY, XLP, XLI, XLB, XLU, XLRE).
- `enabled: true` -- gate active, blocks/scales positions by sector health
- `enabled: false` -- all trades permitted regardless of macro state

---

## Paper vs Live

**CRITICAL**: `environment: "paper"` is the safety guard.
- Paper: logs all actions, simulates fills, **never calls IG order endpoint**
- Live: prompts for `CONFIRM` at startup before any orders placed

IG demo account (`acc_type: DEMO`) is additional safety -- real API calls but no real money.

Switch to live only after:
1. Paper mode validated (signals generating, sizing correct)
2. IG DEMO validated (orders executing, positions tracking)
3. `environment: "live"` + `acc_type: "LIVE"` set

---

## State Files

All state in `state/` is auto-created on first run.

| File                   | Purpose                          | Reset safe?                  |
|------------------------|----------------------------------|------------------------------|
| `stage2_memory.json`   | Stage 2 per-ticker history       | **NEVER reset** -- design locked |
| `positions.json`       | Open positions                   | Reset = lose position tracking |
| `trade_log.json`       | Full trade history               | Safe to archive              |
| `circuit_breaker.json` | CB state + streak                | Can reset after manual review |
| `run_state.json`       | Pending signals/orders           | Safe to clear if stale        |

**Position state note (IG mode)**: `positions.json` uses `mt5_ticket` and `mt5_symbol`
field names (legacy naming preserved). In IG mode these store `ig_deal_id` and `ig_epic`
respectively. Do not rename these fields — it will break state file compatibility.

---

> Full operations reference, circuit breaker math, Telegram setup, indicator formulas, and pre-live checklist: **[SYSTEM_GUIDE.md](SYSTEM_GUIDE.md)**

---

## Environment Variables (.env)

```
# IG (primary broker)
IG_IDENTIFIER=your_username
IG_PASSWORD=your_password
IG_API_KEY=your_api_key
IG_ACCOUNT_ID=your_account_id
IG_ACC_TYPE=DEMO

# MT5 (legacy -- only needed if broker=mt5)
MT5_LOGIN=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=YourBroker-Server
```

Never commit `.env`. It is in `.gitignore`.
