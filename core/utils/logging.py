# core/utils/logging.py
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


def setup_logger(
    name: str,
    log_dir: Path,
    level: int = logging.INFO,
    console: bool = True,
) -> logging.Logger:
    """
    Configure logging for the ENTIRE process, not just a logger named
    `name`. Every module in this codebase calls `logging.getLogger(<its
    own bare name>)` -- e.g. "order_builder", "signal_generator",
    "scheduler" -- none of which are children of a logger literally named
    "apex" in Python's dotted-name hierarchy, so attaching handlers only
    to `logging.getLogger("apex")` (the old behavior) left every other
    module's .info()/.debug() calls with no handler anywhere in their
    ancestor chain. They'd propagate to the root logger, which had none
    either, so Python's built-in "handler of last resort" silently
    swallowed everything below WARNING -- explains why scheduler.py's
    INFO-level startup banner never appeared (looked "stuck" but was
    actually running fine) while WARNING/ERROR lines from other modules
    did leak through via that stderr fallback. Fixed 2026-09-03 by
    attaching handlers to the ROOT logger instead, since every bare
    module logger propagates there by default -- `name` is now only used
    for the log filename, not for handler routing.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = log_dir / f"{name}_{date_str}.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = lambda *args: datetime.now(timezone.utc).timetuple()

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        root.addHandler(ch)

    # Root now catches everything process-wide, including chatty
    # third-party libraries -- keep those at WARNING so real APEX log
    # lines aren't buried.
    for noisy in ("urllib3", "requests", "MetaTrader5", "twelvedata"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger(name)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
