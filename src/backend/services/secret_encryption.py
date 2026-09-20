"""
Symmetric encryption for sensitive values stored at rest (e.g. BLE IRKs).

Keys derived deterministically from `settings.secret_key` (the same secret used
for JWT signing). Uses Fernet (AES-128-CBC + HMAC-SHA256, authenticated).

IRKs are device-tracking secrets — never store or log them in plaintext.

Key rotation (BL-0357, 2026-09-20): ``settings.secret_key_previous`` may list
former SECRET_KEYs (comma-separated, newest first). Encryption always uses the
CURRENT key; decryption tries the current key first, then each previous one
(``cryptography``'s ``MultiFernet``). ``rotate_secret`` re-encrypts a token under
the current key so the previous key can be dropped afterwards
(``bin/rotate_secret_encryption.py`` walks the stored rows).

Without ``SECRET_KEY_PREVIOUS`` a SECRET_KEY rotation is still DESTRUCTIVE for
these secrets — every stored value becomes permanently undecryptable and must
be re-entered.
"""
import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from utils.config import settings

__all__ = [
    "encrypt_secret",
    "decrypt_secret",
    "rotate_secret",
    "is_current_key",
    "reset_key_cache",
    "InvalidToken",
]

logger = logging.getLogger(__name__)

_DEFAULT_SECRET = "changeme-in-production-use-strong-random-key"


def _secret_str(raw) -> str:
    # SecretStr in pydantic; tolerate a plain str too.
    return raw.get_secret_value() if hasattr(raw, "get_secret_value") else str(raw or "")


def _fernet_for(secret: str) -> Fernet:
    # Derive a stable 32-byte urlsafe-base64 Fernet key from an app secret.
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def _previous_secrets() -> list[str]:
    raw = _secret_str(settings.secret_key_previous)
    return [s.strip() for s in raw.split(",") if s.strip()]


@lru_cache(maxsize=1)
def _primary() -> Fernet:
    secret = _secret_str(settings.secret_key)
    if secret == _DEFAULT_SECRET:
        # Encrypting a tracking secret under the publicly-known default key is
        # effectively plaintext — flag loudly (logged once via the cache).
        logger.warning(
            "secret_encryption: SECRET_KEY is the insecure default — "
            "encrypted-at-rest secrets (e.g. BLE IRKs) are NOT protected. "
            "Set a strong SECRET_KEY."
        )
    return _fernet_for(secret)


@lru_cache(maxsize=1)
def _fernet() -> MultiFernet:
    """Current key first (used for encryption), then the previous keys
    (decryption only). A MultiFernet with one key behaves like plain Fernet."""
    current = _secret_str(settings.secret_key)
    keys = [_primary()]
    for prev in _previous_secrets():
        if prev == current:
            continue  # listing the current key again is harmless, skip it
        keys.append(_fernet_for(prev))
    if len(keys) > 1:
        logger.info(
            "secret_encryption: %d previous SECRET_KEY(s) accepted for decryption — "
            "run bin/rotate_secret_encryption.py --commit, then drop SECRET_KEY_PREVIOUS.",
            len(keys) - 1,
        )
    return MultiFernet(keys)


def reset_key_cache() -> None:
    """Forget the derived keys (tests / after a settings change in-process)."""
    _primary.cache_clear()
    _fernet.cache_clear()


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a string for storage at rest with the CURRENT key. Returns a Fernet token (str)."""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(token: str) -> str:
    """Decrypt a Fernet token produced by encrypt_secret() under the current OR a
    previous key. Raises InvalidToken if the data is corrupt or no listed key fits."""
    return _fernet().decrypt(token.encode()).decode()


def is_current_key(token: str) -> bool:
    """True if the token was encrypted under the CURRENT key (no rotation needed).
    Raises InvalidToken if no listed key can decrypt it at all."""
    try:
        _primary().decrypt(token.encode())
        return True
    except InvalidToken:
        _fernet().decrypt(token.encode())  # raises if unknown to every key
        return False


def rotate_secret(token: str) -> str:
    """Re-encrypt a token under the current key (decrypting with whichever listed
    key fits). Returns the token unchanged if it is already current."""
    if is_current_key(token):
        return token
    return _fernet().rotate(token.encode()).decode()
