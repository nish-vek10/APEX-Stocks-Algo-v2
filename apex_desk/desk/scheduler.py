# apex_desk/desk/scheduler.py
"""Daily jobs (America/New_York clock): signal scan after the old system's cache+signals, snapshot, and open-time execution."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.blocking import BlockingScheduler

from desk import engine, settings
from desk.executor import MT5_LOCK, Executor

log = logging.getLogger("desk.scheduler")


def job_scan() -> None:
    """Scan, and keep retrying while the old scheduler's cache refresh is still catching up (stale cache = no signals stored)."""
    t = settings.cfg()["timing"]
    gap = int(t.get("scan_retry_min", 5)) * 60
    deadline = time.monotonic() + float(t.get("scan_retry_max_hours", 15)) * 3600
    n = 0
    while True:
        n += 1
        try:
            r = engine.run_scan()
        except Exception as e:  # noqa: BLE001
            log.exception("scan crashed: %s", e)
            r = {"ok": False, "error": str(e)}
        if r.get("ok"):
            log.info("Scan OK on attempt %d: %s", n, r.get("tier_counts"))
            break
        log.warning("Scan attempt %d not usable (stale cache or error): %s. Retrying in %d min.", n, r.get("signal_date") or r.get("error"), gap // 60)
        if time.monotonic() > deadline:
            log.error("Scan gave up after %d attempts: cache never became fresh", n)
            break
        time.sleep(gap)
    job_broker_modes()
    job_sync()


def job_broker_modes() -> None:
    """Flag setups the broker will not open (close-only etc.) so they show on the dashboard before review."""
    if not MT5_LOCK.acquire(blocking=False):
        return
    try:
        ex = Executor()
        ex.broker.connect()
        try:
            ex.check_broker_modes()
        finally:
            ex.broker.disconnect()
    except Exception as e:  # noqa: BLE001
        log.warning("broker mode check failed: %s", e)
    finally:
        MT5_LOCK.release()


def job_execute() -> None:
    Executor().run_session()


def job_snapshot() -> None:
    if not MT5_LOCK.acquire(blocking=False):                # executor is running: never disturb its MT5 connection
        log.info("Snapshot skipped: an MT5 job is running")
        return
    try:
        ex = Executor()
        ex.broker.connect()
        try:
            ex.snapshot()
            ex.reconcile()
            ex.check_broker_modes()
        finally:
            ex.broker.disconnect()
    finally:
        MT5_LOCK.release()


def job_sync() -> None:
    from desk import sync
    try:
        sync.cycle()
    except Exception as e:  # noqa: BLE001
        log.warning("sync error: %s", e)


def main() -> None:
    t = settings.cfg()["timing"]
    sh, sm = map(int, t["scan_time_et"].split(":"))
    eh, em = map(int, t["exec_time_et"].split(":"))
    sched = BlockingScheduler(timezone="America/New_York")
    sched.add_job(job_scan, "cron", day_of_week="mon-fri", hour=sh, minute=sm, id="scan", misfire_grace_time=3600)
    sched.add_job(job_execute, "cron", day_of_week="mon-fri", hour=eh, minute=em, id="execute", misfire_grace_time=600)
    sched.add_job(job_scan, "date", run_date=datetime.now(timezone.utc) + timedelta(seconds=20), id="scan_startup")   # catch up after restarts / late cache
    sched.add_job(job_snapshot, "interval", minutes=int(t["snapshot_every_min"]), id="snapshot")
    from desk import sync
    if sync.enabled():
        sched.add_job(job_sync, "interval", seconds=int(settings.cfg().get("sync", {}).get("interval_sec", 60)), id="sync", max_instances=1, coalesce=True)
    log.info("Desk scheduler up | mode=%s | scan %02d:%02d ET | execute %02d:%02d ET", settings.cfg()["mode"], sh, sm, eh, em)
    print(f"[desk-scheduler] mode={settings.cfg()['mode']} scan={sh:02d}:{sm:02d} ET execute={eh:02d}:{em:02d} ET (Ctrl+C to stop)")
    sched.start()
