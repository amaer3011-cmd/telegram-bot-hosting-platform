import hashlib
import os
import py_compile
import re
import shutil
import subprocess
import sys
import zipfile

from config import (
    BOT_UID_BASE,
    BOTS_DIR,
    ISOLATION_ACTIVE,
    MAX_BOT_PROCESSES,
    MAX_BOT_FILE_SIZE_MB,
    MAX_EXTRACTED_SIZE_MB,
    MAX_ZIP_FILE_COUNT,
    PIP_CACHE_DIR,
    VENV_SETUP_TIMEOUT_SECONDS,
)


def make_bot_folder(owner_id: int, bot_id: int) -> str:
    folder = os.path.join(BOTS_DIR, str(owner_id), f"bot_{bot_id}")
    os.makedirs(folder, exist_ok=True)
    return folder


def make_temp_upload_folder(owner_id: int, temp_token: str) -> str:
    """مجلد مؤقت لاستقبال ملف/أرشيف البوت قبل إنشاء صف البوت في قاعدة البيانات
    (نؤجل الإدراج في القاعدة حتى نتأكد من نجاح الاستخراج لتفادي "بوتات شبح" فارغة)."""
    folder = os.path.join(BOTS_DIR, str(owner_id), f"tmp_{temp_token}")
    os.makedirs(folder, exist_ok=True)
    return folder


def finalize_bot_folder(temp_folder: str, owner_id: int, bot_id: int) -> str:
    """ينقل مجلد الرفع المؤقت إلى مساره النهائي (bot_<bot_id>) بعد نجاح إنشاء
    صف البوت في قاعدة البيانات."""
    final_folder = os.path.join(BOTS_DIR, str(owner_id), f"bot_{bot_id}")
    if os.path.exists(final_folder):
        shutil.rmtree(final_folder, ignore_errors=True)
    shutil.move(temp_folder, final_folder)
    return final_folder


def safe_filename(fname: str, default: str = "bot_file.py") -> str:
    """يعقّم اسم ملف قادم مباشرة من المستخدم عبر تيليجرام: يأخذ اسم الملف فقط
    (بدون أي جزء من المسار) عبر os.path.basename، ويرفض أي محاولة اجتياز مسار
    (Path Traversal) أو اسمًا فارغًا بعد التعقيم، لأن اسم الملف قد يحتوي أجزاء
    مسار مثل ../../../etc/x تكتب خارج مجلد البوت المخصَّص لو استُخدمت كما هي."""
    name = os.path.basename((fname or "").strip())
    if not name or name in (".", "..") or ".." in name:
        return default
    return name


# نمط اسم ملف آمن للاستخدام في مسارات (مثل ملفات السجل المؤقتة المُرسَلة عبر
# تيليجرام) عندما يكون مصدر الاسم نصًا حرًا أدخله المستخدم (مثل اسم البوت)
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9\u0600-\u06FF _.-]+")


def safe_display_filename(raw_name: str, suffix: str = "", default: str = "file") -> str:
    """يحوّل نصًا حرًا (مثل اسم بوت أدخله المستخدم) إلى اسم ملف آمن للاستخدام
    داخل مسار على القرص: يزيل فواصل المسار وأي محارف قد تُستغل لاجتياز مسار
    (Path Traversal) أو للتضارب مع مسارات أخرى، ويحتفظ بالحروف العربية/اللاتينية
    والأرقام والمسافات والشرطات فقط."""
    name = os.path.basename((raw_name or "").strip())
    name = name.replace("..", "")
    name = _UNSAFE_FILENAME_CHARS.sub("", name).strip()
    if not name:
        name = default
    return f"{name}{suffix}"[:120]


class UnsafeZipError(Exception):
    """يُرفع عند اكتشاف أرشيف يحاول الخروج من مجلد الوجهة أو يتجاوز الحجم المسموح."""


def _safe_member_path(dest_folder: str, member_name: str) -> str:
    """يتحقق أن مسار العضو داخل الأرشيف لا يخرج عن dest_folder (حماية من Zip Slip)."""
    dest_root = os.path.realpath(dest_folder)
    target = os.path.realpath(os.path.join(dest_root, member_name))
    if target != dest_root and not target.startswith(dest_root + os.sep):
        raise UnsafeZipError(f"مسار غير آمن داخل الأرشيف: {member_name}")
    return target


