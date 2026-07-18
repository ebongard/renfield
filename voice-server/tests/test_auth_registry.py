"""Registry auth mode (shared multi-client voice-server).

Covers the AUTH_MODE=registry surface added for the voice-server
consolidation: client-id → per-client verify URL dispatch, the pinned
{user_id} verify contract, bounded anonymous rows (honored only via the
dedicated anon listener port), fail-closed config, and regression checks
that local/callback modes ignore the new arguments.

Network is stubbed at auth._post_verify — no HTTP server needed.
"""
from __future__ import annotations

import pytest
from pydantic import SecretStr

import voice_server.auth as auth
from voice_server.auth import AuthError, authenticate
from voice_server.config import AuthClient, Settings, settings

_STRONG = "a-long-random-secret-key-not-the-default-0123456789"

_REGISTRY = {
    "reva": AuthClient(verify_url="http://reva.example/api/internal/auth/verify"),
    "xidra": AuthClient(verify_url="http://xidra.example/api/internal/auth/verify"),
    "renfield": AuthClient(anonymous=True),
}


@pytest.fixture()
def registry_mode(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "registry")
    monkeypatch.setattr(settings, "auth_clients", dict(_REGISTRY))
    monkeypatch.setattr(settings, "anon_port", 8081)


def _stub_verify(monkeypatch, responses):
    """responses: url → (status, payload) or an Exception to raise."""
    calls = []

    async def fake_post_verify(url, token):
        calls.append((url, token))
        r = responses[url]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(auth, "_post_verify", fake_post_verify)
    return calls


# ---------------------------------------------------------------- config

def test_registry_mode_with_empty_registry_refuses_to_start():
    with pytest.raises(ValueError):
        Settings(auth_mode="registry", auth_clients={},
                 secret_key=SecretStr(_STRONG))


def test_registry_mode_with_clients_starts():
    s = Settings(
        auth_mode="registry",
        auth_clients={"reva": {"verify_url": "http://reva.example/verify"}},
        secret_key=SecretStr(_STRONG),
    )
    assert s.auth_clients["reva"].verify_url == "http://reva.example/verify"


def test_client_row_rejects_both_shapes():
    with pytest.raises(ValueError):
        AuthClient(verify_url="http://x.example/verify", anonymous=True)


def test_client_row_rejects_neither_shape():
    with pytest.raises(ValueError):
        AuthClient()


def test_client_row_rejects_non_http_url():
    with pytest.raises(ValueError):
        AuthClient(verify_url="ftp://x.example/verify")


def test_anon_port_must_differ_from_primary():
    with pytest.raises(ValueError):
        Settings(
            auth_mode="registry",
            auth_clients={"renfield": {"anonymous": True}},
            port=8080, anon_port=8080,
            secret_key=SecretStr(_STRONG),
        )


# ------------------------------------------------------------ dispatch

@pytest.mark.asyncio
async def test_missing_client_id_rejected(registry_mode):
    with pytest.raises(AuthError, match="client id"):
        await authenticate("some-token", None)


@pytest.mark.asyncio
async def test_unknown_client_id_rejected_uniformly(registry_mode):
    # The message must not distinguish unknown-id from anon-on-wrong-port —
    # no client enumeration oracle.
    with pytest.raises(AuthError, match="^unknown client$"):
        await authenticate("some-token", "not-registered")


@pytest.mark.asyncio
async def test_token_routed_to_the_named_clients_verify_url(registry_mode, monkeypatch):
    calls = _stub_verify(monkeypatch, {
        "http://reva.example/api/internal/auth/verify": (200, {"user_id": "42"}),
    })
    payload = await authenticate("tok-a", "reva")
    assert payload["user_id"] == "42"
    assert payload["client_id"] == "reva"
    assert calls == [("http://reva.example/api/internal/auth/verify", "tok-a")]


@pytest.mark.asyncio
async def test_verify_rejection_becomes_auth_error(registry_mode, monkeypatch):
    _stub_verify(monkeypatch, {
        "http://reva.example/api/internal/auth/verify": (401, {"detail": "no"}),
    })
    with pytest.raises(AuthError, match="rejected"):
        await authenticate("tok-a", "reva")


@pytest.mark.asyncio
async def test_verify_transport_failure_becomes_auth_error(registry_mode, monkeypatch):
    _stub_verify(monkeypatch, {
        "http://reva.example/api/internal/auth/verify": AuthError(
            "auth callback unreachable: timeout"),
    })
    with pytest.raises(AuthError, match="unreachable"):
        await authenticate("tok-a", "reva")


@pytest.mark.asyncio
async def test_malformed_verify_payload_rejected(registry_mode, monkeypatch):
    # Pinned contract: user_id required. "sub" alone is NOT accepted.
    _stub_verify(monkeypatch, {
        "http://reva.example/api/internal/auth/verify": (200, {"sub": "42"}),
    })
    with pytest.raises(AuthError, match="malformed"):
        await authenticate("tok-a", "reva")


@pytest.mark.asyncio
async def test_non_dict_verify_payload_rejected(registry_mode, monkeypatch):
    _stub_verify(monkeypatch, {
        "http://reva.example/api/internal/auth/verify": (200, ["not", "a", "dict"]),
    })
    with pytest.raises(AuthError, match="malformed"):
        await authenticate("tok-a", "reva")


@pytest.mark.asyncio
async def test_missing_token_for_verify_row_rejected(registry_mode):
    with pytest.raises(AuthError, match="missing token"):
        await authenticate("", "reva")


# ----------------------------------------------------------- anonymous

@pytest.mark.asyncio
async def test_anonymous_row_honored_on_anon_port(registry_mode):
    payload = await authenticate("", "renfield", via_anon_port=True)
    assert payload["user_id"] == "anonymous"
    assert payload["client_id"] == "renfield"


@pytest.mark.asyncio
async def test_anonymous_row_rejected_on_primary_port(registry_mode):
    # An ingress-reachable caller claiming the household's client id gets the
    # same uniform rejection as an unknown client.
    with pytest.raises(AuthError, match="^unknown client$"):
        await authenticate("", "renfield", via_anon_port=False)


@pytest.mark.asyncio
async def test_anonymous_row_ignores_supplied_token(registry_mode):
    # Household has no signing keys to validate against; a stray token is
    # ignored rather than routed anywhere.
    payload = await authenticate("stray-token", "renfield", via_anon_port=True)
    assert payload["user_id"] == "anonymous"


# --------------------------------------------------- legacy-mode regression

@pytest.mark.asyncio
async def test_local_mode_ignores_client_id_and_port(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "auth_required", False)
    payload = await authenticate("", "reva", via_anon_port=True)
    assert payload["sub"] == "anonymous"


@pytest.mark.asyncio
async def test_callback_mode_ignores_client_id(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "callback")
    monkeypatch.setattr(settings, "auth_callback_url", "http://backend.example/verify")
    calls = _stub_verify(monkeypatch, {
        "http://backend.example/verify": (200, {"user_id": "7"}),
    })
    payload = await authenticate("tok", "ignored-client-id")
    assert payload["user_id"] == "7"
    assert calls[0][0] == "http://backend.example/verify"
