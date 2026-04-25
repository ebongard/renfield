"""
Datenbank Service
"""
from pathlib import Path

from loguru import logger
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from models.database import Base
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

# Session Factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

async def _ensure_alembic_baseline():
    """Stamp alembic_version to HEAD when the DB was just bootstrapped by create_all.

    Fresh installs create the schema directly from SQLAlchemy models — the 41-migration
    history is skipped. Stamping HEAD tells future ``alembic upgrade head`` runs that
    everything up to the current revision has been applied, so only NEW migrations run.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    # Resolve alembic.ini next to this module's package root (src/backend/alembic.ini)
    backend_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend_root / "alembic"))
    head_rev = ScriptDirectory.from_config(cfg).get_current_head()
    if not head_rev:
        logger.warning("Alembic has no head revision — skipping stamp")
        return

    async with engine.begin() as conn:
        tables = await conn.run_sync(lambda c: inspect(c).get_table_names())
        if "alembic_version" in tables:
            result = await conn.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
            if result.fetchone():
                return  # already stamped — respect existing version
        else:
            await conn.execute(text(
                "CREATE TABLE alembic_version ("
                "version_num VARCHAR(64) NOT NULL, "
                "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))"
            ))
        # Idempotent widen — covers the partial-state path: alembic_version
        # exists but is empty (e.g. prior crashed init, manual recovery ops)
        # with a pre-existing VARCHAR(32) column. The CREATE above only runs
        # when the table is fully absent. Without this ALTER the INSERT below
        # crashes with StringDataRightTruncationError on a >32-char head_rev.
        # Mirrors the same pattern in alembic/env.py (PR #462) so both
        # creation paths converge on VARCHAR(64). Postgres-only by design;
        # this function is never invoked against SQLite/test engines.
        await conn.execute(text(
            "DO $$ BEGIN "
            "  IF (SELECT character_maximum_length "
            "      FROM information_schema.columns "
            "      WHERE table_name='alembic_version' "
            "        AND column_name='version_num') < 64 "
            "  THEN ALTER TABLE alembic_version "
            "    ALTER COLUMN version_num TYPE VARCHAR(64); "
            "  END IF; "
            "END $$;"
        ))
        await conn.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:v)"),
            {"v": head_rev},
        )
    logger.info(f"✅ Alembic stamped to HEAD ({head_rev}) for fresh install")


async def init_db():
    """Datenbank initialisieren und Tabellen erstellen"""
    try:
        async with engine.begin() as conn:
            # Ensure pgvector extension exists before creating tables with vector columns
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        logger.info("✅ Datenbank-Tabellen erstellt")
        await _ensure_alembic_baseline()
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
