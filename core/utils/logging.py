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
    log_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    log_file = log_dir / f"{name}_{date_str}.log"

    fmt = logging.Formatter(
        "%(asctime)s | %(name)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = lambda *args: datetime.now(timezone.utc).timetuple()

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.handlers.clear()

    fh = logging.FileHandler(log_file, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    if console:
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)

    logger.propagate = False
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
