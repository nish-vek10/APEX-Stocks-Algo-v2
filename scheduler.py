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
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.executors.pool import ThreadPoolExecutor
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
RETRY_DELAY    = int(os.environ.get("APEX_RETRY_DELAY_SEC", 60))    # 1 min between retries

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

# Cache-refresh retry-until-converged settings. build_td_cache.py is
# idempotent (already-fresh tickers are zero-cost skips), so re-running it
# after a round with errors only re-attempts the tickers that just failed --
# usually enough to clear rate-limit collateral / transient TD API hiccups.
# NOT a blind infinite retry: some errors are PERMANENT (delisted tickers,
# wrong symbol suffix, plan-restricted names -- confirmed 2026-09-14 for
# AAC-U, BF-A, CRD-A, HEI-A, LPRO, MOG-A, SKYT, UHAL-B, EA) and will never
# clear no matter how many rounds run. See job_cache_refresh().
CACHE_MAX_ROUNDS    = int(os.environ.get("APEX_CACHE_MAX_ROUNDS", 5))
CACHE_ROUND_DELAY   = int(os.environ.get("APEX_CACHE_ROUND_DELAY_SEC", 20))


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

        # APEX_SCHEDULED=1 tells run_prod.py to skip its interactive
        # "Type CONFIRM" live-mode safety prompt -- input() would hang
        # forever here (no TTY attached to a subprocess.run call), which
        # would silently deadlock the entire daily automation the first
        # time it ran against environment: "live".
        #
        # PYTHONIOENCODING=utf-8 forces the child process's stdout/stderr
        # to UTF-8 regardless of the Windows console's default codepage
        # (cp1252). Without it, ANY Unicode character anywhere in a
        # captured print()/logger call -- the "⚠" warning symbol, emoji in
        # Telegram alert text, even an em-dash -- raises
        # UnicodeEncodeError and crashes the whole subprocess. Found
        # 2026-09-08: this crashed run_prod.py's very first scheduled live
        # execution run before it got anywhere near placing an order.
        child_env = {**os.environ, "APEX_SCHEDULED": "1", "PYTHONIOENCODING": "utf-8"}
        result = subprocess.run(
            [sys.executable, *argv],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=child_env,
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
            delay_label = f"{RETRY_DELAY}s" if RETRY_DELAY < 60 else f"{RETRY_DELAY // 60}min"
            logger.info("Retrying in %d seconds...", RETRY_DELAY)
            send_alert(
                f"APEX scheduler: mode={mode} attempt {attempt}/{MAX_RETRIES} failed. "
                f"Retrying in {delay_label}.",
                level="WARNING",
                telegram_text=(
                    f"⚠️ <b>APEX RETRY</b>\n"
                    f"──────────────────────\n"
                    f"Mode:     {mode.upper()}\n"
                    f"Attempt:  {attempt}/{MAX_RETRIES}\n"
                    f"Error:    returncode={result.returncode}\n"
                    f"Retrying in {delay_label}...\n"
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


def _parse_cache_summary(stdout: str):
    """
    Pulls the final "ok=X partial=Y err=Z" line out of build_td_cache.py's
    stdout. Returns (ok, partial, err) or None if the line wasn't found
    (e.g. the script crashed before printing it).
    """
    match = re.search(r"ok=(\d+)\s+partial=(\d+)\s+err=(\d+)", stdout)
    if not match:
        return None
    return tuple(int(x) for x in match.groups())


def job_cache_refresh() -> None:
    """
    Runs build_td_cache.py in a bounded retry-until-converged loop instead
    of a single shot. Found 2026-09-14: after a multi-day scheduler outage,
    the first pass left 228 tickers erroring (mostly rate-limit collateral
    from re-fetching a large stale backlog in one go); a second, unassisted
    manual re-run dropped that to 30 with zero code changes -- purely
    because build_td_cache.py is idempotent and only re-attempts tickers
    that failed last time. This automates that same manual pattern.

    Stops as soon as either:
      - err reaches 0 (fully clean), or
      - err stops improving between two consecutive rounds (remaining
        failures are permanent -- delisted/wrong-suffix/plan-restricted
        symbols that will never succeed no matter how many times this
        runs), or
      - CACHE_MAX_ROUNDS is reached.
    Alerts via Telegram only when it stops with errors still remaining, so
    a normal clean day stays silent.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    logger.info("SCHEDULER: starting job mode=cache_refresh @ %s", ts)

    child_env = {**os.environ, "APEX_SCHEDULED": "1", "PYTHONIOENCODING": "utf-8"}
    prev_err = None

    for round_num in range(1, CACHE_MAX_ROUNDS + 1):
        logger.info("cache_refresh round %d/%d", round_num, CACHE_MAX_ROUNDS)
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_td_cache.py")],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            env=child_env,
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if stdout:
            logger.info("[cache_refresh stdout]\n%s", stdout)
        if stderr:
            logger.warning("[cache_refresh stderr]\n%s", stderr)

        summary = _parse_cache_summary(stdout)
        if summary is None:
            logger.error(
                "cache_refresh round %d: could not parse ok/err summary (script may have "
                "crashed, returncode=%d) -- treating as a failed round.",
                round_num, result.returncode,
            )
            if round_num < CACHE_MAX_ROUNDS:
                time.sleep(CACHE_ROUND_DELAY)
                continue
            send_alert(
                "APEX cache refresh: build_td_cache.py did not produce a parseable summary "
                f"after {CACHE_MAX_ROUNDS} round(s) -- check logs.",
                level="WARNING",
                telegram_text=(
                    f"⚠️ <b>APEX CACHE REFRESH -- NO SUMMARY</b>\n"
                    f"──────────────────────\n"
                    f"Rounds attempted: {CACHE_MAX_ROUNDS}\n"
                    f"Script produced no parseable ok/err line -- check logs.\n"
                    f"──────────────────────"
                ),
            )
            return

        ok, partial, err = summary
        logger.info("cache_refresh round %d: ok=%d partial=%d err=%d", round_num, ok, partial, err)

        if err == 0:
            logger.info("cache_refresh: clean (0 errors) after %d round(s).", round_num)
            return

        if prev_err is not None and err >= prev_err:
            logger.warning(
                "cache_refresh: err count not improving (%d -> %d) after round %d -- "
                "stopping, remaining failures are likely permanent (delisted/plan-restricted symbols).",
                prev_err, err, round_num,
            )
            send_alert(
                f"APEX cache refresh: {err} ticker(s) still failing after {round_num} round(s), "
                "not improving further -- likely permanent (delisted/unsupported/plan-restricted symbols).",
                level="WARNING",
                telegram_text=(
                    f"⚠️ <b>APEX CACHE REFRESH -- RESIDUAL ERRORS</b>\n"
                    f"──────────────────────\n"
                    f"ok={ok}  err={err}  (after {round_num} round(s), not improving)\n"
                    f"Likely permanent -- check <code>_errors.jsonl</code> for the ticker list.\n"
                    f"──────────────────────"
                ),
            )
            return

        prev_err = err
        if round_num < CACHE_MAX_ROUNDS:
            logger.info(
                "cache_refresh: %d error(s) remain (improving) -- retrying in %ds.",
                err, CACHE_ROUND_DELAY,
            )
            time.sleep(CACHE_ROUND_DELAY)

    logger.warning(
        "cache_refresh: reached max rounds (%d) with %s error(s) still remaining.",
        CACHE_MAX_ROUNDS, prev_err,
    )
    send_alert(
        f"APEX cache refresh: reached max rounds ({CACHE_MAX_ROUNDS}) with {prev_err} error(s) remaining.",
        level="WARNING",
        telegram_text=(
            f"⚠️ <b>APEX CACHE REFRESH -- MAX ROUNDS REACHED</b>\n"
            f"──────────────────────\n"
            f"Rounds: {CACHE_MAX_ROUNDS}\n"
            f"Remaining errors: {prev_err}\n"
            f"Check: <code>_errors.jsonl</code>\n"
            f"──────────────────────"
        ),
    )


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

    # single-worker executor: APScheduler's default lets different jobs run
    # concurrently on separate threads -- fine normally, but a slow
    # cache_refresh (multi-day catch-up, rate-limit backoffs) can still be
    # mid-flight when the signals cron fires 20 minutes later, letting
    # signal generation read a HALF-updated cache: tickers already
    # refetched show fresh data, tickers not yet reached show whatever was
    # cached before. Found 2026-09-17: 11 tickers logged frozen 2026-09-14
    # signal values on the 2026-09-16 17:05 ET run while others came through
    # fresh, traced to cache_refresh still being on round 1/batch ~10-of-30
    # at the exact moment signals ran. Forcing a single worker thread means
    # every job (cache refresh, signals, execution, heartbeat) runs strictly
    # one at a time -- a job whose cron time arrives while another job still
    # holds the one worker simply waits (up to each job's misfire_grace_time
    # of 1800s) instead of racing it.
    scheduler = BlockingScheduler(
        timezone=SCHED_TZ,
        executors={"default": ThreadPoolExecutor(max_workers=1)},
    )
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
