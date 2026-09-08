"""API key generation/hashing and admin-token comparison.

Keys are never stored in plaintext: we keep sha256(key) plus a short
prefix for display/lookup ("sk-ab12cd34..."). The raw key is returned to
the caller exactly once, at creation time.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

KEY_PREFIX = "sk-"
_PREFIX_DISPLAY_CHARS = 8


def generate_api_key() -> tuple[str, str, str]:
    """Returns (raw_key, key_prefix, key_hash)."""
    raw_secret = secrets.token_urlsafe(32)
    raw_key = f"{KEY_PREFIX}{raw_secret}"
    key_prefix = raw_key[: len(KEY_PREFIX) + _PREFIX_DISPLAY_CHARS]
    key_hash = hash_api_key(raw_key)
    return raw_key, key_prefix, key_hash


def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
