# prod/monitoring/alert.py
"""
APEX alert dispatcher.

Channels:
  1. Python logger (always on — writes to logs/run_YYYYMMDD.jsonl via RunLogger)
  2. Telegram (optional — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env)

Telegram setup:
  1. Create bot via @BotFather -> get BOT_TOKEN
  2. Start a chat with your bot, then fetch:
       https://api.telegram.org/bot<TOKEN>/getUpdates
     Use the chat_id from the result.
  3. Add to .env:
       TELEGRAM_BOT_TOKEN=your_token_here
       TELEGRAM_CHAT_ID=your_chat_id_here

If env vars are missing, Telegram is silently skipped (logs only).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger("alert")

# ── Telegram client (optional) ────────────────────────────────────────────────

def _get_telegram_cfg() -> tuple[str, str] | tuple[None, None]:
    """Return (bot_token, chat_id) from env, or (None, None) if not configured."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        return token, chat_id
    return None, None


def _send_telegram(text: str) -> None:
    """
    Fire-and-forget Telegram message. Fails silently — never crashes the main loop.
    Uses stdlib urllib only (no requests dependency required).
    """
    token, chat_id = _get_telegram_cfg()
    if not token:
        return  # Telegram not configured — skip silently

    try:
        import urllib.request
        import urllib.parse

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")

        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(f"Telegram send failed: HTTP {resp.status}")
    except Exception as exc:
        logger.warning(f"Telegram send error (non-fatal): {exc}")


# ── Core dispatcher ───────────────────────────────────────────────────────────

def send_alert(
    message: str,
    level: str = "INFO",
    metadata: Optional[Dict[str, Any]] = None,
    telegram_text: Optional[str] = None,
) -> None:
    """
    Route alert to logger + Telegram.

    Args:
        message      : Log message string
        level        : Log level — INFO | WARNING | ERROR | CRITICAL
        metadata     : Extra key/value dict appended to log message
        telegram_text: Override text for Telegram (if None, uses `message`)
    """
    log_fn = {
        "INFO":     logger.info,
        "WARNING":  logger.warning,
        "ERROR":    logger.error,
        "CRITICAL": logger.critical,
    }.get(level.upper(), logger.info)

    msg = f"[APEX] {message}"
    if metadata:
        msg += f" | {metadata}"
    log_fn(msg)

    tg_text = telegram_text if telegram_text is not None else msg
    _send_telegram(tg_text)


# ── Typed alert helpers ────────────────────────────────────────────────────────

def alert_signal_found(ticker: str, stage: int, signal_date: str) -> None:
    """Signal detected — end of day."""
    tg = (
        f"📡 <b>APEX SIGNAL</b>\n"
        f"Ticker:  <b>{ticker}</b>\n"
        f"Stage:   {stage} (Breakout Confirmed)\n"
        f"Date:    {signal_date}\n"
        f"Action:  Execute next AM open"
    )
    send_alert(
        f"Signal: {ticker} Stage {stage} @ {signal_date}",
        level="INFO",
        telegram_text=tg,
    )


