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


def _pending(*, mapped: bool, asker_local_user_id=None, peer_owner_id=None,
             peer_display_name="Nachbars Renfield"):
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
        peer_display_name=peer_display_name,
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
    # 🛑 NICHT `None`. Das bedeutet in `_classify_final` „abgebrochen" und
    # verlöre den Grund; dieser Zug ist FEHLGESCHLAGEN, und zwar erkennbar.
    assert k["final_item"]["success"] is False
    assert k["final_item"]["message"] == "kaputt"


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


class TestTheRecordedOutcomeIsTheTruth:
    """🛑 Die Tests oben pruefen, WAS uebergeben wird — nicht, was daraus wird.

    Genau daran ist mir im `/review` ein Fehler durchgerutscht: der
    Erfolgspfad uebergab `{"answer": …}`, und `_classify_final` liest
    `final.get("success")`. Ohne diesen Schluessel ist der Satz falsy, und jede
    GEGLUECKTE Antwort waere als „failed / Unknown failure" im Protokoll
    gelandet. Ein Pruefprotokoll, das Erfolge als Fehlschlaege verzeichnet, ist
    schlimmer als keines.

    Darum hier durch `_classify_final` hindurch gemessen, statt an der
    Aufrufgrenze stehenzubleiben.
    """

    def test_the_success_shape_classifies_as_success(self):
        from services.federation_audit import _classify_final

        status, _sig, auszug, fehler = _classify_final(
            {"success": True, "message": "eine Antwort"})
        assert status == "success"
        assert auszug == "eine Antwort"
        assert fehler is None

    def test_the_failure_shape_carries_the_REAL_reason(self):
        """Nicht `None` uebergeben: das bedeutet dort „abgebrochen", und der
        Grund ginge verloren."""
        from services.federation_audit import _classify_final

        status, _, _, fehler = _classify_final(
            {"success": False, "message": "Zeitablauf beim Abruf"})
        assert status == "failed"
        assert "Zeitablauf" in fehler

    def test_the_OLD_shape_would_have_been_recorded_as_a_failure(self):
        """🛑 Negativkontrolle, die den Fehler festnagelt, den ich gemacht
        habe. Faellt dieser Test, ist `_classify_final` umgebaut worden — dann
        gehoert der Erfolgspfad oben neu geprueft."""
        from services.federation_audit import _classify_final

        status, _, _, _ = _classify_final({"answer": "eine Antwort"})
        assert status == "failed", (
            "ohne `success`-Schluessel gilt der Satz als Fehlschlag — darum "
            "muss der Erfolgspfad ihn setzen")

    @pytest.mark.asyncio
    async def test_the_responder_passes_the_shape_that_classifies_right(self, geschrieben):
        """Die Kette zusammen: was der Antwortende uebergibt, muss durch
        `_classify_final` als Erfolg bzw. Fehlschlag herauskommen."""
        from services.federation_audit import _classify_final

        p = _pending(mapped=True, asker_local_user_id=107)
        p.answer = "eine Antwort"
        await _write_responder_audit(p, erfolg=True)
        status, _, auszug, _ = _classify_final(
            geschrieben.await_args.kwargs["final_item"])
        assert status == "success" and auszug == "eine Antwort"

        geschrieben.reset_mock()
        await _write_responder_audit(p, erfolg=False, fehler="Zeitablauf")
        status, _, _, fehler = _classify_final(
            geschrieben.await_args.kwargs["final_item"])
        assert status == "failed" and "Zeitablauf" in fehler


class TestTheRecordSaysWHOAccessed:
    """🛑 Gefunden im adversarialen Durchgang am 2026-10-29: der Antwortende
    schrieb `peer_display_name_snapshot=""` — fest. In der Pruefansicht steht
    an genau dieser Stelle, WER zugegriffen hat; jede beantwortete Zeile war
    dort leer. Fuer ein Protokoll, dessen Zweck „wer hat auf meine Daten
    zugegriffen, mit welcher Reichweite" ist, fehlte damit die halbe Aussage.
    """

    @pytest.mark.asyncio
    async def test_the_peer_name_is_recorded(self, geschrieben):
        await _write_responder_audit(
            _pending(mapped=True, asker_local_user_id=107), erfolg=True)
        assert geschrieben.await_args.kwargs[
            "peer_display_name_snapshot"] == "Nachbars Renfield"

    @pytest.mark.asyncio
    async def test_an_empty_name_is_NOT_invented(self, geschrieben):
        """Negativkontrolle. Eine Zeile aus einer aelteren Fassung kennt das
        Feld nicht und faellt auf den leeren Vorgabewert — der wird
        durchgereicht, nicht durch einen erfundenen Namen ersetzt. Die
        Ansicht zeigt dafuer den Schluessel."""
        await _write_responder_audit(
            _pending(mapped=True, asker_local_user_id=107, peer_display_name=""),
            erfolg=True)
        assert geschrieben.await_args.kwargs["peer_display_name_snapshot"] == ""

    def test_the_name_survives_the_redis_round_trip(self):
        """🛑 Der Pending-Satz geht beim Redis-Speicher durch JSON. Ein Feld,
        das die Hin- und Rueckwandlung nicht uebersteht, waere beim rollenden
        Deploy still leer — und der Fehler saehe aus wie der alte."""
        from services.federation_pending_store import _from_jsonable, _to_jsonable

        zurueck = _from_jsonable(_to_jsonable(
            _pending(mapped=True, asker_local_user_id=107)))
        assert zurueck.peer_display_name == "Nachbars Renfield"

    def test_an_older_row_without_the_field_still_loads(self):
        """Vorwaertskompatibilitaet: fehlt der Schluessel, faellt er auf die
        Vorgabe, statt die Wandlung zu sprengen."""
        from services.federation_pending_store import _from_jsonable, _to_jsonable

        d = _to_jsonable(_pending(mapped=True, asker_local_user_id=107))
        del d["peer_display_name"]
        assert _from_jsonable(d).peer_display_name == ""


