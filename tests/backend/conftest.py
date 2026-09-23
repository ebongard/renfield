"""
Pytest Fixtures für Renfield Backend Tests

Bietet:
- In-Memory SQLite Datenbank für isolierte Tests
- Mock-Services für externe Abhängigkeiten
- FastAPI TestClient für API-Tests
- Async Support
"""

from collections.abc import AsyncGenerator
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import os as _os
import pytest
from httpx import ASGITransport, AsyncClient

from sqlalchemy import text as _sa_text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# The sqlite compatibility shims that used to live here (TSVECTOR→TEXT,
# BigInteger→INTEGER, a Python `to_tsvector` UDF) are GONE: the test
# database is real Postgres, so the production column types compile as
# themselves and the real functions run.

# Renfield Imports
from models.database import (
    DEFAULT_CAPABILITIES,
    DEVICE_TYPE_SATELLITE,
    DEVICE_TYPE_WEB_BROWSER,
    Base,
    Conversation,
    Document,
    KnowledgeBase,
    Message,
    Role,
    Room,
    RoomDevice,
    Speaker,
    User,
)

# ============================================================================
# Rate-limit isolation
# ============================================================================

# slowapi keeps an in-memory counter keyed by client IP across the test
# session. A long suite that includes many @limiter.limit(...)-decorated
# routes (skills/, tool-health/, trajectories/, ...) accumulates past the
# admin/chat limits and starts returning 429 in tests that were previously
# under-budget. Reset between each test so per-test assertions stay
# deterministic regardless of execution order.
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    try:
        from services.api_rate_limiter import limiter
    except ImportError:
        yield
        return
    try:
        limiter.reset()
    except Exception:
        pass
    yield
    try:
        limiter.reset()
    except Exception:
        pass


# ============================================================================
# Redis client isolation
# ============================================================================

# The backend caches its async Redis clients per PROCESS (services.redis_client
# ._client, used by login_lockout/task_queue/...; TokenBlacklist._redis). In
# production that is one client on one event loop for the process lifetime. The
# suite, however, runs every test on a FRESH function-scoped loop, so a client
# built in test A's loop carries a pooled connection bound to a loop that is
# already closed by the time test B borrows it — the command raises "Event loop
# is closed". That surfaces as an unrelated-looking failure wherever a Redis
# error is handled rather than raised: get_current_user's blacklist check fails
# CLOSED and returns 401, so an authenticated request in test B gets rejected
# purely because test A opened the connection.
#
# Drop the cached clients around every test so each one builds its own on the
# loop it actually runs on. Teardown is best-effort: a test may have swapped in
# a fake, and closing a client whose loop is gone is itself allowed to fail.
@pytest.fixture(autouse=True)
def _reset_shared_redis_clients():
    def _drop():
        try:
            from services import redis_client as _rc
        except ImportError:
            pass
        else:
            _rc._client = None
        # (TokenBlacklist now delegates to the shared services.redis_client
        # pool — resetting _rc._client above covers it.)

    _drop()
    yield
    _drop()


# ============================================================================
# Database Fixtures
# ============================================================================

# The test database is REAL POSTGRES, never sqlite.
#
# A sqlite harness forces dialect fallbacks into production code — the circle
# filter is Postgres SQL (`::text` / `::int` casts), the branching walk is a
# recursive CTE, `search_vector` is a GENERATED column — and a fallback is
# untested branch code that hides the behaviour that actually ships. A green
# run against sqlite proves the wrong system. A Postgres test database is cheap
# and equals production.
#
# `RENFIELD_TEST_PG_URL` points at it (on the build box: the dedicated
# `renfield_test` database, NEVER a live one). Without it the database fixtures
# skip rather than quietly fall back to sqlite.


def _pg_url_or_skip() -> str:
    dsn = _os.environ.get("RENFIELD_TEST_PG_URL")
    if not dsn:
        pytest.skip(
            "RENFIELD_TEST_PG_URL not set — the test database is real Postgres "
            "(see the Testing section in CLAUDE.md); sqlite is not a fallback."
        )
    if dsn.startswith("postgresql://"):
        dsn = dsn.replace("postgresql://", "postgresql+asyncpg://", 1)
    return dsn



_SCHEMA_READY = False


# Which tables exist RIGHT NOW, asked of the catalogue — never of
# `Base.metadata`: the metadata grows as modules import, so a list built at
# first use names tables `create_all` had not seen yet ("relation camera_events
# does not exist").
_EXISTING_TABLES_SQL = """
    SELECT tablename FROM pg_tables WHERE schemaname = 'public'
"""

