"""Idempotent row builders for tests that name ids.

Postgres enforces every foreign key. Tests written against the old sqlite
harness could say `user_id=2` or `role_id=1` for rows that existed nowhere —
the ownership boundary they claimed to check sat between two phantoms. These
helpers create exactly the row a test names, and are safe to call twice.

Deliberately plain functions, not fixtures: most of the call sites are inside
the tests' own builder helpers, which have a session but no fixture scope.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_ROLE_NAME = "test-rolle"


async def ensure_role(db: AsyncSession, name: str = _ROLE_NAME, permissions=None):
    """A role to hang users on (`users.role_id` is NOT NULL)."""
    from models.database import Role

    role = (await db.execute(select(Role).where(Role.name == name))).scalar_one_or_none()
    if role is None:
        role = Role(name=name, permissions=permissions or [], is_system=False)
        db.add(role)
        await db.flush()
    return role


async def ensure_user(db: AsyncSession, user_id: int | None, **kw):
    """The user with this id. ``None`` → ``None`` (auth-off keeps NULL owners)."""
    from models.database import User

    if user_id is None:
        return None
    existing = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if existing is not None:
        return existing
    role = await ensure_role(db, kw.pop("role_name", _ROLE_NAME), kw.pop("permissions", None))
    user = User(
        id=user_id,
        username=kw.pop("username", f"nutzer{user_id}"),
        password_hash=kw.pop("password_hash", "x"),
        is_active=kw.pop("is_active", True),
        role_id=role.id,
        **kw,
    )
    db.add(user)
    await db.flush()
    return user


async def ensure_speaker(db: AsyncSession, speaker_id: int | None, name: str | None = None):
    """The speaker with this id. ``None`` → ``None``."""
    from models.database import Speaker

    if speaker_id is None:
        return None
    existing = (await db.execute(
        select(Speaker).where(Speaker.id == speaker_id)
    )).scalar_one_or_none()
    if existing is not None:
        return existing
    speaker = Speaker(id=speaker_id, name=name or f"Sprecher {speaker_id}")
    db.add(speaker)
    await db.flush()
    return speaker
