"""
Federation query audit (F4d).

Writes one `FederationQueryLog` row per asker-side federated query
lifecycle. Called by `MCPManager._execute_federation_streaming` after
the asker yields its terminal result.

Privacy:
    Rows are scoped strictly to the asker's `user_id`. Responder-side
    audit (who asked me) is a separate future feature with its own
    privacy boundary — do not conflate.

Truncation:
    `query_text` + `answer_excerpt` + `error_message` are all truncated
    at write time to bound row size and keep list-page queries fast.
    Users who want the full answer re-run the query (federation is
    idempotent per `request_id`).

Retention:
    `prune_old_audit_rows()` deletes rows older than
    `FEDERATION_AUDIT_RETENTION_DAYS` (default 90). Called from the
    lifecycle hourly loop.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationQueryLog, PeerUser
from services.database import AsyncSessionLocal
from utils.log_safety import error_text_without_content

# Per-column truncation caps. Chosen to keep a single audit row well
# under 4 KB on disk (Postgres TOAST threshold, though TEXT isn't
# strictly bounded by this — the cap is a UX choice, not a storage one).
MAX_QUERY_TEXT_LEN = 1000
MAX_ANSWER_EXCERPT_LEN = 2000
MAX_ERROR_MESSAGE_LEN = 500

FEDERATION_AUDIT_RETENTION_DAYS = 90

#: The two directions that are QUERIES. Everything that lists or prunes
#: queries filters on these, so a row of another kind (a revocation) is never
#: shown as a question somebody asked.
QUERY_DIRECTIONS = ("asked", "answered")

#: `direction` of a revocation record (the column is 8 characters wide).
REVOCATION_DIRECTION = "revoke"
#: What was revoked, stored in `final_status` (16 characters wide).
PAIRING_REVOKED = "pairing_revoked"
LINK_REVOKED = "link_revoked"
#: `cause` of a link revocation that came with a re-pairing of a revoked
#: pairing (the other cause is `PAIRING_REVOKED`).
PAIRING_REVIVED = "pairing_revived"

#: On whose authority a revocation rested, stored in `identity_basis`.
BASIS_OWNER = "owner"   # the account that owns the pairing
BASIS_SELF = "self"     # the account the link belongs to
BASIS_ADMIN = "admin"   # an `admin` holder acting on someone else's row


# --- The outcome of a query row, as a code -----------------------------------
#
# `error_message` is a sentence for the person reading `/brain/audit`. What
# HAPPENED must not have to be read back out of that sentence -- not by a
# test, and not by anything that later counts or filters these rows. So the
# sentence is led by a code:
#
#     [link_revoked/after_read] Verknüpfung besteht nicht mehr nach dem ...
#
# The code is the record; the sentence is presentation and may be reworded.
# It lives in the text column because a query row has no structured field
# (the JSON a revocation row carries sits in `query_text`, which here holds
# the question). A column of its own would be the cleaner home.

#: How far the access had got when it stopped being served.
STAGE_BEFORE_READ = "before_read"      # nothing was read
STAGE_AFTER_READ = "after_read"        # data was read, no answer was made
STAGE_AFTER_ANSWER = "after_answer"    # an answer was made, never handed over

_OUTCOME_NOTE = re.compile(r"^\[([a-z_]+)(?:/([a-z_]+))?\] ")


def outcome_note(code: str, sentence: str, *, stage: str | None = None) -> str:
    """`sentence`, led by its code (and stage) in machine-readable form."""
    return f"[{code}/{stage}] {sentence}" if stage else f"[{code}] {sentence}"


def outcome_code(text: str | None) -> tuple[str, str | None] | None:
    """`(code, stage)` of a text written by `outcome_note` -- `None` for any
    other text (a plain failure message, a row from before 2026-11-16)."""
    match = _OUTCOME_NOTE.match(text or "")
    return (match.group(1), match.group(2)) if match else None


def _truncate(s: str | None, max_len: int) -> str | None:
    if s is None:
        return None
    if len(s) <= max_len:
        return s
    # Ellipsis marker is included in the budget.
    return s[: max_len - 1] + "…"


def _classify_final(final: dict[str, Any] | None) -> tuple[str, bool, str | None, str | None]:
    """
    Map a FederationQueryAsker terminal dict to (final_status,
    verified_signature, answer_excerpt, error_message).

    - success=True from the asker implies the pair-anchored Ed25519
      signature verified — asker.py's `_finalize` returns success=True
      only on that path. We pivot `verified_signature` on that flag.
    - final=None means the generator was aborted before yielding a
      terminal item (e.g., agent loop cancellation). Record as
      `unknown` so the audit row isn't dishonest.
    """
    if final is None:
        return "unknown", False, None, "No terminal result (query cancelled or aborted)"

    if final.get("success"):
        return (
            "success",
            True,
            _truncate(final.get("message") or "", MAX_ANSWER_EXCERPT_LEN),
            None,
        )

    # 🛑 Check aborted BEFORE failed. A cancelled query carries `success=False`
    # and would otherwise fall into the failure branch -- where it is filed
    # wrongly: on the responder side a cancellation can mean foreign data had
    # already been read. An access sitting among genuine failures gets skipped
    # when someone reads through the log.
    if final.get("aborted"):
        return (
            "aborted",
            False,
            None,
            _truncate(final.get("message") or "Abgebrochen", MAX_ERROR_MESSAGE_LEN),
        )

    # Failure path. The message carries the asker's reason text.
    msg = final.get("message") or "Unknown failure"
    return "failed", False, None, _truncate(msg, MAX_ERROR_MESSAGE_LEN)


async def write_federation_audit(
    *,
    user_id: int | None,
    peer_user_id: int | None,
    peer_pubkey_snapshot: str,
    peer_display_name_snapshot: str,
    query_text: str,
    initiated_at: datetime,
    final_item: dict[str, Any] | None,
    direction: str = "asked",
    identity_basis: str | None = None,
    peer_instance_name_snapshot: str | None = None,
) -> None:
    """
    Write one audit row for a completed federated query.

    Best-effort: any exception is swallowed + logged. The user has
    already received their answer by the time this is called; a DB
    blip here is not allowed to surface as a tool failure.

    user_id=None skips the write entirely — single-user / auth-disabled
    deploys don't have a meaningful asker identity to attribute, and
    a QUERY row must name its account: the database refuses one without
    (CHECK `ck_fed_query_log_user_or_revoke`; only a revocation record
    carries no `user_id`).
    """
    if user_id is None:
        # Auth-disabled / single-user deploys: no asker identity to pin
        # the row to. Debug-log once per call so operators can see why
        # `/brain/audit` stays empty on their deploy.
        logger.debug(
            "Federation audit write skipped (user_id=None — auth-disabled deploy)"
        )
        return

    final_status, verified, answer_excerpt, error_message = _classify_final(final_item)

    try:
        async with AsyncSessionLocal() as session:
            row = FederationQueryLog(
                direction=direction,
                identity_basis=identity_basis,
                user_id=user_id,
                peer_user_id=peer_user_id,
                peer_pubkey_snapshot=peer_pubkey_snapshot,
                peer_display_name_snapshot=peer_display_name_snapshot,
                # The instance part of the label as it stood at query time; a
                # snapshot like the two above, never looked up later.
                peer_instance_name_snapshot=peer_instance_name_snapshot or None,
                query_text=_truncate(query_text, MAX_QUERY_TEXT_LEN),
                initiated_at=initiated_at,
                finalized_at=datetime.now(UTC).replace(tzinfo=None),
                final_status=final_status,
                verified_signature=verified,
                answer_excerpt=answer_excerpt,
                error_message=error_message,
            )
            session.add(row)
            await session.commit()
    except Exception as e:
        logger.warning(
            f"Federation audit write failed (peer={peer_pubkey_snapshot[:12]}…, "
            f"status={final_status}): {error_text_without_content(e)}"
        )


def record_revocation(
    db: AsyncSession,
    *,
    action: str,
    basis: str,
    actor_user_id: int,
    peer: PeerUser,
    subject_user_id: int | None = None,
    link_id: int | None = None,
    links_removed: int | None = None,
    cause: str | None = None,
) -> FederationQueryLog:
    """Add the audit row for a revoked pairing or person link to `db`.

    Not committed here: the row joins the CALLER's transaction, so the
    revocation and its record are written together or not at all. A
    best-effort write in a session of its own (what `write_federation_audit`
    does for queries) could lose exactly the record of an admin acting on
    someone else's row.

    🛑 `user_id` stays NULL. The column is a foreign key onto `users.id`
    without a delete rule, and these rows are never pruned: filed under an
    account -- the affected one or the acting one -- a single revocation would
    make that account undeletable for good. Every account concerned is named in
    `query_text` instead: who acted, who owns the pairing, and the SUBJECT (the
    owner of a revoked pairing, or the account a revoked link belonged to --
    the pairing owner again when that account is gone).

    `cause` names the larger act a link revocation was part of
    (`pairing_revoked`, `pairing_revived`); absent when the link itself was
    the target.

    Ids only. `query_text` is JSON built here from integers and a boolean; the
    display-name snapshot stays empty, because that name is text the far side
    chose. The public key is the pairing's identifier, a validated hex string.
    """
    subject = subject_user_id if subject_user_id is not None else peer.circle_owner_id
    now = datetime.now(UTC).replace(tzinfo=None)
    detail: dict[str, Any] = {
        "action": action,
        "actor_user_id": actor_user_id,
        "as_admin": basis == BASIS_ADMIN,
        "owner_user_id": peer.circle_owner_id,
        "peer_id": peer.id,
        "subject_user_id": subject,
    }
    if link_id is not None:
        detail["link_id"] = link_id
    if links_removed is not None:
        detail["links_removed"] = links_removed
    if cause is not None:
        detail["cause"] = cause
    row = FederationQueryLog(
        direction=REVOCATION_DIRECTION,
        identity_basis=basis,
        user_id=None,
        peer_user_id=peer.id,
        peer_pubkey_snapshot=peer.remote_pubkey,
        peer_display_name_snapshot="",
        peer_instance_name_snapshot=None,
        query_text=json.dumps(detail, sort_keys=True, separators=(",", ":")),
        initiated_at=now,
        finalized_at=now,
        final_status=action,
        verified_signature=False,
    )
    db.add(row)
    return row


async def prune_old_audit_rows(
    retention_days: int = FEDERATION_AUDIT_RETENTION_DAYS,
) -> int:
    """
    Delete audit rows older than `retention_days`. Returns the number
    of rows removed. Called from the lifecycle hourly cleanup loop.

    Pivots on `initiated_at` rather than `finalized_at` so rows that
    never finalized (cancelled queries, bugs) are also eventually
    pruned.

    Only QUERY rows are pruned. A revocation record carries no content and is
    rare; it is the trail of who ended a pairing or a link, and it stays.
    """
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(FederationQueryLog).where(
                    FederationQueryLog.initiated_at < cutoff,
                    FederationQueryLog.direction.in_(QUERY_DIRECTIONS),
                )
            )
            await session.commit()
            deleted = result.rowcount or 0
            if deleted > 0:
                logger.info(
                    f"Federation audit retention: pruned {deleted} row(s) "
                    f"older than {retention_days} days"
                )
            return deleted
    except Exception as e:
        logger.warning(f"Federation audit retention prune failed: {error_text_without_content(e)}")
        return 0


async def list_audit_for_user(
    *,
    user_id: int,
    limit: int = 50,
    offset: int = 0,
    peer_pubkey: str | None = None,
) -> list[FederationQueryLog]:
    """
    Return the user's own audit rows, newest first. Filter by peer if
    `peer_pubkey` is given.

    Never returns rows for other users — the `WHERE user_id = ...`
    clause is mandatory. Callers pass `user_id` from `get_current_user`;
    there is no admin escape hatch.

    Queries only: the view renders every row as a question asked or answered,
    and a revocation record shown that way would be a false statement.
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(FederationQueryLog)
            .where(
                FederationQueryLog.user_id == user_id,
                FederationQueryLog.direction.in_(QUERY_DIRECTIONS),
            )
            .order_by(FederationQueryLog.initiated_at.desc())
            .limit(min(max(limit, 1), 500))
            .offset(max(offset, 0))
        )
        if peer_pubkey:
            stmt = stmt.where(
                FederationQueryLog.peer_pubkey_snapshot == peer_pubkey
            )
        result = await session.execute(stmt)
        return list(result.scalars().all())
