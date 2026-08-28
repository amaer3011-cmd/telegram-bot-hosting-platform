"""
FastAPI Web Dashboard for Bot Hosting Platform
لوحة تحكم ويب لإدارة البوتات وعرض السجلات والإحصائيات
"""
import os
import json
import logging
from pathlib import Path
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, Request, Depends, HTTPException, status, Form, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import uvicorn

import config
import database as db
from docker_manager import get_docker_manager, is_docker_available
from utils import tail_file

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("web-dashboard")

app = FastAPI(
    title="Bot Hosting Dashboard",
    description="لوحة تحكم ويب لاستضافة بوتات تليجرام",
    version="1.0.0"
)

# إعداد القوالب والملفات الثابتة
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"
templates_dir.mkdir(exist_ok=True)
static_dir.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(templates_dir))
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# نظام مصادقة بسيط
security = HTTPBasic()


def get_current_admin(credentials: HTTPBasicCredentials = Depends(security)) -> int:
    """التحقق من صلاحيات المشرف"""
    # تحويل اسم المستخدم إلى user_id للتحقق
    try:
        user_id = int(credentials.username)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="اسم المستخدم يجب أن يكون user_id رقمي",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    if not db.is_admin(user_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="غير مصرح - يجب أن تكون مشرفًا",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    return user_id


@app.get("/", response_class=HTMLResponse)
async def dashboard_home(request: Request):
    """الصفحة الرئيسية للوحة التحكم"""
    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "title": "لوحة التحكم",
        "docker_available": is_docker_available(),
    })


@app.get("/api/stats")
async def get_platform_stats(admin_id: int = Depends(get_current_admin)):
    """الحصول على إحصائيات المنصة"""
    total_users = db.get_total_users_count()
    total_bots = db.get_total_bots_count()
    running_bots = len(db.get_bots_by_status("running"))
    stopped_bots = len(db.get_bots_by_status("stopped"))
    crashed_bots = len(db.get_bots_by_status("crashed"))
    
    return {
        "total_users": total_users,
        "total_bots": total_bots,
        "running_bots": running_bots,
        "stopped_bots": stopped_bots,
        "crashed_bots": crashed_bots,
        "docker_enabled": is_docker_available(),
    }


@app.get("/api/bots")
async def list_bots(admin_id: int = Depends(get_current_admin), status_filter: Optional[str] = None):
    """الحصول على قائمة البوتات"""
    if status_filter:
        bots = db.get_bots_by_status(status_filter)
    else:
        # الحصول على جميع البوتات مع معلومات المستخدمين
        bots = []
        all_bots = db._conn.execute("""
            SELECT b.*, u.username as owner_username 
            FROM bots b 
            LEFT JOIN users u ON b.owner_id = u.user_id 
            ORDER BY b.created_at DESC
        """).fetchall()
        bots = [dict(row) for row in all_bots]
    
    # إضافة حالة Docker إذا كانت متاحة
    docker_mgr = get_docker_manager() if is_docker_available() else None
    
    result = []
    for bot in bots:
        bot_info = dict(bot)
        if docker_mgr:
            container_stats = docker_mgr.get_container_stats(bot["bot_id"])
            bot_info["container_stats"] = container_stats
        result.append(bot_info)
    
    return {"bots": result}


@app.get("/api/bot/{bot_id}")
async def get_bot_details(bot_id: int, admin_id: int = Depends(get_current_admin)):
    """الحصول على تفاصيل بوت محدد"""
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    
    bot_info = dict(bot)
    
    # الحصول على متغيرات البيئة (بدون القيم المشفرة للأمان)
    env_vars = db.get_env_vars_keys(bot_id)
    bot_info["env_keys"] = list(env_vars.keys())
    
    # الحصول على الإحصائيات إذا كان يعمل
    if is_docker_available():
        docker_mgr = get_docker_manager()
        if docker_mgr.is_running(bot_id):
            bot_info["stats"] = docker_mgr.get_container_stats(bot_id)
    
    return bot_info


@app.post("/api/bot/{bot_id}/start")
async def start_bot(bot_id: int, admin_id: int = Depends(get_current_admin)):
    """تشغيل بوت"""
    from process_manager import ProcessManager
    
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    
    if is_docker_available():
        docker_mgr = get_docker_manager()
        success, msg = docker_mgr.start_container(bot)
    else:
        pm = ProcessManager()
        success, msg = pm.start_bot(bot)
    
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    
    return {"status": "success", "message": msg}


