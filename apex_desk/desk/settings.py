# apex_desk/desk/settings.py
"""Loads conf/desk.yaml + .env, resolves paths, and exposes the repo root on sys.path (READ-ONLY reuse of core/ and prod/)."""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

DESK_ROOT = Path(__file__).resolve().parents[1]          # .../apex_desk
REPO_ROOT = DESK_ROOT.parent                              # .../APEX-Stocks_algo-v2
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(DESK_ROOT / ".env")
except Exception:                                         # dotenv optional
    pass


@lru_cache(maxsize=1)
def cfg() -> Dict[str, Any]:
    with (DESK_ROOT / "conf" / "desk.yaml").open("r", encoding="utf-8") as f:
        c = yaml.safe_load(f)
    env_mode = os.environ.get("DESK_MODE")
    if env_mode:
        c["mode"] = env_mode
    return c


def db_path() -> Path:
    env = os.environ.get("DESK_DB")                      # tests / alternate DB
    p = Path(env) if env else DESK_ROOT / cfg()["paths"]["db"]
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def log_dir() -> Path:
    p = DESK_ROOT / cfg()["paths"]["log_dir"]
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_dir() -> Path:
    """Parquet cache (READ-ONLY). Prefers the production cache, then the Work-PC copy."""
    c = cfg()["data"].get("cache_dir")
    if c:
        return Path(c)
    cands = [
        REPO_ROOT / "data" / "raw" / "prices_daily" / "twelvedata" / "parquets",
        REPO_ROOT / "data" / "raw" / "work_pc_cache" / "prices_daily" / "twelvedata" / "parquets",
    ]
    best, best_n = cands[0], -1
    for p in cands:
        if p.exists():
            n = sum(1 for _ in p.glob("*.parquet"))
            if n > best_n:
                best, best_n = p, n
    return best


def mt5_creds() -> Dict[str, Any]:
    return {
        "server": os.environ.get("DESK_MT5_SERVER", ""),
        "login": int(os.environ.get("DESK_MT5_LOGIN", "0") or 0),
        "password": os.environ.get("DESK_MT5_PASSWORD", ""),
        "path": os.environ.get("DESK_MT5_TERMINAL_PATH", "") or None,
        "timeout_ms": 10000,
    }
