"""
Docker Container Manager for Bot Isolation
يُدير حاويات Docker ديناميكية لعزل بوتات العملاء بشكل آمن
"""
import os
import json
import time
import logging
import threading
import docker
from typing import Optional, Dict, Any, List
from pathlib import Path

import config
import database as db

logger = logging.getLogger(__name__)


class DockerContainerManager:
    """
    يُدير دورة حياة حاويات Docker للبوتات
    يوفر عزلًا كاملاً لكل بوت في بيئة منفصلة
    """
    
    def __init__(self):
        self.client: Optional[docker.DockerClient] = None
        self.api_client: Optional[docker.APIClient] = None
        self.containers: Dict[int, Any] = {}  # bot_id -> container
        self.lock = threading.Lock()
        self._initialized = False
        self._initialize()
    
    def _initialize(self):
        """تهيئة عميل Docker"""
        try:
            self.client = docker.from_env()
            self.api_client = docker.APIClient()
            self._initialized = True
            logger.info("✅ Docker client initialized successfully")
        except Exception as e:
            logger.error(f"❌ Failed to initialize Docker client: {e}")
            self._initialized = False
    
    def is_available(self) -> bool:
        """التحقق من توفر Docker"""
        return self._initialized
    
    def build_bot_image(self, bot_id: int, folder: str, entry_file: str) -> tuple[bool, str]:
        """
        بناء صورة Docker مخصصة للبوت
        
        Args:
            bot_id: معرف البوت
            folder: مسار مجلد البوت
            entry_file: ملف الدخول الرئيسي
            
        Returns:
            (success, message)
        """
        if not self._initialized:
            return False, "Docker غير متوفر"
        
        try:
            folder_path = Path(folder).resolve()
            if not folder_path.exists():
                return False, f"مجلد البوت غير موجود: {folder}"
            
            # إنشاء Dockerfile ديناميكي
            dockerfile_content = self._generate_dockerfile(folder_path, entry_file)
            dockerfile_path = folder_path / "Dockerfile.generated"
            
            with open(dockerfile_path, 'w', encoding='utf-8') as f:
                f.write(dockerfile_content)
            
            # تحليل المتطلبات لتثبيت الحزم اللازمة
            requirements_path = folder_path / "requirements.txt"
            has_requirements = requirements_path.exists()
            
            # بناء الصورة
            image_tag = f"bot-{bot_id}:latest"
            logger.info(f"Building Docker image {image_tag} for bot {bot_id}")
            
            build_logs = []
            try:
                image, build_logs = self.client.images.build(
                    path=str(folder_path),
                    tag=image_tag,
                    dockerfile="Dockerfile.generated",
                    rm=True,
                    quiet=False,
                    pull=False,
                    timeout=300  # 5 دقائق كحد أقصى للبناء
                )
                logger.info(f"✅ Image built successfully: {image_tag}")
                return True, f"تم بناء الصورة بنجاح: {image_tag}"
                
            except Exception as build_error:
                error_msg = f"فشل بناء الصورة: {build_error}"
                logger.error(error_msg)
                # تسجيل تفاصيل أخطاء البناء
                for log in build_logs:
                    if 'stream' in log:
                        logger.error(f"Build log: {log['stream'].strip()}")
                return False, error_msg
                
        except Exception as e:
            logger.error(f"Error building image for bot {bot_id}: {e}")
            return False, f"خطأ في بناء الصورة: {str(e)}"
    
    def _generate_dockerfile(self, folder: Path, entry_file: str) -> str:
        """
        توليد Dockerfile ديناميكي بناءً على تحليل البوت
        
        Args:
            folder: مسار مجلد البوت
            entry_file: اسم ملف الدخول
            
        Returns:
            محتوى Dockerfile كنص
        """
        # التحقق من وجود requirements.txt
        requirements_path = folder / "requirements.txt"
        has_requirements = requirements_path.exists()
        
        # محاولة اكتشاف نوع البوت لتحديد الصورة الأساسية المناسبة
        base_image = "python:3.12-slim"
        
        # قراءة ملف الدخول لمعرفة المكتبات المستخدمة
        entry_path = folder / entry_file
        if entry_path.exists():
            try:
                with open(entry_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    
                    # اكتشاف مكتبات التليجرام
                    if 'aiogram' in content:
                        base_image = "python:3.12-slim"
                    elif 'pyrogram' in content:
                        base_image = "python:3.12-slim"
                    elif 'telethon' in content:
                        base_image = "python:3.12-slim"
                        
            except Exception as e:
                logger.warning(f"Could not analyze entry file: {e}")
        
        # توليد محتوى Dockerfile
        dockerfile_lines = [
            f"# Auto-generated Dockerfile for bot",
            f"FROM {base_image}",
            "",
            "# تعيين متغيرات البيئة",
            "ENV PYTHONUNBUFFERED=1",
            "ENV PYTHONDONTWRITEBYTECODE=1",
            "ENV PIP_NO_CACHE_DIR=1",
            "ENV PIP_DISABLE_PIP_VERSION_CHECK=1",
            "",
            "# تثبيت الحزم المطلوبة",
            "WORKDIR /app",
            "",
        ]
        
        if has_requirements:
            dockerfile_lines.extend([
                "# نسخ وتثبيت المتطلبات",
                "COPY requirements.txt .",
                "RUN pip install --no-cache-dir -r requirements.txt",
                "",
            ])
        
        dockerfile_lines.extend([
            "# نسخ كود البوت",
            "COPY . .",
            "",
            "# إنشاء مستخدم غير root للأمان",
            "RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app",
            "USER botuser",
            "",
            "# أمر التشغيل",
            f'CMD ["python", "{entry_file}"]',
        ])
        
        return "\n".join(dockerfile_lines)
    
    def start_container(self, bot_row: dict) -> tuple[bool, str]:
        """
        تشغيل حاوية Docker للبوت
        
        Args:
            bot_row: صف البوت من قاعدة البيانات
            
        Returns:
            (success, message)
        """
        if not self._initialized:
            return False, "Docker غير متوفر"
        
        bot_id = bot_row["bot_id"]
        folder = bot_row["folder"]
        entry_file = bot_row["entry_file"]
        
        with self.lock:
            # التحقق مما إذا كانت الحاوية تعمل بالفعل
            if bot_id in self.containers:
                existing = self.containers[bot_id]
                try:
                    if existing.status == 'running':
                        return False, "البوت يعمل بالفعل في حاوية"
                except:
                    pass
            
            try:
                # الحصول على متغيرات البيئة المشفرة
                env_vars = db.get_env_vars(bot_id)
                
                # إضافة متغيرات البيئة الآمنة
                env_list = [
                    "PYTHONUNBUFFERED=1",
                    "PYTHONDONTWRITEBYTECODE=1",
                ]
                
                # دمج متغيرات بيئة المستخدم
                for key, value in env_vars.items():
                    if key not in config.PROTECTED_ENV_KEYS:
                        env_list.append(f"{key}={value}")
                
                # تحديد حدود الموارد
                memory_limit = bot_row.get("max_memory_mb") or config.MAX_BOT_MEMORY_MB
                cpu_limit = config.MAX_BOT_CPU_SECONDS
                
                # اسم الصورة
                image_tag = f"bot-{bot_id}:latest"
                
                # التحقق من وجود الصورة، وبنائها إذا لم تكن موجودة
                try:
                    self.client.images.get(image_tag)
                except:
                    logger.info(f"Image {image_tag} not found, building...")
                    success, msg = self.build_bot_image(bot_id, folder, entry_file)
                    if not success:
                        return False, msg
                
                # إعداد قيود الموارد
                mem_limit = f"{memory_limit}m"
                
                # إعداد mounts للوصول للمجلدات الضرورية فقط
                mounts = []
                
                # Mount للسجلات
                log_path = os.path.join(folder, "run.log")
                mounts.append({
                    'type': 'bind',
                    'source': log_path,
                    'target': '/app/run.log',
                    'read_only': False
                })
                
                logger.info(f"Starting container for bot {bot_id} with image {image_tag}")
                
                # تشغيل الحاوية
                container = self.client.containers.run(
                    image=image_tag,
                    name=f"bot-{bot_id}",
                    environment=env_list,
                    detach=True,
                    auto_remove=False,
                    mem_limit=mem_limit,
                    nano_cpus=int(cpu_limit * 1e9 / 3600) if cpu_limit else None,  # تحويل لـ nano CPUs
                    network_mode="bridge",
                    mounts=mounts,
                    restart_policy={"Name": "on-failure", "MaximumRetryCount": 3},
                    labels={
                        "bot.id": str(bot_id),
                        "bot.owner": str(bot_row["owner_id"]),
                        "managed.by": "telegram-hosting-bot"
                    }
                )
                
                # تخزين مرجع الحاوية
                self.containers[bot_id] = container
                
                # تحديث قاعدة البيانات
                db.update_status(bot_id, "running")
                db.update_pid(bot_id, container.attrs['State']['Pid'])
                db.set_last_error(bot_id, None)
                db.update_last_started(bot_id)
                
                logger.info(f"✅ Container started for bot {bot_id}: {container.short_id}")
                return True, "تم تشغيل البوت بنجاح في حاوية معزولة ✅"
                
            except docker.errors.ImageNotFound as e:
                logger.error(f"Image not found for bot {bot_id}: {e}")
                return False, f"الصورة غير موجودة: {str(e)}"
                
            except docker.errors.APIError as e:
                logger.error(f"Docker API error for bot {bot_id}: {e}")
                return False, f"خطأ في Docker API: {str(e)}"
                
            except Exception as e:
                logger.error(f"Error starting container for bot {bot_id}: {e}")
                return False, f"فشل تشغيل الحاوية: {str(e)}"
    
    def stop_container(self, bot_id: int, mark_stopped: bool = True) -> tuple[bool, str]:
        """
        إيقاف حاوية البوت
        
        Args:
            bot_id: معرف البوت
            mark_stopped: هل يجب تحديث الحالة في قاعدة البيانات
            
        Returns:
            (success, message)
        """
        if not self._initialized:
            return False, "Docker غير متوفر"
        
        with self.lock:
            container = self.containers.get(bot_id)
            
            if container is None:
                # محاولة العثور على الحاوية بالاسم
                try:
                    container = self.client.containers.get(f"bot-{bot_id}")
                except:
                    if mark_stopped:
                        db.update_status(bot_id, "stopped")
                    return True, "لا توجد حاوية نشطة"
            
            try:
                logger.info(f"Stopping container for bot {bot_id}")
                container.stop(timeout=10)
                container.remove(force=False)
                
                self.containers.pop(bot_id, None)
                
                if mark_stopped:
                    db.update_status(bot_id, "stopped")
                    db.update_pid(bot_id, None)
                
                logger.info(f"✅ Container stopped for bot {bot_id}")
                return True, "تم إيقاف البوت ⏹"
                
            except Exception as e:
                logger.error(f"Error stopping container for bot {bot_id}: {e}")
                return False, f"فشل إيقاف الحاوية: {str(e)}"
    
    def restart_container(self, bot_row: dict) -> tuple[bool, str]:
        """
        إعادة تشغيل حاوية البوت
        
        Args:
            bot_row: صف البوت من قاعدة البيانات
            
        Returns:
            (success, message)
        """
        bot_id = bot_row["bot_id"]
        self.stop_container(bot_id, mark_stopped=False)
        time.sleep(2)
        return self.start_container(bot_row)
    
    def is_running(self, bot_id: int) -> bool:
        """
        التحقق مما إذا كانت الحاوية تعمل
        
        Args:
            bot_id: معرف البوت
            
        Returns:
            bool: حالة التشغيل
        """
        if not self._initialized:
            return False
        
        with self.lock:
            container = self.containers.get(bot_id)
            
            if container is None:
                try:
                    container = self.client.containers.get(f"bot-{bot_id}")
                    self.containers[bot_id] = container
                except:
                    return False
            
            try:
                container.reload()
                return container.status == 'running'
            except:
                return False
    
    def get_container_logs(self, bot_id: int, lines: int = 100) -> str:
        """
        الحصول على سجلات الحاوية
        
        Args:
            bot_id: معرف البوت
            lines: عدد الأسطر المطلوبة
            
        Returns:
            نص السجلات
        """
        if not self._initialized:
            return "Docker غير متوفر"
        
        try:
            container = self.containers.get(bot_id)
            
            if container is None:
                try:
                    container = self.client.containers.get(f"bot-{bot_id}")
                except:
                    return "لا توجد حاوية نشطة"
            
            logs = container.logs(tail=lines, timestamps=True)
            return logs.decode('utf-8', errors='replace')
            
        except Exception as e:
            logger.error(f"Error getting logs for bot {bot_id}: {e}")
            return f"خطأ في قراءة السجلات: {str(e)}"
    
    def get_container_stats(self, bot_id: int) -> Optional[Dict[str, Any]]:
        """
        الحصول على إحصائيات استخدام الموارد للحاوية
        
        Args:
            bot_id: معرف البوت
            
        Returns:
            dict مع CPU, RAM, Uptime أو None
        """
        if not self._initialized:
            return None
        
        try:
            container = self.containers.get(bot_id)
            
            if container is None:
                try:
                    container = self.client.containers.get(f"bot-{bot_id}")
                except:
                    return None
            
            # الحصول على الإحصائيات
            stats = container.stats(stream=False)
            
            # حساب استخدام CPU
            cpu_delta = stats['cpu_stats']['cpu_usage']['total_usage'] - \
                       stats['precpu_stats']['cpu_usage']['total_usage']
            system_delta = stats['cpu_stats']['system_cpu_usage'] - \
                          stats['precpu_stats']['system_cpu_usage']
            
            if system_delta > 0:
                cpu_percent = (cpu_delta / system_delta) * 100.0
            else:
                cpu_percent = 0.0
            
            # حساب استخدام الذاكرة
            memory_usage = stats['memory_stats'].get('usage', 0)
            memory_limit = stats['memory_stats'].get('limit', 0)
            
            # حساب وقت التشغيل
            started_at = container.attrs['State'].get('StartedAt', '')
            if started_at:
                from datetime import datetime
                try:
                    start_time = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                    uptime = (datetime.now(start_time.tzinfo) - start_time).total_seconds()
                except:
                    uptime = 0
            else:
                uptime = 0
            
            return {
                "cpu": f"{cpu_percent:.1f}%",
                "mem": self._human_size(memory_usage),
                "uptime": self._human_uptime(uptime),
            }
            
        except Exception as e:
            logger.error(f"Error getting stats for bot {bot_id}: {e}")
            return None
    
    def _human_size(self, bytes_val: int) -> str:
        """تنسيق حجم الذاكرة بشكل مقروء"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if abs(bytes_val) < 1024.0:
                return f"{bytes_val:.1f}{unit}"
            bytes_val /= 1024.0
        return f"{bytes_val:.1f}TB"
    
    def _human_uptime(self, seconds: float) -> str:
        """تنسيق وقت التشغيل بشكل مقروء"""
        if seconds < 60:
            return f"{int(seconds)}ث"
        elif seconds < 3600:
            return f"{int(seconds // 60)}د {int(seconds % 60)}ث"
        elif seconds < 86400:
            return f"{int(seconds // 3600)}س {int((seconds % 3600) // 60)}د"
        else:
            return f"{int(seconds // 86400)}ي {int((seconds % 86400) // 3600)}س"
    
    def cleanup_all(self):
        """تنظيف جميع الحاويات عند إيقاف النظام"""
        if not self._initialized:
            return
        
        logger.info("Cleaning up all bot containers...")
        with self.lock:
            for bot_id in list(self.containers.keys()):
                try:
                    self.stop_container(bot_id, mark_stopped=True)
                except Exception as e:
                    logger.error(f"Error cleaning up container {bot_id}: {e}")
    
    def get_all_containers(self) -> List[Dict[str, Any]]:
        """
        الحصول على قائمة بجميع الحاويات المُدارة
        
        Returns:
            list من dicts مع معلومات الحاويات
        """
        if not self._initialized:
            return []
        
        containers_info = []
        try:
            # البحث عن جميع الحاويات المُدارة بواسطة النظام
            all_containers = self.client.containers.list(
                filters={'label': 'managed.by=telegram-hosting-bot'},
                all=True
            )
            
            for container in all_containers:
                try:
                    bot_id = int(container.labels.get('bot.id', 0))
                    owner_id = int(container.labels.get('bot.owner', 0))
                    
                    containers_info.append({
                        'bot_id': bot_id,
                        'container_id': container.short_id,
                        'status': container.status,
                        'owner_id': owner_id,
                        'created': container.attrs['Created'],
                    })
                except Exception as e:
                    logger.warning(f"Error processing container {container.short_id}: {e}")
                    
        except Exception as e:
            logger.error(f"Error listing containers: {e}")
        
        return containers_info


# singleton instance
_docker_manager: Optional[DockerContainerManager] = None


def get_docker_manager() -> DockerContainerManager:
    """الحصول على مثانة DockerContainerManager"""
    global _docker_manager
    if _docker_manager is None:
        _docker_manager = DockerContainerManager()
    return _docker_manager


def is_docker_available() -> bool:
    """التحقق من توفر Docker"""
    manager = get_docker_manager()
    return manager.is_available()
