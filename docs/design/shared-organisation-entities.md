# Dieselbe Organisation über Nutzergrenzen ist EINE Entität

Entwurf zu Issue #1314. Status: **entschieden, nicht gebaut.**

## Der Befund

Schreibt dieselbe Organisation an zwei verschiedene Haushaltsmitglieder, entstehen
heute **zwei** `kg_entities`-Knoten. Es ist aber dieselbe Organisation — die
Aufspaltung ist ein Modellierungsfehler, kein Datenschutzmerkmal.

## Warum es so ist

Es ist kein Versehen, sondern eine ausdrücklich vertagte Entscheidung.
`resolve_entity` sagt es wörtlich:

> *Cross-user entity dedup stays deferred to the named-circles work; v1 is
> per-user (own + unowned only).*

Drei Stellen setzen das um:

1. **`resolve_entity`** trifft nur `user_id == Frager OR user_id IS NULL`. Zwei
   Briefe an verschiedene Nutzer landen in zwei Entitäten, selbst bei identischem
   Namen.
2. **`merge_entities` weist Paare über Nutzergrenzen ausdrücklich zurück**
   (`knowledge_graph_service.py:917`). Die Regel existiert, damit nie zwei
   *Personen* verschmelzen — für Organisationen ist sie zu grob.
3. **Der Abgleicher** hat einen Personen-Wächter, aber keinen
   Zusammenführungs-Pfad für Organisationen; der Konflations-Wächter betrachtet
   Nicht-Personen-Paare, mutiert aber nie.

Die vertagende Bedingung ist eingetreten: die Kreise sind gebaut, der
auth-on-Cutover ist vorbereitet.

## Die Trennlinie: Knoten ja, Kanten nein

Die Entität selbst zu teilen ist harmlos — eine Organisation ist keine private
Tatsache. An einer geteilten Entität hängen aber Relationen, und die gehören
weiter je Nutzer: **wer den Knoten sieht, darf daraus nicht ableiten, dass ein
anderes Haushaltsmitglied mit dieser Organisation zu tun hat.**