def extract_zip(zip_path: str, dest_folder: str):
    """يستخرج أرشيف zip بأمان: يمنع Zip Slip (الخروج عن المجلد) و Zip Bomb (تضخم الحجم)."""
    max_bytes = MAX_EXTRACTED_SIZE_MB * 1024 * 1024
    with zipfile.ZipFile(zip_path, "r") as z:
        infos = z.infolist()

        # حماية إضافية من Zip Bomb عبر عدد هائل من الملفات الصغيرة/الفارغة —
        # الحجم الكلي وحده لا يكفي كمقياس لأن ملايين الملفات الفارغة تستنزف
        # الـ inodes وتُبطئ الاستخراج حتى لو كان الحجم الإجمالي صغيرًا
        if len(infos) > MAX_ZIP_FILE_COUNT:
            raise UnsafeZipError(
                f"عدد الملفات داخل الأرشيف ({len(infos)}) يتجاوز الحد المسموح ({MAX_ZIP_FILE_COUNT})."
            )

        # تحقق مسبق من كل المسارات + الحجم الكلي قبل استخراج أي شيء
        total_size = 0
        for info in infos:
            _safe_member_path(dest_folder, info.filename)
            total_size += info.file_size
            if total_size > max_bytes:
                raise UnsafeZipError(
                    f"حجم الأرشيف بعد فك الضغط يتجاوز الحد المسموح ({MAX_EXTRACTED_SIZE_MB}MB)."
                )

        written = 0
        for info in infos:
            target = _safe_member_path(dest_folder, info.filename)
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # الحجم المعلن داخل الأرشيف قد يكذب: نعدّ البايتات الفعلية أثناء النسخ
            with z.open(info, "r") as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 256)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise UnsafeZipError(
                            f"حجم الأرشيف بعد فك الضغط يتجاوز الحد المسموح ({MAX_EXTRACTED_SIZE_MB}MB)."
                        )
                    dst.write(chunk)

    os.remove(zip_path)


_IGNORED_DIRS = {".venv", "__pycache__", ".git", "node_modules", "__MACOSX"}


def flatten_single_root(folder: str) -> str:
    """أرشيفات GitHub وغيرها غالبًا تحتوي مجلدًا واحدًا في الجذر (repo-main/).
    نرفع محتواه إلى الجذر حتى يجد ensure_venv ملف requirements.txt ويكون
    مجلد العمل (cwd) صحيحًا للبوت. يتجاهل __MACOSX والملفات المخفية."""
    entries = [e for e in os.listdir(folder) if e not in ("__MACOSX", "upload.zip")]
    if len(entries) != 1:
        return folder
    inner = os.path.join(folder, entries[0])
    if not os.path.isdir(inner) or os.path.islink(inner):
        return folder
    for name in os.listdir(inner):
        shutil.move(os.path.join(inner, name), os.path.join(folder, name))
    os.rmdir(inner)
    return folder


def find_entry_file(folder: str):
    """يحاول إيجاد ملف التشغيل الرئيسي للبوت داخل المجلد.
    الأولوية للملفات المفضّلة بترتيبها (main قبل bot ثم app...) وللأقرب للجذر."""
    preferred = ["main.py", "bot.py", "app.py", "run.py", "start.py"]
    all_py = []
    best = None  # (depth, preferred_index, path)
    for root, dirs, files in os.walk(folder):
        dirs[:] = [d for d in dirs if d not in _IGNORED_DIRS]
        depth = os.path.relpath(root, folder).count(os.sep) if root != folder else 0
        for f in files:
            if not f.endswith(".py"):
                continue
            full = os.path.join(root, f)
            all_py.append(full)
            if f in preferred:
                key = (depth, preferred.index(f), full)
                if best is None or key < best:
                    best = key
    if best:
        return best[2]
    # لا يوجد اسم مفضّل: نفرز أبجديًا لضمان اختيار حتمي وقابل لإعادة الإنتاج
    all_py.sort()
    return all_py[0] if all_py else None


def check_syntax(entry_path: str):
    """يتحقق من صحة صياغة ملف بايثون قبل أول تشغيل، عبر py_compile (بدون
    تنفيذ الكود فعليًا). يرجع (True, None) إن كانت الصياغة سليمة، أو
    (False, رسالة الخطأ) إن كان هناك خطأ صياغي — لإظهاره للمستخدم فورًا بدل
    اكتشافه بعد محاولة تشغيل فاشلة."""
    try:
        # ⚠️ ملاحظة: quiet=2 في py_compile.compile له سلوك غير بديهي — يمنع رفع
        # الاستثناء تمامًا حتى مع doraise=True (وليس مجرد كتم رسالة الطباعة كما
        # قد يُتوقَّع)، لذلك نستخدم quiet=1 (يكتم الطباعة على stderr لكن يحافظ
        # على رفع PyCompileError بشكل طبيعي عبر doraise)
        py_compile.compile(entry_path, doraise=True, quiet=1)
        return True, None
    except py_compile.PyCompileError as e:
        return False, str(e.exc_value)
    except (SyntaxError, ValueError) as e:
        return False, str(e)
    except Exception:
        # أي خطأ غير متوقع في الفحص نفسه لا يجب أن يمنع محاولة التشغيل العادية
        return True, None


