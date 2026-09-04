# Bi-temporale KG-Kanten — Gültigkeitsintervalle auf `kg_relations`

**Issue:** [#875](https://github.com/ebongard/renfield/issues/875)
**Status:** Entwurf zur Review — **kein Go zum Bauen**
**Datum:** 2026-09-04
**Autor:** Claude Opus 5
**Plan-Bezug:** `tasks/structured-memory-plan.md` §Phase 5 (zurückgestellt)

---

## 1. Das Problem, gegen den echten Code geprüft

Die Ausgangsformulierung des Issues lautet: eine widersprochene Kante soll *expired* statt
*deleted* werden. Beim Nachlesen im Code stellt sich heraus, dass **heute weder das eine noch
das andere passiert**.

`KnowledgeGraphService.save_relation` (`knowledge_graph_service.py:778-848`) dedupliziert auf
`(subject_id, predicate, object_id)` — also auf ein **identisches Tripel**:

```python
query = select(KGRelation).where(
    KGRelation.subject_id == subject_id,
    KGRelation.predicate == predicate,
    KGRelation.object_id == object_id,
    KGRelation.is_active == True,
)
...
if existing:
    existing.confidence = max(existing.confidence or 0, confidence)
    return existing
```

„Jutta wohnt in Bonn" und „Jutta wohnt in Berlin" haben **verschiedene `object_id`**. Der
zweite Satz trifft den Dedup-Zweig nicht, sondern legt eine zweite Kante an. Beide bleiben
`is_active = true`.

**Der heutige Zustand ist also nicht „überschreiben" und nicht „löschen", sondern
stillschweigendes Anhäufen widersprüchlicher Kanten.** Das Retrieval liefert anschließend
beide, ohne Rangfolge und ohne Hinweis darauf, dass sie einander ausschließen.

Es gibt im gesamten KG **keine Widerspruchserkennung** — `grep -rn "contradict"` über
`knowledge_graph_service.py` und `kg_*.py` liefert null Treffer. Sie existiert ausschließlich im
flachen Memory (`conversation_memory_service.py:627`, `settings.memory_contradiction_resolution`).

### 1.1 Was daraus folgt

Das Issue beschreibt eine Verbesserung („expire statt delete"), tatsächlich fehlen aber **zwei**
Dinge, und sie sind unterschiedlich schwer:

| | Was fehlt | Schwierigkeit |
|---|---|---|
| **A** | Ein Ort, an dem Gültigkeit *steht* (Schema + Filter) | mechanisch, gut testbar |
| **B** | Ein Mechanismus, der *erkennt*, dass B das A widerspricht | LLM-Urteil, fehleranfällig |

Ohne B bleibt A wirkungslos: Ein Gültigkeitsintervall, das nie jemand schließt, ist eine Spalte
mit `NULL`. Ohne A ist B nicht darstellbar. **Beide gehören in denselben Umfang** — das ist die
erste Aussage dieses Dokuments, und sie weitet den Umfang gegenüber der Issue-Beschreibung.

## 2. Warum es sich lohnt

Der Nutzen ist nicht „Historie sammeln" — das klingt nach Archiv und wäre schwach. Er ist
**Korrektheit der Gegenwart**:

- Heute kann der Agent auf „Wo wohnt Jutta?" beide Antworten im Kontext haben und würfelt
  faktisch. Mit Gültigkeit gewinnt die aktuelle Kante deterministisch.
- „Was wusste ich im März über X" wird beantwortbar — das ist der Zusatznutzen, nicht der
  Hauptzweck.
- Der Reconciler und `graph_expansion` breiten heute veraltete Kanten mit aus; jeder Hop
  vervielfacht den Fehler.

## 3. Abgrenzung — zwei Verwechslungen, die teuer wären

**Nicht `wb_*`.** Die Migration `pc20260511_wissensbasis_longitudinal` hat ein longitudinales
**Provenienz**-Substrat gelandet (`wb_field_provenance`, `wb_field_provenance_archive`,
`wb_event_log`, `wb_retrospective_annotation`). Das beantwortet „welchen Wert hatte Feld F zum
Beobachtungszeitpunkt T" und wird vom Reva-Konsumenten getrieben. Es ist **nicht** die zeitliche
Gültigkeit einer KG-Relation und wird hier weder benutzt noch erweitert.

**Nicht Identität.** Der Plan ist an dieser Stelle ausdrücklich: *Identität (Entity-Merges) ≠
zeitliche Gültigkeit (Kanten-Lebensdauer).* Ein Merge sagt „das ist dasselbe Ding"; ein Expire
sagt „das war damals wahr, jetzt nicht mehr". Diese beiden zu vermischen verdirbt beide. Konkret:
`merge_entities` (`:949-982`) setzt bei Duplikaten `is_active = false` auf Relationen — das ist
**Deduplizierung**, kein Ungültigwerden, und muss auch nach diesem Vorhaben unterscheidbar
bleiben.

## 4. Warum `is_active` nicht reicht

Naheliegend wäre, den widersprochenen Datensatz einfach auf `is_active = false` zu setzen. Das
ist falsch, weil `KGRelation.is_active` bereits **vier** verschiedene Bedeutungen trägt.
Vollständige Liste aller Schreibstellen, repo-weit ermittelt:

| Bedeutung | Ort |
|---|---|
| Merge-Dedup — Verlierer-Kante nach einem Entity-Merge | `knowledge_graph_service.py:965`, `:982` |
| Kaskade — Entität wird gelöscht, ihre Kanten fallen mit | `knowledge_graph_service.py:1667-1672` |
| Aufräumen — verwaiste Kante nach Soft-Delete ungültiger Entitäten | `kg_cleanup_service.py:88-92` |
| Wikilink entfernt — `[[Ziel]]` steht nicht mehr in der Notiz | `note_links.py:107`, `:128` |

Alle vier heißen „diese Kante zählt nicht mehr", aber aus **strukturell verschiedenen Gründen**,
und keiner davon ist „war wahr, ist es nicht mehr".

`KGEntity.is_active` (etwa `:949`, Grabstein einer zusammengeführten Entität) ist eine **andere
Spalte auf einer anderen Tabelle** und gehört nicht in diese Aufzählung — beim Zählen leicht zu
verwechseln.

Käme „widersprochen" als fünfte Bedeutung dazu, ließe sich hinterher nicht mehr sagen,
**warum** eine Kante inaktiv ist — und damit weder „was galt im März" beantworten noch ein
falsches Expire zurücknehmen. Gültigkeit braucht ihren eigenen Ausdruck.

## 5. Schema

Zwei Spalten auf `kg_relations`, additiv:

```sql
valid_from  TIMESTAMP NULL   -- ab wann die Aussage gilt; NULL = "seit jeher/unbekannt"
valid_to    TIMESTAMP NULL   -- ab wann sie NICHT mehr gilt; NULL = "gilt weiterhin"
```

Dazu eine schmale Begründungsspalte, damit ein Expire nachvollziehbar und rücknehmbar ist:

```sql
invalidated_by_relation_id  INTEGER NULL REFERENCES kg_relations(id) ON DELETE SET NULL
```

**Warum `valid_from`/`valid_to` und nicht die Issue-Namen `valid_at`/`invalid_at`:** Ein
Intervall hat zwei Enden; `valid_at` klingt nach einem Zeitpunkt und lädt zu der Fehllesart ein,
die Spalte sei der Beobachtungszeitpunkt (das ist `created_at`). Der Namensvorschlag im Issue
sollte in der Review bewusst bestätigt oder verworfen werden.

**Kein zweites Zeitachsenpaar.** „Bi-temporal" im Lehrbuchsinn hätte zusätzlich eine
Transaktionszeitachse (`tx_from`/`tx_to`). Die haben wir faktisch schon in `created_at`, und ein
volles Vier-Spalten-Modell verdoppelt jede Abfrage. **Empfehlung: nur die Gültigkeitsachse.** Der
Issue-Titel „bi-temporal" ist insofern zu groß gegriffen — das sollte die Review entscheiden,
nicht ich allein.

**Kein `NOT NULL`, kein Default `now()`.** Bestandskanten bekommen `valid_from = NULL`, was
„gilt, Anfang unbekannt" heißt. Ein Backfill mit `created_at` wäre eine **Erfindung**: Der
Zeitpunkt der Extraktion ist nicht der Zeitpunkt, ab dem der Fakt galt. Diese Unterscheidung
ist der Kern des Vorhabens und darf nicht schon in der Migration verwischt werden.

### 5.1 Index

Die Lesepfade filtern künftig auf „aktuell gültig". Der bestehende
`idx_kg_relations_subj_tier (subject_id, circle_tier)` bleibt führend; ergänzend ein partieller
Index für den Normalfall:

```sql
CREATE INDEX CONCURRENTLY idx_kg_relations_live
    ON kg_relations (subject_id, predicate)
    WHERE is_active = true AND valid_to IS NULL;
```

Partiell, weil abgelaufene Kanten im Normalbetrieb nie gelesen werden. `CONCURRENTLY` erfordert
`op.get_context().autocommit_block()` — das Projekt fährt `transaction_per_migration=True`, der
Ablauf ist in `CLAUDE.md` beschrieben und in `pc20260528` vorgemacht.

## 6. Lesepfade — die vollständige Liste

Das ist der Teil, an dem so etwas üblicherweise scheitert: **eine vergessene Abfrage liefert
weiterhin abgelaufene Kanten**, und der Fehler ist still. Die betroffenen Stellen, ausgezählt:

| Datei | Stellen | Was dort passiert |
|---|---|---|
| `knowledge_graph_service.py` | 14 | Schreiben, Merge, Dedup, Traversal |
| `note_links.py` | 3 | `[[Wikilink]]`-Kanten — **siehe §6.1** |
| `kg_retrieval.py` | 2 (`:330`, `:552`) | Agent-Kontext + Atom-Retrieval |
| `graph_expansion.py` | 2 (`:114`, `:198`) | Multi-Hop-BFS |
| `kg_graph_service.py` | 2 (`:176`, `:277`) | 3D-Wissensgraph (Cluster + Fokus) |
| `kg_cleanup_service.py` | 2 | Aufräumen |

Gezählt am 2026-09-04 mit dem **relationsspezifischen** Muster
`KGRelation.is_active | r.is_active | kg_relations…is_active` — Summe **25**. Ein breiter
`grep -c is_active` über dieselben Dateien liefert **56**, weil er `KGEntity.is_active`
mitzählt; Entitäten sind hier nicht betroffen. Wer nachzählt, sollte dasselbe Muster verwenden.

**Regel:** Jede Stelle, die heute `r.is_active = true` filtert, bekommt zusätzlich
`AND (r.valid_to IS NULL OR r.valid_to > :as_of)`. Der Default für `:as_of` ist `now()`.

**Durchsetzung statt Disziplin.** Sechs Dateien und 25 Stellen sind zu viele für „daran denken".
Zwei Vorschläge, in dieser Reihenfolge:

1. Ein gemeinsamer Filterbaustein analog `circle_sql.py` — `kg_validity_sql.live_clause(as_of)` —
   sodass die Bedingung an einer Stelle definiert ist. `circle_sql` ist im Projekt der etablierte
   Präzedenzfall für genau dieses Problem.
2. Ein Test, der den Quelltext nach `is_active` in `kg_*`-Dateien greppt und fehlschlägt, wenn
   eine Fundstelle nicht auch den Gültigkeitsbaustein verwendet. Grobes Werkzeug, aber es fängt
   die vergessene siebte Datei in sechs Monaten.

### 6.1 `note_links` ist ein Sonderfall und darf NICHT ablaufen

`note_links.py:91` legt für jeden `[[Wikilink]]` eine `note_link`-Relation an. Diese Kanten sind
**strukturell**, nicht behauptend: Sie sagen „Notiz A verweist auf B", nicht „X ist wahr". Sie
können nicht widersprochen werden und dürfen nie ein `valid_to` bekommen — sonst verschwinden
Wikilinks aus dem Graphen, weil ein Widerspruchsdetektor sie missversteht.

Zur Klarstellung: Diese Kanten werden durchaus deaktiviert, nämlich wenn der Wikilink aus der
Notiz verschwindet (`note_links.py:107`, `:128`). Das ist eine **Strukturänderung**, kein
Widerspruch — der Verweis existiert nicht mehr, nicht „er war falsch". Genau diese Unterscheidung
geht verloren, wenn Gültigkeit auf `is_active` abgebildet wird (§4).

**Empfehlung:** Die Widerspruchserkennung arbeitet auf einer **Allowlist von Prädikaten**, nicht
auf allen. Kein Prädikat auf der Liste ⇒ kein Expire, egal was das LLM meint.

## 7. Der schwierige Teil: Widerspruchserkennung

Zwei Kanten widersprechen sich, wenn sie dasselbe Subjekt und dasselbe Prädikat haben, das
Prädikat **funktional** ist (höchstens ein gültiger Wert), und die Objekte verschieden sind.

„Funktional" ist die ganze Schwierigkeit:

| Prädikat | funktional? |
|---|---|
| `wohnt_in` | ja — ein Hauptwohnsitz |
| `arbeitet_bei` | meistens, nicht immer |
| `mag` | **nein** — beliebig viele |
| `ist_kind_von` | nein — zwei Elternteile |
| `note_link` | nein, und nie widersprechbar (§6.1) |

### 7.1 Vorschlag: deklarative Allowlist, kein LLM-Urteil über Funktionalität

```yaml
# config/kg_predicates.yaml
functional:
  - wohnt_in
  - arbeitet_bei
  - hat_telefonnummer
```

Nur Prädikate auf dieser Liste können überhaupt ein Expire auslösen. Das LLM entscheidet dann
lediglich noch, **ob** die neue Aussage die alte ersetzt (siehe 7.2) — nicht, ob das Prädikat
funktional ist. Diese Trennung ist wichtig: Die Funktionalität eines Prädikats ist eine
Eigenschaft des Schemas und über die Zeit stabil; ob eine konkrete Aussage eine andere ablöst,
ist eine Einzelfallfrage.

Die Liste startet **kurz**. Ein fehlendes Prädikat bedeutet: Verhalten wie heute (beide Kanten
bleiben). Ein falsch aufgenommenes bedeutet: Datenverlust aus Sicht des Nutzers. Die
Asymmetrie diktiert die Richtung — im Zweifel nicht aufnehmen.

### 7.2 Wo die Erkennung läuft

**Vorbild ist das flache Memory**, das dieses Problem bereits gelöst hat:
`conversation_memory_service.py:627` mit `settings.memory_contradiction_resolution`, ein
strikt-JSON-Toolaufruf, dessen Schema in `services/memory_ops.py` liegt (`OpType`:
`ADD`/`UPDATE`/`DELETE`/`NOOP`, `MAX_OPS_PER_BATCH = 10`). Ein KG-Pendant sollte diese Form
spiegeln statt eine neue zu erfinden — inklusive der dort gelernten Lehren: **ein** Aufruf pro
Turn statt N pro Fakt, optimistische Nebenläufigkeit über einen Drift-Check der Kandidaten-IDs,
Advisory-Lock nur um Retrieve+Apply, nicht um den LLM-Aufruf.

**Zeitpunkt:** im Hintergrund nach dem `done`-Frame, im selben koordinierten Pfad wie die
KG-Extraktion (`chat_handler._extract_structured_background`). Nicht im Turn — ein zusätzlicher
LLM-Aufruf auf dem kritischen Pfad kostet Latenz für eine Korrektur, die Sekunden später
genauso richtig ist.

**Kandidatenmenge:** Nur Kanten mit demselben Subjekt und Prädikat, die aktuell gültig sind.
Das ist eine kleine, indexgestützte Abfrage — kein Graph-Scan.

## 8. Risiken

### R1 — Falsches Expire ist Datenverlust aus Nutzersicht **(HOCH)**

Setzt der Detektor `valid_to` auf einer Kante, die weiterhin gilt, verschwindet sie aus jeder
Antwort. Die Zeile steht noch in der Datenbank — für den Nutzer ist das Wissen weg.

**Gegenmaßnahmen, kumulativ:**
- Prädikat-Allowlist (§7.1) — begrenzt die Angriffsfläche auf wenige Prädikate.
- `invalidated_by_relation_id` zeigt auf die ablösende Kante ⇒ jedes Expire ist erklärbar und
  durch Nullen der beiden Spalten exakt rücknehmbar.
- Konfidenzschwelle: unter Schwelle kein Expire, sondern beide Kanten stehen lassen.
- **Kein** Expire ohne ablösende Kante. „X gilt nicht mehr" allein reicht nicht — es braucht
  ein „…sondern Y". Das schließt die gefährlichste Klasse aus (Halluzination erzeugt Löschung).

### R2 — Zirkelfilter auf dem neuen Pfad **(HOCH)**

Jede Abfrage, die Gültigkeit filtert, muss weiterhin durch `kg_relations_circles_filter`
(`circle_sql.py:249`). Ein neuer Lesepfad, der den Zirkelfilter vergisst, ist ein Datenleck
zwischen Nutzern — schwerwiegender als der Fehler, den das Vorhaben behebt.

**Gegenmaßnahme:** Kein neuer Lesepfad. Die Gültigkeitsbedingung wird in die **bestehenden**
Abfragen eingehängt, die den Zirkelfilter schon führen. Dazu je Konsument ein Test „Nicht-Mitglied
sieht die abgelaufene Kante auch nicht" — der Zirkelfilter darf durch die Änderung nicht
schwächer werden.

### R3 — Nebenläufigkeit: zwei Turns widersprechen derselben Kante **(MITTEL)**

Zwei parallele Hintergrundläufe könnten dieselbe Kante ablösen und zwei Nachfolger anlegen.
**Gegenmaßnahme:** derselbe Advisory-Lock-Mechanismus wie im Memory-Pendant, plus ein
bedingtes `UPDATE ... WHERE valid_to IS NULL` — der Verlierer schreibt nichts.

### R4 — Abgelaufene Kanten in `graph_expansion` **(MITTEL)**

`expand_fused` wandert über Kanten (`graph_expansion.py:114`, `:198`). Eine abgelaufene Kante als
Brücke würde veraltete Nachbarn einschleppen und über die Hops verstärken.
**Gegenmaßnahme:** Gültigkeitsfilter pro Hop, nicht nur auf den Startknoten — dieselbe Stelle,
an der schon heute der Zirkelfilter pro Hop steht.

### R5 — Der Detektor sieht nur, was extrahiert wurde **(NIEDRIG, aber ehrlich zu benennen)**

Sagt jemand „Jutta ist umgezogen", ohne dass ein neues `wohnt_in` extrahiert wird, läuft nichts
ab. Das Vorhaben behebt Widersprüche zwischen **extrahierten** Fakten, nicht die Lücken der
Extraktion. Das sollte in der Erwartungshaltung stehen.

## 9. Was das Vorhaben NICHT tut

- Keine Beantwortung von „was galt am 3. März" im UI. Die Daten würden es hergeben; die
  Abfrageoberfläche ist ein eigener Umfang.
- Kein Rückwirkendes Ableiten von Gültigkeit aus Bestandsdaten (§5).
- Keine Berührung des `wb_*`-Substrats (§3).
- Keine Änderung an `merge_entities` — Dedup bleibt `is_active`.

## 10. Umfang und Reihenfolge

Bewusst so geschnitten, dass nach jeder Stufe ein sinnvoller, flag-abschaltbarer Zustand steht.

| Stufe | Inhalt | Wirkung ohne die nächste Stufe |
|---|---|---|
| **1** | Migration (2 Spalten + FK + partieller Index), Modell, `kg_validity_sql`-Baustein, Gültigkeitsfilter in allen 25 Lesestellen | **Keine.** Alles `NULL` ⇒ Filter immer wahr ⇒ byte-identisch. Testbar durch Setzen von Hand |
| **2** | Prädikat-Allowlist + Detektor im Hintergrundpfad, flag-gated `KG_TEMPORAL_VALIDITY_ENABLED` | Widersprüche werden aufgelöst. Flag aus = Stufe 1 |
| **3** | `/brain/review`-Sichtbarkeit: Expires anzeigen und zurücknehmbar machen | R1 wird beobachtbar statt nur begrenzt |

**Stufe 1 ist der Löwenanteil und der langweilige Teil** — 25 Fundstellen, jede mit Test. Genau
deshalb sollte sie zuerst und allein landen: Sie ist byte-identisch verifizierbar, was von Stufe 2
niemand behaupten kann.

Für Stufe 3 gibt es im Projekt drei Vorbilder mit derselben Form (Vorschlag → Prüfung → Undo):
KG-Merge-Vorschläge, PDF-Split-Vorschläge, Dokument-Duplikate. Es wäre die vierte Warteschlange
auf `/brain/review` — der Reviewer sollte prüfen, ob das eine Konsolidierung verlangt, statt
einen vierten Sonderfall danebenzustellen.

## 11. Test-Strategie

Nach `CLAUDE.md`: TDD, Läufe auf `.159`, echtes Postgres wo Migrationen berührt sind.

**Stufe 1**
- Golden-SQL je Konsument: Flag aus / alle Spalten `NULL` ⇒ erzeugtes SQL und Ergebnis
  byte-identisch zu heute. Das ist der wichtigste Test des ganzen Vorhabens.
- Migration gegen echtes Postgres (`RENFIELD_TEST_PG_URL`), inklusive
  Upgrade→Downgrade→Upgrade — Vorbild `pc20260423`-Tests.
- Je Lesepfad: Kante mit `valid_to` in der Vergangenheit erscheint nicht; mit `valid_to` in der
  Zukunft erscheint sie.
- Zirkel-Leak-Test je Konsument (R2).

**Stufe 2**
- Allowlist: Prädikat nicht gelistet ⇒ kein Expire, auch wenn das LLM eines vorschlägt.
- `note_link` läuft nie ab (§6.1).
- Kein Expire ohne Nachfolger (R1).
- Nebenläufigkeit: zwei gleichzeitige Ablösungen ⇒ genau ein Expire (R3).
- Ein Eval-Fall mit dem Umzugsbeispiel, in der Form von `bin/run_kg_extraction_eval.py`.

## 12. Offene Fragen für die Review

1. **Spaltennamen** — `valid_from`/`valid_to` (mein Vorschlag) oder `valid_at`/`invalid_at` (Issue)?
2. **Wirklich bi-temporal?** Ich empfehle nur die Gültigkeitsachse (§5). Braucht jemand eine
   getrennte Transaktionszeit, verdoppelt sich der Abfrageaufwand.
3. **Welche Prädikate starten auf der Allowlist?** Meine Neigung: `wohnt_in` allein, alles
   Weitere nach Beobachtung.
4. **Stufe 3 eigenständig oder mit `/brain/review` konsolidieren?**
5. **Reihenfolge gegen #874.** Der Follow-up „`get_relevant_context` auf den fused Pfad" ist
   inzwischen gemergt (PR #1196) — der Agent-String-Pfad läuft über `expand_fused`. Damit hängt
   an Stufe 1 ein Pfad mehr als zum Zeitpunkt der Issue-Formulierung. Kein Blocker, aber die
   Fundstellenzählung in §6 gilt für den heutigen Stand, nicht den von Juli.

## 13. Empfehlung

**Stufe 1 bauen, Stufe 2 erst nach separatem Go.** Stufe 1 ist mechanisch, vollständig
byte-identisch verifizierbar und beseitigt die strukturelle Lücke. Stufe 2 trifft
Löschentscheidungen auf Basis eines LLM-Urteils und verdient eine eigene Betrachtung, wenn
Stufe 1 steht und man an echten Daten sehen kann, wie oft Widersprüche überhaupt auftreten.

Diese Zahl kennt heute niemand. Sie wäre vor Stufe 2 wissenswert und ist mit einer read-only
Abfrage nach Stufe 1 zu bekommen: Subjekt+Prädikat mit mehr als einer gültigen Kante, gruppiert.
Fällt sie klein aus, ist Stufe 2 womöglich gar nicht die Mühe wert — und diese Antwort wäre
ebenfalls ein Ergebnis.
