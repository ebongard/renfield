"""The peer-authenticated routes as a stranger meets them.

  3. With reachability off, the ping path still answered 405 / 307 where a
     path that was never built answers 404.
  4. The inbound body was capped in bytes but not in time.

Each class states the intended behaviour; each was run red against the code
before the fix.
"""
from __future__ import annotations

import asyncio
import secrets
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from httpx import AsyncClient

from services import federation_rate_limits
from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_instance_name import wire_body
from services.federation_pending_store import reset_store_for_tests
from services.federation_query_responder import _clear_state_for_tests
from services.federation_query_schemas import PeerPingRequest, QueryBrainInitiateRequest
from utils.config import settings

PING = "/api/federation/peer/ping"
MISSING = "/api/federation/peer/ping-there-is-no-such-route"
INITIATE = "/api/federation/peer/query_brain/initiate"
UNIFORM_REFUSAL = {"detail": "Federation query failed"}


def _fresh_identity() -> FederationIdentity:
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
async def local_identity(tmp_path, monkeypatch):
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "local_key")
    reset_store_for_tests()
    await _clear_state_for_tests()
    federation_rate_limits.reset_for_tests()
    monkeypatch.setattr(settings, "federation_instance_name", "")
    monkeypatch.setattr(settings, "federation_advertised_url", "")
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    await _clear_state_for_tests()
    reset_store_for_tests()
    federation_rate_limits.reset_for_tests()


def _signed_ping(asker: FederationIdentity) -> PeerPingRequest:
    unsigned = {
        "version": 1,
        "asker_pubkey": asker.public_key_hex(),
        "nonce": secrets.token_hex(16),
        "timestamp": int(time.time()),
    }
    signature = asker.sign(canonical_bytes({"kind": "ping", **unsigned})).hex()
    return PeerPingRequest(**unsigned, signature=signature)


def _signed_initiate(asker: FederationIdentity) -> QueryBrainInitiateRequest:
    pubkey = asker.public_key_hex()
    unsigned = {
        "version": 2,
        "asker_pubkey": pubkey,
        "query": "a question",
        "nonce": secrets.token_hex(16),
        "timestamp": int(time.time()),
        "depth": 3,
        "path": [pubkey],
    }
    return QueryBrainInitiateRequest(
        **unsigned, signature=asker.sign(canonical_bytes(unsigned)).hex())


# =============================================================================
# 3. reachability off: the ping path answers like a path never built
# =============================================================================


