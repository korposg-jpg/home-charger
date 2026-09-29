"""Password hashing and app login tokens (stdlib only)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any

from .db import Database, iso, utcnow

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, digest_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return hmac.compare_digest(digest, expected)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_token(db: Database, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = iso()
    await db.execute(
        "INSERT INTO api_tokens (token_hash, user_id, created_at, last_used) VALUES (?, ?, ?, ?)",
        (_hash_token(token), user_id, now, now),
    )
    return token


async def revoke_token(db: Database, token: str) -> None:
    await db.execute("DELETE FROM api_tokens WHERE token_hash = ?", (_hash_token(token),))


async def user_for_token(db: Database, token: str, ttl_days: int) -> dict[str, Any] | None:
    token_hash = _hash_token(token)
    cutoff = iso(utcnow() - timedelta(days=ttl_days))
    user = await db.fetchone(
        "SELECT u.id, u.username, u.display_name, u.is_admin FROM api_tokens t "
        "JOIN users u ON u.id = t.user_id WHERE t.token_hash = ? AND t.last_used >= ?",
        (token_hash, cutoff),
    )
    if user:
        await db.execute(
            "UPDATE api_tokens SET last_used = ? WHERE token_hash = ?", (iso(), token_hash)
        )
    return user


async def create_user(
    db: Database, username: str, display_name: str, password: str, is_admin: bool = False
) -> int:
    """Create a user plus the id tag the app uses for that user's remote starts."""
    user_id = await db.execute(
        "INSERT INTO users (username, display_name, password_hash, is_admin, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, display_name, hash_password(password), int(is_admin), iso()),
    )
    # OCPP 1.6 id tags are at most 20 characters.
    app_tag = f"APP-{secrets.token_hex(4).upper()}"
    await db.execute(
        "INSERT INTO id_tags (id_tag, user_id, label, is_app_tag) VALUES (?, ?, ?, 1)",
        (app_tag, user_id, "App"),
    )
    return user_id
