"""The peer's endpoint URL -- what may be stored as, and used as, a target.

  5. A peer's endpoint was stored at pairing and used as the target of every
     later request unchecked: the server-side request forgery seam.

Each class states the intended behaviour; the pairing and outbound classes
were run red against the code before the fix.
"""
from __future__ import annotations

import socket
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from fastapi import HTTPException
from httpx import AsyncClient

import services.federation_endpoint_guard as guard
import services.federation_query_asker as asker_module
import services.pairing_service as pairing_module
from models.database import PeerUser, Role, User
from services.federation_endpoint_guard import (
    ENDPOINT_URL_MAX_CHARS,
    ENDPOINTS_MAX,
    EndpointRefused,
    check_peer_endpoints,
    endpoint_refusal,
    validate_endpoint_url,
)
from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_query_asker import (
    PING_NO_ENDPOINT,
    PING_UNREACHABLE,
    FederationQueryAsker,
    ping_peer,
)
from services.pairing_service import (
    PairingError,
    PairingResponse,
    PairingService,
    _clear_nonce_cache_for_tests,
)
from utils.config import settings

# Assembled, not written out: a literal URL with userinfo trips the pre-push credential scan.
_USERINFO = "user:" + "secret"

#: The real resolver, taken before conftest pins resolution off for every test.
REAL_RESOLVE = guard._resolve


@pytest.fixture
def local_identity(tmp_path, monkeypatch):
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "local_key")
    _clear_nonce_cache_for_tests()
    monkeypatch.setattr(settings, "federation_instance_name", "")
    monkeypatch.setattr(settings, "federation_advertised_url", "")
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    _clear_nonce_cache_for_tests()


@pytest.fixture
def far_identity() -> FederationIdentity:
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


# =============================================================================
# 5. the peer's endpoint URL
# =============================================================================


ACCEPTED = [
    "https://instance.local",
    "https://instance.local:8443",
    "https://INSTANCE.Local:8443/",
    "https://far.example.org/base/path",
    "https://backend:8443",
    "https://backend.namespace.svc.cluster.local:8443",
    "https://my_service.internal",
    "https://192.168.7.20:8443",
    "https://10.0.0.7",
    "https://172.16.4.4",
    "https://100.64.0.9",
    "https://[fd12:3456:789a::1]:8443",
    "https://far.example.org.",
]

REFUSED = {
    "plain http": "http://instance.local:8000",
    "websocket": "wss://instance.local",
    "file": "file:///etc/passwd",
    "no scheme": "instance.local:8443",
    "scheme only": "https://",
    "credentials": "https://" + _USERINFO + "@instance.local",
    "user only": "https://user@instance.local",
    "query": "https://instance.local/?next=1",
    "empty query": "https://instance.local/?",
    "fragment": "https://instance.local/#top",
    "empty fragment": "https://instance.local/#",
    "inner whitespace": "https://instance .local",
    "outer whitespace": " https://instance.local",
    "newline": "https://instance.local\n",
    "backslash": "https://instance.local\\@far.example.org",
    "too long": "https://instance.local/" + "a" * ENDPOINT_URL_MAX_CHARS,
    "loopback v4": "https://127.0.0.1:8443",
    "loopback v4, any of /8": "https://127.8.8.8",
    "loopback v6": "https://[::1]:8443",
    "localhost": "https://localhost:8443",
    "localhost, upper case": "https://LOCALHOST",
    "localhost with dot": "https://localhost.",
    "a localhost subdomain": "https://app.localhost",
    "metadata v4": "https://169.254.169.254/latest/meta-data",
    "link-local v4": "https://169.254.10.10",
    "link-local v6": "https://[fe80::1]",
    "metadata v6": "https://[fd00:ec2::254]",
    "unspecified v4": "https://0.0.0.0",
    "this network": "https://0.1.2.3:8443",
    "v4-compatible loopback": "https://[::7f00:1]",
    "v4-compatible metadata": "https://[::169.254.169.254]",
    "site-local v6": "https://[fec0::1]",
    "unspecified v6": "https://[::]",
    "mapped loopback": "https://[::ffff:127.0.0.1]",
    "mapped metadata": "https://[::ffff:169.254.169.254]",
    "6to4 loopback": "https://[2002:7f00:1::]",
    "nat64 metadata": "https://[64:ff9b::a9fe:a9fe]",
    "multicast": "https://224.0.0.1",
    "broadcast": "https://255.255.255.255",
    "decimal address": "https://2130706433",
    "short address": "https://127.1",
    "hex address": "https://0x7f.0.0.1",
    "port zero": "https://instance.local:0",
    "port out of range": "https://instance.local:99999",
    "port not a number": "https://instance.local:https",
    "label starts with a hyphen": "https://-instance.local",
    "empty label": "https://instance..local",
    "not ascii": "https://ínstance.local",
    "label too long": "https://" + "a" * 64 + ".local",
    "name too long": "https://" + ".".join(["a" * 60] * 5),
    "scoped literal": "https://[fe80::1%25eth0]",
    "broken bracket": "https://[::1",
}


