# Dislocation Study: Findings (2026-10-22)

## Question
After a Stage 2 (sharp downtrend), is the Stage 6/7 Donchian breakout profitable only when price is stretched
a set % below the 200 EMA? Which distance is best?

## Data
Merged backtest candles + Work PC cache (2021 to 2026-10-15), 2,980 tickers, no gap. Production stage code, engine replica
(99.8% parity with the original backtest). Signals 2022-01-31 onward. TRAIN 2022-23, TEST 2024-Jan 2026, OOS26 Feb-Oct 2026.

## Decision
**Best distance: price at least 25% below the EMA200 at the breakout bar, with Stage 2 seen within the last 20 bars.**
Workable zone (plateau): 20% to 35% below. Shallower than 20% has no usable edge after costs. Deeper than 35% gets too few trades.

| Evidence (net of commission + swap) | Value |
|---|---|
| Net R, TRAIN / TEST / OOS26 | +0.24 / +0.56 / +0.33 |
| Trades (2022+) | 602 closed, 420 tickers |
| Bootstrap 95% CI, all periods | [+0.12, +0.61] (0.10R slippage: [+0.02, +0.35]) |
| Win rate | ~25% (payoff skew; long losing streaks expected) |
| Top-10 ticker share of positive R | 14% |
| Calendar years | 2022 -0.11R, then +0.64, +0.35, +0.72, +0.32 |

## Does not change
- Unfiltered baseline: net edge about -0.01R before slippage, negative after.
- Stage 2 recency is essential. Without it the distance filter adds little and turns negative past 35% below.
- EMA10>20>50 stack adds nothing. `e10>e20` is identical to no filter (signals already require it).

## Portfolio test ($100k, 1% risk, 75 cap, $2 price floor, 0.10R slippage, 5x gross cap)
- Filtered strategy: $177k final, CAGR +13%, max drawdown -65% (realized basis). 2022 was -58%.
- 0.5% risk: CAGR +8%, DD -41%. 0.25% risk: CAGR +5%, DD -23%.
- Baseline at 1% risk: account effectively wiped out.
- 75 positions at 1% risk can reach 7-12x gross notional unless capped. Open stop-risk peaks 60-77% of equity.

## Verdict
Signal exists but is thin and fragile (OOS26 about 45 trades, 720 combinations tried, edge disappears near 0.2R slippage).
Not ready to switch production. Forward-test log-only first.

## Recommended actions
1. Cap `max_open_positions` at 75 (done in config).
2. Add a price floor (>= $2) and an open stop-risk cap (about 20% of equity) before any size increase.
3. Add a log-only tag `dislocation_flag` (dist200 <= -25% and Stage 2 within 20 bars) to signals and trades on the demo.
   Review after about 100 flagged trades. Only then consider filtering or sizing up flagged signals.
4. Do not apply the filter to live capital until step 3 confirms a positive net R on new data.

## Files
`01`-`05` scripts, `study_config.py`, `lib_common.py`, `run_all.ps1`. Reports in `output/` (git-ignored):
Dislocation_Study_Report.xlsx, Cost_Stress_Report.xlsx, Portfolio_Sim_Report.xlsx.
