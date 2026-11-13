"""The name an INSTANCE goes by on its peers.

A pairing used to carry the name of the PERSON who paired; with one person
owning both instances each side showed the other under that person's name. The
far instance now sends a name of its own, as a separately signed attachment.

What is pinned here:

  - sanitising: letters of any script and a hyphen pass unchanged, control and
    format characters go, the length is capped;
  - the attachment: bound to the key AND to the envelope it rides on;
  - who may teach a name: only a signature-verified, paired, non-revoked key --
    on the pairing handshake, on a query, and on the answer to a query;
  - the fallback order of the label;
  - mixed versions, both directions: an instance one release behind verifies a
    new envelope, and a new instance verifies an old one;
  - an instance WITHOUT a name puts exactly the old bytes on the wire.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import BaseModel, Field

import services.federation_query_asker as asker_module
import services.federation_query_responder as responder_module
from models.database import PeerUser, Role, User
from services import pairing_service as ps
from services.federation_identity import (
    FederationIdentity,
    canonical_bytes,
    get_federation_identity,
    init_federation_identity,
    reset_federation_identity_for_tests,
)
from services.federation_instance_name import (
    INSTANCE_NAME_MAX_CHARS,
    NAME_ATTACHMENT_PURPOSE,
    endpoint_host,
    host_of_url,
    local_instance_name,
    peer_instance_label,
    remember_remote_instance_name,
    sanitize_instance_name,
    sign_name_attachment,
    stored_instance_name,
    verify_name_attachment,
    wire_body,
)
from services.federation_pending_store import get_pending_store, reset_store_for_tests
from services.federation_query_asker import FederationQueryAsker
from services.federation_query_responder import (
    FederationQueryError,
    FederationQueryResponder,
    _clear_state_for_tests,
)
from services.federation_query_schemas import (
    STATUS_COMPLETE,
    QueryBrainInitiateRequest,
    QueryBrainInitiateResponse,
    QueryBrainRetrieveResponse,
    complete_canonical_payload,
    initiate_canonical_payload,
)
from services.pairing_service import (
    PairingOffer,
    PairingResponse,
    PairingService,
    _canonical_bytes,
    _clear_nonce_cache_for_tests,
)
from utils.config import settings

# =============================================================================
# Fixtures
# =============================================================================


def _fresh_identity() -> FederationIdentity:
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


@pytest.fixture
async def local_identity(tmp_path):
    """THIS instance's identity (the process singleton) on clean state."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "local_key")
    _clear_nonce_cache_for_tests()
    reset_store_for_tests()
    await _clear_state_for_tests()
    yield get_federation_identity()
    reset_federation_identity_for_tests()
    _clear_nonce_cache_for_tests()
    await _clear_state_for_tests()
    reset_store_for_tests()


@pytest.fixture
def far_identity() -> FederationIdentity:
    """The far instance's identity -- independent of the singleton."""
    return _fresh_identity()


@pytest.fixture
def named(monkeypatch):
    """Give THIS instance a name (and no advertised URL to fall back on)."""
    def _set(name: str = "Instance A", advertised_url: str = "") -> None:
        monkeypatch.setattr(settings, "federation_instance_name", name)
        monkeypatch.setattr(settings, "federation_advertised_url", advertised_url)
    return _set


@pytest.fixture
def nameless(named):
    named("", "")


def _attachment(identity: FederationIdentity, name: str, envelope_signature: str) -> dict[str, str]:
    """An attachment as the FAR side would build it -- by hand, so the far
    side's name does not depend on this process's settings."""
    payload = {
        "purpose": NAME_ATTACHMENT_PURPOSE,
        "instance_name": name,
        "pubkey": identity.public_key_hex(),
        "envelope_signature": envelope_signature,
    }
    return {
        "instance_name": name,
        "instance_name_signature": identity.sign(canonical_bytes(payload)).hex(),
    }


# =============================================================================
# Sanitising
# =============================================================================


