# research/dislocation_study/study_config.py
"""
Single place for every path / parameter used by the dislocation study.
Edit here only. Everything is READ-ONLY on the repos and data folders;
all outputs go to research/dislocation_study/output/.
"""
from __future__ import annotations

import os
from pathlib import Path

# ── Paths (override with environment variables if your layout differs) ───────
ALGO_ROOT = Path(os.environ.get("ALGO_ROOT", r"C:\Users\ravil\PycharmProjects\ALGO-Stocks"))
APEX_ROOT = Path(os.environ.get("APEX_ROOT", r"C:\Users\ravil\PycharmProjects\APEX-Stocks_algo-v2"))
WORK_CACHE = Path(os.environ.get(
    "WORK_CACHE",
    str(APEX_ROOT / "data" / "raw" / "work_pc_cache" / "prices_daily" / "twelvedata" / "parquets"),
))

BT_CANDLES = ALGO_ROOT / "data" / "raw" / "prices_daily" / "twelvedata" / "parquets"
BT_TRADES = (ALGO_ROOT / "output" / "backtests" / "universe_baseline_v1_20260224_2310"
             / "universe" / "trades_all.parquet")

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
SHARDS = OUT / "shards"
SIM_TRADES = OUT / "sim_trades.parquet"

# ── Strategy mechanics (must match ALGO-Stocks config/backtest.yaml) ─────────
MIN_HISTORY_BARS = 260          # backtest stage floor
ATR_MULT = 2.0
STOP_FLOOR_PCT = 0.005
FIXED_STOP_FALLBACK_PCT = 0.06
TIME_STOP_DAYS = 365

# ── Cost model (R units) ─────────────────────────────────────────────────────
COMMISSION_PER_SHARE_RT = 0.04  # IC Markets demo observed: $0.04/share round trip
EXTRA_SLIPPAGE_R = 0.0          # add e.g. 0.05 to stress entry/exit slippage

# ── Account / portfolio model (steps 04 + 05) ────────────────────────────────
ACCOUNT_START = 100_000.0       # starting equity (USD)
RISK_PCT = 0.01                 # 1% of CURRENT equity risked per trade (= config/risk.yaml)
MAX_POSITIONS = 75              # concurrent position cap (production.yaml is 100; demo showed ~75 is the margin limit)
MAX_POS_VALUE_PCT = 0.20        # position notional <= 20% of equity (= config/risk.yaml max_position_pct)
SWAP_R_PER_TRADE = 0.014        # avg financing cost in R per trade (from demo analysis)
PORT_START = "2022-01-31"       # first entry date included in the portfolio sim
PRIORITIES = ["alpha", "deepest"]   # same-day order when slots are short: alphabetical (today's prod) vs most-below-EMA200 first
GROSS_CAPS = [None, 5.0]        # max gross notional / equity (None = unlimited; 5.0 = FCA retail 1:5)
PORT_SLIPPAGE_R = [0.0, 0.10]   # extra round-trip slippage in R per trade
RISK_SENSITIVITY = [0.0025, 0.005, 0.0075, 0.01]   # risk per trade grid (headline scenario, all specs)
PORT_MIN_PRICE = 2.0            # production has NO price floor; SSII ($0.03, +6,756R) proves one is needed. Applied to all scenarios
R_CAPS = [None, 10.0]           # cap on any single trade's R (None = raw; 10 = conservative, same as the stats winsor)
REPORT_SPECS = ["D25_S20", "D25_S40", "BASE_all_signals"]   # step 06 trade-log workbook
REPORT_SLIPS = [0.0, 0.10]
REPORT_GROSS = 5.0              # None = unlimited
REPORT_LOG = {"slip": 0.10, "r_cap": None}   # scenario written to the Trades_* sheets (raw R, no cap)
HEADLINE = {"priority": "deepest", "gross_cap": 5.0, "slip": 0.10, "r_cap": 10.0}   # scenario drawn on the equity chart

