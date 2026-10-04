"""Die Abhängigkeiten, ohne die der async-Pfad beim IMPORT stirbt.

🛑 DER AUSFALL, GEGEN DEN DIESE DATEI GESCHRIEBEN IST (2026-10-04)
==================================================================
`requirements.txt` führte `sqlalchemy>=2.0.25` — ohne den `[asyncio]`-Extra,
und `greenlet` war nirgends deklariert. Der async-Pfad hat es immer gebraucht;
es kam nur transitiv mit, weil SQLAlchemy 2.0 es implizit zog.

Am 2026-10-04 sprang der Bau über die offene Untergrenze von 2.0.54 auf 2.1.3.
Dort ist greenlet nicht mehr implizit — und jeder Backend-Pod starb beim
Import:

    ImportError: The SQLAlchemy asyncio module requires that the Python
    'greenlet' library is installed.

Haushalt-Backend und alle drei Worker gingen in `CrashLoopBackOff`. Der Dienst
war unten, bis auf das letzte gute Bild zurückgerollt wurde. Kein Test hat es
vorher gemerkt, weil der Prüfstand im ALTEN Bild läuft — er hatte greenlet.

Diese Datei prüft die DEKLARATION, nicht die Umgebung. Eine Umgebungsprüfung
wäre hier wertlos: sie wäre genau dort grün, wo sie nichts beweist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_REQUIREMENTS = Path(__file__).resolve().parents[2] / "src" / "backend" / "requirements.txt"


def _zeilen() -> list[str]:
    """Die Anforderungszeilen ohne Kommentare und Leerzeilen."""
    return [
        z.split("#", 1)[0].strip()
        for z in _REQUIREMENTS.read_text().splitlines()
        if z.split("#", 1)[0].strip()
    ]


def _eintrag(paket: str) -> str:
    """Die Zeile, die `paket` deklariert — Extras eingeschlossen."""
    muster = re.compile(rf"^{re.escape(paket)}(\[[^\]]*\])?\s*[<>=!~]", re.I)
    treffer = [z for z in _zeilen() if muster.match(z)]
    assert len(treffer) == 1, f"{paket}: erwartet genau eine Zeile, gefunden {treffer}"
    return treffer[0]


def test_sqlalchemy_declares_the_asyncio_extra():
    """🛑 Der Code importiert `sqlalchemy.ext.asyncio`; dieser Pfad braucht
    greenlet. Ohne den Extra hängt das an der Laune des Abhängigkeitslösers —
    und am 2026-10-04 fiel sie anders aus als am Tag davor.

    Der Extra ist, was die Fehlermeldung selbst empfiehlt.
    """
    zeile = _eintrag("sqlalchemy")
    assert "[asyncio]" in zeile.lower(), (
        f"sqlalchemy ohne [asyncio]-Extra deklariert: {zeile!r}. "
        "Der async-Pfad braucht greenlet; ohne den Extra kommt es nur zufällig mit."
    )


def test_sqlalchemy_is_bounded_above():
    """🛑 Die andere Hälfte: ein Deploy darf keine ungebetene
    Abhängigkeits-Aktualisierung sein.

    Die offene Untergrenze liess den Bau eine Nebenversionsgrenze überschreiten,
    an der sich die Abhängigkeitssemantik änderte. Der Sprung auf 2.1 ist ein
    eigener, geprüfter Schritt — kein Nebeneffekt davon, dass jemand an einem
    anderen Tag gebaut hat.
    """
    zeile = _eintrag("sqlalchemy")
    assert re.search(r"<\s*\d", zeile), (
        f"sqlalchemy ohne Obergrenze: {zeile!r}. Jeder Bau kann damit über eine "
        "Versionsgrenze springen, ohne dass es jemand entschieden hat."
    )


def test_the_async_imports_the_code_actually_uses_are_declared():
    """Gegenprobe, dass dieser Test nicht am falschen Paket hängt: der Code
    importiert tatsächlich `sqlalchemy.ext.asyncio`. Fällt dieser Import eines
    Tages weg, sind die beiden Tests oben gegenstandslos und sollen auffallen,
    statt still weiterzulaufen."""
    quelle = (
        Path(__file__).resolve().parents[2] / "src" / "backend" / "services" / "database.py"
    ).read_text()
    assert "from sqlalchemy.ext.asyncio import" in quelle, (
        "services/database.py importiert `sqlalchemy.ext.asyncio` nicht mehr — "
        "die Begründung für den [asyncio]-Extra ist damit zu prüfen."
    )
    assert "async_sessionmaker" in quelle
