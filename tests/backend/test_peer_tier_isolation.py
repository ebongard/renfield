"""F-ID-3 — der Diskriminator im Pending-Satz, und warum sein Vorgabewert zählt.

Gefunden im `/review` zu PR #1420: ein Pending-Satz aus einer ÄLTEREN Fassung
kennt weder `mapped` noch `peer_owner_id`. Beim Redis-Speicher ist genau das
während eines rollenden Deploys der Normalfall (`_from_jsonable`: „Missing keys
still fall back to the dataclass defaults — the common older-row case").

🛑 Eine solche Zeile trägt `asker_local_user_id = peer.remote_user_id`, also die
geliehene fremde Kennung, die F-ID-3 abgeschafft hat. Ohne Diskriminator sähe
sie aus wie ein gültiger gemappter Satz und liefe mit genau dieser Kennung —
unter Auth-aus fände sie sogar die Einzelnutzer-Abkürzung und damit das ganze
Gehirn.

Heute ist `FEDERATION_PENDING_USE_REDIS` in beiden Instanzen aus, das Fenster
also null. Aber ein Befund, der nur „solange Flag F aus ist" harmlos ist, hat
ein Verfallsdatum — darum der Riegel statt des Vertrauens auf das Flag.
"""
import pytest

from services.federation_pending_store import _PendingRequest
from services.federation_query_responder import PendingScopeError, _peer_scope_for

pytestmark = [pytest.mark.unit, pytest.mark.backend]


def _satz(**over) -> _PendingRequest:
    grund = dict(
        request_id="r1", asker_pubkey="ab" * 32, peer_user_id=7,
        asker_local_user_id=None, max_visible_tier=2,
        query="was weisst du?", initiated_at=0.0,
    )
    grund.update(over)
    return _PendingRequest(**grund)


def test_a_fallback_record_yields_the_pairing_scope():
    scope = _peer_scope_for(_satz(peer_owner_id=1, mapped=False))
    assert scope is not None
    assert (scope.owner_id, scope.tier) == (1, 2)


def test_a_mapped_record_yields_no_peer_scope():
    """Gemappt heisst: laeuft ALS die verknuepfte Person, voller Kreis-Zugriff."""
    assert _peer_scope_for(_satz(asker_local_user_id=99, mapped=True)) is None


def test_an_OLD_record_is_refused_instead_of_silently_widened():
    """🛑 Der eigentliche Riegel.

    Eine Zeile aus einer aelteren Fassung: kein `mapped`, kein
    `peer_owner_id`, dafuer die geliehene fremde Kennung in
    `asker_local_user_id`. Sie darf NICHT als gemappter Satz durchgehen.
    """
    alt = _satz(asker_local_user_id=42)   # 42 = peer.remote_user_id, wie frueher
    assert alt.mapped is False, "der Vorgabewert muss auf der sicheren Seite liegen"
    assert alt.peer_owner_id is None
    with pytest.raises(PendingScopeError):
        _peer_scope_for(alt)


def test_the_default_of_the_discriminator_is_the_SAFE_side():
    """Negativkontrolle zum Vorgabewert: haette `mapped` die Vorgabe `True`,
    ginge der Test darueber durch, ohne etwas zu pruefen."""
    assert _PendingRequest.__dataclass_fields__["mapped"].default is False
    assert _PendingRequest.__dataclass_fields__["peer_owner_id"].default is None


# =============================================================================
# Die Auflösung einer Paarung — gefunden am 2026-10-28 beim Prüfen der Live-Lage
# =============================================================================
#
# 🛑 Der Eigentümer fand es an der Oberfläche: „auf x-ren gibt es einen peer, auf
# haushalt nicht." Die Peers-Seite zeigt nur EIGENE, nicht widerrufene Zeilen —
# die aktive Zeile im Haushalt gehörte dem Konto 1, die des fragenden Menschen
# war widerrufen. Beim Nachsehen fiel das Ernstere auf: der Antwortende suchte
# seine Paarung ALLEIN über `remote_pubkey`, mit `scalar_one_or_none()`.
#
# Der Eindeutigkeitsindex ist aber `(circle_owner_id, remote_pubkey)`: zwei
# lokale Menschen dürfen sich mit derselben Gegenstelle paaren. Dann wirft
# `scalar_one_or_none()` — jede föderierte Anfrage endet als 500. Genau das
# stand bevor: id=2 (Konto 1, aktiv) und id=3 (Konto 2, widerrufen), beide mit
# x-idras Schlüssel; ein erneutes Paaren hätte die zweite wiederbelebt.

import inspect