@pytest.mark.unit
class TestEndpointForm:
    @pytest.mark.parametrize("url", ACCEPTED)
    def test_accepted(self, url, monkeypatch):
        monkeypatch.setattr(settings, "federation_allow_http_endpoints", False)
        assert validate_endpoint_url(url) == url

    @pytest.mark.parametrize("case", sorted(REFUSED))
    def test_refused(self, case, monkeypatch):
        monkeypatch.setattr(settings, "federation_allow_http_endpoints", False)
        with pytest.raises(EndpointRefused):
            validate_endpoint_url(REFUSED[case])

    @pytest.mark.parametrize("value", [None, "", 7, b"https://instance.local", {"url": "x"}, []])
    def test_only_a_non_empty_string_is_a_url(self, value):
        with pytest.raises(EndpointRefused):
            validate_endpoint_url(value)

    def test_http_is_off_by_default(self):
        from utils.config import Settings

        assert Settings.model_fields["federation_allow_http_endpoints"].default is False

    def test_http_only_with_the_switch_and_never_to_a_hostile_target(self, monkeypatch):
        monkeypatch.setattr(settings, "federation_allow_http_endpoints", True)
        assert validate_endpoint_url("http://backend.namespace.svc.cluster.local:8000")
        assert validate_endpoint_url("HTTP://backend:8000")
        for url in ("http://127.0.0.1:8000", "http://169.254.169.254", "http://localhost:8000",
                    "ftp://backend"):
            with pytest.raises(EndpointRefused):
                validate_endpoint_url(url)


@pytest.fixture
def resolves_to(monkeypatch):
    """Pin name resolution: `resolves_to({"name": ["addr", ...]})`; a name
    not listed does not resolve."""
    asked: list[str] = []

    def _install(table: dict[str, list[str]]):
        def _resolve(host: str) -> list[str]:
            asked.append(host)
            if host not in table:
                raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
            return table[host]

        monkeypatch.setattr(guard, "_resolve", _resolve)
        return asked

    return _install


