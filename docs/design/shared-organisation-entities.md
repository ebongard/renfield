# Basiswissen: Organisationen, Orte, Allgemeinbegriffe

Entwurf zu Issue #1314. Status: **entschieden, nicht gebaut.**

Der Anlass war eng — zwei Briefe derselben Organisation an zwei Familienmitglieder
ergeben zwei Knoten. Die Frage dahinter ist weiter: Organisationen, Länder, Städte
und Allgemeinbegriffe („Brot") sollten **niemandem gehören**, sondern als
Basisinformation im Haushalt existieren. Und weil der Haushalt bereits mit einer
zweiten Instanz verbunden ist und zwei weitere folgen (Eltern, Verein), ist die
Frage „wem gehört es" nicht mehr von der Frage „was verlässt das Haus" zu trennen.
Beides steht deshalb in EINEM Dokument.

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

### D-1 · Ein Haushalts-Konto ist der Eigentümer, nicht eine Person

Basis-Entitäten gehören einem Konto, hinter dem keine Person steht — wie das
Gerätekonto, aber für Wissen. Jedes Mitglied steht in dessen Kreis; die
Mitgliedschaftsrichtung dafür legt `bin/backfill_household_tiers.py` bereits an.

**Warum nicht „der erste Finder behält ihn"** (die erste Fassung dieses Entwurfs):
weil es keine Modellierung ist, sondern eine Verlegenheit. Wer zuerst Post bekam,
besäße die Organisation; senkte er später seine Stufe, verlören die anderen den
Knoten und ihre Relationen zeigten ins Leere. Man hätte einen Wächter gegen die
stille Enteignung bauen müssen — den ersten Fall im System, in dem ein Eigentümer
die Stufe seiner eigenen Zeile nicht frei wählt. Ein Haushalts-Konto braucht ihn
nicht: es gehört von vornherein keiner Person, also kann keine Person es
verengen. Schreiben bleibt eigentümergebunden, das heißt hier: nur der Admin
benennt um oder führt zusammen — dieselbe Form wie beim Raumverlauf, wo sie
funktioniert, und für Weltwissen die richtige (niemand soll „Brot" umbenennen
können).

**Eigentümerlos scheidet aus, und die Begründung ist genauer als zunächst
notiert.** Der öffentliche Zweig des Filters lautet `tabelle.circle_tier =
:asker_id_pub` und fragt NICHT nach dem Eigentümer — eine eigentümerlose Zeile
auf Stufe 4 wäre also durchaus lesbar. Sie scheitert an den anderen drei Zweigen
und an etwas Grundsätzlicherem: `atoms.owner_user_id` ist NOT NULL, ohne
Eigentümer gibt es also kein Atom, und ohne Atom keine Einzelfreigabe, kein
`AtomService.update_tier` und keinen Eintrag in der Kreis-Übersicht. Die Regel
„jede abrufbare Zeile trägt `circle_tier` UND `atom_id`" wäre gebrochen.

**Warum kein fünfter Filterzweig und keine eigene Dimension.** Ein Zweig
„Eigentümer IS NULL heißt Allgemeingut" drückt den Gedanken am ehrlichsten aus,
ändert aber das eine Stück SQL, das jeder Lesepfad teilt, und rehabilitiert die
eigentümerlose Zeile, die P0 Nr. 5 gerade verweigerbar gemacht hat. Eine eigene
Dimension neben `tier` wäre begrifflich am saubersten — „Basiswissen" ist ja
keine Reichweite —, aber der SQL-Filter kann nur `tier`; alles andere prüft
`PolicyEvaluator` Python-seitig nach. Das hieße, die Durchsetzung für eine ganze
Zeilenklasse aus dem SQL zu nehmen, **ausgerechnet auf dem Pfad, der anderen
Instanzen gegenübersteht** (siehe unten). Beides bleibt möglich: von einem
Haushalts-Konto kommt man später zu Zweig oder Dimension — umgekehrt nicht.

### D-1b · Zwei Klassen, und die Föderation ist der Grund

| Klasse | Beispiele | Stufe | Wirkung |
|---|---|---|---|
| **Weltwissen** | Brot, Berlin, Montag, Bundesland | **4** | jeder Peer sieht es ohne Zutun; keine Instanz legt es neu an |
| **Haushaltsspezifisch** | ein Versicherer, eine Schule, ein Arzt | **2** | verlässt das Haus nur zu einem Peer, den Du ausdrücklich auf diese Tiefe aufgenommen hast |

In einer Ein-Instanz-Welt wäre das kosmetisch. Mit vier Instanzen ist es die
Linie, an der entschieden wird, was das Haus verlässt — siehe den nächsten
Abschnitt.

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

## Der föderative Rahmen

Der Haushalt ist bereits mit der xidra-Instanz verbunden; eine Eltern- und eine
Vereins-Instanz kommen hinzu. Vier Instanzen, und damit hört „wem gehört es" auf,
eine hausinterne Frage zu sein.

**Was ein Peer sieht.** Für eine föderierte Abfrage (`peer_scoped`) fallen der
Eigentümer- und der Freigabezweig weg; es bleibt

```sql
tabelle.circle_tier = :asker_id_pub   OR   EXISTS (Mitgliedschaft)
```

Ein Peer sieht **Stufe 4 immer** und alles Engere nur dort, wo der lokale
Eigentümer ihn ausdrücklich auf diese Tiefe aufgenommen hat. Genau deshalb ist
D-1b keine Geschmacksfrage: die Stufe IST der Föderationsschalter.

**Und deshalb steht die Instanzfrage in diesem Dokument und nicht daneben.** Die
Stufenwahl ist bereits die Föderationsentscheidung; sie in ein eigenes Issue zu
heben, hieße die Begründung vom Beschluss zu trennen.

**Was hier trotzdem NICHT entschieden wird: die Entitäts-IDENTITÄT über
Instanzgrenzen.** Sind „Berlin" im Haushalt und „Berlin" bei den Eltern EIN
Knoten oder zwei, die sich gegenseitig sehen? Dieser Entwurf beantwortet nur das
Erste: was hinübergeht. Ob der hinübergehende Knoten *derselbe* ist — Auflösung,
Zusammenführung, wessen Schreibweise gewinnt, was eine entfernte Entitäts-ID
überhaupt bedeutet — ist ungleich größer und **blockiert das Erste nicht**: ein
Peer LIEST unser Berlin, er verschmilzt es nicht. `federation-identity-mapping.md`
bildet **Identitäten** ab; ob es auch **Entitäten** abbildet, ist offen.

**Eine Kante, die dabei herausfällt und die noch niemand durchdacht hat:** der
Verein ist beides. „Schiess-Sport-Verein 1966 Kleinenbroich e.V." ist eine
Organisation im Graphen des Haushalts **und** eine Peer-Instanz. Ein Knoten, der
zugleich ein Gegenüber ist. Was heißt es, wenn der Haushalt eine Entität für
einen Peer führt, der seinerseits eine Entität für den Haushalt führt? Ungelöst,
hier festgehalten, damit es beim Bau nicht überrascht.

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
- **Reihenfolge zum Cutover.** Das Zusammenführen erzeugt Stufe-2- und
  Stufe-4-Knoten. Vor dem Cutover ist das inert, danach sofort wirksam — und
  Stufe 4 ist zusätzlich sofort für jeden verbundenen Peer sichtbar. Also: nach
  dem Cutover bauen, damit die Wirkung beobachtbar ist statt gleichzeitig mit
  zwanzig anderen Änderungen einzutreten.
- **Die Grenze zwischen den beiden Klassen ist nicht immer scharf.** „Grundschule"
  ist Weltwissen, „die Grundschule am Ort" womöglich nicht. Vorschlag: im Zweifel
  Stufe 2 — die engere Seite —, und einzelne Begriffe hebt der Admin bewusst auf
  4. Eine Vorgabe, die im Zweifel weiter stellt, wäre die falsche Richtung.

## Abnahme

1. Zwei Dokumente derselben Organisation, eingegangen bei verschiedenen Nutzern,
   führen auf **einen** `kg_entities`-Knoten.
2. Die Relationen bleiben je Nutzer getrennt, und **kein Nutzer kann ableiten,
   dass ein anderer mit dieser Organisation zu tun hat** — geprüft über den
   Kanten-Filter, nicht nur über den Knoten.
3. Gleicher Name ohne gemeinsames Merkmal → **Vorschlag**, keine Zusammenführung.
4. Ein Paar mit einer Person darin wird nie zusammengeführt, auch nicht bei
   gleichem Namen und gemeinsamem Merkmal.
5. Eine Basis-Entität gehört dem Haushalts-Konto; keine Person kann sie
   umbenennen, zusammenführen oder verengen.
6. Ein Peer sieht Weltwissen (Stufe 4) ohne Zutun und haushaltsspezifische
   Organisationen (Stufe 2) NUR, wenn er ausdrücklich auf diese Tiefe aufgenommen
   wurde — geprüft mit einer echten `peer_scoped`-Abfrage, nicht nur am Knoten.
7. Backfill: Probelauf-Zählwerte = Schreiblauf-Zählwerte; Rundlauf durchlaufen.
