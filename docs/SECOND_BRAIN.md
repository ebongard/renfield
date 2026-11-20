# Second Brain — Persönliches Wissenssystem in Renfield

Renfield pflegt für jeden Nutzer ein persönliches Wissensnetz: ein „zweites Gehirn", aufgebaut aus vier Informationsarten, die unterschiedlich gewonnen und gespeichert werden, aber über eine gemeinsame Identitäts- und Zugriffsschicht als ein einziges Gedächtnis ansprechbar sind. Nichts davon wird in eine Cloud ausgelagert — Ingestion, Embedding, Indizierung, Retrieval laufen vollständig lokal.

Dieses Dokument beschreibt das Zusammenspiel. Für die einzelnen Subsysteme existieren dedizierte Abschnitte in [`FEATURES.md`](FEATURES.md); für die Zugriffslogik siehe [`CIRCLES.md`](CIRCLES.md).

---

## Die vier Informationsarten

| Typ | Quelle | Extraktion | Persistenz |
|---|---|---|---|
| **Dokument-Chunks** (RAG) | Datei-Uploads (PDF, DOCX, TXT, Markdown, Bilder mit OCR) | Parsing + Chunking + Embedding | `document_chunks` (pgvector) |
| **Conversation Memories** (Langzeit) | Chat- und Sprach-Turns | LLM-Extraktion nach Relevanz-Gate | `conversation_memories` (pgvector) |
| **KG-Entities** | Dokumente **und** Konversationen | LLM-Extraktion (strukturiertes JSON) | `kg_entities` (pgvector + Graph-Kanten) |
| **KG-Relations** | Entity-Kontexte | LLM im KG-Extraction-Step | `kg_relations` (gerichtet) |

Jede dieser Zeilen trägt zwei denormalisierte Spalten: `atom_id` (Verweis auf die polymorphe Registry) und `circle_tier`. Das lässt ein Retrieval in einem einzigen SQL-Statement sowohl joinen als auch zugriffskontrollieren.

> **Der Satz ist inzwischen größer als „vier".** Der polymorphe RRF-Store fusioniert heute weitere strukturierte Atom-Quellen: **Schicht-A-Fakten** (`document_facts`, `atom_type='document_fact'`), **prozedurale Skills** (`procedural_skills`), und — Phase 4B — handgeschriebene **Notizen** (`notes`, `atom_type='note'`) als erstklassiger, kreis-getierter Atom mit `[[bidirektionalen Verlinkungen]]` auf dem KG-Substrat (Notiz ↔ `kg_entities`, Link ↔ `kg_relations`). Siehe `docs/design/notes-atom.md` + `docs/FEATURES.md`. Das Grundprinzip — je eine Quelltabelle, gehoben über `atom_id` + `circle_tier` in eine gemeinsame zugriffskontrollierte Retrieval-Ebene — bleibt für alle identisch.

---

## Gemeinsame Identität — der Atoms-Layer

Die vier Informationsarten leben in verschiedenen Tabellen mit verschiedenen Schemata. Die `atoms`-Registry hebt sie auf eine gemeinsame Ebene:

```
                                 ┌──────────────────────┐
                                 │   atoms (registry)   │
                                 ├──────────────────────┤
                                 │  atom_id  (UUID)     │
                                 │  atom_type           │
                                 │  source_table        │
                                 │  source_id           │
                                 │  owner_user_id       │
                                 │  policy (JSON)       │
                                 └──────────────────────┘
                                            ▲
             ┌──────────────────┬───────────┼───────────────┬───────────────────┐
             │                  │           │               │                   │
 ┌───────────┴─────────┐ ┌──────┴──────┐ ┌──┴──────────┐ ┌──┴──────────────┐
 │   document_chunks   │ │ kg_entities │ │ kg_relations │ │ conversation_   │
 │   (atom_id FK,      │ │ (atom_id,   │ │ (atom_id,    │ │ memories        │
 │    circle_tier)     │ │  tier)      │ │  tier)       │ │ (atom_id, tier) │
 └─────────────────────┘ └─────────────┘ └──────────────┘ └─────────────────┘
```

Jeder Schreibzugriff auf eine der Quell-Tabellen läuft über `services/atom_service.py::upsert_atom`. Direkte `INSERT`s sind durch Code-Review + einen CI-Lint verboten. Die Invariante: `atoms.policy` ist die Wahrheit, die denormalisierten Spalten sind der Performance-Schatten.

---

## Retrieval — Cross-Source mit Rang-Fusion

Wenn ein Nutzer fragt *„Was weiß ich über Am Stirkenbend 20?"*, will er keine Treffer-Liste pro Subsystem — er will **eine** Antwort, in der alle vier Informationsarten vertreten sein können.

`services/polymorphic_atom_store.py` löst das per **Reciprocal Rank Fusion (RRF)**:

