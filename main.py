from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

import config
import database as db
import shutil

import psutil

import backup
from ratelimit import RateLimiter
from env_crypto import encrypt_value, decrypt_value
from process_manager import ProcessManager
from ai_analyzer import analyze_bot, AnalysisResult
from utils import (
    check_syntax,
    delete_folder,
    dir_size_bytes,
    export_bot_zip,
    extract_zip,
    finalize_bot_folder,
    find_entry_file,
    flatten_single_root,
    human_size,
    make_temp_upload_folder,
    parse_env_text,
    safe_display_filename,
    sparkline,
    tail_file,
)

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("hosting-bot")
PENDING: dict[int, dict] = {}
MANAGER: ProcessManager | None = None
BROADCAST_TASK: asyncio.Task | None = None
BROADCAST_CANCEL: asyncio.Event | None = None
STARTED_AT = time.time()
LIMITER = RateLimiter(config.RATE_LIMIT_MSGS_PER_MINUTE)


async def rate_limit_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعمل قبل كل المعالجات: يحجب المستخدم العادي المفرط في الرسائل/الضغطات."""
    user = update.effective_user
    if not user or is_admin(user.id) or LIMITER.allow(user.id):
        return
    if LIMITER.should_warn(user.id):
        notice = "⏳ أنت ترسل طلبات كثيرة. انتظر دقيقة ثم حاول مجددًا."
        if update.callback_query:
            await update.callback_query.answer(notice, show_alert=True)
        elif update.message:
            await update.message.reply_text(notice)
    raise ApplicationHandlerStop


def maintenance_on() -> bool:
    return db.get_setting("maintenance", "0") == "1"


async def maintenance_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """في وضع الصيانة يُحجب المستخدمون العاديون عن بوت التحكم فقط؛ البوتات المستضافة
    تبقى تعمل، والأدمن يستمر بالتحكم."""
    user = update.effective_user
    if not user or is_admin(user.id) or not maintenance_on():
        return
    notice = "🛠 المنصة في وضع الصيانة حاليًا. بوتاتك تعمل بشكل طبيعي. حاول لاحقًا."
    if update.callback_query:
        await update.callback_query.answer(notice, show_alert=True)
    elif update.message:
        await update.message.reply_text(notice)
    raise ApplicationHandlerStop


def is_admin(user_id: int | None) -> bool:
    return user_id is not None and user_id in config.ADMIN_IDS


def user_allowed(user_id: int | None) -> bool:
    return user_id is not None and not db.is_banned(user_id)


def ensure_user(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    db.create_user_if_missing(user.id, user.username or "")
    return is_admin(user.id) or user_allowed(user.id)


def manager() -> ProcessManager:
    if MANAGER is None:
        raise RuntimeError("Process manager is not initialized")
    return MANAGER


def back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="menu")]])


def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ إضافة بوت", callback_data="new_bot"), InlineKeyboardButton("🤖 بوتاتي", callback_data="my_bots")],
        [InlineKeyboardButton("📖 المساعدة", callback_data="help"), InlineKeyboardButton("📊 الإحصائيات", callback_data="my_stats")],
    ])


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 إحصائيات المنصة", callback_data="admin:stats"), InlineKeyboardButton("👥 المستخدمون", callback_data="admin:users")],
        [InlineKeyboardButton("🤖 كل البوتات", callback_data="admin:bots"), InlineKeyboardButton("🧾 سجل التدقيق", callback_data="admin:audit")],
        [InlineKeyboardButton("🚫 حظر عضو", callback_data="admin:ban"), InlineKeyboardButton("✅ فك الحظر", callback_data="admin:unban")],
        [InlineKeyboardButton("🎛 حدود الأعضاء", callback_data="admin:limit"), InlineKeyboardButton("🛑 إيقاف كل البوتات", callback_data="admin:stop_all_confirm")],
        [InlineKeyboardButton("🏆 الأكثر استهلاكاً", callback_data="admin:top"), InlineKeyboardButton("🛠 وضع الصيانة", callback_data="admin:maintenance")],
        [InlineKeyboardButton("📢 إذاعة رسالة", callback_data="admin:broadcast"), InlineKeyboardButton("🛑 إيقاف الإذاعة", callback_data="admin:broadcast_cancel")],
        [InlineKeyboardButton("🔙 الرئيسية", callback_data="menu")],
    ])


def bot_keyboard(bot_id: int, admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("▶️ تشغيل", callback_data=f"bot:start:{bot_id}"), InlineKeyboardButton("⏹ إيقاف", callback_data=f"bot:stop:{bot_id}")],
        [InlineKeyboardButton("🔁 إعادة تشغيل", callback_data=f"bot:restart:{bot_id}"), InlineKeyboardButton("📜 السجل", callback_data=f"bot:logs:{bot_id}")],
        [InlineKeyboardButton("📈 الموارد", callback_data=f"bot:usage:{bot_id}"), InlineKeyboardButton("🔐 البيئة", callback_data=f"bot:env:{bot_id}")],
        [InlineKeyboardButton("🧠 الذاكرة", callback_data=f"bot:memory:{bot_id}"), InlineKeyboardButton("♻️ إعادة تلقائية", callback_data=f"bot:auto:{bot_id}")],
        [InlineKeyboardButton("⏰ جدولة", callback_data=f"bot:schedule:{bot_id}"), InlineKeyboardButton("🩺 فحص الصحة", callback_data=f"bot:health:{bot_id}")],
        [InlineKeyboardButton("📥 تحميل السجل كاملاً", callback_data=f"bot:logs_full:{bot_id}")],
        [InlineKeyboardButton("✏️ إعادة تسمية", callback_data=f"bot:rename:{bot_id}"), InlineKeyboardButton("🔧 إعادة بناء البيئة", callback_data=f"bot:rebuild:{bot_id}")],
        [InlineKeyboardButton("📦 تصدير", callback_data=f"bot:export:{bot_id}"), InlineKeyboardButton("📋 نسخ", callback_data=f"bot:clone:{bot_id}")],
        [InlineKeyboardButton("🔄 تحديث", callback_data=f"bot:update:{bot_id}"), InlineKeyboardButton("🗑 حذف", callback_data=f"bot:delete_confirm:{bot_id}")],
        [InlineKeyboardButton("🔙 بوتاتي" if not admin else "🔙 لوحة الإدارة", callback_data="my_bots" if not admin else "admin:back")],
    ]
    return InlineKeyboardMarkup(rows)


def status_label(status: str, running: bool) -> str:
    if running:
        return "🟢 يعمل"
    return {"stopped": "⚪ متوقف", "crashed": "🔴 تعطل", "error": "🔴 خطأ"}.get(status, f"🟡 {status}")


def progress_text(title: str, percent: int, detail: str) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"⏳ <b>{html.escape(title)}</b>\n\n[{bar}] <b>{percent}%</b>\n<i>{html.escape(detail)}</i>"


async def edit_progress(message, title: str, percent: int, detail: str, reply_markup=None) -> None:
    try:
        await message.edit_text(progress_text(title, percent, detail), parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:
        logger.debug("Could not update progress message", exc_info=True)


async def run_with_progress(message, title: str, operation, reply_markup=None):
    progress = await message.reply_text(progress_text(title, 10, "بدء العملية..."), parse_mode=ParseMode.HTML)
    task = asyncio.create_task(asyncio.to_thread(operation))
    percent = 15
    while not task.done():
        await asyncio.sleep(0.8)
        if task.done():
            break
        percent = min(90, percent + 10)
        await edit_progress(progress, title, percent, "ما زلت أعمل، لحظات من فضلك...")
    try:
        result = await task
    except Exception as exc:
        await edit_progress(progress, title, 100, f"فشلت العملية: {str(exc)[:160]}")
        raise
    await edit_progress(progress, title, 100, "اكتملت العملية بنجاح.", reply_markup=reply_markup)
    return result


def bot_summary(row) -> str:
    running = manager().is_running(row["bot_id"])
    name = row["name"] or f"Bot {row['bot_id']}"
    return (
        f"🤖 <b>{html.escape(name)}</b>\n\n"
        f"🆔 المعرف: <code>{row['bot_id']}</code>\n"
        f"📌 الحالة: <b>{status_label(row['status'], running)}</b>\n"
        f"♻️ إعادة التشغيل التلقائي: <b>{'مفعلة' if row['auto_restart'] else 'متوقفة'}</b>\n"
        f"🧠 حد الذاكرة: <b>{row['max_memory_mb'] or config.MAX_BOT_MEMORY_MB} MB</b>\n"
        f"⏰ إعادة التشغيل الدورية: <b>{row['restart_interval_hours'] or 'متوقفة'}</b> ساعة\n"
        f"🕐 آخر تشغيل: <code>{row['last_started_at'] or 'لم يبدأ بعد'}</code>\n"
        f"⚠️ آخر خطأ: <code>{html.escape(row['last_error'] or 'لا يوجد')}</code>"
    )


async def send_menu(message) -> None:
    await message.reply_text(
        "👋 <b>أهلًا بك في منصة استضافة البوتات</b>\n\n"
        "ارفع بوت Telegram الخاص بك، وسأتولى تشغيله ومراقبته وإيقافه وإعادة تشغيله عند الحاجة.\n\n"
        "لا تحتاج إلى بوت مذاكرة هنا؛ هذه المنصة مخصصة لإدارة واستضافة بوتاتك فقط.",
        parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    await send_menu(update.message)


async def send_help(message) -> None:
    await message.reply_text(
        "📖 <b>طريقة استخدام منصة الاستضافة</b>\n\n"
        "1. اضغط «إضافة بوت».\n"
        "2. اكتب اسمًا للبوت.\n"
        "3. أرسل ملف ZIP يحتوي على ملفات البوت وملف التشغيل، مثل <code>main.py</code> أو <code>bot.py</code>.\n"
        "4. بعد قبول الملف، افتح «بوتاتي» واضغط تشغيل.\n\n"
        "الأوامر: <code>/start</code> و<code>/newbot</code> و<code>/mybots</code> و<code>/cancel</code>.\n"
        "بعد رفع البوت يمكنك إضافة متغيراته عبر <code>/setenv BOT_ID KEY VALUE</code>، مثل <code>/setenv 1 BOT_TOKEN 123:ABC</code>.\n"
        "يمكنك ضبط الذاكرة عبر <code>/setmemory BOT_ID MB</code> وجدولة إعادة التشغيل عبر <code>/schedule BOT_ID HOURS</code>.\n"
        "يستطيع المشرف استخدام <code>/admin</code> لمراجعة المستخدمين والبوتات وسجلات التدقيق.",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    await send_help(update.message)


async def begin_new_bot(message, user_id: int) -> None:
    pending = PENDING.get(user_id)
    if pending:
        if pending.get("action") == "archive":
            await message.reply_text("📦 لديك عملية رفع معلّقة. أرسل ملف ZIP أو استخدم /cancel لإلغائها.")
        else:
            await message.reply_text("📝 اكتب اسم البوت أولًا، أو استخدم /cancel لإلغاء العملية الحالية.")
        return
    if db.count_user_bots(user_id) >= db.get_max_bots(user_id):
        await message.reply_text(f"⚠️ وصلت إلى الحد المسموح وهو {db.get_max_bots(user_id)} بوتات.")
        return
    PENDING[user_id] = {"action": "name"}
    await message.reply_text("📝 اكتب اسم البوت الجديد. يمكنك استخدام اسم عربي أو إنجليزي.", reply_markup=back_keyboard())


async def new_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    await begin_new_bot(update.message, update.effective_user.id)


async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    await send_bots(update.message, update.effective_user.id, admin=False)


async def send_bots(message, owner_id: int, admin: bool, page: int = 1) -> None:
    page = max(1, int(page))
    rows, total = db.get_bots_page(admin, None if admin else owner_id, page, 20)
    if not rows and page == 1:
        await message.reply_text("🤖 لا توجد بوتات مضافة بعد. ابدأ بإضافة أول بوت لك.", reply_markup=main_keyboard())
        return
    lines = [f"🤖 <b>البوتات المتاحة</b> — صفحة {page}/{max(1, (total + 19) // 20)}", ""]
    buttons = []
    for row in rows:
        owner = f" — المالك <code>{row['owner_id']}</code>" if admin else ""
        name = row["name"] or f"Bot {row['bot_id']}"
        lines.append(f"• <b>{html.escape(name)}</b> — {status_label(row['status'], manager().is_running(row['bot_id']))}{owner}")
        buttons.append([InlineKeyboardButton(f"⚙️ {name}", callback_data=f"bot:view:{row['bot_id']}")])
    navigation = []
    if page > 1:
        navigation.append(InlineKeyboardButton("⬅️ السابق", callback_data=f"bots:{'admin' if admin else 'user'}:{page - 1}"))
    if page * 20 < total:
        navigation.append(InlineKeyboardButton("التالي ➡️", callback_data=f"bots:{'admin' if admin else 'user'}:{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("🔙 رجوع", callback_data="admin:back" if admin else "menu")])
    await message.reply_text("\n".join(lines)[:3900], parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons))


async def run_broadcast(bot, admin_id: int, text: str) -> None:
    global BROADCAST_TASK, BROADCAST_CANCEL
    targets = [row["user_id"] for row in db.get_all_users() if not row["is_banned"]]
    semaphore = asyncio.Semaphore(8)
    cancel_event = BROADCAST_CANCEL

    async def send_one(target_id: int) -> bool:
        if cancel_event and cancel_event.is_set():
            return False
        async with semaphore:
            if cancel_event and cancel_event.is_set():
                return False
            try:
                await bot.send_message(target_id, text)
                return True
            except RetryAfter as exc:
                await asyncio.sleep(float(exc.retry_after))
                if cancel_event and cancel_event.is_set():
                    return False
                try:
                    await bot.send_message(target_id, text)
                    return True
                except Exception:
                    return False
            except Exception:
                return False

    success = 0
    failed = 0
    total = len(targets)
    for start_index in range(0, total, 100):
        if cancel_event and cancel_event.is_set():
            break
        batch = targets[start_index:start_index + 100]
        results = await asyncio.gather(*(send_one(target_id) for target_id in batch))
        success += sum(1 for result in results if result)
        failed += len(results) - sum(1 for result in results if result)
        processed = min(total, start_index + len(batch))
        if processed and processed < total:
            try:
                await bot.send_message(admin_id, progress_text("الإذاعة جارية", round(processed * 100 / total), f"تمت معالجة {processed} من {total} مستخدم"))
            except Exception:
                logger.debug("Could not send broadcast progress", exc_info=True)
    canceled = bool(cancel_event and cancel_event.is_set())
    db.log_admin_action(admin_id, "broadcast_finished", "all", f"success={success},failed={failed},canceled={canceled}")
    try:
        await bot.send_message(admin_id, f"{'🛑 توقفت' if canceled else '✅ انتهت'} الإذاعة.\nتم الإرسال: {success}\nفشل أو لم يُرسل: {failed}", reply_markup=admin_keyboard())
    finally:
        if BROADCAST_TASK is asyncio.current_task():
            BROADCAST_TASK = None
            BROADCAST_CANCEL = None


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_admin(update.effective_user.id if update.effective_user else None):
        return
    db.log_admin_action(update.effective_user.id, "open_admin", "", "")
    await update.message.reply_text("👑 <b>لوحة إدارة منصة الاستضافة</b>\n\nمن هنا تتابع المستخدمين والبوتات وسجل العمليات.", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    pending = PENDING.pop(update.effective_user.id, None)
    if pending and pending.get("bot_id"):
        row = db.get_bot(int(pending["bot_id"]))
        if row:
            manager().stop_bot(row["bot_id"])
            delete_folder(row["folder"])
            db.delete_bot_db(row["bot_id"])
    await update.message.reply_text("✅ تم إلغاء العملية وتنظيف الملفات المؤقتة. أنا جاهز عندما تحتاجني.", reply_markup=main_keyboard())


async def document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update) or not update.message.document:
        return
    user_id = update.effective_user.id
    pending = PENDING.get(user_id, {})
    if pending.get("action") == "env_assignment":
        doc = update.message.document
        if doc.file_size and doc.file_size > 64 * 1024:
            await update.message.reply_text("⚠️ ملف .env كبير جدًا (الحد 64KB).")
            return
        row = owned_bot(int(pending["bot_id"]), user_id)
        if not row:
            return
        raw = bytes(await (await doc.get_file()).download_as_bytearray())
        pairs, bad_lines = parse_env_text(raw.decode("utf-8", errors="ignore"))
        if not pairs:
            await update.message.reply_text("⚠️ لم أجد أسطر KEY=VALUE صالحة في الملف.")
            return
        saved, rejected = save_env_pairs(row["bot_id"], user_id, pairs)
        PENDING.pop(user_id, None)
        await update.message.reply_text(
            env_report(saved, rejected) + (f"\n⚠️ أسطر غير مفهومة: {len(bad_lines)}" if bad_lines else ""),
            parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(row["bot_id"], is_admin(user_id)),
        )
        return
    if pending.get("action") != "archive":
        await update.message.reply_text("📦 ابدأ أولًا من زر «إضافة بوت»، ثم أرسل ملف ZIP.")
        return
    document_obj = update.message.document
    if document_obj.file_size and document_obj.file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
        await update.message.reply_text(f"⚠️ حجم الملف أكبر من الحد المسموح ({config.MAX_FILE_SIZE_MB}MB).")
        return
    if not (document_obj.file_name or "").lower().endswith(".zip"):
        await update.message.reply_text("⚠️ أرسل ملف ZIP فقط، وليس RAR أو ملفًا تنفيذيًا.")
        return
    bot_id = int(pending["bot_id"])
    temp_folder = make_temp_upload_folder(user_id, str(bot_id))
    archive_path = os.path.join(temp_folder, "upload.zip")
    progress = await update.message.reply_text(progress_text("تجهيز البوت", 10, "جارٍ تنزيل الملف..."), parse_mode=ParseMode.HTML)
    try:
        telegram_file = await document_obj.get_file()
        await telegram_file.download_to_drive(archive_path)
        await edit_progress(progress, "تجهيز البوت", 35, "تم تنزيل الملف، جارٍ فحص الأرشيف...")
        await asyncio.to_thread(extract_zip, archive_path, temp_folder)
        await asyncio.to_thread(flatten_single_root, temp_folder)
        await edit_progress(progress, "تجهيز البوت", 60, "تم فك الأرشيف بأمان، جارٍ تحليل البوت بالذكاء الاصطناعي...")
        
        # AI-powered bot analysis
        analysis_result = await asyncio.to_thread(analyze_bot, temp_folder)
        
        await edit_progress(progress, "تجهيز البوت", 70, "تم التحليل، جارٍ العثور على ملف التشغيل...")
        entry = await asyncio.to_thread(find_entry_file, temp_folder)
        if not entry:
            raise ValueError("لم أجد ملف Python للتشغيل. استخدم main.py أو bot.py أو app.py.")
        valid, error = await asyncio.to_thread(check_syntax, entry)
        if not valid:
            raise ValueError(f"خطأ في صياغة ملف التشغيل: {error}")
        
        await edit_progress(progress, "تجهيز البوت", 85, "تم التحقق من ملف التشغيل، جارٍ حفظ البوت...")
        folder = await asyncio.to_thread(finalize_bot_folder, temp_folder, user_id, bot_id)
        db.set_bot_files(bot_id, folder, entry.replace(temp_folder, folder, 1))
        db.log_admin_action(user_id, "upload_bot", bot_id, f"entry={os.path.basename(entry)}")
        
        # Save detected environment variables
        if analysis_result.success and analysis_result.env_vars:
            for env_key in analysis_result.env_vars.keys():
                if env_key not in config.PROTECTED_ENV_KEYS:
                    db.set_env_var(bot_id, env_key, "")
        
        # Apply suggested memory limit if different from default
        if analysis_result.success and analysis_result.suggested_memory_mb:
            if analysis_result.suggested_memory_mb != config.MAX_BOT_MEMORY_MB:
                db.set_max_memory(bot_id, min(analysis_result.suggested_memory_mb, config.MAX_BOT_MEMORY_MB))
        
        PENDING.pop(user_id, None)
        row = db.get_bot(bot_id)
        
        # Build analysis report message
        analysis_report = ""
        if analysis_result.success:
            if analysis_result.bot_type != "unknown":
                analysis_report += f"\n🤖 نوع البوت: <b>{analysis_result.bot_type}</b>"
            if analysis_result.required_packages:
                pkgs_list = ", ".join(sorted(analysis_result.required_packages)[:8])
                if len(analysis_result.required_packages) > 8:
                    pkgs_list += f" و{len(analysis_result.required_packages) - 8} أخرى"
                analysis_report += f"\n📦 الحزم المكتشفة: <code>{pkgs_list}</code>"
            if analysis_result.env_vars:
                env_list = ", ".join(sorted(analysis_result.env_vars.keys())[:5])
                if len(analysis_result.env_vars) > 5:
                    env_list += f" و{len(analysis_result.env_vars) - 5} أخرى"
                analysis_report += f"\n🔐 متغيرات البيئة المطلوبة: <b>{len(analysis_result.env_vars)}</b>\n<code>{env_list}</code>"
                
                # Check if BOT_TOKEN is required but not set
                if "BOT_TOKEN" in analysis_result.env_vars:
                    analysis_report += "\n\n⚠️ <b>مهم:</b> يبدو أن البوت يحتاج إلى <code>BOT_TOKEN</code>.\nلإضافته، اضغط على «⚙️ إعدادات البوت» ثم «➕ إضافة متغير» وأرسل:\n<code>BOT_TOKEN=123456:ABCxyz</code>"
                    
            if analysis_result.recommendations:
                analysis_report += f"\n💡 التوصيات: {len(analysis_result.recommendations)} توصيات"
            if analysis_result.security_warnings:
                analysis_report += f"\n⚠️ تحذيرات أمنية: {len(analysis_result.security_warnings)}"
        else:
            analysis_report += f"\n⚠️ فشل التحليل: {html.escape(str(analysis_result.error_message)[:200])}"
        
        await edit_progress(progress, "تجهيز البوت", 100, "تم قبول البوت وتجهيزه.", reply_markup=bot_keyboard(bot_id))
        await update.message.reply_text(
            f"✅ <b>تم قبول البوت وتجهيزه</b>\n\n" + 
            bot_summary(row) + 
            analysis_report +
            "\n\nاضغط تشغيل عندما تكون جاهزًا.\n" +
            ("💡 <i>تم تحليل البوت تلقائيًا واكتشاف متطلباته. راجع متغيرات البيئة المطلوبة قبل التشغيل.</i>" if analysis_result.success else ""),
            parse_mode=ParseMode.HTML,
            reply_markup=bot_keyboard(bot_id),
        )
    except Exception as exc:
        logger.exception("Upload failed for user %s", user_id)
        delete_folder(temp_folder)
        db.delete_bot_db(bot_id)
        PENDING.pop(user_id, None)
        await edit_progress(progress, "تجهيز البوت", 100, f"فشلت العملية: {str(exc)[:160]}", reply_markup=main_keyboard())
        await update.message.reply_text(f"❌ لم أستطع تجهيز البوت بأمان. السبب: <code>{html.escape(str(exc)[:500])}</code>", parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text or not ensure_user(update):
        return
    user_id = update.effective_user.id
    pending = PENDING.get(user_id, {})
    value = update.message.text.strip()
    action = pending.get("action")
    if action == "limit":
        parts = value.split()
        if len(parts) != 2:
            await update.message.reply_text("⚠️ الصيغة: USER_ID MAX_BOTS، مثل 123456789 5")
            return
        try:
            target_id, max_bots = int(parts[0]), int(parts[1])
        except ValueError:
            await update.message.reply_text("⚠️ أرسل رقم العضو والحد الأقصى كأرقام.")
            return
        if not is_admin(user_id) or not db.get_user(target_id) or not 1 <= max_bots <= 50:
            await update.message.reply_text("⚠️ العضو غير موجود أو الحد يجب أن يكون بين 1 و50.")
            return
        db.set_max_bots(target_id, max_bots)
        db.log_admin_action(user_id, "set_max_bots", target_id, str(max_bots))
        PENDING.pop(user_id, None)
        await update.message.reply_text(f"✅ تم ضبط الحد الأقصى للعضو على {max_bots} بوتات.", reply_markup=admin_keyboard())
        return
    if action == "env_assignment":
        row = owned_bot(int(pending["bot_id"]), user_id)
        pairs, bad_lines = parse_env_text(value)
        if not row or (not pairs and not bad_lines):
            await update.message.reply_text("⚠️ لا يوجد ما يمكن حفظه.")
            return
        if not pairs:
            await update.message.reply_text(
                "⚠️ اكتب المتغير بهذا الشكل: <code>KEY=VALUE</code>\n\nمثال للتوكن: <code>BOT_TOKEN=123456:ABCxyz</code>\n"
                "ويمكنك لصق عدة أسطر دفعة واحدة أو إرسال ملف <code>.env</code>.",
                parse_mode=ParseMode.HTML,
            )
            return
        saved, rejected = save_env_pairs(row["bot_id"], user_id, pairs)
        PENDING.pop(user_id, None)
        extra = f"\n⚠️ أسطر غير مفهومة: {len(bad_lines)}" if bad_lines else ""
        hint = ""
        if "BOT_TOKEN" in saved:
            hint = "\n🎯 تم حفظ التوكن؛ شغّل البوت الآن."
        await update.message.reply_text(
            env_report(saved, rejected) + extra + hint,
            parse_mode=ParseMode.HTML,
            reply_markup=bot_keyboard(row["bot_id"], is_admin(user_id)),
        )
        return
    if action == "rename_bot":
        row = owned_bot(int(pending["bot_id"]), user_id)
        new_name = " ".join(value.split())
        if not row or not (1 <= len(new_name) <= 40):
            await update.message.reply_text("⚠️ الاسم يجب أن يكون بين 1 و40 حرفًا.")
            return
        db.rename_bot(row["bot_id"], new_name)
        db.log_admin_action(user_id, "rename_bot", row["bot_id"], new_name)
        PENDING.pop(user_id, None)
        await update.message.reply_text(
            f"✅ أصبح اسم البوت «{html.escape(new_name)}».", parse_mode=ParseMode.HTML,
            reply_markup=bot_keyboard(row["bot_id"], is_admin(user_id)),
        )
        return
    if action == "memory_assignment":
        row = owned_bot(int(pending["bot_id"]), user_id)
        if not row:
            PENDING.pop(user_id, None)
            await update.message.reply_text("🚫 لا يمكنك تعديل هذا البوت.")
            return
        if value.lower() == "default":
            memory_mb = None
        else:
            try:
                memory_mb = int(value)
            except ValueError:
                await update.message.reply_text("⚠️ اكتب رقم الذاكرة بالميغابايت أو default.")
                return
            if not 128 <= memory_mb <= config.MAX_BOT_MEMORY_MB:
                await update.message.reply_text(f"⚠️ اختر قيمة بين 128 و{config.MAX_BOT_MEMORY_MB} ميغابايت.")
                return
        db.set_max_memory(row["bot_id"], memory_mb)
        db.log_admin_action(user_id, "set_memory", row["bot_id"], str(memory_mb or "default"))
        PENDING.pop(user_id, None)
        await update.message.reply_text("✅ تم تحديث حد الذاكرة. أعد تشغيل البوت لتطبيقه.", reply_markup=bot_keyboard(row["bot_id"], is_admin(user_id)))
        return
    if action in {"ban", "unban"}:
        try:
            target_id = int(value)
        except ValueError:
            await update.message.reply_text("⚠️ أرسل chat_id رقميًا صحيحًا.")
            return
        target = db.get_user(target_id)
        if not target:
            await update.message.reply_text("⚠️ هذا العضو غير مسجل في المنصة.")
            return
        if target_id in config.ADMIN_IDS:
            await update.message.reply_text("⚠️ لا يمكن حظر مشرف موجودًا في ADMIN_IDS.")
            return
        if action == "ban":
            db.ban_user(target_id)
            for bot_row in db.get_user_bots(target_id):
                manager().stop_bot(bot_row["bot_id"])
            result = "🚫 تم حظر العضو وإيقاف بوتاته العاملة ومنع تشغيلها من خلال المنصة."
        else:
            db.unban_user(target_id)
            result = "✅ تم فك حظر العضو."
        db.log_admin_action(user_id, action, target_id, "")
        PENDING.pop(user_id, None)
        await update.message.reply_text(result, reply_markup=admin_keyboard())
        return
    if action == "broadcast":
        PENDING.pop(user_id, None)
        global BROADCAST_TASK, BROADCAST_CANCEL
        if BROADCAST_TASK and not BROADCAST_TASK.done():
            await update.message.reply_text("⏳ توجد إذاعة قيد التنفيذ بالفعل.", reply_markup=admin_keyboard())
            return
        BROADCAST_CANCEL = asyncio.Event()
        BROADCAST_TASK = asyncio.create_task(run_broadcast(context.bot, user_id, value), name="hosting-broadcast")
        db.log_admin_action(user_id, "broadcast_started", "all", value[:120])
        await update.message.reply_text("📢 بدأت الإذاعة في الخلفية. سأرسل لك تقرير النجاح والفشل عند الانتهاء.", reply_markup=admin_keyboard())
        return
    if action == "name":
        if not value or len(value) > 80:
            await update.message.reply_text("⚠️ اكتب اسمًا بين حرف واحد و80 حرفًا.")
            return
        bot_id = db.insert_bot_if_room(user_id, value, db.get_max_bots(user_id))
        if bot_id is None:
            PENDING.pop(user_id, None)
            await update.message.reply_text("⚠️ وصلت إلى الحد المسموح من البوتات.", reply_markup=main_keyboard())
            return
        PENDING[user_id] = {"action": "archive", "bot_id": bot_id}
        await update.message.reply_text(
            f"✅ تم إنشاء البوت <b>{html.escape(value)}</b>.\n\nأرسل الآن ملف ZIP الخاص به. يجب أن يحتوي على ملف تشغيل Python ويفضل أن يحتوي على <code>requirements.txt</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    if action == "update_archive":
        # Handle ZIP upload for bot update
        pending = PENDING.get(user_id, {})
        bot_id = int(pending.get("bot_id", 0))
        row = db.get_bot(bot_id)
        if not row or row["owner_id"] != user_id and not is_admin(user_id):
            PENDING.pop(user_id, None)
            await update.message.reply_text("🚫 لا يمكنك تحديث هذا البوت.", reply_markup=main_keyboard())
            return
        document_obj = update.message.document
        if not document_obj or not (document_obj.file_name or "").lower().endswith(".zip"):
            await update.message.reply_text("⚠️ أرسل ملف ZIP صالح.")
            return
        if document_obj.file_size and document_obj.file_size > config.MAX_FILE_SIZE_MB * 1024 * 1024:
            await update.message.reply_text(f"⚠️ حجم الملف أكبر من الحد المسموح ({config.MAX_FILE_SIZE_MB}MB).")
            return
        temp_folder = make_temp_upload_folder(user_id, f"update_{bot_id}")
        archive_path = os.path.join(temp_folder, "upload.zip")
        progress = await update.message.reply_text(progress_text("تحديث البوت", 10, "جارٍ تنزيل الملف..."), parse_mode=ParseMode.HTML)
        try:
            telegram_file = await document_obj.get_file()
            await telegram_file.download_to_drive(archive_path)
            await edit_progress(progress, "تحديث البوت", 35, "تم تنزيل الملف، جارٍ فحص الأرشيف...")
            await asyncio.to_thread(extract_zip, archive_path, temp_folder)
            await asyncio.to_thread(flatten_single_root, temp_folder)
            await edit_progress(progress, "تحديث البوت", 60, "تم فك الأرشيف، جارٍ البحث عن ملف التشغيل...")
            entry = await asyncio.to_thread(find_entry_file, temp_folder)
            if not entry:
                raise ValueError("لم أجد ملف Python للتشغيل. استخدم main.py أو bot.py أو app.py.")
            valid, error = await asyncio.to_thread(check_syntax, entry)
            if not valid:
                raise ValueError(f"خطأ في صياغة ملف التشغيل: {error}")
            await edit_progress(progress, "تحديث البوت", 80, "تم التحقق، جارٍ استبدال الملفات...")
            old_folder = row["folder"]
            if os.path.exists(old_folder):
                shutil.rmtree(old_folder, ignore_errors=True)
            new_folder = await asyncio.to_thread(finalize_bot_folder, temp_folder, user_id, bot_id)
            entry_final = entry.replace(temp_folder, new_folder, 1)
            db.set_bot_files(bot_id, new_folder, entry_final)
            db.log_admin_action(user_id, "update_bot", bot_id, f"entry={os.path.basename(entry_final)}")
            PENDING.pop(user_id, None)
            updated_row = db.get_bot(bot_id)
            await edit_progress(progress, "تحديث البوت", 100, "تم تحديث البوت بنجاح!", reply_markup=bot_keyboard(bot_id, is_admin(user_id)))
            await update.message.reply_text(
                "✅ <b>تم تحديث البوت بنجاح</b>\n\n"
                + bot_summary(updated_row)
                + "\n\nاضغط تشغيل لتشغيل النسخة المحدثة.",
                parse_mode=ParseMode.HTML,
                reply_markup=bot_keyboard(bot_id, is_admin(user_id)),
            )
        except Exception as exc:
            logger.exception("Update failed for bot %s", bot_id)
            delete_folder(temp_folder)
            PENDING.pop(user_id, None)
            await edit_progress(progress, "تحديث البوت", 100, f"فشل التحديث: {str(exc)[:160]}", reply_markup=bot_keyboard(bot_id, is_admin(user_id)))
            await update.message.reply_text(f"❌ لم أستطع تحديث البوت. السبب: <code>{html.escape(str(exc)[:500])}</code>", parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(user_id)))
    await update.message.reply_text("استخدم الأزرار أو أرسل /help لمعرفة الخطوات.", reply_markup=main_keyboard())


async def set_env(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    args = context.args or []
    if len(args) < 3:
        await update.message.reply_text("الصيغة: /setenv BOT_ID KEY VALUE\nمثال: /setenv 1 BOT_TOKEN 123:ABC")
        return
    try:
        bot_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ BOT_ID يجب أن يكون رقمًا صحيحًا.")
        return
    key = args[1].upper()
    value = " ".join(args[2:])
    if "\x00" in value:
        await update.message.reply_text("⚠️ قيمة المتغير تحتوي محرفًا غير صالح.")
        return
    if not is_valid_env_key(key):
        await update.message.reply_text("⚠️ اسم المتغير يجب أن يكون مثل BOT_TOKEN أو API_KEY.")
        return
    if key in config.PROTECTED_ENV_KEYS:
        await update.message.reply_text("⚠️ هذا المتغير محمي ولا يمكن تغييره من خلال المنصة.")
        return
    row = db.get_bot(bot_id)
    if not row or (row["owner_id"] != update.effective_user.id and not is_admin(update.effective_user.id)):
        await update.message.reply_text("🚫 لا يمكنك تعديل هذا البوت.")
        return
    db.set_env_var(bot_id, key, value)
    db.log_admin_action(update.effective_user.id, "set_env", bot_id, f"key={key}")
    await update.message.reply_text(f"✅ تم حفظ المتغير <code>{html.escape(key)}</code>.\nلن أعرض قيمته في الواجهة.", parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(update.effective_user.id)))


def save_env_pairs(bot_id: int, user_id: int, pairs) -> tuple[list[str], list[str]]:
    """يحفظ أزواج KEY/VALUE بعد التحقق. يرجع (المحفوظة, المرفوضة)."""
    saved, rejected = [], []
    for key, env_value in pairs:
        if not is_valid_env_key(key) or key in config.PROTECTED_ENV_KEYS or "\x00" in env_value:
            rejected.append(key)
            continue
        db.set_env_var(bot_id, key, env_value)
        saved.append(key)
    if saved:
        db.log_admin_action(user_id, "set_env", bot_id, "keys=" + ",".join(saved))
    return saved, rejected


def env_report(saved, rejected) -> str:
    text_out = f"✅ تم حفظ <b>{len(saved)}</b> متغير: " + ", ".join(f"<code>{html.escape(k)}</code>" for k in saved[:15])
    if rejected:
        text_out += f"\n⚠️ مرفوض ({len(rejected)}): " + ", ".join(f"<code>{html.escape(str(k))}</code>" for k in rejected[:10])
    return text_out + "\n\nأعد تشغيل البوت لتطبيق التغييرات."


def is_valid_env_key(key: str) -> bool:
    return bool(key) and key.replace("_", "").isalnum() and key[0].isalpha() and key.isascii()


def owned_bot(bot_id: int, user_id: int):
    row = db.get_bot(bot_id)
    if not row or (row["owner_id"] != user_id and not is_admin(user_id)):
        return None
    return row


async def set_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text("الصيغة: /setmemory BOT_ID MB\nاستخدم default للعودة إلى الحد العام.")
        return
    try:
        bot_id = int(args[0])
    except ValueError:
        await update.message.reply_text("⚠️ BOT_ID يجب أن يكون رقمًا صحيحًا.")
        return
    row = owned_bot(bot_id, update.effective_user.id)
    if not row:
        await update.message.reply_text("🚫 لا يمكنك تعديل هذا البوت.")
        return
    if args[1].lower() == "default":
        memory_mb = None
    else:
        try:
            memory_mb = int(args[1])
        except ValueError:
            await update.message.reply_text("⚠️ اكتب الذاكرة بالميغابايت، مثل 512، أو اكتب default.")
            return
        if not 128 <= memory_mb <= config.MAX_BOT_MEMORY_MB:
            await update.message.reply_text(f"⚠️ اختر قيمة بين 128 و{config.MAX_BOT_MEMORY_MB} ميغابايت.")
            return
    db.set_max_memory(bot_id, memory_mb)
    db.log_admin_action(update.effective_user.id, "set_memory", bot_id, str(memory_mb or "default"))
    await update.message.reply_text("✅ تم تحديث حد الذاكرة. أعد تشغيل البوت لتطبيق القيمة الجديدة.", reply_markup=bot_keyboard(bot_id, is_admin(update.effective_user.id)))


async def schedule_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text("الصيغة: /schedule BOT_ID HOURS\nالقيم المتاحة: 0 أو 6 أو 12 أو 24 أو 48.")
        return
    try:
        bot_id = int(args[0])
        hours = int(args[1])
    except ValueError:
        await update.message.reply_text("⚠️ أرسل رقم البوت وعدد الساعات كأرقام.")
        return
    row = owned_bot(bot_id, update.effective_user.id)
    if not row or hours not in config.RESTART_INTERVAL_CHOICES_HOURS:
        await update.message.reply_text("⚠️ لا يمكنك تعديل هذا البوت أو أن قيمة الساعات غير مدعومة.")
        return
    db.set_restart_interval(bot_id, hours)
    db.log_admin_action(update.effective_user.id, "set_restart_schedule", bot_id, str(hours))
    label = "متوقفة" if hours == 0 else f"كل {hours} ساعة"
    await update.message.reply_text(f"✅ أصبحت إعادة التشغيل الدورية: <b>{label}</b>.", parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(update.effective_user.id)))


async def my_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not ensure_user(update):
        return
    rows = db.get_user_bots(update.effective_user.id)
    running = sum(1 for row in rows if manager().is_running(row["bot_id"]))
    await update.message.reply_text(
        f"📊 <b>إحصائياتك</b>\n\n🤖 إجمالي البوتات: <b>{len(rows)}</b>\n🟢 البوتات العاملة الآن: <b>{running}</b>\n♻️ الحد الأقصى: <b>{db.get_max_bots(update.effective_user.id)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=back_keyboard(),
    )


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message or not query.from_user:
        return
    await query.answer()
    if not ensure_user(update) and not is_admin(query.from_user.id):
        return
    data = query.data or ""
    if data in {"menu", "help", "my_bots", "my_stats", "admin:back"}:
        PENDING.pop(query.from_user.id, None)
    if data == "menu":
        await send_menu(query.message)
        return
    if data == "help":
        await send_help(query.message)
        return
    if data in {"new_bot", "my_bots", "my_stats"}:
        if data == "new_bot":
            await begin_new_bot(query.message, query.from_user.id)
        elif data == "my_bots":
            await send_bots(query.message, query.from_user.id, admin=False)
        else:
            rows = db.get_user_bots(query.from_user.id)
            running = sum(1 for row in rows if manager().is_running(row["bot_id"]))
            await query.message.reply_text(f"📊 <b>إحصائياتك</b>\n\n🤖 البوتات: <b>{len(rows)}</b>\n🟢 العاملة: <b>{running}</b>", parse_mode=ParseMode.HTML, reply_markup=back_keyboard())
        return
    if data.startswith("admin:"):
        await admin_callback(query, data)
        return
    if data.startswith("bots:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1] in {"admin", "user"}:
            try:
                page = max(1, int(parts[2]))
            except ValueError:
                page = 1
            is_admin_page = parts[1] == "admin"
            if is_admin_page and not is_admin(query.from_user.id):
                return
            await send_bots(query.message, query.from_user.id, admin=is_admin_page, page=page)
        return
    if data.startswith("bot:"):
        await bot_callback(query, data)


def disk_line() -> str:
    try:
        usage = shutil.disk_usage(config.DB_PATH.parent)
    except OSError:
        return ""
    state = "\n🛠 وضع الصيانة: <b>مفعّل</b>" if maintenance_on() else ""
    return f"\n💽 التخزين: <b>{usage.free / usage.total * 100:.0f}%</b> حر ({usage.free // 1048576}MB){state}"


async def admin_callback(query, data: str) -> None:
    if not is_admin(query.from_user.id):
        await query.message.reply_text("🚫 ليس لديك صلاحية الوصول إلى هذا القسم.")
        return
    action = data.split(":", 1)[1]
    if action == "maintenance":
        new_value = "0" if maintenance_on() else "1"
        db.set_setting("maintenance", new_value)
        db.log_admin_action(query.from_user.id, "maintenance", "", new_value)
        await query.message.reply_text(
            "🛠 وضع الصيانة <b>مفعّل</b>: المستخدمون العاديون محجوبون عن بوت التحكم، والبوتات المستضافة تعمل."
            if new_value == "1" else "✅ تم إيقاف وضع الصيانة.",
            parse_mode=ParseMode.HTML, reply_markup=admin_keyboard(),
        )
    elif action == "top":
        def collect():
            items = []
            for r in db.get_bots_by_status("running"):
                u = manager().get_usage(r["bot_id"], fast=True)
                if u:
                    items.append((u["mem_bytes"], u["cpu_value"], r))
            return sorted(items, key=lambda x: x[0], reverse=True)[:10]

        items = await asyncio.to_thread(collect)
        if not items:
            body = "لا توجد بوتات عاملة الآن."
        else:
            body = "\n".join(
                f"{i}. <b>{html.escape(r['name'] or str(r['bot_id']))}</b> — 💾 {human_size(mem)} · 🧠 {cpu:.0f}% · 👤 <code>{r['owner_id']}</code>"
                for i, (mem, cpu, r) in enumerate(items, 1)
            )
        await query.message.reply_text(f"🏆 <b>الأكثر استهلاكًا للذاكرة</b>\n\n{body}", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    elif action == "stats":
        stats = db.global_stats()
        await query.message.reply_text(
            f"📊 <b>إحصائيات المنصة</b>\n\n👥 المستخدمون: <b>{stats['total_users']}</b>\n🤖 إجمالي البوتات: <b>{stats['total_bots']}</b>\n🟢 العاملة: <b>{stats['running']}</b>\n🔴 المتعطلة: <b>{stats['crashed']}</b>\n⏱ مدة تشغيل المنصة: <b>{int(time.time() - STARTED_AT)} ثانية</b>{disk_line()}",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_keyboard(),
        )
    elif action == "users":
        page = 1
        if data.startswith("admin:users_page:"):
            try:
                page = int(data.split(":")[-1])
            except (ValueError, IndexError):
                page = 1
        rows, total = db.get_users_page(page, 50)
        if not rows:
            text_value = "👥 لا يوجد مستخدمون بعد."
        else:
            lines = ["👥 <b>المستخدمون</b>", ""]
            for row in rows:
                lines.append(
                    f"• <code>{row['user_id']}</code> — @{html.escape(row['username'] or 'بدون_username')} — "
                    f"{'🚫 محظور' if row['is_banned'] else '✅ نشط'} — بوتات: {db.count_user_bots(row['user_id'])}"
                )
            text_value = "\n".join(lines)
        if total > 50:
            total_pages = (total + 49) // 50
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("◀️ السابقة", callback_data=f"admin:users_page:{page-1}"))
            nav.append(InlineKeyboardButton(f"الصفحة {page} من {total_pages}", callback_data="admin:users"))
            if page < total_pages:
                nav.append(InlineKeyboardButton("التالية ▶️", callback_data=f"admin:users_page:{page+1}"))
            keyboard = InlineKeyboardMarkup([nav])
        else:
            keyboard = admin_keyboard()
        await query.message.reply_text(text_value[:3900], parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif action == "bots":
        page = 1
        if data.startswith("admin:bots_page:"):
            try:
                page = max(1, int(data.split(":")[-1]))
            except (ValueError, IndexError):
                page = 1
        await send_bots(query.message, query.from_user.id, admin=True, page=page)
    elif action == "ban":
        PENDING[query.from_user.id] = {"action": "ban"}
        await query.message.reply_text("🚫 أرسل chat_id العضو الذي تريد حظره.", reply_markup=admin_keyboard())
    elif action == "limit":
        PENDING[query.from_user.id] = {"action": "limit"}
        await query.message.reply_text("🎛 أرسل chat_id والحد الأقصى بهذا الشكل: <code>123456789 5</code>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())
    elif action == "stop_all_confirm":
        await query.message.reply_text("⚠️ سيؤدي هذا إلى إيقاف كل البوتات العاملة. هل تريد المتابعة؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نعم، أوقف الكل", callback_data="admin:stop_all"), InlineKeyboardButton("إلغاء", callback_data="admin:back")]]))
    elif action == "stop_all":
        stopped = 0
        for bot_row in db.get_all_bots():
            if manager().is_running(bot_row["bot_id"]):
                manager().stop_bot(bot_row["bot_id"])
                stopped += 1
        db.log_admin_action(query.from_user.id, "stop_all", "all", str(stopped))
        await query.message.reply_text(f"🛑 تم إيقاف {stopped} بوتًا عاملًا.", reply_markup=admin_keyboard())
    elif action == "unban":
        PENDING[query.from_user.id] = {"action": "unban"}
        await query.message.reply_text("✅ أرسل chat_id العضو الذي تريد فك حظره.", reply_markup=admin_keyboard())
    elif action == "broadcast":
        if BROADCAST_TASK and not BROADCAST_TASK.done():
            await query.message.reply_text("⏳ توجد إذاعة قيد التنفيذ بالفعل. استخدم زر الإيقاف إذا أردت إنهاءها.", reply_markup=admin_keyboard())
        else:
            PENDING[query.from_user.id] = {"action": "broadcast"}
            await query.message.reply_text("📢 أرسل الآن نص الرسالة التي تريد إرسالها إلى المستخدمين.", reply_markup=admin_keyboard())
    elif action == "broadcast_cancel":
        if BROADCAST_CANCEL:
            BROADCAST_CANCEL.set()
            await query.message.reply_text("🛑 تم طلب إيقاف الإذاعة. سيصلك التقرير بعد إنهاء الإرسال الجاري.", reply_markup=admin_keyboard())
        else:
            await query.message.reply_text("لا توجد إذاعة قيد التنفيذ حاليًا.", reply_markup=admin_keyboard())
    elif action == "audit":
        page = 1
        if data.startswith("admin:audit_page:"):
            try:
                page = int(data.split(":")[-1])
            except (ValueError, IndexError):
                page = 1
        rows, total = db.get_audit_log_page(page, 50)
        if not rows:
            text_value = "🧾 لا يوجد سجل تدقيق بعد."
        else:
            lines = ["🧾 <b>سجل التدقيق</b>", ""]
            for row in rows:
                lines.append(
                    f"<code>{row['ts']}</code> — admin <code>{row['admin_id']}</code> — "
                    f"<b>{html.escape(row['action'])}</b> — {html.escape(row['target'] or '')} — "
                    f"{html.escape(row['details'] or '')}"
                )
            text_value = "\n".join(lines)
        if total > 50:
            total_pages = (total + 49) // 50
            nav = []
            if page > 1:
                nav.append(InlineKeyboardButton("◀️ السابقة", callback_data=f"admin:audit_page:{page-1}"))
            nav.append(InlineKeyboardButton(f"الصفحة {page} من {total_pages}", callback_data="admin:audit"))
            if page < total_pages:
                nav.append(InlineKeyboardButton("التالية ▶️", callback_data=f"admin:audit_page:{page+1}"))
            keyboard = InlineKeyboardMarkup([nav])
        else:
            keyboard = admin_keyboard()
        await query.message.reply_text(text_value[:3900], parse_mode=ParseMode.HTML, reply_markup=keyboard)
    elif action == "back":
        await query.message.reply_text("👑 <b>لوحة إدارة منصة الاستضافة</b>", parse_mode=ParseMode.HTML, reply_markup=admin_keyboard())


async def bot_callback(query, data: str) -> None:
    parts = data.split(":")
    if len(parts) not in {3, 4}:
        return
    action, bot_id_text = parts[1], parts[2]
    try:
        bot_id = int(bot_id_text)
    except ValueError:
        return
    row = db.get_bot(bot_id)
    if not row:
        await query.message.reply_text("⚠️ هذا البوت لم يعد موجودًا.")
        return
    if row["owner_id"] != query.from_user.id and not is_admin(query.from_user.id):
        await query.message.reply_text("🚫 هذا البوت ليس تابعًا لحسابك.")
        return
    if action == "view":
        await query.message.reply_text(bot_summary(row), parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    if action == "schedule":
        rows = [[InlineKeyboardButton("إيقاف", callback_data=f"bot:schedule_set:{bot_id}:0")]]
        rows.extend([[InlineKeyboardButton(f"كل {hours} ساعة", callback_data=f"bot:schedule_set:{bot_id}:{hours}")] for hours in config.RESTART_INTERVAL_CHOICES_HOURS if hours])
        rows.append([InlineKeyboardButton("🔙 رجوع", callback_data=f"bot:view:{bot_id}")])
        await query.message.reply_text("⏰ اختر فاصل إعادة التشغيل الدوري:", reply_markup=InlineKeyboardMarkup(rows))
        return
    if action == "schedule_set":
        try:
            hours = int(parts[3])
        except (IndexError, ValueError):
            return
        if hours not in config.RESTART_INTERVAL_CHOICES_HOURS:
            return
        db.set_restart_interval(bot_id, hours)
        db.log_admin_action(query.from_user.id, "set_restart_schedule", bot_id, str(hours))
        await query.message.reply_text("✅ تم تحديث الجدولة.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    if action == "auto":
        enabled = db.toggle_auto_restart(bot_id)
        db.log_admin_action(query.from_user.id, "toggle_auto_restart", bot_id, str(enabled))
        await query.message.reply_text(f"✅ إعادة التشغيل التلقائي أصبحت {'مفعّلة' if enabled else 'متوقفة'}.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    db.log_admin_action(query.from_user.id, f"bot_{action}", bot_id, "")
    if action == "start":
        ok, message = await run_with_progress(query.message, "تشغيل البوت", lambda: manager().start_bot(row), bot_keyboard(bot_id, is_admin(query.from_user.id)))
    elif action == "stop":
        ok, message = await run_with_progress(query.message, "إيقاف البوت", lambda: manager().stop_bot(bot_id), bot_keyboard(bot_id, is_admin(query.from_user.id)))
    elif action == "restart":
        ok, message = await run_with_progress(query.message, "إعادة تشغيل البوت", lambda: manager().restart_bot(row), bot_keyboard(bot_id, is_admin(query.from_user.id)))
    elif action == "logs":
        log_path = Path(row["folder"]) / "run.log"
        content = tail_file(str(log_path), config.LOG_TAIL_LINES)
        await query.message.reply_text(f"📜 <b>سجل {html.escape(row['name'])}</b>\n\n<pre>{html.escape(content[-3600:])}</pre>", parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    elif action == "logs_full":
        log_path = Path(row["folder"]) / "run.log"
        if not log_path.exists() or log_path.stat().st_size == 0:
            await query.message.reply_text("📥 لا يوجد سجل بعد.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
            return
        # حد تيليجرام للملفات 50MB؛ سجلنا مقتطع عند MAX_LOG_SIZE_MB لذا يكفي عادةً
        with open(log_path, "rb") as fh:
            await query.message.reply_document(
                document=fh,
                filename=safe_display_filename(row["name"], "_run.log", "bot"),
                caption=f"📥 السجل الكامل لبوت «{row['name']}»",
                reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)),
            )
        return
    elif action == "health":
        result = await asyncio.to_thread(manager().check_health, bot_id)
        if not manager().is_running(bot_id):
            text_out = "🩺 البوت متوقف حاليًا؛ شغّله أولًا."
        elif result["status"] is None:
            text_out = ("🩺 العملية تعمل، لكن لا يمكن التحقق من اتصاله بتيليجرام.\n"
                        f"أضف متغير <code>{config.BOT_TOKEN_ENV_KEY}</code> من «🔐 البيئة» لتفعيل الفحص العميق.")
        elif result["status"]:
            text_out = f"🩺 <b>البوت سليم</b> ✅\nالعملية تعمل ويتصل بتيليجرام.\n<code>{html.escape(result['details'])}</code>"
        else:
            text_out = f"🩺 <b>العملية تعمل لكن الاتصال بتيليجرام فاشل</b> ⚠️\nتحقق من صحة التوكن.\n<code>{html.escape(result['details'])}</code>"
        await query.message.reply_text(text_out, parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    elif action == "env":
        env_rows = db.list_env_vars(bot_id)
        listed = "\n".join(
            f"• <code>{html.escape(r['key'])}</code> — {html.escape(r['display_value'])}"
            for r in env_rows
        ) or "لا توجد متغيرات محفوظة."
        delete_buttons = [
            [InlineKeyboardButton(f"🗑 حذف {r['key']}", callback_data=f"bot:env_del:{bot_id}:{r['id']}")]
            for r in env_rows[:20]
        ]
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ إضافة متغير", callback_data=f"bot:env_add:{bot_id}")],
            *delete_buttons,
            [InlineKeyboardButton("🔙 رجوع", callback_data=f"bot:view:{bot_id}")],
        ])
        await query.message.reply_text(
            f"🔐 <b>متغيرات البيئة</b>\n\n{listed}\n\nالقيم الحساسة مشفرة ولا تُعرض.",
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        return
    elif action == "rename":
        PENDING[query.from_user.id] = {"action": "rename_bot", "bot_id": bot_id}
        await query.message.reply_text("✏️ أرسل الاسم الجديد للبوت (حتى 40 حرفًا).")
        return
    elif action == "rebuild":
        ok, message = await run_with_progress(
            query.message, "إعادة بناء البيئة", lambda: manager().rebuild_env(row),
            bot_keyboard(bot_id, is_admin(query.from_user.id)),
        )
    elif action == "env_del":
        try:
            env_id = int(parts[3])
        except (IndexError, ValueError):
            return
        removed = db.delete_env_var_for_bot(bot_id, env_id)
        await query.message.reply_text(
            "🗑 تم حذف المتغير. أعد تشغيل البوت ليأخذ التغيير." if removed else "⚠️ المتغير غير موجود.",
            reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)),
        )
        return
    elif action == "env_add":
        PENDING[query.from_user.id] = {"action": "env_assignment", "bot_id": bot_id}
        await query.message.reply_text(
            "🔐 <b>إضافة متغير بيئة للبوت</b>\n\n"
            "أرسل المتغير بهذا الشكل:\n"
            "<code>BOT_TOKEN=123456:ABCxyz</code>\n\n"
            "حيث <code>123456:ABCxyz</code> هو توكن البوت الذي حصلت عليه من @BotFather.\n"
            "💡 يمكنك لصق <b>عدة أسطر</b> دفعة واحدة، أو إرسال ملف <code>.env</code> مباشرة.",
            parse_mode=ParseMode.HTML
        )
        return
    elif action == "memory":
        PENDING[query.from_user.id] = {"action": "memory_assignment", "bot_id": bot_id}
        await query.message.reply_text(f"🧠 أرسل حد الذاكرة بالميغابايت بين 128 و{config.MAX_BOT_MEMORY_MB}، أو أرسل <code>default</code>.", parse_mode=ParseMode.HTML)
        return
    elif action == "usage":
        usage = manager().get_usage(bot_id)
        if not usage:
            await query.message.reply_text("📈 لا تتوفر بيانات موارد الآن؛ تأكد من أن البوت يعمل.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        else:
            disk_bytes = await asyncio.to_thread(dir_size_bytes, row["folder"])
            disk_text = f"\n💽 القرص: <b>{human_size(disk_bytes)}</b>" + (f" من {config.MAX_BOT_DISK_MB}MB" if config.MAX_BOT_DISK_MB else "")
            history = db.get_usage_history(bot_id, config.USAGE_HISTORY_MAX_POINTS)
            trend = ""
            if len(history) >= 3:
                limit_mb = row["max_memory_mb"] or config.MAX_BOT_MEMORY_MB
                trend = (
                    f"\n\n📊 <b>الاتجاه</b> ({len(history)} لقطة)\n"
                    f"🧠 CPU: <code>{sparkline([h['cpu'] for h in history])}</code>\n"
                    f"💾 RAM: <code>{sparkline([h['mem_mb'] for h in history], top=limit_mb)}</code> (من {limit_mb}MB)"
                )
            await query.message.reply_text(f"📈 <b>موارد البوت</b>\n\n🧠 CPU: <b>{usage['cpu']}</b>\n💾 الذاكرة: <b>{usage['mem']}</b>\n⏱ التشغيل: <b>{usage['uptime']}</b>{disk_text}{trend}", parse_mode=ParseMode.HTML, reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    elif action == "export":
        if not row["folder"] or not os.path.exists(row["folder"]):
            await query.message.reply_text("⚠️ ملفات البوت غير موجودة للتنزيل.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
            return
        try:
            zip_path = export_bot_zip(row["folder"], str(Path(row["folder"]).parent / f"bot_{bot_id}_export"))
            with open(zip_path, "rb") as f:
                await query.message.reply_document(
                    document=f,
                    caption=f"📦 <b>تصدير بوت {html.escape(row['name'])}</b>\n\nملف ZIP جاهز للتحميل.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)),
                )
            os.remove(zip_path)
        except Exception as exc:
            logger.exception("Export failed for bot %s", bot_id)
            await query.message.reply_text(f"❌ فشل تصدير البوت: {html.escape(str(exc)[:300])}.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return
    elif action == "delete_confirm":
        await query.message.reply_text("⚠️ هل أنت متأكد من حذف البوت وملفاته؟ لا يمكن التراجع عن هذا الإجراء.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("نعم، احذف", callback_data=f"bot:delete:{bot_id}"), InlineKeyboardButton("إلغاء", callback_data=f"bot:view:{bot_id}")]]))
        return
    elif action == "clone":
        # Clone bot: create a new bot with same files, env vars, and settings
        try:
            original_folder = row["folder"]
            new_bot_id = db.insert_bot_if_room(query.from_user.id, f"{row['name']} (نسخة)", db.get_max_bots(query.from_user.id))
            if new_bot_id is None:
                await query.message.reply_text("⚠️ وصلت إلى الحد المسموح من البوتات.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
                return
            new_folder = os.path.join(config.BOTS_DIR, str(query.from_user.id), f"bot_{new_bot_id}")
            if os.path.exists(new_folder):
                shutil.rmtree(new_folder, ignore_errors=True)
            shutil.copytree(original_folder, new_folder, ignore=shutil.ignore_patterns(".venv", "run.log", "__pycache__", "*.pyc"))
            entry_file = row["entry_file"]
            if entry_file and entry_file.startswith(original_folder):
                entry_file = entry_file.replace(original_folder, new_folder, 1)
            db.set_bot_files(new_bot_id, new_folder, entry_file or "")
            # Copy env vars
            for key, value in db.get_env_vars(bot_id).items():
                db.set_env_var(new_bot_id, key, value)
            # Copy settings
            if row["max_memory_mb"]:
                db.set_max_memory(new_bot_id, row["max_memory_mb"])
            if row["restart_interval_hours"]:
                db.set_restart_interval(new_bot_id, row["restart_interval_hours"])
            db.set_auto_restart(new_bot_id, bool(row["auto_restart"]))
            db.log_admin_action(query.from_user.id, "clone_bot", new_bot_id, f"from_bot={bot_id}")
            cloned_row = db.get_bot(new_bot_id)
            await query.message.reply_text(
                f"✅ <b>تم نسخ البوت بنجاح</b>\n\n"
                f"🤖 البوت الجديد: <b>{html.escape(cloned_row['name'])}</b>\n"
                f"🆔 المعرف: <code>{cloned_row['bot_id']}</code>\n\n"
                f"يمكنك الآن تشغيل البوت أو تحديثه حسب الحاجة.",
                parse_mode=ParseMode.HTML,
                reply_markup=bot_keyboard(new_bot_id, is_admin(query.from_user.id)),
            )
        except Exception as exc:
            logger.exception("Clone failed for bot %s", bot_id)
            await query.message.reply_text(f"❌ فشل نسخ البوت: {html.escape(str(exc)[:300])}.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
        return

    elif action == "update":
        # Update existing bot: re-upload ZIP to replace files
        if not row["folder"]:
            await query.message.reply_text("⚠️ لا يوجد ملفات للبوت الحالي لتحديثها.", reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))
            return
        # Stop the bot if running
        if manager().is_running(bot_id):
            manager().stop_bot(bot_id)
        # Keep the current files until the replacement archive passes all checks.
        # The old folder is removed only after the new folder is finalized successfully.
        # Set pending state for update
        PENDING[query.from_user.id] = {"action": "update_archive", "bot_id": bot_id, "bot_name": row["name"]}
        await query.message.reply_text(
            f"📦 <b>جاهز لتحديث بوت {html.escape(row['name'])}</b>\n\n"
            f"أرسل ملف ZIP الجديد. سيتم استبدال ملفات البوت القديمة.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard(),
        )
        return

    elif action == "delete":
        manager().stop_bot(bot_id)
        delete_folder(row["folder"])
        db.delete_bot_db(bot_id)
        await query.message.reply_text("🗑 تم حذف البوت وملفاته.", reply_markup=admin_keyboard() if is_admin(query.from_user.id) else main_keyboard())
        return
    else:
        return
    await query.message.reply_text(("✅ " if ok else "❌ ") + message, reply_markup=bot_keyboard(bot_id, is_admin(query.from_user.id)))


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/metrics":
            self._send_metrics()
        elif path in {"/", "/health", "/healthz"}:
            self._send_health()
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_health(self):
        """فحص صحي حقيقي: القاعدة تستجيب، والحارس ينبض. الحارس الميت يعني أن البوتات
        لم تعد مراقبة، فنُرجع 503 ليعيد Railway/Docker تشغيل الخدمة."""
        problems = []
        stats = {}
        try:
            stats = db.global_stats()
        except Exception as exc:
            problems.append(f"database: {type(exc).__name__}")
        mgr = MANAGER
        watchdog_age = None
        if mgr is None:
            problems.append("manager not initialized")
        elif mgr.boot_recovery_done:
            watchdog_age = round(time.time() - mgr.last_watchdog_beat, 1)
            if watchdog_age > max(120, config.WATCHDOG_INTERVAL * 6):
                problems.append(f"watchdog stalled for {watchdog_age}s")
        payload = {
            "status": "ok" if not problems else "unhealthy",
            "service": "telegram-bot-hosting",
            "uptime_seconds": round(time.time() - STARTED_AT, 2),
            "boot_recovery_done": bool(mgr and mgr.boot_recovery_done),
            "watchdog_age_seconds": watchdog_age,
            "problems": problems,
            **stats,
        }
        self._respond(200 if not problems else 503, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _send_metrics(self):
        stats = db.global_stats()
        mgr = manager()
        with mgr.lock:
            bot_ids = list(mgr.processes.keys())
        total_cpu = 0.0
        total_mem_bytes = 0
        for bot_id in bot_ids:
            usage = mgr.get_usage(bot_id, fast=True)
            if usage:
                total_cpu += usage["cpu_value"]
                total_mem_bytes += usage["mem_bytes"]
        total_mem_mb = total_mem_bytes / (1024 * 1024)
        host_mem = psutil.virtual_memory()
        try:
            disk = shutil.disk_usage(config.DB_PATH.parent)
            disk_free_mb, disk_total_mb = disk.free / 1048576, disk.total / 1048576
        except OSError:
            disk_free_mb = disk_total_mb = 0.0

        def metric(name: str, help_text: str, value) -> list[str]:
            return [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value}", ""]

        lines: list[str] = []
        lines += metric("hosting_total_users", "Total registered users", stats["total_users"])
        lines += metric("hosting_total_bots", "Total bots", stats["total_bots"])
        lines += metric("hosting_running_bots", "Currently running bots", stats["running"])
        lines += metric("hosting_crashed_bots", "Crashed bots", stats["crashed"])
        lines += metric("hosting_total_cpu_percent", "Total CPU usage across all bots", f"{total_cpu:.2f}")
        lines += metric("hosting_total_memory_mb", "Total memory usage across all bots (MB)", f"{total_mem_mb:.2f}")
        lines += metric("hosting_host_memory_used_percent", "Host memory used percent", f"{host_mem.percent:.1f}")
        lines += metric("hosting_data_disk_free_mb", "Free space on the data volume (MB)", f"{disk_free_mb:.0f}")
        lines += metric("hosting_data_disk_total_mb", "Size of the data volume (MB)", f"{disk_total_mb:.0f}")
        lines += metric("hosting_watchdog_age_seconds", "Seconds since the last watchdog beat", f"{time.time() - mgr.last_watchdog_beat:.1f}")
        lines += metric("hosting_uptime_seconds", "Service uptime in seconds", round(time.time() - STARTED_AT, 2))
        self._respond(200, "\n".join(lines).encode("utf-8"), "text/plain; version=0.0.4; charset=utf-8")

    def log_message(self, format, *args):
        return


def run_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", config.PORT), HealthHandler)
    logger.info("Health server listening on port %s", config.PORT)
    server.serve_forever()


async def initialize(application: Application) -> None:
    global MANAGER
    db.init_db()
    loop = asyncio.get_running_loop()

    def notify_owner(user_id: int, text: str) -> None:
        """يُستدعى من خيوط الحارس: يجدول إرسال الرسالة على حلقة asyncio الرئيسية."""
        future = asyncio.run_coroutine_threadsafe(application.bot.send_message(user_id, text), loop)
        future.add_done_callback(lambda f: f.exception() and logger.warning("Notify %s failed: %s", user_id, f.exception()))

    if MANAGER is None:
        MANAGER = ProcessManager()
    MANAGER.notifier = notify_owner
    Thread(target=run_health_server, daemon=True).start()
    # استرداد البوتات التي كانت تعمل قبل إعادة النشر ثم الحارس التلقائي
    Thread(target=MANAGER.run_background, args=(config.WATCHDOG_INTERVAL,), daemon=True, name="watchdog").start()
    backup_interval = int(os.getenv("BACKUP_INTERVAL_SECONDS", str(backup.BACKUP_INTERVAL_SECONDS)))
    Thread(target=backup.start_backup_scheduler, args=(backup_interval,), daemon=True).start()
    application.bot_data["initialized_at"] = time.time()


async def shutdown(application: Application) -> None:
    if MANAGER:
        # إيقاف البوتات قد يستغرق ثوانٍ: نُجريه في خيط حتى لا نحجب حلقة asyncio
        await asyncio.to_thread(MANAGER.shutdown_all)
    backup.stop_backup_scheduler()


def build_application() -> Application:
    application = ApplicationBuilder().token(config.HOST_BOT_TOKEN).post_init(initialize).post_shutdown(shutdown).build()
    application.add_handler(TypeHandler(Update, maintenance_guard), group=-2)
    application.add_handler(TypeHandler(Update, rate_limit_guard), group=-1)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("newbot", new_bot))
    application.add_handler(CommandHandler("mybots", my_bots))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("setenv", set_env))
    application.add_handler(CommandHandler("setmemory", set_memory))
    application.add_handler(CommandHandler("schedule", schedule_restart))
    application.add_handler(CommandHandler("admin", admin))
    application.add_handler(MessageHandler(filters.Document.ALL, document))
    application.add_handler(CallbackQueryHandler(callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))
    return application


def preflight() -> None:
    """فحوصات إقلاع: تفشل بوضوح عند خطأ قاتل وتحذّر عند إعداد خطِر، خاصة على Railway."""
    if not config.ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty: nobody can use /admin.")
    if not os.getenv("ENCRYPTION_KEY", "").strip():
        logger.warning("ENCRYPTION_KEY is not set; relying on a key stored in the data volume. Set it as a secret variable.")
    probe = config.DB_PATH.parent / ".write_test"
    try:
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        raise SystemExit(f"Data directory {config.DB_PATH.parent} is not writable: {exc}")
    if config.ON_RAILWAY:
        if not str(config.DB_PATH).startswith("/app/data"):
            logger.warning("DATABASE_PATH=%s is outside /app/data: data will be lost on redeploy unless a Volume is mounted there.", config.DB_PATH)
        if not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
            logger.warning("No Railway Volume detected: attach one at /app/data or all bots/DB vanish on every deploy.")
        if os.getenv("RAILWAY_DEPLOYMENT_DRAINING_SECONDS") is None:
            logger.warning("Set RAILWAY_DEPLOYMENT_DRAINING_SECONDS=15 so bots stop gracefully on redeploy.")


def main() -> None:
    if not config.HOST_BOT_TOKEN:
        raise SystemExit("HOST_BOT_TOKEN is required. Set it in the environment before starting the hosting bot.")
    preflight()
    db.init_db()
    global MANAGER
    MANAGER = ProcessManager()
    build_application().run_polling(drop_pending_updates=True, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
