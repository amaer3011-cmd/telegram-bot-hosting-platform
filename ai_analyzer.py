"""
AI Bot Analyzer Module for Telegram Bot Hosting Platform

This module provides AI-powered analysis of uploaded bots to:
1. Detect required dependencies from code analysis
2. Identify environment variables needed
3. Suggest optimal configuration settings
4. Detect potential security issues
5. Provide recommendations for improvements

Uses pattern matching and AST analysis for static code analysis.
"""

from __future__ import annotations

import ast
import os
import re
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field

logger = logging.getLogger("ai-analyzer")


@dataclass
class AnalysisResult:
    """نتيجة تحليل البوت"""
    
    # المتغيرات البيئية المكتشفة
    env_vars: Dict[str, str] = field(default_factory=dict)
    
    # المكتبات المطلوبة
    required_packages: Set[str] = field(default_factory=set)
    
    # نوع البوت المكتشف
    bot_type: str = "unknown"
    
    # ملف الدخول الرئيسي
    entry_file: Optional[str] = None
    
    # توصيات التحسين
    recommendations: List[str] = field(default_factory=list)
    
    # تحذيرات أمنية
    security_warnings: List[str] = field(default_factory=list)
    
    # إعدادات مقترحة
    suggested_memory_mb: int = 512
    suggested_cpu_seconds: int = 3600
    
    # هل التحليل ناجح
    success: bool = True
    
    # رسالة الخطأ إن وجدت
    error_message: Optional[str] = None