@app.post("/api/bot/{bot_id}/stop")
async def stop_bot(bot_id: int, admin_id: int = Depends(get_current_admin)):
    """إيقاف بوت"""
    from process_manager import ProcessManager
    
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    
    if is_docker_available():
        docker_mgr = get_docker_manager()
        success, msg = docker_mgr.stop_container(bot_id)
    else:
        pm = ProcessManager()
        success, msg = pm.stop_bot(bot_id)
    
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    
    return {"status": "success", "message": msg}


@app.post("/api/bot/{bot_id}/restart")
async def restart_bot(bot_id: int, admin_id: int = Depends(get_current_admin)):
    """إعادة تشغيل بوت"""
    from process_manager import ProcessManager
    
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    
    if is_docker_available():
        docker_mgr = get_docker_manager()
        success, msg = docker_mgr.restart_container(bot)
    else:
        pm = ProcessManager()
        success, msg = pm.restart_bot(bot)
    
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    
    return {"status": "success", "message": msg}


@app.get("/api/bot/{bot_id}/logs")
async def get_bot_logs(bot_id: int, lines: int = 100, admin_id: int = Depends(get_current_admin)):
    """الحصول على سجلات البوت"""
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    
    if is_docker_available():
        docker_mgr = get_docker_manager()
        logs = docker_mgr.get_container_logs(bot_id, lines)
    else:
        log_path = os.path.join(bot["folder"], "run.log")
        if os.path.exists(log_path):
            logs = tail_file(log_path, lines)
        else:
            logs = "لا توجد سجلات متاحة"
    
    return {"logs": logs}


@app.get("/api/bot/{bot_id}/stats")
async def get_bot_stats(bot_id: int, admin_id: int = Depends(get_current_admin)):
    """الحصول على إحصائيات البوت"""
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(status_code=404, detail="البوت غير موجود")
    
    stats = None
    if is_docker_available():
        docker_mgr = get_docker_manager()
        stats = docker_mgr.get_container_stats(bot_id)
    else:
        from process_manager import ProcessManager
        pm = ProcessManager()
        stats = pm.get_usage(bot_id)
    
    if not stats:
        raise HTTPException(status_code=400, detail="البوت غير يعمل أو لا توجد إحصائيات")
    
    return stats


@app.get("/api/users")
async def list_users(admin_id: int = Depends(get_current_admin)):
    """الحصول على قائمة المستخدمين"""
    users = db._conn.execute("SELECT * FROM users ORDER BY joined_at DESC").fetchall()
    return {"users": [dict(u) for u in users]}


@app.post("/api/user/{user_id}/ban")
async def ban_user(user_id: int, admin_id: int = Depends(get_current_admin)):
    """حظر مستخدم"""
    db.ban_user(user_id)
    db.log_audit(admin_id, "ban", f"user:{user_id}", "تم حظر المستخدم")
    return {"status": "success", "message": f"تم حظر المستخدم {user_id}"}


@app.post("/api/user/{user_id}/unban")
async def unban_user(user_id: int, admin_id: int = Depends(get_current_admin)):
    """فك الحظر عن مستخدم"""
    db.unban_user(user_id)
    db.log_audit(admin_id, "unban", f"user:{user_id}", "تم فك الحظر عن المستخدم")
    return {"status": "success", "message": f"تم فك الحظر عن المستخدم {user_id}"}


@app.get("/api/audit")
async def get_audit_log(admin_id: int = Depends(get_current_admin), limit: int = 100):
    """الحصول على سجل التدقيق"""
    entries = db._conn.execute("""
        SELECT a.*, u.username as admin_username 
        FROM audit_log a 
        LEFT JOIN users u ON a.admin_id = u.user_id 
        ORDER BY a.ts DESC 
        LIMIT ?
    """, (limit,)).fetchall()
    
    return {"entries": [dict(e) for e in entries]}


@app.get("/health")
async def health_check():
    """فحص صحة النظام"""
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "docker_available": is_docker_available(),
    }


def run_dashboard(host: str = "0.0.0.0", port: int = 8000, reload: bool = False):
    """تشغيل خادم لوحة التحكم"""
    uvicorn.run(app, host=host, port=port, reload=reload)


if __name__ == "__main__":
    run_dashboard()
