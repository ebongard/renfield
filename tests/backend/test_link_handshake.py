"""F-ID-2 — der Personen-Handschlag, und vor allem seine Ablehnungen.

Anlass (2026-10-27): die Personenverknuepfung liess sich bis dahin nur von
einem Administrator BEHAUPTEN. Der Handschlag ersetzt die Behauptung durch
einen Beweis — und ist damit die Stelle, an der ein Fehler teuer ist: eine
angenommene Verknuepfung hebt fuer die betroffene Person die Paarungsstufe
vollstaendig auf (`enforce_circles=False`, volle eigene Reichweite).

🛑 Die Tests, die hier mehr tragen als der gluecklichen Pfad:

- **Das Angebot nennt seinen Empfaenger.** Ohne diese Pruefung liesse sich ein
  fuer B bestimmtes Angebot bei C einreichen, und C schriebe eine Zeile fuer
  eine Person, der nie jemand zugestimmt hat.
- **Ein Angebot ist nicht uebertragbar.** `stated_by` muss die angemeldete
  Person sein, sonst schliesst ein Fremder ein abgefangenes Angebot auf
  SEINEN Namen ab.
- **Kein halber Link.** Jede Ablehnung darf KEINE Zeile hinterlassen; der
  Test prueft das am Aufrufzaehler von `create_link`, nicht am Rueckgabewert.
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
# Gerüst
# =============================================================================


@pytest.fixture
def instanz_a(tmp_path):
    """Instanz A über das Singleton — so wie der Produktionscode sie sieht."""
    reset_federation_identity_for_tests()
    init_federation_identity(tmp_path / "a_key")
    yield get_federation_identity()
    reset_federation_identity_for_tests()


@pytest.fixture
def instanz_b():
    """Instanz B DANEBEN gebaut, ohne Singleton — das Muster aus
    `test_federation_pairing.py`, mit dem sich zwei Instanzen in einem Prozess
    simulieren lassen."""
    return FederationIdentity(ed25519.Ed25519PrivateKey.generate())


def _nutzer(uid: int):
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
def frischer_nonce_speicher():
    """Der Nonce-Speicher ist modulglobal — ohne Leeren faerbt ein Test auf den
    naechsten ab und `Nonce already used` feuert grundlos."""
    from services.federation_pending_store import get_pending_store, reset_store_for_tests
    reset_store_for_tests()
    yield
    import asyncio
    with __import__("contextlib").suppress(Exception):
        asyncio.get_event_loop().run_until_complete(get_pending_store().clear_for_tests())
    reset_store_for_tests()


def _dienst(identity, *, peer=None, links=None):
    """Ein Dienst mit gestellter Identität, gestellter Peer-Suche und
    gezähltem `create_link`."""
    svc = LinkHandshakeService(db=MagicMock())
    svc.identity = identity
    svc._peer_by_pubkey = AsyncMock(
        return_value=peer if peer is not None else _peer(7, "ab" * 32))
    return svc


@pytest.fixture
def create_link_zaehler(monkeypatch):
    """Zählt echte Schreibversuche. Eine Ablehnung MUSS bei 0 bleiben."""
    import services.link_handshake_service as mod
    aufrufe = []

    async def _fake(db, **kw):
        aufrufe.append(kw)
        row = MagicMock()
        row.id = 100 + len(aufrufe)
        row.peer_id = kw["peer_id"]
        return row

    monkeypatch.setattr(mod, "create_link", _fake)
    return aufrufe


# =============================================================================
# Der glückliche Pfad
# =============================================================================


async def test_both_sides_write_the_SAME_querier_ref(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """🛑 Der Kern: die beiden Zeilen sind nur durch denselben `querier_ref`
    verbunden. Driften sie auseinander, loest die Gegenstelle die Person nie
    auf und faellt still auf die Paarungsstufe zurueck."""
    u, x = _nutzer(1), _nutzer(9)

    a = _dienst(instanz_a, peer=_peer(7, instanz_b.public_key_hex()))
    angebot = a.create_offer(u, _peer(7, instanz_b.public_key_hex()))

    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))
    bestaetigung = await b.accept_offer(x, angebot)

    link = await a.complete(u, angebot, bestaetigung)

    assert len(create_link_zaehler) == 2, create_link_zaehler
    assert create_link_zaehler[0]["querier_ref"] == create_link_zaehler[1]["querier_ref"]
    assert create_link_zaehler[0]["local_user_id"] == 9   # X auf B
    assert create_link_zaehler[1]["local_user_id"] == 1   # U auf A
    assert link.id


async def test_step_one_writes_NOTHING(instanz_a, create_link_zaehler):
    """Bricht die Person nach dem Angebot ab, darf kein halber Link stehen."""
    a = _dienst(instanz_a)
    a.create_offer(_nutzer(1), _peer(7, "cd" * 32))
    assert create_link_zaehler == []


# =============================================================================
# Ablehnungen — und jede davon OHNE Zeile
# =============================================================================


async def test_an_offer_for_SOMEONE_ELSE_is_refused(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """🛑 Das Angebot nennt seinen Empfaenger. Ein fuer B bestimmtes Angebot
    darf bei C nicht durchgehen — sonst schriebe C eine Zeile fuer eine Person,
    der nie jemand zugestimmt hat."""
    instanz_c = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
    a = _dienst(instanz_a)
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))

    c = _dienst(instanz_c, peer=_peer(5, instanz_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="different responder"):
        await c.accept_offer(_nutzer(9), angebot)
    assert create_link_zaehler == []


async def test_an_offer_is_NOT_transferable(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """🛑 `stated_by` bindet das Angebot an die Person, die es erklaert hat.
    Ohne die Pruefung schliesst eine andere angemeldete Person ein abgefangenes
    Angebot auf IHREN Namen ab."""
    a = _dienst(instanz_a, peer=_peer(7, instanz_b.public_key_hex()))
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))
    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))
    bestaetigung = await b.accept_offer(_nutzer(9), angebot)
    create_link_zaehler.clear()

    with pytest.raises(LinkHandshakeError, match="different user"):
        await a.complete(_nutzer(2), angebot, bestaetigung)   # NICHT Nutzer 1
    assert create_link_zaehler == []


async def test_a_tampered_offer_is_refused(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """Ein veraendertes Feld bricht die Signatur — hier die Kennung der Person,
    also genau das Feld, dessen Faelschung sich lohnen wuerde."""
    a = _dienst(instanz_a)
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))
    verbogen = angebot.model_copy(update={"stated_by": 999})

    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="signature"):
        await b.accept_offer(_nutzer(9), verbogen)
    assert create_link_zaehler == []


async def test_a_FORGED_offer_from_an_unknown_key_is_refused(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """Ein Fremder baut ein formal tadelloses Angebot und unterschreibt es mit
    seinem eigenen Schluessel. Die Peer-Zeile nennt aber A's Schluessel."""
    fremd = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
    now = int(time.time())
    unsigned = {
        "version": 1,
        "initiator_pubkey": instanz_a.public_key_hex(),   # BEHAUPTET A zu sein
        "responder_pubkey": instanz_b.public_key_hex(),
        "stated_by": 1,
        "querier_ref": "ef" * 32,
        "nonce": "0123456789abcdef0123456789abcdef",
        "issued_at": now,
        "expires_at": now + 600,
    }
    gefaelscht = LinkOffer(
        **unsigned, signature=fremd.sign(canonical_bytes(unsigned)).hex())

    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="signature"):
        await b.accept_offer(_nutzer(9), gefaelscht)
    assert create_link_zaehler == []


