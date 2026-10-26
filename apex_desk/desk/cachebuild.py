# apex_desk/desk/cachebuild.py
"""
Self-sufficient price-cache refresh: runs the old project's tools/build_td_cache.py (read-only reuse, same files the old
scheduler writes) so the desk never depends on the old scheduler being up. The builder skips tickers that are already
fresh (zero API credits), so running it after the old scheduler has finished is a cheap no-op.
"""
from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from typing import Any, Dict

from desk import clock, db, settings

log = logging.getLogger("desk.cachebuild")
_LOCK = threading.Lock()


def _cfg() -> Dict[str, Any]:
    return {"enabled": True, "script": "tools/build_td_cache.py", "min_gap_min": 30, "timeout_min": 90, "wait_external_min": 45,
            **(settings.cfg().get("cache_build") or {})}


def _external_build_running() -> bool:
    """True while another process (the old scheduler's cache refresh) is running build_td_cache.py."""
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            if "build_td_cache" in " ".join(p.info.get("cmdline") or []):
                return True
        return False
    except ImportError:
        pass
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*build_td_cache*' } | "
                              "Select-Object -ExpandProperty ProcessId"], capture_output=True, text=True, timeout=30).stdout
        return bool(out.strip())
    except Exception:                                      # noqa: BLE001
        return False


def _alert(msg: str, level: str = "WARNING") -> None:
    try:
        import prod.monitoring.alert as al
        al.send_alert(msg, level=level)
    except Exception as exc:                               # noqa: BLE001
        log.debug("alert failed: %s", exc)


def ensure_cache(reason: str, force: bool = False) -> Dict[str, Any]:
    """Build / top up the cache once (respecting min_gap_min between builds). Returns {'ran': bool, 'ok': bool, ...}."""
    c = _cfg()
    if not c["enabled"]:
        return {"ran": False, "why": "disabled"}
    if not _LOCK.acquire(blocking=False):
        return {"ran": False, "why": "build already running in the desk"}
    try:
        last = db.kv_get("cache_build_last") or {}
        gap = (clock.now_utc().timestamp() - float(last.get("ts", 0))) / 60
        if not force and gap < float(c["min_gap_min"]):
            return {"ran": False, "why": f"last build {gap:.0f} min ago"}
        waited = 0.0
        while _external_build_running():                   # old scheduler is mid-refresh: let it finish, then top up
            if waited == 0:
                log.info("Cache build already running elsewhere (old scheduler): waiting for it to finish")
            if waited >= float(c["wait_external_min"]) * 60:
                log.warning("External cache build still running after %d min: continuing anyway", int(c["wait_external_min"]))
                break
            time.sleep(30)
            waited += 30
        script = settings.REPO_ROOT / c["script"]
        if not script.exists():
            log.error("Cache builder not found: %s", script)
            return {"ran": False, "why": "builder missing"}
        log.info("Cache build starting (%s): %s", reason, script)
        t0 = time.time()
        try:
            r = subprocess.run([sys.executable, str(script)], cwd=str(settings.REPO_ROOT), capture_output=True, text=True,
                               timeout=float(c["timeout_min"]) * 60)
            ok = r.returncode == 0
            tail = (r.stdout or "").strip().splitlines()[-3:] + (r.stderr or "").strip().splitlines()[-3:]
        except subprocess.TimeoutExpired:
            ok, tail = False, [f"timed out after {c['timeout_min']} min"]
        secs = int(time.time() - t0)
        db.kv_set("cache_build_last", {"ts": clock.now_utc().timestamp(), "ok": ok, "reason": reason, "seconds": secs, "tail": tail})
        db.audit("system", "cache_build", {"ok": ok, "reason": reason, "seconds": secs, "tail": tail})
        if ok:
            log.info("Cache build finished in %ds: %s", secs, " | ".join(tail))
        else:
            log.error("Cache build FAILED after %ds: %s", secs, " | ".join(tail))
            _alert(f"[APEX DESK] Cache build failed ({reason}): {' | '.join(tail)[:300]}")
        return {"ran": True, "ok": ok, "seconds": secs}
    finally:
        _LOCK.release()
