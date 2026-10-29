"""Der Antwortende hält fest, auf WELCHER Identität er geantwortet hat.

🛑 Der Anlass: am 2026-10-29 lief der erste erfolgreiche föderierte Zug der
Projektgeschichte (`status=success`, Signatur geprüft). Ob er als die
verknüpfte Person lief — volle eigene Reichweite auf der Gegenseite — oder im
Rückfall auf die gewährte Stufe, ließ sich **hinterher nicht feststellen**.
Nur erschließen: aus dem gleichen `querier_ref` auf beiden Seiten und aus dem
Code. Der Pending-Satz, der `mapped` trägt, liegt im Arbeitsspeicher und ist
nach dem Zug weg.

Für eine Unterscheidung, die über die REICHWEITE eines Zugriffs entscheidet,
ist ein Rückschluss zu wenig.

🛑 Und es muss der ANTWORTENDE sein. Bis heute schrieb nur die Fragerseite
(`mcp_client.py`, ein einziger Aufrufer). Ein Vermerk dort wäre die Behauptung
der Gegenseite über ihre eigene Zugriffsentscheidung; die Instanz, deren Daten
gelesen wurden, muss den Zugriff selbst verzeichnen.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from services.federation_query_responder import (
    _PendingRequest,
    _write_responder_audit,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _pending(*, mapped: bool, asker_local_user_id=None, peer_owner_id=None):
    return _PendingRequest(
        request_id="r1",
        asker_pubkey="aa" * 32,
        peer_user_id=3,
        asker_local_user_id=asker_local_user_id,
        max_visible_tier=2,
        query="was liegt in x-idra?",
        initiated_at=time.time(),
        peer_owner_id=peer_owner_id,
        mapped=mapped,
    )


@pytest.fixture
def geschrieben(monkeypatch):
    """Fängt den Schreibvorgang ab, statt die Datenbank zu bemühen."""
    from services import federation_audit

    m = AsyncMock()
    monkeypatch.setattr(federation_audit, "write_federation_audit", m)
    return m


async def test_the_mapped_path_is_recorded_as_mapped(geschrieben):
    """Der Fall, der die Reichweite öffnet — und der belegt werden muss."""
    p = _pending(mapped=True, asker_local_user_id=107)
    p.answer = "eine Antwort"
    await _write_responder_audit(p, erfolg=True)

    geschrieben.assert_awaited_once()
    k = geschrieben.await_args.kwargs
    assert k["identity_basis"] == "mapped"
    assert k["direction"] == "answered"
    assert k["user_id"] == 107, "die VERKNÜPFTE Person, deren Reichweite galt"


async def test_the_fallback_is_recorded_as_peer_tier(geschrieben):
    """🛑 Negativkontrolle gegen eine Prüfung, die immer „mapped" sagt. Und
    `user_id` ist hier der EIGENTÜMER der Paarung — auch im Rückfall wurde
    die Reichweite einer lokalen Person benutzt, nur eine engere."""
    await _write_responder_audit(_pending(mapped=False, peer_owner_id=2), erfolg=True)

    k = geschrieben.await_args.kwargs
    assert k["identity_basis"] == "peer_tier"
    assert k["user_id"] == 2


async def test_a_failure_is_recorded_too(geschrieben):
    """Ein gescheiterter Zugriff ist auch ein Zugriffsversuch und gehört ins
    Protokoll — sonst zeigt es nur die geglückten und sieht ruhiger aus, als
    es war."""
    await _write_responder_audit(
        _pending(mapped=True, asker_local_user_id=107), erfolg=False, fehler="kaputt")

    k = geschrieben.await_args.kwargs
    assert k["direction"] == "answered"
    assert k["final_item"] is None


async def test_a_record_with_NEITHER_writes_nothing(geschrieben):
    """Weder gemappt noch mit Eigentümer: ein Satz aus einer älteren Fassung,
    den `_peer_scope_for` ohnehin abweist. Eine Zeile mit erfundener `user_id`
    wäre schlimmer als keine — sie schriebe einen Zugriff einer Person zu, die
    ihn nicht hatte."""
    await _write_responder_audit(_pending(mapped=False), erfolg=True)
    geschrieben.assert_not_awaited()


async def test_the_asker_side_keeps_its_meaning():
    """🛑 Negativkontrolle an der Vorgabe: wer `write_federation_audit` ohne
    die neuen Angaben ruft — also die unveränderte Fragerseite —, schreibt
    `asked` und KEINE Identitätsgrundlage. Ein Vorgabewert dort würde Unwissen
    als Tatsache ausgeben."""
    import inspect

    from services.federation_audit import write_federation_audit

    sig = inspect.signature(write_federation_audit)
    assert sig.parameters["direction"].default == "asked"
    assert sig.parameters["identity_basis"].default is None


def test_both_columns_exist_on_the_model():
    """Der Schreibvorgang nützt nichts, wenn die Spalten fehlen — und die
    Migration ist die einzige Stelle, die sie anlegt."""
    from models.database import FederationQueryLog

    spalten = {c.name: c for c in FederationQueryLog.__table__.columns}
    assert spalten["direction"].nullable is False
    assert spalten["identity_basis"].nullable is True, (
        "nullable, weil Bestandszeilen und Frager-Zeilen es NICHT wissen")