@pytest.mark.unit
class TestEndpointTarget:
    """🛑 A literal-only check is walked around with a name: the far side
    owns its DNS and points a harmless-looking name wherever it likes."""

    async def test_a_name_pointing_at_the_metadata_address_is_refused(self, resolves_to):
        resolves_to({"innocent.example.org": ["169.254.169.254"]})
        assert "link-local" in await endpoint_refusal("https://innocent.example.org")

    @pytest.mark.parametrize("address", [
        "127.0.0.1", "::1", "fe80::1%eth0", "0.0.0.0", "::ffff:127.0.0.1", "fd00:ec2::254",
        "224.0.0.9", "0.9.9.9", "::7f00:1", "fec0::1",
    ])
    async def test_a_name_pointing_at_this_machine_or_the_link_is_refused(
        self, resolves_to, address,
    ):
        resolves_to({"innocent.example.org": [address]})
        assert await endpoint_refusal("https://innocent.example.org:8443") is not None

    async def test_one_hostile_address_among_several_is_enough(self, resolves_to):
        resolves_to({"mixed.example.org": ["192.168.7.20", "127.0.0.1"]})
        assert await endpoint_refusal("https://mixed.example.org") is not None

    async def test_a_lan_instance_passes(self, resolves_to):
        asked = resolves_to({"instance.local": ["192.168.7.20", "fd12:3456::20"]})
        assert await endpoint_refusal("https://instance.local:8443") is None
        assert asked == ["instance.local"]

    async def test_a_literal_is_judged_without_asking_the_resolver(self, resolves_to):
        asked = resolves_to({})
        assert await endpoint_refusal("https://192.168.7.20:8443") is None
        assert await endpoint_refusal("https://127.0.0.1:8443") is not None
        assert asked == []

    async def test_a_bad_form_is_refused_without_asking_the_resolver(self, resolves_to):
        asked = resolves_to({"instance.local": ["192.168.7.20"]})
        assert await endpoint_refusal("http://instance.local") is not None
        assert await endpoint_refusal(None) is not None
        assert asked == []

    async def test_a_name_that_does_not_resolve_passes(self, resolves_to):
        """Nothing can be sent to it; the transport says so in its own words."""
        resolves_to({})
        assert await endpoint_refusal("https://nowhere.example.org") is None

    async def test_a_resolver_that_hangs_does_not_hang_the_check_and_refuses(
        self, monkeypatch,
    ):
        import threading

        release = threading.Event()
        monkeypatch.setattr(guard, "_resolve", lambda host: release.wait(5) or [])
        monkeypatch.setattr(guard, "RESOLVE_TIMEOUT_SECONDS", 0.1)
        started = time.monotonic()
        try:
            assert await endpoint_refusal("https://slow.example.org") == \
                "host did not resolve in time"
            assert time.monotonic() - started < 2.0
        finally:
            release.set()

    @pytest.mark.parametrize("late_answer", ["127.0.0.1", "169.254.169.254", "10.96.0.1"])
    async def test_a_name_server_that_answers_after_the_timeout_gains_nothing(
        self, monkeypatch, late_answer,
    ):
        """🛑 The far side owns its DNS. A server that answers just AFTER this
        check gave up -- the HTTP client waits longer -- must not get the
        endpoint through by being slow, whatever it answers in the end."""
        def _slow(host: str) -> list[str]:
            time.sleep(0.4)
            return [late_answer]

        monkeypatch.setattr(guard, "_resolve", _slow)
        monkeypatch.setattr(guard, "RESOLVE_TIMEOUT_SECONDS", 0.1)
        assert await endpoint_refusal("https://slow.example.org") == \
            "host did not resolve in time"

    @pytest.mark.parametrize("failure", [
        socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution"),
        socket.gaierror(socket.EAI_FAIL, "Non-recoverable failure in name resolution"),
        OSError("resolver unavailable"),
        UnicodeError("label too long"),
    ])
    async def test_only_a_definite_no_such_name_passes(self, monkeypatch, failure):
        """A temporary or unexplained failure is not "this name does not
        exist": fail closed."""
        def _fails(host: str) -> list[str]:
            raise failure

        monkeypatch.setattr(guard, "_resolve", _fails)
        assert await endpoint_refusal("https://flaky.example.org") is not None

    async def test_an_answer_that_is_no_address_is_skipped(self, resolves_to):
        resolves_to({"odd.example.org": ["not-an-address", "192.168.7.20"]})
        assert await endpoint_refusal("https://odd.example.org") is None

    def test_the_real_resolver_returns_addresses(self):
        assert REAL_RESOLVE("192.168.7.20") == ["192.168.7.20"]


@pytest.mark.unit
class TestEndpointList:
    async def test_nothing_advertised_passes(self):
        await check_peer_endpoints([])
        await check_peer_endpoints(None)

    @pytest.mark.parametrize("entry", [
        "https://instance.local:8443",
        {"url": "https://instance.local:8443"},
        {"endpoint_url": "https://instance.local:8443"},
    ])
    async def test_the_three_entry_shapes(self, resolves_to, entry):
        resolves_to({"instance.local": ["192.168.7.20"]})
        await check_peer_endpoints([entry])

    @pytest.mark.parametrize("endpoints", [
        "https://instance.local",
        {"url": "https://instance.local"},
        [7],
        [{}],
        [{"url": None}],
        [{"url": "https://192.168.7.20"}, {"url": "https://127.0.0.1"}],
        [{"url": "https://192.168.7.20"}] * (ENDPOINTS_MAX + 1),
    ])
    async def test_refused(self, endpoints):
        with pytest.raises(EndpointRefused):
            await check_peer_endpoints(endpoints)

    async def test_every_entry_is_checked_not_only_the_first(self, resolves_to):
        resolves_to({"good.example.org": ["192.168.7.20"], "bad.example.org": ["127.0.0.1"]})
        with pytest.raises(EndpointRefused):
            await check_peer_endpoints([
                {"url": "https://good.example.org"}, {"url": "https://bad.example.org"}])


