"""F-ID-2 -- the consent-signed person handshake.

A **person link** asserts "user U on instance A and user X on instance B are
the same human". It has consequences: once it exists, a federated request no
longer runs as the WHOLE foreign instance with the tier granted at pairing
time, but as THAT PERSON with their own, full reach
(`federation_query_responder`, mapped path: `peer=None`).

Until now it could only be asserted: an administrator typed in two account
numbers and a secret by hand (`api/routes/federation_user_links`). The design
calls that what it is -- *"it trusts an admin, not a proof of same-person, and
is not user-establishable"*. This module replaces the assertion with a PROOF:
the handshake runs while you are signed in as U on A and as X on B at the same
time, and each side signs its own half with the instance key. Neither instance
can forge the other.

    A (U)                                   B (X)
    -----                                   -----
    create_offer   --  LinkOffer       -->  accept_offer
    (nothing in the DB)                     (writes ITS row, signed)
    complete       <-- LinkConfirmation --
    (writes ITS row)

Step 1 deliberately writes NOTHING. If the person abandons the flow after it,
no half link is left behind.

Limits of the handshake, stated explicitly
------------------------------------------
It proves control over both accounts. It does NOT prove the far side is honest:
the load-bearing assumption remains *"M2 trusts that M1 correctly authenticated
the human behind querier_ref"* -- tenable only between instances belonging to
the same person. That is why the link is unilaterally revocable at any time,
and the access stays read-only.

On replay -- the safeguard is the DATABASE, not the nonce
---------------------------------------------------------
`record_nonce` covers ONLY `NONCE_WINDOW_SECONDS` (60 s), but an offer lives
``LINK_OFFER_TTL_SECONDS`` (600 s). Reading this as "nonce, therefore replay
handled" is wrong for 9 of the window's 10 minutes. What actually carries:

1. ``UNIQUE(peer_id, querier_ref)`` on `federation_user_links` -- replaying the
   same offer hits the index and becomes a 409.
2. ``expires_at`` caps the window hard at 600 s.
3. Both steps require a SIGNED-IN session on their respective side; a third
   party with intercepted JSON gets nowhere without one.

`record_nonce` stays anyway: it catches the common case (submitted twice,
button pressed twice) cheaply and early, before the database does. It is a
shortcut, not a wall.

Design: `docs/design/federation-identity-mapping.md` §9 (option A from §4.2).
"""
from __future__ import annotations

import secrets
import time

from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationUserLink, PeerUser, User
from services.federation_identity import canonical_bytes, get_federation_identity
from services.federation_identity_link import create_link, mint_querier_ref
from services.federation_pending_store import get_pending_store

#: Lifetime of an offer. The same 600 s as the instance pairing
#: (`pairing_service.OFFER_TTL_SECONDS`) -- long enough to accept in a second
#: browser window, short enough as a replay window.
LINK_OFFER_TTL_SECONDS = 600

#: Permitted clock skew into the future, as in the pairing it is modelled on.
CLOCK_SKEW_TOLERANCE_SECONDS = 60


class LinkHandshakeError(Exception):
    """Every failure of the handshake -- ONE type, like `PairingError`.

    Deliberate: the route layer turns this into one single, always identical
    400. Different messages per cause would be an oracle from which one could
    read off WHICH check fired.
    """


# =============================================================================
# Wire models -- in this module, like `PairingOffer`/`PairingResponse`
# =============================================================================


class LinkOffer(BaseModel):
    """The half signed by A. Every field except `signature` is covered."""

    version: int = 1
    #: A's instance key -- and also what B knows us by in `peer_users`.
    initiator_pubkey: str = Field(..., min_length=64, max_length=64)
    #: The offer NAMES its recipient. Without this field an offer meant for B
    #: could be submitted to C, and C would write a row for a person nobody
    #: ever consented to.
    responder_pubkey: str = Field(..., min_length=64, max_length=64)
    #: Who declares the link on A. At completion this is checked against the
    #: signed-in person -- an offer is not transferable.
    stated_by: int
    #: The opaque band between the two rows (256 bits).
    querier_ref: str = Field(..., min_length=64, max_length=64)
    nonce: str = Field(..., min_length=16)
    issued_at: int
    expires_at: int
    signature: str = Field(..., min_length=128, max_length=128)


