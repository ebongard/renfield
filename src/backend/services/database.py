"""
Datenbank Service
"""
from pathlib import Path

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from utils.config import settings
from utils.log_safety import error_text_without_content

# Create the async engine
engine = create_async_engine(
    settings.database_url.replace("postgresql://", "postgresql+asyncpg://"),
    echo=False,
    future=True,
    pool_size=settings.db_pool_size,
    max_overflow=settings.db_max_overflow,
    pool_recycle=settings.db_pool_recycle,
    pool_pre_ping=True,
)


# Defensive cleanup: pg_advisory_lock is session-level (per-connection),
# not transaction-level. ROLLBACK does NOT release it. If application code
# fails to call pg_advisory_unlock before the connection returns to the
# pool, the lock leaks and every subsequent caller for the same key
# blocks forever on pg_advisory_lock waiting for nobody. This was
# observed in prod on 2026-05-14 — three sessions queued on user_id=8
# for 20+ minutes after a single v2-shadow-mode path failed mid-flight,
# stalling all memory extraction for that user.
#
# Fix: release every advisory lock at connection checkin. Several features now
# take advisory locks, and ALL of them acquire on a DEDICATED connection they
# hold + explicitly unlock for the lock's whole scope (the KG reconciler
# 0x4B47, the obligation notifier/digest/calendar 0x4F42/0x4F43/0x4F44, the
# Schicht-A fact-override reindex 0x5341, the Scheduled Tasks engine 0x5354,
# and the KB document-dedupe detector 0x4444),
# so this checkin sweep only ever fires as a backstop against a leaked lock —
# never against a live one on the pooled work session. A future feature that
# takes a SESSION-level advisory lock on a POOLED session (not a dedicated
# connection) would have it dropped here mid-scope; keep using the
# dedicated-connection pattern for those.
#
# EXCEPTION, safe by construction — TRANSACTION-scoped locks on the pooled work
# session: the v2 memory extract (0x4D454D30 "MEM0",
# conversation_memory_service._acquire_user_lock_xact) and the scanner-job
# delivery guard (0x534A) use pg_advisory_xact_lock. Those are released by
# COMMIT/ROLLBACK — always before checkin — and pg_advisory_unlock_all()
# releases only SESSION-level locks, so this sweep can neither drop nor leak
# them. The memory path is also the feature the 2026-05-14 incident above was
# about: it held a session-level lock on the pooled session, and in 2026-09 it
# deadlocked outright (5-7/h) because that lock could be released while the row
# locks it was meant to guard still stood. Converting it to a transaction-scoped
# lock is what removed the cycle.
@event.listens_for(engine.sync_engine, "checkin")
def _release_leaked_advisory_locks_on_checkin(dbapi_connection, connection_record):
    """Release any held advisory locks when a connection returns to the pool.

    Best-effort and MUST NEVER raise — a checkin hook that throws breaks pool
    return for every caller. A connection checked in after its event loop has
    closed (e.g. across async-test boundaries) can have a None/dead
    ``dbapi_connection``, so cursor creation itself is inside the guard.
    """
    if dbapi_connection is None:
        return
    cursor = None
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SELECT pg_advisory_unlock_all();")
    except Exception as e:
        # Worst case is the original leak symptom comes back, visible in logs.
        logger.warning(f"checkin: pg_advisory_unlock_all failed (swallowed): {type(e).__name__}: {error_text_without_content(e)}")
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        # Close the transaction the reset SELECT opened. SQLAlchemy's asyncpg
        # DBAPI adapter is NOT autocommit — it lazily opens a real transaction
        # for the SELECT above and relies on an explicit commit/rollback to
        # close it. reset_on_return has already fired (closing the work
        # session's txn) before this checkin hook runs, so nothing else closes
        # this one. Without the rollback the connection returns to the pool
        # `idle in transaction` (last query pg_advisory_unlock_all()) holding a
        # virtualxid lock; a rarely-reused overflow connection can sit wedged
        # for hours — long enough to block a CREATE INDEX CONCURRENTLY migration
        # (observed on xidra 2026-08-29: two connections idle ~4.5 h stalled the
        # FTS index build ~27 min). pg_advisory_unlock_all() is session-level
        # and non-transactional, so this rollback does NOT re-acquire the locks.
        # Must never raise — a throwing checkin hook breaks pool return.
        try:
            dbapi_connection.rollback()
        except Exception as e:
            logger.warning(f"checkin: post-unlock rollback failed (swallowed): {type(e).__name__}: {e}")


# Session Factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

#: The revision IMMEDIATELY BEFORE `pc20260926_baseline`. A fresh database is
#: stamped to this so `upgrade head` runs exactly the baseline and not the 115
#: migrations before it -- those do not run from empty anyway (the root
#: `9a0d8ccea5b0` is an empty `pass` body, and no migration creates
#: `rooms`). `tests/backend/test_database_alembic_baseline.py` pins down
#: that this value really is the baseline's predecessor revision.
PRE_BASELINE_REVISION = "pc20260924_restore_idx"


