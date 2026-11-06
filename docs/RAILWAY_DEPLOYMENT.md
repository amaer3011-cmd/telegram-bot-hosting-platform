# نشر بوت الاستضافة على Railway

## المتطلبات

أنشئ خدمة جديدة من مستودع GitHub، واترك Railway يقرأ `railway.json` من جذر المستودع (يشير إلى `infra/docker/Dockerfile`). لا تضع Start Command مخصصًا في إعدادات الخدمة؛ لأن Railway يستخدم ENTRYPOINT وCMD الموجودين في Dockerfile عندما يكون Start Command فارغًا.

## المتغيرات السرية

أضف هذه القيم من تبويب Variables في Railway، ولا تضعها داخل GitHub:

| المتغير | القيمة |
|---|---|
| `HOST_BOT_TOKEN` | توكن بوت التحكم الذي أنشأته من BotFather. |
| `ADMIN_IDS` | أرقام حسابات Telegram المالكة مفصولة بفواصل. |
| `ENCRYPTION_KEY` | مفتاح تشفير متغيرات البوتات. ولّده مرة واحدة واحتفظ بنسخة منه خارج Railway: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` | `15` — يمنح البوتات وقتًا لتُغلق بهدوء عند إعادة النشر (الافتراضي صفر). |

يمكن ترك بقية المتغيرات على قيمها الافتراضية. Railway يحقن `PORT` تلقائيًا، والتطبيق يستمع إليه. لا تنسخ توكنات البوتات المستضافة إلى ملفات المشروع؛ تُحفظ من خلال زر البيئة داخل Telegram.

## Volume الدائم

أضف **Volume واحدًا فقط** إلى هذه الخدمة، واجعل Mount Path:

```text
/app/data
```

يحفظ التطبيق قاعدة البيانات وملفات البوتات داخل هذا المسار، وتستخدم الإعدادات التالية تلقائيًا:

```text
DATABASE_PATH=/app/data/hosting.db
BOTS_DIR=/app/data/uploaded_bots
```

لا تضف Volumes منفصلة لـ`/app/uploaded_bots` أو `/app/logs`؛ Railway يسمح بVolume واحد لكل خدمة، وكل هذه البيانات تقع الآن تحت `/app/data`.

## Healthcheck

في إعدادات الخدمة ضع:

```text
Healthcheck Path: /health
Healthcheck Timeout: 300
```

يعيد التطبيق HTTP 200 من `/health` و`/healthz` بعد بدء خادم الصحة، و503 إذا توقفت القاعدة أو الحارس. Railway يستخدمه فحص جاهزية أثناء النشر فقط؛ للمراقبة المستمرة اربط `/metrics` بأداة خارجية.

## إعدادات التشغيل

استخدم Replica واحدة فقط لأن بوت التحكم يعمل بـlong polling ولأن Railway لا يسمح باستخدام replicas مع Volume. اترك Restart Policy على `ON_FAILURE` مع عدد محاولات مناسب، واترك `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` بقيمة قصيرة مثل 10 إذا أردت منح العملية وقتًا للإغلاق الهادئ.

## ماذا يحدث عند إعادة النشر؟

تُرسَل SIGTERM فتُوقَف البوتات المستضافة معًا، وتبقى حالتها «تعمل» في القاعدة، ثم تُعاد تلقائيًا بعد الإقلاع (بفاصل `BOOT_RECOVERY_STAGGER_SECONDS` بين كل بوت) دون احتسابها تعطلاً. يتوقف كل شيء لثوانٍ أثناء النشر لأن الـVolume لا يُركَّب على نسختين معًا.

عند الإقلاع يسجّل التطبيق تحذيرات واضحة في Deploy Logs إن غاب `ENCRYPTION_KEY` أو الـVolume أو `ADMIN_IDS`.

## تنبيهات وصيانة (للأدمن)

- ينبّهك البوت على تيليجرام عندما تنخفض المساحة الحرة عن `DISK_ALERT_FREE_PERCENT` (10% افتراضيًا)، مرة كل 6 ساعات.
- زر «🛠 وضع الصيانة» في `/admin` يحجب المستخدمين العاديين عن بوت التحكم مؤقتًا (مفيد قبل ترقية أو تنظيف) بينما تظل البوتات المستضافة تعمل.
- `/metrics` يعرض المساحة والذاكرة وعمر الحارس لربطه بأي أداة مراقبة.

## فحص بعد النشر

بعد نجاح النشر:

1. افتح رابط الخدمة أو نطاقها وتحقق من `/health`؛ يجب أن ترى JSON يحتوي على `status: ok` و`service: telegram-bot-hosting`.
2. افتح بوت التحكم وأرسل `/start`.
3. تحقق من أن حسابك موجود في `ADMIN_IDS` ثم أرسل `/admin`.
4. ارفع بوتًا تجريبيًا صغيرًا، شغّله، افتح سجله، ثم أوقفه.
5. بعد التأكد، أضف التوكن الخاص بالبوت المستضاف من زر **البيئة** وليس داخل ZIP.

## حدود مهمة

التطبيق يحتاج إلى Volume حتى لا تضيع قاعدة البيانات وملفات البوتات عند إعادة النشر. حجم الـVolume يعتمد على خطة Railway؛ راقب المساحة لأن كل بوت مرفوع وبيئته الافتراضية يستهلكان من التخزين. كما أن النسخة الحالية تشغّل البوتات كعمليات فرعية على نفس الحاوية، وليست عزلًا أمنيًا كاملًا لكود مجهول؛ أضف عزل Docker أو خدمة منفصلة قبل فتح الاستضافة للجمهور.

## المراجع الرسمية

- [Railway Start Command](https://docs.railway.com/deployments/start-command)
- [Railway Healthchecks](https://docs.railway.com/deployments/healthchecks)
- [Railway Volumes](https://docs.railway.com/reference/volumes)
- [Railway Config as Code](https://docs.railway.com/config-as-code/reference)
