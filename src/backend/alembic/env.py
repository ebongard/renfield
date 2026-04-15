"""
Alembic Environment Configuration

Async SQLAlchemy support for PostgreSQL migrations.

## Target metadata and the ha_glue split (Phase 1 W3)

`target_metadata = Base.metadata` drives Alembic's autogenerate diff.
After the Phase 1 Week 1-2 ha_glue extraction, HA-specific tables
(Room, RoomDevice, RoomOutputDevice, UserBleDevice, PresenceEvent,
PaperlessAuditResult, HomeAssistantEntity, CameraEvent, RadioFavorite)
live in `ha_glue.models.database` and only register with `Base.metadata`
when that module is imported.

This env.py runs on two deployment flavors:

1. **Home-automation (`ebongard/renfield`, current monorepo HA deploys)** —
   ha_glue is on disk, HA tables are in use. We want autogenerate to
   SEE the HA tables so it doesn't produce "drop tables" diffs.
2. **Platform-only (future `X-idra/renfield`, `RENFIELD_EDITION=pro`
   deploys like Reva)** — ha_glue may or may not be on disk. Pro
   deploys today have it (monorepo), but Phase 3 extracts it into a
   separate repo. We want autogenerate to ignore HA tables so the
   platform schema stays lean.

The pragmatic answer: **import ha_glue.models.database inside a
try/except**. If it's available, its classes register with Base.metadata
as a side effect and autogenerate sees them. If it's not on disk
(future platform-only repo), ImportError is swallowed and only
platform tables drive autogenerate. No env var gate needed — presence
of the package IS the flavor signal.

## Phase 3 cutover plan

When the platform repo splits from ebongard/renfield (Phase 3), the
X-idra/renfield repo will NOT have ha_glue/ on disk. The try/except
below will silently skip and target_metadata will be lean. At that
point, existing platform-only deploys that had the HA tables lingering
from monorepo history should either:
- (a) drop the 9 HA tables via a one-shot cleanup migration (cutover
  path — recommended for X-IDRA because the tables are empty per
  J2.3 audit)
- (b) accept the drift and let the HA tables sit there (no-op path —
  lowest risk, highest cruft)

The J2.3 audit (X-idra-Systems-GmbH/reva
docs/architecture/renfield-open-source-readiness.md) verified all 11
smart-home tables are EMPTY in the Reva production database. So (a)
is safe for X-IDRA's single pro deploy.
"""
import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import platform models (the 22-class Base.metadata after W1.2).
from models.database import Base
from utils.config import settings

# Conditionally register ha_glue models with Base.metadata. If ha_glue
# is on disk (monorepo or ha-glue flavor deploy), importing
# `ha_glue.models.database` as a side effect registers its 9 HA classes
# with the shared Base.metadata. If ha_glue is absent (future X-idra/
# renfield platform repo), the ImportError is swallowed and
# target_metadata stays platform-only.
#
# This single line is the Phase 1 W3 deliverable — it makes
# `alembic revision --autogenerate` and `alembic upgrade` produce the
# correct diff for each deployment flavor without any env var gating.
try:
    from ha_glue.models import database as _ha_glue_db  # noqa: F401 — side-effect registration
except ImportError:
    # Platform-only deploy — ha_glue package not installed. Autogenerate
    # will see only the 22 platform tables in Base.metadata.
    pass

# Alembic Config object
config = context.config

# Override sqlalchemy.url from settings
config.set_main_option(
    "sqlalchemy.url",
    settings.database_url.replace("postgresql://", "postgresql+asyncpg://")
)

# Interpret the config file for Python logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Model's MetaData object for 'autogenerate' support
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
