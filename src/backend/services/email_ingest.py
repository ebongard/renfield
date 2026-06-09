"""Email-ingest bridge — backend seam for email-mailbox auto-ingest (Phase 1).

The dedicated ``renfield-mcp-email-ingest`` watcher pushes each allowlisted
attachment to ``POST /api/email-ingest/document`` (Bearer); the route hands the
bytes + email provenance (mailbox_id, message_id, sender, subject) here. This
module **reuses the folder-ingest bridge** (``ingest_document``) wholesale —
dedup (D2), KB filing, owner/tier, the 4-state contract, and the Paperless leg
(incl. correspondent resolve-or-create) — and adds only:

  - server-authoritative **per-mailbox sphere routing** (mailbox_id → owner/tier/kb),
  - the ``email_ingest_log`` provenance + idempotency ledger,
  - an email-specific Bearer token (independently revocable).

Sphere routing is server-side ON PURPOSE: the watcher sends a ``mailbox_id``,
never a tier/owner, so a leaked push token can't file company invoices at an
arbitrary tier. An unknown ``mailbox_id`` → ``failed``. Two mailboxes routing to
different KBs never cross-dedup (``ingest_document`` keys on ``(file_hash, kb)``)
and never cross-record (the ledger keys on ``mailbox_id``).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    SETTING_EMAIL_INGEST_TOKEN,
    EmailIngestLog,
    KnowledgeBase,
    SystemSetting,
    User,
)
from services.folder_ingest import (
    IngestMeta,
    IngestResult,
    IngestStatus,
    PaperlessLeg,
    ingest_document,
)
from utils.config import settings

# Cross-repo push-contract version (mirrors FOLDER_INGEST_CONTRACT_VERSION).
# Sent in the request + response header; bump on ANY request/response shape or
# IngestStatus change.
EMAIL_INGEST_CONTRACT_VERSION = "1"


@dataclass(frozen=True)
class MailboxTarget:
    """Resolved server-authoritative sphere routing for one mailbox_id."""

    mailbox_id: str
    owner: str  # username / numeric id / "" (ownerless)
    tier: int  # circle tier 0-4
    kb_name: str


def resolve_mailbox_target(mailbox_id: str) -> MailboxTarget | None:
    """Look up ``mailbox_id`` in the server-side routing table
    (``settings.email_ingest_mailboxes``). ``None`` when unknown — the route maps
    that to FAILED so a stray/forged mailbox_id never files anywhere."""
    mid = (mailbox_id or "").strip()
    if not mid:
        return None
    for entry in settings.email_ingest_mailboxes:
        if str(entry.get("id", "")).strip() == mid:
            tier = min(max(int(entry.get("tier", 0)), 0), 4)  # clamp to ladder
            return MailboxTarget(
                mailbox_id=mid,
                owner=str(entry.get("owner", "") or "").strip(),
                tier=tier,
                kb_name=str(entry.get("kb") or "").strip() or "Eingang",
            )
    return None


async def _resolve_kb(db: AsyncSession, kb_name: str) -> KnowledgeBase:
    """Get-or-create the mailbox's target KB by name (mirrors folder-ingest)."""
    kb = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == kb_name))
    ).scalar_one_or_none()
    if kb:
        return kb
    kb = KnowledgeBase(
        name=kb_name, description="Auto-ingested documents from watched email mailboxes"
    )
    db.add(kb)
    await db.commit()
    await db.refresh(kb)
    return kb


async def _resolve_owner(db: AsyncSession, owner: str) -> int | None:
    """Resolve ``owner`` (username or numeric id) to a user id; ``""`` → None."""
    target = (owner or "").strip()
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
        logger.warning(f"email-ingest: owner {target!r} not found; using ownerless")
        return None
    return user.id


