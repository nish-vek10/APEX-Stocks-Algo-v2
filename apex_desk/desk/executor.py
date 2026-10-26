# apex_desk/desk/executor.py
"""
Executes ONLY signals that were reviewed (Trade) and batch-confirmed on the dashboard, at the US open.
Reuses the production order path via brokers.MT5Broker (same tick warm-up retries, lot-step flooring, volume_max clamp,
native broker stop, retcode handling). Supports up to N concurrent positions per ticker (positions table, keyed by ticket).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from desk import brokers, clock, db, rules, settings

log = logging.getLogger("desk.executor")


def _alert(fn: str, *a, **k) -> None:
    try:
        import prod.monitoring.alert as al
        getattr(al, fn)(*a, **k)
    except Exception as exc:                               # noqa: BLE001
        log.debug("alert %s failed: %s", fn, exc)


MT5_LOCK = threading.RLock()          # MT5's python API is process-global: only one job may connect / disconnect at a time
RETRYABLE = ("no_live_quote", "null_result")


class Executor:
    def __init__(self, broker: Optional[brokers.BaseBroker] = None) -> None:
        self.c = settings.cfg()
        self.broker = broker or brokers.make_broker()
        self.env_label = "live" if self.c["mode"] == "live" else "paper"

    # ── snapshot ────────────────────────────────────────────────────────────
    def snapshot(self) -> Dict[str, Any]:
        eq = self.broker.equity()
        if eq and eq > 0:
            db.kv_set("equity", eq)
        snap = {"ts": db.now_iso(), "broker": self.broker.name, "mode": self.c["mode"], "equity": eq,
                "balance": self.broker.balance(), "desk_open_positions": rules.exposure()["open_positions"]}
        db.kv_set("executor_snapshot", snap)
        return snap

    # ── broker permissions (close-only etc.), shown on the dashboard before you review ─────────
    def check_broker_modes(self) -> Dict[str, str]:
        """Needs a connected broker. Flags tradable, unexecuted setups whose symbol the broker will not open (signals.broker_mode)."""
        today = str(clock.now_utc().astimezone(clock.ET).date())
        tickers = sorted({r["ticker"] for r in db.q(
            f"SELECT ticker FROM signals WHERE exec_status IN ('none','queued') AND entry_date>=? AND {rules.tradable_sql()}", (today,))})
        blocked: Dict[str, str] = {}
        for t in tickers:
            sym = self.broker.symbol_for(t)
            mode = "no_symbol" if not sym else self.broker.trade_mode(sym)
            val = mode if mode in rules.BLOCKED_MODES else None
            db.x("UPDATE signals SET broker_mode=? WHERE ticker=? AND exec_status IN ('none','queued') AND entry_date>=?", (val, t, today))
            if val:
                blocked[t] = val
        if blocked:
            log.info("Broker blocks new trades on: %s", blocked)
        return blocked

    # ── reconcile (broker closed a position natively, e.g. stop hit) ────────
    def reconcile(self) -> int:
        live = self.broker.live_tickets()
        if live is None:
            return 0
        n = 0
        for p in rules.open_positions():
            if p["ticket"] in live:
                continue
            info = self.broker.closing_info(p["ticket"], float(p["stop_price"] or p["entry_price"]))
            self._close_record(p, float(info["price"]), "broker_closed_externally")
            _alert("send_alert", f"{p['ticker']} closed by broker ({info.get('detail')}) @ {info['price']:.2f}", level="INFO")
            n += 1
        return n

    def _close_record(self, p: Dict[str, Any], exit_price: float, reason: str) -> None:
        pnl = (exit_price - p["entry_price"]) * p["shares"]
        r = (exit_price - p["entry_price"]) / p["stop_distance"] if p["stop_distance"] else 0.0
        db.x("UPDATE positions SET status='closed', exit_date=?, exit_price=?, exit_reason=?, pnl_total=?, pnl_r=? WHERE id=?",
             (str(clock.now_utc().astimezone(clock.ET).date()), exit_price, reason, pnl, r, p["id"]))
        db.audit("executor", "position_closed", {"ticker": p["ticker"], "reason": reason, "pnl": pnl, "R": r})
        log.info("CLOSED %s ticket=%s %.2f %s pnl=%.2f (%.2fR)", p["ticker"], p["ticket"], exit_price, reason, pnl, r)

    # ── exits ───────────────────────────────────────────────────────────────
    def process_exits(self) -> int:
        today = clock.now_utc().astimezone(clock.ET).date()
        time_stop = int(self.c["strategy"]["time_stop_days"])
        pend = {r["ticker"]: r["reason"] for r in db.q("SELECT * FROM pending_exits")}
        n = 0
        for p in rules.open_positions():
            reason = None
            try:
                tk = self.broker.tick(p["broker_symbol"])
                bid = tk.bid if tk else 0.0
            except Exception:                               # noqa: BLE001
                bid = 0.0
            import core.utils.trading_calendar as tc
            if bid > 0 and bid <= float(p["stop_price"]):
                reason = "stop_hit"
            elif p["ticker"] in pend:
                reason = pend[p["ticker"]]
            elif tc.trading_days_between(p["entry_date"], today) >= time_stop:
                reason = "time_stop"
            if not reason:
                continue
            try:
                res = self.broker.send_close(p["broker_symbol"], int(p["ticket"]), float(p["mt5_volume"] or p["shares"]),
                                             f"{self.c['execution']['comment_prefix']}_EXIT_{p.get('tier') or 'X'}-{p.get('signal_id')}")
                if res.get("success"):
                    self._close_record(p, float(res.get("price") or bid), reason)
                    _alert("alert_position_closed", p["ticker"], p["broker_symbol"], float(res.get("price") or bid),
                           0.0, 0.0, reason, self.env_label)
                    n += 1
                else:
                    log.error("exit failed %s: %s", p["ticker"], res)
                    _alert("alert_order_rejected", p["ticker"], p["broker_symbol"], f"exit failed ({reason}): {res.get('comment') or res.get('retcode')}",
                           self.env_label, is_exit=True)
            except Exception as exc:                        # noqa: BLE001
                log.error("exit error %s: %s", p["ticker"], exc, exc_info=True)
        db.x("DELETE FROM pending_exits")
        return n

    # ── single entry ────────────────────────────────────────────────────────
    def _attempt(self, sig: Dict[str, Any], equity: float) -> Optional[str]:
        """None on success, else a failure reason ('no_live_quote' is retried by the caller)."""
        rk, st = self.c["risk"], self.c["strategy"]
        t = sig["ticker"]
        sym = self.broker.symbol_for(t)
        if not sym:
            return "no_broker_symbol"
        bm = self.broker.trade_mode(sym)
        if bm in rules.BLOCKED_MODES:
            return f"broker_{bm}"                          # e.g. broker_close_only (retcode 10044): never retried
        tk = self.broker.tick(sym)
        if tk is None:
            return "no_live_quote"
        entry = float(tk.ask)
        if entry <= 0:
            return "no_valid_price"
        if entry < float(st["min_price"]):
            return "below_min_price"
        atr = float(sig["atr"] or 0)
        stop_dist = max(atr * float(st["atr_multiplier"]), entry * float(st["stop_floor_pct"]))
        stop = entry - stop_dist
        rm = float(sig.get("risk_mult") or 1.0)
        rp = sig.get("risk_pct")                                   # manual override / tier default stored on the signal
        risk_usd = equity * (float(rp) if rp else float(rk["risk_pct"]) * rm)
        shares = min(risk_usd / stop_dist, equity * float(rk["max_position_value_pct"]) / entry, float(rk["max_shares"]))
        gx = rk.get("max_gross_x")
        if gx:
            room = float(gx) * equity - rules.exposure()["notional"]
            if room < entry:
                return "gross_cap_reached"
            shares = min(shares, room / entry)
        shares = int(shares)
        if shares < 1:
            return "zero_size"
        vol = self.broker.resolve_volume(sym, shares, stop_dist, shares * stop_dist)
        if not vol.get("ok"):
            return f"volume_{vol.get('reason')}"
        tag = f"{self.c['execution']['comment_prefix']}_{sig.get('tier') or 'X'}-{sig['id']}"      # MT5 comment, e.g. DESK_A-4567
        res = self.broker.send_entry(sym, vol["volume"], stop, tag)
        db.x("INSERT INTO executions(signal_id,ticker,ts,ok,reason,retcode,order_id,lots,shares,fill_price,stop_price,risk_usd,detail) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
             (sig["id"], t, db.now_iso(), int(bool(res.get("success"))), res.get("comment"), res.get("retcode"),
              res.get("order"), vol["volume"], vol["actual_shares"], res.get("price"), stop, vol["actual_risk_dollars"],
              str({k: v for k, v in vol.items() if k != 'ok'})))
        if not res.get("success"):
            return str(res.get("comment") or res.get("retcode") or "order_failed")
        fill = float(res.get("price") or entry)
        sd = fill - stop
        db.x("INSERT INTO positions(ticker,broker_symbol,ticket,signal_id,entry_date,entry_price,stop_price,shares,mt5_volume,stop_distance,tier) "
             "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
             (t, sym, res.get("order"), sig["id"], str(clock.now_utc().astimezone(clock.ET).date()), fill, stop,
              vol["actual_shares"], vol["volume"], sd, sig.get("tier")))
        _alert("alert_order_sent", t, sym, vol["actual_shares"], fill, stop, vol["actual_shares"] * sd, sd, self.env_label,
               deal_id=str(res.get("order")))
        log.info("FILLED %s %s lots=%s shares=%s fill=%.4f sl=%.4f risk=$%.0f", t, sym, vol["volume"], vol["actual_shares"],
                 fill, stop, vol["actual_shares"] * sd)
        return None

    # ── session ─────────────────────────────────────────────────────────────
    def run_session(self, force: bool = False) -> Dict[str, Any]:
        with MT5_LOCK:
            return self._run_session(force)

    def _reconnect(self) -> None:
        """order_send returned None (IPC lost): reconnect to the terminal before the order is retried."""
        try:
            self.broker.disconnect()
        except Exception:                                  # noqa: BLE001
            pass
        time.sleep(1)
        self.broker.connect()

    def _run_session(self, force: bool = False) -> Dict[str, Any]:
        today = str(clock.now_utc().astimezone(clock.ET).date())
        import core.utils.trading_calendar as tc
        if not force and not tc.is_trading_day(today):
            return {"skipped": "not a trading day"}
        out: Dict[str, Any] = {"entry_date": today, "executed": 0, "failed": [], "exits": 0, "reconciled": 0}
        self.broker.connect()
        try:
            snap = self.snapshot()
            equity = float(snap["equity"] or db.kv_get("equity", self.c["risk"]["default_equity"]))
            out["reconciled"] = self.reconcile()
            out["exits"] = self.process_exits()
            sigs = db.q(f"SELECT * FROM signals WHERE entry_date=? AND exec_status='queued' AND decision='trade' AND {rules.tradable_sql()} "
                        "ORDER BY dist200 ASC", (today,))
            log.info("Executing %d queued signals (equity %.0f)", len(sigs), equity)
            retry: List[Dict[str, Any]] = []
            for s in sigs:
                why = self._pre_checks(s)
                if why:
                    self._mark(s["id"], "failed", why)
                    out["failed"].append((s["ticker"], why))
                    _alert("alert_order_rejected", s["ticker"], "", why, self.env_label)
                    continue
                db.x("UPDATE signals SET exec_status='executing' WHERE id=?", (s["id"],))
                r = self._attempt(s, equity)
                if r is None:
                    self._mark(s["id"], "executed", "filled")
                    out["executed"] += 1
                elif r in RETRYABLE:
                    db.x("UPDATE signals SET exec_status='queued' WHERE id=?", (s["id"],))
                    retry.append(s)
                    if r == "null_result":
                        self._reconnect()
                else:
                    self._mark(s["id"], "failed", r)
                    out["failed"].append((s["ticker"], r))
                    _alert("alert_order_rejected", s["ticker"], self.broker.symbol_for(s["ticker"]), r, self.env_label)
            # retry phase 1: every `gap` s for `win` s; phase 2: every `gap2` s until `ext_min` minutes after the open
            # (thin 24h symbols such as *.NYSE-24 usually get quotes once the opening rush is over)
            t = self.c["timing"]
            win, gap = float(t["no_quote_retry_window_sec"]), float(t["no_quote_retry_interval_sec"])
            gap2, ext_min = float(t.get("no_quote_extended_interval_sec", 120)), float(t.get("no_quote_extended_minutes_after_open", 30))
            ext_deadline = clock.open_dt(today).astimezone(clock.ET) + pd.Timedelta(minutes=ext_min).to_pytimedelta()
            deadline = time.monotonic() + win
            phase = 1
            while retry:
                if phase == 1 and time.monotonic() >= deadline:
                    phase = 2
                    if clock.now_utc() >= ext_deadline:
                        log.info("Quote retry window closed at %s London: no quote for %d signals", ext_deadline.astimezone(clock.london()).strftime("%H:%M"), len(retry))
                    else:
                        log.info("Quote retry phase 2: %d signals still waiting, retrying every %ds until %s London", len(retry), int(gap2),
                             ext_deadline.astimezone(clock.london()).strftime("%H:%M"))
                if phase == 2 and clock.now_utc() >= ext_deadline:
                    break
                time.sleep(gap if phase == 1 else gap2)
                nxt = []
                for s in retry:
                    why = self._pre_checks(s)
                    r = why or self._attempt(s, equity)
                    if r is None:
                        self._mark(s["id"], "executed", "filled on retry")
                        out["executed"] += 1
                    elif r in RETRYABLE:
                        nxt.append(s)
                        if r == "null_result":
                            self._reconnect()
                    else:
                        self._mark(s["id"], "failed", r)
                        out["failed"].append((s["ticker"], r))
                        _alert("alert_order_rejected", s["ticker"], self.broker.symbol_for(s["ticker"]), r, self.env_label)
                retry = nxt
            for s in retry:
                self._mark(s["id"], "failed", f"no_live_quote (no quote until {int(ext_min)} min after the open)")
                out["failed"].append((s["ticker"], "no_live_quote"))
                _alert("alert_order_rejected", s["ticker"], self.broker.symbol_for(s["ticker"]),
                       f"no_live_quote (still no quote {int(ext_min)} min after the open)", self.env_label)
            rules.expire_stale()
            db.x("UPDATE batches SET status='executed' WHERE entry_date=? AND status='queued'", (today,))
            self.snapshot()
        finally:
            self.broker.disconnect()
        db.audit("executor", "session", out)
        return out

    def _pre_checks(self, s: Dict[str, Any]) -> str:
        rk = self.c["risk"]
        if len(rules.open_positions()) >= int(rk["max_positions"]):
            return "position_cap_reached"
        if rules.per_ticker_open(s["ticker"]) >= int(rk["max_per_ticker"]):
            return "per_ticker_cap_reached"
        return ""

    @staticmethod
    def _mark(sig_id: int, status: str, detail: str) -> None:
        db.x("UPDATE signals SET exec_status=?, exec_detail=? WHERE id=?", (status, detail, sig_id))
