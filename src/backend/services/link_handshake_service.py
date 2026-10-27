"""F-ID-2 — der einwilligungs-signierte Personen-Handschlag.

Eine **Personenverknuepfung** behauptet „Nutzer U auf Instanz A und Nutzer X auf
Instanz B sind derselbe Mensch". Sie ist folgenreich: liegt sie vor, laeuft eine
foederierte Anfrage nicht mehr als die GANZE fremde Instanz mit der bei der
Paarung gewaehrten Stufe, sondern als DIESE PERSON mit deren eigener, voller
Reichweite (`federation_query_responder`, `enforce_circles=False`).

Bis heute liess sie sich nur behaupten: ein Administrator trug zwei
Kontonummern und ein Geheimnis von Hand ein (`api/routes/federation_user_links`).
Der Entwurf nennt das beim Namen — *„it trusts an admin, not a proof of
same-person, and is not user-establishable"*. Dieses Modul ersetzt die
Behauptung durch einen BEWEIS: der Handschlag laeuft, waehrend man gleichzeitig
als U auf A und als X auf B angemeldet ist, und jede Seite unterschreibt ihre
eigene Haelfte mit dem Instanzschluessel. Keine Instanz kann die andere faelschen.

    A (U)                                   B (X)
    ─────                                   ─────
    create_offer   ──  LinkOffer       ──▶  accept_offer
    (nichts in der DB)                      (schreibt SEINE Zeile, signiert)
    complete       ◀── LinkConfirmation ──
    (schreibt SEINE Zeile)

🛑 Schritt 1 schreibt bewusst NICHTS. Bricht die Person danach ab, bleibt kein
halber Link stehen.

Grenzen des Handschlags, ausdruecklich benannt
----------------------------------------------
Er beweist die Kontrolle ueber beide Konten. Er beweist NICHT, dass die
Gegenstelle ehrlich ist: die tragende Annahme bleibt *„M2 trusts that M1
correctly authenticated the human behind querier_ref"* — tragbar nur zwischen
Instanzen, die derselben Person gehoeren. Darum ist die Verknuepfung jederzeit
einseitig widerrufbar, und der Zugriff bleibt lesend.

Zur Wiedereinspielung — die Sicherung ist die DATENBANK, nicht der Nonce
----------------------------------------------------------------------
`record_nonce` deckt NUR `NONCE_WINDOW_SECONDS` (60 s) ab, ein Angebot lebt aber
``LINK_OFFER_TTL_SECONDS`` (600 s). Wer hier „Nonce also Wiedereinspielung
abgewehrt" liest, irrt fuer 9 von 10 Minuten des Fensters. Was wirklich traegt:

1. ``UNIQUE(peer_id, querier_ref)`` auf `federation_user_links` — ein zweites
   Einspielen desselben Angebots trifft den Index und wird zu einer 409.
2. ``expires_at`` begrenzt das Fenster hart auf 600 s.
3. Beide Schritte verlangen eine ANGEMELDETE Sitzung auf der jeweiligen Seite;
   ein Dritter mit abgefangenem JSON kommt ohne sie nicht durch.

`record_nonce` bleibt trotzdem drin: es faengt den haeufigen Fall (doppelt
abgeschickt, Knopf zweimal gedrueckt) billig und frueh ab, bevor die Datenbank
ihn abfaengt. Es ist eine Abkuerzung, keine Mauer.

Entwurf: `docs/design/federation-identity-mapping.md` §9 (Option A aus §4.2).
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

#: Lebensdauer eines Angebots. Dieselben 600 s wie bei der Instanz-Paarung
#: (`pairing_service.OFFER_TTL_SECONDS`) — lang genug, um in einem zweiten
#: Browserfenster anzunehmen, kurz genug als Fenster fuer eine Wiedereinspielung.
LINK_OFFER_TTL_SECONDS = 600

#: Erlaubte Uhren-Abweichung nach vorn, wie beim Paarungs-Vorbild.
CLOCK_SKEW_TOLERANCE_SECONDS = 60


class LinkHandshakeError(Exception):
    """Jeder Fehlschlag des Handschlags — EIN Typ, wie `PairingError`.

    🛑 Absicht: die Routenschicht macht daraus eine einzige, immer gleiche 400.
    Unterschiedliche Meldungen je Ursache waeren ein Orakel, an dem sich
    ablesen liesse, WELCHE Pruefung gegriffen hat.
    """


# =============================================================================
# Wire-Modelle — im Modul, wie `PairingOffer`/`PairingResponse`
# =============================================================================


class LinkOffer(BaseModel):
    """Die von A signierte Haelfte. Jedes Feld ausser `signature` ist gedeckt."""

    version: int = 1
    #: A's Instanzschluessel — und zugleich das, womit B uns in `peer_users` kennt.
    initiator_pubkey: str = Field(..., min_length=64, max_length=64)
    #: 🛑 Das Angebot NENNT seinen Empfaenger. Ohne dieses Feld liesse sich ein
    #: fuer B bestimmtes Angebot bei C einreichen, und C schriebe eine Zeile fuer
    #: eine Person, der nie jemand zugestimmt hat.
    responder_pubkey: str = Field(..., min_length=64, max_length=64)
    #: Wer auf A die Verknuepfung erklaert. Beim Abschluss wird gegen die
    #: angemeldete Person geprueft — ein Angebot ist nicht uebertragbar.
    stated_by: int
    #: Das opake Band zwischen beiden Zeilen (256 Bit).
    querier_ref: str = Field(..., min_length=64, max_length=64)
    nonce: str = Field(..., min_length=16)
    issued_at: int
    expires_at: int
    signature: str = Field(..., min_length=128, max_length=128)


class LinkConfirmation(BaseModel):
    """Die von B signierte Haelfte: „`querier_ref` bin ich hier."""

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
    """Die drei Schritte. Schreibt mit `flush`, committet NICHT.

    🛑 Die Schreibkonvention folgt `federation_identity_link` (flush, Route
    committet), NICHT `pairing_service` (committet selbst). Dadurch rollt ein
    Fehlschlag NACH dem `create_link` — etwa beim Signieren der Bestaetigung —
    die Zeile mit zurueck. Sonst entstuende genau der halbe Link, den Schritt 1
    vermeidet.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.identity = get_federation_identity()

    # -- Schritt 1 ------------------------------------------------------------

    def create_offer(self, current_user: User, peer: PeerUser) -> LinkOffer:
        """A erklaert: „Ich, U, moechte mich bei diesem Peer verknuepfen."

        Kein Schreibvorgang. Das Angebel traegt sich selbst: beim Abschluss
        prueft A die EIGENE Signatur darueber, statt sich an einen Nonce im
        Arbeitsspeicher zu erinnern.
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

    # -- Schritt 2 ------------------------------------------------------------

    async def accept_offer(
        self, current_user: User, offer: LinkOffer,
    ) -> LinkConfirmation:
        """B stimmt zu und schreibt SEINE Zeile.

        Reihenfolge wie beim Paarungs-Vorbild: erst die nicht-mutierenden
        Pruefungen, dann der Nonce, dann der Schreibvorgang. Eine gefaelschte
        Signatur darf keinen Nonce verbrennen.
        """
        self._verify_offer_shape(offer)

        # 🛑 Das Angebot muss UNS meinen. Ohne diese Pruefung liesse sich ein
        # fuer eine dritte Instanz bestimmtes Angebot hier einreichen.
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

    # -- Schritt 3 ------------------------------------------------------------

    async def complete(
        self, current_user: User, offer: LinkOffer, confirmation: LinkConfirmation,
    ) -> FederationUserLink:
        """A prueft B's Zustimmung und schreibt SEINE Zeile. Danach lebt der Link.

        🛑 Das Angebot kommt vom Browser zurueck, ist also unvertraut — aber es
        traegt UNSERE eigene Signatur. Die pruefen wir zuerst; damit ist alles
        darin wieder vertrauenswuerdig, ohne dass der Server sich etwas gemerkt
        haette.
        """
        self._verify_offer_shape(offer)
        self._verify_signature(
            offer, pubkey_hex=self.identity.public_key_hex(), was="Offer",
        )

        # 🛑 Ein Angebot ist NICHT uebertragbar: es gilt der Person, die es
        # erklaert hat. Ohne diese Zeile koennte jede andere angemeldete Person
        # ein abgefangenes Angebot abschliessen — und zwar auf IHREN Namen.
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
        """Die Peer-Zeile, oder ein Fehlschlag.

        🛑 Ohne abgeschlossene Instanz-Paarung gibt es nichts zu verknuepfen:
        der Handschlag setzt sie voraus, er ersetzt sie nicht. Eine widerrufene
        Paarung zaehlt nicht (`revoked_at IS NULL`).
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
        """Billiger Frueh-Abweiser gegen das doppelte Abschicken.

        🛑 KEINE Mauer gegen Wiedereinspielung — der Speicher haelt einen Nonce
        nur `NONCE_WINDOW_SECONDS` (60 s), das Angebot lebt zehnmal so lang.
        Was wirklich traegt, steht im Modul-Docstring: der Eindeutigkeitsindex
        auf `(peer_id, querier_ref)`, das Ablaufdatum und die Pflicht zur
        angemeldeten Sitzung auf beiden Seiten.
        """
        if not await get_pending_store().record_nonce(nonce, time.time()):
            raise LinkHandshakeError("Nonce already used")
