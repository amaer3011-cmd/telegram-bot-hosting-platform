from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

import pytest

import main
from utils import UnsafeZipError, extract_zip, find_entry_file, safe_filename


def test_safe_filename_rejects_path_traversal() -> None:
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("main.py") == "main.py"


def test_extract_zip_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    destination = tmp_path / "destination"
    destination.mkdir()
    with zipfile.ZipFile(archive, "w") as zip_file:
        zip_file.writestr("../../outside.txt", "blocked")
    with pytest.raises(UnsafeZipError):
        extract_zip(str(archive), str(destination))
    assert not (tmp_path / "outside.txt").exists()


def test_find_entry_file_prefers_main(tmp_path: Path) -> None:
    (tmp_path / "z.py").write_text("print('z')\n", encoding="utf-8")
    (tmp_path / "main.py").write_text("print('main')\n", encoding="utf-8")
    assert find_entry_file(str(tmp_path)) == str(tmp_path / "main.py")


def test_hosting_application_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main.config, "HOST_BOT_TOKEN", "123456:ABC-test-token")
    application = main.build_application()
    assert len(application.handlers.get(0, [])) >= 12


def test_bot_controls_include_runtime_management() -> None:
    callbacks = [
        button.callback_data
        for row in main.bot_keyboard(7).inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "bot:auto:7" in callbacks
    assert "bot:schedule:7" in callbacks
    assert "bot:env:7" in callbacks
    assert "bot:memory:7" in callbacks


def test_progress_bar_is_bounded_and_readable() -> None:
    progress = main.progress_text("تشغيل البوت", 145, "جارٍ العمل")
    assert "100%" in progress
    assert "██████████" in progress
    assert "تشغيل البوت" in progress


def test_admin_controls_include_broadcast_and_bulk_stop() -> None:
    callbacks = [
        button.callback_data
        for row in main.admin_keyboard().inline_keyboard
        for button in row
        if button.callback_data
    ]
    assert "admin:broadcast" in callbacks
    assert "admin:broadcast_cancel" in callbacks
    assert "admin:stop_all_confirm" in callbacks


def test_broadcast_sends_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[tuple[int, str]] = []

        async def send_message(self, target_id: int, text: str, **kwargs) -> None:
            self.sent.append((target_id, text))

    fake_bot = FakeBot()
    users = [{"user_id": 101, "is_banned": 0}, {"user_id": 102, "is_banned": 1}, {"user_id": 103, "is_banned": 0}]
    monkeypatch.setattr(main.db, "get_all_users", lambda: users)
    monkeypatch.setattr(main.db, "log_admin_action", lambda *args: None)
    main.BROADCAST_CANCEL = None
    asyncio.run(main.run_broadcast(fake_bot, 999, "hello"))
    assert {target_id for target_id, _ in fake_bot.sent[:2]} == {101, 103}
    assert fake_bot.sent[-1][0] == 999
    assert "تم الإرسال: 2" in fake_bot.sent[-1][1]


# ---------------- hosting runtime tests ----------------

import os
import sys
import time


def test_env_crypto_roundtrip_with_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from cryptography.fernet import Fernet
    import env_crypto

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(env_crypto, "_master_key", None)
    monkeypatch.setattr(env_crypto, "_fernet", None)
    token = env_crypto.encrypt_value("123456:secret")
    assert env_crypto.is_encrypted(token)
    assert env_crypto.decrypt_value(token) == "123456:secret"


def test_env_crypto_persists_generated_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import env_crypto

    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "hosting.db"))
    monkeypatch.setattr(env_crypto, "_master_key", None)
    monkeypatch.setattr(env_crypto, "_fernet", None)
    token = env_crypto.encrypt_value("abc")
    # simulate a restart: key must be re-read from the volume
    monkeypatch.setattr(env_crypto, "_master_key", None)
    monkeypatch.setattr(env_crypto, "_fernet", None)
    assert env_crypto.decrypt_value(token) == "abc"
    assert oct((tmp_path / ".encryption_key").stat().st_mode & 0o777) == "0o600"


def test_bot_env_does_not_leak_host_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import process_manager as pm

    monkeypatch.setenv("HOST_BOT_TOKEN", "999:host-secret")
    monkeypatch.setenv("ENCRYPTION_KEY", "super-secret")
    monkeypatch.setattr(pm.db, "get_env_vars", lambda bot_id: {"BOT_TOKEN": "1:mine", "LD_PRELOAD": "/evil.so"})
    env = pm.build_bot_env(1, str(tmp_path))
    assert "HOST_BOT_TOKEN" not in env and "ENCRYPTION_KEY" not in env
    assert env["BOT_TOKEN"] == "1:mine"
    assert "LD_PRELOAD" not in env
    assert env["HOME"] == str(tmp_path)


