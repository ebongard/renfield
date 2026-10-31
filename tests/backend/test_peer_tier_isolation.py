"""F-ID-3 -- the discriminator in the pending record, and why its default counts.

Found in the `/review` of PR #1420: a pending record from an OLDER version
knows neither `mapped` nor `peer_owner_id`. With the Redis store that is
exactly the normal case during a rolling deploy (`_from_jsonable`: "Missing
keys still fall back to the dataclass defaults -- the common older-row case").

🛑 Such a row carries `asker_local_user_id = peer.remote_user_id`, i.e. the
borrowed foreign id that F-ID-3 abolished. Without a discriminator it would
look like a valid mapped record and would run with exactly that id -- with auth
off it would even find the single-user shortcut, and with it the whole brain.

Today `FEDERATION_PENDING_USE_REDIS` is off on both instances, so the window is
zero. But a finding that is harmless only "while flag F is off" has an expiry
date -- hence the guard instead of trusting the flag.
"""
import pytest

from services.federation_pending_store import _PendingRequest
from services.federation_query_responder import PendingScopeError, _peer_scope_for

pytestmark = [pytest.mark.unit, pytest.mark.backend]


def _record(**over) -> _PendingRequest:
    defaults = dict(
        request_id="r1", asker_pubkey="ab" * 32, peer_user_id=7,
        asker_local_user_id=None, max_visible_tier=2,
        query="was weisst du?", initiated_at=0.0,
    )
    defaults.update(over)
    return _PendingRequest(**defaults)


def test_a_fallback_record_yields_the_pairing_scope():
    scope = _peer_scope_for(_record(peer_owner_id=1, mapped=False))
    assert scope is not None
    assert (scope.owner_id, scope.tier) == (1, 2)


def test_a_mapped_record_yields_no_peer_scope():
    """Mapped means: runs AS the linked person, with full circle access."""
    assert _peer_scope_for(_record(asker_local_user_id=99, mapped=True)) is None


def test_an_OLD_record_is_refused_instead_of_silently_widened():
    """🛑 The actual guard.

    A row from an older version: no `mapped`, no `peer_owner_id`, but the
    borrowed foreign id in `asker_local_user_id`. It must NOT pass as a mapped
    record.
    """
    legacy = _record(asker_local_user_id=42)   # 42 = peer.remote_user_id, as before
    assert legacy.mapped is False, "the default must be on the safe side"
    assert legacy.peer_owner_id is None
    with pytest.raises(PendingScopeError):
        _peer_scope_for(legacy)


def test_the_default_of_the_discriminator_is_the_SAFE_side():
    """Negative control on the default: were `mapped` to default to `True`,
    the test above it would pass without checking anything."""
    assert _PendingRequest.__dataclass_fields__["mapped"].default is False
    assert _PendingRequest.__dataclass_fields__["peer_owner_id"].default is None


# =============================================================================
# Resolving a pairing -- found on 2026-10-28 while checking the live situation
# =============================================================================
#
# 🛑 The owner found it at the surface: "on x-ren there is a peer, on the
# household there is not." The peers page shows only one's OWN, non-revoked
# rows -- the active row in the household belonged to account 1, the one
# belonging to the person asking was revoked. Looking into it turned up the
# more serious thing: the responder looked up its pairing by `remote_pubkey`
# ALONE, with `scalar_one_or_none()`.
#
# But the uniqueness index is `(circle_owner_id, remote_pubkey)`: two local
# people may pair with the same far side. Then `scalar_one_or_none()` raises --
# every federated request ends as a 500. That was exactly what lay ahead: id=2
# (account 1, active) and id=3 (account 2, revoked), both with x-idra's key;
# pairing again would have revived the second one.

import inspect


def test_the_responder_no_longer_resolves_a_peer_by_pubkey_ALONE():
    """Structural test: `scalar_one_or_none()` is gone from the key lookup.

    🛑 The BODY is checked, not the source as a string -- the comment above it
    EXPLAINS the old call and would match a text search. I made that mistake
    twice on 2026-10-27.
    """
    import ast
    import textwrap

    from services.federation_query_responder import FederationQueryResponder

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(FederationQueryResponder._lookup_peer)))
    fn = tree.body[0]
    body_src = ast.unparse(ast.Module(
        body=[k for k in fn.body if not (
            isinstance(k, ast.Expr) and isinstance(k.value, ast.Constant)
            and isinstance(k.value.value, str))],
        type_ignores=[]))
    assert "scalar_one_or_none" not in body_src, body_src
    # Negative control: the body is not empty, so the check measures something.
    assert "scalars" in body_src and "revoked_at" in body_src, body_src


def test_the_pairing_refuses_a_second_owner_for_the_same_instance():
    """🛑 The cause, not merely its detection.

    Without this block the ambiguous situation would keep arising, and the
    responder would then refuse ALL requests -- a silent total outage of the
    federation instead of a readable refusal while pairing.
    """
    import ast
    import textwrap

    from services.pairing_service import PairingService

    source = textwrap.dedent(inspect.getsource(PairingService._upsert_peer_user))
    tree = ast.parse(source)
    body_src = ast.unparse(tree)
    assert "circle_owner_id != owner_user_id" in body_src, body_src
    # Since the review of PR #1421 the VISIBLE subclass -- the uniform 400
    # would have swallowed the reason.
    assert "AlreadyPairedElsewhere" in body_src, body_src
    # And it sits in `_upsert_peer_user`, so it applies on accept AND on
    # complete -- not just at one of the two steps.
    import services.pairing_service as ps
    callers = [
        n for n in ast.walk(ast.parse(inspect.getsource(ps)))
        if isinstance(n, ast.Attribute) and n.attr == "_upsert_peer_user"
    ]
    assert len(callers) >= 2, f"only {len(callers)} callers found"


def test_the_already_paired_refusal_is_VISIBLE_not_swallowed():
    """🛑 Found in the review of this PR.

    The route layer catches every `PairingError` and turns it into a uniform
    "Pairing handshake failed" -- no oracle. That rule protects the
    CRYPTOGRAPHIC checks (signature, nonce, expiry): nobody should be able to
    read off which of them fired.

    "Another local person is already paired" is something else: local state
    that the signed-in person is allowed to change anyway. Kept opaque it sends
    them to the wrong place -- exactly this failure mode cost an hour of
    searching on 2026-10-27.

    Hence a class of its own and a catch of its own BEFORE the uniform one.
    This test holds the order: put the uniform catch first and the message is
    gone again, and nothing but this test notices.
    """
    import ast
    import inspect

    from api.routes import federation_pairing
    from services.pairing_service import AlreadyPairedElsewhere, PairingError

    assert issubclass(AlreadyPairedElsewhere, PairingError), (
        "must stay a subclass, or it escapes the uniform catch entirely")

    tree = ast.parse(inspect.getsource(federation_pairing))
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        names = [
            h.type.id for t in ast.walk(fn) if isinstance(t, ast.Try)
            for h in t.handlers if isinstance(h.type, ast.Name)
        ]
        if "PairingError" in names:
            assert "AlreadyPairedElsewhere" in names, (
                f"{fn.name}: catches PairingError, but not the visible special case")
            assert names.index("AlreadyPairedElsewhere") < names.index("PairingError"), (
                f"{fn.name}: the uniform catch comes BEFORE the special case and "
                "swallows it")
