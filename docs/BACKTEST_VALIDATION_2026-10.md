# docs/BACKTEST_VALIDATION_2026-10.md
# APEX Backtest Validation & Live-Readiness Findings (2026-10-06)

Source: `ALGO-Stocks` (read-only). Run `universe_baseline_v1_20260224_2310` (full universe, 2,582 tickers, 2022-02 → 2026-01, 43k closed trades) and `gate_enabled_v1_20260404_1850`.

## 1. Headline vs robust metrics (full universe)

| Metric | Reported | Robust |
|---|---|---|
| E[R] | 0.198 | **0.030** ex-SSII; 0.019 winsorized @10R |
| Profit factor | 1.36 | **1.09** ex-SSII |
| Win rate | 36.2% | 35.6% |
| Median R | n/a | −0.38 |

Single trade SSII (entry $0.028, +6,784R) supplies the reported expectancy.

## 2. Edge by year (ex-SSII, signal year)

| Year | n | E[R] | Winsorized E[R] | Win % |
|---|---|---|---|---|
| 2022 | 5.6k | −0.09 | −0.09 | 32 |
| 2023 | 11.1k | 0.15 | 0.15 | 39 |
| 2024 | 13.1k | −0.01 | −0.02 | 34 |
| 2025 | 12.5k | 0.05 | 0.03 | 37 |
| 2026 (Jan) | 1.1k | −0.37 | −0.37 | 24 |

## 3. Filtered universe (1,251 tickers) — selection bias

- Reported PF 2.26 / E[R] 0.63 (0.31 ex-SSII). Filter (PF≥1, E[R]≥0, ≥5 trades) was fit on the **same** 2022–26 trades.
- Walk-forward (select on 2022–23 → test 2024–26): selected 966 tickers: **E[R] 0.01, PF 1.07**; rejected: E[R] 0.00, PF 1.01. No persistence.
- Do not quote PF 2.26 externally.

## 4. Spider gate

- Ex-SSII, blocked trades E[R] 0.039 vs allowed 0.029: no discrimination. Inconsistent by year.
- Gate run used `overlap_mode: scale_in`; baseline used `disabled`. Not matched.
- **Decision: keep `spider_gate.enabled: false`.**

## 5. Production parity (tools/parity_replay.py)

Production `SignalGenerator` replayed on backtest OHLCV, 150 random tickers, 2,568 backtest signals, 1,800 non-signal days.

| History window | Recall | False positives |
|---|---|---|
| 360 bars (old production cache) | **48.5%** | 0.0% |
| 1,275 bars (full) | **100.0%** | 0.06% |

Root cause: cache `outputsize=360` + `tail(lookback_days+60)` truncated Stage-2 memory lookback. Truncation skews mix toward Stage 1-origin signals (recall 81%) vs Stage 8-origin (33%). E[R] of both subsets ≈ 0 (−0.05 missed, +0.06 fired) — parity gap does not explain demo losses.

**Fix applied:** `lookback_days: 1300` (production.yaml), `LOOKBACK_DAYS = 1300` (build_td_cache.py). Requires full cache rebuild (`python tools/build_td_cache.py --force`).

## 6. Robustness slices (ex-SSII)

| Slice | n | Winsorized E[R] | PF | E[R] 2024+ |
|---|---|---|---|---|
| All | 43.4k | 0.019 | 1.09 | −0.010 |
| Price ≥ $5 | 41.0k | 0.000 | 1.04 | −0.029 |
| Price ≥ $10 | 37.1k | −0.005 | 1.03 | −0.030 |
| Price ≥ $20 | 29.1k | −0.006 | 1.03 | −0.037 |
| Stop distance <2% | 1.1k | −0.180 | 0.64 | −0.173 |
| Stop distance >10% | 6.9k | +0.180 | 1.42 | +0.229 |

- Min-price filter creates no edge. Leave `min_entry_price: 0.0`.
- Wide-stop (high-volatility) names positive in-sample, incl. 2024+. Hypothesis only; needs walk-forward before use.

## 7. Demo vs backtest

Demo: 24 closed trades, 0 wins. Backtest win rate 35.6% → P(0/24) ≈ 3e-5 if independent; clustered days raise it, but backtest days with ≤5% win rate are rare (3%). Treat as significant adverse evidence, not proof of broken code (execution audit found clean −1R stops).

## 7b. Demo update (2026-10-06, account 53003220, 97 trades)

| | n | Result |
|---|---|---|
| Closed | 56 | 5 wins, −$27.2k (30 stop-outs, 26 Stage-9 exits) |
| Open (marked to market) | 41 | 30 in profit, +$18.4k |
| Combined | 97 | win rate 36.1%, mean R −0.195, median −0.31, net −$8.9k |

- Earlier "0 wins in 24" was a **closed-only sample bias**: stops resolve in median ~6 days, winners run ~17 days (backtest hold medians). Winners were still open.
- Win rate 36.1% vs backtest 35.6%. Stop-out share 31% vs 33%. Stop-out mean R −1.087 vs backtest −1.074 → execution fidelity confirmed.
- Mean R −0.195 vs backtest +0.03: bootstrap P = 6.6% (SE ≈ 0.165). Not statistically distinguishable; open R is unrealised.
- Early cohort (09-08/09-09) ran with misconfigured risk; excluded-cohort result not materially different.
- Issues: DNA close-only at broker (retcode 10044) → add to `excluded_tickers`; 4 trades with stop <2% of price (one-off −1.2 to −1.8R fills; backtest <2% bucket PF 0.64).

## 8. Conclusion

Backtest does not support deploying real capital. Expected edge ≈ 0 before costs. Next: wide-stop walk-forward, cost model, parity re-test after cache rebuild, ≥100 demo trades on the corrected pipeline.
