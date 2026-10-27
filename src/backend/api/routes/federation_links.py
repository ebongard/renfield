"""F-ID-2 — die Routen des Personen-Handschlags.

Der SELBSTBEDIENTE Weg zu einer Personenverknuepfung, als Ersatz fuer die
Admin-Behauptung in `federation_user_links.py`. Der Unterschied ist nicht
Bequemlichkeit, sondern die Art des Nachweises: dort traegt ein Administrator
zwei Kontonummern ein, hier beweist die Person die Kontrolle ueber BEIDE Konten,
indem sie den Handschlag angemeldet auf beiden Instanzen durchlaeuft.

🛑 Deshalb steht hier KEIN `require_permission(Permission.ADMIN)`. Das ist der
ganze Punkt des Stuecks — wer es nachtraeglich hinzufuegt, macht den Handschlag
wieder zur Admin-Behauptung mit mehr Schritten.

Dienst: `services/link_handshake_service.py` (dort stehen die Grenzen des
Verfahrens und warum der Nonce KEINE Mauer gegen Wiedereinspielung ist).
Entwurf: `docs/design/federation-identity-mapping.md` §9.
"""
from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import FederationUserLink, PeerUser, User
from services.auth_service import get_user_or_default
from services.database import get_db
from services.federation_identity_link import delete_link
from services.link_handshake_service import (
    LinkConfirmation,
    LinkHandshakeError,
    LinkHandshakeService,
    LinkOffer,
)

router = APIRouter()


class CreateLinkOfferRequest(BaseModel):
    #: Die Peer-Zeile, mit der verknuepft werden soll — eine abgeschlossene
    #: Instanz-Paarung ist Vorbedingung, nicht Teil des Handschlags.
    peer_id: int


class AcceptLinkOfferRequest(BaseModel):
    offer: LinkOffer


class CompleteLinkRequest(BaseModel):
    #: 🛑 Das Angebot kommt zurueck, weil der Server sich NICHTS gemerkt hat.
    #: Es traegt unsere eigene Signatur, die beim Abschluss geprueft wird —
    #: kryptografische Bindung statt Sitzungszustand im Arbeitsspeicher.
    offer: LinkOffer
    confirmation: LinkConfirmation


class LinkResponse(BaseModel):
    id: int
    peer_id: int
    peer_display_name: str
    created_at: str


def _reject(e: LinkHandshakeError, user_id: int) -> HTTPException:
    """Eine einzige, immer gleiche 400.

    🛑 Kein Orakel: unterschiedliche Meldungen je Ursache verrieten, WELCHE
    Pruefung gegriffen hat — abgelaufen, falscher Empfaenger, Signatur falsch.
    Der Grund steht im Protokoll, nicht in der Antwort. Gleiches Muster wie
    `federation_pairing` („Pairing handshake failed").
    """
    logger.warning(f"Link handshake failed for user {user_id}: {e}")
    return HTTPException(status_code=400, detail="Link handshake failed")


@router.post("/links/offer", response_model=LinkOffer)
async def create_link_offer(
    body: CreateLinkOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Schritt 1 (auf A, als U) — signiertes Angebot. Schreibt NICHTS."""
    peer = (await db.execute(
        select(PeerUser).where(
            PeerUser.id == body.peer_id,
            PeerUser.circle_owner_id == current_user.id,
            PeerUser.revoked_at.is_(None),
        )
    )).scalar_one_or_none()
    if peer is None:
        raise HTTPException(status_code=404, detail="Unknown or revoked peer")
    return LinkHandshakeService(db).create_offer(current_user, peer)


@router.post("/links/accept", response_model=LinkConfirmation)
async def accept_link_offer(
    body: AcceptLinkOfferRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Schritt 2 (auf B, als X) — zustimmen, eigene Zeile schreiben, signieren."""
    try:
        confirmation = await LinkHandshakeService(db).accept_offer(
            current_user, body.offer,
        )
        await db.commit()
    except LinkHandshakeError as e:
        await db.rollback()
        raise _reject(e, current_user.id) from e
    except IntegrityError:
        # 🛑 Der Eindeutigkeitsindex auf (peer_id, querier_ref) IST die
        # eigentliche Sicherung gegen Wiedereinspielung — siehe den
        # Modul-Docstring des Dienstes. Hier schlaegt er zu.
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="This link already exists",
        ) from None
    return confirmation