def test_the_responder_no_longer_resolves_a_peer_by_pubkey_ALONE():
    """Strukturtest: `scalar_one_or_none()` auf der Schluesselsuche ist weg.

    🛑 Der Rumpf wird geprueft, nicht der Quelltext als Zeichenkette — der
    Kommentar darueber ERKLAERT den alten Aufruf und wuerde eine Textsuche
    treffen. Diesen Fehler habe ich am 2026-10-27 zweimal gemacht.
    """
    import ast
    import textwrap

    from services.federation_query_responder import FederationQueryResponder

    baum = ast.parse(textwrap.dedent(
        inspect.getsource(FederationQueryResponder._lookup_peer)))
    fn = baum.body[0]
    rumpf = ast.unparse(ast.Module(
        body=[k for k in fn.body if not (
            isinstance(k, ast.Expr) and isinstance(k.value, ast.Constant)
            and isinstance(k.value.value, str))],
        type_ignores=[]))
    assert "scalar_one_or_none" not in rumpf, rumpf
    # Negativkontrolle: der Rumpf ist nicht leer, die Pruefung misst also etwas.
    assert "scalars" in rumpf and "revoked_at" in rumpf, rumpf


def test_the_pairing_refuses_a_second_owner_for_the_same_instance():
    """🛑 Die Ursache, nicht nur ihre Erkennung.

    Ohne diese Sperre entstuende die mehrdeutige Lage weiterhin, und der
    Antwortende wiese danach ALLE Anfragen ab — ein stiller Totalausfall der
    Foederation statt einer lesbaren Absage beim Paaren.
    """
    import ast
    import textwrap

    from services.pairing_service import PairingService

    quelle = textwrap.dedent(inspect.getsource(PairingService._upsert_peer_user))
    baum = ast.parse(quelle)
    rumpf = ast.unparse(baum)
    assert "circle_owner_id != owner_user_id" in rumpf, rumpf
    # Seit dem Review zu PR #1421 die SICHTBARE Unterklasse — der uniforme
    # 400 haette die Begruendung verschluckt.
    assert "AlreadyPairedElsewhere" in rumpf, rumpf
    # Und sie sitzt in `_upsert_peer_user`, also greift sie bei accept UND
    # complete — nicht nur an einem der beiden Schritte.
    import services.pairing_service as ps
    aufrufer = [
        n for n in ast.walk(ast.parse(inspect.getsource(ps)))
        if isinstance(n, ast.Attribute) and n.attr == "_upsert_peer_user"
    ]
    assert len(aufrufer) >= 2, f"nur {len(aufrufer)} Aufrufer gefunden"


def test_the_already_paired_refusal_is_VISIBLE_not_swallowed():
    """🛑 Gefunden im Review zu diesem PR.

    Die Routenschicht faengt jeden `PairingError` und macht daraus ein
    uniformes „Pairing handshake failed" — kein Orakel. Diese Regel schuetzt
    die KRYPTOGRAFISCHEN Pruefungen (Signatur, Nonce, Ablauf): niemand soll
    ablesen koennen, welche davon griff.

    „Eine andere lokale Person ist schon gepaart" ist etwas anderes: lokaler
    Zustand, den die angemeldete Person ohnehin aendern darf. Opak gehalten
    schickt er sie an die falsche Stelle — genau dieser Fehlermodus hat am
    2026-10-27 eine Stunde Suche gekostet.

    Darum eine eigene Klasse und ein eigener Fang VOR dem uniformen. Dieser
    Test haelt die Reihenfolge fest: steht der uniforme Fang zuerst, ist die
    Meldung wieder weg, und nichts ausser diesem Test merkt es.
    """
    import ast
    import inspect

    from api.routes import federation_pairing
    from services.pairing_service import AlreadyPairedElsewhere, PairingError

    assert issubclass(AlreadyPairedElsewhere, PairingError), (
        "muss eine Unterklasse bleiben, sonst entkommt sie dem uniformen Fang ganz")

    baum = ast.parse(inspect.getsource(federation_pairing))
    for fn in ast.walk(baum):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        namen = [
            h.type.id for t in ast.walk(fn) if isinstance(t, ast.Try)
            for h in t.handlers if isinstance(h.type, ast.Name)
        ]
        if "PairingError" in namen:
            assert "AlreadyPairedElsewhere" in namen, (
                f"{fn.name}: faengt PairingError, aber nicht den sichtbaren Sonderfall")
            assert namen.index("AlreadyPairedElsewhere") < namen.index("PairingError"), (
                f"{fn.name}: der uniforme Fang steht VOR dem Sonderfall und "
                "verschluckt ihn")
