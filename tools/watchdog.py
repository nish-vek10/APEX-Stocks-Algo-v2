# tools/watchdog.py
"""
APEX Scheduler Watchdog — dead-man's-switch alert.

MUST run as its OWN independently-scheduled process (Windows Task
Scheduler), separate from scheduler.py. If scheduler.py's own process
dies -- closed terminal, PC restart, crash, sleep -- nothing inside that
same process can ever alert you, because the thing that would send the
alert is exactly what just died. This script solves that by living
outside scheduler.py entirely: it just reads state/heartbeat.json (written
every HEARTBEAT_MIN minutes by scheduler.py's job_heartbeat()) and fires a
Telegram alert if that file hasn't been updated recently.

Found 2026-09-21/22: scheduler.py sat dead over a weekend with nobody
noticing until the next manual status check, missing a full trading day's
execution (2026-09-21's 09:31 ET run never fired). This closes that gap.

Alert dedup: writes state/watchdog_alerted.json once an alert fires, so a
watchdog run every 15 min doesn't spam Telegram every 15 min for the same
outage. Cleared automatically the moment the heartbeat is fresh again, so
the NEXT outage still alerts.

Usage (run this, standalone, on a schedule independent of scheduler.py):
    python tools/watchdog.py

Windows Task Scheduler setup (run once, from an elevated PowerShell):
    schtasks /create /tn "APEX Watchdog" /tr "python C:\\path\\to\\tools\\watchdog.py" ^
        /sc minute /mo 15 /ru SYSTEM /f
(adjust the python/script path to match your actual install)
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from prod.monitoring.alert import send_alert

HEARTBEAT_FILE = ROOT / "state" / "heartbeat.json"
ALERTED_FILE = ROOT / "state" / "watchdog_alerted.json"

# How stale the heartbeat can be before we consider the scheduler dead.
# HEARTBEAT_MIN in scheduler.py defaults to 10 -- this gives a full missed
# beat plus buffer before alerting, so normal jitter doesn't false-alarm.
STALE_THRESHOLD_MIN = 20


def _read_last_heartbeat() -> datetime | None:
    if not HEARTBEAT_FILE.exists():
        return None
    try:
        data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
        return datetime.fromisoformat(data["last_heartbeat_utc"])
    except Exception:
        return None


def _already_alerted() -> bool:
    return ALERTED_FILE.exists()


def _mark_alerted() -> None:
    ALERTED_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERTED_FILE.write_text(
        json.dumps({"alerted_at_utc": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )


def _clear_alerted() -> None:
    if ALERTED_FILE.exists():
        ALERTED_FILE.unlink()


def main() -> None:
    now = datetime.now(timezone.utc)
    last_beat = _read_last_heartbeat()

    if last_beat is None:
        # Never seen a heartbeat at all -- scheduler.py has never run
        # since this feature was added, or state/ was wiped. Alert once.
        if not _already_alerted():
            send_alert(
                "APEX watchdog: no heartbeat file found at all -- scheduler.py "
                "may never have been started, or state/ was cleared.",
                level="CRITICAL",
                telegram_text=(
                    "🔥 <b>APEX WATCHDOG -- NO HEARTBEAT EVER SEEN</b>\n"
                    "──────────────────────\n"
                    "state/heartbeat.json does not exist.\n"
                    "Check whether scheduler.py is running on the Work PC.\n"
                    "──────────────────────"
                ),
            )
            _mark_alerted()
        return

    age_min = (now - last_beat).total_seconds() / 60.0

    if age_min > STALE_THRESHOLD_MIN:
        if not _already_alerted():
            send_alert(
                f"APEX watchdog: scheduler heartbeat is {age_min:.0f} min old "
                f"(threshold {STALE_THRESHOLD_MIN}m) -- scheduler.py appears dead.",
                level="CRITICAL",
                telegram_text=(
                    "🔥 <b>APEX WATCHDOG -- SCHEDULER APPEARS DEAD</b>\n"
                    "──────────────────────\n"
                    f"Last heartbeat: {age_min:.0f} min ago\n"
                    f"Threshold: {STALE_THRESHOLD_MIN} min\n"
                    "scheduler.py is likely not running -- check the Work PC.\n"
                    "──────────────────────"
                ),
            )
            _mark_alerted()
    else:
        # Heartbeat is fresh -- clear any prior alert flag so the NEXT
        # outage (a genuinely new one) alerts again instead of staying
        # silenced forever after the first incident.
        _clear_alerted()


if __name__ == "__main__":
    main()
