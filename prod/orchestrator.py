# prod/orchestrator.py
"""
APEX Master Production Orchestrator.

Broker dispatch via config/production.yaml -> broker: "ig" | "mt5"

IG mode  (default):
  Data    -> yfinance (via ig_fetcher.fetch_universe_ig)
  Connect -> IGConnector
  Execute -> ig_order_executor.send_order_ig

MT5 mode (legacy):
  Data    -> MetaTrader5 (via mt5_fetcher.fetch_universe)
  Connect -> MT5Connector
  Execute -> order_executor.send_order

All signal / stage / risk / state / position / monitoring code is
broker-agnostic and completely unchanged regardless of broker selection.

Position state compatibility note:
  PositionManager uses field names mt5_ticket / mt5_symbol (legacy naming).
  In IG mode these fields store ig_deal_id / ig_epic respectively.
  This is intentional -- avoids breaking the state schema.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.features.technicals.pipeline import IndicatorConfig, apply_indicators
from core.filters.spider_gate import SpiderGate
from core.utils.config_loader import (
    load_indicator_config,
    load_ig_epic_map,
    load_production_config,
    load_risk_config,
    load_spider_config,
    load_stage_config,
    load_symbol_map,
    resolve_ig_credentials,
    resolve_mt5_credentials,
)
from core.utils.logging import setup_logger
from prod.data.universe import (
    build_epic_map,
    build_symbol_map,
    get_ig_currency,
    get_ig_expiry,
    get_ticker_sector,
)
from prod.monitoring.alert import (
    alert_circuit_breaker,
    alert_error,
    alert_order_sent,
    alert_signal_found,
)
from prod.monitoring.logger import RunLogger
from prod.positions.portfolio_tracker import PortfolioTracker
from prod.positions.position_manager import PositionManager
from prod.risk.circuit_breaker import CircuitBreaker
from prod.risk.position_sizer import compute_position_size
from prod.signals.signal_generator import SignalGenerator
from prod.state.state_manager import StateManager

ROOT = Path(__file__).resolve().parents[1]
logger = logging.getLogger("orchestrator")


class APEXOrchestrator:
    """
    Master production orchestrator.
    Coordinates: data fetch -> signals -> gate -> sizing -> execution -> position management.
    Broker selected at init time from config/production.yaml  broker: "ig" | "mt5"
    """

    def __init__(self) -> None:
        self.root = ROOT
        self.prod_cfg = load_production_config(ROOT)
        self.risk_cfg = load_risk_config(ROOT)
        self.ind_cfg_dict = load_indicator_config(ROOT)
        self.stage_cfg = load_stage_config(ROOT)
        self.spider_cfg = load_spider_config(ROOT)

        self.environment = self.prod_cfg.get("environment", "paper")
        self.broker = self.prod_cfg.get("broker", "ig").lower()

        state_dir = ROOT / "state"
        log_dir = ROOT / "logs"
        setup_logger("apex", log_dir)

        self.state_mgr = StateManager(state_dir)
        self.pos_mgr = PositionManager(state_dir)
        self.portfolio = PortfolioTracker(state_dir)
        self.run_logger = RunLogger(log_dir)

        tickers = self.prod_cfg.get("universe", {}).get("tickers", [])

        # Broker-specific init
        if self.broker == "ig":
            self._init_ig(tickers)
        else:
            self._init_mt5(tickers)

        # Shared components
        self.spider_gate = SpiderGate(self.prod_cfg, ROOT)
        self.circuit_breaker = CircuitBreaker(self.prod_cfg, state_dir)
        self.signal_gen = SignalGenerator(
            self.prod_cfg, self.ind_cfg_dict, self.stage_cfg, state_dir,
        )

        logger.info(
            "APEX Orchestrator initialised | broker=%s | env=%s | tickers=%d | gate=%s",
            self.broker, self.environment, len(self.epic_map),
            self.prod_cfg.get("spider_gate", {}).get("enabled"),
        )

    # -------------------------------------------------------------------------
    # Broker init helpers
    # -------------------------------------------------------------------------

    def _init_ig(self, tickers: List[str]) -> None:
        from prod.execution.ig_connector import IGConnector
        self.ig_epic_cfg: Dict[str, Any] = load_ig_epic_map(ROOT)
        self.ig_creds: Dict[str, Any] = resolve_ig_credentials(self.prod_cfg)
        self.connector = IGConnector(self.ig_creds)
        self.epic_map: Dict[str, str] = build_epic_map(tickers, self.ig_epic_cfg)
        self.symbol_map: Dict[str, str] = self.epic_map  # shared alias

    def _init_mt5(self, tickers: List[str]) -> None:
        from prod.execution.mt5_connector import MT5Connector
        self.symbol_map_cfg: Dict[str, Any] = load_symbol_map(ROOT)
        self.mt5_creds: Dict[str, Any] = resolve_mt5_credentials(self.prod_cfg)
        self.connector = MT5Connector(self.mt5_creds)
        self.symbol_map = build_symbol_map(tickers, self.symbol_map_cfg)
        self.epic_map = self.symbol_map
        self.magic: int = self.prod_cfg.get("mt5", {}).get("magic_number", 20240001)

    # -------------------------------------------------------------------------
    # EOD Signal Run
    # -------------------------------------------------------------------------

    def run_signals(self) -> List[Dict[str, Any]]:
        """Fetch data, generate signals, persist to state."""
        logger.info("=== EOD SIGNAL RUN ===")

        lookback = self.prod_cfg.get("universe", {}).get("lookback_days", 300)

        if self.broker == "ig":
            universe_data = self._fetch_data_ig(lookback)
        else:
            with self.connector:
                tf = self.prod_cfg.get("universe", {}).get("timeframe", "D1")
                universe_data = self._fetch_data_mt5(tf, lookback)

        signals = self.signal_gen.generate_all(universe_data)

        for s in signals:
            self.run_logger.log_signal(s)
            alert_signal_found(s["ticker"], s["stage"], str(s["signal_date"]))

        self.state_mgr.set_pending_signals(signals)
        logger.info("Signal run complete: %d signals.", len(signals))
        return signals

    # -------------------------------------------------------------------------
    # Execution Run
    # -------------------------------------------------------------------------

    def run_execution(self) -> None:
        """Execute pending signals at next-day open."""
        logger.info("=== EXECUTION RUN ===")

        with self.connector:
            equity = self.connector.get_equity()
            today = datetime.now(timezone.utc).date()

            # Circuit breaker check
            cb = self.circuit_breaker.check(equity, today)
            if not cb["allowed"]:
                logger.critical("Circuit breaker: %s -- all execution halted.", cb["reason"])
                alert_circuit_breaker(cb["reason"])
                self.run_logger.log_circuit_breaker(cb["reason"])
                return

            # Process exits first
            self._process_exits(equity)

            # Portfolio cap check
            max_pos = self.prod_cfg.get("portfolio", {}).get("max_open_positions", 5)
            current_count = self.pos_mgr.count()
            available_slots = max_pos - current_count

            if available_slots <= 0:
                logger.info("Portfolio full (%d/%d) -- no new entries.", current_count, max_pos)
                self.state_mgr.clear_pending_signals()
                return

            # Process entries
            signals = self.state_mgr.get_pending_signals()
            executed = 0

            for signal in signals:
                if executed >= available_slots:
                    break

                ticker = signal["ticker"]
                if self.pos_mgr.is_open(ticker):
                    logger.debug("%s: already open -- skip.", ticker)
                    continue

                broker_sym = self.epic_map.get(ticker, "")
                if not broker_sym:
                    logger.warning("%s: no broker symbol/epic mapped -- skip.", ticker)
                    continue

                # Spider gate check
                spider_id = get_ticker_sector(ticker, self.spider_cfg)
                gate_result = self.spider_gate.check(spider_id, today, self.spider_cfg)
                self.run_logger.log_gate_decision(
                    ticker, gate_result["allowed"],
                    gate_result["reason"], gate_result["risk_mult"],
                )
                if not gate_result["allowed"]:
                    logger.info("%s: gate blocked (%s)", ticker, gate_result["reason"])
                    continue

                try:
                    if self.broker == "ig":
                        result, entry_open, stop_price, shares = self._execute_entry_ig(
                            signal, broker_sym, ticker, equity, gate_result,
                        )
                    else:
                        result, entry_open, stop_price, shares = self._execute_entry_mt5(
                            signal, broker_sym, ticker, equity, gate_result,
                        )

                    if result["success"]:
                        fill_price = result.get("price", entry_open)
                        # deal_id for IG, order ticket for MT5 -- stored in mt5_ticket field
                        order_id = result.get("deal_id") or result.get("order", 0)
                        self.pos_mgr.open_position(
                            ticker, order_id,
                            fill_price, stop_price,
                            shares, signal, broker_sym,
                        )
                        alert_order_sent(ticker, shares, fill_price, self.environment)
                        self.run_logger.log_position_open(
                            ticker, fill_price, stop_price, shares,
                        )
                        executed += 1
                    else:
                        logger.error(
                            "%s: order failed -- reason=%s",
                            ticker, result.get("reason") or result.get("retcode"),
                        )

                except Exception as exc:
                    logger.error("%s: execution error -- %s", ticker, exc, exc_info=True)
                    alert_error(ticker, str(exc))
                    self.run_logger.log_error(ticker, str(exc))

            self.state_mgr.clear_pending_signals()
            self.state_mgr.mark_execution_complete()
            self.pos_mgr.update_days_held()

        summary = self.portfolio.summary()
        self.run_logger.log_run_summary(summary)
        logger.info("Execution run complete. Portfolio: %s", summary)

    # -------------------------------------------------------------------------
    # Exit Processing
    # -------------------------------------------------------------------------

    def _process_exits(self, equity: float) -> None:
        """Check all open positions for exit conditions."""
        open_positions = self.pos_mgr.get_open()
        time_stop = self.prod_cfg.get("exit", {}).get("time_stop_days", 10)

        for pos in open_positions:
            ticker = pos["ticker"]
            broker_sym = pos["mt5_symbol"]   # stores ig_epic in IG mode
            order_id = pos["mt5_ticket"]     # stores ig_deal_id in IG mode
            shares = pos["shares"]
            stop_price = pos["stop_price"]
            days_held = pos.get("days_held", 0)

            # Current price
            try:
                if self.broker == "ig":
                    prices = self.connector.get_live_price(broker_sym)
                    current_price = prices["bid"]
                else:
                    import MetaTrader5 as mt5
                    tick = mt5.symbol_info_tick(broker_sym)
                    current_price = float(tick.bid) if tick else pos["entry_price"]
            except Exception:
                current_price = pos["entry_price"]

            exit_reason: Optional[str] = None

            if current_price > 0 and current_price <= stop_price:
                exit_reason = "stop_hit"
            elif days_held >= time_stop:
                exit_reason = "time_stop"

            if exit_reason is None:
                continue

            try:
                if self.broker == "ig":
                    result = self._execute_exit_ig(
                        ticker, broker_sym, str(order_id), float(shares),
                    )
                else:
                    result = self._execute_exit_mt5(
                        ticker, broker_sym, int(order_id), float(shares),
                    )

                if result["success"]:
                    exit_price = result.get("price", current_price)
                    trade = self.pos_mgr.close_position(ticker, exit_price, exit_reason)
                    if trade:
                        self.portfolio.record(trade)
                        self.circuit_breaker.record_trade_result(trade.get("pnl_total", 0))
                        self.run_logger.log_position_close(
                            ticker, exit_price,
                            trade.get("pnl_total", 0),
                            trade.get("pnl_r", 0),
                            exit_reason,
                        )

            except Exception as exc:
                logger.error("%s: exit error -- %s", ticker, exc, exc_info=True)
                alert_error(ticker, "exit: %s" % exc)

    # -------------------------------------------------------------------------
    # IG Execution Helpers
    # -------------------------------------------------------------------------

    def _fetch_data_ig(self, lookback: int) -> Dict[str, pd.DataFrame]:
        from prod.data.ig_fetcher import fetch_universe_ig
        return fetch_universe_ig(self.epic_map, lookback)

    def _execute_entry_ig(
        self,
        signal: Dict[str, Any],
        epic: str,
        ticker: str,
        equity: float,
        gate_result: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], float, float, int]:
        """
        Fetch live ask, recompute stop, size position, build + send IG entry order.
        Returns (result, entry_open, stop_price, shares).
        """
        from prod.execution.ig_order_builder import build_entry_request_ig
        from prod.execution.ig_order_executor import send_order_ig

        # Live ask price from IG
        prices = self.connector.get_live_price(epic)
        entry_open = prices["ask"] if prices["ask"] > 0 else float(signal.get("close", 0))

        # Recompute stop at live entry price
        atr = float(signal.get("atr", 0))
        stop_cfg = self.prod_cfg.get("stop", {})
        atr_mult = float(stop_cfg.get("atr_multiplier", 2.0))
        floor_pct = float(stop_cfg.get("floor_pct", 0.005))
        stop_price = max(
            entry_open - atr * atr_mult,
            entry_open * (1 - floor_pct),
        )

        signal["entry_open"] = entry_open
        signal["stop_price"] = stop_price

        # Position sizing (broker-agnostic)
        sizing = compute_position_size(
            signal, equity, self.prod_cfg, self.risk_cfg,
            gate_risk_mult=gate_result["risk_mult"],
        )
        shares = sizing["shares"]
        if shares <= 0:
            logger.info("%s: sizing = 0 shares -- skip.", ticker)
            return {"success": False, "reason": "zero_size"}, entry_open, stop_price, 0

        currency = get_ig_currency(ticker, self.ig_epic_cfg)
        expiry = get_ig_expiry(ticker, self.ig_epic_cfg)

        req = build_entry_request_ig(
            epic=epic,
            size=float(shares),
            stop_price=stop_price,
            currency_code=currency,
            expiry=expiry,
        )
        result = send_order_ig(
            req,
            ig_service=self.connector.service,
            environment=self.environment,
        )
        self.run_logger.log_order_sent(ticker, {
            "retcode": result.get("reason"),
            "success": result.get("success"),
            "volume": result.get("volume"),
            "price": result.get("price"),
        })
        return result, entry_open, stop_price, shares

    def _execute_exit_ig(
        self,
        ticker: str,
        epic: str,
        deal_id: str,
        shares: float,
    ) -> Dict[str, Any]:
        from prod.execution.ig_order_builder import build_close_request_ig
        from prod.execution.ig_order_executor import send_order_ig

        req = build_close_request_ig(
            deal_id=deal_id,
            epic=epic,
            size=shares,
            comment="APEX_EXIT",
        )
        return send_order_ig(
            req,
            ig_service=self.connector.service,
            environment=self.environment,
        )

    # -------------------------------------------------------------------------
    # MT5 Execution Helpers (legacy -- logic unchanged)
    # -------------------------------------------------------------------------

    def _fetch_data_mt5(self, tf: str, lookback: int) -> Dict[str, pd.DataFrame]:
        from prod.data.mt5_fetcher import fetch_universe
        return fetch_universe(self.symbol_map, tf, lookback)

    def _execute_entry_mt5(
        self,
        signal: Dict[str, Any],
        mt5_sym: str,
        ticker: str,
        equity: float,
        gate_result: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], float, float, int]:
        from prod.execution.order_builder import build_entry_request
        from prod.execution.order_executor import send_order
        import MetaTrader5 as mt5

        tick = mt5.symbol_info_tick(mt5_sym)
        entry_open = float(tick.ask) if tick else float(signal.get("close", 0))

        atr = float(signal.get("atr", 0))
        stop_cfg = self.prod_cfg.get("stop", {})
        atr_mult = float(stop_cfg.get("atr_multiplier", 2.0))
        floor_pct = float(stop_cfg.get("floor_pct", 0.005))
        stop_price = max(
            entry_open - atr * atr_mult,
            entry_open * (1 - floor_pct),
        )

        signal["entry_open"] = entry_open
        signal["stop_price"] = stop_price

        sizing = compute_position_size(
            signal, equity, self.prod_cfg, self.risk_cfg,
            gate_risk_mult=gate_result["risk_mult"],
        )
        shares = sizing["shares"]
        if shares <= 0:
            return {"success": False, "retcode": 0, "deal_id": 0}, entry_open, stop_price, 0

        req = build_entry_request(
            mt5_sym, shares, stop_price,
            self.magic, self.symbol_map_cfg,
        )
        result = send_order(req, self.environment)
        self.run_logger.log_order_sent(ticker, result)
        result["deal_id"] = result.get("order", 0)
        return result, entry_open, stop_price, shares

    def _execute_exit_mt5(
        self,
        ticker: str,
        mt5_sym: str,
        ticket: int,
        shares: float,
    ) -> Dict[str, Any]:
        from prod.execution.order_builder import build_close_request
        from prod.execution.order_executor import send_order
        import MetaTrader5 as mt5

        req = build_close_request(
            mt5_sym, ticket, shares,
            mt5.ORDER_TYPE_BUY,
            self.magic, self.symbol_map_cfg,
            comment="APEX_EXIT",
        )
        result = send_order(req, self.environment)
        result["deal_id"] = ticket
        return result