class LinkConfirmation(BaseModel):
    """The half signed by B: "`querier_ref` is me over here"."""

    version: int = 1
    nonce: str = Field(..., min_length=16)
    querier_ref: str = Field(..., min_length=64, max_length=64)
    responder_pubkey: str = Field(..., min_length=64, max_length=64)
    responder_user_id: int
    accepted_at: int
    signature: str = Field(..., min_length=128, max_length=128)


# =============================================================================
# Dienst
# =============================================================================


class LinkHandshakeService:
    """The three steps. Writes with `flush`, does NOT commit.

    The write convention follows `federation_identity_link` (flush, route
    committet), NICHT `pairing_service` (committet selbst). Dadurch rollt ein
    failure AFTER `create_link` -- when signing the confirmation, say -- takes
    the row back with it. Otherwise exactly the half link step 1
    vermeidet.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.identity = get_federation_identity()

    # -- Step 1 ---------------------------------------------------------------

    def create_offer(self, current_user: User, peer: PeerUser) -> LinkOffer:
        """A declares: "I, U, want to link myself at this peer."

        No write. The offer carries itself: at completion A verifies its OWN
        signature over it instead of remembering a nonce in memory.
        """
        now = int(time.time())
        unsigned = {
            "version": 1,
            "initiator_pubkey": self.identity.public_key_hex(),
            "responder_pubkey": peer.remote_pubkey,
            "stated_by": current_user.id,
            "querier_ref": mint_querier_ref(),
            "nonce": secrets.token_hex(16),
            "issued_at": now,
            "expires_at": now + LINK_OFFER_TTL_SECONDS,
        }
        signature = self.identity.sign(canonical_bytes(unsigned)).hex()
        return LinkOffer(**unsigned, signature=signature)

    # -- Step 2 ---------------------------------------------------------------

    async def accept_offer(
        self, current_user: User, offer: LinkOffer,
    ) -> LinkConfirmation:
        """B consents and writes ITS row.

        Order as in the pairing precedent: the non-mutating checks first, then
        the nonce, then the write. A forged signature must not burn a nonce.
        """
        self._verify_offer_shape(offer)

        # The offer has to mean US. Without this check an offer meant for a
        # third instance could be submitted here.
        if offer.responder_pubkey != self.identity.public_key_hex():
            raise LinkHandshakeError("Offer names a different responder")

        peer = await self._peer_by_pubkey(current_user.id, offer.initiator_pubkey)
        self._verify_signature(
            offer, pubkey_hex=offer.initiator_pubkey, was="Offer",
        )
        await self._burn_nonce(offer.nonce)

        link = await create_link(
            self.db,
            peer_id=peer.id,
            local_user_id=current_user.id,
            querier_ref=offer.querier_ref,
            created_by=current_user.id,
        )
        logger.info(
            f"🔗 Personen-Handschlag angenommen: Verknuepfung {link.id} "
            f"fuer Peer {peer.id}"
        )

        unsigned = {
            "version": 1,
            "nonce": offer.nonce,
            "querier_ref": offer.querier_ref,
            "responder_pubkey": self.identity.public_key_hex(),
            "responder_user_id": current_user.id,
            "accepted_at": int(time.time()),
        }
        signature = self.identity.sign(canonical_bytes(unsigned)).hex()
        return LinkConfirmation(**unsigned, signature=signature)

    # -- Step 3 ---------------------------------------------------------------

    async def complete(
        self, current_user: User, offer: LinkOffer, confirmation: LinkConfirmation,
    ) -> FederationUserLink:
        """A verifies B's consent and writes ITS row. The link is live after this.

        The offer comes back from the browser, so it is untrusted -- but it
        carries OUR own signature. We verify that first; everything inside is
        then trustworthy again without the server having remembered
        anything.
        """
        self._verify_offer_shape(offer)
        self._verify_signature(
            offer, pubkey_hex=self.identity.public_key_hex(), was="Offer",
        )

        # An offer is NOT transferable: it belongs to the person who declared
        # it. Without this line any other signed-in person could complete an
        # intercepted offer -- in THEIR name.
        if offer.stated_by != current_user.id:
            raise LinkHandshakeError("Offer was stated by a different user")

        if confirmation.version != 1:
            raise LinkHandshakeError("Unsupported confirmation version")
        if confirmation.nonce != offer.nonce:
            raise LinkHandshakeError("Confirmation does not answer this offer")
        if confirmation.querier_ref != offer.querier_ref:
            raise LinkHandshakeError("Confirmation carries a different querier_ref")
        if confirmation.responder_pubkey != offer.responder_pubkey:
            raise LinkHandshakeError("Confirmation came from a different instance")

        peer = await self._peer_by_pubkey(
            current_user.id, confirmation.responder_pubkey,
        )
        self._verify_signature(
            confirmation,
            pubkey_hex=confirmation.responder_pubkey,
            was="Confirmation",
        )
        await self._burn_nonce(f"linkdone:{offer.nonce}")

        link = await create_link(
            self.db,
            peer_id=peer.id,
            local_user_id=current_user.id,
            querier_ref=offer.querier_ref,
            created_by=current_user.id,
        )
        logger.info(
            f"🔗 Personen-Handschlag abgeschlossen: Verknuepfung {link.id} "
            f"fuer Peer {peer.id}"
        )
        return link

    # -- Innereien ------------------------------------------------------------

    def _verify_offer_shape(self, offer: LinkOffer) -> None:
        if offer.version != 1:
            raise LinkHandshakeError("Unsupported offer version")
        now = int(time.time())
        if offer.issued_at > now + CLOCK_SKEW_TOLERANCE_SECONDS:
            raise LinkHandshakeError("Offer issued in the future — clock skew")
        if offer.expires_at < now:
            raise LinkHandshakeError("Offer expired")

    def _verify_signature(
        self, payload: LinkOffer | LinkConfirmation, *, pubkey_hex: str, was: str,
    ) -> None:
        unsigned = payload.model_dump(exclude={"signature"})
        try:
            pubkey = bytes.fromhex(pubkey_hex)
            signature = bytes.fromhex(payload.signature)
        except ValueError as e:
            raise LinkHandshakeError("Malformed pubkey or signature hex") from e
        if not self.identity.verify(pubkey, signature, canonical_bytes(unsigned)):
            raise LinkHandshakeError(f"{was} signature failed verification")

    async def _peer_by_pubkey(self, owner_user_id: int, pubkey: str) -> PeerUser:
        """The peer row, or a refusal.

        Without a completed instance pairing there is nothing to link: the
        handshake presupposes it, it does not replace it. A revoked pairing
        does not count (`revoked_at IS NULL`).
        """
        peer = (await self.db.execute(
            select(PeerUser).where(
                PeerUser.circle_owner_id == owner_user_id,
                PeerUser.remote_pubkey == pubkey,
                PeerUser.revoked_at.is_(None),
            )
        )).scalar_one_or_none()
        if peer is None:
            raise LinkHandshakeError("No active pairing with that instance")
        return peer

    @staticmethod
    async def _burn_nonce(nonce: str) -> None:
        """Cheap early rejection of a double submit.

        NOT a wall against replay -- the store keeps a nonce for only
        `NONCE_WINDOW_SECONDS` (60 s), while the offer lives ten times as long.
        What actually carries is in the module docstring: the uniqueness index
        on `(peer_id, querier_ref)`, the expiry, and the requirement of a
        signed-in session on both sides.
        """
        if not await get_pending_store().record_nonce(nonce, time.time()):
            raise LinkHandshakeError("Nonce already used")
