# scheduler.py
"""
APEX Cloud Scheduler — Railway deployment entry point.

Runs three jobs on a Mon-Fri schedule, anchored to NYSE local time
(America/New_York), NOT a fixed UTC offset:
  16:45 ET  -> python tools/build_td_cache.py       (refresh TwelveData cache)
  17:05 ET  -> python run_prod.py --mode signals    (after 16:00 ET close)
  09:31 ET  -> python run_prod.py --mode execution  (after 09:30 ET open)

The cache-refresh job is REQUIRED, not cosmetic: signal generation reads
exclusively from the local TwelveData parquet cache (prod/orchestrator.py's
_fetch_data_mt5 -> fetch_universe_from_cache), never a live pull. Without a
daily refresh, the cache silently goes stale and signals keep firing off
whatever date it was last built (found 2026-08-27: a manual test run
produced signals dated 2026-08-14/08-17 -- 10-13 days old -- because the
cache hadn't been touched since the initial build). build_td_cache.py is
safe to run every day: is_cache_fresh() skips any ticker already current,
so a normal day only re-fetches the handful that genuinely need it and
exits in seconds, not the ~6hr full-universe cold-start time.

Using an America/New_York cron trigger (not UTC) means these times
auto-adjust across DST transitions with zero code changes -- APScheduler
resolves the IANA timezone's UTC offset at each fire, so 09:31 ET stays
09:31 ET whether that's 13:31 UTC (EDT, Mar-Nov) or 14:31 UTC (EST,
Nov-Mar). This also sidesteps the US/UK DST mismatch entirely (US DST
ends first Sunday of Nov, UK BST ends last Sunday of Oct -- the two are
offset by up to a week each year), since nothing here is keyed to UK time.

Retry policy: up to MAX_RETRIES attempts per job.
              RETRY_DELAY_SEC between attempts.
              Telegram alert fired if all retries exhausted.

Deploy on Railway:
  Procfile  -> worker: python scheduler.py
  Volume    -> mount at /app/state  (persists state/*.json across deploys)
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from dotenv import load_dotenv

# ── Bootstrap ─────────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

# Ensure project root on path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.utils.logging import setup_logger
from prod.monitoring.alert import send_alert

setup_logger("apex", ROOT / "logs", console=True)
logger = logging.getLogger("scheduler")

# ── Config ────────────────────────────────────────────────────────────────────

MAX_RETRIES    = int(os.environ.get("APEX_MAX_RETRIES", 3))
RETRY_DELAY    = int(os.environ.get("APEX_RETRY_DELAY_SEC", 300))   # 5 min between retries

# All times below are America/New_York LOCAL time (NYSE hours), not UTC.
# The scheduler itself runs in this timezone (see BlockingScheduler below),
# so these fire at the same NYSE-local clock time year-round regardless of
# US or UK daylight saving state.
SCHED_TZ       = "America/New_York"
CACHE_HOUR     = int(os.environ.get("APEX_CACHE_HOUR", 16))    # 16:45 ET, before signals
CACHE_MIN      = int(os.environ.get("APEX_CACHE_MIN", 45))
SIGNAL_HOUR    = int(os.environ.get("APEX_SIGNAL_HOUR", 17))   # 17:05 ET, after 16:00 ET close
SIGNAL_MIN     = int(os.environ.get("APEX_SIGNAL_MIN", 5))
EXEC_HOUR      = int(os.environ.get("APEX_EXEC_HOUR", 9))      # 09:31 ET, after 09:30 ET open
EXEC_MIN       = int(os.environ.get("APEX_EXEC_MIN", 31))
HEARTBEAT_MIN  = int(os.environ.get("APEX_HEARTBEAT_MIN", 10))


# ── Job runner with retry ─────────────────────────────────────────────────────

def run_script(mode: str, argv: list[str]) -> None:
    """
    Run `python <argv>` with retry logic (shared by cache-refresh, signals,
    execution). `mode` is just a label used for logging/alerts.
    Sends Telegram alert on success and on all-retries-exhausted failure.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logger.info("SCHEDULER: starting job mode=%s @ %s", mode, ts)

    for attempt in range(1, MAX_RETRIES + 1):
        logger.info("mode=%s attempt=%d/%d", mode, attempt, MAX_RETRIES)

        result = subprocess.run(
            [sys.executable, *argv],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stdout:
            logger.info("[%s stdout]\n%s", mode, stdout)
        if stderr:
            logger.warning("[%s stderr]\n%s", mode, stderr)

        if result.returncode == 0:
            logger.info("mode=%s completed successfully on attempt %d", mode, attempt)
            return

        logger.error(
            "mode=%s failed (attempt %d/%d) | returncode=%d",
            mode, attempt, MAX_RETRIES, result.returncode,
        )

        if attempt < MAX_RETRIES:
            logger.info("Retrying in %d seconds...", RETRY_DELAY)
            send_alert(
                f"APEX scheduler: mode={mode} attempt {attempt}/{MAX_RETRIES} failed. "
                f"Retrying in {RETRY_DELAY // 60} min.",
                level="WARNING",
                telegram_text=(
                    f"⚠️ <b>APEX RETRY</b>\n"
                    f"──────────────────────\n"
                    f"Mode:     {mode.upper()}\n"
                    f"Attempt:  {attempt}/{MAX_RETRIES}\n"
                    f"Error:    returncode={result.returncode}\n"
                    f"Retrying in {RETRY_DELAY // 60} min...\n"
                    f"──────────────────────"
                ),
            )
            time.sleep(RETRY_DELAY)

    # All retries exhausted
    logger.critical("mode=%s FAILED after %d attempts — manual intervention required", mode, MAX_RETRIES)
    send_alert(
        f"APEX SCHEDULER FAILURE: mode={mode} failed after {MAX_RETRIES} attempts.",
        level="CRITICAL",
        telegram_text=(
            f"🔥 <b>APEX SCHEDULER FAILURE</b>\n"
            f"──────────────────────\n"
            f"Mode:     {mode.upper()}\n"
            f"Attempts: {MAX_RETRIES}/{MAX_RETRIES} — all failed\n"
            f"Time:     {ts}\n"
            f"──────────────────────\n"
            f"⛔ Manual intervention required.\n"
            f"Check: <code>logs/run_*.jsonl</code>"
        ),
    )


def job_cache_refresh() -> None:
    run_script("cache_refresh", [str(ROOT / "tools" / "build_td_cache.py")])


def job_signals() -> None:
    run_script("signals", [str(ROOT / "run_prod.py"), "--mode", "signals"])


def job_execution() -> None:
    run_script("execution", [str(ROOT / "run_prod.py"), "--mode", "execution"])


_scheduler_ref: BlockingScheduler | None = None


def job_heartbeat() -> None:
    """
    Prints every HEARTBEAT_MIN minutes so it's visible at a glance in the
    PowerShell window that the process is still alive and hasn't silently
    died -- distinct from the job-specific logging, this fires
    independently of whether cache/signals/execution jobs have run yet.
    """
    now_et = datetime.now(timezone.utc).astimezone()
    line = f"[HEARTBEAT] {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')} -- scheduler alive."
    if _scheduler_ref is not None:
        for job in _scheduler_ref.get_jobs():
            if job.id == "apex_heartbeat":
                continue
            next_run = getattr(job, "next_run_time", None)
            line += f"\n  {job.name} -> next: {next_run if next_run is not None else 'unknown'}"
    print(line)
    logger.info(line)


# ── APScheduler event hooks ───────────────────────────────────────────────────

def on_job_event(event) -> None:
    if event.exception:
        logger.error("Scheduler job raised exception: %s", event.exception)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    logger.info(
        "APEX Scheduler starting | cache=%02d:%02d ET | signals=%02d:%02d ET | execution=%02d:%02d ET | "
        "retries=%d | retry_delay=%ds | days=Mon-Fri | tz=%s (auto DST)",
        CACHE_HOUR, CACHE_MIN, SIGNAL_HOUR, SIGNAL_MIN, EXEC_HOUR, EXEC_MIN, MAX_RETRIES, RETRY_DELAY, SCHED_TZ,
    )

    send_alert(
        "APEX Scheduler started.",
        level="INFO",
        telegram_text=(
            f"🕐 <b>APEX SCHEDULER STARTED</b>\n"
            f"──────────────────────\n"
            f"Cache refresh: {CACHE_HOUR:02d}:{CACHE_MIN:02d} ET (Mon-Fri)\n"
            f"Signals:   {SIGNAL_HOUR:02d}:{SIGNAL_MIN:02d} ET (Mon-Fri)\n"
            f"Execution: {EXEC_HOUR:02d}:{EXEC_MIN:02d} ET (Mon-Fri)\n"
            f"Retries:   {MAX_RETRIES} × {RETRY_DELAY // 60}min gap\n"
            f"Timezone:  {SCHED_TZ} (auto-adjusts for DST)\n"
            f"──────────────────────"
        ),
    )

    scheduler = BlockingScheduler(timezone=SCHED_TZ)
    scheduler.add_listener(on_job_event, EVENT_JOB_ERROR)

    # Daily TwelveData cache refresh — MUST run before signals, otherwise
    # signal generation silently reads stale cached bars (see module
    # docstring). is_cache_fresh() makes this a fast no-op on normal days.
    scheduler.add_job(
        job_cache_refresh,
        trigger="cron",
        day_of_week="mon-fri",
        hour=CACHE_HOUR,
        minute=CACHE_MIN,
        id="apex_cache_refresh",
        name="APEX TwelveData Cache Refresh",
        misfire_grace_time=1800,
        coalesce=True,
    )

    # EOD signals — after US market close
    scheduler.add_job(
        job_signals,
        trigger="cron",
        day_of_week="mon-fri",
        hour=SIGNAL_HOUR,
        minute=SIGNAL_MIN,
        id="apex_signals",
        name="APEX EOD Signals",
        misfire_grace_time=1800,   # allow up to 30-min late start (e.g. cold boot)
        coalesce=True,             # don't stack if missed multiple fires
    )

    # AM execution — after US market open
    scheduler.add_job(
        job_execution,
        trigger="cron",
        day_of_week="mon-fri",
        hour=EXEC_HOUR,
        minute=EXEC_MIN,
        id="apex_execution",
        name="APEX AM Execution",
        misfire_grace_time=1800,
        coalesce=True,
    )

    # Heartbeat -- prints/logs every HEARTBEAT_MIN minutes so it's obvious
    # at a glance the process hasn't silently died, independent of whether
    # any of the actual trading jobs have fired yet.
    scheduler.add_job(
        job_heartbeat,
        trigger="interval",
        minutes=HEARTBEAT_MIN,
        id="apex_heartbeat",
        name="APEX Heartbeat",
    )

    global _scheduler_ref
    _scheduler_ref = scheduler

    logger.info("Scheduler running. Next jobs:")
    for job in scheduler.get_jobs():
        # job.next_run_time only exists on APScheduler 3.x's Job class --
        # some environments have picked up a newer/older APScheduler build
        # (requirements.txt pins "apscheduler>=3.10.0" with no upper bound)
        # where this attribute doesn't exist. This is purely a startup log
        # line, not functional -- never let it crash the scheduler.
        next_run = getattr(job, "next_run_time", None)
        logger.info("  %s -> next: %s", job.name, next_run if next_run is not None else "(unknown -- non-fatal)")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