# Sequences whose counter has moved (`last_value IS NOT NULL` means "called").
_USED_SEQUENCES_SQL = """
    SELECT sequencename FROM pg_sequences
     WHERE schemaname = 'public' AND last_value IS NOT NULL
"""


def _non_empty_tables_sql(tables: list[str]) -> str:
    """One query returning the tables that actually hold rows.

    Truncating ALL ~120 tables costs 1.7 s per test (measured) — each takes an
    ACCESS EXCLUSIVE lock and rewrites its files and sequences. A test touches a
    handful, so ask first. EXISTS is exact (unlike `pg_class.reltuples`, an
    estimate that only VACUUM refreshes — a stale 0 there would leave rows
    standing and poison the next test), and against an empty table it is a scan
    of zero pages.
    """
    parts = [
        f"SELECT '{t}' AS t WHERE EXISTS (SELECT 1 FROM \"{t}\")" for t in tables
    ]
    return " UNION ALL ".join(parts)


def _truncate_sql(names: list[str]) -> str:
    """RESTART IDENTITY so a test that asserts on ids starts at 1; CASCADE
    because the tables reference each other."""
    joined = ", ".join(f'"{n}"' for n in names)
    return f"TRUNCATE {joined} RESTART IDENTITY CASCADE"


# `create_all` renders `search_vector` as a plain tsvector column: SQLAlchemy
# does not know it is GENERATED — the migrations add it with an ALTER. A plain
# column is never filled, so every full-text search finds nothing and the test
# that "passes" proves only that the sqlite fallback ran. Rebuild the columns
# the way production has them, from the very same expression builder the
# migrations use.
# table → the content EXPRESSION the migration feeds to the tsvector builder
# (copied from the migrations so the harness and production agree).
_FTS_GENERATED_COLUMNS = {
    "messages": "content",
    "conversation_memories": "content",
    "document_chunks": "content",
    "document_facts": (
        "value || ' ' || coalesce(normalized_value, '') || ' ' || "
        "coalesce(excerpt, '') || ' ' || kind"
    ),
    "notes": "title || ' ' || coalesce(body, '')",
    "documents": (
        "(coalesce(generated_title, '') || ' ' || coalesce(title, '') || ' ' "
        "|| coalesce(filename, ''))"
    ),
}


async def _add_generated_search_vectors(conn) -> None:
    from services.fts_languages import build_generated_tsvector_expression

    for table, content_expr in _FTS_GENERATED_COLUMNS.items():
        exists = (await conn.execute(_sa_text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = :t"
        ), {"t": table})).scalar()
        if exists is None:
            continue  # table gone — the migration owns the truth, not this list
        expr = build_generated_tsvector_expression(content_expr)
        await conn.execute(_sa_text(
            f"ALTER TABLE {table} DROP COLUMN IF EXISTS search_vector"
        ))
        await conn.execute(_sa_text(
            f"ALTER TABLE {table} ADD COLUMN search_vector tsvector "
            f"GENERATED ALWAYS AS ({expr}) STORED"
        ))


# One key for the whole run. `_SCHEMA_READY` is a PROCESS flag, and the first
# database test of a process DROPS the schema — so two runs against the same
# database (an xdist worker, or simply a second terminal) would pull the tables
# out from under each other mid-test. The lock makes that impossible: the second
# run stops immediately and says why, instead of producing nonsense.
_RUN_LOCK_KEY = 0x52454E46  # "RENF"
_RUN_LOCK_CONN = None
_RUN_LOCK_ENGINE = None


async def _claim_database(dsn: str) -> None:
    """Hold a session-level advisory lock for the duration of this pytest run.

    On its OWN engine: a session-level lock lives with its connection, and the
    schema engine is disposed as soon as the tables exist — which would hand
    the lock straight back.
    """
    global _RUN_LOCK_CONN, _RUN_LOCK_ENGINE
    from sqlalchemy.pool import NullPool

    _RUN_LOCK_ENGINE = create_async_engine(dsn, poolclass=NullPool, echo=False)
    conn = await _RUN_LOCK_ENGINE.connect()
    got = (await conn.execute(
        _sa_text("SELECT pg_try_advisory_lock(:k)"), {"k": _RUN_LOCK_KEY}
    )).scalar()
    if not got:
        await conn.close()
        pytest.exit(
            "Another pytest run holds this test database "
            f"({_os.environ.get('RENFIELD_TEST_PG_URL')}). Use a database of "
            "your own — a second run would drop this one's schema mid-test.",
            returncode=1,
        )
    _RUN_LOCK_CONN = conn   # released when the process ends


