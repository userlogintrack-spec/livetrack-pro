"""Symmetric encryption + one-way hashing helpers for sensitive model fields.

Why a thin wrapper over `cryptography.fernet`:
  - Centralises key handling so models don't import Fernet directly.
  - Lets us swap key sources later (env var → KMS) without touching call sites.
  - Returns "" for empty input so blank fields round-trip cleanly through the DB.

Key sourcing:
  - `FIELD_ENCRYPTION_KEY` env var if set (must be a Fernet 32-byte url-safe base64 key).
  - Otherwise derived from `SECRET_KEY` via PBKDF2 — convenient for dev, but rotating
    SECRET_KEY then becomes a destructive op on encrypted fields. Set the dedicated
    env var in production.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

from django.conf import settings
from cryptography.fernet import Fernet, InvalidToken


_FERNET: Fernet | None = None


def _get_fernet() -> Fernet:
    global _FERNET
    if _FERNET is not None:
        return _FERNET
    raw = os.getenv('FIELD_ENCRYPTION_KEY', '').strip()
    if raw:
        try:
            _FERNET = Fernet(raw.encode() if isinstance(raw, str) else raw)
            return _FERNET
        except Exception as e:
            raise RuntimeError(
                'FIELD_ENCRYPTION_KEY is set but is not a valid Fernet key. '
                'Generate one with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from e
    # Derive from SECRET_KEY — PBKDF2-SHA256 with a fixed salt is fine here because
    # the input (SECRET_KEY) is already high-entropy; the salt only namespaces the
    # derived key away from any other use of SECRET_KEY.
    derived = hashlib.pbkdf2_hmac(
        'sha256',
        settings.SECRET_KEY.encode('utf-8'),
        b'tracker.field_encryption.v1',
        iterations=200_000,
        dklen=32,
    )
    _FERNET = Fernet(base64.urlsafe_b64encode(derived))
    return _FERNET


def encrypt_str(value: str) -> str:
    """Fernet-encrypt a string. Empty string round-trips as empty (so blank=True
    fields stay blank in the DB instead of holding a non-empty ciphertext)."""
    if not value:
        return ''
    return _get_fernet().encrypt(value.encode('utf-8')).decode('utf-8')


def decrypt_str(value: str) -> str:
    """Inverse of encrypt_str. Returns '' if the ciphertext is empty or invalid —
    callers shouldn't crash because a row was written before encryption was on."""
    if not value:
        return ''
    try:
        return _get_fernet().decrypt(value.encode('utf-8')).decode('utf-8')
    except (InvalidToken, ValueError):
        return ''


# ────────────────────────────────────────────────
# One-way hashing for one-time codes (backup codes)
# ────────────────────────────────────────────────

_HASH_ITER = 120_000


def hash_code(code: str) -> str:
    """PBKDF2-SHA256 hash a short one-time code. Format: pbkdf2_sha256$iter$salt$hash."""
    if not code:
        return ''
    salt = secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac('sha256', code.encode('utf-8'), salt.encode('utf-8'), _HASH_ITER)
    return f'pbkdf2_sha256${_HASH_ITER}${salt}${base64.b64encode(digest).decode("ascii")}'


def verify_code(code: str, hashed: str) -> bool:
    """Constant-time compare a plaintext code against a stored hash."""
    if not code or not hashed:
        return False
    try:
        algo, iter_str, salt, b64 = hashed.split('$', 3)
        if algo != 'pbkdf2_sha256':
            return False
        expected = base64.b64decode(b64)
        candidate = hashlib.pbkdf2_hmac('sha256', code.encode('utf-8'), salt.encode('utf-8'), int(iter_str))
        return hmac.compare_digest(expected, candidate)
    except (ValueError, TypeError):
        return False