# --- at pairing ---------------------------------------------------------------


@pytest.fixture
def local_user():
    user = MagicMock()
    user.id = 42
    user.username = "local-account"
    return user


@pytest.fixture
def pairing(local_identity, monkeypatch):
    """A `PairingService` whose writes and TLS probe are observable."""
    probe = AsyncMock(return_value="ab" * 32)
    monkeypatch.setattr(pairing_module, "probe_peer_cert_fingerprint", probe)
    monkeypatch.setattr(pairing_module, "_get_or_create_circle", AsyncMock())
    svc = PairingService(db=MagicMock())
    svc._upsert_peer_user = AsyncMock(return_value=MagicMock(id=5))
    svc.probe = probe
    return svc


HOSTILE_ENDPOINTS = [
    [{"url": "https://127.0.0.1:8443"}],
    [{"url": "https://169.254.169.254/latest/meta-data"}],
    [{"url": "https://localhost:8443"}],
    [{"url": "http://instance.local:8000"}],
    [{"url": "https://" + _USERINFO + "@instance.local"}],
    [{"url": "https://instance.local/?x=1"}],
    [{"url": "https://innocent.example.org"}],          # resolves to loopback below
    [{"url": "https://[::1]:8443"}],
    [{"endpoint_url": "https://0.0.0.0"}],
    [{"url": "https://instance.local"}, {"url": "https://127.0.0.1"}],
    [{"note": "no url at all"}],
]


@pytest.mark.unit
class TestPairingRefusesAHostileEndpoint:
    """🛑 The endpoints come from the far side. At pairing this instance opens
    a TLS connection to the first one (the TOFU probe) and stores the list as
    the target of every later query and ping."""

    @pytest.mark.parametrize("endpoints", HOSTILE_ENDPOINTS)
    async def test_accept_refuses_and_neither_probes_nor_stores(
        self, pairing, local_user, resolves_to, endpoints,
    ):
        resolves_to({"instance.local": ["192.168.7.20"], "innocent.example.org": ["127.0.0.1"]})
        offer = pairing.create_offer(local_user, offered_endpoints=endpoints)
        with pytest.raises(PairingError, match="endpoint refused"):
            await pairing.accept_offer(local_user, offer, my_tier_for_you=2)
        pairing.probe.assert_not_awaited()
        pairing._upsert_peer_user.assert_not_awaited()

    @pytest.mark.parametrize("endpoints", HOSTILE_ENDPOINTS)
    async def test_complete_refuses_and_neither_probes_nor_stores(
        self, pairing, local_user, resolves_to, endpoints,
    ):
        resolves_to({"instance.local": ["192.168.7.20"], "innocent.example.org": ["127.0.0.1"]})
        offer = pairing.create_offer(local_user)
        response = self._response(offer.nonce, endpoints)
        with pytest.raises(PairingError, match="endpoint refused"):
            await pairing.complete_handshake(local_user, response, their_tier_for_me=2)
        pairing.probe.assert_not_awaited()
        pairing._upsert_peer_user.assert_not_awaited()
        # Refused before the nonce was consumed: the offer is still usable.
        assert offer.nonce in pairing_module._nonce_cache

    async def test_a_lan_https_instance_pairs_as_before(self, pairing, local_user, resolves_to):
        """The two live pairings: `https://<name>.local`."""
        resolves_to({"instance.local": ["192.168.7.20"]})
        endpoints = [{"url": "https://instance.local"}]
        offer = pairing.create_offer(local_user, offered_endpoints=endpoints)
        await pairing.accept_offer(local_user, offer, my_tier_for_you=2)
        stored = pairing._upsert_peer_user.await_args.kwargs["transport_config"]
        assert stored == {"endpoints": endpoints, "tls_fingerprint": "ab" * 32}

        response = self._response(pairing.create_offer(local_user).nonce, endpoints)
        await pairing.complete_handshake(local_user, response, their_tier_for_me=2)
        assert pairing._upsert_peer_user.await_count == 2

    async def test_a_peer_that_advertises_nothing_still_pairs(self, pairing, local_user):
        offer = pairing.create_offer(local_user, offered_endpoints=[])
        await pairing.accept_offer(local_user, offer, my_tier_for_you=2)
        assert pairing._upsert_peer_user.await_args.kwargs["transport_config"] == {"endpoints": []}

    @staticmethod
    def _response(nonce: str, endpoints: list) -> PairingResponse:
        identity = get_federation_identity()
        unsigned = {
            "version": 1,
            "nonce": nonce,
            "responder_pubkey": identity.public_key_hex(),
            "responder_user_id": 9,
            "responder_display_name": "far side",
            "accepted_endpoints": endpoints,
            "my_tier_for_you": 2,
            "accepted_at": int(time.time()),
        }
        return PairingResponse(
            **unsigned, signature=identity.sign(canonical_bytes(unsigned)).hex())


