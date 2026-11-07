"""#658 — restart-safe reconciler for the async Paperless-commit finalize +
finalize idempotency.

The interactive commit persists a paperless_pending_finalize row before spawning
the fire-and-forget finalize; the reconciler re-runs rows still finalized_at IS
NULL past a grace, and _finalize_paperless_commit is idempotent so a re-run that
races a just-finished live task doesn't duplicate the tracking row.
"""
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.database import PaperlessPendingFinalize, PaperlessUploadTracking


def _naive_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _FakeRedis:
    """In-memory SET NX EX so the per-row lease logic is exercised sans Redis."""

    def __init__(self):
        self._keys: set = set()

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self._keys:
            return None
        self._keys.add(key)
        return True


@pytest.fixture
def session_factory(monkeypatch, db_session):
    """Bind AsyncSessionLocal to the test engine (db_session.bind) so the
    reconciler's own sessions hit the same per-test database.

    The rows the pending-finalize row points at are created here: Postgres
    enforces `user_id → users.id` and `chat_upload_id → chat_uploads.id`. The
    old note ("sqlite FKs off → no ChatUpload row needed") was true of the
    harness, not of production — the reconciler always runs against real
    constraints."""
    import services.database as db_mod
    smk = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "AsyncSessionLocal", smk)
    return smk


@pytest.fixture(autouse=True)
async def _referenced_rows(db_session):
    """User 1 and chat upload 1 — what every `_mk` row below points at."""
    from models.database import ChatUpload
    from tests.backend.dbrows import ensure_user

    await ensure_user(db_session, 1)
    existing = await db_session.get(ChatUpload, 1)
    if existing is None:
        db_session.add(ChatUpload(
            id=1, session_id="s", filename="doc.pdf", status="completed",
        ))
    await db_session.commit()


async def _mk(smk, **kw) -> int:
    defaults = dict(
        task_id="task-x", chat_upload_id=1, user_id=1, session_id="s",
        filename="doc.pdf", deferred_patch={"created_date": "2026-01-01"},
        original_metadata={"title": "Doc"}, created_note="", doc_text=None,
        attempts=0, finalized_at=None, created_at=_naive_now(),
    )
    defaults.update(kw)
    async with smk() as db:
        row = PaperlessPendingFinalize(**defaults)
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