@router.post("/links/complete", response_model=LinkResponse)
async def complete_link(
    body: CompleteLinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Schritt 3 (zurueck auf A, als U) — pruefen, eigene Zeile schreiben."""
    try:
        link = await LinkHandshakeService(db).complete(
            current_user, body.offer, body.confirmation,
        )
        await db.commit()
    except LinkHandshakeError as e:
        await db.rollback()
        raise _reject(e, current_user.id) from e
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="This link already exists",
        ) from None
    return await _to_response(db, link)


@router.get("/links/mine", response_model=list[LinkResponse])
async def list_my_links(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Die EIGENEN Verknuepfungen dieser Person.

    🛑 Hart auf `local_user_id == current_user.id` gefiltert, nicht auf den
    Peer-Eigentuemer. Und ohne `querier_ref` in der Antwort: der ist das
    opake Band zwischen zwei Instanzen, kein Anzeigewert — wer ihn hat, kann
    sich bei der Gegenstelle als diese Person ausgeben.
    """
    rows = (await db.execute(
        select(FederationUserLink, PeerUser)
        .join(PeerUser, PeerUser.id == FederationUserLink.peer_id)
        .where(FederationUserLink.local_user_id == current_user.id)
        .order_by(FederationUserLink.created_at.desc())
    )).all()
    return [
        LinkResponse(
            id=link.id,
            peer_id=link.peer_id,
            peer_display_name=peer.remote_display_name,
            created_at=link.created_at.isoformat() if link.created_at else "",
        )
        for link, peer in rows
    ]


@router.delete("/links/{link_id}", status_code=204)
async def revoke_my_link(
    link_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_user_or_default),
):
    """Die eigene Verknuepfung widerrufen — einseitig und sofort wirksam."""
    row = (await db.execute(
        select(FederationUserLink, PeerUser)
        .join(PeerUser, PeerUser.id == FederationUserLink.peer_id)
        .where(
            FederationUserLink.id == link_id,
            # 🛑 404 statt 403 bei einer fremden Kennung: eine 403 bestaetigte,
            # dass es diese Verknuepfung GIBT.
            FederationUserLink.local_user_id == current_user.id,
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown link")
    link, peer = row

    await delete_link(db, link_id=link.id)
    await db.commit()

    # 🛑 Laufende Anfragen nachraeumen, wie `revoke_peer` es tut: `handle_retrieve`
    # prueft die Verknuepfung NICHT noch einmal, eine vor dem Widerruf
    # begonnene Anfrage koennte also noch mit voller Reichweite abgeholt werden.
    # Bewusst zu grob — es trifft auch unverknuepfte Anfragen desselben Peers —,
    # dafuer kann es die gemappte nicht verfehlen.
    from services.federation_query_responder import purge_requests_for_pubkey
    discarded = await purge_requests_for_pubkey(peer.remote_pubkey)
    logger.info(
        f"🔗 Verknuepfung {link_id} widerrufen; {discarded} laufende Anfrage(n) verworfen"
    )


async def _to_response(db: AsyncSession, link: FederationUserLink) -> LinkResponse:
    peer = (await db.execute(
        select(PeerUser).where(PeerUser.id == link.peer_id)
    )).scalar_one()
    return LinkResponse(
        id=link.id,
        peer_id=link.peer_id,
        peer_display_name=peer.remote_display_name,
        created_at=link.created_at.isoformat() if link.created_at else "",
    )