**Das liefert die vorhandene Mechanik von selbst.** Eine Relation bekommt ihre
Stufe als `LEAST(Subjekt, Objekt)` (`atom_service.update_tier`, die Hausregel
„eine Zusammenführung hebt nie die Sichtbarkeit"). Zeigt die Stufe-0-Person eines
Nutzers auf die Stufe-2-Organisation, wird die Kante `LEAST(0, 2) = 0` — sie
bleibt privat. Für die Trennlinie ist **nichts zu bauen**; sie ist eine
Eigenschaft der Kaskade, die schon da ist. Das ist der Grund, warum dieser Posten
M und nicht L ist.

## Entschieden

### D-1 · Der erste Finder behält den Knoten, die Stufe steigt auf 2

Der Knoten bleibt bei dem Nutzer, dessen Dokument ihn angelegt hat, und wird auf
Stufe 2 gehoben; der zweite Nutzer hängt seine Relationen daran. Kein neuer
Eigentümer-Begriff, keine Sonderkonten.

**Eigentümerlos scheidet technisch aus** und das ist keine Geschmacksfrage: unter
auth-on ist eine eigentümerlose Zeile in *jedem* Zweig des Filters unerreichbar —
der Eigentümerzweig vergleicht gegen NULL, der Mitgliedschaftszweig schlüsselt
`circle_owner_id` darauf. Eine eigentümerlose Organisation wäre für niemanden
sichtbar. (Dieselbe Falle hat das Backfill-Skript aus P0 Nr. 8 zutage gefördert.)

**Die Folge, die diese Wahl mitbringt und die gebaut werden muss.** Der erste
Finder ist Eigentümer und könnte die Stufe später senken — dann verlieren die
anderen den Knoten, und ihre Relationen zeigen ins Leere. Eine willkürliche
Ersteintreffer-Eigentümerschaft darf nicht zur stillen Enteignung der anderen
werden. Deshalb:

> **Eine Organisations-Entität auf Stufe 2, an der Relationen anderer Nutzer
> hängen, kann nicht unter die Reichweite dieser Nutzer verengt werden.** Der
> Versuch wird abgelehnt und benennt, wie viele fremde Relationen betroffen
> wären — nicht stillschweigend ausgeführt und nicht stillschweigend verweigert.

Das ist neu und in keiner vorhandenen Regel enthalten. **Es braucht Deine
ausdrückliche Zustimmung**, weil es das erste Mal wäre, dass ein Eigentümer die
Stufe seiner eigenen Zeile nicht frei wählen darf.

### D-2 · Automatisch nur bei zusätzlichem Beleg, sonst Vorschlag

Exakter normalisierter Name **und** ein gemeinsames hartes Merkmal aus den
Dokumenten → automatische Zusammenführung. Sonst: Vorschlag in die Prüfliste, wie
jede andere Grauzone.

„Hartes Merkmal" heißt ein Schicht-A-Fakt, der beiden Seiten gemeinsam ist:
gleiche IBAN, gleiche Anschrift, gleiche Kundennummer-Domäne. Der Name allein
reicht nicht — „Stadtwerke" gibt es in jeder Stadt.

Ohne Fakten fällt alles auf die Prüfliste zurück. Das ist der ehrliche
Rückfall und keine Notlösung: eine Zusammenführung ohne Beleg ist genau der
Fehler, für den der Personen-Wächter gebaut wurde.

**Der Personen-Wächter bleibt unangetastet.** Er gilt weiter für jedes Paar, an
dem eine Person beteiligt ist — primär oder über `entity_types`. Die neue
Ausnahme gilt ausschließlich für `entity_type = organization`.

## Offen

- **(b) aus dem Issue: nur `organization`, oder auch `place` / `product`?**
  Nicht entschieden. Orte haben dieselbe Eigenschaft („Berlin" ist für alle
  dasselbe Berlin), Produkte auch. Vorschlag: mit `organization` anfangen, die
  anderen nachziehen, wenn sich das Belegkriterium bewährt hat — bei Orten fehlt
  das harte Merkmal, sie hätten also faktisch nur den Vorschlagsweg.
- **(d) aus dem Issue: Backfill der schon gespaltenen Knoten.** Dieselbe Regel
  rückwirkend, über ein Skript nach dem Muster von
  `bin/backfill_household_tiers.py`: Probelauf mit Zählwerten, Freigabe,
  Schreiblauf, Protokoll als Rückweg. **Der Rückweg wird durchlaufen, nicht
  behauptet** (`.claude/rules/migrations.md`).
- **Reihenfolge zum Cutover.** Das Zusammenführen erzeugt Stufe-2-Knoten. Vor dem
  Cutover ist das inert, danach sofort wirksam. Sauberer also: nach dem Cutover
  bauen, damit die Wirkung beobachtbar ist statt gleichzeitig mit zwanzig anderen
  Änderungen einzutreten.

## Abnahme

1. Zwei Dokumente derselben Organisation, eingegangen bei verschiedenen Nutzern,
   führen auf **einen** `kg_entities`-Knoten.
2. Die Relationen bleiben je Nutzer getrennt, und **kein Nutzer kann ableiten,
   dass ein anderer mit dieser Organisation zu tun hat** — geprüft über den
   Kanten-Filter, nicht nur über den Knoten.
3. Gleicher Name ohne gemeinsames Merkmal → **Vorschlag**, keine Zusammenführung.
4. Ein Paar mit einer Person darin wird nie zusammengeführt, auch nicht bei
   gleichem Namen und gemeinsamem Merkmal.
5. Die Verengung einer Organisation unter die Reichweite fremder Relationen wird
   abgelehnt und benennt die Zahl der betroffenen Relationen.
6. Backfill: Probelauf-Zählwerte = Schreiblauf-Zählwerte; Rundlauf durchlaufen.