async def test_an_expired_offer_is_refused(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    a = _dienst(instanz_a)
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))
    alt = int(time.time()) - LINK_OFFER_TTL_SECONDS - 1
    abgelaufen = angebot.model_copy(update={"issued_at": alt, "expires_at": alt + 1})

    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))
    with pytest.raises(LinkHandshakeError, match="expired"):
        await b.accept_offer(_nutzer(9), abgelaufen)
    assert create_link_zaehler == []


async def test_a_confirmation_from_ANOTHER_instance_is_refused(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """Die Bestaetigung muss von der Instanz kommen, der das Angebot galt."""
    instanz_c = FederationIdentity(ed25519.Ed25519PrivateKey.generate())
    a = _dienst(instanz_a, peer=_peer(7, instanz_b.public_key_hex()))
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))

    # C bestaetigt, obwohl das Angebot B nennt.
    unsigned = {
        "version": 1,
        "nonce": angebot.nonce,
        "querier_ref": angebot.querier_ref,
        "responder_pubkey": instanz_c.public_key_hex(),
        "responder_user_id": 9,
        "accepted_at": int(time.time()),
    }
    fremde = LinkConfirmation(
        **unsigned, signature=instanz_c.sign(canonical_bytes(unsigned)).hex())

    with pytest.raises(LinkHandshakeError, match="different instance"):
        await a.complete(_nutzer(1), angebot, fremde)
    assert create_link_zaehler == []


