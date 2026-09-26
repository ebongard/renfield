"""Meldet, wenn ein eingebuchter Satellit nicht mehr da ist.

WARUM ES DAS GIBT
-----------------
Am 2026-09-26 war die halbe Flotte dunkel, und NICHTS hat es gesagt:

    Fitnessraum, Kinderbad, Wohnzimmer   verbunden
    Esszimmer                            seit 1 Tag weg (Knoten NotReady)
    Arbeitszimmer                        seit 2 Tagen weg
    BensZimmer                           seit 30 TAGEN weg

In 36 Stunden Benachrichtigungen: ausschliesslich `mcp_health`, keine einzige zu
einem Satelliten. Die MCP-Server haben Gesundheitsproben mit Alarm, die
Aufgaben eine Fehlerserien-Erkennung — die Satelliten hatten nichts. Ein Raum
hoert auf zu antworten, und man merkt es, wenn man hineingeht und redet.

🛑 WARUM NICHT `last_authenticated_at` ALLEIN
---------------------------------------------
Das war der naheliegende Wurf und er ist falsch. Die Spalte wird beim
VERBINDUNGSAUFBAU gesetzt, nicht laufend. Ihr Alter misst „Zeit seit dem
letzten Neuverbinden", nicht „Zeit seit dem letzten Lebenszeichen" — ein
Satellit, der seit Tagen stabil verbunden ist, sieht damit aus wie einer, der
seit Tagen weg ist.

Gemessen am 2026-09-26: unmittelbar nach einem Backend-Neustart zeigten alle
drei GESUNDEN Satelliten vier Minuten, weil sie sich neu angemeldet hatten. Vor
dem Neustart waere derselbe Wert Stunden alt gewesen. Ein Waechter auf dieser
Spalte haette also die Gesunden gemeldet und die Toten verschwiegen.

WAS STATTDESSEN
---------------
Die Registratur des `SatelliteManager` sagt, wer JETZT verbunden ist — das ist
die Wahrheit, die auch die Oberflaeche zeigt. Sie lebt aber im Arbeitsspeicher
und ist nach einem Neustart leer, bis sich alle wieder gemeldet haben. Deshalb:

* **Karenz nach dem Start.** Vor `SETTLE_SECONDS` urteilt die Aufgabe NICHT.
  Ohne das meldete jeder Rollout die ganze Flotte als tot — Fehlalarm, der eine
  echte Meldung wertlos macht.
* **Erwartet** sind die eingebuchten, nicht widerrufenen, aktivierten Zeilen aus
  `satellites`.
* **Fehlend** = erwartet minus verbunden. `last_authenticated_at` liefert dann
  nur noch das SEIT WANN fuer den Text, nicht das OB.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy import text

from services.database import AsyncSessionLocal

#: Wie lange nach dem Prozessstart NICHT geurteilt wird. Ein Satellit verbindet
#: sich nach einem Backend-Neustart binnen Sekunden neu (gemessen: alle drei
#: gesunden innerhalb von vier Minuten); fuenf Minuten sind reichlich Luft.
SETTLE_SECONDS = 300

#: Zeitpunkt des Prozessstarts. Modulweit, weil die Aufgabe zustandslos laeuft.
_STARTED_AT = time.monotonic()


def _uptime_seconds() -> float:
    return time.monotonic() - _STARTED_AT


async def _fetch_enrolled() -> list[tuple[str, str | None, datetime | None]]:
    """Eingebucht = nicht widerrufen UND aktiviert. Widerrufene Geraete sind
    absichtlich weg und duerfen nicht als Ausfall gemeldet werden."""
    async with AsyncSessionLocal() as db:
        return list((await db.execute(text("""
            SELECT satellite_id, room, last_authenticated_at
            FROM satellites
            WHERE revoked_at IS NULL AND is_enabled = true
            ORDER BY satellite_id
        """))).all())


async def check_satellite_fleet(*, notify=None, manager=None, fetch_enrolled=None) -> str | None:
    """Vergleiche eingebuchte gegen verbundene Satelliten.

    ``notify`` und ``manager`` sind nur fuer Tests einspeisbar — ohne sie waere
    weder die Karenz noch der Meldeweg pruefbar, ohne einen echten Satelliten.

    Rueckgabe: eine Zeile fuers Laufprotokoll, oder None, wenn nichts zu melden
    ist. Die Aufgabe schweigt im Normalfall.
    """
    if _uptime_seconds() < SETTLE_SECONDS:
        # KEIN Urteil. Die Registratur ist nach einem Neustart leer, bis sich
        # alle gemeldet haben — hier zu melden hiesse, jeden Rollout als
        # Totalausfall zu melden.
        logger.debug("Flottenwache: Karenz nach dem Start, kein Urteil")
        return None

    if manager is None:
        from ha_glue.services.satellite_manager import get_satellite_manager

        manager = get_satellite_manager()
    connected = set(getattr(manager, "satellites", {}) or {})

    if fetch_enrolled is None:
        fetch_enrolled = _fetch_enrolled
    rows = await fetch_enrolled()

    missing = [(sid, room, seen) for sid, room, seen in rows if sid not in connected]
    if not missing:
        return None

    now = datetime.now(UTC).replace(tzinfo=None)
    parts: list[str] = []
    for sid, room, seen in missing:
        if seen is None:
            parts.append(f"{room or sid} (nie verbunden)")
            continue
        hours = int((now - seen).total_seconds() // 3600)
        parts.append(f"{room or sid} (seit {hours} h)" if hours < 48
                     else f"{room or sid} (seit {hours // 24} Tagen)")

    summary = ", ".join(parts)
    logger.warning(
        f"🛰 Flottenwache: {len(missing)} von {len(rows)} Satelliten nicht "
        f"verbunden — {summary}"
    )

    if notify is None:
        from services import ops_alert

        notify = ops_alert.notify_admin
    try:
        await notify(
            title=f"{len(missing)} Satellit(en) offline",
            # Raumnamen sind keine privaten INHALTE — sie stehen so auch in der
            # Geraeteverwaltung. Es geht kein Gespraech und kein Dokument mit.
            message=f"Nicht verbunden: {summary}.",
            # Ein Schluessel je LAGE, nicht je Lauf: solange dieselben Raeume
            # fehlen, wird nicht erneut gemeldet. Aendert sich die Menge, ist es
            # eine neue Nachricht — und das soll es auch sein.
            dedup_key="satellite_fleet_offline:" + ",".join(sorted(s for s, _, _ in missing)),
            event_type="satellite_health",
            source="satellite_fleet_watchdog",
        )
    except Exception as exc:  # pragma: no cover - eine Meldung darf nie stoeren
        logger.warning(f"Flottenwache: Benachrichtigung fehlgeschlagen: {exc}")

    return f"offline: {summary}"
