"""
Datenbank Service
"""
from pathlib import Path

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from utils.config import settings

# Async Engine erstellen
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
        logger.warning(f"checkin: pg_advisory_unlock_all failed (swallowed): {type(e).__name__}: {e}")
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

#: Revision UNMITTELBAR VOR `pc20260926_baseline`. Eine frische Datenbank wird
#: hierauf gestempelt, damit `upgrade head` genau die Basis faehrt und nicht die
#: 115 Migrationen davor — die laufen aus dem Leeren ohnehin nicht durch (die
#: Wurzel `9a0d8ccea5b0` ist ein leerer `pass`-Rumpf, `rooms` legt keine
#: Migration an). `tests/backend/test_database_alembic_baseline.py` haelt fest,
#: dass dieser Wert wirklich die Vorgaengerrevision der Basis ist.
PRE_BASELINE_REVISION = "pc20260924_restore_idx"


async def _run_alembic_upgrade_head() -> None:
    """`alembic upgrade head` im selben Prozess, gegen dieselbe Datenbank."""
    import asyncio

    from alembic.config import Config

    from alembic import command

    backend_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    # Alembic laeuft synchron; in einem eigenen Thread, damit die Ereignisschleife
    # nicht blockiert.
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def _stamp_pre_baseline() -> None:
    """`alembic_version` auf die Revision VOR der Basis setzen.

    Nur fuer eine leere Datenbank. Der Unterschied zum frueheren Verhalten ist
    genau ein Wort: frueher HEAD, jetzt die Vorgaengerrevision der Basis — damit
    die Basis danach auch WIRKLICH laeuft, statt uebersprungen zu werden.
    """
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS alembic_version ("
            "version_num VARCHAR(64) NOT NULL, "
            "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
        ))
        # Idempotente Verbreiterung, aus `_ensure_alembic_baseline` uebernommen:
        # eine `alembic_version`, die aus einem frueheren Anlauf mit VARCHAR(32)
        # stehengeblieben ist, laesst das INSERT unten sonst an einer laengeren
        # Revision scheitern. Heute passt der Wert (22 Zeichen) — die Zukunft
        # nicht unnoetig zu verbauen kostet hier drei Zeilen.
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
    logger.info(f"alembic_version auf {PRE_BASELINE_REVISION} gestempelt (vor der Basis)")


async def _warn_about_tables_no_migration_created() -> None:
    """Melde Tabellen, die das MODELL kennt und die Datenbank nicht.

    Kein Abbruch: eine dunkel ausgerollte Funktion darf einen Start nicht
    verhindern. Aber sichtbar — genau die Klasse Fehler, die sonst erst als
    `UndefinedTable` mitten im Betrieb auffaellt.
    """
    try:
        import ha_glue.models.database  # noqa: F401 — fuellt Base.metadata
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
                "%d Tabelle(n) stehen im Modell, aber nicht in der Datenbank: %s. "
                "Es fehlt eine Migration — frueher hat `create_all` das beim Start "
                "still nachgeholt und damit verdeckt.",
                len(missing), ", ".join(missing[:10]),
            )
    except Exception as exc:  # pragma: no cover - eine Warnung darf nie stoeren
        logger.debug("Tabellenabgleich uebersprungen: %s", exc)


async def init_db():
    """Schema einer frischen Datenbank herstellen — ueber die MIGRATION, nicht
    ueber `create_all`.

    🛑 WARUM DAS GEAENDERT WURDE. Frueher lief hier `Base.metadata.create_all`
    und danach ein STEMPEL auf den Alembic-Kopf. Damit entstand jedes DDL, das
    eine Migration per `op.execute("CREATE INDEX …")` anlegt, auf einer
    Neuinstallation NIE — und wurde nie nachgeholt, weil der Stempel behauptete,
    die Migration sei angewandt. Nachgewiesen am 2026-09-25 an 37 Datenpunkten
    ohne eine Ausnahme: neun Indizes fehlten auf BEIDEN Instanzen, fuenf davon
    HNSW; jede semantische Suche lief als Seq Scan. #1336 hat den Bestand
    repariert, `pc20260926_baseline` behebt die Ursache.

    Jetzt: eine LEERE Datenbank wird auf die Revision VOR der Basis gestempelt
    und dann durch `alembic upgrade head` gefahren. Die Basis ist das Einzige,
    was dabei laeuft, und sie bringt beides mit — die Tabellen aus den Modellen
    UND das Roh-SQL. Verifiziert per MENGENvergleich gegen das Produktionsschema
    (nicht per Zahl): 76 Tabellen und 333 Indizes, beide Differenzen leer.

    🛑 Eine BESTEHENDE Datenbank wird hier NICHT migriert. Migrationen laufen in
    diesem Projekt als eigener Job VOR dem Rollout (`bin/deploy-production.sh
    --migrate`) — ein automatisches `upgrade` beim Start waere eine andere
    Betriebsart und genau die Art stiller Aenderung, die hier nichts zu suchen
    hat.
    """
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            fresh = (await conn.execute(text(
                "SELECT to_regclass('public.users') IS NULL"
            ))).scalar()

        if not fresh:
            # Frueher lief hier bei JEDEM Start `create_all` und legte still an,
            # was im Modell stand, aber in keiner Migration. Das Netz ist weg —
            # richtig so, denn es hat eine fehlende Migration kaschiert. Aber es
            # darf nicht in einen Laufzeitfehler bei der ersten Abfrage muenden:
            # lieber EINE laute Zeile beim Start als `UndefinedTable` irgendwann
            # mitten im Betrieb.
            await _warn_about_tables_no_migration_created()
            logger.info("Datenbank vorhanden — keine Initialisierung noetig")
            return

        logger.info("Leere Datenbank erkannt — Schema ueber die Basis-Migration")
        await _stamp_pre_baseline()
        await _run_alembic_upgrade_head()
        logger.info("✅ Schema ueber `alembic upgrade head` hergestellt")
    except Exception as e:
        logger.error(f"❌ Fehler beim Initialisieren der Datenbank: {e}")
        raise

async def get_db():
    """Dependency für FastAPI Endpoints"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
