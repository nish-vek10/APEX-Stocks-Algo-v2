# prod/orchestrator.py
"""
APEX Master Production Orchestrator.

Broker dispatch via config/production.yaml -> broker: "ig" | "mt5"

IG mode  (default):
  Data    -> TwelveData cache (via twelvedata_fetcher.fetch_universe_from_cache)
             -- EXACT backtest data source parity (ALGO-Stocks used TwelveData
             exclusively). Cache built/refreshed by tools/build_td_cache.py,
             run after EOD close. Falls back to live TwelveData per-ticker for
             any cache miss. yfinance (ig_fetcher.py) kept in the codebase as
             an emergency fallback only -- not used by default anymore.
  Connect -> IGConnector
  Execute -> ig_order_executor.send_order_ig

MT5 mode:
  Data    -> TwelveData cache (same fetch_universe_from_cache as IG mode --
             exact backtest data source parity). MT5 (mt5_fetcher.py) is
             NEVER used for signal/indicator calculation, only for:
               - live tick price at order time (entry ask / exit bid)
               - order placement with native broker-side SL
               - lot-size resolution against the symbol's actual
                 contract_size/volume_step/volume_min (order_builder.
                 resolve_mt5_volume) -- MT5 lots != IG "shares" units
  Connect -> MT5Connector
  Execute -> order_executor.send_order

All signal / stage / risk / state / position / monitoring code is
broker-agnostic and completely unchanged regardless of broker selection.
Position cap: config/production.yaml portfolio.max_open_positions (100).
Risk per trade: config/risk.yaml equity_pct.risk_pct_per_trade (1%).

Position state compatibility note:
  PositionManager uses field names mt5_ticket / mt5_symbol (legacy naming).
  In IG mode these fields store ig_deal_id / ig_epic respectively.
  This is intentional -- avoids breaking the state schema.
"""
from __future__ import annotations

