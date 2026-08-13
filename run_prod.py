# run_prod.py
"""
APEX Production Runner
======================
Usage:
    python run_prod.py --mode signals       # EOD: generate signals
    python run_prod.py --mode execution     # AM:  execute pending signals + process exits
    python run_prod.py --mode full          # Full cycle: signals + execution (for testing)
    python run_prod.py --mode status        # Print current positions + portfolio summary

Environment:
    Set in config/production.yaml:  environment: "paper" | "live"
    MT5 credentials via .env file (see .env.example)
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from core.utils.logging import setup_logger
from prod.orchestrator import APEXOrchestrator

logger = logging.getLogger("run_prod")


def main() -> None:
    parser = argparse.ArgumentParser(description="APEX Production Runner")
    parser.add_argument(
        "--mode",
        choices=["signals", "execution", "full", "status"],
        default="status",
        help="Run mode",
    )
    args = parser.parse_args()

    setup_logger("apex", ROOT / "logs", console=True)
    logger.info(f"APEX starting | mode={args.mode}")

    orch = APEXOrchestrator()

    if orch.environment == "live":
        print("\n" + "=" * 60)
        print("  ⚠  LIVE MODE ACTIVE — REAL ORDERS WILL BE PLACED  ⚠")
        print("=" * 60)
        confirm = input("Type 'CONFIRM' to proceed: ")
        if confirm.strip() != "CONFIRM":
            print("Aborted.")
            sys.exit(0)

    if args.mode == "signals":
        signals = orch.run_signals()
        print(f"\n[DONE] {len(signals)} signal(s) generated and persisted.")

    elif args.mode == "execution":
        orch.run_execution()
        print("\n[DONE] Execution run complete.")

    elif args.mode == "full":
        signals = orch.run_signals()
        print(f"\n[SIGNALS] {len(signals)} generated.")
        orch.run_execution()
        print("[EXECUTION] Complete.")

    elif args.mode == "status":
        positions = orch.pos_mgr.get_open()
        summary = orch.portfolio.summary()
        state = orch.state_mgr.get_state()

        print("\n─── APEX STATUS ─────────────────────────────────────────")
        print(f"Environment : {orch.environment.upper()}")
        print(f"Open positions : {len(positions)}")
        for p in positions:
            print(
                f"  {p['ticker']:8s} | entry={p['entry_price']:.2f} "
                f"| stop={p['stop_price']:.2f} | days={p.get('days_held', 0)}"
            )
        print(f"\nPortfolio summary : {summary}")
        print(f"Pending signals   : {len(state.get('pending_signals', []))}")
        print(f"Last signal run   : {state.get('last_signal_run', 'never')}")
        print(f"Last exec run     : {state.get('last_execution_run', 'never')}")
        print("─────────────────────────────────────────────────────────\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
    except Exception as exc:
        logger.critical(f"Fatal error: {exc}", exc_info=True)
        sys.exit(1)
