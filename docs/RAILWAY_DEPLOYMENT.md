# نشر بوت الاستضافة على Railway

## المتطلبات

أنشئ خدمة جديدة من مستودع GitHub، واترك Railway يستخدم `infra/railway/railway.json` و`infra/docker/Dockerfile`. لا تضع Start Command مخصصًا في إعدادات الخدمة؛ لأن Railway يستخدم ENTRYPOINT وCMD الموجودين في Dockerfile عندما يكون Start Command فارغًا.

## المتغيرات السرية

أضف هذه القيم من تبويب Variables في Railway، ولا تضعها داخل GitHub:

| المتغير | القيمة |
|---|---|
| `HOST_BOT_TOKEN` | توكن بوت التحكم الذي أنشأته من BotFather. |
| `ADMIN_IDS` | أرقام حسابات Telegram المالكة مفصولة بفواصل. |

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

يعيد التطبيق HTTP 200 من `/health` و`/healthz` بعد بدء خادم الصحة. هذا فحص جاهزية أثناء النشر، وليس مراقبة مستمرة بعد أن تصبح الخدمة Active.

## إعدادات التشغيل

استخدم Replica واحدة فقط لأن بوت التحكم يعمل بـlong polling ولأن Railway لا يسمح باستخدام replicas مع Volume. اترك Restart Policy على `ON_FAILURE` مع عدد محاولات مناسب، واترك `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` بقيمة قصيرة مثل 10 إذا أردت منح العملية وقتًا للإغلاق الهادئ.

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