def _refuse_the_application_database(dsn: str) -> None:
    """Never run the suite against the database the application itself uses."""
    live = _os.environ.get("DATABASE_URL", "")
    if not live:
        return

    def _name(url: str) -> str:
        return url.rsplit("/", 1)[-1].split("?")[0]

    if _name(dsn) and _name(dsn) == _name(live):
        pytest.exit(
            f"RENFIELD_TEST_PG_URL points at the application database "
            f"({_name(dsn)}). This harness drops and truncates — use a "
            "dedicated test database.",
            returncode=1,
        )


async def _ensure_schema() -> None:
    """Create the schema ONCE per pytest run (the first database test pays for
    it). Its engine is NullPool'd and disposed immediately, so no connection is
    carried across the per-test event loops pytest-asyncio creates."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    from sqlalchemy.pool import NullPool

    # Import every model module BEFORE create_all: a table whose module has not
    # been imported is simply absent from the metadata, and the first test that
    # imports it then queries a table that was never created.
    import ha_glue.models.database  # noqa: F401

    dsn = _pg_url_or_skip()
    _refuse_the_application_database(dsn)
    engine = create_async_engine(dsn, poolclass=NullPool, echo=False)
    await _claim_database(dsn)
    try:
        async with engine.begin() as conn:
            await conn.execute(_sa_text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
            await _add_generated_search_vectors(conn)
    finally:
        await engine.dispose()
    _SCHEMA_READY = True


@pytest.fixture
async def async_engine():
    """Async engine on the real Postgres test database.

    NullPool: pytest-asyncio's auto mode gives every test its own event loop,
    and a pooled asyncpg connection bound to a previous loop raises "got Future
    attached to a different loop". Without a pool each test opens its own
    connection, which costs a millisecond and removes the whole class of
    cross-loop failures.

    The schema is built ONCE per run; between tests the tables that hold rows
    are emptied. Committed data stays visible to code that opens its OWN
    session through `AsyncSessionLocal` — a transaction-rollback harness would
    hide those rows from it.
    """
    from sqlalchemy.pool import NullPool

    await _ensure_schema()
    engine = create_async_engine(_pg_url_or_skip(), poolclass=NullPool, echo=False)

    # Empty every table instead of rebuilding the schema: dropping and creating
    # ~120 tables costs seconds PER TEST (measured: 2.5 s), one TRUNCATE costs
    # milliseconds, and both give the same thing that matters — a test starts
    # with an empty database and sees committed rows, including those written
    # by code that opens its own `AsyncSessionLocal`.
    async with engine.begin() as conn:
        tables = [r[0] for r in (await conn.execute(_sa_text(_EXISTING_TABLES_SQL))).all()]
        if tables:
            dirty = [
                r[0] for r in
                (await conn.execute(_sa_text(_non_empty_tables_sql(tables)))).all()
            ]
            if dirty:
                await conn.execute(_sa_text(_truncate_sql(dirty)))
            # A test that INSERTS and rolls back leaves the table empty but the
            # sequence advanced — so the truncate above never touches it, and
            # the next test's ids depend on run order. Reset every sequence that
            # has been called; that is what `RESTART IDENTITY` promises.
            used = [
                r[0] for r in (await conn.execute(_sa_text(_USED_SEQUENCES_SQL))).all()
            ]
            for seq in used:
                await conn.execute(_sa_text(f'ALTER SEQUENCE "{seq}" RESTART'))

    yield engine

    await engine.dispose()


@pytest.fixture
async def db_session(async_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create async database session for tests"""
    async_session_maker = async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

    async with async_session_maker() as session:
        yield session
        await session.rollback()