def test_flatten_single_root_and_entry_priority(tmp_path: Path) -> None:
    from utils import flatten_single_root

    inner = tmp_path / "repo-main"
    inner.mkdir()
    (inner / "requirements.txt").write_text("x\n")
    (inner / "app.py").write_text("print(1)\n")
    (inner / "main.py").write_text("print(2)\n")
    flatten_single_root(str(tmp_path))
    assert (tmp_path / "requirements.txt").exists() and not inner.exists()
    assert find_entry_file(str(tmp_path)) == str(tmp_path / "main.py")


def test_extract_zip_counts_real_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import utils

    monkeypatch.setattr(utils, "MAX_EXTRACTED_SIZE_MB", 1)
    archive = tmp_path / "bomb.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.bin", b"\0" * (3 * 1024 * 1024))
    with pytest.raises(UnsafeZipError):
        extract_zip(str(archive), str(tmp_path / "out"))


def test_backup_bots_archive_skips_venv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import tarfile
    import backup

    bots = tmp_path / "bots"
    bot = bots / "1" / "bot_1"
    (bot / ".venv").mkdir(parents=True)
    (bot / ".venv" / "junk").write_text("x")
    (bot / "main.py").write_text("print(1)\n")
    (bot / "run.log").write_text("log")
    monkeypatch.setattr(backup.config, "BOTS_DIR", bots)
    monkeypatch.setattr(backup, "BACKUP_DIR", tmp_path / "backups")
    (tmp_path / "backups" / "bots").mkdir(parents=True)
    result = backup.backup_bot_folders()
    assert result is not None
    with tarfile.open(result) as tar:
        names = tar.getnames()
    assert any(n.endswith("main.py") for n in names)
    assert not any(".venv" in n or n.endswith("run.log") for n in names)


def _make_manager_with_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: str):
    import process_manager as pm

    monkeypatch.setattr(pm, "ensure_venv", lambda *a, **k: None)
    monkeypatch.setattr(pm, "run_as_kwargs", lambda bot_id: {})
    monkeypatch.setattr(pm, "prepare_bot_dir", lambda folder, bot_id: os.makedirs(os.path.join(folder, ".tmp"), exist_ok=True))
    monkeypatch.setattr(pm, "venv_python", lambda folder: sys.executable)
    monkeypatch.setattr(pm.db, "get_env_vars", lambda bot_id: {})
    for name in ("update_status", "update_pid", "set_last_error", "update_last_started"):
        monkeypatch.setattr(pm.db, name, lambda *a, **k: None)
    entry = tmp_path / "main.py"
    entry.write_text(script)
    row = {"bot_id": 1, "folder": str(tmp_path), "entry_file": str(entry), "max_memory_mb": None, "owner_id": 5, "name": "t"}
    return pm.ProcessManager(), row


def test_stop_bot_kills_whole_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import psutil

    script = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid', 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    mgr, row = _make_manager_with_bot(monkeypatch, tmp_path, script)
    ok, _ = mgr.start_bot(row)
    assert ok
    for _ in range(50):
        if (tmp_path / "child.pid").exists():
            break
        time.sleep(0.1)
    child_pid = int((tmp_path / "child.pid").read_text())
    mgr.stop_bot(1)
    time.sleep(0.3)
    assert not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE


def test_memory_limit_kills_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mgr, row = _make_manager_with_bot(monkeypatch, tmp_path, "import time\nx = bytearray(60*1024*1024)\ntime.sleep(60)\n")
    row["max_memory_mb"] = 128  # below the floor in config, set directly for the test
    assert mgr.start_bot(row)[0]
    import process_manager as pm

    row_hot = dict(row, max_memory_mb=20)
    monkeypatch.setattr(pm.db, "get_bots_by_status", lambda s: [row_hot])
    time.sleep(1.0)
    mgr._enforce_memory_limits()
    time.sleep(0.5)
    assert not mgr.is_running(1)
    assert "الذاكرة" in mgr._kill_reasons.get(1, "")
    mgr.stop_bot(1)


