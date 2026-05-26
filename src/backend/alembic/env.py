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
import importlib
import os
from logging.config import fileConfig

from loguru import logger
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context


# Pre-migration bootstrap SQL — ensure `alembic_version.version_num` is wide
# enough for Renfield's human-readable revision-ID convention (some IDs run to
# ~40 chars; alembic's default column width is VARCHAR(32) which would fail
# with StringDataRightTruncationError on the first UPDATE).
#
# Two idempotent statements cover both fresh installs and pre-bootstrap legacy
# DBs:
#   1. CREATE TABLE IF NOT EXISTS with VARCHAR(64).
#   2. Conditional ALTER widening any pre-existing 32-char column.
#
# Online-mode-only: do_run_migrations() runs these against the live connection
# before context.configure(). Offline mode does NOT use them — see the comment
# in run_migrations_offline() for why the asymmetry cannot be closed without
# forking alembic.
_BOOTSTRAP_SQL: tuple[str, ...] = (
    "CREATE TABLE IF NOT EXISTS alembic_version ("
    "  version_num VARCHAR(64) NOT NULL,"
    "  CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)"
    ")",
    "DO $$ BEGIN "
    "  IF (SELECT character_maximum_length "
    "      FROM information_schema.columns "
    "      WHERE table_name='alembic_version' "
    "        AND column_name='version_num') < 64 "
    "  THEN "
    "    ALTER TABLE alembic_version "
    "    ALTER COLUMN version_num TYPE VARCHAR(64); "
    "  END IF; "
    "END $$;",
)

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

# ---------------------------------------------------------------------------
# Plugin metadata discovery (X-idra/reva#151)
# ---------------------------------------------------------------------------
#
# Plugins like Reva declare their own SQLAlchemy `Base = declarative_base()`
# so their tables live on a separate MetaData instance from Renfield's core
# `Base.metadata`. Without this block, `alembic revision --autogenerate`
# sees those tables in the live DB but not in `target_metadata`, and emits
# `drop_table` for every one of them — a latent footgun waiting for the
# day someone blindly applies that diff.
#
# Fix: each plugin can set `PLUGIN_METADATA_MODULES` to a comma-separated
# list of dotted module paths. Each module is imported for its side effects
# and, if it exposes a top-level `Base` attribute, that Base's metadata is
# appended to Alembic's `target_metadata` list. Alembic natively supports
# multiple MetaData objects via a list.
#
# Example (Reva):
#   PLUGIN_METADATA_MODULES=reva.metadata
#
# Reva's `reva.metadata` module imports every Reva model submodule as a
# side effect and re-exports `reva.core.models.Base`, which owns the full
# set of reva_* tables.
#
# If the env var is unset or the module fails to import, target_metadata
# stays exactly what it would have been — zero behaviour change for
# platform-only deploys or deploys without the env var set.
_plugin_metadatas: list = []
_plugin_modules_env = os.getenv("PLUGIN_METADATA_MODULES", "")
for _mod_spec in _plugin_modules_env.split(","):
    _spec = _mod_spec.strip()
    if not _spec:
        continue
    try:
        _plugin_mod = importlib.import_module(_spec)
    except ImportError as _exc:
        logger.warning(
            f"alembic/env.py: plugin metadata module {_spec!r} not importable: {_exc}"
        )
        continue
    _plugin_base = getattr(_plugin_mod, "Base", None)
    if _plugin_base is None:
        logger.warning(
            f"alembic/env.py: plugin metadata module {_spec!r} has no top-level 'Base' attribute"
        )
        continue
    _plugin_metadatas.append(_plugin_base.metadata)
    logger.info(
        f"alembic/env.py: registered plugin metadata from {_spec} "
        f"({len(_plugin_base.metadata.tables)} tables)"
    )

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

