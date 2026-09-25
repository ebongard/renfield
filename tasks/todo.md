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

- [x] B1 Die 25 redundanten `index=True` (Primärschlüsselspalten) aus den Modellen
      entfernen. Belegen, dass `create_all` danach genau 25 Indizes weniger baut.
- [x] B2 ENTSCHIEDEN. Nicht nach Namen verglichen, sondern nach (Tabelle,
      Spalten) UND Prädikat — ein partieller Index deckt eben nicht alles ab.

      | Gruppe | n | Entscheidung |
      |---|---|---|
      | gleichwertig, nur anderer Name | 8 | Modell bekommt den PRODUKTIONSNAMEN (erst mit B3) |
      | gedeckt durch zusammengesetzten (führende Spalte) | 5 | Modell deklariert den ZUSAMMENGESETZTEN (erst mit B3) |
      | wirklich fehlend | 7 | KEINE Migration — gemessen, s.u. |

      🛑 **Die Falle, in die ich fast gelaufen wäre:** die 13 aus den ersten
      beiden Gruppen NICHT einfach aus dem Modell entfernen. Auf einer
      Neuinstallation werden die Migrationen übersprungen — `create_all` ist
      dort die EINZIGE Quelle. Ohne die Deklaration bekäme eine frische Instanz
      gar keinen Index auf diesen Spalten, also genau den Schaden, den dieser
      Strang beheben soll.

      🛑 **Zwei meiner ersten Einstufungen waren falsch** und wurden beim
      Nachprüfen gefangen: `ix_pf_finalize_unfinalized (created_at) WHERE
      finalized_at IS NULL` und `uq_tool_outcome_system_tool (tool_name) WHERE
      user_id IS NULL` sind PARTIELL. Gleiche Spalte ist nicht gleiche Deckung.
      Sie zählen zu den 7 Fehlenden, nicht zu den Gleichwertigen.

      **Warum für die 7 keine Migration:** die Tabellen sind zu klein, als dass
      der Planer einen Index wählte — `kg_entities` 4 813 Zeilen / 1,2 MB,
      `kg_relations` 4 231, `conversation_memories` 81, `procedural_skills` 5,
      `tool_outcome_stats` 3, `paperless_pending_finalize` LEER. Dieselbe
      Lektion wie bei den Vektorindizes: ein Index, den der Planer nicht nimmt,
      ist reine Schreiblast. Wenn eine dieser Tabellen wächst, neu messen.

      **Umsetzung der 13 gehört zu B3**, nicht davor: der Nutzen entsteht erst
      mit der Basis-Migration, und B3 legt ohnehin fest, wie das Modell Indizes
      deklariert. Vorher umzubauen hieße, Arbeit auf eine noch nicht getroffene
      Entscheidung zu setzen.
- [x] B3 Basis-Migration gebaut: `pc20260926_baseline`, am Kettenende MIT WÄCHTER, die das Produktionsschema erzeugt. Mechanismus noch
      offen — die Kette hat heute EINE Wurzel (`9a0d8ccea5b0`, leerer Rumpf), und
      eine committete Migration darf nicht bearbeitet werden. Kandidaten:
      zweite Wurzel, gestauchte Kette mit angepasstem `alembic_version` auf beiden
      Instanzen, oder Basis am Kettenende mit Neuinstallations-Weiche.
- [x] B4 `init_db()` stempelt die Vorgängerrevision und fährt `upgrade head`: eine frische Datenbank läuft durch
      `alembic upgrade head` statt `create_all` + Stempel.
- [x] B5 BEWIESEN per Mengenvergleich: 76 Tabellen, 333 Indizes, beide Differenzen LEER. leere Datenbank -> `upgrade head` -> Schemaabzug Zeile für Zeile
      gegen die Produktion. KEIN Zahlenvergleich, ein Mengenvergleich.
- [x] B6 Rückweg durchlaufen (333→333→333 und 326→326→326). durchlaufen, nicht behaupten.

## Nicht in diesem Vorhaben
- Die 44 Indizes mit 0 Scans beschneiden — das Fenster trägt die Aussage nicht.
  Wenn das gewollt ist: Statistiken zurücksetzen, zwei Wochen laufen lassen, dann messen.
