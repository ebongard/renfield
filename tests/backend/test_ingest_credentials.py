"""Per-integration ingest credentials (docs/design/ingest-credentials.md).

The properties that matter here are security properties, so they are asserted
directly rather than inferred: the plaintext is never stored, a credential is
bound to ONE route, revocation is immediate, the legacy shared token keeps
working through the transition, and the whole thing is inert when the flag is
off.
"""

import pytest
from sqlalchemy import select

from models.database import IngestCredential
from services import ingest_credentials as ic

pytestmark = [pytest.mark.backend, pytest.mark.database]


async def _legacy_true(db, token):
    return token == "legacy-shared-token"


async def _legacy_false(db, token):
    return False


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setattr(ic.settings, "ingest_credentials_enabled", True, raising=False)


# --- token format -----------------------------------------------------------

def test_token_is_self_identifying():
    # The client_id rides in the token so verification is ONE lookup + ONE
    # bcrypt, instead of a bcrypt round against every stored credential.
    assert ic._split_token("rfi.scanner.abc123") == ("scanner", "abc123")


@pytest.mark.parametrize("bad", [
    "", "nope", "rfi.scanner", "rfi..secret", "xyz.scanner.secret",
    "rfi.Scanner.secret", "rfi.scan_ner.secret", "rfi.scanner.",
])
def test_non_conforming_tokens_are_not_ours(bad):
    # These must fall through to the legacy path, not raise.
    assert ic._split_token(bad) is None


def test_secret_may_contain_dots():
    # split(".", 2) — a secret containing a dot must survive intact.
    assert ic._split_token("rfi.scanner.a.b.c") == ("scanner", "a.b.c")


@pytest.mark.parametrize("bad", ["", "-x", "x" * 65, "a_b", "a.b", "ä", " ", "a b"])
def test_client_id_charset_enforced(bad):
    with pytest.raises(ic.InvalidClientId):
        ic._validate_client_id(bad)


@pytest.mark.parametrize("given,expected", [
    ("Scanner", "scanner"), ("  files  ", "files"), ("EMAIL-INGEST", "email-ingest"),
])
def test_client_id_is_normalised_not_rejected(given, expected):
    # Case and surrounding space are normalised on the way IN, so an operator
    # typing "Scanner" gets a working credential. The token PARSER stays strict
    # (rfi.Scanner.x is not ours) because every minted token is already
    # canonical — normalising there would let two spellings hit one row.
    assert ic._validate_client_id(given) == expected
    assert ic._split_token(f"rfi.{given}.secret") is None or given == expected


# --- mint / verify ----------------------------------------------------------

async def test_mint_returns_plaintext_and_stores_only_a_hash(db_session):
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="Scanner", route=ic.ROUTE_FOLDER)
    assert token.startswith("rfi.scanner.")
    row = (await db_session.execute(
        select(IngestCredential).where(IngestCredential.client_id == "scanner")
    )).scalar_one()
    secret = token.split(".", 2)[2]
    assert secret not in row.token_hash        # never the plaintext
    assert row.token_hash.startswith("$2")     # bcrypt


async def test_minted_token_resolves_to_its_client(db_session):
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="Scanner", route=ic.ROUTE_FOLDER)
    client = await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    assert client is not None
    assert client.client_id == "scanner" and not client.legacy


async def test_wrong_secret_is_rejected(db_session):
    await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "rfi.scanner.wrong") is None


async def test_unknown_client_id_is_rejected(db_session):
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "rfi.ghost.secret") is None


async def test_duplicate_client_id_refused(db_session):
    await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    with pytest.raises(ValueError, match="already exists"):
        await ic.mint_credential(
            db_session, client_id="scanner", label="S2", route=ic.ROUTE_FOLDER)


async def test_unknown_route_refused(db_session):
    with pytest.raises(ValueError, match="unknown ingest route"):
        await ic.mint_credential(
            db_session, client_id="x", label="X", route="nope")


# --- the isolation properties ----------------------------------------------

async def test_credential_is_bound_to_one_route(db_session):
    # A folder-ingest credential must not authenticate on the email route.
    token = await ic.mint_credential(
        db_session, client_id="files", label="Files", route=ic.ROUTE_FOLDER)
    assert await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token) is not None
    assert await ic.resolve_ingest_client(db_session, ic.ROUTE_EMAIL, token) is None


async def test_revocation_is_immediate_and_independent(db_session):
    a = await ic.mint_credential(
        db_session, client_id="files", label="Files", route=ic.ROUTE_FOLDER)
    b = await ic.mint_credential(
        db_session, client_id="scanner", label="Scanner", route=ic.ROUTE_FOLDER)
    assert await ic.revoke_credential(db_session, "files") is True
    # The whole point of per-integration credentials: one dies, the other lives.
    assert await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, a) is None
    assert await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, b) is not None