def test_shutdown_all_keeps_status_for_recovery(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mgr, row = _make_manager_with_bot(monkeypatch, tmp_path, "import time\ntime.sleep(60)\n")
    assert mgr.start_bot(row)[0]
    started = time.monotonic()
    mgr.shutdown_all()
    assert time.monotonic() - started < 5
    assert not mgr.is_running(1)


# ---------------- new features ----------------

def test_rate_limiter_blocks_after_limit_and_recovers() -> None:
    from ratelimit import RateLimiter

    rl = RateLimiter(3)
    assert all(rl.allow(1, now=t) for t in (0, 1, 2))
    assert not rl.allow(1, now=3)
    assert rl.allow(2, now=3)  # other users unaffected
    assert rl.allow(1, now=61)  # window slid
    assert rl.should_warn(1, now=62) and not rl.should_warn(1, now=63)
    assert RateLimiter(0).allow(1)  # disabled


def test_sparkline_scales_to_peak() -> None:
    from utils import sparkline

    assert sparkline([0, 50, 100]) == "▁▅█"
    assert sparkline([10, 10], top=100) == "▂▂"
    assert sparkline([]) == ""


def test_guard_registered_and_new_buttons() -> None:
    callbacks = [b.callback_data for row in main.bot_keyboard(7).inline_keyboard for b in row if b.callback_data]
    assert "bot:health:7" in callbacks and "bot:logs_full:7" in callbacks


def test_rate_limit_guard_stops_spammer(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram.ext import ApplicationHandlerStop

    monkeypatch.setattr(main, "LIMITER", main.RateLimiter(1))
    monkeypatch.setattr(main.config, "ADMIN_IDS", (999,))
    replies: list[str] = []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)

    class Upd:
        effective_user = type("U", (), {"id": 5})()
        callback_query = None
        message = Msg()

    asyncio.run(main.rate_limit_guard(Upd(), None))  # first passes
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(main.rate_limit_guard(Upd(), None))
    assert len(replies) == 1
    Upd.effective_user = type("U", (), {"id": 999})()  # admin exempt
    for _ in range(5):
        asyncio.run(main.rate_limit_guard(Upd(), None))


# ---------------- maintenance / env delete / disk alert ----------------

def test_env_delete_is_scoped_to_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import importlib
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "t.db"))
    import config as cfg
    importlib.reload(cfg)
    import database as d
    importlib.reload(d)
    d.init_db()
    d.set_env_var(1, "A", "x")
    d.set_env_var(2, "B", "y")
    row_b = [r for r in d.list_env_vars(2)][0]
    assert not d.delete_env_var_for_bot(1, row_b["id"])  # other bot's var is untouched
    assert d.delete_env_var_for_bot(2, row_b["id"])
    d.set_setting("maintenance", "1")
    assert d.get_setting("maintenance") == "1" and d.get_setting("nope", "d") == "d"


def test_maintenance_guard_blocks_users_not_admins(monkeypatch: pytest.MonkeyPatch) -> None:
    from telegram.ext import ApplicationHandlerStop

    monkeypatch.setattr(main, "maintenance_on", lambda: True)
    monkeypatch.setattr(main.config, "ADMIN_IDS", (999,))
    replies: list[str] = []

    class Msg:
        async def reply_text(self, text, **kw):
            replies.append(text)

    class Upd:
        effective_user = type("U", (), {"id": 5})()
        callback_query = None
        message = Msg()

    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(main.maintenance_guard(Upd(), None))
    assert "صيانة" in replies[0]
    Upd.effective_user = type("U", (), {"id": 999})()
    asyncio.run(main.maintenance_guard(Upd(), None))


def test_disk_alert_fires_once_per_cooldown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import collections
    import process_manager as pm

    sent: list[int] = []
    mgr = pm.ProcessManager(bot_notifier=lambda uid, text: sent.append(uid))
    monkeypatch.setattr(pm.config, "ADMIN_IDS", (1, 2))
    monkeypatch.setattr(pm.config, "DISK_ALERT_FREE_PERCENT", 10)
    U = collections.namedtuple("U", "total used free")
    monkeypatch.setattr(pm.shutil, "disk_usage", lambda p: U(100, 97, 3))
    mgr._check_disk()
    mgr._check_disk()
    assert sent == [1, 2]
    monkeypatch.setattr(pm.shutil, "disk_usage", lambda p: U(100, 20, 80))
    mgr._last_disk_alert = 0.0
    mgr._check_disk()
    assert sent == [1, 2]


# ---------------- isolation / quota / env parsing ----------------

def test_parse_env_text_handles_dotenv_formats() -> None:
    from utils import parse_env_text

    pairs, rejected = parse_env_text(
        "# comment\n\nbot_token = 123:ABC\nexport API_KEY=\"quoted value\"\nNAME=abc # trailing\nbad line here\nEMPTY=\n"
    )
    assert pairs == [("BOT_TOKEN", "123:ABC"), ("API_KEY", "quoted value"), ("NAME", "abc"), ("EMPTY", "")]
    assert rejected == ["bad line here"]