class TestSanitising:
    @pytest.mark.unit
    @pytest.mark.parametrize("name", [
        "x-idra",            # the hyphen is part of a real instance's name
        "Haushalt",
        "Küche",             # non-ASCII letters
        "Büro 2 (Süd)",
        "Čeština-Ω",
        "東京",
        "Instance A",
        "a.b_c+d",
    ])
    def test_an_ordinary_name_survives_unchanged(self, name):
        assert sanitize_instance_name(name) == name

    @pytest.mark.unit
    @pytest.mark.parametrize(("raw", "expected"), [
        ("a\x00b", "ab"),                      # control character
        ("a\x1b[31mb", "a[31mb"),              # ESC of a terminal sequence
        ("evil‮gnp.exe", "evilgnp.exe"),  # right-to-left override
        ("​name‍", "name"),          # zero-width space / joiner
        ("﻿name", "name"),                # byte-order mark
        ("a\n\tb", "a b"),                     # line break and tab become ONE space
        ("  spaced   out  ", "spaced out"),
        ("private", "private"),          # private-use code point
    ])
    def test_control_and_format_characters_are_removed(self, raw, expected):
        assert sanitize_instance_name(raw) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(("raw", "expected"), [
        ("\uff58\uff0d\uff49\uff44\uff52\uff41", "x-idra"),   # fullwidth letters and hyphen
        ("\ufb01le", "file"),                                   # the "fi" ligature
        ("Ku\u0308che", "K\u00fcche"),                          # decomposed umlaut -> composed
        ("\u2460 Haus", "1 Haus"),                              # circled digit
    ])
    def test_a_compatibility_form_is_stored_as_what_it_stands_for(self, raw, expected):
        """NFKC: two names that look the same are stored the same."""
        assert sanitize_instance_name(raw) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize("raw", [
        "\u3164",                 # Hangul filler -- category Lo, a "letter"
        "\u115f\u1160",           # Hangul choseong / jungseong fillers
        "\uffa0",                 # halfwidth Hangul filler
        "\u2800\u2800\u2800",     # blank braille patterns -- category So, a "symbol"
        "\ufe0f",                 # variation selector
        "\u034f",                 # combining grapheme joiner
        "\u0301",                 # a lone combining acute
        "\u0301\u0308\u0327",     # nothing but combining marks
        "\u3164 \u2800 \ufe0f",   # fillers with spaces between them
        "\U000e0100",             # variation selector supplement
    ])
    def test_a_name_nobody_can_see_is_no_name(self, raw):
        """🛑 Each of these passes a test on control and format characters and
        renders as an EMPTY label -- a row, a node and an audit entry without
        a name on them."""
        assert sanitize_instance_name(raw) is None

    @pytest.mark.unit
    @pytest.mark.parametrize(("raw", "expected"), [
        ("a\u3164b", "ab"),
        ("\u2800Instance\u2800B\u2800", "InstanceB"),
        ("\u0301abc", "abc"),            # a mark with nothing to sit on
        ("x \u0301y", "x y"),            # a space is no base either
        ("\u2764\ufe0f Haus", "\u2764 Haus"),
    ])
    def test_blank_characters_and_stray_marks_are_dropped_from_a_real_name(self, raw, expected):
        assert sanitize_instance_name(raw) == expected

    @pytest.mark.unit
    def test_combining_marks_are_capped_per_base_character(self):
        """Hundreds of marks on one letter paint over the rows around it."""
        import unicodedata

        from services.federation_instance_name import MAX_COMBINING_MARKS_PER_BASE

        assert MAX_COMBINING_MARKS_PER_BASE == 2
        cleaned = sanitize_instance_name("Z" + "\u0338" * 300 + "al" + "\u0489" * 300 + "go")
        marks = [ch for ch in cleaned if unicodedata.category(ch).startswith("M")]
        assert "".join(ch for ch in cleaned if ch not in marks) == "Zalgo"
        assert len(marks) == 2 * MAX_COMBINING_MARKS_PER_BASE
        # A stacked diacritic of a real script keeps both of its marks.
        assert sanitize_instance_name("Vie\u0323\u0302t") == unicodedata.normalize(
            "NFKC", "Vie\u0323\u0302t")

    @pytest.mark.unit
    def test_the_length_is_capped(self):
        assert sanitize_instance_name("x" * 500) == "x" * INSTANCE_NAME_MAX_CHARS
        assert len(sanitize_instance_name("Ü" * 500)) == INSTANCE_NAME_MAX_CHARS

    @pytest.mark.unit
    def test_the_cap_matches_the_column(self):
        assert PeerUser.__table__.c.remote_instance_name.type.length == INSTANCE_NAME_MAX_CHARS

    @pytest.mark.unit
    @pytest.mark.parametrize("raw", [None, 123, b"bytes", "", "   ", "\x00\x01", "​"])
    def test_nothing_usable_is_None(self, raw):
        assert sanitize_instance_name(raw) is None


class TestLocalName:
    @pytest.mark.unit
    def test_the_setting_wins(self, named):
        named("x-idra", "https://far.example.org:8443")
        assert local_instance_name() == "x-idra"

    @pytest.mark.unit
    def test_falls_back_to_the_host_of_the_advertised_url(self, named):
        named("", "https://far.example.org:8443/some/path")
        assert local_instance_name() == "far.example.org"

    @pytest.mark.unit
    def test_neither_means_no_name_at_all(self, nameless):
        assert local_instance_name() is None

    @pytest.mark.unit
    @pytest.mark.parametrize(("url", "host"), [
        ("https://far.example.org:8443/x", "far.example.org"),
        ("http://10.0.0.7:8000", "10.0.0.7"),
        ("", None), ("   ", None), (None, None), ("not a url", None), (42, None),
    ])
    def test_host_of_url(self, url, host):
        assert host_of_url(url) == host


# =============================================================================
# The signed attachment
# =============================================================================

ENVELOPE_SIG = "ab" * 64
OTHER_ENVELOPE_SIG = "cd" * 64


