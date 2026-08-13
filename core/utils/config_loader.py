# core/utils/config_loader.py
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

import yaml


def load_yaml(path: Path) -> Dict[str, Any]:
    """Load a YAML file, expanding env vars in string values."""
    with path.open("r", encoding="utf-8") as f:
        raw = f.read()
    data = yaml.safe_load(raw)
    return _expand_env(data)


def _expand_env(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _expand_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_env(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("${") and obj.endswith("}"):
        key = obj[2:-1]
        return os.environ.get(key, obj)
    return obj


def load_production_config(root: Path) -> Dict[str, Any]:
    return load_yaml(root / "config" / "production.yaml")


def load_risk_config(root: Path) -> Dict[str, Any]:
    return load_yaml(root / "config" / "risk.yaml")


def load_indicator_config(root: Path) -> Dict[str, Any]:
    return load_yaml(root / "config" / "indicators.yaml")


def load_stage_config(root: Path) -> Dict[str, Any]:
    return load_yaml(root / "config" / "stages.yaml")


def load_spider_config(root: Path) -> Dict[str, Any]:
    return load_yaml(root / "config" / "spiders.yaml")


def load_symbol_map(root: Path) -> Dict[str, Any]:
    return load_yaml(root / "config" / "mt5_symbol_map.yaml")


def resolve_mt5_credentials(prod_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Pull MT5 credentials from env vars, fall back to config values."""
    mt5 = prod_cfg.get("mt5", {})
    return {
        "server": os.environ.get("MT5_SERVER", mt5.get("server", "")),
        "login": int(os.environ.get("MT5_LOGIN", mt5.get("login", 0))),
        "password": os.environ.get("MT5_PASSWORD", mt5.get("password", "")),
        "timeout_ms": mt5.get("timeout_ms", 10000),
        "magic_number": mt5.get("magic_number", 20240001),
    }


def resolve_ig_credentials(prod_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull IG Group API credentials from env vars, fall back to config values.
    Priority: environment variable > production.yaml ig: section.
    """
    ig = prod_cfg.get("ig", {})
    return {
        "identifier": os.environ.get("IG_IDENTIFIER", ig.get("identifier", "")),
        "password":   os.environ.get("IG_PASSWORD",   ig.get("password", "")),
        "api_key":    os.environ.get("IG_API_KEY",     ig.get("api_key", "")),
        "acc_number": os.environ.get("IG_ACCOUNT_ID",  ig.get("acc_number", "")),
        "acc_type":   os.environ.get("IG_ACC_TYPE",    ig.get("acc_type", "DEMO")),
    }


def load_ig_epic_map(root: Path) -> Dict[str, Any]:
    """Load config/ig_epic_map.yaml."""
    return load_yaml(root / "config" / "ig_epic_map.yaml")