1. Suche gegen alle Quell-Tabellen über **zwei komplementäre Retrieval-Pfade**:
   - **Dense** — Vektorsuche mit der Query-Embedding (semantische Nähe).
   - **Lexical** (`services/lexical_retrieval.py`) — Postgres-FTS mit `ts_rank` (Keyword- und Eigennamen-Treffer). Schließt die Recall-Lücke bei Einzeltoken-Queries wie *„Erika"*, deren Embedding-Ähnlichkeit zur passenden Stelle unter der Threshold liegen kann.
2. Jeder Pfad liefert sein Top-*k* unter Berücksichtigung des Circle-Filters (`services/circle_sql.build_filter`).
3. RRF berechnet einen kombinierten Rang-Score pro Atom anhand der Position in allen Ergebnislisten. Ein Atom, das sowohl semantisch als auch lexikalisch trifft, bekommt den Doppel-Boost — exakt das gewünschte Verhalten für „beides".
4. Die fusionierte Top-*n*-Liste wandert zurück zum Aufrufer, angereichert mit Source-Metadaten (Dokumenttitel, Entity-Label, Memory-Kategorie).

🛑 **Ob die acht Quellen gleichzeitig laufen, entscheidet die SITZUNG, nicht das `asyncio.gather`.** Eine
`AsyncSession` hält eine Verbindung; gleichzeitige Abfragen auf ihr unterstützt SQLAlchemy nicht. Gemessen
am 2026-10-29 gegen echtes Postgres (`bin/probe-atom-store-sessions.py`), acht Abrufe à ~50 ms reine SQL-Zeit:

| Sitzung | geliefert | Dauer |
|---|---|---|
| eine, **kalt** | **1 von 8** | 52 ms |
| eine, vorgewärmt | 8 von 8 | **408 ms** (reihum) |
| je Quelle eine eigene | 8 von 8 | 56 ms |

Daraus folgen zwei Betriebsarten, die der AUFRUFER über den Konstruktor wählt:

- **Föderiert** (`federation_query_responder._retrieve`): die Sitzung ist frisch, also fasst keine Quelle sie
  an (`PolymorphicAtomStore(session, source_sessions=…)`). Jede Quelle bekommt einen `SourceReader`; jede
  einzelne ANWEISUNG darauf läuft in einer eigenen Sitzung, die genau so lange lebt wie die Anweisung. Bis
  zum 2026-11-15 fächerte dieser Pfad auf der einen kalten Sitzung — live stand bei jeder eingehenden Anfrage
  „2/8 sources delivered, 3 crashed", und die Antwort an die Gegenseite war fast leer. Vorwärmen wäre keine
  Reparatur gewesen: es beseitigt die Meldung, indem es die Nebenläufigkeit beseitigt.
- **Lokal** (`GET /api/atoms`): unverändert eine Sitzung, reihum. Sie ist warm, weil die
  Anmelde-Abhängigkeit dieselbe Sitzung vor dem Routenrumpf benutzt hat. Bewusst nicht umgestellt: die
  SQL-Zeit ist dort nicht der bestimmende Anteil, acht Verbindungen je Suche wären es im Pool aber sehr wohl.