class TestAttachment:
    @pytest.mark.unit
    def test_round_trip(self, local_identity, named):
        named("Instance A")
        att = sign_name_attachment(local_identity, ENVELOPE_SIG)
        assert set(att) == {"instance_name", "instance_name_signature"}
        assert verify_name_attachment(
            name=att["instance_name"],
            signature_hex=att["instance_name_signature"],
            signer_pubkey_hex=local_identity.public_key_hex(),
            envelope_signature_hex=ENVELOPE_SIG,
        ) == "Instance A"

    @pytest.mark.unit
    def test_an_instance_without_a_name_signs_nothing(self, local_identity, nameless):
        assert sign_name_attachment(local_identity, ENVELOPE_SIG) == {}

    @pytest.mark.unit
    def test_it_cannot_be_lifted_onto_another_envelope(self, far_identity):
        """Bound to the envelope's own signature: an attachment recorded from
        one request proves nothing on the next."""
        att = _attachment(far_identity, "Instance B", ENVELOPE_SIG)
        assert verify_name_attachment(
            name=att["instance_name"],
            signature_hex=att["instance_name_signature"],
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=OTHER_ENVELOPE_SIG,
        ) is None

    @pytest.mark.unit
    def test_another_key_cannot_vouch_for_the_name(self, far_identity):
        stranger = _fresh_identity()
        att = _attachment(stranger, "Instance B", ENVELOPE_SIG)
        assert verify_name_attachment(
            name=att["instance_name"],
            signature_hex=att["instance_name_signature"],
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=ENVELOPE_SIG,
        ) is None

    @pytest.mark.unit
    def test_a_changed_name_does_not_verify(self, far_identity):
        att = _attachment(far_identity, "Instance B", ENVELOPE_SIG)
        assert verify_name_attachment(
            name="Instance C",
            signature_hex=att["instance_name_signature"],
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=ENVELOPE_SIG,
        ) is None

    @pytest.mark.unit
    @pytest.mark.parametrize("signature", [None, "", "zz", "00" * 64, "ab" * 300, 7])
    def test_a_missing_or_broken_signature_is_no_name(self, far_identity, signature):
        assert verify_name_attachment(
            name="Instance B",
            signature_hex=signature,
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=ENVELOPE_SIG,
        ) is None

    @pytest.mark.unit
    @pytest.mark.parametrize("envelope_signature", [None, ""])
    def test_without_an_envelope_signature_there_is_nothing_to_bind_to(
        self, far_identity, envelope_signature,
    ):
        att = _attachment(far_identity, "Instance B", "")
        assert verify_name_attachment(
            name=att["instance_name"],
            signature_hex=att["instance_name_signature"],
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=envelope_signature,
        ) is None

    @pytest.mark.unit
    def test_a_verified_name_is_still_sanitised(self, far_identity):
        """The signature is checked over the name AS SENT, the result is what
        gets stored: a paired peer cannot push control characters or 500
        characters into the row either."""
        raw = "Inst‮ance\x00 B" + "x" * 200
        att = _attachment(far_identity, raw, ENVELOPE_SIG)
        verified = verify_name_attachment(
            name=att["instance_name"],
            signature_hex=att["instance_name_signature"],
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=ENVELOPE_SIG,
        )
        assert verified == ("Instance B" + "x" * 200)[:INSTANCE_NAME_MAX_CHARS]

    @pytest.mark.unit
    def test_the_purpose_separates_it_from_every_other_signed_structure(self, far_identity):
        """Without the purpose field the same key's signature over a look-alike
        dict would pass. Sign the payload WITHOUT it and it must not verify."""
        payload = {
            "instance_name": "Instance B",
            "pubkey": far_identity.public_key_hex(),
            "envelope_signature": ENVELOPE_SIG,
        }
        assert verify_name_attachment(
            name="Instance B",
            signature_hex=far_identity.sign(canonical_bytes(payload)).hex(),
            signer_pubkey_hex=far_identity.public_key_hex(),
            envelope_signature_hex=ENVELOPE_SIG,
        ) is None


# =============================================================================
# The label: fallback order
# =============================================================================


def _row(name: str | None = None, transport_config: Any = None) -> PeerUser:
    return PeerUser(
        id=5, circle_owner_id=1, remote_pubkey="ab" * 32,
        remote_display_name="evdb", remote_instance_name=name,
        transport_config=transport_config if transport_config is not None else {},
    )


class TestFallbackOrder:
    @pytest.mark.unit
    def test_the_verified_name_comes_first(self):
        peer = _row("Instance B", {"endpoints": ["https://far.example.org:8443"]})
        assert peer_instance_label(peer) == "Instance B"

    @pytest.mark.unit
    @pytest.mark.parametrize("config", [
        {"endpoints": ["https://far.example.org:8443"]},
        {"endpoints": [{"url": "https://far.example.org:8443/api"}]},
        {"accepted_endpoints": [{"url": "https://far.example.org"}]},
        {"endpoint_url": "https://far.example.org"},
    ])
    def test_then_the_host_of_the_stored_endpoint(self, config):
        assert peer_instance_label(_row(None, config)) == "far.example.org"

    @pytest.mark.unit
    @pytest.mark.parametrize("config", [{}, {"endpoints": []}, {"endpoints": ["nonsense"]}])
    def test_then_nothing_and_the_person_stands_alone(self, config):
        assert peer_instance_label(_row(None, config)) is None

    @pytest.mark.unit
    def test_an_empty_stored_name_is_no_name(self):
        assert stored_instance_name(_row("")) is None

    @pytest.mark.unit
    def test_a_mock_row_never_yields_a_label(self):
        """Many tests hand the code a MagicMock peer. A mock attribute must
        not come out as a label -- it would land in payloads as an object."""
        mock = MagicMock()
        assert stored_instance_name(mock) is None
        assert endpoint_host(mock) is None
        assert peer_instance_label(mock) is None


# =============================================================================
# Pairing handshake
# =============================================================================


class _LegacyPairingOffer(BaseModel):
    """`PairingOffer` exactly as it was BEFORE the attachment existed. Unknown
    fields are dropped while parsing (pydantic's default), which is what an
    instance one release behind does with a new offer."""
    version: int = 1
    initiator_pubkey: str = Field(..., min_length=64, max_length=64)
    initiator_user_id: int
    display_name: str = Field(..., max_length=255)
    offered_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    nonce: str = Field(..., min_length=16)
    issued_at: int
    expires_at: int
    signature: str = Field(..., min_length=128, max_length=128)


class _LegacyPairingResponse(BaseModel):
    """`PairingResponse` as it was before the attachment existed."""
    version: int = 1
    nonce: str = Field(..., min_length=16)
    responder_pubkey: str = Field(..., min_length=64, max_length=64)
    responder_user_id: int
    responder_display_name: str = Field(..., max_length=255)
    accepted_endpoints: list[dict[str, Any]] = Field(default_factory=list)
    my_tier_for_you: int = Field(..., ge=0, le=4)
    accepted_at: int
    signature: str = Field(..., min_length=128, max_length=128)


