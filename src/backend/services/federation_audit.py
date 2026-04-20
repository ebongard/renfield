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

from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import delete, select

from models.database import FederationQueryLog
from services.database import AsyncSessionLocal

# Per-column truncation caps. Chosen to keep a single audit row well
# under 4 KB on disk (Postgres TOAST threshold, though TEXT isn't
# strictly bounded by this — the cap is a UX choice, not a storage one).
MAX_QUERY_TEXT_LEN = 1000
MAX_ANSWER_EXCERPT_LEN = 2000
MAX_ERROR_MESSAGE_LEN = 500

FEDERATION_AUDIT_RETENTION_DAYS = 90


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
) -> None:
    """
    Write one audit row for a completed federated query.

    Best-effort: any exception is swallowed + logged. The user has
    already received their answer by the time this is called; a DB
    blip here is not allowed to surface as a tool failure.

    user_id=None skips the write entirely — single-user / auth-disabled
    deploys don't have a meaningful asker identity to attribute, and
    the `user_id` column on the table is NOT NULL by design (scoping
    audit rows unambiguously).
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
                user_id=user_id,
                peer_user_id=peer_user_id,
                peer_pubkey_snapshot=peer_pubkey_snapshot,
                peer_display_name_snapshot=peer_display_name_snapshot,
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
    except Exception as e:  # noqa: BLE001 — best-effort write
        logger.warning(
            f"Federation audit write failed (peer={peer_pubkey_snapshot[:12]}…, "
            f"status={final_status}): {e}"
        )


async def prune_old_audit_rows(
    retention_days: int = FEDERATION_AUDIT_RETENTION_DAYS,
) -> int:
    """
    Delete audit rows older than `retention_days`. Returns the number
    of rows removed. Called from the lifecycle hourly cleanup loop.

    Pivots on `initiated_at` rather than `finalized_at` so rows that
    never finalized (cancelled queries, bugs) are also eventually
    pruned.
    """
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=retention_days)
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(FederationQueryLog).where(
                    FederationQueryLog.initiated_at < cutoff
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
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Federation audit retention prune failed: {e}")
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
    """
    async with AsyncSessionLocal() as session:
        stmt = (
            select(FederationQueryLog)
            .where(FederationQueryLog.user_id == user_id)
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
