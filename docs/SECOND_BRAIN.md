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

1. Parallele Vektorsuche gegen alle vier Quell-Tabellen mit der Query-Embedding.
2. Jede Quell-Tabelle liefert ihr Top-*k* unter Berücksichtigung des Circle-Filters (`services/circle_sql.build_filter`).
3. RRF berechnet einen kombinierten Rang-Score pro Atom anhand der Position in den jeweiligen Ergebnislisten.
4. Die fusionierte Top-*n*-Liste wandert zurück zum Aufrufer, angereichert mit Source-Metadaten (Dokumenttitel, Entity-Label, Memory-Kategorie).

Der API-Endpunkt `/api/atoms` und die `/brain`-Frontend-Seite exponieren genau diesen Weg.

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

**Chat-Memory** läuft analog, aber als Hintergrund-Task nach jeder Chat-Response (`_extract_memories_background` in `chat_handler.py`). Eine mehrstufige Gate-Kette (Stage 1–4) entscheidet, ob eine Konversation memorable Fakten enthielt; wenn ja, extrahiert ein LLM-Call die Fakten und legt sie als `conversation_memories` an — wieder mit Atom-Registrierung und Tier-Zuweisung.

**KG-Extraktion** läuft sowohl bei Dokument-Ingest (als Hook) als auch bei Chat-Memory-Ingest. Derselbe LLM-Prompt, unterschiedliche Quell-Kontexte. Entity-Deduplizierung per Cosine-Similarity (Embedding-basiert) verhindert das Anlegen von `Eduard van den Bongard` und `Eduard` als zwei Entitäten.

---

## Tier-Defaults und Tier-Review

Neue Atome erhalten einen **Default-Tier** — aktuell `2` (household) bei Dokument-Uploads, `1` (trusted) bei KG-Entities aus Chat-Memories. Die Defaults sind bewusst eher einschränkend: was nicht ausdrücklich geteilt wurde, bleibt nah am Eigentümer.

`/brain/review` listet Atome, die der Eigentümer neu klassifizieren sollte — neue Uploads, Entities mit Tier-Konflikten zwischen Relationen, Memories die ein Gate knapp passiert haben. Der Eigentümer kann dort batch-weise Tiers setzen; die Tier-Cascade propagiert auf incidente Relationen.

---

## Federation — Zweite Gehirne, die sich begegnen

Zwei paarweise verbundene Renfield-Instanzen können Queries über die Circle-Grenze schicken: Nutzer A auf Maschine M1 fragt, Nutzer B auf M2 antwortet aus seinem Second Brain — aber nur mit Atomen, für die B's Circles den Leseranger A enthalten. Details siehe [`FEDERATION_MULTI_PEER.md`](FEDERATION_MULTI_PEER.md). Der Circle-Filter läuft dabei **auf der Responder-Seite** — A bekommt nie zu sehen, was er nicht sehen darf, nicht weil M1 filtert, sondern weil M2 gar nichts anderes zurückgibt.

---

## Daten-Besitz

Atome gehören immer **genau einem** `owner_user_id`. Es gibt keine geteilten Atome ohne expliziten Grant — das Modell kennt keinen „shared folder" mit Eigentum-am-Ordner. Das hält die Verantwortlichkeit scharf: wer ein Atom löscht, löscht *sein* Atom; was Mitglieder niedrigerer Tiers davon sehen, war nie *ihres*.

Konsequenz: bei User-Löschung werden alle Atome des Nutzers kaskadierend gelöscht. `AtomExplicitGrant`-Einträge zu anderen Nutzern ebenso. Dort wo KG-Relations auf gelöschte Entitäten zeigen, werden sie mit-abgeräumt.

---

## Siehe auch

- [CIRCLES.md](CIRCLES.md) — Zugriffsebenen-Modell, Datentabellen und Retrieval-Filter im Detail
- [FEATURES.md](FEATURES.md) — Einzel-Feature-Beschreibungen (RAG, Memory, KG)
- [ACCESS_CONTROL.md](ACCESS_CONTROL.md) — RPBAC-Schicht darunter (Authentifizierung + Rollen)
- [FEDERATION_MULTI_PEER.md](FEDERATION_MULTI_PEER.md) — Cross-Instance-Queries über die Circle-Grenze