def _legacy_verifies(model: BaseModel, pubkey_hex: str) -> bool:
    """The verification the previous release ran: everything but `signature`."""
    return FederationIdentity.verify(
        bytes.fromhex(pubkey_hex),
        bytes.fromhex(model.signature),
        _canonical_bytes(model.model_dump(exclude={"signature"})),
    )


def _legacy_offer(identity: FederationIdentity, **extra) -> dict[str, Any]:
    """An offer as the previous release SENT it: no attachment keys at all."""
    now = int(time.time())
    unsigned = {
        "version": 1,
        "initiator_pubkey": identity.public_key_hex(),
        "initiator_user_id": 7,
        "display_name": "evdb",
        "offered_endpoints": [],
        "nonce": "0123456789abcdef" * 2,
        "issued_at": now,
        "expires_at": now + 600,
    }
    signature = identity.sign(_canonical_bytes(unsigned)).hex()
    return {**unsigned, "signature": signature, **extra}


@pytest.fixture
def responder_service(local_identity, monkeypatch):
    """A PairingService accepting offers on THIS instance, with the DB writes
    captured instead of run."""
    monkeypatch.setattr(ps, "_get_or_create_circle", AsyncMock(return_value=MagicMock()))
    svc = PairingService(db=MagicMock())
    svc._upsert_peer_user = AsyncMock(return_value=MagicMock(id=10))
    return svc


class TestPairingCarriesTheName:
    @pytest.mark.unit
    def test_an_offer_carries_the_name_outside_the_signed_bytes(self, local_identity, named):
        named("Instance A")
        offer = PairingService(db=MagicMock()).create_offer(MagicMock(id=1, username="evdb"))
        assert offer.instance_name == "Instance A"
        assert offer.display_name == "evdb", "the person's name is not replaced"
        # What an instance ONE RELEASE BEHIND does with this offer: parse it
        # with its own model (the two keys fall away) and verify everything
        # but `signature`.
        legacy = _LegacyPairingOffer.model_validate(offer.model_dump())
        assert _legacy_verifies(legacy, offer.initiator_pubkey)

    @pytest.mark.unit
    def test_an_instance_without_a_name_emits_the_OLD_offer(self, local_identity, nameless):
        """Byte for byte what went into the QR code before: the route
        serialises with `exclude_none`, and no legacy field can be `None`."""
        offer = PairingService(db=MagicMock()).create_offer(MagicMock(id=1, username="evdb"))
        assert set(offer.model_dump(exclude_none=True)) == set(_LegacyPairingOffer.model_fields)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_accepting_stores_the_verified_name(self, responder_service, far_identity):
        offer_dict = _legacy_offer(far_identity)
        offer_dict.update(_attachment(far_identity, "Instance B", offer_dict["signature"]))
        await responder_service.accept_offer(
            MagicMock(id=2, username="bob"), PairingOffer(**offer_dict), my_tier_for_you=2,
        )
        kwargs = responder_service._upsert_peer_user.await_args.kwargs
        assert kwargs["remote_instance_name"] == "Instance B"
        assert kwargs["remote_display_name"] == "evdb"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_OLD_offer_is_accepted_and_teaches_no_name(
        self, responder_service, far_identity,
    ):
        """New instance, old initiator: the offer has no attachment keys."""
        await responder_service.accept_offer(
            MagicMock(id=2, username="bob"),
            PairingOffer(**_legacy_offer(far_identity)),
            my_tier_for_you=2,
        )
        assert responder_service._upsert_peer_user.await_args.kwargs["remote_instance_name"] is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("forgery", ["other_key", "other_envelope", "garbage", "name_only"])
    async def test_a_name_that_does_not_verify_is_dropped_and_the_pairing_stands(
        self, responder_service, far_identity, forgery,
    ):
        """🛑 The offer itself is authentic; only the name is not. Refusing the
        pairing would add a failure mode, storing the name would let anyone who
        can alter a QR payload rename the peer."""
        offer_dict = _legacy_offer(far_identity)
        if forgery == "other_key":
            offer_dict.update(_attachment(_fresh_identity(), "Mallory", offer_dict["signature"]))
        elif forgery == "other_envelope":
            offer_dict.update(_attachment(far_identity, "Mallory", OTHER_ENVELOPE_SIG))
        elif forgery == "garbage":
            offer_dict.update(instance_name="Mallory", instance_name_signature="nothex")
        else:
            offer_dict.update(instance_name="Mallory")
        await responder_service.accept_offer(
            MagicMock(id=2, username="bob"), PairingOffer(**offer_dict), my_tier_for_you=2,
        )
        assert responder_service._upsert_peer_user.await_args.kwargs["remote_instance_name"] is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_forged_OFFER_never_reaches_the_name(self, responder_service, far_identity):
        """The order: envelope first. With a broken offer signature the name
        is not looked at, whatever its own signature says."""
        offer_dict = _legacy_offer(far_identity)
        offer_dict["display_name"] = "somebody else"   # breaks the offer signature
        offer_dict.update(_attachment(far_identity, "Instance B", offer_dict["signature"]))
        with pytest.raises(ps.PairingError):
            await responder_service.accept_offer(
                MagicMock(id=2, username="bob"), PairingOffer(**offer_dict), my_tier_for_you=2,
            )
        responder_service._upsert_peer_user.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_response_carries_OUR_name_and_an_old_initiator_verifies_it(
        self, responder_service, far_identity, named,
    ):
        named("Instance A")
        response = await responder_service.accept_offer(
            MagicMock(id=2, username="bob"),
            PairingOffer(**_legacy_offer(far_identity)),
            my_tier_for_you=2,
        )
        assert response.instance_name == "Instance A"
        legacy = _LegacyPairingResponse.model_validate(response.model_dump())
        assert _legacy_verifies(legacy, response.responder_pubkey)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_completing_stores_the_responders_verified_name(
        self, local_identity, far_identity, monkeypatch, nameless,
    ):
        """Initiator side: the name on the RESPONSE, bound to its signature."""
        monkeypatch.setattr(ps, "_get_or_create_circle", AsyncMock(return_value=MagicMock()))
        alice = MagicMock(id=1, username="alice")
        svc = PairingService(db=MagicMock())
        svc._upsert_peer_user = AsyncMock(return_value=MagicMock(id=20))

        def _response(nonce: str) -> dict[str, Any]:
            unsigned = {
                "version": 1, "nonce": nonce,
                "responder_pubkey": far_identity.public_key_hex(),
                "responder_user_id": 99, "responder_display_name": "evdb",
                "accepted_endpoints": [], "my_tier_for_you": 2,
                "accepted_at": int(time.time()),
            }
            return {**unsigned, "signature": far_identity.sign(_canonical_bytes(unsigned)).hex()}

        # A response with a valid attachment.
        resp = _response(svc.create_offer(alice).nonce)
        resp.update(_attachment(far_identity, "Instance B", resp["signature"]))
        await svc.complete_handshake(alice, PairingResponse(**resp), their_tier_for_me=3)
        assert svc._upsert_peer_user.await_args.kwargs["remote_instance_name"] == "Instance B"

        # An OLD responder: no attachment keys.
        resp = _response(svc.create_offer(alice).nonce)
        await svc.complete_handshake(alice, PairingResponse(**resp), their_tier_for_me=3)
        assert svc._upsert_peer_user.await_args.kwargs["remote_instance_name"] is None

        # A stranger's attachment on an authentic response.
        resp = _response(svc.create_offer(alice).nonce)
        resp.update(_attachment(_fresh_identity(), "Mallory", resp["signature"]))
        await svc.complete_handshake(alice, PairingResponse(**resp), their_tier_for_me=3)
        assert svc._upsert_peer_user.await_args.kwargs["remote_instance_name"] is None