@pytest.fixture
def feature_on(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", True)


@pytest.fixture
def feature_off(monkeypatch):
    monkeypatch.setattr(settings, "federation_reachability_enabled", False)


def _comparable(resp: httpx.Response) -> tuple:
    headers = {k.lower(): v for k, v in resp.headers.items() if k.lower() != "x-request-id"}
    return resp.status_code, resp.content, headers


#: Standard methods, one the router knows no route for anywhere, and one that
#: is no HTTP method at all.
OTHER_METHODS = ["GET", "HEAD", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "FOO"]


@pytest.mark.database
class TestFeatureOffIsIndistinguishableFromAnOlderVersion:
    """🛑 A registered route answers a GET with 405 + `Allow: POST` and a
    trailing slash with a redirect; a path that does not exist answers 404 to
    both. Either difference told a stranger "this version has the ping and it
    is switched off"."""

    @pytest.mark.parametrize("method", [*OTHER_METHODS, "POST"])
    async def test_every_method_gets_what_a_missing_path_gets(
        self, async_client: AsyncClient, local_identity, feature_off, method,
    ):
        there = await async_client.request(method, PING)
        missing = await async_client.request(method, MISSING)
        assert there.status_code == 404
        assert "allow" not in {k.lower() for k in there.headers}
        assert _comparable(there) == _comparable(missing)

    @pytest.mark.parametrize("method", ["GET", "POST", "FOO"])
    async def test_a_trailing_slash_is_not_redirected(
        self, async_client: AsyncClient, local_identity, feature_off, method,
    ):
        there = await async_client.request(method, PING + "/")
        missing = await async_client.request(method, MISSING + "/")
        assert there.status_code == 404
        assert "location" not in {k.lower() for k in there.headers}
        assert _comparable(there) == _comparable(missing)

    async def test_the_route_is_not_in_the_published_schema(
        self, async_client: AsyncClient, local_identity, feature_off,
    ):
        schema = (await async_client.get("/openapi.json")).json()
        assert not [path for path in schema["paths"] if "peer/ping" in path]


@pytest.mark.database
class TestFeatureOnStillAnswersItsPeers:
    """What must NOT change: the far side's reading of this instance's
    answers (`federation_reachability.reading_for`)."""

    async def test_a_get_is_a_plain_method_not_allowed(
        self, async_client: AsyncClient, local_identity, feature_on,
    ):
        resp = await async_client.get(PING)
        assert resp.status_code == 405
        assert resp.headers["allow"] == "POST"
        assert resp.json() == {"detail": "Method Not Allowed"}

    async def test_a_stranger_gets_the_uniform_refusal(
        self, async_client: AsyncClient, local_identity, feature_on,
    ):
        resp = await async_client.post(PING, json=wire_body(_signed_ping(_fresh_identity())))
        assert resp.status_code == 400
        assert resp.json() == UNIFORM_REFUSAL

    async def test_a_trailing_slash_is_a_404_here_too(
        self, async_client: AsyncClient, local_identity, feature_on,
    ):
        resp = await async_client.post(PING + "/", json={})
        assert resp.status_code == 404


# =============================================================================
# 4. the inbound body read has ONE deadline
# =============================================================================


class _SlowUpload:
    """A body that arrives `chunk` by `chunk`, `pause` seconds apart."""

    def __init__(self, chunks: int, pause: float, chunk: bytes = b"a"):
        self.headers: dict[str, str] = {}
        self._chunks, self._pause, self._chunk = chunks, pause, chunk
        self.sent = 0

    async def stream(self):
        for _ in range(self._chunks):
            await asyncio.sleep(self._pause)
            self.sent += 1
            yield self._chunk


@pytest.mark.unit
class TestTheBodyReadHasADeadline:
    """🛑 The byte cap bounds bytes, not time: a caller dripping one byte
    every few seconds stays inside any cap and holds the connection."""

    async def test_a_dripped_body_is_cut_off_at_the_deadline(self):
        from api.routes.federation_query import _read_capped

        upload = _SlowUpload(chunks=1000, pause=0.02)
        started = time.monotonic()
        assert await _read_capped(upload, 4096, deadline_seconds=0.2) is None
        assert time.monotonic() - started < 2.0
        assert upload.sent < 1000

    async def test_the_deadline_is_for_the_whole_read_not_per_chunk(self):
        """Every chunk arrives well inside the deadline; the body does not."""
        from api.routes.federation_query import _read_capped

        upload = _SlowUpload(chunks=40, pause=0.05)
        assert await _read_capped(upload, 4096, deadline_seconds=0.3) is None

    async def test_a_body_that_arrives_in_time_comes_back_whole(self):
        from api.routes.federation_query import _read_capped

        upload = _SlowUpload(chunks=3, pause=0.01, chunk=b"ab")
        assert await _read_capped(upload, 4096, deadline_seconds=5.0) == b"ababab"

    async def test_the_default_deadline_is_the_setting(self, monkeypatch):
        from api.routes.federation_query import _read_capped

        monkeypatch.setattr(settings, "federation_inbound_read_timeout_seconds", 0.1)
        started = time.monotonic()
        assert await _read_capped(_SlowUpload(chunks=1000, pause=0.02), 4096) is None
        assert time.monotonic() - started < 2.0

    def test_the_setting_has_a_sane_default_and_bounds(self):
        from pydantic import ValidationError

        from utils.config import Settings

        field = Settings.model_fields["federation_inbound_read_timeout_seconds"]
        assert field.default == 10.0
        for bad in (0, 0.5, 61, 3600):
            with pytest.raises(ValidationError):
                Settings(federation_inbound_read_timeout_seconds=bad)


async def _drip(total: int, pause: float):
    for _ in range(total):
        await asyncio.sleep(pause)
        yield b" "


@pytest.mark.database
class TestTheDeadlineOnTheRoutes:
    @pytest.fixture
    def short_deadline(self, monkeypatch):
        monkeypatch.setattr(settings, "federation_inbound_read_timeout_seconds", 0.3)

    async def test_a_dripped_ping_gets_the_uniform_refusal(
        self, async_client: AsyncClient, local_identity, feature_on, short_deadline,
    ):
        started = time.monotonic()
        resp = await async_client.post(
            PING, content=_drip(60, 0.05), headers={"content-type": "application/json"})
        assert time.monotonic() - started < 2.5
        assert resp.status_code == 400
        assert resp.json() == UNIFORM_REFUSAL

    async def test_a_dripped_query_gets_the_uniform_refusal(
        self, async_client: AsyncClient, local_identity, short_deadline,
    ):
        """`initiate` declares its body, so FastAPI reads it before the route
        function runs -- that read is bounded as well."""
        started = time.monotonic()
        resp = await async_client.post(
            INITIATE, content=_drip(60, 0.05), headers={"content-type": "application/json"})
        assert time.monotonic() - started < 2.5
        assert resp.status_code == 400
        assert resp.json() == UNIFORM_REFUSAL

    async def test_an_oversized_query_body_is_refused_unparsed(
        self, async_client: AsyncClient, local_identity,
    ):
        resp = await async_client.post(
            INITIATE, content=b'{"x": "' + b"a" * (80 * 1024) + b'"}',
            headers={"content-type": "application/json"})
        assert resp.status_code == 400
        assert resp.json() == UNIFORM_REFUSAL

    async def test_a_query_in_time_is_handled_as_before(
        self, async_client: AsyncClient, local_identity,
    ):
        """Control: a stranger's well-formed query gets the uniform refusal
        from the RESPONDER, and a body that is no envelope still a 422."""
        stranger = _fresh_identity()
        refused = await async_client.post(
            INITIATE, json=_signed_initiate(stranger).model_dump())
        assert refused.status_code == 400
        assert refused.json() == UNIFORM_REFUSAL
        assert (await async_client.post(INITIATE, json={"hello": "world"})).status_code == 422

    async def test_the_largest_valid_query_fits_the_cap(self, local_identity):
        import json

        from api.routes.federation_query import _QUERY_BODY_MAX_BYTES

        envelope = _signed_initiate(_fresh_identity()).model_dump()
        envelope["query"] = "\U0001F600" * 4000
        envelope["path"] = [envelope["asker_pubkey"]] * 10
        assert len(json.dumps(envelope).encode()) < _QUERY_BODY_MAX_BYTES
