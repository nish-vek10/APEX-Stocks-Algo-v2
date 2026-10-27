# apex_desk/desk/brokers.py
"""Broker adapters. MT5Broker wraps the PRODUCTION execution helpers (read-only reuse). SimBroker = no-MT5 simulation for testing."""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd
import yaml

from desk import db, settings

log = logging.getLogger("desk.broker")


def load_symbol_map_cfg() -> Dict[str, Any]:
    local = settings.DESK_ROOT / "conf" / "symbol_map.yaml"
    path = local if local.exists() else settings.REPO_ROOT / "config" / "mt5_symbol_map.yaml"
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class BaseBroker:
    name = "base"
    send_environment = "paper"

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def equity(self) -> float: raise NotImplementedError
    def balance(self) -> float: return self.equity()
    def symbol_for(self, ticker: str) -> str: raise NotImplementedError
    def tick(self, symbol: str) -> Optional[SimpleNamespace]: raise NotImplementedError
    def resolve_volume(self, symbol: str, shares: float, stop_dist: float, risk_usd: float) -> Dict[str, Any]: raise NotImplementedError
    def send_entry(self, symbol: str, volume: float, stop: float, comment: str) -> Dict[str, Any]: raise NotImplementedError
    def send_close(self, symbol: str, ticket: int, volume: float, comment: str = "DESK_EXIT") -> Dict[str, Any]: raise NotImplementedError
    def live_tickets(self) -> Optional[set]: return None
    def closing_info(self, ticket: int, fallback: float) -> Dict[str, Any]: return {"price": fallback, "detail": "no history"}
    def trade_mode(self, symbol: str) -> str: return "full"        # full | long_only | short_only | close_only | disabled | unknown
    def candidates(self, ticker: str) -> List[str]:
        """Every broker symbol this ticker can trade as, preferred first (mapped symbol, then the other .NAS/.NYSE/-24 variants)."""
        s = self.symbol_for(ticker)
        return [s] if s else []


class MT5Broker(BaseBroker):
    """Uses prod.execution.* unchanged: get_live_tick, resolve_mt5_volume (clamps to broker volume_max), build_entry_request, send_order."""
    name = "mt5"

    def __init__(self, live: bool) -> None:
        from prod.execution.mt5_connector import MT5Connector
        self.live = live
        self.send_environment = "live" if live else "paper"
        self.conn = MT5Connector(settings.mt5_creds())
        self.symcfg = load_symbol_map_cfg()
        self.symbols = self.symcfg.get("symbols", {})
        self._resolved: Dict[str, str] = {}
        self.magic = int(settings.cfg()["execution"]["magic_number"])

    def connect(self) -> None:
        self.conn.connect()

    def disconnect(self) -> None:
        self.conn.disconnect()

    def equity(self) -> float:
        return self.conn.get_equity()

    def balance(self) -> float:
        return self.conn.get_balance()

    SUFFIXES = (".NAS-24", ".NYSE-24", ".NAS", ".NYSE")          # IC Markets naming: 24h CFDs first, then regular-hours

    def symbol_for(self, ticker: str) -> str:
        """Symbol map first; tickers not in the (static) map, e.g. new listings like BETR, are looked up on the broker by suffix."""
        s = self.symbols.get(ticker)
        if s:
            return s
        if ticker in self._resolved:
            return self._resolved[ticker]
        try:
            import MetaTrader5 as mt5
            for suf in self.SUFFIXES:
                if mt5.symbol_info(ticker + suf) is not None:
                    self._resolved[ticker] = ticker + suf              # only hits are cached (a miss may just be a dropped connection)
                    log.info("Symbol auto-resolved: %s -> %s (not in the symbol map)", ticker, ticker + suf)
                    return ticker + suf
        except Exception as exc:                           # noqa: BLE001
            log.debug("symbol lookup failed for %s: %s", ticker, exc)
        return ""

    def candidates(self, ticker: str) -> List[str]:
        """Mapped symbol first, then every other existing variant (.NAS-24 / .NYSE-24 / .NAS / .NYSE): some feeds are dead or close-only."""
        out: List[str] = []
        first = self.symbol_for(ticker)
        if first:
            out.append(first)
        try:
            import MetaTrader5 as mt5
            for suf in self.SUFFIXES:
                s = ticker + suf
                if s not in out and mt5.symbol_info(s) is not None:
                    out.append(s)
        except Exception as exc:                           # noqa: BLE001
            log.debug("candidate lookup failed for %s: %s", ticker, exc)
        return out

    def tick(self, symbol: str):
        from prod.execution.order_builder import get_live_tick
        t = get_live_tick(symbol)
        return None if t is None else SimpleNamespace(ask=float(t.ask), bid=float(t.bid))

    def resolve_volume(self, symbol, shares, stop_dist, risk_usd):
        from prod.execution.order_builder import resolve_mt5_volume
        ex = settings.cfg()["execution"]
        return resolve_mt5_volume(symbol, shares, stop_dist, risk_usd)

    def send_entry(self, symbol, volume, stop, comment):
        from prod.execution.order_builder import build_entry_request
        from prod.execution.order_executor import send_order
        req = build_entry_request(symbol, volume, stop, self.magic, self.symcfg, comment=comment)
        return send_order(req, self.send_environment)

    def send_close(self, symbol, ticket, volume, comment="DESK_EXIT"):
        import MetaTrader5 as mt5
        from prod.execution.order_builder import build_close_request
        from prod.execution.order_executor import send_order
        req = build_close_request(symbol, ticket, volume, mt5.ORDER_TYPE_BUY, self.magic, self.symcfg, comment=comment)
        return send_order(req, self.send_environment)

    def trade_mode(self, symbol: str) -> str:
        """Broker permission for opening a long: close_only / disabled / short_only block new entries (retcode 10044 otherwise)."""
        import MetaTrader5 as mt5
        try:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        except Exception:                                  # noqa: BLE001
            return "unknown"
        if info is None:
            return "unknown"
        return {mt5.SYMBOL_TRADE_MODE_DISABLED: "disabled", mt5.SYMBOL_TRADE_MODE_LONGONLY: "long_only",
                mt5.SYMBOL_TRADE_MODE_SHORTONLY: "short_only", mt5.SYMBOL_TRADE_MODE_CLOSEONLY: "close_only",
                mt5.SYMBOL_TRADE_MODE_FULL: "full"}.get(info.trade_mode, "unknown")

    def live_tickets(self) -> Optional[set]:
        import MetaTrader5 as mt5
        if self.send_environment == "paper":
            return None                                    # paper orders never reach the broker: nothing to reconcile
        pos = mt5.positions_get()
        return {p.ticket for p in pos} if pos else set()

    def closing_info(self, ticket, fallback):
        import MetaTrader5 as mt5
        out = {"price": fallback, "detail": "history unavailable -- stop price used"}
        try:
            deals = mt5.history_deals_get(position=ticket)
            if deals:
                outs = [d for d in deals if getattr(d, "entry", None) == mt5.DEAL_ENTRY_OUT]
                d = max(outs or deals, key=lambda z: z.time)
                out = {"price": float(d.price), "detail": d.comment or "broker close",
                       "time": datetime.fromtimestamp(d.time, tz=timezone.utc).isoformat()}
        except Exception as exc:                           # noqa: BLE001
            log.warning("history_deals_get failed: %s", exc)
        return out