# =============================================================================
# Query: the responder learns the asker's name
# =============================================================================


def _initiate(asker: FederationIdentity, **extra) -> QueryBrainInitiateRequest:
    import secrets

    pubkey = asker.public_key_hex()
    unsigned = {
        "version": 2, "asker_pubkey": pubkey, "query": "was liegt bei euch?",
        "nonce": secrets.token_hex(16), "timestamp": int(time.time()),
        "depth": 3, "path": [pubkey],
    }
    signature = asker.sign(_canonical_bytes(unsigned)).hex()
    return QueryBrainInitiateRequest(**unsigned, signature=signature, **extra)


def _with_name(asker: FederationIdentity, name: str, signer: FederationIdentity | None = None):
    req = _initiate(asker)
    return req.model_copy(update=_attachment(signer or asker, name, req.signature))


def _db_returning(peers: list) -> MagicMock:
    db = MagicMock()

    async def _execute(stmt):
        result = MagicMock()
        rows = peers if "peer_users" in str(stmt) else []
        result.scalars = lambda: MagicMock(all=lambda: rows)
        result.scalar_one_or_none = lambda: rows[0] if rows else None
        return result

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    return db


def _paired_row(asker: FederationIdentity, name: str | None = None) -> PeerUser:
    return PeerUser(
        id=77, circle_owner_id=1, remote_pubkey=asker.public_key_hex(),
        remote_display_name="evdb", remote_user_id=42, granted_tier=2,
        remote_instance_name=name, transport_config={}, revoked_at=None,
    )


@pytest.fixture
def remembered(monkeypatch):
    """Capture what the responder asks to have stored."""
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(responder_module, "remember_remote_instance_name", spy)
    return spy