class BotAnalyzer:
    """محلل البوتات بالذكاء الاصطناعي"""
    
    # أنماط شائعة لمتغيرات البيئة
    ENV_PATTERNS = {
        # Telegram
        r'BOT_TOKEN|TELEGRAM_TOKEN|TG_BOT_TOKEN': 'BOT_TOKEN',
        r'API_ID|TELEGRAM_API_ID': 'TELEGRAM_API_ID',
        r'API_HASH|TELEGRAM_API_HASH': 'TELEGRAM_API_HASH',
        
        # Database
        r'DATABASE_URL|DB_URL|MONGO_URI|POSTGRES_URL': 'DATABASE_URL',
        r'REDIS_URL|REDIS_HOST': 'REDIS_URL',
        
        # API Keys
        r'OPENAI_API_KEY|GPT_KEY|AI_KEY': 'OPENAI_API_KEY',
        r'STRIPE_KEY|PAYMENT_KEY': 'STRIPE_KEY',
        r'GOOGLE_API_KEY|GCP_KEY': 'GOOGLE_API_KEY',
        
        # General
        r'SECRET_KEY|JWT_SECRET': 'SECRET_KEY',
        r'WEBHOOK_URL|WEBHOOK_SECRET': 'WEBHOOK_URL',
    }
    
    # مكتبات شائعة واستيراداتها
    PACKAGE_IMPORTS = {
        'python-telegram-bot': ['telegram', 'telegram.ext'],
        'pyrogram': ['pyrogram'],
        'telethon': ['telethon'],
        'aiogram': ['aiogram'],
        'discord.py': ['discord'],
        'pymongo': ['pymongo', 'motor'],
        'psycopg2': ['psycopg2'],
        'redis': ['redis'],
        'requests': ['requests'],
        'aiohttp': ['aiohttp'],
        'flask': ['flask'],
        'fastapi': ['fastapi'],
        'django': ['django'],
        'sqlalchemy': ['sqlalchemy'],
        'openai': ['openai'],
        'google-generativeai': ['google.generativeai'],
        'pillow': ['PIL', 'Image'],
        'numpy': ['numpy'],
        'pandas': ['pandas'],
    }
    
    def __init__(self):
        self.compiled_env_patterns = {
            re.compile(pattern, re.IGNORECASE): var_name 
            for pattern, var_name in self.ENV_PATTERNS.items()
        }
    
    def analyze_folder(self, folder_path: str) -> AnalysisResult:
        """تحليل مجلد البوت بالكامل"""
        result = AnalysisResult()
        
        try:
            folder = Path(folder_path)
            if not folder.exists():
                result.success = False
                result.error_message = f"المجلد غير موجود: {folder_path}"
                return result
            
            # جمع كل ملفات Python
            python_files = list(folder.rglob("*.py"))
            
            if not python_files:
                result.success = False
                result.error_message = "لم يتم العثور على ملفات Python"
                return result
            
            # تحليل كل ملف
            all_imports: Set[str] = set()
            all_env_vars: Set[str] = set()
            code_lines: List[str] = []
            
            for py_file in python_files:
                file_result = self._analyze_file(py_file)
                all_imports.update(file_result['imports'])
                all_env_vars.update(file_result['env_vars'])
                code_lines.extend(file_result['lines'])
                
                # تحديد ملف الدخول المحتمل
                if py_file.name in ['main.py', 'bot.py', 'app.py', 'index.py']:
                    result.entry_file = str(py_file)
            
            # إذا لم نجد ملف دخول واضح، نأخذ أول ملف
            if not result.entry_file and python_files:
                result.entry_file = str(python_files[0])
            
            # تحويل الاستيرادات إلى حزم
            result.required_packages = self._map_imports_to_packages(all_imports)
            
            # اكتشاف متغيرات البيئة
            result.env_vars = self._resolve_env_vars(all_env_vars)
            
            # تحديد نوع البوت
            result.bot_type = self._detect_bot_type(all_imports, code_lines)
            
            # توليد التوصيات
            result.recommendations = self._generate_recommendations(
                result.bot_type, 
                result.required_packages,
                code_lines
            )
            
            # فحص أمني
            result.security_warnings = self._security_check(code_lines)
            
            # اقتراح الموارد
            result.suggested_memory_mb = self._suggest_memory(result.required_packages)
            
            logger.info(f"تم تحليل البوت بنجاح: {len(python_files)} ملفات، "
                       f"{len(result.required_packages)} حزمة، "
                       f"{len(result.env_vars)} متغير بيئة")
            
        except Exception as e:
            logger.exception(f"فشل تحليل البوت: {e}")
            result.success = False
            result.error_message = str(e)
        
        return result
    
    def _analyze_file(self, file_path: Path) -> Dict[str, Any]:
        """تحليل ملف Python واحد"""
        imports: Set[str] = set()
        env_vars: Set[str] = set()
        lines: List[str] = []
        
        try:
            content = file_path.read_text(encoding='utf-8')
            lines = content.split('\n')
            
            # تحليل AST للاستيرادات
            try:
                tree = ast.parse(content)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports.add(alias.name.split('.')[0])
                    elif isinstance(node, ast.ImportFrom):
                        if node.module:
                            imports.add(node.module.split('.')[0])
            except SyntaxError:
                # إذا فشل تحليل AST، نستخدم regex كبديل
                imports.update(self._extract_imports_regex(content))
            
            # استخراج متغيرات البيئة من os.getenv و os.environ
            env_vars.update(self._extract_env_vars(content))
            
        except Exception as e:
            logger.warning(f"فشل تحليل الملف {file_path}: {e}")
        
        return {
            'imports': imports,
            'env_vars': env_vars,
            'lines': lines
        }
    
    def _extract_imports_regex(self, content: str) -> Set[str]:
        """استخراج الاستيرادات باستخدام regex عند فشل AST"""
        imports = set()
        
        # import xxx
        for match in re.finditer(r'^import\s+([\w\.]+)', content, re.MULTILINE):
            imports.add(match.group(1).split('.')[0])
        
        # from xxx import
        for match in re.finditer(r'^from\s+([\w\.]+)\s+import', content, re.MULTILINE):
            imports.add(match.group(1).split('.')[0])
        
        return imports
    
    def _extract_env_vars(self, content: str) -> Set[str]:
        """استخراج متغيرات البيئة من الكود"""
        env_vars = set()
        
        # os.getenv('XXX'), os.environ.get('XXX')
        patterns = [
            r"os\.(?:getenv|environ\.get)\(['\"]([^'\"]+)['\"]",
            r"os\.(?:getenv|environ\.get)\([\"']([^\"']+)[\"']",
            r"getenv\(['\"]([^'\"]+)['\"]",
            r"config\.get\(['\"]([^'\"]+)['\"]",
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, content):
                env_vars.add(match.group(1))
        
        # البحث عن الأنماط المعروفة
        for pattern, var_name in self.compiled_env_patterns.items():
            if pattern.search(content):
                env_vars.add(var_name)
        
        return env_vars
    
    def _map_imports_to_packages(self, imports: Set[str]) -> Set[str]:
        """تحويل أسماء الاستيرادات إلى أسماء الحزم في requirements.txt"""
        packages = set()
        
        # عكس الخريطة للبحث السريع
        import_to_package = {}
        for pkg, import_list in self.PACKAGE_IMPORTS.items():
            for imp in import_list:
                import_to_package[imp.split('.')[0]] = pkg
        
        for imp in imports:
            if imp in import_to_package:
                packages.add(import_to_package[imp])
            elif imp in ['os', 'sys', 'time', 'json', 're', 'asyncio', 
                        'threading', 'multiprocessing', 'pathlib', 'typing',
                        'collections', 'functools', 'itertools', 'logging',
                        'datetime', 'hashlib', 'base64', 'pickle', 'shutil',
                        'subprocess', 'socket', 'http', 'urllib', 'contextlib']:
                # مكتبات قياسية لا تحتاج إضافة
                continue
            else:
                # إضافة كما هي كحزمة محتملة
                packages.add(imp.lower())
        
        return packages
    
    def _resolve_env_vars(self, detected_vars: Set[str]) -> Dict[str, str]:
        """تحديد متغيرات البيئة المطلوبة مع قيم افتراضية مقترحة"""
        resolved = {}
        
        for var in detected_vars:
            # البحث عن تطابق في الأنماط المعروفة
            matched = False
            for pattern, canonical_name in self.compiled_env_patterns.items():
                if pattern.search(var):
                    resolved[canonical_name] = ""  # قيمة فارغة ليملؤها المستخدم
                    matched = True
                    break
            
            if not matched:
                # إضافة كما هي
                resolved[var] = ""
        
        return resolved
    
    def _detect_bot_type(self, imports: Set[str], code_lines: List[str]) -> str:
        """تحديد نوع البوت من الاستيرادات والكود"""
        code_text = '\n'.join(code_lines)
        
        if 'telegram' in imports or 'telegram.ext' in imports:
            if 'asyncio' in imports or 'async ' in code_text:
                return "telegram-async"
            return "telegram-sync"
        
        if 'pyrogram' in imports:
            return "pyrogram"
        
        if 'telethon' in imports:
            return "telethon"
        
        if 'aiogram' in imports:
            return "aiogram"
        
        if 'discord' in imports:
            return "discord"
        
        if 'flask' in imports or 'fastapi' in imports:
            return "webhook"
        
        return "generic-python"
    
    def _generate_recommendations(
        self, 
        bot_type: str,
        packages: Set[str],
        code_lines: List[str]
    ) -> List[str]:
        """توليد توصيات لتحسين البوت"""
        recommendations = []
        code_text = '\n'.join(code_lines)
        
        # توصيات عامة
        if 'requirements.txt' not in code_text and packages:
            recommendations.append(
                f"📦 يُنصح بإنشاء ملف requirements.txt يحتوي على: "
                f"{', '.join(sorted(packages)[:10])}"
            )
        
        # توصيات حسب النوع
        if bot_type.startswith('telegram'):
            recommendations.append(
                "💡 استخدم ConversationHandler لإدارة المحادثات المعقدة"
            )
        
        if 'logging' not in code_text:
            recommendations.append(
                "📝 أضف نظام تسجيل (logging) لتتبع الأخطاء والأداء"
            )
        
        if 'try' not in code_text or 'except' not in code_text:
            recommendations.append(
                "⚠️ أضف معالجة للأخطاء (try/except) لتحسين الاستقرار"
            )
        
        if bot_type == 'telegram-sync' and 'asyncio' not in code_text:
            recommendations.append(
                "🚀 فكّر في استخدام النسخة غير المتزامنة (async) لأداء أفضل"
            )
        
        if len(packages) > 20:
            recommendations.append(
                "📦 عدد الحزم كبير ({})، راجع ما هو ضروري فقط".format(len(packages))
            )
        
        return recommendations
    
    def _security_check(self, code_lines: List[str]) -> List[str]:
        """فحص أمني للكود"""
        warnings = []
        code_text = '\n'.join(code_lines)
        
        # كلمات مرور صريحة
        if re.search(r'password\s*=\s*[\'"][^\'"]+[\'"]', code_text, re.IGNORECASE):
            warnings.append("🔐 تم العثور على كلمة مرور صريحة في الكود")
        
        # توكنات صريحة
        if re.search(r'token\s*=\s*[\'"][A-Za-z0-9:_-]{20,}[\'"]', code_text, re.IGNORECASE):
            warnings.append("🔐 تم العثور على توكن صريح في الكود")
        
        # eval خطير
        if 'eval(' in code_text:
            warnings.append("⚠️ استخدام eval() قد يكون خطرًا أمنيًا")
        
        # exec خطير
        if 'exec(' in code_text:
            warnings.append("⚠️ استخدام exec() قد يكون خطرًا أمنيًا")
        
        # طلبات HTTP بدون تحقق
        if 'requests.get' in code_text or 'aiohttp.get' in code_text:
            if 'verify=False' in code_text:
                warnings.append("🔒 تم تعطيل التحقق من SSL في الطلبات HTTP")
        
        return warnings
    
    def _suggest_memory(self, packages: Set[str]) -> int:
        """اقتراح حد الذاكرة بناءً على الحزم المستخدمة"""
        base_memory = 256  # أساسي
        
        # حزم ثقيلة
        heavy_packages = {
            'tensorflow': 512,
            'pytorch': 512,
            'opencv-python': 256,
            'pillow': 128,
            'numpy': 128,
            'pandas': 256,
            'scikit-learn': 256,
        }
        
        extra = 0
        for pkg in packages:
            if pkg in heavy_packages:
                extra += heavy_packages[pkg]
        
        # الحد الأقصى المقترح
        suggested = min(base_memory + extra, 1024)
        
        return suggested


# دالة مساعدة للتحليل السريع
def analyze_bot(folder_path: str) -> AnalysisResult:
    """دالة مساعدة لتحليل بوت بسرعة"""
    analyzer = BotAnalyzer()
    return analyzer.analyze_folder(folder_path)
