# Basis-Migration: eine Neuinstallation soll dasselbe Schema bekommen wie die Produktion

Auslöser 2026-09-25: `_ensure_alembic_baseline()` baut das Schema aus den ORM-Modellen
und stempelt den Alembic-Kopf. Roh-SQL-DDL aus Migrationen entsteht dadurch NIE.
Repariert wurde der Bestand (#1336); die URSACHE steht noch.

## Gemessen, nicht vermutet (2026-09-25)

`create_all` (mit ALLEN Modellmodulen importiert) gegen das Produktionsschema:

| | Anzahl | Art |
|---|---|---|
| Tabellen | 75 = 75 | deckungsgleich |
| Indizes | 333 = 333 | **aber zu einem Drittel verschiedene** |
| nur Produktion | 45 | HNSW, Volltext-GIN, partielle + zusammengesetzte, 3 UNIQUE |
| nur `create_all` | 45 | **25 redundant zum Primärschlüssel**, 20 eigenständig-unbelegt |

🛑 Gleiche ZAHL ist nicht gleiche MENGE — das hätte mich fast in die Irre geführt.

**Nutzungsmessung taugt NICHT zum Beschneiden:** Postgres lief erst 3,5 h, die
Zähler sahen also höchstens dieses Fenster. „0 Scans" heißt nicht „ungenutzt",
sondern „in 3,5 h nicht gebraucht" — bei täglichen/wöchentlichen Aufgaben wertlos.
Belastbar ist nur die Gegenrichtung: was Scans HAT, wird gebraucht.

## Entscheidung (aus der Messung, nicht aus Geschmack)

**Die Produktion ist die Wahrheit.** Die Basis erzeugt das heute laufende Schema.
Die 25 redundanten `index=True` kommen NICHT hinein — sie doppeln den PK-Index und
wären auf jeder Neuinstallation reine Schreiblast.

## Schritte

- [ ] B1 Die 25 redundanten `index=True` (Primärschlüsselspalten) aus den Modellen
      entfernen. Belegen, dass `create_all` danach genau 25 Indizes weniger baut.
- [ ] B2 Entscheiden, was mit den 20 eigenständigen `create_all`-Zugaben geschieht:
      entweder ins Produktionsschema aufnehmen (dann Migration) oder aus den
      Modellen entfernen. Je Index einzeln begründen, nicht pauschal.
- [ ] B3 Basis-Migration bauen, die das Produktionsschema erzeugt. Mechanismus noch
      offen — die Kette hat heute EINE Wurzel (`9a0d8ccea5b0`, leerer Rumpf), und
      eine committete Migration darf nicht bearbeitet werden. Kandidaten:
      zweite Wurzel, gestauchte Kette mit angepasstem `alembic_version` auf beiden
      Instanzen, oder Basis am Kettenende mit Neuinstallations-Weiche.
- [ ] B4 `_ensure_alembic_baseline()` ablösen: eine frische Datenbank läuft durch
      `alembic upgrade head` statt `create_all` + Stempel.
- [ ] B5 Beweis: leere Datenbank -> `upgrade head` -> Schemaabzug Zeile für Zeile
      gegen die Produktion. KEIN Zahlenvergleich, ein Mengenvergleich.
- [ ] B6 Rückweg durchlaufen, nicht behaupten.

## Nicht in diesem Vorhaben
- Die 44 Indizes mit 0 Scans beschneiden — das Fenster trägt die Aussage nicht.
  Wenn das gewollt ist: Statistiken zurücksetzen, zwei Wochen laufen lassen, dann messen.