# Model's MetaData object(s) for 'autogenerate' support.
# Alembic accepts a single MetaData or a list of them. When no plugin
# metadata modules are configured we pass the bare MetaData to preserve
# the pre-#151 shape; when any are registered we pass a list so each
# plugin's tables are considered independently.
target_metadata = [Base.metadata, *_plugin_metadatas] if _plugin_metadatas else Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Per-migration transaction: each migration gets its own
        # transaction boundary instead of wrapping every migration in
        # one outer txn. Required for any migration that needs
        # `op.get_context().autocommit_block()` for non-transactional
        # DDL like `CREATE INDEX CONCURRENTLY`. Without this, the
        # autocommit_block raises `AssertionError: self._transaction is
        # not None` because there's no per-migration transaction to
        # commit + reopen. Surfaced 2026-05-26 when pc20260526b
        # (GIN-index migration) failed to apply on prod.
        transaction_per_migration=True,
    )

    # Offline-mode bootstrap asymmetry (PR #626 F1 — known limitation):
    # we deliberately do NOT emit _BOOTSTRAP_SQL here. Alembic in
    # offline mode unconditionally emits its own
    # `CREATE TABLE alembic_version (version_num VARCHAR(32) ...)`
    # at the top of run_migrations() — it cannot introspect to know the
    # table already exists. If we emitted our `CREATE TABLE IF NOT
    # EXISTS ... VARCHAR(64)` first, alembic's subsequent unconditional
    # CREATE would fail with "relation already exists" when the
    # operator runs the script. Emitting only the ALTER doesn't help
    # either: alembic's CREATE still bakes the wrong VARCHAR(32) into
    # the output, and our ALTER would have to run AFTER all migrations
    # finish (by which point migration INSERTs of long version_num
    # values would already have failed). The only clean fix is to
    # override alembic's version-table emission, which requires
    # forking. `alembic upgrade head --sql` is not used in this
    # project's deploy flow (online mode is the only supported path);
    # documented limitation, not a runtime hazard.
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    # Pre-migration bootstrap. See _BOOTSTRAP_SQL at module top for the
    # full rationale; this online path runs the same statements via the
    # raw connection (offline path emits them via context.execute()).
    for _stmt in _BOOTSTRAP_SQL:
        connection.exec_driver_sql(_stmt)

    # Close the SQLAlchemy autobegin that the bootstrap exec_driver_sql
    # calls above just triggered. Without this commit, the connection is
    # IN a transaction when context.configure() runs, which makes alembic
    # set `_in_external_transaction=True` — turning
    # `transaction_per_migration=True` into a silent no-op. The first
    # migration that uses `autocommit_block()` then asserts
    # `self._transaction is not None` and the entire chain fails.
    # Surfaced 2026-05-27 when pc20260526b + pc20260528 ran in the
    # alembic-upgrade Job: the env.py change shipped in PR #625 didn't
    # take effect until this commit was added. Required for any future
    # migration that uses `op.get_context().autocommit_block()`.
    #
    # FUTURE-AUTHOR WARNING (PR #626 F5): if you later wrap
    # do_run_migrations() in an explicit caller-managed transaction
    # (e.g., switch the async wrapper to `async with connectable.begin()`
    # instead of `async with connectable.connect()`), this commit() would
    # close that OUTER user-managed transaction prematurely — leaving
    # any subsequent failures uncommittable as a unit. In that scenario
    # MOVE or REMOVE this commit and rethink the bootstrap (e.g., run
    # bootstrap in its own short-lived connection before the long one).
    connection.commit()

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # See offline-mode block above for the rationale. Both paths
        # must agree on the transaction model or migrations behave
        # differently depending on how alembic is invoked.
        transaction_per_migration=True,
    )

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
        # Explicit commit — `connectable.connect()` does not auto-commit on
        # exit. Alembic's `context.begin_transaction()` inside
        # do_run_migrations runs synchronously via greenlet and its commit
        # does not propagate through the asyncpg adapter, so DDL silently
        # rolls back when the async connection closes. Without this line,
        # `alembic upgrade head` logs every migration as "Running upgrade…"
        # but nothing persists. The contrast: `engine.begin()` (used by
        # `_ensure_alembic_baseline` in services/database.py) auto-commits
        # on exit and works correctly.
        await connection.commit()

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
