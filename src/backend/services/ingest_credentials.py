"""Per-integration machine credentials for the ingest push routes.

Replaces the single shared SystemSetting token. See
``docs/design/ingest-credentials.md``.

Token format is ``rfi.<client_id>.<secret>`` — SELF-IDENTIFYING on purpose. The
client_id rides in the token so verification is one indexed lookup plus ONE
bcrypt round. The obvious alternative (bcrypt every stored credential until one
matches) costs N rounds for every REJECTED token, which hands anyone who can
reach the push endpoint a cheap denial of service, and gets worse with each
integration added.

Nothing here ever stores or returns a plaintext token except at mint/rotate
time, where it is returned exactly once.
"""

import asyncio
import re
import secrets
from dataclasses import dataclass
from datetime import datetime

from loguru import logger
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import TIER_PUBLIC, TIER_SELF, IngestCredential
from utils.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Same trick as auth_service._DUMMY_PASSWORD_HASH (security audit M3): an unknown
# client_id would otherwise return without running bcrypt (~0ms) while a real one
# spends a full round (~250ms) — a timing oracle that enumerates valid client_ids.
# One throwaway verify on the miss path equalises the two.
_DUMMY_TOKEN_HASH = pwd_context.hash("renfield-ingest-timing-equalizer")

# Stamping last_authenticated_at on EVERY push would put a write + commit in the
# ingest hot path — the same pooled-connection pressure that exhausted the pool
# in the 2026-07-01 watch-folder outage. The signal it feeds ("is this
# credential still in use?") does not need second precision, so it is throttled.
_LAST_SEEN_THROTTLE_SECONDS = 60

# Reserved: `legacy` is the synthetic client reported for the shared token, so a
# real credential of that name would be indistinguishable from it downstream.
_RESERVED_CLIENT_IDS = frozenset({"legacy"})


async def _verify(secret: str, hashed: str) -> bool:
    """bcrypt is CPU-bound and takes ~150ms. Called inline it would block the
    event loop — every other request in this worker — for that long ON EVERY
    PUSH. Logins can afford it; a watch-folder backlog cannot."""
    return await asyncio.to_thread(pwd_context.verify, secret, hashed)

TOKEN_PREFIX = "rfi"
ROUTE_FOLDER = "folder_ingest"
ROUTE_EMAIL = "email_ingest"

# client_id appears inside the token and in URLs; keep it boring.
_CLIENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


class InvalidClientId(ValueError):
    """client_id fails the charset rule."""


@dataclass(frozen=True)
class IngestClient:
    """Who pushed. ``legacy`` marks the shared SystemSetting token, which has no
    identity — that is precisely the gap this module closes.

    The sphere fields are read from the credential ROW, never from the request.
    A client cannot name its own owner, tier or knowledge base; if it could, a
    stolen token would be an escalation rather than a nuisance. ``None`` on any
    of them means "use the global folder_ingest_* configuration".
    """

    client_id: str
    label: str
    route: str
    legacy: bool = False
    owner: str | None = None
    tier: int | None = None
    kb_name: str | None = None


def _clamp_tier(value) -> int:
    """Clamp to the circle ladder using its canonical bounds, not literals — a
    hardcoded 0/4 would silently diverge if the ladder ever changed."""
    return min(max(int(value), TIER_SELF), TIER_PUBLIC)


def _validate_client_id(client_id: str) -> str:
    cid = (client_id or "").strip().lower()
    if not _CLIENT_ID_RE.match(cid):
        raise InvalidClientId(
            f"client_id must match {_CLIENT_ID_RE.pattern!r} (got {client_id!r})"
        )
    if cid in _RESERVED_CLIENT_IDS:
        raise InvalidClientId(f"client_id {cid!r} is reserved")
    return cid