🛑 **Gedeckelt ist die Anweisung, nicht die Quelle:** höchstens `SOURCE_SESSION_LIMIT` (8) Anweisungen —
und damit Verbindungen — zugleich im ganzen Prozess, gleichgültig wie viele Anfragen eingehen: 8 von 30
Pool-Verbindungen (`db_pool_size` 10 + `db_max_overflow` 20). Zwischen zwei Anweisungen hält eine Quelle
nichts. Das ist der Punkt, weil fünf der acht Quellen dazwischen einen Modellserver fragen (`kg` vor der
ersten Anweisung ein LLM, Lesefrist 300 s): hielte die Quelle ihren Platz über den ganzen Aufruf, belegten
wartende Modellaufrufe alle Plätze ohne eine einzige Verbindung, und das SQL jeder anderen Anfrage stünde
dahinter an. Die Schranke gilt nur, weil der Responder EINE `SourceSessions`-Instanz für alle Abrufe hält.
Dazu hat jede Quelle dort eine Frist (`ATOM_FANOUT_SOURCE_TIMEOUT`, 20 s): wer sie überschreitet, wird
abgebrochen und als abgestürzt gezählt, die übrigen tragen die Antwort. Der GANZE Abrufschritt des
Responders steht zusätzlich unter einer abgeleiteten Frist (60 s Anfragefenster −
`FEDERATION_SYNTHESIS_TIMEOUT` − 5 s Reserve, Standard 25 s); der Abruf selbst bekommt davon ein Budget, das
1 s früher endet (`federation_retrieve_budget`). Was an dieser Grenze geschieht, hängt davon ab, WAS gerade
läuft: Die Quellen sind die Antwort — werden sie im Budget nicht fertig, endet die Anfrage als fehlgeschlagen,
mit einem Grund, der Schritt und Frist nennt (`RetrievalDeadlineExceeded`). Die Graph-Erweiterung danach ist
eine Anreicherung — sie bekommt, was die Quellen vom Budget übrig lassen; reicht das nicht, wird sie
abgebrochen, und die Antwort entsteht aus dem NICHT erweiterten Ergebnis (WARNING `UNEXPANDED`, in der
Protokollzeile der Code `graph_expansion_skipped`). Nur die EIGENE Frist zählt als „Budget aufgebraucht“: Ein Abbruch von außen (Herunterfahren, abgebrochene Anfrage) beendet den Abruf — auch wenn er eintrifft, während die Erweiterung sich nach ihrer Frist noch abwickelt (#1563, `_within_budget`). Eine Quelle, die ihre Frist ausschöpft, kostet die übrigen
sieben also nicht mehr ihr Ergebnis. Dass die Quellenfrist ins Budget passt, prüft der Start
(`docs/ENVIRONMENT_VARIABLES.md`).

🛑 **Ein föderierter Abruf ist ein reiner Lesevorgang — und das folgt der ANGABE des Aufrufers
(`query(federated=True)`), nicht dem Sitzungsregime.** Zwei Quellen schreiben sonst, und beide sind für einen
föderierten Aufruf ausdrücklich abgeschaltet: der Zugriffszähler der Erinnerungen (`access_count`,
`last_accessed_at`; `track_access=False`) und das Schattenprotokoll der Fertigkeiten
(`record_shadow_log=False`) — letzteres legte bei einer gemappten Anfrage Kuratorzeilen unter dem
verknüpften lokalen Konto an. Die Frage einer Gegenseite ist kein Zug einer lokalen Person, gleich auf
welcher Sitzung sie läuft. Die umgekehrte Kombination — ein LOKALER Aufruf auf eigenen Sitzungen — wird
abgelehnt (`ValueError`): er verlöre beide Schreibvorgänge stillschweigend. Der `SourceReader` kann nicht
schreiben, und zwar doppelt: er hat kein `add`, `flush`, `commit`, und jede Anweisung läuft in einer
READ-ONLY-Transaktion, sodass Postgres auch ein `UPDATE … RETURNING` oder `SELECT … FOR UPDATE` über
`execute` zurückweist — eine Quelle, die es versucht, fällt sichtbar aus. Der Geltungsbereich (`asker_id`,
`PeerScope`) ist in beiden Betriebsarten derselbe — eine eigene Sitzung ändert, über welche Verbindung eine
Quelle liest, nie, was sie lesen darf. `query` protokolliert je Durchlauf eine Zeile (geliefert / leer /
abgestürzt, Dauer, Pfad).

Der API-Endpunkt `/api/atoms` und die `/brain`-Frontend-Seite exponieren genau diesen Weg.

**Mehrsprachigkeit im Lexical-Pfad:** `conversation_memories.search_vector` (Migration `pc20260528`), `document_chunks.search_vector` (Migration `pc20260529`) und `document_facts.search_vector` (Migration `pc20260602`) sind `GENERATED STORED`-Spalten, deren Ausdruck `to_tsvector`-Aufrufe über alle in `services/fts_languages.FTS_LANGUAGES` aufgeführten Configs (DE / EN / FR / IT / ES / NL) unioniert. Die Query-Seite unioniert `websearch_to_tsquery` über dieselbe Menge. So matcht ein französisches Memory oder Dokument eine deutsche Anfrage und umgekehrt — wichtig für mehrsprachige Haushalte. Alle drei Spalten pflegen sich serverseitig — App-Code schreibt nicht mehr in `search_vector` (Postgres würde mit `cannot insert a non-DEFAULT value into column "search_vector"` antworten). Eine vierte solche Spalte, `documents.search_vector` (Migration `pc20260829_documents_fts`, über `generated_title || title || filename`), trägt die **Dokumentsuche nach Name**: `GET /api/knowledge/documents?q=` ist eine rang-gewichtete Hybridsuche (Name-FTS + Schicht-A-Fakten + semantische Chunks, RRF-fusioniert), sodass ein Dokument über Name/Fakten/Inhalt auffindbar ist statt nur in den 100 neuesten der Liste zu erscheinen; ein einziges Circle-Gate auf die fusionierten IDs ist die alleinige Sicht-Grenze (die Namens-FTS ist für sich nicht kreis-gefiltert). Code: `services/document_search.py`.

**Schicht-A-Fakten als Retrieval-Quelle:** Aus Dokumenten extrahierte strukturierte Fakten (`document_facts`) waren bis dato *write-only*: nichts las sie. Die Extraktion ist **offen/generisch** — ein deterministischer Pass sichert Identifikatoren (Steuernummer, IBAN), ein LLM-Pass liefert Verpflichtungen plus eine freie `facts[]`-Liste, sodass jeder Dokumenttyp seine eigenen Eckdaten (Rechnungsdatum, Vertragskonto, Leistungszeitraum, Guthaben, Beträge, Aussteller …) statt einer festen Feldauswahl trägt; gespeichert wird in zwei Kübeln (`identifier` / `universal`) plus die typisierten `obligation`-Fakten. `services/document_fact_retrieval.py` macht sie abfragbar und fusioniert `document_fact` als weitere Quelle in dieselbe RRF (grünes „Fakt"-Badge unter `/brain`). Keyword-FTS über `search_vector` plus ein Identifier-`ILIKE`-Zweig auf `normalized_value` (Postgres tokenisiert `114/5876/5293` unzuverlässig — der ILIKE-Zweig ist die verlässliche Exakt-Identifier-Suche, nur bei identifier-förmigen Query-Tokens aktiv). Fakten erben die Circle-Tier-Policy ihres Eltern-Dokuments. Zwei weitere Lesepfade tragen die UI: `facts_for_document(doc_id)` (alle Fakten eines Dokuments) speist die inline **Fakten**-Panel auf jeder `/knowledge`-Dokumentkarte, und `obligations(due_before, offset)` speist die Verpflichtungs-Agenda unter `/brain/fristen` (Rechnungen + Behörden-Fristen, nach Dringlichkeit gruppiert, nächste Frist zuerst) — das eigentlich tragende Versprechen gegen die „stiller-Archiv"-Narbe. Beide Flächen rendern Herkunft (`✓` deterministisch / `~` Modell-Vorschlag) und Tier; rechtliche Fristen (`legal_gate`) sind als `⚑ rechtlich` markiert. Das Bestätigen einer Frist ist serverseitig im Quittungs-Ledger (`obligation_acknowledgements`) verankert (`POST/DELETE /api/atoms/obligations/{id}/confirm`, pro Nutzer) — geräteübergreifend und zugleich das Signal, das den **Fristen-Notifier** stoppt. Der Notifier (`OBLIGATION_NOTIFIER_ENABLED`, täglicher besitzer-adressierter Scan über `obligation_date`, restart-fest via Ledger) macht aus den Fristen *ankommende* Erinnerungen — die andere Hälfte des Versprechens gegen die „stiller-Archiv"-Narbe; rechtliche Fristen werden gemeldet, aber human-gated über `/brain/review`.

Die spezialisierten Retrieval-Pfade bleiben daneben erhalten:

- **RAG** (`services/rag_retrieval.py`) — wenn der Agent explizit nach Dokumenten sucht
- **KG** (`services/kg_retrieval.py`) — wenn Entity-Resolution nötig ist
- **Memory** (`services/memory_retrieval.py` / `ConversationMemoryService.retrieve`) — im Chat-Handler als Prompt-Kontext

Alle drei nutzen dieselbe `circle_sql.build_filter`-Klausel, sodass Circle-Reichweite in jedem Pfad identisch angewendet wird.

---

## Ingestion — wie Wissen entsteht

```
┌─────────────┐    ┌────────────────┐    ┌──────────────────┐
│  Datei      │───▶│ chat_upload    │───▶│ extraction       │
│  (Paperclip)│    │ (+ ChatUpload) │    │ (Docling/PDF/    │
└─────────────┘    └────────────────┘    │  OCR)            │
                                          └────────┬─────────┘
                                                   ▼
                                          ┌──────────────────┐
                                          │ RAGService       │
                                          │ .ingest_document │─┐
                                          └──────────────────┘ │
                                                               ▼
                                          ┌──────────────────────────┐
                                          │ atoms + document_chunks  │
                                          │ (mit circle_tier)        │
                                          └──────────────────────────┘
                                                   │
                                                   ▼
                                          ┌──────────────────────────┐
                                          │ Hook: post_document_     │
                                          │ ingest                   │
                                          │  └─▶ KG-Extraction       │
                                          │  └─▶ Paperless-Audit     │
                                          │  └─▶ Custom Plugins      │
                                          └──────────────────────────┘
```

**Konversations-Memory** läuft analog, aber als Hintergrund-Task, nachdem die Antwort beim Nutzer ist (`extract_memories_background` in `services/turn_extraction.py`). Eine mehrstufige Gate-Kette (Stage 1–4) entscheidet, ob eine Konversation memorable Fakten enthielt; wenn ja, extrahiert ein LLM-Call die Fakten und legt sie als `conversation_memories` an — wieder mit Atom-Registrierung und Tier-Zuweisung.

Dieser Seam gehört **nicht dem Browser allein**: sowohl der Chat-WebSocket (`chat_handler`, nach dem `done`-Frame) als auch der Satelliten-Sprachpfad (`satellite_handler._spawn_satellite_extraction`, nach dem Persistieren des Turns) planen dieselbe Extraktion ein — vorher wurde ein gesprochener Turn zwar beantwortet und gespeichert, aber nie extrahiert, also sofort wieder vergessen. Beide Pfade teilen sich auch die Skip-Regeln (Flags aus / leere Antwort / fehlgeschlagene Tool-Aktion). **Gesprochene Turns werden nur bei erkanntem Sprecher extrahiert**: ohne Speaker→User-Zuordnung (`User.speaker_id`) läuft weder Memory-Extraktion noch der `post_message`-Hook — eine unzugeordnete Stimme im Raum darf nicht zum Gedächtnis einer Person werden. Ein Turn ohne Konversations-`session_id` (also nicht persistiert) wird ebenfalls nicht extrahiert.

**KG-Extraktion** läuft sowohl bei Dokument-Ingest (als Hook) als auch bei Chat-Memory-Ingest. Derselbe LLM-Prompt, unterschiedliche Quell-Kontexte. Entity-Deduplizierung per Cosine-Similarity (Embedding-basiert) verhindert das Anlegen von `Max van den Muster` und `Max` als zwei Entitäten.

**Ingest-Audit (`document_processing_history`).** Jeder Lauf durch `RAGService.process_existing_document` schreibt eine Zeile in eine reine Audit-Tabelle: `started_at`, `finished_at`, `status` (`processing`/`completed`/`failed`), `force_ocr`, `ocr_engine` (`docling`/`docling_full_page_ocr`), `chunks_produced`, `chunks_dropped_low_quality`, `trigger` (`initial_ingest`/`user_reindex`/`script_purge`/`startup_sweep`), `error_message`. Geschrieben durch den Single-Writer `DocumentProcessingHistoryService.track()` Async-Context-Manager — die Ingest-Funktion belegt die Metriken auf einem Handle, der Manager schließt die Zeile beim Verlassen. Verwendet vom Cleanup-Skript `bin/purge_low_quality_chunks.py` (Re-OCR von Altbestand mit OCR-Garbage): `has_force_ocr_succeeded(doc_id)` filtert über einen Partial-Index Dokumente, die bereits per `force_ocr=True` neu eingelesen wurden — macht das Skript über mehrere Läufe idempotent.

---

## Tier-Defaults und Tier-Review

Neue Atome erhalten einen **Default-Tier** — aktuell `2` (household) bei Dokument-Uploads, `1` (trusted) bei KG-Entities aus Chat-Memories. Die Defaults sind bewusst eher einschränkend: was nicht ausdrücklich geteilt wurde, bleibt nah am Eigentümer.

`/brain/review` listet Atome, die der Eigentümer neu klassifizieren sollte — neue Uploads, Entities mit Tier-Konflikten zwischen Relationen, Memories die ein Gate knapp passiert haben. Der Eigentümer kann dort batch-weise Tiers setzen; die Tier-Cascade propagiert auf incidente Relationen.

## Strukturiertes Memory — Kanonisierung & Subjekt

Damit das Gedächtnis nicht nur „flacher Text" ist, traegt jeder konversationelle Fakt ein **Subjekt** (`subject_name` / `subject_entity_id`): über WEN er handelt. Das Retrieval reicht das Subjekt mit und taggt den injizierten Kontext (`- [FACT · <Name>] …`), sodass Fakten über verschiedene Personen strukturell nicht mehr vermischt werden.

Auf der KG-Seite werden Entitäten **kanonisiert**: Schreibvarianten sammeln sich als `surface_forms` auf der kanonischen Zeile, eine Entität kann mehrere Rollen tragen (`entity_types`, z. B. Person + Musiker), und Relationen halten die Provenienz (`stated_by_user_id` — wer hat es gesagt). Ein periodischer **Reconciler** führt nachträglich entstandene Dubletten zusammen: same-tier mit hoher Ähnlichkeit automatisch, Cross-Tier/Grauzone/Schreibvariante eines Personennamens (`name_typo`, ein Edit innerhalb eines Tokens) als **Merge-Vorschlag** zur Owner-Review (`/brain/review`); ein abgelehnter Vorschlag wird nicht erneut gestellt. Zwei Riegel halten den Abgleicher ehrlich, weil die Einbettung sie nicht sehen kann: Personen mit unverwandten Namen werden gar nicht erst vorgeschlagen, und Entitäten **verschiedener Art** (ein Ort und die dort ansässige Firma stehen im selben Briefkopf und ähneln sich entsprechend) nur dann, wenn die Namen verwandt sind — dann als Vorschlag `cross_type`, nie als stille Faltung. Eine Verschmelzung erhöht nie die Sichtbarkeit (Tier = MIN) — Details in [`CIRCLES.md`](CIRCLES.md#merge-invariante-structured-memory).

---

## Federation — Zweite Gehirne, die sich begegnen

Zwei paarweise verbundene Renfield-Instanzen können Queries über die Circle-Grenze schicken: Nutzer A auf Maschine M1 fragt, Nutzer B auf M2 antwortet aus seinem Second Brain — aber nur mit Atomen, für die B's Circles den Leseranger A enthalten. Details siehe [`FEDERATION_MULTI_PEER.md`](FEDERATION_MULTI_PEER.md). Der Circle-Filter läuft dabei **auf der Responder-Seite** — A bekommt nie zu sehen, was er nicht sehen darf, nicht weil M1 filtert, sondern weil M2 gar nichts anderes zurückgibt.

**Peer-Scope — der Responder-Filter gilt IMMER, auch bei `AUTH_ENABLED=false`.** Der Responder reicht eine `PeerScope(owner_id, tier)` an `PolymorphicAtomStore.query`. Das schaltet den Single-User-Bypass ab (eine `auth_enabled=false`-Instanz „sieht" lokal alles — ein föderierter Peer ist aber nie der lokale Single-User) **und** lässt die Zweige „Owner-Gleichheit" und „expliziter Grant" weg; übrig bleibt `(public) ODER (dieser Eigentümer UND seine bei der Paarung gewährte Stufe)`. Jeder nicht-föderierte Aufrufer übergibt `peer=None` → byte-identisch wie zuvor. Siehe `services/circle_sql.py` (`PeerScope`) + `services/polymorphic_atom_store.py`. 🛑 Hier stand bis 2026-10-27, die `asker_id` des Peers stamme aus `PeerUser.remote_user_id` und kollidiere „in einem Single-User-Haushalt" mit der Owner-ID. Der Haushalt ist seit dem 2026-09-23 auth-on, und die Kollision war nicht hypothetisch, sondern hat am 2026-10-27 die erste echte Paarung zerrissen (F-ID-3 / BL-0135). Im peer-bezogenen Zweig entsteht jetzt gar keine `asker_id` mehr.

---

## Daten-Besitz

Atome gehören immer **genau einem** `owner_user_id`. Es gibt keine geteilten Atome ohne expliziten Grant — das Modell kennt keinen „shared folder" mit Eigentum-am-Ordner. Das hält die Verantwortlichkeit scharf: wer ein Atom löscht, löscht *sein* Atom; was Mitglieder niedrigerer Tiers davon sehen, war nie *ihres*.

Konsequenz: bei User-Löschung werden alle Atome des Nutzers kaskadierend gelöscht. `AtomExplicitGrant`-Einträge zu anderen Nutzern ebenso. Dort wo KG-Relations auf gelöschte Entitäten zeigen, werden sie mit-abgeräumt.

---

## Ein Ort für alles — der Wissens-Workspace

Die vier Informationsarten haben historisch je eine eigene Seite (`/knowledge`, `/brain`, `/memory`, `/knowledge-graph`, plus die Fristen-Agenda und die Review-Queue). Hinter dem Flag `wissen_workspace_enabled` (standardmäßig aus) verschmelzen sie zu **einem** `/wissen`-Workspace — denn es ist *ein* Korpus, betrachtet durch verschiedene Linsen:

- **Lens-Leiste** statt sechs Navigationspunkte: Übersicht · Dokumente · Graph · Erinnerungen · Fristen · Prüfen. Jede Linse ist die bestehende Seite, eingebettet (Doppel-Header/Breite entfallen via `LensFrame` + Kontext); Sichtbarkeit pro Linse über dieselben Permission-/Feature-Gates.
- **Lens-bezogene Omnisuche** als Dreh- und Angelpunkt: `scope=lens` lässt die aktive Linse ihre *eigene* Inline-Suche fahren (Dokument-Chunk-Suche, Entity-Tabellenfilter), `scope=everything` legt ein Cross-Source-RRF-Overlay über die `/api/atoms`-Fusion (genau der Weg aus dem Retrieval-Abschnitt oben).
- **Universeller Detail-Drawer**: Klick auf ein beliebiges Ergebnis öffnet typ-spezifischen Inhalt (Dokument→Fakten, Fakt→ObligationRow+Herkunft, Memory→Text, Entity→Name/Typ, Kante→Triple) samt Tier-Edit. Damit Graph-Entities einzeln adressierbar sind, liefert das Retrieval nun **pro-Entity-`kg_node`-/pro-Relation-`kg_edge`-Atome** (statt eines aggregierten Blocks); der String-Kontext für den Agenten bleibt unverändert.

Alte URLs leiten (mit `?search`/`#hash`) in die Linsen um; ist das Flag aus, bleibt die flache Navigation unverändert. Code-Detail: Abschnitt „Unified Wissen workspace" in `CLAUDE.md`.

---

## Siehe auch

- [CIRCLES.md](CIRCLES.md) — Zugriffsebenen-Modell, Datentabellen und Retrieval-Filter im Detail
- [FEATURES.md](FEATURES.md) — Einzel-Feature-Beschreibungen (RAG, Memory, KG)
- [ACCESS_CONTROL.md](ACCESS_CONTROL.md) — RPBAC-Schicht darunter (Authentifizierung + Rollen)
- [FEDERATION_MULTI_PEER.md](FEDERATION_MULTI_PEER.md) — Cross-Instance-Queries über die Circle-Grenze

---

## Background moved from CLAUDE.md (2026-09-20)

Developer-facing detail that used to live in the `CLAUDE.md` section "Circles v1 (access tiers)". The invariants for
editing this code are in `.claude/rules/documents-facts.md` and `.claude/rules/obligations.md`; this section keeps the
history, the tooling and the frontend wiring.

### Generated document titles

- `services/schicht_a_extractor.generate_document_title(facts)` synthesizes a short human title (issuer + type + date,
  inferring the doc type from context) via one LLM call. It is stored in `documents.generated_title` (migration
  `pc20260611`).
- The Schicht A ingest hook sets it best-effort after the facts commit. No facts means no title AND no document
  date; the hook then logs `Schicht A: doc N produced no facts (error=…)` (counts only) and there is NO automatic
  retry — the document stays untitled until it is re-extracted (reindex). Until 2026-10-31 that exit was silent
  (xidra doc 669 could not be traced).
- `GET /api/knowledge/documents` returns `display_name = generated_title → title → filename`; the `/wissen/dokumente`
  (+ `/knowledge`) list renders it.
- Existing documents are titled by `bin/backfill_document_titles.py` (`--dry-run` / `--commit`; works off stored
  facts, no re-OCR). The hook is gated by `schicht_a_extraction_enabled`; the backfill only needs a chat model.

### Document date — why the derivation is this strict

- `documents.document_date` (Date, migration `pc20260831`) is the document's OWN date (invoice / letter date),
  distinct from `created_at` (the import). It is derived at Schicht-A extraction by
  `services/document_date.derive_document_date` and is always a full date.
- Before 2026-09-15 the derivation took the first parsable date out of ANY fact. That dated household documents up
  to a year ahead (an obligation's excerpt, a validity end, next year's instalment period) and silently mis-dated
  others with a past deadline.
- The kind sets come from the kinds the extractor actually emits, including the date kinds the first cut left
  unranked (both instances measured, names/counts only).
- The Simba booking period (`simba_ingest_review._document_period`) uses the same helper.

### Document-date backfill tooling

- `bin/backfill_document_dates.py` fills NULL dates.
- `--rederive` (core: `services/document_date_backfill.py`) re-derives documents that already have a date:
  - `--scope future` (default) — documents dated more than the tolerance after import;
  - `--scope all` — every dated document; this also repairs a past deadline that was taken as the date.
  - It writes only where the result differs (idempotent), is a dry run unless `--commit`, and prints ids plus
    `unchanged` / `changed_earlier` / `changed_later` / `cleared`.
- `--paperless-ids-out FILE` writes the Paperless ids of CHANGED documents for the follow-up
  `bin/backfill_paperless_metadata.py --mode created-date`.
  - **Cleared** documents (date now NULL) are listed separately and must not be handed to that follow-up: it sources
    `created` from `document_date` and skips NULL, so Paperless keeps its current date.
  - Caveat: the follow-up only PATCHes where Paperless `created` still equals `added`. A document whose `created` an
    earlier run already set to the old wrong date is reported `skipped_not_consume_date`, not corrected.

### List sort + integration icons

- `GET /api/knowledge/documents` takes `sort` (`name`|`imported`|`document_date`) + `order`. The recency branch sorts
  the full set in SQL (`rag_service.list_documents`, `NULLS LAST`); the `q`-search branch page-sorts the returned
  relevance page. Under auth without `kb.all` and without a KB id the recency branch lists only the documents the
  caller may reach (the search gate's document filter, applied before pagination; `docs/CIRCLES.md`).
- The response also carries `document_date` and `in_paperless` (`paperless_document_id` present OR
  `paperless_state='done'`). `paperless_url` (built from the Paperless id) is served under auth only to a caller
  with `mcp.paperless.read` (`services/paperless_visibility.py`); `in_paperless` is a status without the id and
  stays for everyone who sees the document. `paperless_link_withheld` (no id) is true when the document is filed
  but the link was withheld — so the client does not mistake it for a duplicate without an id.
- Frontend `/wissen/dokumente` (`pages/KnowledgePage.tsx`): a sort-button bar (Name / Importdatum / Dokumentdatum; the
  active arrow toggles asc/desc) plus per-row integration status icons (`Archive` = Paperless, green = present /
  muted = absent).

### Document search by name/content

- `GET /api/knowledge/documents?q=` is a ranked hybrid document search — always-on, no flag, a bare `q` param on the
  existing list route. A document is reachable by its NAME (incl. the synthesized `generated_title`), its Schicht-A
  FACTS or its CONTENT, regardless of the 100-newest recency window the plain list shows.
- The concrete gap it closed: a re-ingested document dedups against an old KB copy that sits far down the id order
  and was otherwise unreachable in the UI.
- `services/document_search.py::search_documents` runs three candidate signals — NAME (`documents.search_vector` FTS
  via `ts_rank` + an ILIKE partial-token fallback), FACTS (`DocumentFactRetrieval`), CHUNKS (`RAGRetrieval`) —
  RRF-fuses them (`rag_hybrid_rrf_k`) and applies ONE circle-visibility gate on the fused ids (D2 —
  `document_chunks_circles_filter`). With `gate_available = not enforce_circles or postgres`, a leak is structurally
  impossible even in an unsupported auth-on-against-sqlite config. 🛑 `enforce_circles` carries **no default** since
  2026-10-28 — the route passes `settings.auth_enabled`, and omitting it raises instead of silently enforcing: when
  F-ID-3 dropped the argument, the inherited `True` reduced every auth-off result to the public rung. It means only
  "apply the circle filter at all" and is NOT the federation switch of the same name (that is `circle_sql.PeerScope`).
- Migration `pc20260829_documents_fts` adds the GENERATED multilingual `documents.search_vector`
  (`build_generated_tsvector_expression` over `generated_title || title || filename`) + a GIN index built
  `CONCURRENTLY`.
- Frontend: the `/knowledge` (+ `/wissen/dokumente`) search box drives it debounced
  (`useKnowledgeDocumentsQuery({q})`), suppressed only under the unified workspace's `everything` omniscope.
- **PR2 (deferred):** federated document search across Paperless.

### Per-fact tier override — UI and scope

- A `document_fact` can carry a tier independent of its parent document (e.g. a public issuer on an
  otherwise-private document).
- The Wissen detail drawer's TierPicker sets the override and surfaces a reset action
  (`AtomService.reset_fact_tier`, `POST /api/atoms/documents/facts/{id}/reset-tier`, owner-only); `FaktenPanel` shows
  a read-only override marker.
- Carry-over across re-extraction needed no new column (it reuses `tier_overridden`); before it, a re-ingest /
  re-OCR silently reset a deliberate override to the document tier.
- The per-document advisory lock (`_reindex_lock`, NS `0x5341`) exists so two overlapping re-extractions of one
  document cannot both leave their new set behind (duplicate facts).
- A re-extraction whose LLM step failed (`SchichtAResult.error`: outage / timeout) holds only the deterministic
  subset. Since 2026-10-31 the hook then KEEPS the prior fact set, generated title and document date (WARNING
  `Schicht A: doc N re-extraction incomplete; kept the existing K fact(s)…`, id and count only) instead of replacing
  them with that subset; before, a reindex during an outage downgraded the document for good. A FIRST extraction
  with an LLM error still stores the deterministic facts (nothing to keep). The next successful reindex refreshes.
- Since 2026-11-01 that keep is limited to the LLM facts, title and date: the deterministic layer DID run on the new
  text (a reindex after audit re-OCR / VLM fallback exists to get cleaner text), so the prior `source='deterministic'`
  facts are replaced by the fresh ones (`_refresh_deterministic_facts`). A fresh fact with the same
  `_fact_identity_key` as a prior one keeps that ROW unchanged (id, atom, tier, per-fact override) instead of
  delete + insert; an unmatched fresh fact is written at the document tier, an unmatched prior one purged after the
  commit. WARNING `… kept N LLM fact(s), title and document date; deterministic facts: K kept, A added, R removed`
  (id and counts only). An EMPTY fresh deterministic set keeps everything, like a successful run with zero facts
  (owner decision 2026-11-01): WARNING `… re-extraction incomplete and found no deterministic facts; kept the existing
  K fact(s), title and document date`. Before, the old regex identifiers from the possibly garbled text survived even
  when the new text read different ones, and a document whose LLM step always failed stayed frozen.

### Obligation notifier + weekly digest — background

- Design follows the cross-model learning `schicht-a-obligations-source-of-truth`: obligations ARE the scheduling
  source of truth — no `Reminder` rows, no reuse of the chat-reminder loop.
- The notifier was originally scheduled by `_schedule_obligation_deadline_notifier`, the digest by
  `_schedule_obligation_digest` and the calendar sync by `_schedule_obligation_calendar_sync`; all three have since
  moved onto the Scheduled-Tasks engine (`docs/design/scheduled-tasks.md`), each re-asserting its gate in-handler.
- Notifier (`OBLIGATION_NOTIFIER_ENABLED`, also needs `PROACTIVE_ENABLED`): one daily idempotent, owner-targeted scan,
  `run_at_boot`, per-user advisory lock. `current_milestone(days_until)` returns the single current bucket
  (`14d`/`7d`/`3d`/`1d`/`due`/`overdue`); each fires once via
  `NotificationService.process_webhook(target_user_id, privacy="personal")`. Scan window
  `[today − OBLIGATION_NOTIFIER_OVERDUE_GRACE_DAYS, today + 14d]`.
- Digest (`OBLIGATION_DIGEST_ENABLED`, also needs `PROACTIVE_ENABLED`; `services/obligation_digest.py`, weekly,
  `run_at_boot`, per-user advisory lock ns `0x4F44`): deduped by a `(user, period_key)` row in
  `obligation_digest_log`.
- Calendar auto-push: see `docs/OBLIGATION_CALENDAR_SYNC.md`.
