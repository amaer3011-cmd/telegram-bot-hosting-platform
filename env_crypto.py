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


def _load_master_key() -> bytes:
    """Load or generate the master encryption key.

    Priority:
        1. ENCRYPTION_KEY env var (base64-encoded 32-byte key)
        2. Auto-generated ephemeral key (logged a warning, only for dev)
    """
    global _master_key, _fernet
    if _master_key is not None:
        return _master_key

    env_key = os.getenv("ENCRYPTION_KEY", "").strip()
    if env_key:
        try:
            # Accept both raw base64 and the Fernet.generate_key() format
            _master_key = base64.urlsafe_b64decode(env_key)
        except Exception as exc:
            logger.error("Invalid ENCRYPTION_KEY format: %s", exc)
            raise RuntimeError("ENCRYPTION_KEY must be a valid base64-encoded 32-byte key") from exc
    else:
        logger.warning(
            "ENCRYPTION_KEY is not set. Using an auto-generated ephemeral key. "
            "All encrypted values will be LOST on restart. Set a permanent key in production."
        )
        _master_key = Fernet.generate_key()

    _fernet = Fernet(_master_key)
    return _master_key


def get_fernet() -> Fernet:
    """Return the Fernet instance, initialising the key on first call."""
    _load_master_key()
    assert _fernet is not None
    return _fernet


def rotate_key(new_base64_key: str) -> None:
    """Rotate to a new master key (for key rotation scenarios).

    DOES NOT re-encrypt existing values — call reencrypt_all_values() afterwards.
    """
    global _master_key, _fernet
    new_key = base64.urlsafe_b64decode(new_base64_key.strip())
    _master_key = new_key
    _fernet = Fernet(new_key)


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
    """Heuristic: an encrypted token is a base64 string that decrypts cleanly."""
    if not token or len(token) < 32:
        return False
    try:
        decrypt_value(token)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Bulk re-encryption (for key rotation / migration)
# ---------------------------------------------------------------------------

def reencrypt_all_values() -> int:
    """Re-encrypt every currently-encrypted value with the active key.

    Call this after rotate_key() to bring all existing records in sync.
    Returns the number of values re-encrypted.
    """
    import database as db

    rows = db.list_env_vars(None)  # type: ignore[arg-type]
    if not rows:
        return 0

    count = 0
    for row in rows:
        value = row["value"]
        if is_encrypted(value):
            try:
                decrypted = decrypt_value(value)
                encrypted = encrypt_value(decrypted)
                if encrypted != value:
                    db.set_env_var(row["bot_id"], row["key"], encrypted)
                    count += 1
            except Exception:
                logger.warning("Skipping re-encryption for bot %s key %s", row["bot_id"], row["key"])
    logger.info("Re-encrypted %d environment variable values", count)
    return count
