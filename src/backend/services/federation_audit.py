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
    `prune_old_audit_rows()` deletes query rows older than
    `FEDERATION_AUDIT_RETENTION_DAYS` (90) and revocation records older
    than `FEDERATION_REVOCATION_RETENTION_DAYS` (default 730, #1558).
    Called from the scheduled-task engine.
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import case, cast, delete, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationQueryLog, PeerUser, User
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

#: Every code a row can carry. `outcome_code` knows no other: the column also
#: holds free failure text -- on asker rows the far side's own message -- and
#: a text that merely LOOKS like `[word] ...` must be neither read as a code
#: nor have its head cut off.
OUTCOME_GONE = "gone"
OUTCOME_PAIRING_REVOKED = "pairing_revoked"
OUTCOME_LINK_REVOKED = "link_revoked"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_NOT_STORED = "not_stored"
OUTCOME_UNEXPANDED = "graph_expansion_skipped"
OUTCOME_CODES = frozenset({
    OUTCOME_GONE, OUTCOME_PAIRING_REVOKED, OUTCOME_LINK_REVOKED,
    OUTCOME_CANCELLED, OUTCOME_NOT_STORED, OUTCOME_UNEXPANDED,
})
OUTCOME_STAGES = frozenset({STAGE_BEFORE_READ, STAGE_AFTER_READ, STAGE_AFTER_ANSWER})

_OUTCOME_NOTE = re.compile(r"^\[([a-z_]+)(?:/([a-z_]+))?\] ")


def outcome_note(code: str, sentence: str, *, stage: str | None = None) -> str:
    """`sentence`, led by its code (and stage) in machine-readable form."""
    if code not in OUTCOME_CODES or (stage is not None and stage not in OUTCOME_STAGES):
        raise ValueError(f"unknown outcome code or stage: {code!r}/{stage!r}")
    return f"[{code}/{stage}] {sentence}" if stage else f"[{code}] {sentence}"


def _known_note(text: str | None) -> re.Match[str] | None:
    match = _OUTCOME_NOTE.match(text or "")
    if match is None or match.group(1) not in OUTCOME_CODES:
        return None
    if match.group(2) is not None and match.group(2) not in OUTCOME_STAGES:
        return None
    return match


def outcome_code(text: str | None) -> tuple[str, str | None] | None:
    """`(code, stage)` of a text written by `outcome_note` -- `None` for any
    other text (a plain failure message, a row from before 2026-11-16, a text
    in brackets that is not one of the known codes)."""
    match = _known_note(text)
    return (match.group(1), match.group(2)) if match else None


def outcome_sentence(text: str | None) -> str | None:
    """`text` without a leading KNOWN code -- what a person is shown. Any
    other text comes back unchanged."""
    match = _known_note(text)
    return text[match.end():] if match and text else text


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
            # A delivered answer can still carry a NOTE about how it came
            # about (`outcome_note`, e.g. made without graph expansion). It
            # shares the column with the error text; the status says which
            # of the two a row holds.
            _truncate(final.get("note") or None, MAX_ERROR_MESSAGE_LEN),
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
    request_id: str | None = None,
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
                # The responder names its request so the row can be found
                # again (`mark_answer_not_handed_over`).
                request_id=request_id,
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


async def mark_answer_not_handed_over(*, request_id: str, error_message: str) -> bool:
    """Correct the responder's row of `request_id` from "answered" to
    "answer made, NOT handed over". Returns whether a row was corrected.

    The responder writes its `success` row when the answer is ready. If the
    pairing or the link is revoked before the far side collects it, the
    answer is refused at the poll -- and the log would still say it was
    delivered. The EXISTING row is corrected rather than a second one
    written: the view lists one entry per query, and two rows would show
    the same question once as answered and once as not.

    The shape is the one an `aborted` access already has (`_classify_final`):
    status `aborted`, no answer excerpt, not verified, the reason in
    `error_message`. An access that happened and was not delivered is not a
    failure.

    🛑 IDEMPOTENT, and it has to be: the far side polls about twice a second.
    The update only matches a row that still says `success`, so the second
    call changes nothing and returns False.

    Best-effort like every write here: a refusal must not fail because its
    record could not be written.
    """
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                update(FederationQueryLog)
                .where(
                    FederationQueryLog.request_id == request_id,
                    FederationQueryLog.direction == "answered",
                    FederationQueryLog.final_status == "success",
                )
                .values(
                    final_status="aborted",
                    verified_signature=False,
                    answer_excerpt=None,
                    error_message=_truncate(error_message, MAX_ERROR_MESSAGE_LEN),
                    finalized_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
            await session.commit()
            return bool(result.rowcount)
    except Exception as e:
        logger.warning(
            f"Federation audit correction failed (request_id={request_id}): "
            f"{error_text_without_content(e)}"
        )
        return False


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
    The instance-name snapshot is the far instance's VERIFIED name
    (`remote_instance_name`, written only from a signed attachment), as it
    stood at the revocation -- the one thing of the far side the revocation
    view shows (#1558). NULL when none was verified.
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
        peer_instance_name_snapshot=peer.remote_instance_name or None,
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
    revocation_retention_days: int | None = None,
) -> int:
    """
    Delete audit rows past their retention. Returns the number of rows
    removed. Called from the scheduled-task engine.

    Pivots on `initiated_at` rather than `finalized_at` so rows that
    never finalized (cancelled queries, bugs) are also eventually
    pruned.

    Two periods, one per kind of row:
    * QUERY rows (`asked`/`answered`) after `retention_days` (90): they
      carry the question and an excerpt of the answer.
    * REVOCATION records after `revocation_retention_days` (default
      `FEDERATION_REVOCATION_RETENTION_DAYS`, 730; #1558). They carry no
      content and are rare -- the trail of who ended a pairing or a link,
      kept long enough to answer that question, not forever.
    No other direction exists; one added later is pruned by neither.
    """
    if revocation_retention_days is None:
        from utils.config import settings

        revocation_retention_days = settings.federation_revocation_retention_days
    now = datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(days=retention_days)
    revocation_cutoff = now - timedelta(days=revocation_retention_days)
    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                delete(FederationQueryLog).where(
                    FederationQueryLog.initiated_at < cutoff,
                    FederationQueryLog.direction.in_(QUERY_DIRECTIONS),
                )
            )
            revoked = await session.execute(
                delete(FederationQueryLog).where(
                    FederationQueryLog.initiated_at < revocation_cutoff,
                    FederationQueryLog.direction == REVOCATION_DIRECTION,
                )
            )
            await session.commit()
            deleted = (result.rowcount or 0) + (revoked.rowcount or 0)
            if deleted > 0:
                logger.info(
                    f"Federation audit retention: pruned {result.rowcount or 0} query "
                    f"row(s) older than {retention_days} days and "
                    f"{revoked.rowcount or 0} revocation record(s) older than "
                    f"{revocation_retention_days} days"
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


# --- Revocation records, for the people they concern (#1558) -----------------


def _person_name(user: User | None) -> str | None:
    """How a local account is named in the view; `None` = the account is gone."""
    if user is None:
        return None
    full = " ".join(p.strip() for p in (user.first_name, user.last_name) if p and p.strip())
    return full or user.username


async def list_revocations_for_user(
    *,
    user_id: int,
    is_admin: bool,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The revocation records `user_id` may see, newest first.

    A record concerns two accounts: the SUBJECT (whose link was removed, or
    the owner of a revoked pairing) and the ACTOR (who removed it). Both see
    it; an `admin` holder sees every record. Nobody else sees any -- the
    filter is in the SQL, on the ids `record_revocation` wrote, not on a
    list read back in full.

    🛑 Revocation records ONLY (`direction = 'revoke'`), never a query row:
    `list_audit_for_user` is the mirror image and shows queries only. The
    two must not mix -- a revocation shown as a question would be a false
    statement, and a query shown here would hand its question to an admin,
    which the query trail never does.

    Ids are resolved to names HERE, server-side; the response carries no id
    of another account. Of the far side only the verified instance name
    snapshotted at the revocation -- not the far person's self-chosen name,
    not the key, not today's name for an old record.
    """
    # 🛑 Behind a CASE: Postgres does not promise to test `direction` before
    # the cast, and a query row's text is no JSON -- a bare cast could fail
    # the whole statement on any query row in the table.
    detail = case(
        (FederationQueryLog.direction == REVOCATION_DIRECTION,
         cast(FederationQueryLog.query_text, JSONB)),
        else_=None,
    )
    me = str(user_id)
    stmt = (
        select(FederationQueryLog)
        .where(FederationQueryLog.direction == REVOCATION_DIRECTION)
        .order_by(FederationQueryLog.initiated_at.desc(), FederationQueryLog.id.desc())
        .limit(min(max(limit, 1), 500))
        .offset(max(offset, 0))
    )
    if not is_admin:
        stmt = stmt.where(or_(
            detail["subject_user_id"].astext == me,
            detail["actor_user_id"].astext == me,
        ))
    async with AsyncSessionLocal() as session:
        rows = list((await session.execute(stmt)).scalars().all())
        details = [json.loads(r.query_text) for r in rows]
        ids = {
            d[k] for d in details
            for k in ("subject_user_id", "actor_user_id", "owner_user_id")
            if isinstance(d.get(k), int)
        }
        users = {}
        if ids:
            users = {
                u.id: u for u in (await session.execute(
                    select(User).where(User.id.in_(ids))
                )).scalars().all()
            }

    def _name(d: dict[str, Any], key: str) -> str | None:
        uid = d.get(key)
        return _person_name(users.get(uid)) if isinstance(uid, int) else None

    return [
        {
            "id": row.id,
            "at": row.initiated_at.isoformat() if row.initiated_at else "",
            "action": row.final_status,
            "cause": d.get("cause"),
            "basis": row.identity_basis,
            "links_removed": d.get("links_removed"),
            "subject_name": _name(d, "subject_user_id"),
            "actor_name": _name(d, "actor_user_id"),
            "owner_name": _name(d, "owner_user_id"),
            "subject_is_you": d.get("subject_user_id") == user_id,
            "actor_is_you": d.get("actor_user_id") == user_id,
            "peer_instance_name": row.peer_instance_name_snapshot,
        }
        for row, d in zip(rows, details, strict=True)
    ]
