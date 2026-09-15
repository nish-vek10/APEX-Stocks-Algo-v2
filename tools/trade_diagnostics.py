# tools/trade_diagnostics.py
"""
APEX Trade Diagnostics — read-only.

Joins closed trades from state/trade_log.json against the per-signal
stage/transition_from context recorded in logs/apex_*.log (the raw
"SIGNAL: TICKER | date=... | stage=... | ... | transition_from=..."
lines emitted by signal_generator.py), so every closed trade's outcome
can be inspected against the setup that produced it.

Touches nothing: no MT5 connection, no writes to positions.json or
run_state.json. Safe to run at any time, including while scheduler.py
or a live run_prod.py process is active.

Usage:
    python tools/trade_diagnostics.py
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
TRADE_LOG = ROOT / "state" / "trade_log.json"
LOG_DIR = ROOT / "logs"

SIGNAL_LINE_RE = re.compile(
    r"SIGNAL:\s+(?P<ticker>\S+)\s+\|\s+date=(?P<date>\S+)\s+\|\s+"
    r"stage=(?P<stage>\S+)\s+\|\s+close=(?P<close>\S+)\s+\|\s+"
    r"atr=(?P<atr>\S+)\s+\|\s+stop_est=(?P<stop_est>\S+)\s+\|\s+"
    r"transition_from=(?P<transition_from>\S+)"
)


def load_trades() -> list[Dict[str, Any]]:
    if not TRADE_LOG.exists():
        print(f"[!] {TRADE_LOG} not found — no closed trades yet.")
        return []
    return json.loads(TRADE_LOG.read_text(encoding="utf-8"))


def build_signal_index() -> Dict[Tuple[str, str], Dict[str, Any]]:
    """(ticker, signal_date[:10]) -> {stage, transition_from, close, atr, stop_est}"""
    index: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for log_file in sorted(LOG_DIR.glob("apex_*.log")):
        with log_file.open(encoding="utf-8", errors="ignore") as f:
            for line in f:
                m = SIGNAL_LINE_RE.search(line)
                if not m:
                    continue
                key = (m.group("ticker"), m.group("date")[:10])
                index[key] = {
                    "stage": m.group("stage"),
                    "transition_from": m.group("transition_from"),
                    "close": m.group("close"),
                    "atr": m.group("atr"),
                    "stop_est": m.group("stop_est"),
                }
    return index


def find_signal_context(
    trade: Dict[str, Any], index: Dict[Tuple[str, str], Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    ticker = trade.get("ticker", "")
    sig_date = str(trade.get("signal_date", ""))[:10]
    return index.get((ticker, sig_date))


def main() -> None:
    trades = load_trades()
    if not trades:
        return

    index = build_signal_index()

    rows = []
    for t in trades:
        ctx = find_signal_context(t, index) or {}
        rows.append({
            "ticker": t.get("ticker", ""),
            "signal_date": str(t.get("signal_date", ""))[:10],
            "entry_date": t.get("entry_date", ""),
            "transition_from": ctx.get("transition_from", "?"),
            "stage_at_signal": ctx.get("stage", "?"),
            "entry_price": t.get("entry_price", 0.0),
            "stop_price": t.get("stop_price", 0.0),
            "exit_price": t.get("exit_price", 0.0),
            "exit_reason": t.get("exit_reason", ""),
            "pnl_r": t.get("pnl_r", 0.0),
            "pnl_total": t.get("pnl_total", 0.0),
        })

    # ── Per-trade table ────────────────────────────────────────────────
    header = (
        f"{'TICKER':<8} {'SIG_DATE':<11} {'FROM':<10} {'ENTRY':>9} {'STOP':>9} "
        f"{'EXIT':>9} {'REASON':<22} {'R':>7} {'PNL':>10}"
    )
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['ticker']:<8} {r['signal_date']:<11} {r['transition_from']:<10} "
            f"{r['entry_price']:>9.2f} {r['stop_price']:>9.2f} {r['exit_price']:>9.2f} "
            f"{r['exit_reason']:<22} {r['pnl_r']:>7.2f} {r['pnl_total']:>10.2f}"
        )

    # ── Aggregate by transition_from ──────────────────────────────────
    print()
    print("=== Aggregate by transition_from ===")
    by_from: Dict[str, list] = {}
    for r in rows:
        by_from.setdefault(r["transition_from"], []).append(r["pnl_r"])
    for key, r_list in sorted(by_from.items()):
        n = len(r_list)
        avg_r = sum(r_list) / n
        wins = sum(1 for x in r_list if x > 0)
        print(f"  {key:<10} n={n:<3} win_rate={wins/n:.0%}  avg_r={avg_r:+.3f}")

    # ── Aggregate by exit_reason ───────────────────────────────────────
    print()
    print("=== Aggregate by exit_reason ===")
    by_reason: Dict[str, list] = {}
    for r in rows:
        by_reason.setdefault(r["exit_reason"], []).append(r["pnl_r"])
    for key, r_list in sorted(by_reason.items()):
        n = len(r_list)
        avg_r = sum(r_list) / n
        print(f"  {key:<25} n={n:<3} avg_r={avg_r:+.3f}")

    print()
    print(f"Total trades: {len(rows)}")


if __name__ == "__main__":
    main()