class TestACancelledAccessIsStillAnAccess:
    """🛑 Gefunden am 2026-10-29: der Abbruchzweig von `_run_query` schrieb
    NICHTS und warf weiter. Die FRAGERSEITE schreibt ihren Vermerk seit F4d in
    einem `finally`, ausdruecklich fuer diesen Fall — diese Seite kam spaeter
    und hat es nicht uebernommen. Ausgerechnet sie verzeichnet den FREMDEN
    Zugriff auf eigene Daten.

    Die datenschutzrelevante Trennlinie ist `_retrieve`: davor wurde nichts
    gelesen, danach lagen fremde Daten im Speicher — auch ohne ausgelieferte
    Antwort. Hinterher ist das nicht mehr rekonstruierbar.
    """

    @pytest.mark.asyncio
    async def test_a_cancellation_AFTER_the_read_is_recorded(self, geschrieben):
        from services.federation_query_responder import (
            _abgebrochenen_zugriff_verzeichnen,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 3
        await _abgebrochenen_zugriff_verzeichnen(p)

        k = geschrieben.await_args.kwargs
        assert k["direction"] == "answered"
        assert k["identity_basis"] == "mapped"
        assert "3 Treffer" in k["final_item"]["message"]

    @pytest.mark.asyncio
    async def test_a_cancellation_BEFORE_the_read_is_recorded_TOO(self, geschrieben):
        """Auch ein Zugriffsversuch ohne Lesevorgang gehoert ins Protokoll —
        dieselbe Begruendung wie bei `test_a_failure_is_recorded_too`: zeigte
        es nur die geglueckten, saehe es ruhiger aus, als es war."""
        from services.federation_query_responder import (
            _abgebrochenen_zugriff_verzeichnen,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        assert p.retrieved_count is None, "Vorgabe: noch nichts gelesen"
        await _abgebrochenen_zugriff_verzeichnen(p)

        assert "keine Daten gelesen" in geschrieben.await_args.kwargs[
            "final_item"]["message"]

    @pytest.mark.asyncio
    async def test_the_two_cases_do_NOT_collapse(self, geschrieben):
        """🛑 Negativkontrolle. `retrieved_count = 0` heisst „gelesen, nichts
        gefunden" und darf nicht mit `None` („nicht gelesen") zusammenfallen —
        darum ist das Feld `int | None` und nicht `int = 0`."""
        from services.federation_query_responder import (
            _abgebrochenen_zugriff_verzeichnen,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 0
        await _abgebrochenen_zugriff_verzeichnen(p)
        text_null = geschrieben.await_args.kwargs["final_item"]["message"]

        geschrieben.reset_mock()
        p.retrieved_count = None
        await _abgebrochenen_zugriff_verzeichnen(p)
        text_none = geschrieben.await_args.kwargs["final_item"]["message"]

        assert text_null != text_none
        assert "nach dem Abruf" in text_null
        assert "vor dem Abruf" in text_none

    @pytest.mark.asyncio
    async def test_it_classifies_as_aborted_NOT_failed(self, geschrieben):
        """🛑 Die Kette, nicht die Aufrufgrenze. Ein Abbruch traegt
        `success=False` und fiele sonst in den Fehlschlag-Zweig — dort liegt
        ein gelesener Zugriff zwischen echten Fehlschlaegen und wird beim
        Durchsehen uebergangen."""
        from services.federation_audit import _classify_final
        from services.federation_query_responder import (
            _abgebrochenen_zugriff_verzeichnen,
        )

        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 3
        await _abgebrochenen_zugriff_verzeichnen(p)

        status, verifiziert, auszug, fehler = _classify_final(
            geschrieben.await_args.kwargs["final_item"])
        assert status == "aborted"
        assert verifiziert is False
        assert auszug is None
        assert "3 Treffer" in fehler

    def test_a_plain_failure_is_STILL_failed(self):
        """Negativkontrolle zum Klassierer: der neue Zweig darf den alten
        nicht schlucken. Ohne `aborted` bleibt es ein Fehlschlag."""
        from services.federation_audit import _classify_final

        status, _, _, _ = _classify_final(
            {"success": False, "message": "Zeitablauf"})
        assert status == "failed"

    @pytest.mark.asyncio
    async def test_a_failing_write_does_NOT_swallow_the_cancellation(self, monkeypatch):
        """🛑 Der Vermerk darf den Abbruch nie ueberdecken: `_run_query` muss
        weiterhin `CancelledError` werfen, auch wenn das Schreiben scheitert."""
        from services import federation_audit
        from services.federation_query_responder import (
            _abgebrochenen_zugriff_verzeichnen,
        )

        monkeypatch.setattr(
            federation_audit, "write_federation_audit",
            AsyncMock(side_effect=RuntimeError("Bank weg")))
        p = _pending(mapped=True, asker_local_user_id=107)
        p.retrieved_count = 3
        # Kein Werfen — der Aufrufer wirft danach selbst weiter.
        await _abgebrochenen_zugriff_verzeichnen(p)


class TestTheCancellationBranchIsACTUALLYWired:
    """🛑 Die Klasse darueber prueft die FUNKTION. Diese prueft, dass
    `_run_query` sie beim Abbruch auch RUFT — genau die Lücke, die mir in
    dieser Sitzung schon einmal nachgewiesen wurde: eine gemessene Funktion
    und eine ungeprueefte Verwendung.
    """

    @pytest.mark.asyncio
    async def test_cancelling_mid_retrieve_still_writes_a_row(self, monkeypatch):
        import asyncio

        from services import federation_query_responder as fqr

        geschrieben = AsyncMock()
        monkeypatch.setattr(fqr, "_abgebrochenen_zugriff_verzeichnen", geschrieben)

        p = _pending(mapped=True, asker_local_user_id=107)
        store = AsyncMock()
        store.get = AsyncMock(return_value=p)
        store.save = AsyncMock()
        monkeypatch.setattr(fqr, "get_pending_store", lambda: store)

        responder = fqr.FederationQueryResponder(db=AsyncMock())
        monkeypatch.setattr(responder, "_emit_progress", AsyncMock())

        losgelaufen = asyncio.Event()

        async def haengender_abruf(session, pending):
            losgelaufen.set()
            await asyncio.sleep(3600)   # bis zum Abbruch

        monkeypatch.setattr(responder, "_retrieve", haengender_abruf)
        # Die eigene Sitzung des Hintergrundlaufs darf keine Bank anfassen.
        monkeypatch.setattr(fqr, "AsyncSessionLocal", lambda: AsyncMock())

        task = asyncio.create_task(responder._run_query(p.request_id))
        await losgelaufen.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        geschrieben.assert_awaited_once()
        assert geschrieben.await_args.args[0] is p

    @pytest.mark.asyncio
    async def test_the_retrieved_count_is_set_before_synthesis(self, monkeypatch):
        """🛑 Die Trennlinie muss GESETZT sein, bevor der naechste
        Unterbrechungspunkt kommt. Stuende sie erst nach der Synthese, waere
        ein Abbruch waehrend der Synthese als „nichts gelesen" verzeichnet —
        die Beschoenigung, gegen die das Ganze geschrieben ist."""
        import asyncio

        from services import federation_query_responder as fqr

        monkeypatch.setattr(fqr, "_abgebrochenen_zugriff_verzeichnen", AsyncMock())
        p = _pending(mapped=True, asker_local_user_id=107)
        store = AsyncMock()
        store.get = AsyncMock(return_value=p)
        store.save = AsyncMock()
        monkeypatch.setattr(fqr, "get_pending_store", lambda: store)
        monkeypatch.setattr(fqr, "AsyncSessionLocal", lambda: AsyncMock())

        responder = fqr.FederationQueryResponder(db=AsyncMock())
        monkeypatch.setattr(responder, "_emit_progress", AsyncMock())
        monkeypatch.setattr(responder, "_retrieve", AsyncMock(return_value=[1, 2, 3]))

        in_synthese = asyncio.Event()

        async def haengende_synthese(frage, treffer):
            in_synthese.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(responder, "_synthesize", haengende_synthese)

        task = asyncio.create_task(responder._run_query(p.request_id))
        await in_synthese.wait()
        assert p.retrieved_count == 3, (
            "zum Zeitpunkt der Synthese muss feststehen, dass gelesen wurde")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
