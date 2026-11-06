#!/bin/sh
set -eu

mkdir -p /app/data /app/data/uploaded_bots /app/data/backups /app/data/pip-cache /app/logs

case "${ISOLATE_BOTS:-auto}" in
    false|0|no|off) ISOLATE=0 ;;
    *) ISOLATE=1 ;;
esac

if [ "$ISOLATE" = "1" ]; then
    # عزل البوتات: المنصة تبقى root لتشغّل كل بوت بمستخدم مستقل (انظر process_manager)،
    # وتبقى القاعدة والمفتاح والنسخ الاحتياطية root فقط (600/700). المجلد نفسه 711
    # (عبور بلا سرد) ليصل كل بوت إلى مجلده فقط.
    chown root:root /app/data /app/data/backups
    chmod 711 /app/data /app/data/uploaded_bots
    chmod 700 /app/data/backups
    chown appuser:appuser /app/logs
    exec "$@"
fi

# بلا عزل: نخفض الصلاحيات لمستخدم عادي. chown -R على Volume فيه بيئات افتراضية
# (آلاف الملفات) يبطئ كل إقلاع، لذا نصحح الصلاحيات فقط إذا كان مالك الجذر خاطئًا.
if [ "$(stat -c %U /app/data)" != "appuser" ]; then
    chown -R appuser:appuser /app/data
fi
chown appuser:appuser /app/logs
chmod 700 /app/data

exec gosu appuser "$@"
