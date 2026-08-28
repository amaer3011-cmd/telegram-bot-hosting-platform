"""
Automatic backup system for the Telegram Bot Hosting Platform.

Provides:
    1. SQLite database hot-backup (safe copy while the DB is in use)
    2. Compressed archive of bot folders (excluding .venv and run.log)
    3. Log archive: rotated log files bundled into timestamped tar.gz

Backups are stored under BACKUP_DIR with the layout:
    BACKUP_DIR/
    ├── db/
    │   ├── hosting_20260828_120000.db
    │   └── ...
    ├── bots/
    │   ├── bots_20260828_120000.tar.gz
    │   └── ...
    └── logs/
        ├── logs_20260828_120000.tar.gz
        └── ...

Retention: only the last BACKUP_RETENTION_COUNT backups of each type are kept;
older ones are deleted automatically.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tarfile
import threading
import time
from datetime import datetime
from pathlib import Path

import config

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(config.DB_PATH.parent / "backups"))).resolve()
BACKUP_RETENTION_COUNT = int(os.getenv("BACKUP_RETENTION_COUNT", "7"))
BACKUP_INTERVAL_SECONDS = int(os.getenv("BACKUP_INTERVAL_SECONDS", "3600"))  # 1 hour default
BACKUP_DB = bool(os.getenv("BACKUP_DB", "true").lower() != "false")
BACKUP_BOTS = bool(os.getenv("BACKUP_BOTS", "true").lower() != "false")
BACKUP_LOGS = bool(os.getenv("BACKUP_LOGS", "true").lower() != "false")

BACKUP_DIR.mkdir(parents=True, exist_ok=True)
(BACKUP_DIR / "db").mkdir(exist_ok=True)
(BACKUP_DIR / "bots").mkdir(exist_ok=True)
(BACKUP_DIR / "logs").mkdir(exist_ok=True)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _cleanup_old(backup_type: str, keep: int = BACKUP_RETENTION_COUNT) -> int:
    """Remove old backups of a given type, keeping only the most recent `keep`."""
    directory = BACKUP_DIR / backup_type
    if not directory.exists():
        return 0
    files = sorted(directory.iterdir(), key=lambda p: p.stat().st_mtime)
    to_delete = files[:-keep] if len(files) > keep else []
    for f in to_delete:
        try:
            f.unlink()
        except Exception:
            pass
    return len(to_delete)


# ---------------------------------------------------------------------------
# Database backup (hot-backup via SQLite backup API)
# ---------------------------------------------------------------------------

def backup_database() -> Path | None:
    """Create a hot-backup copy of the SQLite database.

    Uses sqlite3.Connection.backup() which is safe to call while the DB
    is being used by other connections (the WAL mode helps here too).

    Returns the path to the backup file, or None on failure.
    """
    if not BACKUP_DB:
        return None
    try:
        src = sqlite3.connect(str(config.DB_PATH))
        dst_path = BACKUP_DIR / "db" / f"hosting_{_timestamp()}.db"
        dst = sqlite3.connect(str(dst_path))
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        _cleanup_old("db")
        return dst_path
    except Exception as exc:
        import logging

        logging.getLogger("backup").exception("Database backup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Bot folders archive
# ---------------------------------------------------------------------------

def backup_bot_folders() -> Path | None:
    """Create a tar.gz archive of all bot folders (excluding .venv and run.log).

    Returns the path to the archive, or None on failure.
    """
    if not BACKUP_BOTS:
        return None
    try:
        archive_path = BACKUP_DIR / "bots" / f"bots_{_timestamp()}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            bots_dir = config.BOTS_DIR
            if bots_dir.exists():
                for owner_dir in bots_dir.iterdir():
                    if not owner_dir.is_dir():
                        continue
                    for bot_dir in owner_dir.iterdir():
                        if not bot_dir.is_dir():
                            continue
                        arcname = bot_dir.relative_to(bots_dir)
                        tar.add(bot_dir, arcname=arcname, exclude=lambda m: m.endswith(".venv") or m.endswith("run.log"))
        _cleanup_old("bots")
        return archive_path
    except Exception as exc:
        import logging

        logging.getLogger("backup").exception("Bot folders backup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Log archive (rotated + current logs bundled)
# ---------------------------------------------------------------------------

def backup_logs() -> Path | None:
    """Create a tar.gz archive of rotated log files and current run.log files.

    This collects log files that have been rotated (via rotate_log_if_needed
    in utils.py) plus the current run.log files, bundling them into a single
    timestamped archive for long-term storage.

    Returns the path to the archive, or None on failure.
    """
    if not BACKUP_LOGS:
        return None
    try:
        archive_path = BACKUP_DIR / "logs" / f"logs_{_timestamp()}.tar.gz"
        with tarfile.open(archive_path, "w:gz") as tar:
            bots_dir = config.BOTS_DIR
            if bots_dir.exists():
                for owner_dir in bots_dir.iterdir():
                    if not owner_dir.is_dir():
                        continue
                    for bot_dir in owner_dir.iterdir():
                        if not bot_dir.is_dir():
                            continue
                        log_path = bot_dir / "run.log"
                        if log_path.exists():
                            tar.add(log_path, arcname=log_path.relative_to(bots_dir))
        _cleanup_old("logs")
        return archive_path
    except Exception as exc:
        import logging

        logging.getLogger("backup").exception("Logs backup failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Full backup cycle
# ---------------------------------------------------------------------------

def run_backup_cycle() -> dict:
    """Run a complete backup cycle (DB + bots + logs).

    Returns a summary dict with paths of created backups (or None for failures).
    """
    result = {"timestamp": _timestamp(), "db": None, "bots": None, "logs": None}
    result["db"] = backup_database()
    result["bots"] = backup_bot_folders()
    result["logs"] = backup_logs()
    return result


# ---------------------------------------------------------------------------
# Background backup thread
# ---------------------------------------------------------------------------

_backup_thread: threading.Thread | None = None
_backup_stop_event: threading.Event | None = None


def start_backup_scheduler(interval: int = BACKUP_INTERVAL_SECONDS) -> None:
    """Start a background thread that runs backup cycles periodically.

    The thread logs each backup result. It stops when the stop event is set
    or the process exits.
    """
    global _backup_thread, _backup_stop_event
    if _backup_thread and _backup_thread.is_alive():
        return  # already running

    _backup_stop_event = threading.Event()

    def _run():
        import logging

        logger = logging.getLogger("backup")
        logger.info("Backup scheduler started (interval=%s seconds)", interval)
        while not _backup_stop_event.is_set():
            try:
                result = run_backup_cycle()
                db_ok = "✓" if result["db"] else "✗"
                bots_ok = "✓" if result["bots"] else "✗"
                logs_ok = "✓" if result["logs"] else "✗"
                logger.info(
                    "Backup cycle complete: DB %s (%s), Bots %s, Logs %s",
                    db_ok,
                    result["db"].name if result["db"] else "failed",
                    bots_ok,
                    logs_ok,
                )
            except Exception:
                logger.exception("Backup cycle failed with exception")
            _backup_stop_event.wait(interval)

    _backup_thread = threading.Thread(target=_run, name="backup-scheduler", daemon=True)
    _backup_thread.start()


def stop_backup_scheduler() -> None:
    """Signal the backup scheduler thread to stop."""
    global _backup_stop_event
    if _backup_stop_event:
        _backup_stop_event.set()
