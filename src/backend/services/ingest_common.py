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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import KnowledgeBase, SystemSetting, User

# The unique index (name, owner_id) NULLS NOT DISTINCT that makes "one KB of a
# name per owner" hold under concurrency (migration pc20261031_kb_name_per_owner).
KB_NAME_OWNER_INDEX = "uq_knowledge_bases_name_owner"


def _violated_constraint(err: IntegrityError) -> str | None:
    """The constraint/index name Postgres reports for ``err``, or None.

    SQLAlchemy's asyncpg adapter wraps the driver exception; the name sits on
    the asyncpg ``UniqueViolationError`` (``orig.__cause__``), so look there as
    well as on ``orig`` itself."""
    orig = getattr(err, "orig", None)
    for candidate in (orig, getattr(orig, "__cause__", None)):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return None


def owned_kb_filter(name: str, owner_user_id: int | None):
    """WHERE clause for "the KB called ``name`` owned by exactly this owner".

    ``owner_id IS NULL`` for None — an ownerless lookup must not match a KB that
    somebody owns, and vice versa. The unique index (name, owner_id) is
    NULLS NOT DISTINCT, so at most one row matches."""
    owner_clause = (
        KnowledgeBase.owner_id.is_(None)
        if owner_user_id is None
        else KnowledgeBase.owner_id == owner_user_id
    )
    return (KnowledgeBase.name == name) & owner_clause


async def get_or_create_owned_kb(
    db: AsyncSession, name: str, owner_user_id: int | None, *, description: str
) -> KnowledgeBase:
    """Get-or-create the ingest inbox ``name`` of ONE owner.

    One inbox per person: the KB owner reaches every document in the KB
    (``circle_sql`` owner branch on ``kb.owner_id``), so an ingest path must
    never file a document into a KB owned by somebody else. Looking up by
    (name, owner) and creating for exactly that owner keeps the KB owner and
    the document's atom owner identical. ``owner_user_id`` None → the shared
    ownerless KB of that name, as before per-owner inboxes existed.

    Race-safe: two first uploads of one owner both miss the lookup and both
    INSERT; the unique index lets one through and the other raises. The INSERT
    runs inside a SAVEPOINT, so the loser rolls back only the savepoint (the
    caller's transaction stays usable) and re-selects the winner's row, which
    READ COMMITTED shows to the new statement. Only a violation of
    ``uq_knowledge_bases_name_owner`` is absorbed; any other integrity error
    (e.g. an owner that does not exist) propagates."""
    lookup = select(KnowledgeBase).where(owned_kb_filter(name, owner_user_id))
    kb = (await db.execute(lookup)).scalar_one_or_none()
    if kb:
        return kb
    try:
        async with db.begin_nested():
            kb = KnowledgeBase(name=name, description=description, owner_id=owner_user_id)
            db.add(kb)
    except IntegrityError as err:
        if _violated_constraint(err) != KB_NAME_OWNER_INDEX:
            raise
        logger.info(
            f"KB '{name}' (owner {owner_user_id}) was created concurrently; using that row"
        )
        return (await db.execute(lookup)).scalar_one()
    await db.commit()
    await db.refresh(kb)
    return kb


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
