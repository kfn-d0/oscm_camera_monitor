from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from .config import AppConfig, CameraConfig
from .database import Database
from .healthcheck import check_camera
from .utils import Notifier

logger = logging.getLogger(__name__)

_camera_locks: Dict[str, asyncio.Lock] = {}


def _get_camera_lock(camera_id: str) -> asyncio.Lock:
    if camera_id not in _camera_locks:
        _camera_locks[camera_id] = asyncio.Lock()
    return _camera_locks[camera_id]


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _fmt_timestamp(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso).astimezone()
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return iso


async def _check_camera_async(
    cam: CameraConfig,
    db: Database,
    notifier: Notifier,
    cfg: AppConfig,
) -> None:
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, check_camera, cam.host, cam.ports, cfg.timeout, cam.icmp_only
    )
    ts = _now_iso()

    db.insert_check(cam.id, ts, result.ok, result.rtt_ms, result.method, result.detail, result.port)

    lock = _get_camera_lock(cam.id)
    async with lock:
        state = await loop.run_in_executor(
            None,
            db.update_state_atomic,
            cam.id,
            result.ok,
            cfg.failure_threshold,
            cfg.recovery_threshold,
            ts,
        )

        direction = state["direction"]

        if direction == "went_online":
            ticket = await loop.run_in_executor(None, db.get_open_ticket, cam.id)
            if ticket:
                await loop.run_in_executor(None, db.close_ticket, ticket["id"], ts)
                try:
                    duration = _fmt_duration(
                        (
                            datetime.fromisoformat(ts)
                            - datetime.fromisoformat(ticket["opened_at"])
                        ).total_seconds()
                    )
                except Exception:
                    duration = "—"
                await notifier.notify_online_async(
                    cam.name, cam.host, _fmt_timestamp(ts), duration
                )
            logger.info(
                "RECOVERED %s after %d successes", cam.name, state["successes"]
            )

        elif direction == "went_offline":
            existing = await loop.run_in_executor(None, db.get_open_ticket, cam.id)
            if not existing:
                await loop.run_in_executor(
                    None, db.open_ticket, cam.id, result.detail, ts
                )
                await notifier.notify_offline_async(
                    cam.name, cam.host, _fmt_timestamp(ts), result.detail, cam.location
                )
                logger.warning(
                    "OFFLINE %s failures=%d reason=%s",
                    cam.name,
                    state["failures"],
                    result.detail,
                )

    logger.log(
        logging.DEBUG if result.ok else logging.INFO,
        "CHECK %-25s %s",
        cam.name,
        result.detail,
    )


def get_process_memory_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    return 0.0


async def run_monitor_loop(
    cfg: AppConfig, db: Database, notifier: Notifier, once: bool = False
) -> None:
    logger.info("Monitor started - %d cameras", len(cfg.enabled_cameras))
    cleanup_task = None
    if not once and cfg.retention_days > 0:
        cleanup_task = asyncio.create_task(
            run_cleanup_loop(db._path, cfg.retention_days, cfg.vacuum_interval_days)
        )

    try:
        while True:
            tasks = [
                _check_camera_async(cam, db, notifier, cfg)
                for cam in cfg.enabled_cameras
            ]
            await asyncio.gather(*tasks, return_exceptions=True)
            if once:
                break
            await asyncio.sleep(cfg.check_interval)
    finally:
        if cleanup_task:
            cleanup_task.cancel()


async def run_cleanup_loop(
    db_path: str, retention_days: int, vacuum_interval_days: int
) -> None:
    logger.info("Cleanup loop started (retention=%dd)", retention_days)
    await asyncio.sleep(60)
    loop = asyncio.get_event_loop()
    vacuum_counter = 0

    while True:
        try:
            cutoff = (
                datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
            ).isoformat()

            def _do_cleanup() -> int:
                with sqlite3.connect(db_path) as conn:
                    cur = conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
                    return cur.rowcount

            deleted = await loop.run_in_executor(None, _do_cleanup)
            if deleted:
                logger.info("CLEANUP removed %d old records", deleted)

            vacuum_counter += 1
            if vacuum_counter >= vacuum_interval_days:
                vacuum_counter = 0

                def _do_vacuum() -> None:
                    with sqlite3.connect(db_path) as conn:
                        conn.execute("VACUUM")

                await loop.run_in_executor(None, _do_vacuum)
                logger.info("VACUUM completed")

        except Exception as exc:
            logger.error("Cleanup error: %s", exc)

        await asyncio.sleep(86400)


def get_disk_usage(path: str) -> dict:
    import shutil
    try:
        total, used, free = shutil.disk_usage(path)
        return {
            "total_gb": round(total / 1024**3, 1),
            "used_gb":  round(used  / 1024**3, 1),
            "free_gb":  round(free  / 1024**3, 1),
            "pct":      round(used / total * 100, 1),
        }
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "pct": 0}


def get_memory_usage() -> dict:
    try:
        with open("/proc/meminfo") as f:
            m = {
                line.split()[0]: int(line.split()[1]) * 1024
                for line in f
                if line.split()[0] in ("MemTotal:", "MemAvailable:")
            }
        total, avail = m["MemTotal:"], m["MemAvailable:"]
        used = total - avail
        return {
            "total_gb": round(total / 1024**3, 1),
            "used_gb":  round(used  / 1024**3, 1),
            "free_gb":  round(avail / 1024**3, 1),
            "pct":      round(used / total * 100, 1),
        }
    except Exception:
        return {"total_gb": 8, "used_gb": 0, "free_gb": 8, "pct": 0}


def get_db_detailed_stats(
    db_path: str, retention_days: int, num_cameras: int, interval_s: int
) -> dict:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        stats: dict = {}
        row = conn.execute("SELECT COUNT(*) as cnt FROM checks").fetchone()
        stats["checks_total"] = row["cnt"]
        row = conn.execute("SELECT MIN(ts) as oldest FROM checks").fetchone()
        stats["checks_oldest"] = row["oldest"]
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tickets WHERE status='OPEN'"
        ).fetchone()
        stats["tickets_open"] = row["cnt"]
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM tickets WHERE status='RESOLVED'"
        ).fetchone()
        stats["tickets_resolved"] = row["cnt"]

    db_size_bytes = Path(db_path).stat().st_size if Path(db_path).exists() else 0
    stats["db_size_mb"] = round(db_size_bytes / 1024**2, 2)
    stats["db_path"] = db_path

    checks_per_day = num_cameras * (86400 // interval_s)
    stats["checks_per_day"]  = checks_per_day
    stats["checks_per_year"] = checks_per_day * 365

    bytes_per_check = (
        (db_size_bytes // stats["checks_total"]) if stats["checks_total"] > 0 else 200
    )
    bytes_per_check = max(150, bytes_per_check)

    stats["projected_db_mb_with_retention"] = round(
        (checks_per_day * retention_days * bytes_per_check) / 1024**2, 1
    )
    stats["projected_db_mb_year_no_retention"] = round(
        (checks_per_day * 365 * bytes_per_check) / 1024**2, 1
    )

    stats.update(
        {
            "retention_days":   retention_days,
            "num_cameras":      num_cameras,
            "check_interval_s": interval_s,
        }
    )

    log_files = list(Path(".").glob("*.log*")) + list(
        Path("/var/log/").glob("camera-monitor.log*")
    )
    stats["log_size_mb"] = round(
        sum(f.stat().st_size for f in log_files if f.is_file()) / 1024**2, 2
    )

    return stats