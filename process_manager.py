import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.request

import psutil

import config
import database as db
from utils import (
    dir_size_bytes,
    ensure_venv,
    human_size,
    human_uptime,
    make_resource_limiter,
    prepare_bot_dir,
    rotate_log_if_needed,
    run_as_kwargs,
    tail_file,
    venv_path,
    venv_python,
)

logger = logging.getLogger("process-manager")

# متغيرات البيئة الوحيدة التي تُورَّث من المنصة إلى البوتات المستضافة. نستخدم قائمة
# سماح (allowlist) بدل نسخ os.environ كاملًا، حتى لا تتسرب أسرار المنصة
# (HOST_BOT_TOKEN وENCRYPTION_KEY وغيرها) إلى كود المستخدمين المرفوع.
_INHERITED_ENV_KEYS = (
    "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
)


def build_bot_env(bot_id, folder):
    """يبني بيئة تشغيل نظيفة للبوت: قائمة السماح + متغيرات المستخدم المخزنة."""
    env = {k: os.environ[k] for k in _INHERITED_ENV_KEYS if k in os.environ}
    user_vars = db.get_env_vars(bot_id)
    for key, value in user_vars.items():
        # المفاتيح المحمية لا يستطيع المستخدم تغييرها (PATH وLD_PRELOAD...)
        if key in config.PROTECTED_ENV_KEYS:
            continue
        env[key] = value

    venv_dir = venv_path(folder)
    bin_dir = os.path.join(venv_dir, "Scripts" if os.name == "nt" else "bin")
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", os.defpath)
    env["VIRTUAL_ENV"] = venv_dir
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # HOME وTMPDIR داخل مجلد البوت حتى لا تتشارك البوتات الملفات المؤقتة وكاش المكتبات
    tmp_dir = os.path.join(folder, ".tmp")
    env["HOME"] = folder
    env["TMPDIR"] = tmp_dir
    return env