async def test_a_confirmation_answering_ANOTHER_offer_is_refused(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """Zwei Angebote nebeneinander: die Bestaetigung des einen darf das andere
    nicht abschliessen."""
    a = _dienst(instanz_a, peer=_peer(7, instanz_b.public_key_hex()))
    erstes = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))
    zweites = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))

    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))
    bestaetigung_zum_zweiten = await b.accept_offer(_nutzer(9), zweites)
    create_link_zaehler.clear()

    with pytest.raises(LinkHandshakeError, match="does not answer"):
        await a.complete(_nutzer(1), erstes, bestaetigung_zum_zweiten)
    assert create_link_zaehler == []


async def test_WITHOUT_a_pairing_there_is_nothing_to_link(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """🛑 Der Handschlag SETZT die Instanz-Paarung voraus, er ersetzt sie nicht.
    Ohne aktive Peer-Zeile gibt es keinen Schluessel, gegen den sich pruefen
    liesse."""
    a = _dienst(instanz_a)
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))

    b = _dienst(instanz_b)
    b._peer_by_pubkey = AsyncMock(
        side_effect=LinkHandshakeError("No active pairing with that instance"))
    with pytest.raises(LinkHandshakeError, match="No active pairing"):
        await b.accept_offer(_nutzer(9), angebot)
    assert create_link_zaehler == []


async def test_the_same_offer_cannot_be_accepted_twice_in_the_nonce_window(
    instanz_a, instanz_b, create_link_zaehler, frischer_nonce_speicher,
):
    """Der billige Frueh-Abweiser gegen das doppelte Abschicken.

    🛑 Er ist KEINE Mauer gegen Wiedereinspielung: der Speicher haelt einen
    Nonce nur `NONCE_WINDOW_SECONDS` (60 s), das Angebot lebt 600 s. Was
    wirklich traegt, ist der Eindeutigkeitsindex auf `(peer_id, querier_ref)` —
    siehe den Modul-Docstring des Dienstes. Dieser Test prueft genau den
    Ausschnitt, den der Nonce abdeckt, und nicht mehr.
    """
    a = _dienst(instanz_a)
    angebot = a.create_offer(_nutzer(1), _peer(7, instanz_b.public_key_hex()))
    b = _dienst(instanz_b, peer=_peer(3, instanz_a.public_key_hex()))

    await b.accept_offer(_nutzer(9), angebot)
    with pytest.raises(LinkHandshakeError, match="Nonce already used"):
        await b.accept_offer(_nutzer(9), angebot)
    assert len(create_link_zaehler) == 1


# =============================================================================
# Strukturtest
# =============================================================================


def test_the_handshake_routes_are_NOT_admin_gated():
    """🛑 Der ganze Zweck des Stuecks.

    Die Admin-Behauptung existiert bereits (`federation_user_links`); dieser
    Handschlag ist ihr Ersatz, weil er die Kontrolle ueber BEIDE Konten
    beweist statt einem Administrator zu glauben. Haengt jemand hier ein
    `require_permission(ADMIN)` davor, ist der Beweis wieder eine Behauptung
    mit mehr Schritten — und nichts ausser diesem Test merkt es.

    🛑 Geprueft werden die ECHTEN Abhaengigkeiten der Routen, nicht der
    Quelltext. Die erste Fassung greppte nach „require_permission" und fiel
    ueber den Kommentar, der genau das verspricht — ein Test, der die Prosa
    misst statt die Wirkung. Die Gegenprobe darunter zeigt, dass der Zaehler
    ueberhaupt anschlaegt.
    """
    from api.routes import federation_links, federation_user_links

    def waechter(router) -> set[str]:
        namen: set[str] = set()
        for route in router.routes:
            for dep in getattr(route, "dependant", None).dependencies:
                if dep.call is not None:
                    namen.add(dep.call.__name__)
        return namen

    frei = waechter(federation_links.router)
    gesperrt = waechter(federation_user_links.router)

    # Negativkontrolle zuerst: der Nachbar IST gesperrt, der Zaehler misst also.
    assert "permission_checker" in gesperrt, (
        "Die Gegenprobe greift nicht mehr — `federation_user_links` ist nicht "
        f"mehr admin-gesperrt. Gefunden: {sorted(gesperrt)}")
    assert "permission_checker" not in frei, (
        "Der Personen-Handschlag ist admin-gesperrt und damit sinnlos. "
        f"Gefunden: {sorted(frei)}")


def test_both_protocols_sign_the_SAME_bytes():
    """🛑 Eine abgeschriebene zweite Kanonisierung waere der stille Bruch:
    beide Seiten signierten fleissig, und nichts verifizierte mehr."""
    from services import pairing_service

    assert pairing_service._canonical_bytes is canonical_bytes