def venv_path(folder: str) -> str:
    return os.path.join(folder, ".venv")


def venv_python(folder: str) -> str:
    v = venv_path(folder)
    if os.name == "nt":
        return os.path.join(v, "Scripts", "python.exe")
    return os.path.join(v, "bin", "python")


def _file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def ensure_venv(folder: str, log_path: str, run_as: dict | None = None):
    """ينشئ بيئة افتراضية خاصة بالبوت ويثبّت المتطلبات إن وُجد requirements.txt.
    يتخطى إعادة التثبيت إذا لم يتغيّر محتوى requirements.txt منذ آخر تشغيل ناجح.
    يستخدم كاش pip مشتركًا بين كل البوتات، ويعيد بناء البيئة إذا كانت تالفة."""
    v = venv_path(folder)
    timeout = VENV_SETUP_TIMEOUT_SECONDS
    run_as = run_as or {}
    # أسرار المنصة لا تصل إلى pip (قد ينفّذ setup.py لحزم المستخدم). ومع العزل
    # يعمل pip بمستخدم البوت نفسه ولا نشارك الكاش بين المستخدمين.
    pip_env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                                          "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY") if k in os.environ}
    pip_env.update(PIP_DISABLE_PIP_VERSION_CHECK="1", HOME=folder)
    pip_args = ["--disable-pip-version-check"]
    if ISOLATION_ACTIVE:
        pip_args.append("--no-cache-dir")
    else:
        pip_env["PIP_CACHE_DIR"] = str(PIP_CACHE_DIR)
    with open(log_path, "a", encoding="utf-8") as log:
        try:
            # بيئة نصف مُنشأة (فشل سابق/إيقاف مفاجئ): نحذفها ونبدأ من جديد
            if os.path.exists(v) and not os.path.exists(venv_python(folder)):
                log.write(">>> البيئة الافتراضية تالفة — إعادة إنشائها...\n")
                shutil.rmtree(v, ignore_errors=True)

            if not os.path.exists(v):
                log.write(">>> إنشاء بيئة افتراضية جديدة...\n")
                log.flush()
                try:
                    subprocess.run(
                        [sys.executable, "-m", "venv", v],
                        check=True, stdout=log, stderr=log, timeout=timeout, env=pip_env, **run_as,
                    )
                except Exception:
                    shutil.rmtree(v, ignore_errors=True)  # لا نترك بيئة نصف مكتملة
                    raise

            py = venv_python(folder)
            req = os.path.join(folder, "requirements.txt")
            hash_marker = os.path.join(v, ".requirements.sha256")

            if not os.path.exists(req):
                log.write(">>> لا يوجد requirements.txt — سيتم التشغيل بدون تثبيت مكتبات إضافية.\n")
                return

            current_hash = _file_hash(req)
            previous_hash = None
            if os.path.exists(hash_marker):
                with open(hash_marker, encoding="utf-8") as f:
                    previous_hash = f.read().strip()

            if current_hash == previous_hash:
                log.write(">>> requirements.txt لم يتغيّر — تخطي إعادة التثبيت.\n")
                return

            # لا نحدّث pip في كل مرة (يستهلك شبكة ووقتًا بلا فائدة)؛ pip الذي يأتي
            # مع بايثون كافٍ، وكاش pip المشترك يجعل التثبيت المتكرر شبه فوري.
            log.write(">>> تثبيت المتطلبات من requirements.txt ...\n")
            log.flush()
            result = subprocess.run(
                [py, "-m", "pip", "install", *pip_args, "-r", req],
                stdout=log, stderr=log, timeout=timeout, env=pip_env, cwd=folder, **run_as,
            )
            if result.returncode != 0:
                raise RuntimeError("فشل تثبيت requirements.txt؛ راجع سجل البوت.")
            with open(hash_marker, "w", encoding="utf-8") as f:
                f.write(current_hash)
        except subprocess.TimeoutExpired as e:
            log.write(f">>> ⏱ انتهت المهلة أثناء تجهيز البيئة: {e}\n")
            raise


