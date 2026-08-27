from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
HOST_BOT_TOKEN = os.getenv("HOST_BOT_TOKEN", "").strip()
ADMIN_IDS = tuple(int(value.strip()) for value in os.getenv("ADMIN_IDS", "").split(",") if value.strip().isdigit())
BOTS_DIR = Path(os.getenv("BOTS_DIR", str(BASE_DIR / "uploaded_bots"))).resolve()
DB_PATH = Path(os.getenv("DATABASE_PATH", str(BASE_DIR / "data" / "hosting.db"))).resolve()
PORT = int(os.getenv("PORT", "8080"))
DEFAULT_MAX_BOTS = max(1, int(os.getenv("DEFAULT_MAX_BOTS", "3")))
WATCHDOG_INTERVAL = max(5, int(os.getenv("WATCHDOG_INTERVAL", "20")))
MAX_FILE_SIZE_MB = max(1, int(os.getenv("MAX_FILE_SIZE_MB", "25")))
MAX_EXTRACTED_SIZE_MB = max(1, int(os.getenv("MAX_EXTRACTED_SIZE_MB", "200")))
MAX_ZIP_FILE_COUNT = max(1, int(os.getenv("MAX_ZIP_FILE_COUNT", "2000")))
LOG_TAIL_LINES = max(10, int(os.getenv("LOG_TAIL_LINES", "60")))
MAX_AUTO_RESTART_ATTEMPTS = max(1, int(os.getenv("MAX_AUTO_RESTART_ATTEMPTS", "5")))
CRASH_LOOP_RESET_SECONDS = max(60, int(os.getenv("CRASH_LOOP_RESET_SECONDS", "300")))
RESTART_BACKOFF_BASE_SECONDS = max(1, int(os.getenv("RESTART_BACKOFF_BASE_SECONDS", "5")))
MAX_BOT_MEMORY_MB = max(128, int(os.getenv("MAX_BOT_MEMORY_MB", "1024")))
MAX_BOT_CPU_SECONDS = max(60, int(os.getenv("MAX_BOT_CPU_SECONDS", "3600")))
VENV_SETUP_TIMEOUT_SECONDS = max(30, int(os.getenv("VENV_SETUP_TIMEOUT_SECONDS", "300")))
MAX_LOG_SIZE_MB = max(1, int(os.getenv("MAX_LOG_SIZE_MB", "10")))
MAX_CONCURRENT_VENV_SETUPS = max(1, int(os.getenv("MAX_CONCURRENT_VENV_SETUPS", "2")))
CLEANUP_INTERVAL_SECONDS = max(60, int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300")))
USAGE_HISTORY_INTERVAL_SECONDS = max(30, int(os.getenv("USAGE_HISTORY_INTERVAL_SECONDS", "300")))
USAGE_HISTORY_MAX_POINTS = max(1, int(os.getenv("USAGE_HISTORY_MAX_POINTS", "50")))
HEALTH_CHECK_TIMEOUT_SECONDS = max(1, int(os.getenv("HEALTH_CHECK_TIMEOUT_SECONDS", "6")))
BOT_TOKEN_ENV_KEY = "BOT_TOKEN"
RESTART_INTERVAL_CHOICES_HOURS = (0, 6, 12, 24, 48)
PROTECTED_ENV_KEYS = {
    "PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
}

BOTS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
