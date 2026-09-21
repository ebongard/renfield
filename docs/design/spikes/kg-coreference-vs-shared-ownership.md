# Spike: `sameAs`-Co-Referenz vs. Shared-Ownership für die nutzerübergreifende KG-Kanonisierung

**Issue:** [#876](https://github.com/ebongard/renfield/issues/876) · Entwurf: [`docs/design/kg-cross-user-canonicalization.md`](../kg-cross-user-canonicalization.md) §14
**Status:** Spike abgeschlossen 2026-09-21 — **Empfehlung: Co-Referenz (`sameAs`-Kante). Shared-Ownership (Phase A–D des Entwurfs) nicht bauen.** Für den Linker gibt es **kein Go**; er braucht ein ausdrückliches (§8).
**Datum:** 2026-09-21
**Autor:** Claude Fable 5.1
**Datengrundlage:** Eigentümer-Messung auf der auth-on-Instanz, Kommentar auf #876 vom 2026-09-21 (pseudonymisiert); Code-Fakten gegen `main` `4a29f632` bzw. den Branch `fix/kg-reconciler-person-guard-typo` (PR #1301) für `kg_reconciler_service.py`. Faktencheck gegen Code und Issue am 2026-09-21 (Zeilenangaben danach berichtigt).

---

## 0. Warum dieser Spike

Das Eng-Review vom 2026-09-02 hat den Bau von #876 **umgeleitet**: bevor `circle_sql` — nach eigener Aussage des Entwurfs der riskanteste Eingriff im Code (§12) — für Shared-Ownership angefasst wird, muss ein datengestützter Vergleich zeigen, ob eine `sameAs`-Co-Referenzkante das eigentliche Ziel („Alice und Bob lösen dieselbe Erika auf“) nicht ohne Eigentümerwechsel, ohne neue SQL-Verzweigung, ohne Rückbau-Risiko und ohne neue Leckfläche erreicht (§14.1). Zwei Tatsachen machten den Spike dringlich: **kein Live-Verbraucher braucht Shared-Ownership** (Haushalt ist `AUTH_ENABLED=false`, die auth-on-Instanz ist ein Geschäftsbetrieb ohne „Familie“), und der Vergleich sollte in **≤ 1 Design-Zyklus** Phase A–D vollständig ersetzen können.

Die Triage vom 2026-09-20 hat #876 deshalb geparkt (BL-0302). Am 2026-09-21 hat der Eigentümer aus der auth-on-Instanz **den fehlenden Spike-Input geliefert** — aus einer Richtung, die der Entwurf nicht vorgesehen hatte.

## 1. Die Felddaten (auth-on, vier Monate organisches Wachstum)

`kg_entities` hält 99 Zeilen mit `entity_type='person'`. **Eine natürliche Person belegt 13 davon**, verteilt auf **acht Eigentümer-Kennbuchstaben** (der Kommentar spricht von sieben Eigentümern; die Tabelle weist A–H aus — die Abweichung ist vom Eigentümer zu klären, für den Vergleich ist sie unerheblich) und **3 Schreibweisen**:

| Eigentümer | Schreibweise | `mention_count` |
|---|---|---:|
| A | `Firstname von der Lastname` | 1 |
| B | `Firstname von der Lastname` | 10 |
| C | `Firstname von der Lastname` | 294 |
| C | `Firstname von der Lastnrame` *(Buchstabendreher im letzten Token)* | 14 |
| D | `Firstname von der Lastname` | 35 |
| D | `Firstname von der Lastnrame` | 41 |
| D | `Firstname` | 1 |
| E | `Firstname von der Lastnrame` | 5 |
| F | `Firstname von der Lastnrame` | 2 |
| G | `Firstname von der Lastnrame` | 2 |
| H | `Firstname von der Lastname` | 3 085 |
| H | `Firstname von der Lastnrame` | 134 |
| H | `Firstname` | 3 |

Form, nicht Inhalt: ein viergliedriger Personenname, eine Variante mit Buchstabendreher *innerhalb* des letzten Tokens, eine Variante nur Vorname. `surface_forms` ist `[]` und `canonical_id` `NULL` auf **allen 99** Personenzeilen — keiner der beiden Konsolidierungsmechanismen (Resolve-Kaskade, Reconciler) hat auf dieser Instanz je gegriffen.

**Ein Widerspruch, offen benannt:** der Kommentar sagt, `kg_reconciler_enabled` sei auf der auth-on-Instanz *bewusst aus* gelassen worden („measured no-op“), nur der Konflations-Tripwire sei an. Eine Live-Lesung der ConfigMap am 2026-09-21 zeigte dagegen `KG_RECONCILER_ENABLED=true` auf beiden Instanzen. Welcher Stand gilt, ist vom Eigentümer zu klären (§8 Schritt 2 hängt daran); für die Analyse zählt, dass der Reconciler den Tippfehler-Fall in beiden Zuständen nicht erreichte.

Drei Beobachtungen strukturieren den Vergleich:

1. **Zwei getrennte Probleme in einer Tabelle.** *Innerhalb* eines Eigentümers (C, D, H) sind es Tippfehler-Dubletten — Sache des Reconcilers, nicht von #876. *Zwischen* Eigentümern sind es korrekte, absichtlich getrennte Knoten desselben Referenten — das ist #876.
2. **Der Reconciler war für den Tippfehler-Fall blind.** `_names_related` (Token-Teilmenge) erkennt „Lastname“/„Lastnrame“ nicht; der Person-Guard verwarf das Paar; der Konflations-Tripwire schließt Personen aus. Trockenübung über den ganzen Graphen bei 0,85: **7 Paare**, alle Personen — **6 Paare aus 4 nummerierten Testkonten** (verschiedene Identitäten) und **1** echtes Tippfehler-Paar (Eigentümer H, 0,9011). → **Behoben, unabhängig von Circles** (PR #1301): Tippfehler-Paare (ein Edit innerhalb eines Tokens, beide Schreibweisen ≥ 4 Zeichen) werden als Prüfvorschlag `name_typo` aufgeworfen, nie automatisch zusammengeführt; Ordnungszahl-Paare bleiben verworfen; abgelehnte Paare kehren nicht zurück.
3. **Der Bedarf ist nicht Retrieval-Qualität.** Er ist — nach Einschätzung des Eigentümers, der die Instanz verantwortet — **Auffindbarkeit je Betroffenem** (Art. 15/16/17 DSGVO): eine Auskunfts- oder Löschanfrage muss alles zu einer Person aufzählen. Heute heißt das 13 Zeilen unter 3 Schreibweisen bei acht Kennbuchstaben finden — ohne `surface_forms`, ohne `canonical_id`. Es gibt keine Abfrage, die verlässlich „was haltet ihr über diese Person“ beantwortet (§3.7). Das ist dasselbe Primitiv wie #876, nur aus anderer Richtung.

## 2. Die fünfte Dimension, die §14.1 nicht wiegt

§14.1 vergleicht `sameAs` und Shared-Ownership auf Retrieval-Qualität, Leckfläche, Rückbau und Wechselwirkung mit #875/#877. Für eine auth-on-**Geschäfts**instanz kommt — so der Eigentümer — eine Dimension hinzu, und sie trennt die Optionen scharf:

| | Zusammenführung in einen Knoten | `sameAs`-Co-Referenz |
|---|---|---|
| Graph behauptet einen Referenten | ja | ja |
| Auffindbarkeit Art. 15/17 | ja | ja |
| `mention_count` | wird ein **org-weites** Aggregat | bleibt je Eigentümer |
| neue Leckfläche | `circle_sql` (§12: riskantester Eingriff) | keine an den Link-Zeilen (§3.2, §3.5) |

Die dritte Zeile entscheidet. Nach **BetrVG § 87 Abs. 1 Nr. 6** besteht ein Mitbestimmungsrecht des Betriebsrats — sofern einer besteht — bei technischen Einrichtungen, die *dazu bestimmt* sind, Verhalten oder Leistung von Beschäftigten zu überwachen; die Rechtsprechung des BAG liest das als *objektiv geeignet*. „Diese namentlich benannte Person wurde org-weit 3 085-mal genannt, zuerst am …, zuletzt heute“ ist ein solches Aggregat — und die Zusammenführung **erzeugt** es als Nebenwirkung einer Korrektheitsreparatur. Der Code bestätigt, dass es kein theoretisches Aggregat wäre: `mention_count` ist ein roher Skalar **auf** der Zeile, den der Circle-Filter nie schwärzt (`knowledge_graph_service.py:935` summiert ihn beim Merge; sichtbar in der Review-Karte `api/routes/knowledge_graph.py:510`, im Entitätsdetail, in der Wissen-3D-Szene als Feld `wissensbasis.py:53,100` und als „importance“ `kg_graph_service.py:364`). Wer den zusammengeführten Knoten sehen darf, sieht die Summe. Eine Co-Referenzkante liefert dieselbe Korrektheit und lässt jede Zählung bei ihrem Eigentümer: **die Identität wird geteilt, das Zählen nicht.**

Auf dieser Instanz ist die billigere Option nach dieser Einschätzung also auch die rechtlich unproblematischere; die teure trägt zusätzlich einen Mitbestimmungspreis, den der Entwurf bisher nicht ansetzt. Dies ist eine Design-Abwägung, keine Rechtsberatung. Sollte Shared-Ownership dennoch je gewinnen, braucht das Aggregat je Knoten eine ausdrückliche Entscheidung — plausibel: Zählungen leben auf der Kante (`kg_relations.stated_by_user_id` existiert bereits), werden nie auf einem geteilten Knoten summiert.

## 3. Vergleich entlang der §14.1-Kriterien — mit Code-Fakten

Alle Pfade relativ zu `src/backend/`.

### 3.1 Was heute existiert — und was nicht

- **Keine Co-Referenz im Code.** Ein Grep nach `same_as|sameas|coref|alias_of|linked_entity|cross_owner` über `src/backend/` und `docs/` trifft nur die Prosa des Entwurfs. `kg_entities.external_id` (#877) ist Spalte ohne Leser oder Schreiber (`models/database.py:2116-2118`; `merge_entities` fasst es beim Absorbieren nicht an, `knowledge_graph_service.py:930-945`).
- **`canonical_id` taugt nicht als Link.** Es bedeutet „ich bin tot“: `merge_entities` setzt `is_active=False` und `canonical_id=<winner>` zusammen (`:948-950`); `canonical_id IS NULL` ist Lesefilter an ≥ 10 Stellen (Reconciler, Resolve, `graph_expansion.py:68`, `note_links.py:140`, `conversation_memory_service.py:814`, Bridge-Backfill, Konflations-Monitor); `ondelete="SET NULL"` würde beim Löschen des Überlebenden den Tombstone stillschweigend wiederbeleben. Ein `sameAs` muss „wir existieren beide“ bedeuten und symmetrisch sein.
- **`kg_relations.predicate` ist freier Text** (`String(100)`, kein Enum, keine Allowlist — anders als `entity_type`, das an `knowledge_graph_service.py:482` gegen `KG_ENTITY_TYPES` gegated wird). Ein reserviertes Prädikat `sameAs` braucht ein Schreibtor, das es nicht gibt; sonst prägt der Extraktor aus Alltagschat ein kollidierendes „ist dasselbe wie“.
- **Keine Eindeutigkeit auf `(subject_id, predicate, object_id)`**; `save_relation` dedupliziert per SELECT-then-INSERT (`:794-803`), `merge_entities` trägt einen Nach-Dedup (`:968-980`), weil Duplikate entstehen. Link-Zeilen würden unter Nebenläufigkeit doppelt.

### 3.2 Leckfläche (§14.1: „zero new leak surface“) — bestätigt, mit einer Randbedingung

- Der Filter (`circle_sql.py:37-197`) hat vier OR-Zweige: Eigentümer-Gleichheit (`:124`), `public`, expliziter Grant, Tier-Reichweite. Er wird auf Entitäten **und** Relationen angewandt: `kg_retrieval.py:258-267,499-508,199-202`, `graph_expansion.py:41-55,77-100`, `kg_graph_service.py:245-251`.
- `expand_fused` (`graph_expansion.py:151-246`) ist generisch über `kg_relations`: die Traversal-Abfrage filtert die Kante selbst (`:196-200`), der ferne Endpunkt wird je Hop erneut gefiltert (`:209`) und **verworfen**, nicht bloß verborgen; `_edges_within` (`:103-118`) nennt eine Kante nur, wenn *beide* Endpunkte erreichbar sind (`:240-241`). **Eine `sameAs`-Kante, die als gewöhnliche `kg_relations`-Zeile über `save_relation` entsteht, erbt alle drei Tore kostenlos.** Eine eigene Tabelle müsste sie neu herleiten.
- **Die Randbedingung:** `save_relation` berechnet `circle_tier = MIN(subject, object)` (`:810-816`) — tier-only, eigentümerblind. Die Kante trägt genau ein `user_id`; der Eigentümerzweig des Filters ist `r.user_id = :asker`. Eine Kante zwischen Alices Tier-0-Knoten und Bobs Tier-0-Knoten sieht also **einer von beiden**, nicht beide. Symmetrie braucht **zwei Zeilen (eine je Eigentümer)** — und zwar **mit** der `MIN()`-Regel: bei Tier 0 beider Endpunkte sieht jeder Eigentümer genau seine Zeile über den Eigentümerzweig, Dritte sehen keine. Wer stattdessen der Link-Zeile das Tier des *eigenen* Knotens gäbe, öffnete den Tier-Reichweite-Zweig: ein Haushaltsmitglied von Alice (Tier 2) erreichte Alices Link-Zeile, während Bobs Endpunkt gefiltert bleibt — die Relation erschiene als `Erika same_as ?` (Endpunktname über `_resolve_entity_names` → „?“), also als Strukturpreisgabe, vor der `graph_expansion.py:191-193` ausdrücklich warnt. Deshalb: `MIN()` beibehalten, zwei Zeilen, und der Leck-Test (§8) muss **Kanten** prüfen, nicht nur Knoten.
- `merge_entities` verweigert nutzerübergreifend (`:914-921`, gepinnt durch `test_kg_merge_pg.py:225-234`). Ein Linker geht **nicht** durch `merge_entities` — richtig so.

**Fazit 3.2:** kein neuer SQL-Zweig, kein Eingriff in `circle_sql`, Leck-Eigenschaften erben. Das §14.1-Versprechen hält für die Link-Zeilen, sofern sie je Eigentümer eine `kg_relations`-Zeile mit `MIN()`-Tier sind. Zwei Stellen liegen *außerhalb* dieser Zusage und werden in §3.5 und §3.7 benannt.

### 3.3 Retrieval-Qualität (§14.1: „answers *was weiß ich über Erika* as well as a merged node?“) — der ehrliche Preis

Vorab die Grenze: die Union erweitert nie die Reichweite. Ein über `sameAs` erreichter Knoten wird nur mitgeführt, wenn der Asker ihn über Circles **ohnehin** sehen dürfte — das Per-Hop-Tor (`graph_expansion.py:209`) verwirft ihn sonst. Der Link ändert, *dass* der Asker den bereits sichtbaren Knoten als dieselbe Person erkennt, nicht *ob* er ihn sieht.

- **Resolve-Kaskade.** `resolve_entity` filtert dreimal `user_id == asker OR NULL` (`knowledge_graph_service.py:508,527,717-719`). Ein „folge `sameAs` zum Knoten eines anderen Eigentümers“-Schritt gehört **zwischen Stufe 2 (Surface-Form) und Stufe 3 (Embedding)** — denn Stufe 3 wird für Personen unbedingt übersprungen (`embed_match = use_embedding and not is_person`, `:598`, Begründung `:586-597`: der 127-Nennungen-Magnetknoten). Ein Link-Schritt *nach* Stufe 3 wäre für genau die Klasse unerreichbar, für die #876 existiert. Nebenbefund zugunsten der Co-Referenz: der Pro-Nutzer-Deckel (`kg_max_entities_per_user`, `:609-620`) zählt nur eigene Knoten — verlinkte Knoten des anderen Eigentümers blähen ihn nicht auf.
- **Union an vier Stellen je Methode, dreifach dupliziert.** `get_relevant_context` und `get_relevant_atoms` sind erklärte Beinahe-Duplikate (`kg_retrieval.py:469-473`, „TODO: DRY“), dazu `kg_graph_service`. Je Methode: Saat-kNN (`:298-318`/`:524-544`, `LIMIT 10` je Suchtext **vor** jeder Union — der verlinkte Knoten konkurriert um dieselben zehn Plätze), Relationsabruf (`LIMIT :max_triples` = 15, `config.py:943`, ebenfalls vor der Union — fremde Relationen verdrängen eigene), Endpunkt-Namensauflösung (`:363-365`/`:574-576`), Expansions-Pivots (`:394-413`). **Eine naive Union hungert die eigenen Fakten aus, statt hinzuzufügen** — die Budgets müssen je Seite gelten oder erweitert werden.
- **Hop-Budget.** `graph_expansion_max_hops = 2` (`config.py:947`, in keiner ConfigMap überschrieben). Der Link kostet einen Hop; die Fakten des verlinkten Knotens kommen bei Hop 2 mit Abklingen `pivot/3` (`graph_expansion.py:212`) statt Hop 1 / `pivot/2` bei einem zusammengeführten Knoten. **Das ist der messbare Qualitätsunterschied, den §14.1 einfordert** — konstruktiv adressierbar: den `sameAs`-Hop im BFS nicht zählen (Hop-0-Äquivalenz der verlinkten Knoten, weiterhin hinter dem Per-Hop-Tor) statt `max_hops` zu erhöhen.

**Fazit 3.3:** Co-Referenz ist **nicht** kostenlos für Retrieval — sie verlangt eine Union an bekannten, aufgezählten Stellen und eine Hop-Regel. Shared-Ownership hätte denselben Union-Bedarf (OV-6 des Entwurfs: „shared-Erika + Alice-private-Erika + Bob-private-Erika gleichzeitig“) **plus** Phase A–D. Der Preis ist also nicht spezifisch für Co-Referenz; er ist der Preis des Problems.

### 3.4 Rückbau (§14.1: „zero rollback hazard“) — bestätigt

Link-Zeilen löschen = Zustand vorher. Kein Eigentümer hat gewechselt, kein Atom ist umgezogen, kein Filterzweig ist tot geworden. Gegenüber OV-5 des Entwurfs (Shared-Ownership: „Rückbau = dokumentierte Un-Share-Datenmigration, nicht `alembic downgrade`“, **HIGH**) ist das der größte einzelne Unterschied.

### 3.5 Review-Primitiv — der eine echte Neubau, und eine bewusste Preisgabe

`KgMergeProposal.user_id` ist **ein** Integer (`models/database.py:2211`); `_owned_pending_proposal` (`api/routes/knowledge_graph.py:527-538`) autorisiert genau einen Eigentümer; `find_duplicate_pairs` erzeugt nie eigentümerübergreifende Paare (`a.user_id = b.user_id`, `WHERE a.user_id = :uid`). **Ein Vorschlag, der den Knoten eines anderen Eigentümers berührt, hat heute keine Darstellung** — ein Eigentümer würde über den Knoten eines anderen entscheiden. Ein Linker braucht ein **Zwei-Parteien-Review**: beide Eigentümer bestätigen, oder ein Admin mit Rolle. Das ist der eine Baustein, den die Co-Referenz *nicht* geschenkt bekommt — er ist aber klein gegenüber Phase A (Circle-Eigentum + fünfter Filterzweig + Un-Share-Migration).

Offen benannt: **der Vorschlag selbst ist eine Preisgabe.** Wer Alice fragt „ist Deine Erika dieselbe wie Bobs?“, teilt ihr mit, dass Bob eine (nahezu) gleichnamige Person führt — Name und Existenz eines fremden, möglicherweise privaten Knotens. Für den Geschäftsfall mit Admin-Bestätigung ist das hinnehmbar; für ein Zwei-Parteien-Modell unter Privatpersonen ist es eine Entscheidung (E-2), keine Selbstverständlichkeit.

Die Namens-Hürde gilt auch hier: Embedding-Ähnlichkeit allein trennt zwei Personen nicht (gemessen 0,894/0,863 zwischen verschiedenen Personen, `kg_reconciler_service.py:112-114`); ein Link-Vorschlag braucht mindestens `_names_related`- oder `_names_near_typo`-Evidenz.

### 3.6 Wechselwirkung #875 / #877 (Entwurf §10, gegen den Code geprüft)

- **#875 (bi-temporale Kanten):** unberührt. Ein `sameAs` ist keine Faktenkante mit Gültigkeitsintervall, sondern eine Identitätsbehauptung; sie kollabiert keine Tripel, also kein Dedup-Schlüssel-Konflikt.
- **#877 (`external_id`-Linker):** §14.1 erwog, #877 vor #876 zu ziehen. Der Code zeigt: `external_id` ist inert und indiziert; ein Linker, der auf gemeinsame `external_id` schlüsselt, **ist** #877 (braucht die Wikidata/GND-Pipeline). Ein Linker auf Namens- + Embedding-Evidenz braucht keine externe Pipeline, nutzt aber den Index nicht. Für den Geschäftsfall (Mitarbeitende, Ansprechpartner) gibt es keine externe Autorität — #877 hilft dort nicht. **Reihenfolge: #876-Co-Referenz zuerst, #877 bleibt unabhängig.** Nebenbefund: `merge_entities` verwirft beim Absorbieren die `external_id` des Verlierers stillschweigend (`:930-945`) — ein Vorgriff, den #877 selbst lösen muss.

### 3.7 Löschpfade und Betroffenenauskunft — für beide Optionen ungelöst, aber ungleich schwer

Es gibt **keinen** Betroffenen-Export und keinen Löschpfad für `kg_entities`/`kg_relations`: die einzigen `/export`-Routen sind ICS-Obligationen und Trajectories; der einzige Art.-17-Löschmechanismus ist `atom_purge_service` (atom-scoped, kaskadiert über `atom_id`). Nutzerlöschung (`api/routes/users.py:502-548`) hat **keine** Beziehung zu KG-Zeilen; `kg_entities.user_id`, `kg_relations.user_id/stated_by_user_id/subject_id/object_id` tragen **kein** `ondelete` — eine Nutzerlöschung mit KG-Besitz dürfte an einer FK-Verletzung scheitern (abgeleitet, nicht ausgeführt).

Der Unterschied ist gerichtet: einen **zusammengeführten** Knoten zu löschen zerstört die Fakten beider Eigentümer in einer Anweisung, ohne Aufzeichnung, welche Hälfte wem gehörte; eine Seite eines **`sameAs`-Paares** zu löschen bleibt auf diesen Eigentümer beschränkt und hinterlässt einen hängenden Link — den ein `ondelete="CASCADE"` auf der Link-Zeile beseitigt.

**Auffindbarkeit (Art. 15) ist mit Co-Referenz möglich, aber nicht geschenkt.** `circle_sql` hat genau vier Zweige und **keinen Admin-Umgehungspfad**; ein Admin auf einer auth-on-Instanz wird gefiltert wie jeder andere. „Zeige alles zu Person X über alle Eigentümer“ ist deshalb ein **neuer, berechtigungsgeschützter, eigentümerübergreifender Lesepfad** an `circle_sql` vorbei — eine privilegierte Lesefläche, die es heute nicht gibt und die als Bauteil zählt (§5.6), nicht nur als Entscheidung. Der Unterschied zur Zusammenführung bleibt: die Knoten werden dafür nie vermischt, und der Pfad ist ein Report, kein Retrieval.

### 3.8 Wo es sich überhaupt prüfen lässt

`AUTH_ENABLED: "false"` macht auf dem Haushalt jeden Circle-Filter zum Leerstring (`kg_retrieval.py:246,487,193`; `graph_expansion.py:49,94`; `kg_graph_service.py:134,203,245`). Leck-Eigenschaften eines Linkers sind **nur auf der auth-on-Instanz oder in `.159`-Tests mit auth-on-Fixture** prüfbar — OV-8 des Entwurfs, im Code bestätigt. Der Haushalt hat weiterhin keinen Verbraucher; ein Linker ist zunächst ein Feature der auth-on-Instanz, dunkel im Haushalt.

## 4. Vergleichstabelle

| Kriterium | Shared-Ownership (Phase A–D) | `sameAs`-Co-Referenz |
|---|---|---|
| Eingriff in `circle_sql` | fünfter Zweig, alle 6 Verbraucher (§14.2 Nr. 3) | keiner |
| Neue Leckfläche | R1 Tier-Selektor, OV-1 Misch-Eigentümer-Kanten | keine an den Link-Zeilen (zwei Zeilen, `MIN()`); benannt: Vorschlag als Preisgabe (§3.5), Art.-15-Report als privilegierter Lesepfad (§3.7) |
| Rückbau | Un-Share-Datenmigration (OV-5, HIGH) | Link-Zeilen löschen |
| Retrieval-Union | nötig (OV-6) | nötig (§3.3), an denselben Stellen; erweitert nie die Reichweite |
| Hop-Budget | Fakten bei Hop 1 | Hop 2 ohne Sonderregel; Hop-0-Äquivalenz hinter dem Per-Hop-Tor löst es |
| Review-Primitiv | Circle-Admin-Rollen (O-2), Phase D | Zwei-Parteien-Vorschlag (neu, klein) |
| `mention_count`/Zeitstempel | org-weites Aggregat → § 87 BetrVG (Einschätzung des Eigentümers) | bleibt je Eigentümer |
| Art. 15 Auffindbarkeit | ja | ja, über einen neuen berechtigungsgeschützten Report (§3.7) |
| Art. 17 Löschung | zerstört beide Hälften | je Eigentümer, Link kaskadiert |
| Mehrfach-Haushalt / Mandant (O-1) | `conversations.scoped_circle_id` + UI in Phase (OV-4) | entfällt: Links sind paarweise, kein Eigentümerobjekt |
| Voraussetzungen | named circles v2, D-A/D-C | keine Modelländerung |
| Aufwand (Mensch / CC+gstack) | mehrere Wochen / mehrere Tage | ~1 Woche / ~½ Tag Kern + Union + Review + Report |

## 5. Empfehlung

**Co-Referenz.** Phase A–D des Entwurfs nicht bauen; named-circles v2 bleibt geparkt, bis ein anderer Bedarf es trägt. Der Entwurf bleibt als Dokumentation des verworfenen Pfads bestehen; §14.5 verweist hierher.

Umriss des Linkers (Design, kein Bauauftrag):

1. **Repräsentation:** je Paar **zwei** `kg_relations`-Zeilen mit reserviertem Prädikat (Arbeitsname `same_as`), je eine mit `user_id` = Eigentümer der Subjektseite, `circle_tier` **nach der bestehenden `MIN()`-Regel** (§3.2 — nicht das Tier des eigenen Knotens), `stated_by_user_id` = wer bestätigt hat. Schreibtor: das Prädikat ist für den Extraktor gesperrt (Allowlist-Prüfung in `save_relation`, heute nicht vorhanden — §3.1), nur der Linker schreibt es.
2. **Vorschlagsweg:** ein neuer Vorschlagstyp „Identität“ in `kg_merge_proposals` oder eigener Tabelle mit **zwei** Eigentümer-Spalten und Zwei-Parteien-Status (`pending_a`, `pending_b`, `linked`, `rejected`); Kandidaten aus einem eigentümerübergreifenden Selbst-Join mit derselben Namens-Evidenz wie der Reconciler (`_names_related` oder `_names_near_typo`), Personen nur mit Namens-Evidenz. Ein Admin (Rolle) darf allein bestätigen — Geschäftsfall; die Preisgabe aus §3.5 ist Teil von E-2.
3. **Resolve:** neuer Schritt zwischen Surface-Form und Embedding: eigener Treffer → `same_as`-Nachbarn (gefiltert) mitführen, **nicht** in den fremden Knoten schreiben. Schreiben bleibt immer im eigenen Knoten.
4. **Retrieval:** Union je Seite mit eigenem Budget (Saat-kNN und Tripel-Limit je Knotenmenge), Endpunkt-Namen wie bisher gefiltert; im BFS zählt ein `same_as`-Hop nicht (Hop-0-Äquivalenz), das Per-Hop-Tor bleibt — die Union macht keinen Knoten sichtbar, den Circles verbergen.
5. **Zählungen:** nie summieren. Anzeigen je Eigentümer; die Review-Karte zeigt beide Seiten getrennt.
6. **Betroffenenauskunft und Löschung:** `ondelete="CASCADE"` auf den Link-Zeilen über die Endpunkte; ein **Admin-Report „alles zu Person X“** als eigener, berechtigungsgeschützter Lesepfad an `circle_sql` vorbei (§3.7) — Bauteil, nicht Nebenprodukt; traversiert `same_as` über alle Eigentümer, ohne die Knoten zu vermischen.
7. **Flag:** `KG_COREFERENCE_ENABLED=false`; aus → byte-identisch (kein Prädikat geschrieben, kein Resolve-Schritt, keine Union, kein Report). Zuerst die auth-on-Instanz.

## 6. Was Co-Referenz NICHT löst — ehrlich

- **Keine geteilten Fakten.** „Erika wohnt in Köln“ von Alice bleibt Alices Kante; Bob sieht sie nur, wenn er sie über Circles ohnehin sehen dürfte. Wer geteiltes *Wissen* will (nicht nur geteilte *Identität*), braucht weiterhin Tier-Promotion (Entwurf §5.3) — das ist heute schon möglich und bleibt der Weg.
- **Kein Ende der Tippfehler-Dubletten je Eigentümer.** Das erledigt der Reconciler mit dem `name_typo`-Fix; der Linker verbindet nur Eigentümer.
- **Transitivität.** A~B und B~C impliziert A~C; ob der Linker transitiv schließt oder nur paarweise verlinkt, ist eine Entscheidung (E-3).
- **Haushalt ohne Verbraucher.** Solange der Haushalt auth-off läuft, ist der Linker dort tot; das ist gewollt, aber es heißt: keine Haushalts-Evidenz für den Nutzen.

## 7. Offene Entscheidungen für den Eigentümer

- **E-1 Go für den Linker** (nach §5) — jetzt, oder nach dem auth-on-Entwurf (BL-0400), der ohnehin Sichtbarkeitsstufen für Unterhaltungen und Subsume mehrnutzerfähig neu ordnet?
- **E-2 Bestätigungsautorität und Preisgabe:** beide Eigentümer (dann sieht jeder, dass der andere die Person führt — §3.5), Admin allein (Geschäftsfall), oder beides je Instanz-Typ?
- **E-3 Transitivität:** paarweise Links oder transitiver Identitätscluster (Union-Find) mit Cluster-ID?
- **E-4 Art.-15-Report:** als berechtigungsgeschützte Admin-Route Teil des Linkers, oder eigener Posten? Empfehlung: Teil des Linkers, denn er ist der eigentliche Bedarf — und er ist eine privilegierte Lesefläche, die ein eigenes Sicherheits-Review verdient.
- **E-5 Reihenfolge zu #877:** unabhängig lassen (Empfehlung) oder `external_id` als weiteres Link-Signal vorsehen?
- **E-6 Faktenklärung:** Eigentümerzahl (7 oder 8) und Reconciler-Zustand auf der auth-on-Instanz (Kommentar: aus; Live-Lesung: an).

## 8. Nächste Schritte

1. Dieses Dokument mergen; Entwurf §14.5 verweist hierher (dieser PR).
2. PR #1301 deployen. Falls der Reconciler auf der auth-on-Instanz tatsächlich aus ist (E-6): einschalten. Dann die Review-Queue dort zwei Läufe beobachten: erwartet ist **mindestens das H-Paar** als `name_typo`-Vorschlag (0,9011 in der Trockenübung); die Paare von C und D nur, falls sie nach dem Embedding-Backfill ≥ 0,85 liegen — die Trockenübung fand sie nicht.
3. Eigentümer entscheidet E-1 bis E-6 (Option-Picker, kein Freitext).
4. Bei Go: Spezifikation aus §5 in ein Design-Dokument `docs/design/kg-coreference-linker.md` überführen (Schema, Tests auf `.159` mit auth-on-Fixture; Leck-Eigenschaftstest über **Knoten und Kanten**: ein Link darf weder einen Knoten noch eine Relation sichtbar machen, die der Filter ohne Link verbirgt — auch nicht als `same_as ?`), dann `/plan-eng-review`; der Art.-15-Report bekommt ein eigenes Sicherheits-Review.
5. Messung nach Rollout auf der auth-on-Instanz: Anzahl verlinkter Personen, Anteil der Art.-15-Abfragen, die vollständig sind (13/13 Zeilen für die gemessene Person).