async def test_revoking_unknown_client_is_false_not_an_error(db_session):
    assert await ic.revoke_credential(db_session, "ghost") is False


async def test_rotation_invalidates_the_old_secret(db_session):
    old = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    new = await ic.rotate_credential(db_session, "scanner")
    assert new != old
    assert await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, new) is not None
    assert await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, old) is None


async def test_rotate_unknown_client_raises(db_session):
    with pytest.raises(ValueError, match="unknown client_id"):
        await ic.rotate_credential(db_session, "ghost")


async def test_last_authenticated_at_is_stamped(db_session):
    # This is what makes "is the legacy token still in use?" observable rather
    # than a guess, so it must actually be written.
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    row = (await db_session.execute(
        select(IngestCredential).where(IngestCredential.client_id == "scanner")
    )).scalar_one()
    assert row.last_authenticated_at is None
    await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    await db_session.refresh(row)
    assert row.last_authenticated_at is not None


# --- legacy fallback + the flag --------------------------------------------

async def test_legacy_shared_token_still_works(db_session):
    client = await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "legacy-shared-token", legacy_verify=_legacy_true)
    assert client is not None and client.legacy and client.client_id == "legacy"


async def test_new_and_legacy_tokens_coexist(db_session):
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, token, legacy_verify=_legacy_true) is not None
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "legacy-shared-token",
        legacy_verify=_legacy_true) is not None


async def test_flag_off_ignores_credentials_entirely(db_session, monkeypatch):
    # Flag off must be byte-identical to the legacy path: a perfectly good
    # credential is not consulted at all.
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    monkeypatch.setattr(ic.settings, "ingest_credentials_enabled", False, raising=False)
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, token, legacy_verify=_legacy_false) is None
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "legacy-shared-token",
        legacy_verify=_legacy_true) is not None


async def test_empty_token_is_rejected(db_session):
    assert await ic.resolve_ingest_client(
        db_session, ic.ROUTE_FOLDER, "", legacy_verify=_legacy_true) is None


# --- review findings ---------------------------------------------------------

def test_legacy_client_id_is_reserved():
    # "legacy" is the synthetic client reported for the shared token; a real
    # credential of that name would be indistinguishable from it downstream.
    with pytest.raises(ic.InvalidClientId, match="reserved"):
        ic._validate_client_id("legacy")
    with pytest.raises(ic.InvalidClientId, match="reserved"):
        ic._validate_client_id("  LEGACY  ")


async def test_cannot_mint_a_reserved_client_id(db_session):
    with pytest.raises(ic.InvalidClientId):
        await ic.mint_credential(
            db_session, client_id="legacy", label="x", route=ic.ROUTE_FOLDER)


async def test_last_seen_write_is_throttled(db_session, monkeypatch):
    # A write + commit on EVERY push is pooled-connection pressure in the ingest
    # hot path. The freshness signal does not need second precision.
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    row = (await db_session.execute(
        select(IngestCredential).where(IngestCredential.client_id == "scanner")
    )).scalar_one()
    first = row.last_authenticated_at
    assert first is not None                      # first push always stamps
    await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    await db_session.refresh(row)
    assert row.last_authenticated_at == first     # second, immediately after, does not


async def test_last_seen_stamps_again_once_stale(db_session):
    from datetime import timedelta
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    row = (await db_session.execute(
        select(IngestCredential).where(IngestCredential.client_id == "scanner")
    )).scalar_one()
    row.last_authenticated_at = ic.datetime.utcnow() - timedelta(
        seconds=ic._LAST_SEEN_THROTTLE_SECONDS + 5)
    await db_session.commit()
    stale = row.last_authenticated_at
    await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    await db_session.refresh(row)
    assert row.last_authenticated_at > stale


async def test_verify_does_not_block_the_event_loop(db_session):
    # bcrypt takes ~150ms. Called inline it stalls every other request in this
    # worker on EVERY push, so it must be offloaded to a thread.
    import asyncio as _a
    token = await ic.mint_credential(
        db_session, client_id="scanner", label="S", route=ic.ROUTE_FOLDER)
    ticks = 0

    async def _ticker():
        nonlocal ticks
        while True:
            await _a.sleep(0.005)
            ticks += 1

    t = _a.create_task(_ticker())
    await ic.resolve_ingest_client(db_session, ic.ROUTE_FOLDER, token)
    t.cancel()
    # If bcrypt ran inline, the loop would be blocked and the ticker starved.
    assert ticks > 0, "event loop was blocked during token verification"