@pytest.mark.database
class TestFinalizeReconciler:
    async def test_reruns_only_unfinalized_past_grace(self, session_factory):
        from services import paperless_finalize_reconciler as rec
        old = _naive_now() - timedelta(hours=1)
        recent = _naive_now() - timedelta(seconds=5)
        old_id = await _mk(session_factory, created_at=old)                       # re-run
        await _mk(session_factory, created_at=recent)                             # within grace → skip
        await _mk(session_factory, created_at=old, finalized_at=_naive_now())     # done → skip

        fin = AsyncMock()
        with patch.object(rec, "get_redis", return_value=_FakeRedis()), \
             patch("services.paperless_commit_tool._finalize_paperless_commit", fin):
            await rec.reconcile_pending_finalizes(mcp_manager=MagicMock())

        assert fin.await_count == 1
        assert fin.await_args.kwargs["pending_finalize_id"] == old_id
        async with session_factory() as db:
            assert (await db.get(PaperlessPendingFinalize, old_id)).attempts == 1

    async def test_gives_up_after_max_attempts(self, session_factory):
        from services import paperless_finalize_reconciler as rec
        from utils.config import settings
        stuck_id = await _mk(
            session_factory,
            created_at=_naive_now() - timedelta(hours=1),
            attempts=settings.paperless_finalize_reconciler_max_attempts,
        )
        fin = AsyncMock()
        with patch.object(rec, "get_redis", return_value=_FakeRedis()), \
             patch("services.paperless_commit_tool._finalize_paperless_commit", fin):
            await rec.reconcile_pending_finalizes(mcp_manager=MagicMock())

        assert fin.await_count == 0  # not re-run
        async with session_factory() as db:
            assert (await db.get(PaperlessPendingFinalize, stuck_id)).finalized_at is not None

    async def test_pending_refunds_attempt(self, session_factory):
        """A still-consuming ("pending") re-run must NOT burn the error budget:
        the attempt is pre-counted then refunded, and the row stays open."""
        from services import paperless_finalize_reconciler as rec
        row_id = await _mk(
            session_factory, created_at=_naive_now() - timedelta(hours=1), attempts=0,
        )
        fin = AsyncMock(return_value="pending")
        with patch.object(rec, "get_redis", return_value=_FakeRedis()), \
             patch("services.paperless_commit_tool._finalize_paperless_commit", fin):
            await rec.reconcile_pending_finalizes(mcp_manager=MagicMock())

        assert fin.await_count == 1
        async with session_factory() as db:
            pf = await db.get(PaperlessPendingFinalize, row_id)
            assert pf.attempts == 0          # pre-incremented to 1, refunded to 0
            assert pf.finalized_at is None   # still open → next pass retries

    async def test_aged_out_pending_gives_up_without_rerun(self, session_factory):
        """A row unfinalized past the wall-clock backstop is closed loudly even
        with attempts well under max (a forever-"pending" document)."""
        from services import paperless_finalize_reconciler as rec
        from utils.config import settings
        row_id = await _mk(
            session_factory,
            created_at=_naive_now()
            - timedelta(hours=settings.paperless_finalize_reconciler_giveup_hours + 1),
            attempts=0,
        )
        fin = AsyncMock()
        with patch.object(rec, "get_redis", return_value=_FakeRedis()), \
             patch("services.paperless_commit_tool._finalize_paperless_commit", fin):
            await rec.reconcile_pending_finalizes(mcp_manager=MagicMock())

        assert fin.await_count == 0  # aged out → not re-run
        async with session_factory() as db:
            assert (await db.get(PaperlessPendingFinalize, row_id)).finalized_at is not None

    async def test_noop_when_mcp_manager_none(self, session_factory):
        from services import paperless_finalize_reconciler as rec
        await _mk(session_factory, created_at=_naive_now() - timedelta(hours=1))
        fin = AsyncMock()
        with patch("services.paperless_commit_tool._finalize_paperless_commit", fin):
            await rec.reconcile_pending_finalizes(mcp_manager=None)
        assert fin.await_count == 0

    async def test_finalize_idempotent_and_marks_row(self, session_factory):
        """_finalize writes exactly ONE tracking row + stamps finalized_at; a
        second run (reconciler re-run) does not duplicate it."""
        from services import paperless_commit_tool as pct
        pf_id = await _mk(session_factory)
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={"success": True})
        with patch("services.chat_upload_tool._poll_paperless_task",
                   AsyncMock(return_value=(None, 42))), \
             patch("services.chat_upload_tool._bump_confirms_used", AsyncMock()):
            for _ in range(2):  # run twice → must be idempotent
                await pct._finalize_paperless_commit(
                    task_id="task-x",
                    deferred_patch={"created_date": "2026-01-01"},
                    user_approved={"title": "Doc"},
                    attachment_id=1, user_id=1, session_id=None,
                    filename="doc.pdf", created_note="", doc_text=None,
                    mcp_manager=mcp, pending_finalize_id=pf_id, poll_timeout_s=1.0,
                )
        async with session_factory() as db:
            tracks = (await db.execute(
                select(PaperlessUploadTracking).where(
                    PaperlessUploadTracking.chat_upload_id == 1
                )
            )).scalars().all()
            assert len(tracks) == 1  # no duplicate
            assert (await db.get(PaperlessPendingFinalize, pf_id)).finalized_at is not None

    async def test_a_refused_announcement_keeps_the_filing(self, session_factory, db_session):
        """The conversation was deleted before the consume finished (#1485).
        The announcement is refused -- and the filing must survive that: a
        refused `save_message` rolls its session back, and when it shared the
        filing's session the tracking row and the finalize stamp went with it,
        so the reconciler re-ran the finalize on every pass."""
        from models.database import Conversation
        from services import paperless_commit_tool as pct
        from services.conversation_service import ConversationService

        await ConversationService(db_session).save_message("weg-paperless", "user", "Ablegen")
        conv = (await db_session.execute(
            select(Conversation).where(Conversation.session_id == "weg-paperless")
        )).scalar_one()
        await db_session.delete(conv)
        await db_session.commit()
        pf_id = await _mk(session_factory, session_id="weg-paperless")
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={"success": True})
        with patch("services.chat_upload_tool._poll_paperless_task",
                   AsyncMock(return_value=(None, 42))), \
             patch("services.chat_upload_tool._bump_confirms_used", AsyncMock()), \
             patch("api.websocket.shared.notify_session", AsyncMock()):
            status = await pct._finalize_paperless_commit(
                task_id="task-x",
                deferred_patch={},
                user_approved={"title": "Doc"},
                attachment_id=1, user_id=1, session_id="weg-paperless",
                filename="doc.pdf", created_note="", doc_text=None,
                mcp_manager=mcp, pending_finalize_id=pf_id, poll_timeout_s=1.0,
            )

        assert status == "completed"
        async with session_factory() as db:
            assert (await db.execute(
                select(PaperlessUploadTracking.id)
                .where(PaperlessUploadTracking.chat_upload_id == 1)
            )).first() is not None
            assert (await db.get(PaperlessPendingFinalize, pf_id)).finalized_at is not None
            assert (await db.execute(
                select(Conversation).where(Conversation.session_id == "weg-paperless")
            )).scalar_one_or_none() is None

    async def test_a_failing_announcement_keeps_the_filing_and_the_push(
        self, session_factory,
    ):
        """A transient failure of the announcement write (not a refusal) costs
        the persisted message only: the filing is stamped, the live push goes out."""
        from services import paperless_commit_tool as pct
        from services.conversation_service import ConversationService

        pf_id = await _mk(session_factory, session_id="kaputt-paperless")
        mcp = MagicMock()
        mcp.execute_tool = AsyncMock(return_value={"success": True})
        push = AsyncMock()
        with patch("services.chat_upload_tool._poll_paperless_task",
                   AsyncMock(return_value=(None, 42))), \
             patch("services.chat_upload_tool._bump_confirms_used", AsyncMock()), \
             patch.object(ConversationService, "save_message",
                          AsyncMock(side_effect=RuntimeError("db gone"))), \
             patch("api.websocket.shared.notify_session", push):
            status = await pct._finalize_paperless_commit(
                task_id="task-x",
                deferred_patch={},
                user_approved={"title": "Doc"},
                attachment_id=1, user_id=1, session_id="kaputt-paperless",
                filename="doc.pdf", created_note="", doc_text=None,
                mcp_manager=mcp, pending_finalize_id=pf_id, poll_timeout_s=1.0,
            )

        assert status == "completed"
        async with session_factory() as db:
            assert (await db.get(PaperlessPendingFinalize, pf_id)).finalized_at is not None
            assert (await db.execute(
                select(PaperlessUploadTracking.id)
                .where(PaperlessUploadTracking.chat_upload_id == 1)
            )).first() is not None
        push.assert_awaited_once()
