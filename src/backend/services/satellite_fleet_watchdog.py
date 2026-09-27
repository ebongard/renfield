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

🛑 GEMELDET WIRD NUR EINE AENDERUNG
-----------------------------------
Die erste Fassung verliess sich auf den `dedup_key` von `notify_admin` und ich
habe behauptet, damit werde „nicht stuendlich erneut gemeldet". Das war falsch,
und der Betrieb hat es innerhalb von drei Stunden gezeigt: drei identische
Benachrichtigungen um 13:59, 14:59, 15:59. Der Grund steht in `config.py` —
`proactive_suppression_window` ist **60 Sekunden**. Das unterdrueckt Salven,
keine stuendliche Wiederholung. Bei BensZimmer (30 Tage weg) waeren das 24
Meldungen am Tag: Alarmmuedigkeit mit Ansage, und damit genau das kaputt, wofuer
diese Aufgabe gebaut ist.

Deshalb merkt sich der Waechter die zuletzt GEMELDETE Menge in `system_settings`
(dasselbe Schluessel-Wert-Moebel, das `chat_upload_tool` schon nutzt) und meldet
nur, wenn sie sich aendert — einschliesslich der Rueckkehr: kommt ein Raum
zurueck, ist das eine eigene, gute Nachricht. Ins PROTOKOLL geht weiterhin jeder
Lauf, damit die Lage ohne Benachrichtigung nachlesbar bleibt.
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

#: Schluessel in `system_settings`, unter dem die zuletzt GEMELDETE Menge steht.
#: Dauerhaft, nicht im Speicher: sonst meldete jeder Neustart erneut, und bei
#: einem wochenlangen Ausfall waere das wieder Laerm.
STATE_KEY = "satellite_fleet_watchdog:last_reported"


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


async def _read_last_reported() -> str:
    from models.database import SystemSetting

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, STATE_KEY)
        return (row.value or "") if row else ""


async def _write_last_reported(value: str) -> None:
    from models.database import SystemSetting

    async with AsyncSessionLocal() as db:
        row = await db.get(SystemSetting, STATE_KEY)
        if row is None:
            db.add(SystemSetting(key=STATE_KEY, value=value))
        else:
            row.value = value
        await db.commit()


async def check_satellite_fleet(
    *, notify=None, manager=None, fetch_enrolled=None,
    read_state=None, write_state=None,
) -> str | None:
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

    # Die zuletzt GEMELDETE Menge, damit nur eine Aenderung eine Nachricht wird.
    read_state = read_state or _read_last_reported
    write_state = write_state or _write_last_reported
    signature = ",".join(sorted(sid for sid, _, _ in missing))
    previous = await read_state()
    changed = signature != previous

    if not missing:
        if changed:
            # Rueckkehr ist eine eigene, gute Nachricht — und der Marker MUSS
            # geleert werden, sonst bliebe der naechste Ausfall stumm.
            logger.info("🛰 Flottenwache: alle Satelliten wieder verbunden")
            await _try_notify(
                notify, title="Alle Satelliten wieder verbunden",
                message="Es fehlt kein Satellit mehr.",
                dedup_key=f"{STATE_KEY}:clear",
            )
            await write_state("")
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
    line = (f"🛰 Flottenwache: {len(missing)} von {len(rows)} Satelliten nicht "
            f"verbunden — {summary}")
    # Ins PROTOKOLL geht JEDER Lauf: die Lage soll ohne Benachrichtigung
    # nachlesbar sein. Nur die Dringlichkeit unterscheidet unveraendert von neu.
    (logger.warning if changed else logger.info)(
        line if changed else line + " (unveraendert, keine neue Meldung)")

    if changed:
        await _try_notify(
            notify, title=f"{len(missing)} Satellit(en) offline",
            # Raumnamen sind keine privaten INHALTE — sie stehen so auch in der
            # Geraeteverwaltung. Es geht kein Gespraech und kein Dokument mit.
            message=f"Nicht verbunden: {summary}.",
            dedup_key=f"satellite_fleet_offline:{signature}",
        )
        await write_state(signature)

    return f"offline: {summary}" + ("" if changed else " (unveraendert)")


async def _try_notify(notify, *, title: str, message: str, dedup_key: str) -> None:
    """EINE Meldung an den Eigentuemer. Ein Fehler hier darf die Aufgabe nie
    umbringen — die Lage steht ohnehin im Protokoll."""
    if notify is None:
        from services import ops_alert

        notify = ops_alert.notify_admin
    try:
        await notify(title=title, message=message, dedup_key=dedup_key,
                     event_type="satellite_health",
                     source="satellite_fleet_watchdog")
    except Exception as exc:  # pragma: no cover - eine Meldung darf nie stoeren
        logger.warning(f"Flottenwache: Benachrichtigung fehlgeschlagen: {exc}")
