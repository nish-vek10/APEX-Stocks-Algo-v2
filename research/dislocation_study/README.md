# Dislocation Study (research/dislocation_study)

**Question.** After a Stage 2 (sharp downtrend), is the Stage 6/7 Donchian breakout profitable
*only* when price is stretched a certain % **below the 200 EMA**, and which distance (or band) is
best and stable? Optionally also require the EMA10 > 20 > 50 line-up.

**Read-only.** Nothing in `APEX-Stocks_algo-v2` (config/state/prod) or `ALGO-Stocks` is modified.
All outputs go to `output/` (git-ignored).

## Run (PowerShell)
```
cd C:\Users\ravil\PycharmProjects\APEX-Stocks_algo-v2\research\dislocation_study
.\run_all.ps1
```
Requires: `pandas numpy pyarrow pyyaml openpyxl` (already in your env). Paths and every
parameter live in `study_config.py`. Expected runtime: step 1 ~3-6 min, step 2 ~3-5 min, step 3 ~1-2 min.
Step 1 is resumable (delete `output/shards/` to rebuild from scratch).

| Step | Script | Does |
|---|---|---|
| 1 | `01_build_merged_features.py` | Merges backtest candles (to 2026-01) with the Work PC cache (to 2026-10); rebuilds indicators, stages, signals with the production code (100% parity validated) |
| 2 | `02_simulate_trades.py` | Exact replica of ALGO-Stocks `engine.simulate_trade`, one position per ticker; writes the parity proof vs the original backtest |
| 3 | `03_dislocation_study.py` | Distance-below-EMA200 study, grids, robustness, bootstrap; writes the Excel report |

## Output: `output/Dislocation_Study_Report.xlsx`
Index, Summary, Baselines, A_Distance_Bands, A2_Matrix_MeanR / _N, B_Threshold_Grid, C_Band_Grid,
D_Top_Candidates, E_Yearly, F_Concentration, Stages, Parity.

## Method safeguards
- Net of commission ($0.04/share round trip, change in config) and optional extra slippage.
- R winsorized at +10 (one lottery ticker cannot drive a result).
- TRAIN 2022-23 / TEST 2024-26-01 / OOS26 (Feb-Oct 2026, new data). Rank = min(train, test).
- Cluster (signal-date) bootstrap CIs; plateau score (neighbour cells) on the threshold grid.
- The grid has hundreds of combinations: some will look good by chance. Trust a candidate only if
  positive in all periods, on a plateau, CI above 0 and not concentrated in a few tickers.

## Stages (production code, order of checks 2,3,4,5,7,6,9,8)
1 Not Eligible | 2 Sharp Downtrend | 3 Downtrend | 4 Below Zone | 5 Lower Zone |
6 Breakout | 7 Breakout Confirmed | 8 In-Zone | 9 In-Zone Fading (exit trigger).
Stage 7 requires EMA10 > EMA20 and close > EMA50; it does **not** require EMA20 > EMA50 (the
full line-up is tested as `stack = e10>e20>e50`).

## Steps 4-5 (added 2026-10-19)
| Step | Script | Does |
|---|---|---|
| 4 | `04_cost_stress.py` | Spec x slippage (0-0.30R) x min price x min 20d $ volume; net R per period + bootstrap CI; breakeven slippage. Output `output/Cost_Stress_Report.xlsx` |
| 5 | `05_portfolio_sim.py` | $100k account, 1% risk of current equity, 75-position cap, 20% max position value, one position per ticker, same-day priority (alpha vs deepest), gross-exposure cap (none / 5x), slippage, R cap. Output `output/Portfolio_Sim_Report.xlsx` |

Shared: `lib_common.py`. All account/risk/scenario parameters live in `study_config.py`.
Notes: `e10>e20` stack == `none` (signals already require EMA10>EMA20). A price floor (`PORT_MIN_PRICE`, $2) is applied
because one $0.03 stock (SSII) returned +6,756R. Portfolio equity is REALIZED equity (open P&L not marked), so reported
drawdowns are a lower bound; `peak_open_risk_pct` and `peak_gross_x` show the unrealized envelope.