class TestResponderLearnsTheName:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_paired_asker_teaches_its_name(self, local_identity, far_identity, remembered):
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        responder._run_query = AsyncMock()
        resp = await responder.handle_initiate(_with_name(far_identity, "Instance B"))
        remembered.assert_awaited_once_with(77, "Instance B", current=None)
        # ... and the audit snapshot of THIS access already carries it.
        pending = await get_pending_store().get(resp.request_id)
        assert pending.peer_instance_name == "Instance B"
        assert pending.peer_display_name == "evdb"

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_name_already_stored_writes_nothing(self, local_identity, far_identity, remembered):
        """The request path of every query: no UPDATE unless the name changed."""
        responder = FederationQueryResponder(
            db=_db_returning([_paired_row(far_identity, "Instance B")]))
        responder._run_query = AsyncMock()
        await responder.handle_initiate(_with_name(far_identity, "Instance B"))
        remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_UNPAIRED_key_is_refused_and_its_name_never_looked_at(
        self, local_identity, far_identity, remembered, monkeypatch,
    ):
        """🛑 A stranger can sign an envelope AND an attachment with its own
        key -- both verify. What makes a name believable is the pairing."""
        verify = MagicMock(side_effect=verify_name_attachment)
        monkeypatch.setattr(responder_module, "verify_name_attachment", verify)
        responder = FederationQueryResponder(db=_db_returning([]))
        with pytest.raises(FederationQueryError, match="Unknown or revoked"):
            await responder.handle_initiate(_with_name(far_identity, "Mallory"))
        verify.assert_not_called()
        remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_forged_ENVELOPE_never_reaches_the_name(
        self, local_identity, far_identity, remembered,
    ):
        req = _with_name(far_identity, "Instance B")
        tampered = req.model_copy(update={"query": "something else"})
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        with pytest.raises(FederationQueryError, match="Signature"):
            await responder.handle_initiate(tampered)
        remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("forgery", ["other_key", "other_envelope", "garbage"])
    async def test_a_bad_attachment_is_dropped_and_the_query_runs(
        self, local_identity, far_identity, remembered, forgery,
    ):
        req = _initiate(far_identity)
        if forgery == "other_key":
            req = _with_name(far_identity, "Mallory", signer=_fresh_identity())
        elif forgery == "other_envelope":
            req = req.model_copy(update=_attachment(far_identity, "Mallory", OTHER_ENVELOPE_SIG))
        else:
            req = req.model_copy(update={
                "instance_name": "Mallory", "instance_name_signature": "x" * 300})
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        responder._run_query = AsyncMock()
        resp = await responder.handle_initiate(req)
        assert resp.request_id
        remembered.assert_not_awaited()
        assert (await get_pending_store().get(resp.request_id)).peer_instance_name == ""

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_OLD_asker_is_served_as_before(self, local_identity, far_identity, remembered):
        """New responder, old asker: no attachment keys on the request."""
        responder = FederationQueryResponder(db=_db_returning([_paired_row(far_identity)]))
        responder._run_query = AsyncMock()
        body = _initiate(far_identity).model_dump()
        del body["instance_name"], body["instance_name_signature"]
        resp = await responder.handle_initiate(QueryBrainInitiateRequest.model_validate(body))
        assert resp.request_id
        remembered.assert_not_awaited()

    @pytest.mark.unit
    def test_the_name_is_outside_the_signed_bytes(self, far_identity):
        """Why an OLD responder accepts a NEW request: it recomputes the
        signed bytes from a fixed field list that does not know the name."""
        plain = _initiate(far_identity)
        named_req = plain.model_copy(update=_attachment(far_identity, "Instance B", plain.signature))
        assert initiate_canonical_payload(named_req) == initiate_canonical_payload(plain)
        assert "instance_name" not in initiate_canonical_payload(named_req)


# =============================================================================
# Query: the asker sends its name and learns the responder's
# =============================================================================


class _Client:
    """httpx stand-in that records what was posted."""

    def __init__(self, responses: dict[str, list[dict]]):
        self._responses = {k: list(v) for k, v in responses.items()}
        self.posted: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict | None = None) -> httpx.Response:
        self.posted.append((url, json or {}))
        for suffix, queue in self._responses.items():
            if url.endswith(suffix):
                return httpx.Response(200, json=queue.pop(0))
        raise AssertionError(f"no response registered for {url}")


def _complete(responder: FederationIdentity, **extra) -> dict:
    resp = QueryBrainRetrieveResponse(
        status=STATUS_COMPLETE, answer="eine Antwort", answered_at=int(time.time()),
        responder_pubkey=responder.public_key_hex(),
    )
    resp.responder_signature = responder.sign(
        _canonical_bytes(complete_canonical_payload(resp))).hex()
    return {**resp.model_dump(), **extra}


def _client_for(final: dict) -> _Client:
    return _Client({
        "query_brain/initiate": [QueryBrainInitiateResponse(
            request_id="req-1", accepted_at=int(time.time())).model_dump()],
        "query_brain/retrieve": [final],
    })


@pytest.fixture
def asked_peer(far_identity) -> PeerUser:
    return PeerUser(
        id=3, circle_owner_id=1, remote_pubkey=far_identity.public_key_hex(),
        remote_display_name="evdb", transport_config={"endpoints": ["http://far.test:8000"]},
    )


@pytest.fixture
def asker_remembered(monkeypatch):
    spy = AsyncMock(return_value=True)
    monkeypatch.setattr(asker_module, "remember_remote_instance_name", spy)
    return spy


async def _ask(client: _Client, peer: PeerUser) -> dict:
    items = [item async for item in FederationQueryAsker(client=client).query_peer(peer, "frage")]
    return items[-1]


#: The keys an initiate body has always had on the wire.
LEGACY_INITIATE_KEYS = {
    "version", "asker_pubkey", "query", "nonce", "timestamp", "depth", "path",
    "querier_ref", "signature",
}
#: The keys a retrieve answer has always had on the wire.
LEGACY_RETRIEVE_KEYS = {
    "version", "status", "progress", "answer", "provenance", "answered_at",
    "responder_pubkey", "responder_signature",
}