def test_dir_size_ignores_symlinks(tmp_path: Path) -> None:
    from utils import dir_size_bytes

    (tmp_path / "a.bin").write_bytes(b"x" * 1000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"x" * 500)
    (tmp_path / "link").symlink_to(tmp_path / "a.bin")
    assert dir_size_bytes(str(tmp_path)) == 1500


def test_disk_quota_stops_oversized_bot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mgr, row = _make_manager_with_bot(monkeypatch, tmp_path, "import time\ntime.sleep(60)\n")
    import process_manager as pm

    (tmp_path / "big.bin").write_bytes(b"0" * (3 * 1024 * 1024))
    assert mgr.start_bot(row)[0]
    sent: list[str] = []
    mgr.notifier = lambda uid, text: sent.append(text)
    monkeypatch.setattr(pm.config, "MAX_BOT_DISK_MB", 1)
    monkeypatch.setattr(pm.db, "get_bots_by_status", lambda s: [row])
    monkeypatch.setattr(pm.db, "get_bot", lambda bid: row)
    mgr._enforce_disk_quota()
    assert not mgr.is_running(1)
    assert sent and "القرص" in sent[0]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root to switch UID")
def test_isolated_bot_cannot_read_platform_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import process_manager as pm
    import utils

    monkeypatch.setattr(utils, "ISOLATION_ACTIVE", True)
    monkeypatch.setattr(pm, "prepare_bot_dir", utils.prepare_bot_dir)
    monkeypatch.setattr(pm, "run_as_kwargs", utils.run_as_kwargs)
    monkeypatch.setattr(pm, "ensure_venv", lambda *a, **k: None)
    monkeypatch.setattr(pm, "venv_python", lambda f: sys.executable)
    monkeypatch.setattr(pm.db, "get_env_vars", lambda b: {})
    for name in ("update_status", "update_pid", "set_last_error", "update_last_started"):
        monkeypatch.setattr(pm.db, name, lambda *a, **k: None)
    root = tmp_path / "data"
    folder = root / "bots" / "1" / "bot_1"
    other = root / "bots" / "1" / "bot_2"
    folder.mkdir(parents=True)
    other.mkdir()
    secret = root / "hosting.db"
    secret.write_text("SECRET")
    secret.chmod(0o600)
    (other / "token.txt").write_text("OTHER")
    other.chmod(0o700)
    os.chown(other, utils.bot_uid(2), utils.bot_uid(2))
    for d in (*tmp_path.parents[:2], tmp_path, root, root / "bots"):
        d.chmod(0o711)
    (folder / "main.py").write_text(
        "import os\n"
        f"def can(p):\n    try:\n        open(p).read(); return 'READ'\n    except Exception as e: return type(e).__name__\n"
        f"print('uid', os.getuid(), 'db', can({str(secret)!r}), 'other', can({str(other / 'token.txt')!r}))\n"
    )
    entry = str(folder / "main.py")
    mgr = pm.ProcessManager()
    row = {"bot_id": 1, "folder": str(folder), "entry_file": entry, "max_memory_mb": None, "owner_id": 5, "name": "t"}
    assert mgr.start_bot(row)[0]
    time.sleep(1.0)
    out = (folder / "run.log").read_text()
    assert f"uid {utils.bot_uid(1)}" in out
    assert "db PermissionError" in out and "other PermissionError" in out
    mgr.stop_bot(1)


def test_new_buttons_and_bulk_env_save(monkeypatch: pytest.MonkeyPatch) -> None:
    callbacks = [b.callback_data for row in main.bot_keyboard(7).inline_keyboard for b in row if b.callback_data]
    assert "bot:rename:7" in callbacks and "bot:rebuild:7" in callbacks
    assert "admin:top" in [b.callback_data for row in main.admin_keyboard().inline_keyboard for b in row]
    stored: dict[str, str] = {}
    monkeypatch.setattr(main.db, "set_env_var", lambda bot_id, k, v: stored.__setitem__(k, v))
    monkeypatch.setattr(main.db, "log_admin_action", lambda *a: None)
    saved, rejected = main.save_env_pairs(1, 5, [("BOT_TOKEN", "1:a"), ("PATH", "/evil"), ("OK_KEY", "v")])
    assert saved == ["BOT_TOKEN", "OK_KEY"] and rejected == ["PATH"]
    assert "PATH" not in stored