def _split_token(token: str) -> tuple[str, str] | None:
    """Parse ``rfi.<client_id>.<secret>``. None when it is not one of ours
    (a legacy token), which is a normal path, not an error."""
    parts = (token or "").split(".", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    if not _CLIENT_ID_RE.match(parts[1]) or not parts[2]:
        return None
    return parts[1], parts[2]


def _format_token(client_id: str, secret: str) -> str:
    return f"{TOKEN_PREFIX}.{client_id}.{secret}"


async def mint_credential(
    db: AsyncSession,
    *,
    client_id: str,
    label: str,
    route: str,
    created_by_user_id: int | None = None,
    owner: str | None = None,
    tier: int | None = None,
    kb_name: str | None = None,
) -> str:
    """Create a credential. Returns the plaintext ONCE; only the hash is stored."""
    cid = _validate_client_id(client_id)
    if route not in (ROUTE_FOLDER, ROUTE_EMAIL):
        raise ValueError(f"unknown ingest route: {route!r}")
    existing = (
        await db.execute(select(IngestCredential).where(IngestCredential.client_id == cid))
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"client_id {cid!r} already exists — rotate it instead")

    secret = secrets.token_urlsafe(48)
    db.add(
        IngestCredential(
            client_id=cid,
            label=label or cid,
            route=route,
            token_hash=await asyncio.to_thread(pwd_context.hash, secret),
            created_by_user_id=created_by_user_id,
            created_at=datetime.utcnow(),
            owner=(owner or None),
            tier=(_clamp_tier(tier) if tier is not None else None),
            kb_name=(kb_name or None),
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # The unique index on client_id makes a concurrent duplicate mint safe,
        # but unhandled it surfaces as a 500. Report the same error the
        # pre-check would have.
        await db.rollback()
        raise ValueError(f"client_id {cid!r} already exists — rotate it instead") from None
    logger.info(f"🔑 ingest credential minted: {cid} ({route})")
    return _format_token(cid, secret)


async def rotate_credential(db: AsyncSession, client_id: str) -> str:
    """Issue a new secret for an existing credential. Returns plaintext once."""
    cid = _validate_client_id(client_id)
    row = (
        await db.execute(select(IngestCredential).where(IngestCredential.client_id == cid))
    ).scalar_one_or_none()
    if row is None:
        raise ValueError(f"unknown client_id: {cid}")
    secret = secrets.token_urlsafe(48)
    row.token_hash = await asyncio.to_thread(pwd_context.hash, secret)
    row.rotated_at = datetime.utcnow()
    # Rotation is also the RECOVERY path. Without this, revoking is a permanent
    # dead end: the row keeps the client_id (it is unique), so mint refuses it
    # forever and the name can never be used again. Re-enabling here is safe
    # because rotation issues a NEW secret — the revoked one stays dead — and it
    # takes a deliberate operator action.
    was_revoked = row.revoked_at is not None or not row.is_enabled
    row.revoked_at = None
    row.is_enabled = True
    await db.commit()
    logger.info(
        f"🔑 ingest credential rotated: {cid}" + (" (re-enabled)" if was_revoked else "")
    )
    return _format_token(cid, secret)


async def revoke_credential(db: AsyncSession, client_id: str) -> bool:
    """Revoke. Kept as a row (not deleted) so the audit trail survives."""
    cid = _validate_client_id(client_id)
    row = (
        await db.execute(select(IngestCredential).where(IngestCredential.client_id == cid))
    ).scalar_one_or_none()
    if row is None:
        return False
    row.revoked_at = datetime.utcnow()
    row.is_enabled = False
    await db.commit()
    logger.info(f"🔒 ingest credential revoked: {cid}")
    return True


async def list_credentials(db: AsyncSession, route: str | None = None) -> list[IngestCredential]:
    """List credentials. Rows carry the HASH; callers must never expose it."""
    stmt = select(IngestCredential).order_by(IngestCredential.client_id)
    if route:
        stmt = stmt.where(IngestCredential.route == route)
    return list((await db.execute(stmt)).scalars().all())


async def resolve_ingest_client(
    db: AsyncSession, route: str, token: str, legacy_verify=None
) -> IngestClient | None:
    """Resolve a presented Bearer token to the client that owns it.

    ``legacy_verify`` is the route's existing shared-token check, kept as a
    fallback for the whole transition so a client that has not been migrated
    keeps working. It is awaited only when the token is NOT one of ours.

    Flag off ⇒ the legacy path only, byte-identical to before.
    """
    if not token:
        return None

    if getattr(settings, "ingest_credentials_enabled", False):
        parsed = _split_token(token)
        if parsed is not None:
            cid, secret = parsed
            row = (
                await db.execute(
                    select(IngestCredential).where(IngestCredential.client_id == cid)
                )
            ).scalar_one_or_none()
            if row is None:
                # Equalise timing against the found-but-wrong-secret path so a
                # response time cannot enumerate valid client_ids.
                await _verify(secret, _DUMMY_TOKEN_HASH)
                return None
            if not await _verify(secret, row.token_hash):
                return None
            # Checked AFTER the hash so a revoked/disabled/wrong-route client
            # cannot be distinguished from a wrong secret by timing either.
            if not row.is_enabled or row.revoked_at is not None or row.route != route:
                logger.warning(
                    f"ingest credential {cid} rejected "
                    f"(enabled={row.is_enabled} revoked={row.revoked_at is not None} "
                    f"route={row.route} wanted={route})"
                )
                return None
            now = datetime.utcnow()
            last = row.last_authenticated_at
            if last is None or (now - last).total_seconds() >= _LAST_SEEN_THROTTLE_SECONDS:
                row.last_authenticated_at = now
                await db.commit()
            return IngestClient(
                client_id=row.client_id, label=row.label, route=row.route,
                owner=(row.owner or None),
                # Clamp to the circle ladder. A row edited outside the API must
                # not be able to express a tier that does not exist.
                tier=(_clamp_tier(row.tier) if row.tier is not None else None),
                kb_name=(row.kb_name or None),
            )
        # Not one of ours → fall through to the legacy shared token.

    if legacy_verify is not None and await legacy_verify(db, token):
        return IngestClient(client_id="legacy", label="Legacy shared token",
                            route=route, legacy=True)
    return None


async def set_client_sphere(
    db: AsyncSession,
    client_id: str,
    *,
    owner: str | None,
    tier: int | None,
    kb_name: str | None,
) -> bool:
    """Set where this client's documents land. Admin-only, server-side.

    Passing ``None`` for a field clears it back to the global default rather
    than leaving a stale value — an operator removing an owner must actually
    remove it.
    """
    cid = _validate_client_id(client_id)
    row = (
        await db.execute(select(IngestCredential).where(IngestCredential.client_id == cid))
    ).scalar_one_or_none()
    if row is None:
        return False
    row.owner = owner or None
    row.tier = _clamp_tier(tier) if tier is not None else None
    row.kb_name = kb_name or None
    await db.commit()
    logger.info(f"ingest credential {cid} sphere set: owner={row.owner} tier={row.tier} kb={row.kb_name}")
    return True
