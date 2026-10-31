"""F-ID-2 -- the person handshake, and above all its refusals.

The occasion (2026-10-27): until then a person link could only be ASSERTED by
an administrator. The handshake replaces the assertion with a proof -- which
makes it the place where a mistake is expensive: an accepted link lifts the
pairing tier entirely for the person concerned (`enforce_circles=False`, that
person's own full reach).

🛑 The tests that carry more here than the happy path:

- **The offer names its recipient.** Without this check an offer intended for B
  could be submitted at C, and C would write a row for a person nobody ever
  consented for.
- **An offer is not transferable.** `stated_by` must be the signed-in person,
  otherwise a stranger completes an intercepted offer in THEIR own name.
- **No half link.** Every refusal must leave NO row behind; the test checks
  that on the call counter of `create_link`, not on the return value.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519

from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.link_handshake_service import (
    LINK_OFFER_TTL_SECONDS,
    LinkConfirmation,
    LinkHandshakeError,
    LinkHandshakeService,
    LinkOffer,
)

pytestmark = [pytest.mark.unit, pytest.mark.backend, pytest.mark.asyncio]


# =============================================================================
# Scaffolding
# =============================================================================


@pytest.fixture
def instance_a(tmp_path):
    """Instance A via the singleton -- the way the production code sees it."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "a_key")
    yield get_federation_identity()
    reset_federation_identity_for_tests()


@pytest.fixture
def instance_b():
    """Instance B built ALONGSIDE, without the singleton -- the pattern from
    `test_federation_pairing.py` that lets two instances be simulated in one
    process."""
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


def _user(uid: int):
    u = MagicMock()
    u.id = uid
    return u


def _peer(pid: int, pubkey: str):
    p = MagicMock()
    p.id = pid
    p.remote_pubkey = pubkey
    p.revoked_at = None
    return p


@pytest.fixture
def fresh_nonce_store():
    """The nonce store is module-global -- without clearing it one test bleeds
    into the next and `Nonce already used` fires for no reason."""
    from services.federation_pending_store import get_pending_store, reset_store_for_tests
    reset_store_for_tests()
    yield
    import asyncio
    with __import__("contextlib").suppress(Exception):
        asyncio.get_event_loop().run_until_complete(get_pending_store().clear_for_tests())
    reset_store_for_tests()


def _service(identity, *, peer=None, links=None):
    """A service with a stubbed identity, a stubbed peer lookup and a counted
    `create_link`."""
    svc = LinkHandshakeService(db=MagicMock())
    svc.identity = identity
    svc._peer_by_pubkey = AsyncMock(
        return_value=peer if peer is not None else _peer(7, "ab" * 32))
    return svc


@pytest.fixture
def create_link_calls(monkeypatch):
    """Counts real write attempts. A refusal MUST stay at 0."""
    import services.link_handshake_service as mod
    calls = []

    async def _fake(db, **kw):
        calls.append(kw)
        row = MagicMock()
        row.id = 100 + len(calls)
        row.peer_id = kw["peer_id"]
        return row

    monkeypatch.setattr(mod, "create_link", _fake)
    return calls


# =============================================================================
# The happy path
# =============================================================================


async def test_both_sides_write_the_SAME_querier_ref(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """🛑 The core: the two rows are connected only by the same `querier_ref`.
    If they drift apart, the far side never resolves the person and falls back
    silently to the pairing tier."""
    u, x = _user(1), _user(9)

    a = _service(instance_a, peer=_peer(7, instance_b.public_key_hex()))
    offer = a.create_offer(u, _peer(7, instance_b.public_key_hex()))

    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))
    confirmation = await b.accept_offer(x, offer)

    link = await a.complete(u, offer, confirmation)

    assert len(create_link_calls) == 2, create_link_calls
    assert create_link_calls[0]["querier_ref"] == create_link_calls[1]["querier_ref"]
    assert create_link_calls[0]["local_user_id"] == 9   # X on B
    assert create_link_calls[1]["local_user_id"] == 1   # U on A
    assert link.id


async def test_step_one_writes_NOTHING(instance_a, create_link_calls):
    """If the person aborts after the offer, no half link may be left standing."""
    a = _service(instance_a)
    a.create_offer(_user(1), _peer(7, "cd" * 32))
    assert create_link_calls == []


# =============================================================================
# Refusals -- and every one of them WITHOUT a row
# =============================================================================


async def test_an_offer_for_SOMEONE_ELSE_is_refused(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """🛑 The offer names its recipient. An offer intended for B must not pass
    at C -- otherwise C would write a row for a person nobody ever consented
    for."""
    instance_c = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
    a = _service(instance_a)
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))

    c = _service(instance_c, peer=_peer(5, instance_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="different responder"):
        await c.accept_offer(_user(9), offer)
    assert create_link_calls == []


async def test_an_offer_is_NOT_transferable(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """🛑 `stated_by` binds the offer to the person who declared it. Without
    the check another signed-in person completes an intercepted offer in THEIR
    own name."""
    a = _service(instance_a, peer=_peer(7, instance_b.public_key_hex()))
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))
    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))
    confirmation = await b.accept_offer(_user(9), offer)
    create_link_calls.clear()

    with pytest.raises(LinkHandshakeError, match="different user"):
        await a.complete(_user(2), offer, confirmation)   # NOT user 1
    assert create_link_calls == []


async def test_a_tampered_offer_is_refused(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """A changed field breaks the signature -- here the person's id, which is
    exactly the field worth forging."""
    a = _service(instance_a)
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))
    tampered = offer.model_copy(update={"stated_by": 999})

    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="signature"):
        await b.accept_offer(_user(9), tampered)
    assert create_link_calls == []


