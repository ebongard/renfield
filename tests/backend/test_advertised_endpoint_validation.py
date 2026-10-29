"""Ein unauflösbarer Endpunkt wird bei der Paarung abgewiesen, nicht gespeichert.

🛑 Der Anlass, am 2026-10-29 gefunden: in `peer_users.transport_config` stand
`https://renfield.loca` — dem Namen fehlte das letzte `l`, UND es war der Name
der GEGENSEITE statt des eigenen. Beides kam aus dem Feld „Erreichbare URL" im
Paarungsdialog, das die Adresse meint, unter der andere DIESE Instanz erreichen.

Die Paarung meldete Erfolg. Der Ausfall zeigte sich erst beim ersten echten
Gebrauch — Tage später, als `[Errno -2] Name or service not known` tief im
Föderationsclient, und auch dann nur, weil wir ihn gesucht haben.

Die vorhandene Säuberung verwarf schon Leeres mit der Begründung, sonst
persistiere „a garbage endpoint". Ein Tippfehler ist auch Unrat.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from api.routes.federation_pairing import _resolve_advertised_endpoints

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


async def test_an_unresolvable_host_is_refused():
    """Der gemeldete Fall, wörtlich."""
    with pytest.raises(HTTPException) as e:
        await _resolve_advertised_endpoints([{"url": "https://renfield.loca"}])
    assert e.value.status_code == 400
    assert "renfield.loca" in str(e.value.detail)


async def test_the_message_says_WHOSE_address_is_meant():
    """🛑 Die Meldung muss die Fehldeutung benennen, die den Fehler erzeugt
    hat — sonst trägt die Person denselben falschen Namen noch einmal ein,
    nur richtig geschrieben. Die Richtung ist der Fehler, nicht die
    Rechtschreibung."""
    with pytest.raises(HTTPException) as e:
        await _resolve_advertised_endpoints([{"url": "https://gibtesnicht.invalid"}])
    text = str(e.value.detail)
    assert "GEGENSEITE" in text and "DIESE" in text
    assert "leer" in text.lower()


async def test_a_resolvable_host_passes():
    """🛑 Negativkontrolle: die Prüfung darf nicht einfach alles abweisen.
    `localhost` löst überall auf."""
    assert await _resolve_advertised_endpoints(
        [{"url": "http://localhost:8000"}]) == [{"url": "http://localhost:8000"}]


async def test_a_url_without_a_hostname_is_refused():
    with pytest.raises(HTTPException) as e:
        await _resolve_advertised_endpoints([{"url": "keine-url"}])
    assert e.value.status_code == 400


async def test_the_CONFIGURED_default_is_not_checked(monkeypatch):
    """🛑 Geprüft wird nur die Eingabe aus der Oberfläche.

    Der konfigurierte Standard ist Betreibersache, durchläuft ein Review und
    darf bewusst einen Namen tragen, den diese Instanz selbst nicht auflöst
    (geteilte Horizonte: der öffentliche Name zeigt nach außen woanders hin).
    Würde die Prüfung auch ihn treffen, bräche sie einen gültigen Aufbau —
    und zwar beim Paaren, also genau dann, wenn niemand damit rechnet.
    """
    from api.routes import federation_pairing as fp

    monkeypatch.setattr(fp.settings, "federation_advertised_url",
                        "https://nur-von-aussen.invalid")
    assert await _resolve_advertised_endpoints([]) == [
        {"url": "https://nur-von-aussen.invalid"}]


async def test_an_all_blank_override_still_falls_through(monkeypatch):
    """Das vorhandene Verhalten bleibt: leere Einträge zählen nicht als
    Übersteuerung und dürfen die Prüfung nicht auslösen."""
    from api.routes import federation_pairing as fp

    monkeypatch.setattr(fp.settings, "federation_advertised_url",
                        "https://localhost")
    assert await _resolve_advertised_endpoints([{"url": ""}, {}]) == [
        {"url": "https://localhost"}]