# ============================================================================
# Postgres test infrastructure (real server, NOT the sqlite shim)
# ============================================================================
#
# The default test stack uses sqlite-in-memory with TSVECTOR/Vector shims —
# fast, hermetic, but cannot exercise Postgres-only behavior:
#   - GENERATED STORED columns (sqlite has them but rejects to_tsvector as
#     non-deterministic; production schema relies on them for search_vector)
#   - websearch_to_tsquery, ts_rank, ts_rank_cd (no sqlite equivalent)
#   - GIN indexes, ANALYZE, CONCURRENTLY DDL
#   - asyncpg-level autobegin semantics that bit env.py during PR #625/#626
#
# Tests that need any of the above mark themselves with @pytest.mark.postgres
# and depend on `pg_db_session` (or `pg_async_engine`). The marker is
# registered in pyproject.toml.
#
# Reachability is gated by the RENFIELD_TEST_PG_URL env var. Set it to a
# DSN like `postgresql+asyncpg://user:pw@host:5432/dbname` to enable. When
# unset, tests requiring the fixture are skipped at collection time —
# CI / local sqlite-only runs are unaffected. On the .159 build box,
# `docker exec renfield-backend pytest …` should set the env var via
# the wrapper script (or run with `--env RENFIELD_TEST_PG_URL=$DATABASE_URL`)
# pointing at the build-box postgres.
#
# Cleanup strategy: each test gets its own savepoint-style transaction
# that rolls back on teardown. That works for everything the FTS tests
# do (INSERT + SELECT). It would NOT work for tests that need to observe
# DDL (e.g., the migration itself); those would need a fresh DB per test
# and aren't in scope here.



def _pg_test_dsn() -> str | None:
    """Return the Postgres test DSN if configured, else None.

    Reads RENFIELD_TEST_PG_URL. Caller is responsible for handling None
    (typically via pytestmark skipif at the module level).
    """
    return _os.environ.get("RENFIELD_TEST_PG_URL")


# Backstop marker registration — the container's pytest invocation doesn't
# pick up pyproject.toml (no copy is present at the right path), so the
# `postgres` marker raises PytestUnknownMarkWarning at collection time.
# Registering here in conftest.py works regardless of pyproject.toml
# discovery and is idempotent with the pyproject.toml registration for
# laptop test runs that DO find it. Keep this description BYTE-IDENTICAL
# to the pyproject.toml entry so grep against either location returns
# the same canonical text.
def pytest_collection_modifyitems(config, items):
    """Refuse a run that would SKIP every database test.

    Without `RENFIELD_TEST_PG_URL` each database fixture calls `pytest.skip`,
    and the run reports green while thousands of tests quietly did nothing —
    the exact outcome the project rule "a failing test is an issue, not noise"
    exists to prevent. Say it once, loudly, at collection time.
    """
    if _os.environ.get("RENFIELD_TEST_PG_URL"):
        return
    needs_db = [
        item for item in items
        if item.get_closest_marker("database") or item.get_closest_marker("postgres")
        or "db_session" in getattr(item, "fixturenames", ())
        or "async_client" in getattr(item, "fixturenames", ())
    ]
    if needs_db:
        raise pytest.UsageError(
            f"{len(needs_db)} tests need the database, and RENFIELD_TEST_PG_URL "
            "is not set. The test database is real Postgres (see CLAUDE.md); "
            "point the variable at a dedicated database — a run that skips them "
            "all would report green while testing nothing."
        )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: Tests requiring real PostgreSQL + pgvector (skipped on sqlite test harness)",  # noqa: E501 — kept verbatim to match pyproject.toml; the harness itself is Postgres now
    )


@pytest.fixture
async def pg_async_engine(async_engine):
    """Kept as a NAME, not as a second mechanism.

    There is only one kind of test database now: real Postgres. This used to
    build and drop the whole schema per test alongside the sqlite default —
    which, in one pytest process, pulled the tables out from under every other
    test that had already seen the schema created ("relation conversations does
    not exist"). Tests that ask for it get exactly the default engine.
    """
    return async_engine


@pytest.fixture
async def pg_db_session(db_session):
    """Alias of `db_session` — see `pg_async_engine`."""
    return db_session


@pytest.fixture
def sample_room_data():
    """Sample room data for tests"""
    return {
        "name": "Wohnzimmer",
        "alias": "wohnzimmer",
        "source": "renfield",
        "ha_area_id": None,
        "icon": "mdi:sofa"
    }


@pytest.fixture
def sample_device_data():
    """Sample device data for tests"""
    return {
        "device_id": "web-wohnzimmer-abc123",
        "device_type": DEVICE_TYPE_WEB_BROWSER,
        "device_name": "Test Browser",
        "capabilities": DEFAULT_CAPABILITIES[DEVICE_TYPE_WEB_BROWSER],
        "is_stationary": False,
        "ip_address": "192.168.1.100",
        "user_agent": "Mozilla/5.0 Test"
    }


