"""Shared helpers for the ingest bridges (folder-ingest, email-ingest).

Factored out of folder_ingest so a second ingest source (email) reuses the
revocable-Bearer-token plumbing and the username/id → user-id resolution instead
of copying them a third time. Each bridge keeps its own ``SETTING_*`` key and a
thin named wrapper (the routes import the named functions), so a security fix to
the token compare / rotation lands in one place.
"""

from __future__ import annotations

import secrets

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import SystemSetting, User


async def get_ingest_token(db: AsyncSession, key: str) -> str | None:
    """The stored Bearer token for ``key`` (a SystemSetting), or None."""
    setting = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    return setting.value if setting else None


async def generate_ingest_token(db: AsyncSession, key: str) -> str:
    """Mint/rotate the token at ``key`` and persist it. Returns the plaintext."""
    token = secrets.token_urlsafe(48)
    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if existing:
        existing.value = token
    else:
        db.add(SystemSetting(key=key, value=token))
    await db.commit()
    logger.info(f"🔑 ingest token (re)generated for {key}")
    return token


async def set_ingest_token(db: AsyncSession, key: str, value: str) -> None:
    """Upsert a SPECIFIC token value at ``key`` (vs generate_ which mints a random
    one). Used by the boot credential-reconciler to seed the DB from the
    authoritative secret so a DB wipe self-heals. No-op if already equal."""
    existing = (
        await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()
    if existing:
        if existing.value == value:
            return
        existing.value = value
    else:
        db.add(SystemSetting(key=key, value=value))
    await db.commit()


async def verify_ingest_token(db: AsyncSession, key: str, presented: str) -> bool:
    """Constant-time compare ``presented`` against the stored token at ``key``.
    False when none is configured (feature unprovisioned)."""
    stored = await get_ingest_token(db, key)
    if not stored:
        return False
    return secrets.compare_digest(stored, presented)


async def resolve_user_id(db: AsyncSession, target: str) -> int | None:
    """Resolve a username or numeric-id string to a user id; ``""`` → None
    (ownerless). Used by both ingest bridges for the configured owner."""
    target = (target or "").strip()
    if not target:
        return None
    user = (
        await db.execute(select(User).where(User.username == target))
    ).scalar_one_or_none()
    if user is None and target.isdigit():
        user = (
            await db.execute(select(User).where(User.id == int(target)))
        ).scalar_one_or_none()
    if user is None:
        logger.warning(f"ingest: target user {target!r} not found; using ownerless")
        return None
    return user.id