async def _record_ledger(
    db: AsyncSession,
    *,
    mailbox_id: str,
    message_id: str,
    attachment_sha256: str,
    sender: str | None,
    subject: str | None,
    result: IngestResult,
) -> None:
    """Idempotent provenance/audit row keyed (mailbox_id, message_id, sha).
    Best-effort — a ledger write failure never changes the 4-state result."""
    try:
        existing = (
            await db.execute(
                select(EmailIngestLog).where(
                    EmailIngestLog.mailbox_id == mailbox_id,
                    EmailIngestLog.message_id == message_id,
                    EmailIngestLog.attachment_sha256 == attachment_sha256,
                )
            )
        ).scalar_one_or_none()
        if existing:
            existing.document_id = result.document_id or existing.document_id
            existing.status = result.status.value
        else:
            db.add(
                EmailIngestLog(
                    mailbox_id=mailbox_id,
                    message_id=message_id,
                    attachment_sha256=attachment_sha256,
                    document_id=result.document_id,
                    sender=sender,
                    subject=subject,
                    status=result.status.value,
                )
            )
        await db.commit()
    except Exception as exc:  # noqa: BLE001 - ledger is best-effort
        logger.warning(f"email-ingest: ledger write failed (non-fatal): {exc}")
        await db.rollback()


async def ingest_email_document(
    file_bytes: bytes,
    *,
    db: AsyncSession,
    mailbox_id: str,
    message_id: str,
    filename: str,
    sender: str | None = None,
    subject: str | None = None,
    mime: str | None = None,
    sha256: str | None = None,
    paperless_leg: PaperlessLeg | None = None,
) -> IngestResult:
    """Route one pushed email attachment through the reused folder-ingest bridge
    with server-authoritative per-mailbox sphere routing, then record provenance.
    Returns the same 4-state ``IngestResult`` the watcher keys its move on."""
    target = resolve_mailbox_target(mailbox_id)
    if target is None:
        logger.warning(f"email-ingest: unknown mailbox_id {mailbox_id!r}; rejecting")
        return IngestResult(IngestStatus.FAILED, detail="unknown_mailbox")

    kb = await _resolve_kb(db, target.kb_name)
    owner_user_id = await _resolve_owner(db, target.owner)

    meta = IngestMeta(
        filename=filename,
        root=target.mailbox_id,  # provenance: which mailbox
        relpath=f"{message_id}/{filename}",  # provenance: which message + attachment
        sha256=sha256,
        mime=mime,
    )
    result = await ingest_document(
        file_bytes,
        meta,
        db=db,
        kb_id=kb.id,
        owner_user_id=owner_user_id,
        default_tier=target.tier,
        paperless_leg=paperless_leg,
    )

    attachment_sha = (sha256 or "").lower() or hashlib.sha256(file_bytes).hexdigest()
    await _record_ledger(
        db,
        mailbox_id=target.mailbox_id,
        message_id=message_id or "",
        attachment_sha256=attachment_sha,
        sender=sender,
        subject=subject,
        result=result,
    )
    return result


# ---------------------------------------------------------------------------
# Bearer token (mirrors folder_ingest's helpers, SETTING_EMAIL_INGEST_TOKEN —
# separate so the two watchers are independently revocable).
# ---------------------------------------------------------------------------

async def get_email_ingest_token(db: AsyncSession) -> str | None:
    setting = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.key == SETTING_EMAIL_INGEST_TOKEN)
        )
    ).scalar_one_or_none()
    return setting.value if setting else None


async def generate_email_ingest_token(db: AsyncSession) -> str:
    """Mint/rotate the email-ingest token; persist to SystemSetting. Returns the
    plaintext token (shown once to the admin)."""
    token = secrets.token_urlsafe(48)
    existing = (
        await db.execute(
            select(SystemSetting).where(SystemSetting.key == SETTING_EMAIL_INGEST_TOKEN)
        )
    ).scalar_one_or_none()
    if existing:
        existing.value = token
    else:
        db.add(SystemSetting(key=SETTING_EMAIL_INGEST_TOKEN, value=token))
    await db.commit()
    logger.info("🔑 email-ingest token (re)generated")
    return token


async def verify_email_ingest_token(db: AsyncSession, token: str) -> bool:
    stored = await get_email_ingest_token(db)
    if not stored:
        return False
    return secrets.compare_digest(stored, token)
