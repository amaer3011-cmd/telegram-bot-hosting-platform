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
BOT_TOKEN_ENV_KEY = os.getenv("BOT_TOKEN_ENV_KEY", "BOT_TOKEN").strip() or "BOT_TOKEN"
# أقصى حجم لملف واحد يكتبه البوت (يحمي القرص من الامتلاء)
MAX_BOT_FILE_SIZE_MB = max(16, int(os.getenv("MAX_BOT_FILE_SIZE_MB", "512")))
# كاش pip مشترك بين كل البوتات: يسرّع التثبيت ويوفر الشبكة والقرص
PIP_CACHE_DIR = Path(os.getenv("PIP_CACHE_DIR", str(DB_PATH.parent / "pip-cache"))).resolve()
# فاصل بين تشغيل بوت وآخر عند الإقلاع حتى لا ترتفع الذاكرة/المعالج دفعة واحدة
BOOT_RECOVERY_STAGGER_SECONDS = max(0, int(os.getenv("BOOT_RECOVERY_STAGGER_SECONDS", "3")))
# المهلة التي ننتظرها لإيقاف كل البوتات بلطف عند إيقاف المنصة قبل القتل القسري
SHUTDOWN_GRACE_SECONDS = max(1, int(os.getenv("SHUTDOWN_GRACE_SECONDS", "10")))
# عزل البوتات: كل بوت يعمل بمستخدم نظام مستقل (UID) فلا يقرأ قاعدة البيانات ولا
# مفتاح التشفير ولا ملفات بقية البوتات. يتطلب تشغيل المنصة كـroot (auto = مفعّل إن كنا root).
_isolate_mode = os.getenv("ISOLATE_BOTS", "auto").strip().lower()
ISOLATION_ACTIVE = (
    os.name == "posix"
    and hasattr(os, "geteuid")
    and os.geteuid() == 0
    and _isolate_mode not in ("false", "0", "no", "off")
)
BOT_UID_BASE = max(1000, int(os.getenv("BOT_UID_BASE", "20000")))
# أقصى عدد عمليات/خيوط لكل بوت (حماية من fork bomb؛ يعمل فقط مع العزل لأنه حد لكل UID)
MAX_BOT_PROCESSES = max(16, int(os.getenv("MAX_BOT_PROCESSES", "256")))
# حصة القرص لكل بوت (الملفات + البيئة الافتراضية)؛ 0 = بلا حد
MAX_BOT_DISK_MB = max(0, int(os.getenv("MAX_BOT_DISK_MB", "1024")))
DISK_QUOTA_CHECK_SECONDS = max(60, int(os.getenv("DISK_QUOTA_CHECK_SECONDS", "300")))
# تنبيه الأدمن عندما تنخفض المساحة الحرة في الـVolume عن هذه النسبة (0 = معطّل)
DISK_ALERT_FREE_PERCENT = max(0, min(50, int(os.getenv("DISK_ALERT_FREE_PERCENT", "10"))))
DISK_ALERT_COOLDOWN_SECONDS = max(300, int(os.getenv("DISK_ALERT_COOLDOWN_SECONDS", "21600")))
ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
# مدة الاحتفاظ بسجل التدقيق بالأيام
AUDIT_RETENTION_DAYS = max(7, int(os.getenv("AUDIT_RETENTION_DAYS", "90")))
# متغيرات بيئة المنصة التي يجب ألا تصل أبدًا إلى البوتات المستضافة
HOST_SECRET_ENV_KEYS = {
    "HOST_BOT_TOKEN", "ENCRYPTION_KEY", "ADMIN_IDS", "DATABASE_PATH", "BOTS_DIR",
    "POSTGRES_PASSWORD", "REDIS_PASSWORD", "DASHBOARD_PASSWORD", "DASHBOARD_SECRET",
}
RESTART_INTERVAL_CHOICES_HOURS = (0, 6, 12, 24, 48)
PROTECTED_ENV_KEYS = {
    "PATH",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "PYTHONHOME",
    "PYTHONPATH",
    "PYTHONSTARTUP",
}

# Rate limiting: max messages per user per minute (0 = disabled)
RATE_LIMIT_MSGS_PER_MINUTE = int(os.getenv("RATE_LIMIT_MSGS_PER_MINUTE", "10"))

# Encryption
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "").strip()

# Docker settings
USE_DOCKER = os.getenv("USE_DOCKER", "false").lower() in ("true", "1", "yes")
DOCKER_SOCKET = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")

# Web Dashboard settings
DASHBOARD_ENABLED = os.getenv("DASHBOARD_ENABLED", "true").lower() in ("true", "1", "yes")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")

# PostgreSQL/Redis settings (optional - for production scaling)
USE_POSTGRESQL = os.getenv("USE_POSTGRESQL", "false").lower() in ("true", "1", "yes")
POSTGRES_HOST = os.getenv("POSTGRES_HOST", "localhost")
POSTGRES_PORT = int(os.getenv("POSTGRES_PORT", "5432"))
POSTGRES_DB = os.getenv("POSTGRES_DB", "bot_hosting")
POSTGRES_USER = os.getenv("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.getenv("POSTGRES_PASSWORD", "")

USE_REDIS = os.getenv("USE_REDIS", "false").lower() in ("true", "1", "yes")
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD", "")

# AI Analysis settings
AI_ANALYSIS_ENABLED = os.getenv("AI_ANALYSIS_ENABLED", "true").lower() in ("true", "1", "yes")
AI_MAX_FILE_SIZE_KB = int(os.getenv("AI_MAX_FILE_SIZE_KB", "512"))  # الحد الأقصى لحجم الملف للتحليل

BOTS_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
PIP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