# ── Cost stress (step 04) ────────────────────────────────────────────────────
SLIPPAGE_GRID = [0.0, 0.05, 0.10, 0.20, 0.30]       # extra R per trade
MIN_PRICE_GRID = [0.0, 2.0, 5.0, 10.0]               # entry price floor (USD)
MIN_ADV_GRID = [0.0, 1e6, 5e6]                       # 20d avg dollar volume floor (USD)

# Specs shared by 04 and 05: name -> (max dist200 [price at least X below EMA200], max bars since Stage 2)
SPECS = {
    "BASE_all_signals": (None, None),
    "D15_S60": (0.15, 60),
    "D20_S60": (0.20, 60),
    "D25_S20": (0.25, 20),
    "D25_S40": (0.25, 40),
    "D25_S60": (0.25, 60),
    "D30_S60": (0.30, 60),
}

# ── Analysis periods ─────────────────────────────────────────────────────────
TRAIN = ("2022-01-31", "2023-12-31")
TEST = ("2024-01-01", "2026-01-31")
OOS26 = ("2026-02-01", "2026-12-31")    # new data from Work PC cache (true out-of-sample)
LEGACY_END = "2022-01-30"               # signals before this = older-history tickers only

WINSOR_R = 10.0                         # cap R at +10 (tames lottery-ticket outliers)
MIN_N = 100                             # min trades per period for a 'robust' flag
MIN_N_OOS = 30
BOOTSTRAP_REPS = 1000
SEED = 7

# ── Grids ────────────────────────────────────────────────────────────────────
X_BELOW = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]            # price at least X below EMA200
N_S2 = [10, 20, 40, 60, 90, 120, 180, 260, None]                       # Stage 2 within N bars (None=any)
STACKS = ["none", "e10>e20", "e10>e20>e50"]
BAND_LO = [-0.60, -0.50, -0.40, -0.35, -0.30, -0.25, -0.20, -0.15, -0.10]
BAND_HI = [-0.40, -0.35, -0.30, -0.25, -0.20, -0.15, -0.10, -0.05, 0.0]
BAND_N = [60, 120, None]

# ── Multi-entry / ATR-stop optimisation (steps 07-08) ────────────────────────
POOL_DIST_MAX = 0.15            # candidate pool: price >= 15% below EMA200 ...
POOL_S2_MAX = 60                # ... and Stage 2 within 60 bars (largest grid values)
ATR_GRID = [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0]
M_DIST = [0.15, 0.20, 0.25, 0.30, 0.35]
M_S2 = [10, 20, 40, 60]
M_MAX_PER_TICKER = [1, 3]       # 1 = one trade per ticker (as before); 3 = up to 3 concurrent same-ticker trades
M_GROSS = [5.0, 9.0]            # 5x = FCA retail 1:5; 9x ~ IC Markets limit seen on demo ("No money" 10019)
M_R_CAPS = [10.0]               # tail-robust R cap used for the grid; raw-R versions of the chosen combos are re-run at the end
M_COMMISSION = 0.0              # user request: raw-spread account, NO commission / swap / slippage in this study
M_SWAP_R = 0.0
M_SLIP_R = 0.0
M_SPLIT = "2025-01-01"          # selection on data before this date, validation on/after it
SIM_ATR = OUT / "sim_trades_atr.parquet"

# ── Trailing-stop optimisation (steps 09-10) ─────────────────────────────────
TRAIL_INIT = [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
TRAIL_MULT = [None, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0]         # chandelier trail = highest high since entry - mult x ATR(prev bar); None = no trail
T_DIST = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
T_S2 = [10, 20, 30, 40, 60]
T_MAX_PER_TICKER = [1, 2, 3]
T_GROSS = [5.0]
TRAIL_PARTS = OUT / "trail_parts_v2"
SIM_TRAIL = OUT / "sim_trades_trail.parquet"