import logging
import math
import os
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
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
    alert_order_rejected,
    alert_order_sent,
    alert_position_closed,
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

        # Data is ALWAYS TwelveData cache regardless of broker (see
        # _fetch_data_mt5 docstring) -- no MT5/IG connection needed for
        # signal calculation, only for execution.
        if self.broker == "ig":
            universe_data = self._fetch_data_ig(lookback)
        else:
            universe_data = self._fetch_data_mt5("D1", lookback)

        raw_signals = self.signal_gen.generate_all(universe_data)

        # Drop any (ticker, signal_date) already fired in a previous run --
        # generate() has no memory of its own and will re-detect the same
        # transition every time it's pointed at an unchanged cache (see
        # StateManager.has_fired_signal() docstring). Without this, a
        # scheduler retry/misfire or a manual re-run replays the exact
        # same entries.
        signals = []
        skipped_dupes = []
        for s in raw_signals:
            if self.state_mgr.has_fired_signal(s["ticker"], str(s["signal_date"])[:10]):
                skipped_dupes.append(s["ticker"])
                continue
            signals.append(s)
        if skipped_dupes:
            logger.info(
                "Skipped %d already-fired signal(s) (unchanged cache since last run): %s",
                len(skipped_dupes), skipped_dupes,
            )

        for s in signals:
            self.run_logger.log_signal(s)
            alert_signal_found(s["ticker"], s["stage"], str(s["signal_date"]), s.get("stage_name", ""))

        self.state_mgr.set_pending_signals(signals)
        self.state_mgr.record_fired_signals(signals)

        # Stage 9 (In-Zone Fading) exit check for currently-open positions --
        # matches backtest exactly: "signal observed at EOD close, exit sent
        # at next morning's open" (ALGO-Stocks backtest/engine.py exit #3).
        self._check_stage9_exits(universe_data)

        logger.info("Signal run complete: %d signals.", len(signals))
        return signals

    def _check_stage9_exits(self, universe_data: Dict[str, pd.DataFrame]) -> None:
        """
        For every currently open position, classify today's stage using the
        same universe_data already fetched for signals (no extra fetch).
        Tickers landing on Stage 9 today are queued for exit at tomorrow's
        open by _process_exits -- matches backtest exit_reason=stage9_exit.
        """
        stage9_tickers: List[str] = []
        for pos in self.pos_mgr.get_open():
            ticker = pos["ticker"]
            df = universe_data.get(ticker)
            if df is None or df.empty:
                continue
            stage = self.signal_gen.current_stage(ticker, df)
            if stage == 9:
                stage9_tickers.append(ticker)

        self.state_mgr.set_pending_stage9_exits(stage9_tickers)
        if stage9_tickers:
            logger.info("Stage 9 fade detected on %d open position(s): %s", len(stage9_tickers), stage9_tickers)

    @staticmethod
    def _is_nyse_regular_session() -> bool:
        """
        True iff "now" falls within NYSE regular trading hours (09:30-16:00
        ET, Mon-Fri), using an IANA timezone so this is automatically DST-
        correct year-round (see scheduler.py for the matching cron trigger).

        Does NOT account for NYSE market holidays (Thanksgiving, Christmas,
        etc.) -- there is no holiday calendar wired in yet. On a holiday
        this will incorrectly report the session as open; MT5/IG order
        rejection is the current backstop for that gap.
        """
        now_et = datetime.now(ZoneInfo("America/New_York"))
        if now_et.weekday() >= 5:  # Sat=5, Sun=6
            return False
        return dt_time(9, 30) <= now_et.time() <= dt_time(16, 0)

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

            # Process exits first -- never gated on market-hours: closing
            # existing risk (stop/stage9/time-stop) should never be delayed.
            self._process_exits(equity)

            # NYSE market-hours guard for NEW ENTRIES only. This is a safety
            # net behind scheduler.py's America/New_York cron (which already
            # fires at 09:31 ET) -- catches misfires (e.g. a missed-run
            # catch-up after a server restart firing hours late).
            if not self._is_nyse_regular_session():
                # Do NOT clear pending_signals here -- this guard fires
                # whenever run_execution() is invoked outside 09:30-16:00 ET
                # (e.g. testing --mode full mid-afternoon, or a scheduler
                # misfire recovering late). The signals are still valid for
                # "next AM open" and must survive until an execution run
                # actually happens during market hours. Found 2026-09-03:
                # this branch was wiping the day's entire signal queue,
                # meaning tomorrow's real 09:31 ET execution run would find
                # nothing queued and silently execute zero orders.
                logger.warning(
                    "Outside NYSE regular hours (09:30-16:00 ET, Mon-Fri) -- "
                    "no new entries this run. %d signal(s) remain queued for next execution run.",
                    len(self.state_mgr.get_pending_signals()),
                )
                return

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
                        stop_distance = fill_price - stop_price
                        risk_dollars = shares * stop_distance
                        self.pos_mgr.open_position(
                            ticker, order_id,
                            fill_price, stop_price,
                            shares, signal, broker_sym,
                            mt5_volume=result.get("mt5_volume"),
                        )
                        alert_order_sent(
                            ticker, broker_sym, shares, fill_price, stop_price,
                            risk_dollars, stop_distance, self.environment,
                            deal_id=str(order_id),
                        )
                        self.run_logger.log_position_open(
                            ticker, fill_price, stop_price, shares,
                        )
                        executed += 1
                    else:
                        fail_reason = result.get("reason") or result.get("comment") or result.get("retcode")
                        logger.error("%s: order failed -- reason=%s", ticker, fail_reason)
                        alert_order_rejected(ticker, broker_sym, str(fail_reason), self.environment)
                        self.run_logger.log_error(ticker, f"order_failed: {fail_reason}")

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
        """
        Check all open positions for exit conditions, matching backtest
        exit priority (ALGO-Stocks backtest/engine.py): gap/stop first,
        then Stage 9 fade, then time stop.

        Note on gap_protection vs stop_hit: production polls once per day
        at the AM execution run rather than replaying continuous intraday
        OHLC bars like the backtest, so "current_price <= stop_price" at
        this single poll is production's equivalent of the backtest's
        gap-check (today's open <= stop). True intraday-low stop touches
        between poll times are instead caught by MT5's native broker-side
        SL order attached at entry (build_entry_request's sl= field), which
        the broker enforces continuously, independent of this poll -- this
        poll is the backstop for cases where that didn't cleanly fire
        (e.g. a gap-through), plus the two exits MT5 has no native concept
        of at all: Stage 9 fade and time stop.
        """
        open_positions = self.pos_mgr.get_open()
        time_stop = self.prod_cfg.get("exit", {}).get("time_stop_days", 365)
        stage9_tickers = set(self.state_mgr.get_pending_stage9_exits())

        for pos in open_positions:
            ticker = pos["ticker"]
            broker_sym = pos["mt5_symbol"]   # stores ig_epic in IG mode
            order_id = pos["mt5_ticket"]     # stores ig_deal_id in IG mode
            # MT5 close must use the broker LOT volume, not the underlying
            # share-equivalent "shares" figure used for P&L math.
            close_volume = pos.get("mt5_volume") or pos["shares"]
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
                exit_reason = "stop_gap" if days_held == 0 else "stop_hit"
            elif ticker in stage9_tickers:
                exit_reason = "stage9_exit"
            elif days_held >= time_stop:
                exit_reason = "time_stop"

            if exit_reason is None:
                continue

            try:
                if self.broker == "ig":
                    result = self._execute_exit_ig(
                        ticker, broker_sym, str(order_id), float(pos["shares"]),
                    )
                else:
                    result = self._execute_exit_mt5(
                        ticker, broker_sym, int(order_id), float(close_volume),
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
                        alert_position_closed(
                            ticker, broker_sym, exit_price,
                            trade.get("pnl_total", 0), trade.get("pnl_r", 0),
                            exit_reason, self.environment,
                        )
                else:
                    logger.error(
                        "%s: exit order failed -- reason=%s",
                        ticker, result.get("reason") or result.get("comment") or result.get("retcode"),
                    )
                    alert_order_rejected(
                        ticker, broker_sym,
                        f"exit failed ({exit_reason}): {result.get('reason') or result.get('comment') or result.get('retcode')}",
                        self.environment,
                        is_exit=True,
                    )
                    self.run_logger.log_error(ticker, f"exit_failed: {result}")

            except Exception as exc:
                logger.error("%s: exit error -- %s", ticker, exc, exc_info=True)
                alert_error(ticker, "exit: %s" % exc)
                self.run_logger.log_error(ticker, f"exit error: {exc}")

        self.state_mgr.clear_pending_stage9_exits()

    # -------------------------------------------------------------------------
    # IG Execution Helpers
    # -------------------------------------------------------------------------

    def _fetch_data_ig(self, lookback: int) -> Dict[str, pd.DataFrame]:
        """
        Primary data path: TwelveData local parquet cache (built by
        tools/build_td_cache.py) -- exact backtest data source parity.
        Cache misses fall back to a live TwelveData call per ticker.

        If TWELVEDATA_API_KEY is unset entirely, falls back to yfinance
        (ig_fetcher.py) so the system still runs, but this deviates from
        backtest data parity and should only be a temporary state.
        """
        if os.environ.get("TWELVEDATA_API_KEY", "").strip():
            from prod.data.twelvedata_fetcher import fetch_universe_from_cache
            return fetch_universe_from_cache(list(self.epic_map.keys()), lookback)

        logger.warning("TWELVEDATA_API_KEY not set -- falling back to yfinance (backtest parity NOT guaranteed).")
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
            "reason": result.get("reason"),
            "success": result.get("success"),
            "volume": result.get("volume"),
            "price": result.get("price"),
            "sl": stop_price,
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
    # MT5 Execution Helpers
    # -------------------------------------------------------------------------

    def _fetch_data_mt5(self, tf: str, lookback: int) -> Dict[str, pd.DataFrame]:
        """
        Signal data for MT5 mode now comes from the SAME TwelveData cache as
        IG mode -- exact backtest data source parity regardless of broker.
        MT5 itself is used ONLY for execution (live tick price at order time,
        SL attached to the order) -- never for signal/indicator calculation.
        This replaces the old mt5_fetcher.fetch_universe() live-bar pull,
        which was a silent backtest-parity deviation (different price series
        than TwelveData/the validated backtest).
        """
        if os.environ.get("TWELVEDATA_API_KEY", "").strip():
            from prod.data.twelvedata_fetcher import fetch_universe_from_cache
            return fetch_universe_from_cache(list(self.symbol_map.keys()), lookback)

        logger.warning("TWELVEDATA_API_KEY not set -- falling back to yfinance (backtest parity NOT guaranteed).")
        from prod.data.ig_fetcher import fetch_universe_ig
        return fetch_universe_ig(self.symbol_map, lookback)

    def _execute_entry_mt5(
        self,
        signal: Dict[str, Any],
        mt5_sym: str,
        ticker: str,
        equity: float,
        gate_result: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], float, float, float]:
        from prod.execution.order_builder import build_entry_request, resolve_mt5_volume, get_live_tick
        from prod.execution.order_executor import send_order
        import MetaTrader5 as mt5

        # get_live_tick() selects the symbol AND retries briefly (up to
        # ~1.5s) if the first tick comes back with ask=0.0 -- a freshly
        # selected "-24" extended-hours symbol's quote stream can take a
        # moment to warm up, and a single immediate check was wrongly
        # treating that as "no quote at all" (found 2026-09-01: 7/21
        # successful signals that day were false-rejected this way, all
        # "-24" symbols). Only fall back to the EOD close -- and flag the
        # entry as stale -- if still no live ask after retrying.
        tick = get_live_tick(mt5_sym)
        stale_price = tick is None
        if tick is not None:
            entry_open = float(tick.ask)
        else:
            entry_open = float(signal.get("close", 0))
            logger.warning(
                "%s: no live MT5 ask for %s after retries -- using EOD close $%.2f as fallback entry price.",
                ticker, mt5_sym, entry_open,
            )

        if entry_open <= 0:
            return {"success": False, "reason": "no_valid_price", "retcode": 0, "deal_id": 0}, 0.0, 0.0, 0

        if stale_price:
            # Don't attempt to place a real order against a symbol with no
            # live MT5 quote right now -- build_entry_request would just
            # re-fetch the same dead tick and either send a $0 price
            # (rejected by broker) or a stale one. Skip cleanly instead;
            # this will self-resolve once the symbol has live quotes
            # (e.g. during its actual trading session).
            return {"success": False, "reason": "no_live_quote", "retcode": 0, "deal_id": 0}, entry_open, 0.0, 0

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
        target_shares = sizing["shares"]
        if target_shares <= 0:
            return {"success": False, "reason": "zero_size", "retcode": 0, "deal_id": 0}, entry_open, stop_price, 0

        # Convert broker-agnostic "shares" into a valid MT5 lot size for
        # THIS symbol's actual contract_size/volume_step/volume_min -- see
        # resolve_mt5_volume() docstring for why this matters for 1% risk
        # accuracy.
        exec_cfg = self.prod_cfg.get("execution", {})
        vol = resolve_mt5_volume(
            mt5_sym, target_shares,
            sizing["stop_distance"], sizing["risk_dollars"],
            deviation_warn_pct=float(exec_cfg.get("risk_deviation_warn_pct", 0.15)),
            deviation_max_pct=float(exec_cfg.get("risk_deviation_max_pct", 0.30)),
        )
        if not vol["ok"]:
            logger.info("%s: MT5 volume resolution failed (%s) -- skip.", ticker, vol["reason"])
            return {"success": False, "reason": f"mt5_volume_{vol['reason']}", "retcode": 0, "deal_id": 0}, entry_open, stop_price, 0

        req = build_entry_request(
            mt5_sym, vol["volume"], stop_price,
            self.magic, self.symbol_map_cfg,
        )
        result = send_order(req, self.environment)
        self.run_logger.log_order_sent(ticker, {
            **result,
            "sl": stop_price,
            "mt5_volume": vol["volume"],
            "actual_shares": vol["actual_shares"],
            "target_risk_dollars": vol["target_risk_dollars"],
            "actual_risk_dollars": vol["actual_risk_dollars"],
            "deviation_pct": vol["deviation_pct"],
        })
        result["deal_id"] = result.get("order", 0)
        result["mt5_volume"] = vol["volume"]
        # actual_shares (underlying share-equivalent exposure) is what
        # position_manager needs for correct $ P&L math -- NOT the raw lot
        # count, which is only meaningful to MT5's order_send/close.
        return result, entry_open, stop_price, vol["actual_shares"]

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