async def _run_alembic_upgrade_head() -> None:
    """`alembic upgrade head` in the same process, against the same database."""
    import asyncio

    from alembic.config import Config

    from alembic import command

    backend_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    # Alembic runs synchronously; in a thread of its own so the event loop is
    # not blocked.
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def _stamp_pre_baseline() -> None:
    """Set `alembic_version` to the revision BEFORE the baseline.

    For an empty database only. The difference from the earlier behaviour is
    exactly one word: HEAD before, now the baseline's predecessor -- so the
    baseline afterwards REALLY runs instead of being skipped.
    """
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(64) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
        # Idempotent widening, carried over from `_ensure_alembic_baseline`:
        # an `alembic_version` left behind by an earlier run with VARCHAR(32)
        # would otherwise make the INSERT below fail on a longer revision. The
        # current value fits (22 characters) -- not boxing in the future costs
        # three lines here.
        await conn.execute(text(
            "DO $$ BEGIN "
            "  IF (SELECT character_maximum_length FROM information_schema.columns "
            "      WHERE table_name='alembic_version' AND column_name='version_num') < 64 "
            "  THEN ALTER TABLE alembic_version "
            "    ALTER COLUMN version_num TYPE VARCHAR(64); "
            "  END IF; "
            "END $$;"
        ))
        existing = (await conn.execute(text(
            "SELECT version_num FROM alembic_version LIMIT 1"
        ))).first()
        if existing:
            return
        await conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
                           {"v": PRE_BASELINE_REVISION})
    logger.info(f"alembic_version stamped to {PRE_BASELINE_REVISION} (before the baseline)")


async def _warn_about_tables_no_migration_created() -> None:
    """Report tables the MODEL knows and the database does not.

    Does not abort: a dark-shipped feature must not prevent a start. But
    visible -- exactly the class of bug that otherwise surfaces only as
    `UndefinedTable` in the middle of operation.
    """
    try:
        import ha_glue.models.database  # noqa: F401 — populates Base.metadata
        from models.database import Base

        async with engine.begin() as conn:
            rows = await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ))
            live = {r[0] for r in rows}
        missing = sorted(set(Base.metadata.tables) - live)
        if missing:
            logger.warning(
                "{} table(s) are in the model but not in the database: {}. "
                "A migration is missing -- `create_all` used to fill this in "
                "silently at startup and thereby hid it.",
                len(missing), ", ".join(missing[:10]),
            )
    except Exception as exc:  # pragma: no cover - a warning must never disrupt
        logger.debug("Tabellenabgleich uebersprungen: {}", exc)


async def init_db():
    """Create the schema of a fresh database -- via the MIGRATION, not
    via `create_all`.

    🛑 WHY THIS WAS CHANGED. This used to run `Base.metadata.create_all`
    followed by a STAMP onto the Alembic head. As a result every piece of DDL
    a migration creates via `op.execute("CREATE INDEX ...")` NEVER came into
    existence on a fresh install -- and was never filled in, because the stamp
    claimed the migration had been applied. Demonstrated on 2026-09-25 across
    37 data points without a single exception: nine indexes were missing on
    BOTH instances, five of them HNSW; every semantic search ran as a Seq Scan.
    #1336 repaired the existing stock, and `pc20260926_baseline` fixes the
    cause.

    Now: an EMPTY database is stamped to the revision BEFORE the baseline and
    then run through `alembic upgrade head`. The baseline is the only thing
    that runs, and it brings both -- the tables from the models AND the raw
    SQL. Verified by SET comparison against the production schema (not by
    count): 76 tables and 333 indexes, both differences empty.

    🛑 An EXISTING database is NOT migrated here. In this project migrations run
    as their own job BEFORE the rollout (`bin/deploy-production.sh --migrate`)
    -- an automatic `upgrade` at startup would be a different operating mode,
    and exactly the kind of silent change that has no place here.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            fresh = (await conn.execute(text(
                "SELECT to_regclass('public.users') IS NULL"
            ))).scalar()

        if not fresh:
            # `create_all` used to run here on EVERY start and silently
            # create whatever was in the model but in no migration. That net is
            # gone -- rightly so, because it masked a missing migration. But it
            # must not turn into a runtime error on the first query:
            # better ONE loud line at startup than `UndefinedTable` at some
            # point in the middle of operation.
            await _warn_about_tables_no_migration_created()
            logger.info("Datenbank vorhanden — keine Initialisierung noetig")
            return

        logger.info("Empty database detected -- schema via the baseline migration")
        await _stamp_pre_baseline()
        await _run_alembic_upgrade_head()
        logger.info("✅ Schema ueber `alembic upgrade head` hergestellt")
    except Exception as e:
        logger.error(f"❌ Failed to initialise the database: {error_text_without_content(e)}")
        raise

async def get_db():
    """Dependency for FastAPI endpoints"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