class TestAskerSendsAndLearns:
    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_the_request_carries_our_name_as_a_valid_attachment(
        self, local_identity, far_identity, asked_peer, asker_remembered, named,
    ):
        named("Instance A")
        client = _client_for(_complete(far_identity))
        await _ask(client, asked_peer)
        body = client.posted[0][1]
        assert set(body) == LEGACY_INITIATE_KEYS | {"instance_name", "instance_name_signature"}
        assert verify_name_attachment(
            name=body["instance_name"],
            signature_hex=body["instance_name_signature"],
            signer_pubkey_hex=local_identity.public_key_hex(),
            envelope_signature_hex=body["signature"],
        ) == "Instance A"
        # An OLD responder verifies the envelope from its fixed field list.
        legacy_view = QueryBrainInitiateRequest.model_validate(
            {k: v for k, v in body.items() if k in LEGACY_INITIATE_KEYS})
        assert FederationIdentity.verify(
            bytes.fromhex(body["asker_pubkey"]), bytes.fromhex(body["signature"]),
            _canonical_bytes(initiate_canonical_payload(legacy_view)),
        )

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_instance_without_a_name_sends_the_OLD_body(
        self, local_identity, far_identity, asked_peer, asker_remembered, nameless,
    ):
        """Not two `null` keys: exactly the key set of before, `querier_ref:
        null` included."""
        client = _client_for(_complete(far_identity))
        await _ask(client, asked_peer)
        body = client.posted[0][1]
        assert set(body) == LEGACY_INITIATE_KEYS
        assert body["querier_ref"] is None

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_verified_answer_teaches_the_responders_name(
        self, local_identity, far_identity, asked_peer, asker_remembered,
    ):
        final = _complete(far_identity)
        final.update(_attachment(far_identity, "Instance B", final["responder_signature"]))
        result = await _ask(_client_for(final), asked_peer)
        assert result["success"] is True
        asker_remembered.assert_awaited_once_with(3, "Instance B", current=None)

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_answer_from_ANOTHER_key_teaches_nothing(
        self, local_identity, far_identity, asked_peer, asker_remembered,
    ):
        """🛑 Pair-anchor binding first: an answer signed by a key we did not
        pair with fails, and its (self-consistent) name is never stored."""
        impostor = _fresh_identity()
        final = _complete(impostor)
        final.update(_attachment(impostor, "Mallory", final["responder_signature"]))
        result = await _ask(_client_for(final), asked_peer)
        assert result["success"] is False
        asker_remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    @pytest.mark.parametrize("forgery", ["other_key", "other_envelope"])
    async def test_a_bad_attachment_on_a_good_answer_is_dropped(
        self, local_identity, far_identity, asked_peer, asker_remembered, forgery,
    ):
        final = _complete(far_identity)
        if forgery == "other_key":
            final.update(_attachment(_fresh_identity(), "Mallory", final["responder_signature"]))
        else:
            final.update(_attachment(far_identity, "Mallory", OTHER_ENVELOPE_SIG))
        result = await _ask(_client_for(final), asked_peer)
        assert result["success"] is True, "the answer itself is authentic"
        asker_remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_an_OLD_responder_is_understood(
        self, local_identity, far_identity, asked_peer, asker_remembered,
    ):
        final = {k: v for k, v in _complete(far_identity).items() if k in LEGACY_RETRIEVE_KEYS}
        result = await _ask(_client_for(final), asked_peer)
        assert result["success"] is True
        asker_remembered.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.unit
    async def test_a_known_name_is_not_written_again(
        self, local_identity, far_identity, asked_peer, asker_remembered,
    ):
        asked_peer.remote_instance_name = "Instance B"
        final = _complete(far_identity)
        final.update(_attachment(far_identity, "Instance B", final["responder_signature"]))
        await _ask(_client_for(final), asked_peer)
        asker_remembered.assert_not_awaited()


class TestTheAnswerOnTheWire:
    """What the responder puts on the wire for a retrieve."""

    def _signed(self, responder: FederationQueryResponder) -> QueryBrainRetrieveResponse:
        resp = QueryBrainRetrieveResponse(
            status=STATUS_COMPLETE, answer="eine Antwort", answered_at=int(time.time()),
            responder_pubkey=responder.identity.public_key_hex(),
        )
        resp.responder_signature = responder._sign_response(resp)
        responder._attach_instance_name(resp)
        return resp

    @pytest.mark.unit
    def test_a_named_instance_attaches_its_name_outside_the_signed_bytes(
        self, local_identity, named,
    ):
        named("Instance A")
        resp = self._signed(FederationQueryResponder(db=MagicMock()))
        body = wire_body(resp)
        assert set(body) == LEGACY_RETRIEVE_KEYS | {"instance_name", "instance_name_signature"}
        assert "instance_name" not in complete_canonical_payload(resp)
        # An OLD asker: parses the keys it knows and verifies the answer.
        legacy = QueryBrainRetrieveResponse.model_validate(
            {k: v for k, v in body.items() if k in LEGACY_RETRIEVE_KEYS})
        assert FederationIdentity.verify(
            bytes.fromhex(body["responder_pubkey"]),
            bytes.fromhex(body["responder_signature"]),
            _canonical_bytes(complete_canonical_payload(legacy)),
        )

    @pytest.mark.unit
    def test_an_instance_without_a_name_answers_with_the_OLD_keys(self, local_identity, nameless):
        resp = self._signed(FederationQueryResponder(db=MagicMock()))
        body = wire_body(resp)
        assert set(body) == LEGACY_RETRIEVE_KEYS
        # `null`s of legacy keys are still sent -- only the attachment is omitted.
        assert body["progress"] is None


# =============================================================================
# The single writer, on real Postgres
# =============================================================================


@pytest.fixture
async def pairing_rows(db_session, own_session_to_test_db):
    role = Role(name="role-fed-name", permissions=["chat.own"], is_system=False)
    db_session.add(role)
    await db_session.flush()
    owner = User(username="fed-name-owner", password_hash="x", role_id=role.id)
    db_session.add(owner)
    await db_session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    active = PeerUser(
        circle_owner_id=owner.id, remote_pubkey="ab" * 32, remote_display_name="evdb",
        granted_tier=2, transport_config={}, paired_at=now,
    )
    revoked = PeerUser(
        circle_owner_id=owner.id, remote_pubkey="cd" * 32, remote_display_name="evdb",
        granted_tier=2, transport_config={}, paired_at=now, revoked_at=now,
    )
    db_session.add_all([active, revoked])
    await db_session.commit()
    return {"active": active, "revoked": revoked}