class SimBroker(BaseBroker):
    """No MetaTrader5 needed. Prices = last cached close; fills instantly; native stops emulated from cache bars."""
    name = "sim"

    def __init__(self) -> None:
        self._eq = float(db.kv_get("equity", settings.cfg()["risk"]["default_equity"]))
        self._n = 900000

    def equity(self) -> float:
        return self._eq

    def symbol_for(self, ticker: str) -> str:
        return ticker

    def _last_close(self, ticker: str) -> float:
        p = settings.cache_dir() / f"{ticker}.parquet"
        df = pd.read_parquet(p, columns=["date", "close"]).sort_values("date")
        return float(df.close.iloc[-1])

    def tick(self, symbol):
        px = self._last_close(symbol)
        return SimpleNamespace(ask=px, bid=px)

    def resolve_volume(self, symbol, shares, stop_dist, risk_usd):
        v = float(math.floor(shares))
        if v < 1:
            return {"ok": False, "reason": "below_volume_min", "volume": 0.0}
        return {"ok": True, "volume": v, "contract_size": 1.0, "volume_step": 1.0, "volume_min": 1.0,
                "actual_shares": v, "actual_risk_dollars": v * stop_dist, "target_risk_dollars": risk_usd, "deviation_pct": 0.0}

    def send_entry(self, symbol, volume, stop, comment):
        self._n += 1
        t = self.tick(symbol)
        return {"success": True, "retcode": 10009, "order": self._n, "volume": volume, "price": t.ask, "comment": "sim_fill"}

    def send_close(self, symbol, ticket, volume, comment="DESK_EXIT"):
        t = self.tick(symbol)
        return {"success": True, "retcode": 10009, "order": ticket, "volume": volume, "price": t.bid, "comment": "sim_close"}

    def live_tickets(self):
        live = set()
        for p in db.q("SELECT * FROM positions WHERE status='open'"):
            try:
                df = pd.read_parquet(settings.cache_dir() / f"{p['ticker']}.parquet", columns=["date", "low"]).sort_values("date")
                after = df[pd.to_datetime(df.date) > pd.Timestamp(p["entry_date"])]
                if not (after.low <= p["stop_price"]).any():
                    live.add(p["ticket"])
            except Exception:                              # noqa: BLE001
                live.add(p["ticket"])
        return live

    def closing_info(self, ticket, fallback):
        return {"price": fallback, "detail": "sim native stop"}


def make_broker() -> BaseBroker:
    mode = settings.cfg()["mode"]
    creds = settings.mt5_creds()
    try:
        import MetaTrader5  # noqa: F401
        have_mt5 = True
    except ImportError:
        have_mt5 = False
    if mode == "live":
        if not have_mt5 or not creds["login"]:
            raise RuntimeError("mode=live needs the MetaTrader5 package and DESK_MT5_* credentials in apex_desk/.env")
        return MT5Broker(live=True)
    if have_mt5 and creds["login"]:
        return MT5Broker(live=False)                       # real quotes, simulated orders
    return SimBroker()
