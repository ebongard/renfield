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