def _signal_group(pid, sig):
    """يرسل إشارة لمجموعة العملية كاملة (البوت وأي عمليات فرعية أطلقها)."""
    try:
        os.killpg(pid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        try:
            os.kill(pid, sig)
            return True
        except OSError:
            return False


class ProcessManager:
    def __init__(self, bot_notifier=None):
        self.processes = {}  # bot_id -> subprocess.Popen
        self.lock = threading.Lock()
        self.notifier = bot_notifier  # دالة لإرسال إشعار لصاحب البوت: notifier(user_id, text)
        self._recovering = set()  # bot_ids قيد محاولة إعادة تشغيل تلقائي حاليًا (لمنع التكرار المتوازي)
        self._recovering_lock = threading.Lock()
        # يحدّ من عدد عمليات تجهيز venv/pip install المتزامنة على مستوى السيرفر
        # كله، حتى لا يُنهك عدة مستخدمين يضغطون "تشغيل" في نفس اللحظة المعالج/الشبكة
        self._venv_semaphore = threading.Semaphore(config.MAX_CONCURRENT_VENV_SETUPS)
        self._last_usage_snapshot = 0.0
        self._last_audit_prune = 0.0
        self._last_disk_alert = 0.0
        # قفل لكل بوت: يمنع تشغيلين متزامنين لنفس البوت (مستخدم + حارس مثلًا)
        self._bot_locks = {}
        self._bot_locks_guard = threading.Lock()
        self._ps_cache = {}  # pid -> psutil.Process (لقياس CPU دون حجب)
        self._kill_reasons = {}  # bot_id -> سبب إيقاف قسري من المنصة (مثل تجاوز الذاكرة)
        self.last_watchdog_beat = time.time()
        self.boot_recovery_done = False

    @staticmethod
    def _log_path(folder):
        return os.path.join(folder, "run.log")

    def _lock_for(self, bot_id):
        with self._bot_locks_guard:
            return self._bot_locks.setdefault(bot_id, threading.Lock())

    def _notify(self, user_id, text):
        if not self.notifier:
            return
        try:
            self.notifier(user_id, text)
        except Exception:
            logger.exception("Notifier failed for user %s", user_id)

    # ---------------- تشغيل / إيقاف ----------------

    def start_bot(self, bot_row):
        bot_id = bot_row["bot_id"]
        with self._lock_for(bot_id):
            return self._start_bot_locked(bot_row)

    def _start_bot_locked(self, bot_row):
        bot_id = bot_row["bot_id"]
        folder = bot_row["folder"]
        entry = bot_row["entry_file"]

        if not folder or not entry or not os.path.exists(entry):
            return False, "ملفات البوت غير موجودة."
        log_path = self._log_path(folder)

        with self.lock:
            existing = self.processes.get(bot_id)
            if existing and existing.poll() is None:
                return False, "البوت يعمل بالفعل."

        try:
            prepare_bot_dir(folder, bot_id)
        except OSError as e:
            db.set_last_error(bot_id, str(e))
            return False, f"فشل تجهيز مجلد البوت: {e}"
        run_as = run_as_kwargs(bot_id)

        # نحصر عدد عمليات تجهيز البيئة الافتراضية المتزامنة عبر Semaphore عام؛
        # طلبات إضافية تنتظر دورها بدل إطلاق pip install كثيرة معًا
        with self._venv_semaphore:
            try:
                ensure_venv(folder, log_path, run_as)
            except Exception as e:
                db.set_last_error(bot_id, str(e))
                return False, f"فشل تجهيز البيئة الافتراضية: {e}"

        py = venv_python(folder)
        try:
            env = build_bot_env(bot_id, folder)
        except Exception as e:
            db.set_last_error(bot_id, str(e))
            return False, f"فشل تجهيز بيئة التشغيل: {e}"

        # اقتطاع السجل إن تجاوز الحد الأقصى للحجم قبل فتحه للإلحاق، لمنعه من
        # النمو بلا حدود واستهلاك مساحة القرص مع الوقت
        rotate_log_if_needed(log_path, config.MAX_LOG_SIZE_MB)

        try:
            bot_memory_mb = bot_row["max_memory_mb"]
        except (KeyError, IndexError):
            bot_memory_mb = None
        memory_limit_mb = bot_memory_mb or config.MAX_BOT_MEMORY_MB
        limiter = make_resource_limiter(memory_limit_mb, config.MAX_BOT_CPU_SECONDS)

        # العملية الفرعية تحتفظ بنسخة من واصف السجل؛ نغلق نسختنا نحن فور الإطلاق
        # حتى لا يتسرب واصف ملف مفتوح مع كل تشغيل/إعادة تشغيل
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n\n===== بدء التشغيل {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
            log_file.flush()
            try:
                proc = subprocess.Popen(
                    [py, entry],
                    cwd=os.path.dirname(entry) or folder,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    preexec_fn=limiter,
                    # جلسة جديدة مستقلة: لا ترث إشارات الخادم، ويمكننا إيقاف
                    # مجموعة العمليات كاملة (البوت وأبناءه) عبر killpg
                    start_new_session=True,
                    **run_as,
                )
            except Exception as e:
                db.set_last_error(bot_id, str(e))
                return False, f"فشل تشغيل البوت: {e}"

        with self.lock:
            self.processes[bot_id] = proc
        self._kill_reasons.pop(bot_id, None)

        db.update_status(bot_id, "running")
        db.update_pid(bot_id, proc.pid)
        db.set_last_error(bot_id, None)
        db.update_last_started(bot_id)
        return True, "تم تشغيل البوت بنجاح ✅"

    def _terminate(self, proc, grace=None):
        """إيقاف لطيف (SIGTERM) لمجموعة العملية، ثم قسري (SIGKILL) بعد المهلة."""
        grace = config.SHUTDOWN_GRACE_SECONDS if grace is None else grace
        if proc.poll() is not None:
            _signal_group(proc.pid, signal.SIGKILL)  # تنظيف أي أبناء متبقين
            return
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        else:
            _signal_group(proc.pid, signal.SIGKILL)  # أبناء قد يكونون نجوا

    def stop_bot(self, bot_id, mark_stopped=True):
        with self.lock:
            proc = self.processes.get(bot_id)

        if proc:
            try:
                self._terminate(proc)
            except Exception:
                logger.exception("Failed to terminate bot %s", bot_id)
        else:
            # لا عملية متتبَّعة (مثلًا بعد إعادة تشغيل المنصة): قد يكون هناك PID يتيم
            row = db.get_bot(bot_id)
            if row and row["pid"]:
                self._kill_stale_pid(row)

        with self.lock:
            self.processes.pop(bot_id, None)

        if mark_stopped:
            db.update_status(bot_id, "stopped")
            db.update_pid(bot_id, None)
        return True, "تم إيقاف البوت ⏹"

    def restart_bot(self, bot_row):
        self.stop_bot(bot_row["bot_id"], mark_stopped=False)
        time.sleep(1)
        return self.start_bot(bot_row)

    def is_running(self, bot_id) -> bool:
        with self.lock:
            proc = self.processes.get(bot_id)
        return bool(proc and proc.poll() is None)

    # ---------------- استخدام الموارد ----------------

    def _ps(self, pid):
        ps = self._ps_cache.get(pid)
        if ps is None or not ps.is_running():
            ps = psutil.Process(pid)
            self._ps_cache[pid] = ps
        return ps

    def _tree_rss(self, pid):
        """مجموع الذاكرة الفعلية (RSS) للبوت وكل العمليات الفرعية التابعة له."""
        ps = self._ps(pid)
        total = ps.memory_info().rss
        for child in ps.children(recursive=True):
            try:
                total += child.memory_info().rss
            except psutil.Error:
                pass
        return total

    def get_usage(self, bot_id, fast=False):
        """fast=True لا يحجب (للمقاييس واللقطات الدورية)؛ الافتراضي يقيس CPU لمدة 0.3 ثانية."""
        with self.lock:
            proc = self.processes.get(bot_id)
        if not proc or proc.poll() is not None:
            return None
        try:
            p = self._ps(proc.pid)
            cpu = p.cpu_percent(interval=None if fast else 0.3)
            mem = self._tree_rss(proc.pid)
            uptime = time.time() - p.create_time()
            return {
                "cpu": f"{cpu:.1f}%",
                "mem": human_size(mem),
                "uptime": human_uptime(uptime),
                "cpu_value": cpu,
                "mem_bytes": mem,
            }
        except Exception:
            return None

    def check_health(self, bot_id):
        """يستدعي getMe بتوكن البوت الفرعي (إن وُجد كمتغير بيئة BOT_TOKEN_ENV_KEY)
        للتأكد أنه متصل فعليًا بتيليجرام، وليس فقط أن عمليته حيّة على مستوى نظام
        التشغيل. يرجع dict مع تفاصيل الفحص: {status: bool, checks: {getMe: bool, ping: bool}, details: str}."""
        if not self.is_running(bot_id):
            return {"status": False, "checks": {}, "details": "Bot process not running"}

        env = db.get_env_vars(bot_id)
        token = env.get(config.BOT_TOKEN_ENV_KEY)
        if not token:
            return {"status": None, "checks": {}, "details": "No BOT_TOKEN configured"}

        checks = {}
        details = []
        getme_ok = False
        try:
            url = f"https://api.telegram.org/bot{token}/getMe"
            with urllib.request.urlopen(url, timeout=config.HEALTH_CHECK_TIMEOUT_SECONDS) as resp:
                getme_ok = resp.status == 200
                if getme_ok:
                    data = json.loads(resp.read().decode())
                    bot_name = data.get("result", {}).get("username", "unknown")
                    details.append(f"getMe OK: @{bot_name}")
                else:
                    details.append("getMe failed: HTTP error")
        except Exception as exc:
            # لا نكتب الاستثناء كما هو: قد يحتوي الرابط (وفيه التوكن)
            details.append(f"getMe error: {type(exc).__name__}")
        checks["getMe"] = getme_ok
        checks["ping"] = None  # غير مفعّل افتراضيًا لتجنب إزعاج المستخدم برسائل اختبار
        return {"status": getme_ok, "checks": checks, "details": "; ".join(details) if details else "No checks performed"}

    # ---------------- الإقلاع والاسترداد ----------------

    def _kill_stale_pid(self, bot_row):
        """بعد انهيار المنصة فجأة قد تبقى عملية البوت حيّة (جلسة مستقلة). نقتلها قبل
        إعادة التشغيل حتى لا يعمل نسختان بنفس التوكن (خطأ 409 Conflict في تيليجرام).
        نتحقق أن العملية فعلًا بوتنا (مسار ملف التشغيل داخل سطر الأوامر) قبل قتلها
        لأن PID قد يكون أُعيد استخدامه لعملية أخرى."""
        pid = bot_row["pid"]
        if not pid:
            return False
        try:
            ps = psutil.Process(pid)
            cmdline = " ".join(ps.cmdline())
            if bot_row["entry_file"] and bot_row["entry_file"] in cmdline:
                logger.info("Killing stale process %s of bot %s", pid, bot_row["bot_id"])
                _signal_group(pid, signal.SIGTERM)
                try:
                    ps.wait(timeout=config.SHUTDOWN_GRACE_SECONDS)
                except psutil.TimeoutExpired:
                    _signal_group(pid, signal.SIGKILL)
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            pass
        return False

    def recover_on_boot(self):
        """يعيد تشغيل كل البوتات التي كانت تعمل قبل إيقاف/إعادة نشر المنصة.
        إعادة نشر المنصة ليست تعطّل بوت: لا نحسبها ضمن عدّاد التعطلات ولا نزعج
        أصحاب البوتات. التشغيل متدرّج لتفادي ذروة CPU/RAM لحظة الإقلاع."""
        try:
            rows = db.get_bots_by_status("running")
        except Exception:
            logger.exception("Boot recovery: cannot read bots")
            self.boot_recovery_done = True
            return
        logger.info("Boot recovery: %d bot(s) to restore", len(rows))
        for row in rows:
            bot_id = row["bot_id"]
            try:
                self._kill_stale_pid(row)
                ok, msg = self.start_bot(row)
                if ok:
                    logger.info("Boot recovery: bot %s restored", bot_id)
                else:
                    logger.warning("Boot recovery: bot %s failed: %s", bot_id, msg)
                    db.update_status(bot_id, "crashed")
                    db.set_last_error(bot_id, f"فشلت الاستعادة بعد إعادة تشغيل المنصة: {msg}")
                    self._notify(row["owner_id"], f"❌ تعذّر تشغيل بوتك «{row['name']}» بعد إعادة تشغيل المنصة.\n{msg}")
            except Exception:
                logger.exception("Boot recovery: unexpected error for bot %s", bot_id)
            if config.BOOT_RECOVERY_STAGGER_SECONDS:
                time.sleep(config.BOOT_RECOVERY_STAGGER_SECONDS)
        self.boot_recovery_done = True

    def run_background(self, interval):
        """نقطة دخول الخيط الخلفي: استرداد الإقلاع أولًا ثم الحارس، لتفادي سباق
        بين الحارس (الذي يرى البوتات المسجلة running بلا عملية) والاسترداد."""
        self.recover_on_boot()
        self.watchdog_loop(interval)

    # ---------------- الحارس التلقائي (Watchdog) ----------------

    def watchdog_loop(self, interval):
        while True:
            time.sleep(interval)
            self.last_watchdog_beat = time.time()
            for step in (self._check_all, self._enforce_memory_limits, self._enforce_disk_quota, self._periodic_tasks, self._apply_scheduled_restarts):
                try:
                    step()
                except Exception:
                    logger.exception("Watchdog step %s failed", step.__name__)

    def _check_disk(self):
        """ينبّه الأدمن (مرة كل DISK_ALERT_COOLDOWN_SECONDS) عند انخفاض المساحة الحرة."""
        if not config.DISK_ALERT_FREE_PERCENT or not config.ADMIN_IDS:
            return
        usage = shutil.disk_usage(config.DB_PATH.parent)
        free_percent = usage.free / usage.total * 100
        if free_percent >= config.DISK_ALERT_FREE_PERCENT:
            return
        now = time.monotonic()
        if self._last_disk_alert and now - self._last_disk_alert < config.DISK_ALERT_COOLDOWN_SECONDS:
            return
        self._last_disk_alert = now
        text = (f"💽 تنبيه: المساحة الحرة في التخزين {free_percent:.1f}% فقط "
                f"({human_size(usage.free)} من {human_size(usage.total)}).\n"
                "احذف بوتات غير مستخدمة أو وسّع الـVolume قبل أن يتوقف النظام.")
        for admin_id in config.ADMIN_IDS:
            self._notify(admin_id, text)

    def _periodic_tasks(self):
        self._check_disk()
        now = time.monotonic()
        if now - self._last_usage_snapshot >= config.USAGE_HISTORY_INTERVAL_SECONDS:
            self._record_usage_snapshots()
            self._last_usage_snapshot = now
        if now - self._last_audit_prune >= 86400:
            removed = db.prune_audit_log(config.AUDIT_RETENTION_DAYS)
            self._last_audit_prune = now
            if removed:
                logger.info("Pruned %d old audit log rows", removed)

    def rebuild_env(self, bot_row):
        """يحذف البيئة الافتراضية للبوت ويعيد بناءها (حل لمشاكل المكتبات التالفة أو
        تعارض الإصدارات). إن كان البوت يعمل يُعاد تشغيله بعد البناء."""
        bot_id = bot_row["bot_id"]
        was_running = self.is_running(bot_id)
        self.stop_bot(bot_id, mark_stopped=not was_running)
        import shutil as _sh
        _sh.rmtree(venv_path(bot_row["folder"]), ignore_errors=True)
        if was_running:
            return self.start_bot(db.get_bot(bot_id))
        return True, "تم حذف البيئة الافتراضية؛ ستُعاد عند التشغيل القادم 🔧"

    def _enforce_disk_quota(self):
        """يوقف البوت الذي تجاوز حصة القرص (ملفاته + بيئته) حتى لا يملأ الـVolume
        على بقية المستخدمين."""
        limit_mb = config.MAX_BOT_DISK_MB
        now = time.monotonic()
        if not limit_mb or now - getattr(self, "_last_quota_check", 0.0) < config.DISK_QUOTA_CHECK_SECONDS:
            return
        self._last_quota_check = now
        for row in db.get_bots_by_status("running"):
            if not row["folder"]:
                continue
            size_mb = dir_size_bytes(row["folder"]) / (1024 * 1024)
            if size_mb <= limit_mb:
                continue
            bot_id = row["bot_id"]
            reason = f"تجاوز حصة القرص ({size_mb:.0f}MB من {limit_mb}MB)"
            logger.warning("Bot %s stopped: %s", bot_id, reason)
            self.stop_bot(bot_id, mark_stopped=False)
            db.update_status(bot_id, "crashed")
            db.update_pid(bot_id, None)
            db.set_last_error(bot_id, reason)
            self._notify(row["owner_id"], f"💽 تم إيقاف بوتك «{row['name']}»: {reason}.\nاحذف ملفات غير ضرورية ثم شغّله يدويًا.")

    def _enforce_memory_limits(self):
        """يفرض حد الذاكرة الفعلي على RSS (البوت + أبناؤه). عند التجاوز نوقف مجموعة
        العمليات، ويتولى الحارس معالجتها كتعطل عادي (إعادة تشغيل بتأخير تصاعدي)."""
        for row in db.get_bots_by_status("running"):
            bot_id = row["bot_id"]
            with self.lock:
                proc = self.processes.get(bot_id)
            if not proc or proc.poll() is not None:
                continue
            try:
                limit_mb = row["max_memory_mb"] or config.MAX_BOT_MEMORY_MB
            except (KeyError, IndexError):
                limit_mb = config.MAX_BOT_MEMORY_MB
            try:
                rss_mb = self._tree_rss(proc.pid) / (1024 * 1024)
            except Exception:
                continue
            if rss_mb > limit_mb:
                reason = f"تجاوز حد الذاكرة ({rss_mb:.0f}MB من {limit_mb}MB)"
                logger.warning("Bot %s killed: %s", bot_id, reason)
                self._kill_reasons[bot_id] = reason
                try:
                    with open(self._log_path(row["folder"]), "a", encoding="utf-8") as f:
                        f.write(f"\n[⚠️ المنصة] تم إيقاف البوت: {reason}\n")
                except OSError:
                    pass
                _signal_group(proc.pid, signal.SIGKILL)

    def _record_usage_snapshots(self):
        """لقطة من استهلاك CPU/RAM لكل بوت شغّال في سجل تاريخي بسيط (آخر
        USAGE_HISTORY_MAX_POINTS نقطة فقط لكل بوت)."""
        with self.lock:
            bot_ids = list(self.processes.keys())
        for bot_id in bot_ids:
            usage = self.get_usage(bot_id, fast=True)
            if not usage:
                continue
            db.add_usage_point(
                bot_id, usage["cpu_value"], usage["mem_bytes"] / (1024 * 1024), config.USAGE_HISTORY_MAX_POINTS
            )

    def _apply_scheduled_restarts(self):
        """يعيد تشغيل أي بوت مفعّل له فاصل إعادة تشغيل دوري (restart_interval_hours)
        متى ما تجاوز وقت تشغيله الحالي هذا الفاصل."""
        now = int(time.time())
        for bot_row in db.get_bots_with_scheduled_restart():
            bot_id = bot_row["bot_id"]
            if not self.is_running(bot_id):
                continue
            last_started = bot_row["last_started_at"] or now
            interval_seconds = bot_row["restart_interval_hours"] * 3600
            if now - last_started < interval_seconds:
                continue
            ok, _ = self.restart_bot(bot_row)
            if ok:
                self._notify(
                    bot_row["owner_id"],
                    f"⏰ تمت إعادة تشغيل بوتك «{bot_row['name']}» تلقائيًا حسب الجدولة "
                    f"({bot_row['restart_interval_hours']} ساعة).",
                )

    def _describe_exit(self, bot_id, folder):
        """وصف مختصر لسبب توقف العملية: سبب المنصة، أو رمز الخروج + آخر سطور السجل."""
        reason = self._kill_reasons.pop(bot_id, None)
        if reason:
            return reason
        with self.lock:
            proc = self.processes.get(bot_id)
        code = proc.returncode if proc else None
        if code is None:
            parts = ["العملية غير موجودة"]
        elif code < 0:
            try:
                parts = [f"أُنهيت بالإشارة {signal.Signals(-code).name}"]
            except ValueError:
                parts = [f"أُنهيت بالإشارة {-code}"]
            if -code == signal.SIGXCPU:
                parts.append("(تجاوز حد وقت المعالج)")
        else:
            parts = [f"خرجت برمز {code}"]
        tail = tail_file(self._log_path(folder), 5).strip() if folder else ""
        if tail:
            parts.append("— آخر السجل: " + tail[-300:])
        return " ".join(parts)

    def _check_all(self):
        running_in_db = db.get_bots_by_status("running")
        for bot_row in running_in_db:
            bot_id = bot_row["bot_id"]
            if self.is_running(bot_id):
                continue
            # البوت متوقف فعليًا رغم أنه مسجَّل "running" => توقف غير متوقع
            with self._recovering_lock:
                if bot_id in self._recovering:
                    continue  # محاولة إعادة تشغيل لهذا البوت جارية بالفعل
                self._recovering.add(bot_id)

            detail = self._describe_exit(bot_id, bot_row["folder"])
            db.set_last_error(bot_id, detail)

            if not bot_row["auto_restart"]:
                db.update_status(bot_id, "crashed")
                db.update_pid(bot_id, None)
                self._notify(
                    bot_row["owner_id"],
                    f"❌ توقف بوتك «{bot_row['name']}» بشكل غير متوقع، "
                    f"وخاصية إعادة التشغيل التلقائي معطّلة له.\n{detail}",
                )
                with self._recovering_lock:
                    self._recovering.discard(bot_id)
                continue

            threading.Thread(target=self._handle_crash, args=(bot_row, detail), daemon=True).start()

    def _handle_crash(self, bot_row, detail=""):
        bot_id = bot_row["bot_id"]
        try:
            crash_count = db.register_crash(bot_id, config.CRASH_LOOP_RESET_SECONDS)

            if crash_count > config.MAX_AUTO_RESTART_ATTEMPTS:
                # حلقة تعطل متكررة: نوقف إعادة التشغيل التلقائي لحماية السيرفر
                db.update_status(bot_id, "crashed")
                db.update_pid(bot_id, None)
                db.set_auto_restart(bot_id, False)
                db.set_last_error(
                    bot_id,
                    f"تم تعطيل إعادة التشغيل التلقائي بعد {crash_count} تعطلات متتالية "
                    f"خلال {config.CRASH_LOOP_RESET_SECONDS} ثانية (حلقة تعطل). آخر سبب: {detail}",
                )
                self._notify(
                    bot_row["owner_id"],
                    f"🚫 بوتك «{bot_row['name']}» يدخل في حلقة تعطل متكررة "
                    f"({crash_count} مرات متتالية). تم إيقاف إعادة التشغيل التلقائي "
                    f"لحماية السيرفر. راجع السجلات وأعد التشغيل يدويًا بعد الإصلاح.",
                )
                return

            # تأخير تصاعدي قبل إعادة المحاولة، بحد أقصى 120 ثانية
            backoff = min(config.RESTART_BACKOFF_BASE_SECONDS * (2 ** (crash_count - 1)), 120)
            time.sleep(backoff)

            # إعادة قراءة الصف: قد يكون المستخدم تدخّل يدويًا أثناء الانتظار (أوقف/شغّل/حذف)
            fresh_row = db.get_bot(bot_id)
            if not fresh_row or self.is_running(bot_id) or not fresh_row["auto_restart"] or fresh_row["status"] != "running":
                return

            ok, msg = self.start_bot(fresh_row)
            db.increment_restart_count(bot_id)
            if not ok:
                db.update_status(bot_id, "crashed")
            status = "تمت إعادة تشغيله تلقائيًا 🔁" if ok else f"فشلت إعادة التشغيل: {msg}"
            self._notify(
                fresh_row["owner_id"],
                f"⚠️ توقف بوتك «{fresh_row['name']}» بشكل غير متوقع "
                f"(المحاولة {crash_count}/{config.MAX_AUTO_RESTART_ATTEMPTS}).\n{detail}\n{status}",
            )
        except Exception:
            logger.exception("Crash handler failed for bot %s", bot_id)
        finally:
            with self._recovering_lock:
                self._recovering.discard(bot_id)

    def shutdown_all(self):
        """إيقاف كل البوتات بالتوازي ضمن مهلة واحدة (SHUTDOWN_GRACE_SECONDS) بدل
        الانتظار لكل بوت على حدة، لأن المنصات (Railway/Docker) تمنح مهلة قصيرة قبل
        SIGKILL. حالة البوتات في القاعدة تبقى running ليُعاد تشغيلها عند الإقلاع."""
        with self.lock:
            procs = list(self.processes.values())
        for proc in procs:
            if proc.poll() is None:
                _signal_group(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + config.SHUTDOWN_GRACE_SECONDS
        for proc in procs:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                pass
        for proc in procs:
            _signal_group(proc.pid, signal.SIGKILL)
        with self.lock:
            self.processes.clear()
