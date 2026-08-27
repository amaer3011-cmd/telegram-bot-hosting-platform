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