@pytest.fixture
def sample_satellite_data():
    """Sample satellite device data"""
    return {
        "device_id": "sat-wohnzimmer-main",
        "device_type": DEVICE_TYPE_SATELLITE,
        "device_name": "Living Room Satellite",
        "capabilities": DEFAULT_CAPABILITIES[DEVICE_TYPE_SATELLITE],
        "is_stationary": True,
        "ip_address": "192.168.1.50"
    }


@pytest.fixture
def sample_speaker_data():
    """Sample speaker data for tests"""
    return {
        "name": "Max Mustermann",
        "alias": "max",
        "is_admin": False
    }


@pytest.fixture
def sample_conversation_data():
    """Sample conversation data for tests"""
    return {
        "session_id": "test-session-123"
    }


@pytest.fixture
def sample_message_data():
    """Sample message data for tests"""
    return {
        "role": "user",
        "content": "Schalte das Licht ein",
        "message_metadata": {"intent": "homeassistant.turn_on"}
    }


@pytest.fixture
def sample_intent_data():
    """Sample intent data for tests"""
    return {
        "intent": "homeassistant.turn_on",
        "parameters": {
            "entity_id": "light.wohnzimmer",
            "name": "Wohnzimmer Licht"
        },
        "confidence": 0.95
    }


@pytest.fixture
def sample_ha_areas():
    """Sample Home Assistant areas for tests"""
    return [
        {"area_id": "living_room", "name": "Wohnzimmer", "icon": "mdi:sofa"},
        {"area_id": "kitchen", "name": "Küche", "icon": "mdi:pot"},
        {"area_id": "bedroom", "name": "Schlafzimmer", "icon": "mdi:bed"},
    ]


# ============================================================================
# Database Object Fixtures
# ============================================================================

@pytest.fixture
async def test_room(db_session: AsyncSession, sample_room_data) -> Room:
    """Create a test room in database"""
    room = Room(**sample_room_data)
    db_session.add(room)
    await db_session.commit()
    await db_session.refresh(room)
    return room


