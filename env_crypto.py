"""
Environment variable encryption module for the Telegram Bot Hosting Platform.

Provides secure encryption/decryption of sensitive environment variables
(BOT_TOKEN, API_KEY, etc.) stored in the SQLite database.

Uses Fernet symmetric encryption (AES-128-CBC + HMAC-SHA256) from the
cryptography library. The encryption key is derived from a MASTER_KEY
environment variable, with a fallback to an automatically-generated
key stored on first run (NOT recommended for production).

Security note:
    In production, set ENCRYPTION_KEY environment variable to a 32-byte
    URL-safe base64-encoded key generated via:
        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    Without a fixed key, the auto-generated key is lost on restart and
    all encrypted values become permanently unreadable.
"""

from __future__ import annotations

import os
import base64
import logging
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("env-crypto")

# ---------------------------------------------------------------------------
# Master key management
# ---------------------------------------------------------------------------

_master_key: bytes | None = None
_fernet: Fernet | None = None


def _key_file_path() -> Path:
    """مسار ملف المفتاح المولَّد تلقائيًا (داخل نفس Volume قاعدة البيانات)."""
    base = os.getenv("DATABASE_PATH")
    directory = Path(base).resolve().parent if base else Path(__file__).resolve().parent / "data"
    return directory / ".encryption_key"


def _load_master_key() -> bytes:
    """Load or generate the master encryption key.

    Priority:
        1. ENCRYPTION_KEY env var (Fernet key: url-safe base64 of 32 bytes)
        2. A key persisted in the data volume (.encryption_key, mode 0600),
           generated on first run so encrypted values survive restarts.
    """
    global _master_key, _fernet
    if _master_key is not None:
        return _master_key

    env_key = os.getenv("ENCRYPTION_KEY", "").strip()
    if env_key:
        try:
            # Fernet expects the url-safe base64 text itself (not the decoded bytes)
            _fernet = Fernet(env_key.encode("ascii"))
            _master_key = env_key.encode("ascii")
        except Exception as exc:
            logger.error("Invalid ENCRYPTION_KEY format: %s", exc)
            raise RuntimeError(
                "ENCRYPTION_KEY must be a Fernet key (url-safe base64, 32 bytes). Generate one with: "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            ) from exc
        return _master_key

    key_path = _key_file_path()
    try:
        if key_path.exists():
            _master_key = key_path.read_bytes().strip()
        else:
            _master_key = Fernet.generate_key()
            key_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(_master_key)
            logger.warning(
                "ENCRYPTION_KEY is not set: generated a persistent key at %s. "
                "Prefer setting ENCRYPTION_KEY as a secret variable.",
                key_path,
            )
        _fernet = Fernet(_master_key)
    except OSError:
        logger.warning("Could not persist an encryption key; using an ephemeral key (values lost on restart).")
        _master_key = Fernet.generate_key()
        _fernet = Fernet(_master_key)
    return _master_key


def get_fernet() -> Fernet:
    """Return the Fernet instance, initialising the key on first call."""
    _load_master_key()
    assert _fernet is not None
    return _fernet


def rotate_key(new_base64_key: str) -> int:
    """Rotate to a new master key and re-encrypt every stored value with it.

    Old values are decrypted with the current key first, so nothing is lost.
    Returns the number of re-encrypted values. Remember to update the
    ENCRYPTION_KEY variable to the new key afterwards.
    """
    global _master_key, _fernet
    import database as db

    old = get_fernet()
    new_key = new_base64_key.strip().encode("ascii")
    new = Fernet(new_key)

    count = 0
    for row in db.get_all_env_rows():
        value = row["value"]
        if not is_encrypted(value):
            continue
        try:
            plain = old.decrypt(value.encode("ascii"))
        except InvalidToken:
            logger.warning("Skipping bot %s key %s (cannot decrypt with current key)", row["bot_id"], row["key"])
            continue
        db.set_env_raw(row["id"], new.encrypt(plain).decode("ascii"))
        count += 1
    _master_key, _fernet = new_key, new
    logger.info("Re-encrypted %d environment variable values", count)
    return count


# ---------------------------------------------------------------------------
# Encrypt / decrypt helpers
# ---------------------------------------------------------------------------

def encrypt_value(plain: str) -> str:
    """Encrypt a plaintext string. Returns a base64 token safe for DB storage."""
    if not plain:
        return plain
    f = get_fernet()
    return f.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_value(token: str) -> str:
    """Decrypt a token produced by encrypt_value. Returns plaintext or raises."""
    if not token:
        return token
    f = get_fernet()
    try:
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt env value (corrupted or wrong key): %s", exc)
        raise ValueError("Encrypted value is corrupted or key mismatch") from exc


def is_encrypted(token: str) -> bool:
    """Cheap check: Fernet tokens are base64 text that always starts with 'gAAAA'."""
    return bool(token) and len(token) >= 32 and token.startswith("gAAAA")