async def test_a_FORGED_offer_from_an_unknown_key_is_refused(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """A stranger builds a formally impeccable offer and signs it with their
    own key. But the peer row names A's key."""
    stranger = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
    now = int(time.time())
    unsigned = {
        "version": 1,
        "initiator_pubkey": instance_a.public_key_hex(),   # CLAIMS to be A
        "responder_pubkey": instance_b.public_key_hex(),
        "stated_by": 1,
        "querier_ref": "ef" * 32,
        "nonce": "0123456789abcdef0123456789abcdef",
        "issued_at": now,
        "expires_at": now + 600,
    }
    forged = LinkOffer(
        **unsigned, signature=stranger.sign(canonical_bytes(unsigned)).hex())

    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="signature"):
        await b.accept_offer(_user(9), forged)
    assert create_link_calls == []


async def test_an_expired_offer_is_refused(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    a = _service(instance_a)
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))
    past = int(time.time()) - LINK_OFFER_TTL_SECONDS - 1
    expired = offer.model_copy(update={"issued_at": past, "expires_at": past + 1})

    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="expired"):
        await b.accept_offer(_user(9), expired)
    assert create_link_calls == []


async def test_a_confirmation_from_ANOTHER_instance_is_refused(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """The confirmation must come from the instance the offer was meant for."""
    instance_c = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
    a = _service(instance_a, peer=_peer(7, instance_b.public_key_hex()))
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))

    # C confirms, although the offer names B.
    unsigned = {
        "version": 1,
        "nonce": offer.nonce,
        "querier_ref": offer.querier_ref,
        "responder_pubkey": instance_c.public_key_hex(),
        "responder_user_id": 9,
        "accepted_at": int(time.time()),
    }
    foreign_confirmation = LinkConfirmation(
        **unsigned, signature=instance_c.sign(canonical_bytes(unsigned)).hex())

    with pytest.raises(LinkHandshakeError, match="different instance"):
        await a.complete(_user(1), offer, foreign_confirmation)
    assert create_link_calls == []


async def test_a_confirmation_answering_ANOTHER_offer_is_refused(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """Two offers side by side: the confirmation of one must not complete the
    other."""
    a = _service(instance_a, peer=_peer(7, instance_b.public_key_hex()))
    first_offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))
    second_offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))

    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))
    confirmation_of_second = await b.accept_offer(_user(9), second_offer)
    create_link_calls.clear()

    with pytest.raises(LinkHandshakeError, match="does not answer"):
        await a.complete(_user(1), first_offer, confirmation_of_second)
    assert create_link_calls == []


async def test_WITHOUT_a_pairing_there_is_nothing_to_link(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """🛑 The handshake PRESUPPOSES the instance pairing, it does not replace
    it. Without an active peer row there is no key to check against."""
    a = _service(instance_a)
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))

    b = _service(instance_b)
    b._peer_by_pubkey = AsyncMock(
        side_effect=LinkHandshakeError("No active pairing with that instance"))
    with pytest.raises(LinkHandshakeError, match="No active pairing"):
        await b.accept_offer(_user(9), offer)
    assert create_link_calls == []


async def test_the_same_offer_cannot_be_accepted_twice_in_the_nonce_window(
    instance_a, instance_b, create_link_calls, fresh_nonce_store,
):
    """The cheap early rejector against a double submit.

    🛑 It is NOT a wall against replay: the store keeps a nonce only for
    `NONCE_WINDOW_SECONDS` (60 s), while the offer lives 600 s. What really
    holds is the uniqueness index on `(peer_id, querier_ref)` -- see the
    service's module docstring. This test checks exactly the slice the nonce
    covers, and no more.
    """
    a = _service(instance_a)
    offer = a.create_offer(_user(1), _peer(7, instance_b.public_key_hex()))
    b = _service(instance_b, peer=_peer(3, instance_a.public_key_hex()))

    await b.accept_offer(_user(9), offer)
    with pytest.raises(LinkHandshakeError, match="Nonce already used"):
        await b.accept_offer(_user(9), offer)
    assert len(create_link_calls) == 1


# =============================================================================
# Structural test
# =============================================================================


def test_the_handshake_routes_are_NOT_admin_gated():
    """🛑 The entire point of the piece.

    The admin assertion already exists (`federation_user_links`); this
    handshake replaces it, because it PROVES control over BOTH accounts instead
    of believing an administrator. Hang a `require_permission(ADMIN)` in front
    of it and the proof is an assertion again, with more steps -- and nothing
    but this test notices.

    🛑 What is checked are the routes' REAL dependencies, not the source. The
    first version grepped for "require_permission" and tripped over the comment
    that promises exactly that -- a test measuring the prose instead of the
    effect. The counter-check below shows that the detector fires at all.
    """
    from api.routes import federation_links, federation_user_links

    def guards(router) -> set[str]:
        names: set[str] = set()
        for route in router.routes:
            for dep in getattr(route, "dependant", None).dependencies:
                if dep.call is not None:
                    names.add(dep.call.__name__)
        return names

    open_deps = guards(federation_links.router)
    gated_deps = guards(federation_user_links.router)

    # Negative control first: the neighbour IS gated, so the detector measures.
    assert "permission_checker" in gated_deps, (
        "The negative control no longer bites — `federation_user_links` is no "
        f"longer admin-gated. Found: {sorted(gated_deps)}")
    assert "permission_checker" not in open_deps, (
        "The person handshake is admin-gated and therefore pointless. "
        f"Found: {sorted(open_deps)}")


def test_both_protocols_sign_the_SAME_bytes():
    """🛑 A copied second canonicalization would be the silent break: both
    sides would sign away diligently, and nothing would verify any more."""
    from services import pairing_service

    assert pairing_service._canonical_bytes is canonical_bytes