def alert_order_sent(
    ticker: str,
    epic: str,
    shares: float,
    entry_price: float,
    stop_price: float,
    risk_dollars: float,
    stop_distance: float,
    environment: str,
    deal_id: str = "",
    timestamp: Optional[str] = None,
) -> None:
    """Order submitted to IG."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    env_label = "🟡 PAPER" if environment.lower() == "paper" else "🟢 LIVE"
    tg = (
        f"✅ <b>APEX ORDER SENT</b> {env_label}\n"
        f"──────────────────────\n"
        f"Ticker:     <b>{ticker}</b>  ({epic})\n"
        f"Time:       {ts}\n"
        f"Entry:      ${entry_price:.2f}\n"
        f"Size:       {shares:.0f} shares\n"
        f"Stop:       ${stop_price:.2f}  (${stop_distance:.2f} below entry)\n"
        f"Risk $:     ${risk_dollars:.2f}\n"
        f"Deal ID:    {deal_id or 'N/A'}\n"
        f"──────────────────────"
    )
    send_alert(
        f"Order sent: {ticker} x{shares:.0f} @ ${entry_price:.2f} | stop=${stop_price:.2f} | risk=${risk_dollars:.2f} [{environment.upper()}]",
        level="INFO",
        telegram_text=tg,
    )


def alert_order_rejected(
    ticker: str,
    epic: str,
    reason: str,
    environment: str,
) -> None:
    """Order was rejected by IG or paper guard."""
    tg = (
        f"❌ <b>APEX ORDER REJECTED</b>\n"
        f"──────────────────────\n"
        f"Ticker:  <b>{ticker}</b>  ({epic})\n"
        f"Reason:  {reason}\n"
        f"Mode:    {environment.upper()}\n"
        f"──────────────────────\n"
        f"⚠️ Review logs — no position opened."
    )
    send_alert(
        f"Order REJECTED: {ticker} ({epic}) — {reason} [{environment.upper()}]",
        level="WARNING",
        telegram_text=tg,
    )


def alert_position_closed(
    ticker: str,
    epic: str,
    exit_price: float,
    pnl: float,
    pnl_r: float,
    reason: str,
    environment: str,
    timestamp: Optional[str] = None,
) -> None:
    """Position fully closed."""
    ts = timestamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    pnl_emoji = "🟢" if pnl >= 0 else "🔴"
    tg = (
        f"{pnl_emoji} <b>APEX POSITION CLOSED</b>\n"
        f"──────────────────────\n"
        f"Ticker:   <b>{ticker}</b>  ({epic})\n"
        f"Time:     {ts}\n"
        f"Exit:     ${exit_price:.2f}\n"
        f"P&L:      ${pnl:+.2f}  ({pnl_r:+.2f}R)\n"
        f"Reason:   {reason}\n"
        f"──────────────────────"
    )
    send_alert(
        f"Position closed: {ticker} @ ${exit_price:.2f} | PnL=${pnl:+.2f} ({pnl_r:+.2f}R) | {reason}",
        level="INFO",
        telegram_text=tg,
    )


def alert_circuit_breaker(reason: str, state: Optional[Dict[str, Any]] = None) -> None:
    """Circuit breaker tripped — all execution halted."""
    state_str = ""
    if state:
        dd_daily = state.get("daily_drawdown_pct", 0) * 100
        dd_weekly = state.get("weekly_drawdown_pct", 0) * 100
        streak = state.get("consecutive_losses", 0)
        state_str = (
            f"\n──────────────────────\n"
            f"Daily DD:   {dd_daily:.2f}%\n"
            f"Weekly DD:  {dd_weekly:.2f}%\n"
            f"Loss streak: {streak}"
        )
    tg = (
        f"🚨 <b>APEX CIRCUIT BREAKER TRIPPED</b>\n"
        f"──────────────────────\n"
        f"Reason: {reason}"
        f"{state_str}\n"
        f"──────────────────────\n"
        f"⛔ All execution halted. Review and run:\n"
        f"<code>python run_prod.py --reset-circuit-breaker</code>"
    )
    send_alert(
        f"CIRCUIT BREAKER TRIPPED: {reason}",
        level="CRITICAL",
        telegram_text=tg,
    )


def alert_spider_gate_block(ticker: str, sector: str, reason: str) -> None:
    """Spider gate blocked a trade."""
    tg = (
        f"🕷️ <b>SPIDER GATE BLOCKED</b>\n"
        f"Ticker:  <b>{ticker}</b>\n"
        f"Sector:  {sector}\n"
        f"Reason:  {reason}"
    )
    send_alert(
        f"Spider gate blocked: {ticker} ({sector}) — {reason}",
        level="WARNING",
        telegram_text=tg,
    )


def alert_run_summary(
    mode: str,
    signals_count: int,
    orders_sent: int,
    positions_closed: int,
    equity: float,
    environment: str,
) -> None:
    """End-of-run summary."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tg = (
        f"📊 <b>APEX RUN SUMMARY</b>\n"
        f"──────────────────────\n"
        f"Mode:       {mode.upper()} [{environment.upper()}]\n"
        f"Time:       {ts}\n"
        f"Signals:    {signals_count}\n"
        f"Orders:     {orders_sent}\n"
        f"Closed:     {positions_closed}\n"
        f"Equity:     ${equity:,.2f}\n"
        f"──────────────────────"
    )
    send_alert(
        f"Run summary [{mode}/{environment}]: signals={signals_count} orders={orders_sent} closed={positions_closed} equity=${equity:,.2f}",
        level="INFO",
        telegram_text=tg,
    )


def alert_error(
    ticker: str,
    error: str,
    context: str = "",
    critical: bool = False,
) -> None:
    """
    Operational error — always fires to Telegram.
    Use critical=True for errors that require immediate human intervention.
    """
    level = "CRITICAL" if critical else "ERROR"
    ctx_str = f"\nContext: {context}" if context else ""
    tg = (
        f"{'🔥' if critical else '⚠️'} <b>APEX {'CRITICAL ' if critical else ''}ERROR</b>\n"
        f"──────────────────────\n"
        f"Ticker:  <b>{ticker}</b>\n"
        f"Error:   {error}"
        f"{ctx_str}\n"
        f"──────────────────────\n"
        f"Check: <code>logs/run_*.jsonl</code>"
    )
    send_alert(
        f"{'CRITICAL ' if critical else ''}ERROR [{ticker}]: {error}{ctx_str}",
        level=level,
        telegram_text=tg,
    )


def alert_startup(mode: str, environment: str, broker: str, equity: float) -> None:
    """System startup notification."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    tg = (
        f"🚀 <b>APEX STARTED</b>\n"
        f"──────────────────────\n"
        f"Mode:    {mode.upper()}\n"
        f"Env:     {environment.upper()}\n"
        f"Broker:  {broker.upper()}\n"
        f"Equity:  ${equity:,.2f}\n"
        f"Time:    {ts}\n"
        f"──────────────────────"
    )
    send_alert(
        f"APEX started: mode={mode} env={environment} broker={broker} equity=${equity:,.2f}",
        level="INFO",
        telegram_text=tg,
    )


def alert_connection_failed(broker: str, error: str) -> None:
    """Broker connection failed at startup."""
    tg = (
        f"🔴 <b>APEX CONNECTION FAILED</b>\n"
        f"──────────────────────\n"
        f"Broker:  {broker.upper()}\n"
        f"Error:   {error}\n"
        f"──────────────────────\n"
        f"⛔ Run aborted. Check credentials in .env"
    )
    send_alert(
        f"Connection FAILED [{broker}]: {error}",
        level="CRITICAL",
        telegram_text=tg,
    )