def tail_file(path: str, n: int = 60) -> str:
    if not os.path.exists(path):
        return "لا يوجد سجل بعد."
    try:
        # نقرأ آخر 256KB فقط بدل تحميل سجل بحجم عشرات الميغابايت في الذاكرة
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read().decode("utf-8", errors="ignore")
        content = "".join(data.splitlines(keepends=True)[-n:])
        return content or "السجل فارغ."
    except Exception as e:
        return f"تعذر قراءة السجل: {e}"


def human_size(num_bytes: float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def human_uptime(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} س {m} د"
    if m:
        return f"{m} د {s} ث"
    return f"{s} ث"


def rotate_log_if_needed(log_path: str, max_size_mb: int, archive: bool = True):
    """إن تجاوز حجم ملف السجل الحد الأقصى، يقتطعه ويحتفظ بآخر نصف الحجم المسموح
    فقط (بدل حذفه بالكامل أو تركه ينمو بلا حدود)، حتى لا يفقد المستخدم كل
    تاريخ سجله دفعة واحدة عند كل تجاوز.

    إذا كان archive=True، يتم حفظ الجزء المقطوع في ملف أرشيف منفصل
    (run.log.1, run.log.2, إلخ) قبل القطع.
    """
    try:
        if not os.path.exists(log_path):
            return
        max_bytes = max_size_mb * 1024 * 1024
        size = os.path.getsize(log_path)
        if size <= max_bytes:
            return

        # Archive the current log before truncating
        if archive:
            _archive_log_part(log_path, size, max_bytes)

        keep_bytes = max(max_bytes // 2, 1024)
        with open(log_path, "rb") as f:
            if size > keep_bytes:
                f.seek(-keep_bytes, os.SEEK_END)
            tail_data = f.read()
        with open(log_path, "wb") as f:
            f.write(
                "…\n[⚠️ تم اقتطاع بداية السجل تلقائيًا لتجاوزه الحد الأقصى للحجم]\n…\n\n"
                .encode()
            )
            f.write(tail_data)
    except Exception:
        # لا نُفشل تشغيل البوت بسبب مشكلة في تدوير السجل
        pass


def _archive_log_part(log_path: str, current_size: int, max_bytes: int) -> None:
    """Archive the part of the log that will be truncated.

    Creates numbered archive files: run.log.1, run.log.2, etc.
    Keeps up to MAX_LOG_ARCHIVES archives.
    """
    try:
        log_dir = os.path.dirname(log_path)
        base_name = os.path.basename(log_path)

        # Find existing archives and rotate them
        MAX_LOG_ARCHIVES = int(os.getenv("MAX_LOG_ARCHIVES", "5"))

        # Shift existing archives: .5 -> .6, .4 -> .5, etc.
        for i in range(MAX_LOG_ARCHIVES, 0, -1):
            src = os.path.join(log_dir, f"{base_name}.{i}")
            if os.path.exists(src):
                if i == MAX_LOG_ARCHIVES:
                    # Delete the oldest
                    os.remove(src)
                else:
                    dst = os.path.join(log_dir, f"{base_name}.{i+1}")
                    os.rename(src, dst)

        # Save the truncated part as .1
        archive_path = os.path.join(log_dir, f"{base_name}.1")
        with open(log_path, "rb") as f:
            # Read the part that will be truncated (the older part)
            f.seek(0)
            truncated_part = f.read(current_size - max(max_bytes // 2, 1024))
        with open(archive_path, "wb") as f:
            f.write(truncated_part)
    except Exception:
        # Silent fail - don't interrupt bot operation
        pass


def delete_folder(folder: str):
    shutil.rmtree(folder, ignore_errors=True)


def export_bot_zip(folder: str, dest_zip_path_no_ext: str) -> str:
    """يضغط مجلد بوت (باستثناء .venv وملف السجل) إلى أرشيف zip للتنزيل.
    يرجع مسار الملف الناتج."""
    tmp_export_dir = dest_zip_path_no_ext + "_export_src"
    if os.path.exists(tmp_export_dir):
        shutil.rmtree(tmp_export_dir, ignore_errors=True)
    shutil.copytree(
        folder, tmp_export_dir,
        ignore=shutil.ignore_patterns(".venv", "run.log", "__pycache__", "*.pyc"),
    )
    archive_path = shutil.make_archive(dest_zip_path_no_ext, "zip", tmp_export_dir)
    shutil.rmtree(tmp_export_dir, ignore_errors=True)
    return archive_path


def make_resource_limiter(max_memory_mb: int, max_cpu_seconds: int):
    """يرجع دالة preexec_fn لتمريرها إلى subprocess.Popen لتقييد موارد العملية
    الفرعية (Linux فقط).

    - RLIMIT_AS سقف أمان واسع (3× حد الذاكرة): الحد الفعلي للذاكرة يُفرض على
      RSS من الحارس (watchdog)، لأن تقييد الذاكرة الافتراضية بالحد نفسه يكسر
      مكتبات كثيرة (numpy وaiohttp وخيوط متعددة) رغم أن استهلاكها الحقيقي صغير.
    - RLIMIT_FSIZE يمنع ملفًا واحدًا من ملء القرص.
    - RLIMIT_CPU إجمالي وقت المعالج."""
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None

    def _limit():
        def _set(which, value):
            try:
                resource.setrlimit(which, (value, value))
            except Exception:
                # إن فشل ضبط حد (صلاحيات مثلاً) نكمل بدل تعطيل التشغيل بالكامل
                pass

        _set(resource.RLIMIT_AS, max_memory_mb * 3 * 1024 * 1024)
        _set(resource.RLIMIT_CPU, max_cpu_seconds)
        _set(resource.RLIMIT_FSIZE, MAX_BOT_FILE_SIZE_MB * 1024 * 1024)
        if ISOLATION_ACTIVE:  # RLIMIT_NPROC لكل UID: آمن فقط عندما يكون لكل بوت UID خاص
            _set(resource.RLIMIT_NPROC, MAX_BOT_PROCESSES)

    return _limit


def sparkline(values, top=None) -> str:
    """رسم نصي مصغّر للاتجاه (▁▂▃▄▅▆▇█). top = القيمة التي تمثل الارتفاع الأقصى
    (مثل حد الذاكرة)، وإلا يُستخدم أكبر قيمة في السلسلة."""
    blocks = "▁▂▃▄▅▆▇█"
    vals = [float(v or 0) for v in values][-30:]
    if not vals:
        return ""
    peak = float(top) if top else max(vals)
    if peak <= 0:
        return blocks[0] * len(vals)
    return "".join(blocks[min(len(blocks) - 1, int(max(v, 0) / peak * (len(blocks) - 1) + 0.5))] for v in vals)


# ---------------- عزل البوتات (UID لكل بوت) ----------------

def bot_uid(bot_id) -> int:
    return BOT_UID_BASE + int(bot_id)


def run_as_kwargs(bot_id) -> dict:
    """وسائط subprocess لتشغيل أمر بمستخدم البوت (فارغة إن كان العزل غير مفعّل)."""
    if not ISOLATION_ACTIVE:
        return {}
    uid = bot_uid(bot_id)
    return {"user": uid, "group": uid, "extra_groups": []}


def _chown_tree(path: str, uid: int) -> None:
    os.chown(path, uid, uid, follow_symlinks=False)
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            try:
                os.chown(os.path.join(root, name), uid, uid, follow_symlinks=False)
            except FileNotFoundError:
                pass


def prepare_bot_dir(folder: str, bot_id) -> None:
    """يجهّز مجلد البوت للعزل: مالكه UID البوت وصلاحياته 700، والمجلدات الأب قابلة
    للعبور (711) دون سرد محتوياتها. يعيد chown -R فقط إذا تغيّر المالك (بعد رفع/تحديث)."""
    os.makedirs(os.path.join(folder, ".tmp"), exist_ok=True)
    if not ISOLATION_ACTIVE:
        return
    uid = bot_uid(bot_id)
    if os.stat(folder).st_uid != uid:
        _chown_tree(folder, uid)
    os.chmod(folder, 0o700)
    os.chown(os.path.join(folder, ".tmp"), uid, uid)
    parent = os.path.dirname(folder)
    for d in (parent, str(BOTS_DIR)):
        try:
            os.chmod(d, 0o711)
        except OSError:
            pass


def dir_size_bytes(path: str) -> int:
    """حجم المجلد الفعلي (بدون تتبّع الروابط الرمزية)."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


_ENV_LINE = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")


def parse_env_text(text: str):
    """يحلل نص .env متعدد الأسطر. يرجع (pairs, rejected_lines). يتجاهل التعليقات
    والأسطر الفارغة، ويزيل علامات الاقتباس المحيطة بالقيمة."""
    pairs, rejected = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _ENV_LINE.match(line)
        if not m:
            rejected.append(line[:40])
            continue
        key, value = m.group(1).upper(), m.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:  # تعليق في نهاية السطر لقيمة غير مقتبسة
            value = value.split(" #", 1)[0].rstrip()
        pairs.append((key, value))
    return pairs, rejected