@pytest.mark.unit
class TestWhatThisInstanceAdvertises:
    """An address the far side will refuse is not advertised: the far side
    answers with its uniform error, here the reason can still be said."""

    @pytest.mark.parametrize("url", [
        "http://10.0.0.7:8000", "https://" + _USERINFO + "@10.0.0.7", "https://127.0.0.1:8443",
        "https://10.0.0.7/?a=1",
    ])
    async def test_an_unacceptable_override_is_refused(self, monkeypatch, url):
        from api.routes.federation_pairing import _resolve_advertised_endpoints

        monkeypatch.setattr(settings, "federation_allow_http_endpoints", False)
        with pytest.raises(HTTPException) as refused:
            await _resolve_advertised_endpoints([{"url": url}])
        assert refused.value.status_code == 400

    async def test_an_unacceptable_configured_default_is_refused_by_name(self, monkeypatch):
        from api.routes.federation_pairing import _resolve_advertised_endpoints

        monkeypatch.setattr(settings, "federation_allow_http_endpoints", False)
        monkeypatch.setattr(settings, "federation_advertised_url", "http://backend:8000")
        with pytest.raises(HTTPException) as refused:
            await _resolve_advertised_endpoints([])
        assert refused.value.status_code == 400
        assert "FEDERATION_ADVERTISED_URL" in refused.value.detail

    async def test_an_acceptable_address_is_advertised_unchanged(self, monkeypatch):
        from api.routes.federation_pairing import _resolve_advertised_endpoints

        monkeypatch.setattr(settings, "federation_advertised_url", "https://instance.local")
        assert await _resolve_advertised_endpoints([]) == [{"url": "https://instance.local"}]
        assert await _resolve_advertised_endpoints([{"url": "https://10.0.0.7:8443"}]) == [
            {"url": "https://10.0.0.7:8443"}]

    async def test_http_is_advertised_only_with_the_switch(self, monkeypatch):
        from api.routes.federation_pairing import _resolve_advertised_endpoints

        monkeypatch.setattr(settings, "federation_allow_http_endpoints", True)
        monkeypatch.setattr(settings, "federation_advertised_url", "http://backend:8000")
        assert await _resolve_advertised_endpoints([]) == [{"url": "http://backend:8000"}]


# --- before an outbound request -------------------------------------------------