@pytest.mark.database
class TestRememberOnPostgres:
    async def _name(self, db, peer) -> str | None:
        await db.refresh(peer)
        return peer.remote_instance_name

    async def test_stores_then_reports_no_change_then_stores_the_rename(
        self, db_session, pairing_rows, monkeypatch,
    ):
        # The rename floor is a subject of its own (below); here it is off.
        monkeypatch.setattr(settings, "federation_instance_rename_min_interval_seconds", 0)
        active = pairing_rows["active"]
        assert await remember_remote_instance_name(active.id, "Instance B") is True
        assert await self._name(db_session, active) == "Instance B"
        assert await remember_remote_instance_name(active.id, "Instance B") is False
        assert await remember_remote_instance_name(active.id, "x-idra") is True
        assert await self._name(db_session, active) == "x-idra"

    async def test_a_second_rename_inside_ten_minutes_is_ignored(
        self, db_session, pairing_rows, monkeypatch,
    ):
        """🛑 The name shows on this instance's pages and wall and each change
        resyncs the tool registry. A peer may change it once per interval."""
        import services.federation_instance_name as fin
        import services.peer_mcp_registry as registry

        assert settings.federation_instance_rename_min_interval_seconds == 600
        resync = AsyncMock(return_value=True)
        monkeypatch.setattr(registry, "resync_bound_manager", resync)
        active = pairing_rows["active"]

        assert await remember_remote_instance_name(active.id, "Instance B", current=None) is True
        for flicker in ("Instance C", "Instance D", "Instance B2"):
            assert await remember_remote_instance_name(
                active.id, flicker, current="Instance B") is False
        assert await self._name(db_session, active) == "Instance B"
        assert resync.await_count == 1

        # The interval has passed (a fresh record stands for that): the next
        # name the far side sends is taken.
        fin.set_rename_claims_for_tests(fin.MemoryRenameClaims())
        assert await remember_remote_instance_name(
            active.id, "Instance C", current="Instance B") is True
        assert await self._name(db_session, active) == "Instance C"
        assert resync.await_count == 2

    async def test_the_floor_is_per_pairing(self):
        import services.federation_instance_name as fin

        claims = fin.MemoryRenameClaims()
        assert await claims.claim(1, 600) is True
        assert await claims.claim(1, 600) is False
        assert await claims.claim(2, 600) is True

    async def test_an_unchanged_name_costs_nothing_at_all(self, pairing_rows, monkeypatch):
        """The name rides on EVERY query and ping. Known to be unchanged it
        opens no session, takes no claim and resyncs nothing."""
        import services.database as database
        import services.federation_instance_name as fin
        import services.peer_mcp_registry as registry

        def _no_session():
            raise AssertionError("an unchanged name opened a database session")

        claims = MagicMock()
        claims.claim = AsyncMock(return_value=True)
        fin.set_rename_claims_for_tests(claims)
        resync = AsyncMock(return_value=True)
        monkeypatch.setattr(registry, "resync_bound_manager", resync)
        monkeypatch.setattr(database, "AsyncSessionLocal", _no_session)

        assert await remember_remote_instance_name(
            pairing_rows["active"].id, "Instance B", current="Instance B") is False
        claims.claim.assert_not_awaited()
        resync.assert_not_awaited()

    async def test_the_log_line_names_the_pairing_and_lengths_never_the_name(
        self, db_session, pairing_rows,
    ):
        from loguru import logger

        lines: list[str] = []
        sink = logger.add(lambda message: lines.append(str(message)), level="DEBUG")
        try:
            active = pairing_rows["active"]
            assert await remember_remote_instance_name(
                active.id, "Sehr geheimer Name", current=None) is True
        finally:
            logger.remove(sink)
        changed = [line for line in lines if "instance name changed" in line]
        assert len(changed) == 1
        assert f"Peer {active.id}:" in changed[0]
        assert "(0 -> 18 characters)" in changed[0]
        assert not any("geheimer" in line for line in lines)

    async def test_a_revoked_pairing_is_never_touched(self, db_session, pairing_rows):
        revoked = pairing_rows["revoked"]
        assert await remember_remote_instance_name(revoked.id, "Instance B") is False
        assert await self._name(db_session, revoked) is None

    @pytest.mark.parametrize("name", [None, ""])
    async def test_no_name_writes_nothing(self, db_session, pairing_rows, name):
        active = pairing_rows["active"]
        assert await remember_remote_instance_name(active.id, name) is False
        assert await self._name(db_session, active) is None

    async def test_a_change_refreshes_the_tool_description(
        self, db_session, pairing_rows, monkeypatch,
    ):
        """The description the model reads names the instance, so it has to
        follow -- but only when something actually changed."""
        import services.peer_mcp_registry as registry

        resync = AsyncMock(return_value=True)
        monkeypatch.setattr(registry, "resync_bound_manager", resync)
        active = pairing_rows["active"]
        await remember_remote_instance_name(active.id, "Instance B")
        assert resync.await_count == 1
        await remember_remote_instance_name(active.id, "Instance B")
        assert resync.await_count == 1

    async def test_pairing_again_without_a_name_keeps_the_stored_one(
        self, db_session, pairing_rows, local_identity,
    ):
        """`_upsert_peer_user`: an old far side sends no name on a second
        pairing; what the same key vouched for earlier stays."""
        active = pairing_rows["active"]
        svc = PairingService(db=db_session)
        common = dict(
            owner_user_id=active.circle_owner_id, remote_pubkey=active.remote_pubkey,
            remote_display_name="evdb", remote_user_id=None, granted_tier=2,
            transport_config={},
        )
        await svc._upsert_peer_user(**common, remote_instance_name="Instance B")
        await db_session.commit()
        assert await self._name(db_session, active) == "Instance B"
        await svc._upsert_peer_user(**common)
        await db_session.commit()
        assert await self._name(db_session, active) == "Instance B"
        await svc._upsert_peer_user(**common, remote_instance_name="x-idra")
        await db_session.commit()
        assert await self._name(db_session, active) == "x-idra"