@pytest.fixture
async def test_device(db_session: AsyncSession, test_room: Room, sample_device_data) -> RoomDevice:
    """Create a test device in database"""
    device = RoomDevice(
        room_id=test_room.id,
        **sample_device_data
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


@pytest.fixture
async def test_satellite(db_session: AsyncSession, test_room: Room, sample_satellite_data) -> RoomDevice:
    """Create a test satellite in database"""
    satellite = RoomDevice(
        room_id=test_room.id,
        **sample_satellite_data
    )
    db_session.add(satellite)
    await db_session.commit()
    await db_session.refresh(satellite)
    return satellite


@pytest.fixture
async def test_speaker(db_session: AsyncSession, sample_speaker_data) -> Speaker:
    """Create a test speaker in database"""
    speaker = Speaker(**sample_speaker_data)
    db_session.add(speaker)
    await db_session.commit()
    await db_session.refresh(speaker)
    return speaker


@pytest.fixture
async def test_conversation(db_session: AsyncSession, sample_conversation_data) -> Conversation:
    """Create a test conversation in database"""
    conversation = Conversation(**sample_conversation_data)
    db_session.add(conversation)
    await db_session.commit()
    await db_session.refresh(conversation)
    return conversation


@pytest.fixture
async def test_message(db_session: AsyncSession, test_conversation: Conversation, sample_message_data) -> Message:
    """Create a test message in database"""
    message = Message(
        conversation_id=test_conversation.id,
        **sample_message_data
    )
    db_session.add(message)
    await db_session.commit()
    await db_session.refresh(message)
    return message


@pytest.fixture
def sample_knowledge_base_data():
    """Sample knowledge base data for tests"""
    return {
        "name": "Test Knowledge Base",
        "description": "A test knowledge base for unit tests",
        "is_active": True,
        "is_public": False
    }


@pytest.fixture
def sample_document_data():
    """Sample document data for tests"""
    return {
        "filename": "test_document.pdf",
        "title": "Test Document",
        "file_path": "/tmp/test_document.pdf",
        "file_type": "pdf",
        "file_size": 12345,
        "file_hash": "abc123def456",
        "status": "completed",
        "chunk_count": 5,
        "page_count": 3
    }


@pytest.fixture
def sample_role_data():
    """Sample role data for tests"""
    return {
        "name": "TestRole",
        "description": "A test role",
        "permissions": ["kb.all", "ha.full", "admin"],
        "is_system": False
    }


@pytest.fixture
def sample_user_data():
    """Sample user data for tests"""
    return {
        "username": "testuser",
        "email": "test@example.com",
        "password_hash": "$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/X4wPpY/ABCDEFGH",  # Fake hash
        "is_active": True
    }


@pytest.fixture
async def test_role(db_session: AsyncSession, sample_role_data) -> Role:
    """Create a test role in database"""
    role = Role(**sample_role_data)
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role



@pytest.fixture
async def make_user(db_session: AsyncSession):
    """Create real users with the ids a test wants to talk about.

    Postgres enforces `conversations.user_id → users.id`; the old sqlite
    harness did not, so ownership tests happily wrote `user_id=7` for a user
    that never existed. Ask for the users you assert about:

        u = await make_user(7)        # -> User with id 7
    """
    from models.database import Role, User as _User

    role = Role(name="rolle-fixture", permissions=["chat.own"], is_system=False)
    db_session.add(role)
    await db_session.flush()

    async def _make(user_id: int, **kw):
        user = _User(
            id=user_id,
            username=kw.pop("username", f"nutzer{user_id}"),
            password_hash="x",
            role_id=role.id,
            **kw,
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    return _make


@pytest.fixture
async def test_user(db_session: AsyncSession, test_role: Role, sample_user_data) -> User:
    """Create a test user in database"""
    user = User(
        role_id=test_role.id,
        **sample_user_data
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def test_knowledge_base(db_session: AsyncSession, sample_knowledge_base_data) -> KnowledgeBase:
    """Create a test knowledge base in database"""
    kb = KnowledgeBase(**sample_knowledge_base_data)
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def test_knowledge_base_with_owner(
    db_session: AsyncSession,
    test_user: User,
    sample_knowledge_base_data
) -> KnowledgeBase:
    """Create a test knowledge base with an owner"""
    kb = KnowledgeBase(
        owner_id=test_user.id,
        **sample_knowledge_base_data
    )
    db_session.add(kb)
    await db_session.commit()
    await db_session.refresh(kb)
    return kb


@pytest.fixture
async def test_document(
    db_session: AsyncSession,
    test_knowledge_base: KnowledgeBase,
    sample_document_data
) -> Document:
    """Create a test document in database"""
    document = Document(
        knowledge_base_id=test_knowledge_base.id,
        **sample_document_data
    )
    db_session.add(document)
    await db_session.commit()
    await db_session.refresh(document)
    return document


# ============================================================================
# Mock Service Fixtures
# ============================================================================

@pytest.fixture
def mock_ha_client():
    """Mock Home Assistant client"""
    client = AsyncMock()

    # Default responses
    client.get_state.return_value = {
        "state": "off",
        "attributes": {"friendly_name": "Wohnzimmer Licht"}
    }
    client.turn_on.return_value = True
    client.turn_off.return_value = True
    client.toggle.return_value = True
    client.search_entities.return_value = [
        {"entity_id": "light.wohnzimmer", "friendly_name": "Wohnzimmer Licht"}
    ]
    client.get_all_entities.return_value = [
        {"entity_id": "light.wohnzimmer", "state": "off", "attributes": {"friendly_name": "Wohnzimmer Licht"}},
        {"entity_id": "switch.fernseher", "state": "on", "attributes": {"friendly_name": "Fernseher"}},
    ]
    client.get_keywords.return_value = ["wohnzimmer", "licht", "fernseher", "küche"]
    client.is_configured.return_value = True
    client.get_areas.return_value = [
        {"area_id": "living_room", "name": "Wohnzimmer"},
        {"area_id": "kitchen", "name": "Küche"}
    ]

    return client


@pytest.fixture
def mock_ollama_client():
    """Mock Ollama client"""
    client = AsyncMock()

    client.generate.return_value = {
        "response": "Das Licht wurde eingeschaltet."
    }
    client.chat.return_value = {
        "message": {"content": "Das Licht wurde eingeschaltet."}
    }

    return client


@pytest.fixture
def mock_whisper_service():
    """Mock Whisper STT service"""
    service = AsyncMock()

    service.transcribe.return_value = {
        "text": "Schalte das Licht im Wohnzimmer ein",
        "language": "de"
    }
    service.transcribe_with_speaker.return_value = {
        "text": "Schalte das Licht ein",
        "language": "de",
        "speaker_id": 1,
        "speaker_name": "Max"
    }

    return service


@pytest.fixture
def mock_piper_service():
    """Mock Piper TTS service"""
    service = MagicMock()

    # Return fake audio bytes
    service.synthesize.return_value = b"RIFF" + b"\x00" * 100
    service.synthesize_async = AsyncMock(return_value=b"RIFF" + b"\x00" * 100)

    return service


@pytest.fixture
def mock_frigate_client():
    """Mock Frigate client"""
    client = AsyncMock()

    client.get_events.return_value = [
        {
            "id": "event-1",
            "camera": "front_door",
            "label": "person",
            "confidence": 0.85,
            "timestamp": datetime.utcnow().isoformat()
        }
    ]
    client.get_snapshot.return_value = b"\x89PNG" + b"\x00" * 100

    return client


@pytest.fixture
def mock_n8n_client():
    """Mock n8n client"""
    client = AsyncMock()

    client.trigger_workflow.return_value = {
        "success": True,
        "executionId": "exec-123"
    }

    return client


@pytest.fixture
def mock_speaker_service():
    """Mock speaker recognition service"""
    service = AsyncMock()

    service.extract_embedding.return_value = [0.1] * 192  # 192-dimensional embedding
    service.identify_speaker.return_value = {
        "speaker_id": 1,
        "speaker_name": "Max",
        "confidence": 0.85
    }
    service.enroll_speaker.return_value = True

    return service


# ============================================================================
# Service Fixtures
# ============================================================================

@pytest.fixture
def room_service(db_session: AsyncSession):
    """Create RoomService with test database"""
    from ha_glue.services.room_service import RoomService
    return RoomService(db_session)


@pytest.fixture
def mock_mcp_manager():
    """Create a mock MCP manager"""
    manager = AsyncMock()
    manager.execute_tool.return_value = {
        "success": True,
        "message": "MCP tool executed",
        "action_taken": True,
    }
    return manager


@pytest.fixture
def action_executor(mock_mcp_manager):
    """Create ActionExecutor with mocked dependencies"""
    from services.action_executor import ActionExecutor

    executor = ActionExecutor(
        mcp_manager=mock_mcp_manager,
    )

    return executor


# ============================================================================
# FastAPI Test Client Fixtures
# ============================================================================

@pytest.fixture
def override_get_db(db_session: AsyncSession):
    """Override database dependency for FastAPI"""
    async def _override():
        yield db_session
    return _override


# The ha_glue-owned REST routers (camera, homeassistant, satellites, rooms,
# presence, paperless_audit) and the device/satellite WebSockets are mounted
# at runtime by the ``register_routes`` hook fired inside the FastAPI lifespan
# (see api/lifecycle.py). The bare ``from main import app`` used below never
# triggers the lifespan, so without this explicit mount every HA route 404s.
#
# Each router is mounted at most once: we probe ``app.routes`` for a known
# path before calling ``include_router`` so repeated fixture invocations
# don't stack duplicates. A per-router probe (rather than a single global
# flag) makes this self-healing — a transient import failure on the first
# fixture call doesn't permanently disable every HA route for the session.
def _has_route(app, path: str) -> bool:
    return any(getattr(r, "path", None) == path for r in app.routes)


def _ensure_ha_glue_routes(app):
    """Mount ha_glue REST/WS routers on the shared app, idempotently.

    Mirrors ha_glue.bootstrap.ha_glue_register_routes — replicated here
    (rather than awaited) because this runs inside an already-running
    event loop where the coroutine cannot be driven to completion."""
    # (module import path, include_router kwargs, sentinel path to probe)
    specs = [
        ("ha_glue.api.admin", {}, "/admin/refresh-keywords"),
        ("ha_glue.api.routes.camera", {"prefix": "/api/camera"}, "/api/camera/events"),
        (
            "ha_glue.api.routes.homeassistant",
            {"prefix": "/api/homeassistant"},
            "/api/homeassistant/states",
        ),
        (
            "ha_glue.api.routes.satellites",
            {"prefix": "/api/satellites"},
            "/api/satellites",
        ),
        (
            "ha_glue.api.routes.satellite_enrollment",
            {},
            "/api/satellite-enrollment",
        ),
        ("ha_glue.api.routes.rooms", {"prefix": "/api/rooms"}, "/api/rooms"),
        ("ha_glue.api.routes.presence", {}, None),
        ("ha_glue.api.routes.paperless_audit", {}, None),
        ("ha_glue.api.websocket.device_handler", {}, None),
        ("ha_glue.api.websocket.satellite_handler", {}, None),
    ]
    import importlib

    for module_path, kwargs, sentinel in specs:
        if sentinel is not None and _has_route(app, sentinel):
            continue
        try:
            module = importlib.import_module(module_path)
            router = module.router
        except Exception:  # noqa: BLE001 — platform-only deploy / broken module
            continue
        # For routers with no easily-probed sentinel path, fall back to a
        # router-identity check so they're still mounted only once.
        if sentinel is None and any(
            r is route for r in app.routes for route in getattr(router, "routes", [])
        ):
            continue
        app.include_router(router, **kwargs)


@pytest.fixture
async def own_session_to_test_db(async_engine, monkeypatch):
    """Point code that opens its OWN session at the test database.

    Some code deliberately opens `AsyncSessionLocal()` instead of taking the
    request-scoped session: a streaming export that must outlive the request, a
    background task that finishes after the response, a WebSocket boundary check
    that must not borrow the caller's transaction. None of that is reached by
    the `Depends(get_db)` override.

    Patching `services.database.AsyncSessionLocal` alone is NOT enough: several
    modules bind the NAME at import time (`from services.database import
    AsyncSessionLocal` — chat_upload, voice, both WS handlers), so they keep
    their own reference. Unswept, such a test reads and writes the REAL database
    behind `DATABASE_URL` — silently, and usually still green, because it finds
    no rows and every answer comes out as the default. (Exactly how the first
    cut of `test_shared_conversations_boundaries.py` "passed".)

    Request this fixture from any test that exercises such code WITHOUT the HTTP
    client; `app_with_test_db` already includes it.
    """
    import services.database as _db_mod

    test_sessionmaker = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )
    _original_sessionmaker = _db_mod.AsyncSessionLocal
    monkeypatch.setattr(_db_mod, "AsyncSessionLocal", test_sessionmaker)
    import sys as _sys

    for _mod in list(_sys.modules.values()):
        # `__dict__`, never getattr: a module with a PEP-562 `__getattr__`
        # (speechbrain's lazy integrations, for one) would IMPORT something on
        # every probe — the sweep would drag half the ML stack into every test.
        if getattr(_mod, "__dict__", None) is None:
            continue
        if _mod.__dict__.get("AsyncSessionLocal") is _original_sessionmaker:
            monkeypatch.setattr(_mod, "AsyncSessionLocal", test_sessionmaker, raising=False)
    return test_sessionmaker


@pytest.fixture
async def app_with_test_db(
    override_get_db, mock_ha_client, async_engine, own_session_to_test_db, monkeypatch
):
    """FastAPI app with test database and mocked services"""
    from main import app
    from services.database import get_db

    # Mount the ha_glue routers that the lifespan would normally register.
    _ensure_ha_glue_routes(app)

    # Override the request-scoped DB dependency (Depends(get_db)). Code that
    # opens its own session is covered by `own_session_to_test_db` above.
    app.dependency_overrides[get_db] = override_get_db

    yield app

    # Cleanup
    app.dependency_overrides.clear()


@pytest.fixture
async def async_client(app_with_test_db) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client for API tests"""
    transport = ASGITransport(app=app_with_test_db)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================================
# WebSocket Test Fixtures
# ============================================================================

@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection"""
    ws = AsyncMock()
    ws.accept = AsyncMock()
    ws.send_json = AsyncMock()
    ws.send_bytes = AsyncMock()
    ws.receive_json = AsyncMock()
    ws.receive_bytes = AsyncMock()
    ws.close = AsyncMock()
    ws.client = MagicMock()
    ws.client.host = "127.0.0.1"

    return ws


# ============================================================================
# Utility Fixtures
# ============================================================================

@pytest.fixture
def freeze_time():
    """Fixture for mocking datetime.utcnow()"""
    frozen_time = datetime(2024, 1, 15, 12, 0, 0)

    with patch('datetime.datetime') as mock_datetime:
        mock_datetime.utcnow.return_value = frozen_time
        mock_datetime.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
        yield frozen_time


@pytest.fixture
def mock_settings():
    """Mock settings for tests"""
    from utils.config import Settings

    return Settings(
        database_url="sqlite:///:memory:",
        redis_url="redis://localhost:6379",
        ollama_url="http://localhost:11434",
        ollama_model="llama3.2:3b",
        home_assistant_url="http://localhost:8123",
        home_assistant_token="test_token",
        speaker_recognition_enabled=True,
        speaker_auto_enroll=True,
        ws_rate_limit_enabled=False
    )