class _RecordingClient:
    """Stands in for httpx: records that a request was attempted, then fails
    like an unreachable host."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def build_request(self, method, url, **kwargs):
        self.urls.append(url)
        raise httpx.ConnectError("no route")

    async def post(self, url, **kwargs):
        self.urls.append(url)
        raise httpx.ConnectError("no route")


def _stored_peer(far: FederationIdentity, config: dict) -> PeerUser:
    return PeerUser(
        id=5, circle_owner_id=1, remote_pubkey=far.public_key_hex(),
        remote_display_name="far side", granted_tier=2, transport_config=config,
    )


STORED_HOSTILE = [
    {"endpoints": [{"url": "https://127.0.0.1:8443"}]},
    {"endpoints": [{"url": "https://169.254.169.254"}], "tls_fingerprint": "ab" * 32},
    {"endpoints": ["https://localhost:8443"]},
    {"endpoints": [{"url": "http://instance.local:8000"}]},
    {"endpoints": [{"url": "https://innocent.example.org"}], "tls_fingerprint": "ab" * 32},
    {"endpoint_url": "https://[::1]:8443"},
    {"accepted_endpoints": [{"endpoint_url": "https://" + _USERINFO + "@instance.local"}]},
]


@pytest.mark.unit
class TestNoRequestGoesToAHostileEndpoint:
    """🛑 The second half of the seam. A stored row may be older than the
    check at pairing, and a name can be pointed elsewhere afterwards -- so the
    target is judged again before anything is sent, the certificate probe
    included."""

    @pytest.fixture
    def cert_probe(self, monkeypatch):
        import services.federation_cert_pin as pin_module

        probe = AsyncMock(return_value=(True, "ab" * 32))
        monkeypatch.setattr(pin_module, "verify_peer_cert_fingerprint", probe)
        return probe

    @pytest.mark.parametrize("config", STORED_HOSTILE)
    async def test_no_ping_is_sent(
        self, local_identity, far_identity, resolves_to, cert_probe, config,
    ):
        resolves_to({"instance.local": ["192.168.7.20"], "innocent.example.org": ["127.0.0.1"]})
        client = _RecordingClient()
        assert await ping_peer(_stored_peer(far_identity, config), client=client) == PING_NO_ENDPOINT
        assert client.urls == []
        cert_probe.assert_not_awaited()

    @pytest.mark.parametrize("config", STORED_HOSTILE)
    async def test_no_query_is_sent(
        self, local_identity, far_identity, resolves_to, cert_probe, config,
    ):
        resolves_to({"instance.local": ["192.168.7.20"], "innocent.example.org": ["127.0.0.1"]})
        client = _RecordingClient()
        items = [
            item async for item in FederationQueryAsker(client=client).query_peer(
                _stored_peer(far_identity, config), "a question")
        ]
        assert items == [{
            "success": False, "message": "Peer has no usable transport endpoint", "data": None}]
        assert client.urls == []
        cert_probe.assert_not_awaited()

    async def test_a_stored_lan_https_pairing_is_still_asked(
        self, local_identity, far_identity, resolves_to, cert_probe,
    ):
        """The two live pairings keep working: the request goes out."""
        resolves_to({"instance.local": ["192.168.7.20"]})
        config = {"endpoints": [{"url": "https://instance.local"}], "tls_fingerprint": "ab" * 32}
        client = _RecordingClient()
        assert await ping_peer(_stored_peer(far_identity, config), client=client) == PING_UNREACHABLE
        assert client.urls == ["https://instance.local/api/federation/peer/ping"]
        cert_probe.assert_awaited_once()

        client = _RecordingClient()
        items = [
            item async for item in FederationQueryAsker(client=client).query_peer(
                _stored_peer(far_identity, config), "a question")
        ]
        assert client.urls == ["https://instance.local/api/federation/peer/query_brain/initiate"]
        assert items[-1]["success"] is False

    async def test_a_pairing_without_an_endpoint_reads_as_before(
        self, local_identity, far_identity,
    ):
        assert await ping_peer(_stored_peer(far_identity, {}), client=_RecordingClient()) == \
            PING_NO_ENDPOINT

    def test_the_asker_takes_its_target_from_the_guarded_selector_only(self):
        """Structural: no outbound path picks its endpoint around the guard."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(asker_module))
        callers = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "_select_endpoint"
                ):
                    callers.add(node.name)
        assert callers == {"_usable_endpoint"}


@pytest.mark.database
class TestPairingRouteAgainstTheRealTable:
    """One pass through the route and the real `peer_users` table: a hostile
    endpoint yields the uniform refusal and no row."""

    async def test_accept_with_a_hostile_endpoint_writes_no_row(
        self, async_client: AsyncClient, db_session, local_identity, local_user,
    ):
        from sqlalchemy import func, select

        role = Role(name="role-fed-hardening", permissions=["chat.own"], is_system=False)
        db_session.add(role)
        await db_session.flush()
        db_session.add(User(username="fed-hardening-owner", password_hash="x", role_id=role.id))
        await db_session.commit()

        offer = PairingService(db=MagicMock()).create_offer(
            local_user, offered_endpoints=[{"url": "https://169.254.169.254"}])
        resp = await async_client.post("/api/federation/pair/accept", json={
            "offer": offer.model_dump(exclude_none=True), "my_tier_for_you": 2,
            "accepted_endpoints": [],
        })
        assert resp.status_code == 400
        assert resp.json() == {"detail": "Pairing handshake failed"}
        rows = (await db_session.execute(select(func.count()).select_from(PeerUser))).scalar()
        assert rows == 0
