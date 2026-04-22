# Atoms-Granularität — Chunk-Level vs Document-Level

**Status:** Entwurf, pending Entscheidung
**Kontext:** Lane B/C (v2.0.0) setzte `atoms` als polymorphen Access-Control-Layer über vier Quell-Tabellen. Für `document_chunks` wurde **pro Chunk** ein Atom angelegt — diese Entscheidung steht zur Debatte.

---

## Die Frage

Soll der Access-Control-Layer für Dokumente auf **Chunk-Ebene** (eine Access-Richtlinie pro Text-Passage) oder auf **Dokument-Ebene** (eine Access-Richtlinie für das gesamte Dokument) greifen?

---

## Status quo

```
documents                           ← kein atom_id
    ▲
    │ FK
    │
document_chunks                     ← atom_id NOT NULL, circle_tier
    ▲
    │ FK (atom_id)
    │
atoms (type='kb_chunk')             ← eine Row pro Chunk
```

Ein Dokument mit 50 Chunks erzeugt:
- 1 `documents` Row
- 50 `document_chunks` Rows
- 50 `atoms` Rows

Access-Check pro Retrieval: `document_chunks.circle_tier` + optional `atom_explicit_grants` joint.

`kb_shares_service` übersetzt ein „KB-Share" in **N Per-Chunk-Grants**.

---

## Problem

### 1. Kontext-Verlust bei abweichenden Tiers

Der Hauptkritikpunkt: **Tiers auf Chunk-Ebene zerreißen den Informations-Träger „Dokument".**

Konkretes Beispiel: Rechnung mit 10 Chunks. Chunks 1–5 werden vom Eigentümer auf Tier 2 (household) gesetzt, Chunks 6–10 bleiben auf Tier 0 (self). Ein Household-Member sieht nur die ersten 5 Chunks.

Was der Household-Member sieht: Adresse, Rechnungs-Header, halbe Tabelle.
Was ihm fehlt: Summe, IBAN, wichtigste Zahlen.

Die vermittelte „Information" ist nicht nur unvollständig — sie ist **strukturell falsch**. Der Leser glaubt, die Rechnung gesehen zu haben, hat aber aus Fragmenten einen verzerrten Eindruck rekonstruiert. Das ist schlechter als „keine Sicht".

Analog: ein Geheimnis-Absatz in einem sonst öffentlichen Dokument — eine Leserin auf tier=4 sieht alles außer dem einen Absatz und denkt, die Geschichte sei vollständig.

### 2. Write-Amplifikation

Aktuell pro Dokument-Ingest: 1× Document-Row + N× Chunk-Rows + N× Atom-Rows + N× Atom-Source-ID-Updates.

Für ein 200-Seiten-PDF mit ~600 Chunks: 1801 DB-Writes. Pro Atom 2 Round-Trips (INSERT mit Placeholder + UPDATE mit echter source_id). Das ist spürbar.

### 3. Storage-Overhead

~64 Bytes pro atoms-Row × 600 Chunks × 10k Dokumente = ~380 MB atoms-Tabelle nur für Chunk-Atoms. Nicht tragisch, aber dominant gegenüber den anderen Atom-Typen.

### 4. Feature-Fläche, die niemand braucht

Per-Chunk-Sharing ist Notion-mäßig hübsch („teile diesen einen Absatz"), aber:
- Keine UI exponiert es
- Typischer User-Intent ist „teile dieses Dokument" oder „teile diese KB"
- Die Feature-Fläche wird vom Code getragen, nicht von Nutzern

**Verifiziert gegen Prod-DB (2026-04-22):**

```sql
SELECT
  (SELECT COUNT(*) FROM atom_explicit_grants
   WHERE atom_id IN (SELECT atom_id FROM atoms WHERE atom_type='kb_chunk')) AS chunk_grants,
  (SELECT COUNT(DISTINCT granted_to_user_id) FROM atom_explicit_grants
   WHERE atom_id IN (SELECT atom_id FROM atoms WHERE atom_type='kb_chunk')) AS distinct_grantees;

-- Ergebnis:
--  chunk_grants | distinct_grantees
-- --------------+-------------------
--             0 |                 0
```

Keine Produktions-Abhängigkeit auf dem per-chunk Feature. Die Migration kann die Fläche ohne Aggregationslogik droppen. Der Gesamt-Bestand ist aktuell 123 kb_chunk-Atoms in 123 document_chunks-Rows (1:1, keine Inkonsistenz).

---

## Wo per-Element-Atoms weiterhin Sinn macht

Chunks sind das **einzige** Quell-Schema, bei dem die Atom-pro-Row-Entscheidung fragwürdig ist:

| Quelle | Natürliche Granularität | Bleibt 1-Atom-pro-Row? |
|---|---|---|
| `kg_entities` | eine Entity = eine Person/Ort/Organisation, semantisch atomar | **ja** |
| `kg_relations` | ein Triple (subject, predicate, object), semantisch atomar | **ja** |
| `conversation_memories` | ein extrahierter Fakt, semantisch atomar | **ja** |
| `document_chunks` | Parse-Schnitt, **kein** semantisch atomares Ding | **nein** |

Die Redesign-Entscheidung betrifft **nur** den RAG-Pfad.

---

## Vorschlag: Atoms auf Dokument-Ebene

### Neues Modell

```
documents (type='kb_document')      ← atom_id NOT NULL, circle_tier
    ▲
    │ FK
    │
document_chunks                     ← kein atom_id mehr; circle_tier
                                      denormalisiert aus documents
    ▲
    │ FK (atom_id) — entfällt
    │
atoms (type='kb_document')          ← eine Row pro Dokument
```

### Retrieval-Pfad

Aktuell (Lane B SQL, vereinfacht):
```sql
SELECT ... FROM document_chunks c
WHERE c.atom_id IN (SELECT atom_id FROM <circle-filter>)
  OR c.circle_tier = 4 OR ...
```

Nach Umbau:
```sql
SELECT ... FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE d.atom_id IN (SELECT atom_id FROM <circle-filter>)
  OR d.circle_tier = 4 OR ...
```

Der zusätzliche Join ist billig: `documents.id` ist PK, indexiert. `d.atom_id` ist indexiert (wird es — siehe Migration).

**Alternative ohne Join**: `document_chunks.circle_tier` weiterhin denormalisieren, als Spiegel von `documents.circle_tier`. Update-Trigger auf documents propagiert auf alle Child-Chunks. Dann bleibt der Retrieval-Filter auf `document_chunks.circle_tier` wie heute — nur Ownership/atom_id-Check zieht auf documents um.

Letzteres ist meine Empfehlung: minimaler Eingriff in Retrieval, Tier-Wechsel wird ein Write-Trigger-Kaskadeneffekt (ähnlich wie `AtomService.update_tier` schon für KG-Relations macht).

### `kb_shares_service` wird einfacher

Aktuell: ein KB-Share wird in 1× N Chunks = N Per-Chunk-Grants explodiert.
Neu: ein KB-Share wird in 1× M Documents = M Per-Document-Grants explodiert.

Für typische KBs ist M erheblich kleiner als N. „Share this KB" wird von ~10k AtomExplicitGrant-Rows auf ~100 reduziert.

### Per-Chunk-Feature wird aufgegeben

Die Fähigkeit „einzelne Chunks innerhalb eines Dokuments unterschiedlich teilen" entfällt. Explicit-Grants kann es nur pro Dokument geben.

Das ist der **gewollte** Preis — der Informations-Kohärenz-Schutz, den du gerade beschrieben hast.

---

## Migration

### Phase 1 — Schema

Neue Alembic-Revision nach `pc20260420_circles_v1`:

```python
# Add atom_id to documents
op.add_column("documents", sa.Column("atom_id", sa.String(36), nullable=True))
op.add_column("documents", sa.Column("circle_tier", sa.Integer, nullable=False, server_default="0"))
op.create_foreign_key(
    "fk_documents_atom", "documents", "atoms",
    ["atom_id"], ["atom_id"], ondelete="CASCADE",
)

# Drop atom_id from document_chunks (FK + NOT NULL + index)
op.drop_constraint("fk_kb_chunks_atom", "document_chunks")  # name TBD
op.drop_index("ix_document_chunks_atom_id", "document_chunks")
op.drop_column("document_chunks", "atom_id")
# circle_tier BLEIBT auf document_chunks — denormalisiert von documents
```

### Phase 2 — Backfill

**Pre-Migration-Gate (FAIL LOUD):** Bevor wir die `kb_chunk`-Atoms droppen, sicherstellen dass keine Dokument-Row heterogene Chunk-Tiers hat. Wenn heute nicht in Prod, kann es durch Edits/Imports später auftreten — die Migration muss es als Assertion haben:

```sql
-- Fail if any document has chunks across different tiers — would silently
-- collapse to the most-permissive tier in the backfill below otherwise.
DO $$
DECLARE
    violating_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO violating_count
    FROM (
        SELECT document_id
        FROM document_chunks
        GROUP BY document_id
        HAVING COUNT(DISTINCT circle_tier) > 1
    ) x;
    IF violating_count > 0 THEN
        RAISE EXCEPTION 'Migration blocked: % documents have chunks across heterogeneous tiers. Resolve before upgrade (consolidate tiers manually, or extend migration with MIN-based collapse logic).', violating_count;
    END IF;
END $$;
```

**Back-fill — konservative Collapse-Semantik:**

```sql
-- Eine Atom-Row pro Dokument. Tier wird aus MIN(chunks.circle_tier) abgeleitet,
-- NICHT aus kb.default_circle_tier. Begründung: selbst wenn der Pre-Migration-Gate
-- hetero-tier Chunks akzeptiert hat (falls jemand die Assertion manuell skipped),
-- kollabieren wir conservativ auf den RESTRICTIVSTEN Tier. Jeder Chunk darf nach
-- Migration mindestens so restrictiv gelesen werden wie vorher, nie permissiver.
-- Wenn alle Chunks eines Doks denselben Tier haben (heute 100% der Fall), ist
-- MIN == MAX == der eine Tier.
INSERT INTO atoms (atom_id, atom_type, source_table, source_id, owner_user_id, policy, created_at, updated_at)
SELECT
    gen_random_uuid()::text,
    'kb_document',
    'documents',
    d.id::text,
    COALESCE(kb.owner_id, <admin_fallback>),
    json_build_object(
        'tier',
        COALESCE(
            (SELECT MIN(c.circle_tier) FROM document_chunks c WHERE c.document_id = d.id),
            kb.default_circle_tier,
            0
        )
    ),
    d.created_at,
    NOW()
FROM documents d
JOIN knowledge_bases kb ON kb.id = d.knowledge_base_id;

UPDATE documents d
SET atom_id = a.atom_id
FROM atoms a
WHERE a.atom_type = 'kb_document'
  AND a.source_table = 'documents'
  AND a.source_id = d.id::text;

UPDATE documents
SET circle_tier = (SELECT CAST(policy->>'tier' AS INTEGER) FROM atoms WHERE atom_id = documents.atom_id);

ALTER TABLE documents ALTER COLUMN atom_id SET NOT NULL;

-- Alte kb_chunk Atoms droppen (ON DELETE CASCADE sorgt für saubere Entfernung)
DELETE FROM atoms WHERE atom_type = 'kb_chunk';
```

### Phase 2b — Tier-Cascade-Sicherheit

`document_chunks.circle_tier` **muss** in derselben Transaktion wie `documents.circle_tier` fortgeschrieben werden, sonst entsteht ein **Retrieval-Leak-Window**: Dokument-Tier wurde auf `self` demoted, Chunks tragen noch `household`, der pgvector-Filter sieht die Chunks weiterhin als für Household-Peers lesbar — bis die Cascade nachzieht.

**Pflicht-Implementierung in `AtomService.update_tier` für `kb_document`-Atoms:**

```python
# Siehe existing update_tier pattern für kg_node/kg_edge
async def update_tier(self, atom_id, new_policy):
    # ... existing atom + source row update ...

    if atom.atom_type == "kb_document":
        new_tier = int(new_policy.get("tier", 0))
        # Atomar in derselben Session/Transaktion:
        await self.db.execute(
            text(
                "UPDATE document_chunks SET circle_tier = :tier "
                "WHERE document_id = :doc_id"
            ),
            {"tier": new_tier, "doc_id": int(atom.source_id)},
        )
```

**Pflicht-Test:** Integration-Test der 50× einen mehrfach-Chunk-Dokument-Tier flippt unter 10 parallelen Sessions, danach assertiert:

```sql
SELECT COUNT(*) FROM document_chunks c
JOIN documents d ON d.id = c.document_id
WHERE c.circle_tier != d.circle_tier;
-- MUSS 0 sein
```

**Langfristige Absicherung:** CI-Lint oder Ruff-Regel, die jede Retrieval-SQL daraufhin prüft, dass `document_chunks.circle_tier` nur gelesen wird, wenn `documents` mit gejoint ist. Alternative: eine DB-Trigger auf UPDATE von `documents.circle_tier`, die automatisch die Chunks mitaktualisiert (fail-safe, auch wenn App-Code den AtomService-Pfad umgeht).

Ohne diese Maßnahme ist die Drift-Wahrscheinlichkeit ein Security-Bug, kein Cache-Problem.

### Phase 3 — Code

- `AtomService._table_for_atom_type`: `'kb_document' → 'documents'`, `'kb_chunk'` entfernen
- `RAGService.process_existing_document`: Atom wird pro Dokument (vor dem Dokument-INSERT) erzeugt, Chunks werden unverändert mit `circle_tier` aus KB-Default geschrieben
- `rag_retrieval`: Filter joint über `documents.atom_id` statt `document_chunks.atom_id`
- `kb_shares_service`: KB-Share → Per-Document-Grants statt Per-Chunk-Grants
- `polymorphic_atom_store`: Cross-Source-RRF integriert `kb_document`-Atoms in die Fusion (statt `kb_chunk`)

### Phase 4 — Abgekündigte Feature entfernen

Per-Chunk-Explicit-Grants. Wenn niemand sie nutzt, verlustfrei. Falls produktiv genutzt: Migration müsste sie verdichten (MAX über Chunks pro Dokument → Per-Document-Grant).

---

## Effort-Einschätzung

| Phase | Human Team | CC+gstack |
|---|---|---|
| Alembic-Migration (+ Back-fill + Tests) | 1 Tag | 30 min |
| `AtomService` + `RAGService` Umbau | 1 Tag | 30 min |
| `kb_shares_service` + Retrieval-Pfade | 1 Tag | 30 min |
| `polymorphic_atom_store` + Frontend-Auswirkungen | 2 Tage | 1 h |
| Tests + Manual-E2E + Dogfood | 1 Tag | 45 min |
| **Summe** | **~1 Woche** | **~3,5 Stunden** |

---

## Verifikation — Premissen gegen Prod-DB geprüft (2026-04-22)

### P1 — Per-Chunk-Grants sind unbenutzt

**Gate:**
```sql
SELECT COUNT(*) AS chunk_grants,
       COUNT(DISTINCT granted_to_user_id) AS distinct_grantees
FROM atom_explicit_grants
WHERE atom_id IN (SELECT atom_id FROM atoms WHERE atom_type='kb_chunk');
```

**Ergebnis:** `chunk_grants=0, distinct_grantees=0` gegen Prod. Migration kann die Feature-Fläche risikofrei droppen. Keine Aggregations-Logik nötig.

### P2 — JOIN-Cost gegenüber aktueller Query

**Baseline (heute, ohne JOIN):** 4.15 ms, 957 shared buffer hits.
**Neu (mit JOIN auf documents):** 4.26 ms, 958 shared buffer hits.
**Overhead:** 0.11 ms (2.7 %), 1 zusätzlicher Buffer-Hit.

Postgres wählt Hash Join; die documents-Seite ist 1-Digit-Count in Prod (6 Docs) und hasht in 9 KB. Selbst bei 10k Docs → ~160 KB Hash. Planner-Entscheidung ist stabil, keine Seq-Scan-Degradation zu erwarten.

**Separate Erkenntnis:** pgvector-Retrieval läuft in Prod aktuell OHNE vector index (weder HNSW noch IVFFlat). Seq-Scan bei 123 Chunks ist trivial, aber das skaliert nicht. **Nicht-Blocker für diesen Redesign**, aber eigenes Issue wert.

### P3 — Federation-Wire-Protokoll überlebt atom_type-Rename

`FederationProvenance` (in `federation_query_schemas.py:145`) trägt `atom_id + atom_type` als Display-Metadaten. Responder serialisiert direkt aus dem internen `atom.atom_type`-Feld. Signed-Payload (`complete_canonical_payload`) deckt den String ab — kein Byte-Drift. Asker-Seite hat KEINE Hard-Codings auf spezifische `atom_type`-Werte (geprüft: `federation_query_asker.py`, `atom_store.py`).

**Frontend-Impact:** `BrainReviewPage.jsx:11` (Farbmapping) und `i18n/locales/de.json:1084` + `en.json:1084` (Labels) verdrahten `kb_chunk`. Cosmetic-Rename in 3 Dateien nach Migration; i18n-Label war schon bisher „Dokument" (die Übersetzung log für den Chunk-Fall — wird zur Wahrheit).

### Neu-identifizierte Risks (Claude-Subagent-Review, 2026-04-22)

**Risk A — Tier-Cascade-Atomicity:** addressiert in Phase 2b oben.

**Risk B — Back-fill-Collapse-Correctness:** addressiert durch MIN-basierte Aggregation + Pre-Migration-Gate oben. Prod ist heute konsistent (0 Docs mit heterogenen Chunk-Tiers verifiziert), aber die SQL ist defensive-correct-by-default.

## Offene Fragen

1. **Embedding-Level Retrieval** — geklärt: pgvector-Similarity bleibt per Chunk, Tier-Filter joint auf Documents. Performance ist messbar-vernachlässigbar (P2-Ergebnis).

2. **Legacy-Uploads ohne KB** — geklärt: `documents.knowledge_base_id` ist NOT NULL, gibt es also nicht.

3. **Vector-Index in Produktion** — **separates Issue**: aktuell kein HNSW/IVFFlat auf `document_chunks.embedding`. Seq-Scan ok bei 123 Chunks, nicht bei 10M. Sollte vor der nächsten Größenordnung passieren, aber unabhängig von der Atoms-Granularität.

4. **`chat_uploads` → RAG-Indexierung** — Frontend-Paperclip-Upload → `/api/chat_upload/{id}/index` → RAGService.ingest_document. Funktioniert weiterhin; Atom wird einmal für das entstehende Document angelegt.

---

## Vorschlag zum Vorgehen

**Kurzfristig (heute):**
- #443 ist geschlossen ✓
- #441 + #442 bleiben auf main, werden deployed → KG + Chat-Upload-Ownership greifen in Prod
- **Akzeptierter Bruch:** RAG-Ingest + Memory-Save bleiben im `atom_id NOT NULL`-Fehler hängen, silent warnung. Für Memory (Hintergrund-Task): akzeptabel, aber nicht für RAG-Ingest. Falls RAG-Ingest in Produktion kritisch ist, brauchen wir einen **Zwischenfix**: den ConversationMemory-Teil von #443 **ohne** den document_chunks-Teil mergen. Das unblockt Memory ohne den RAG-Design-Punkt zu berühren.

**Mittelfristig (nächste Session oder zwei):**
- Separater PR für atom-per-document Migration, Phasen 1-4 wie oben
- Ziel-Version: v2.1.0 (bump weil Schema-Change)

**Langfristig:**
- Rückblickend im v3-KG-Migration-Scope (siehe TODOS.md) nochmal evaluieren — ggf. ist `kb_document`-Atoms die richtige Basis dafür.

---

## Entscheidungen, die du treffen kannst

1. **„Zwischenfix Memory jetzt, RAG-Atom-Redesign danach"** → ich spinne ConversationMemory aus #443 raus, neuer kleiner PR, deployed mit #441 + #442
2. **„Erst Design finalisieren, dann alles zusammen"** → RAG/Memory bleibt gebrochen bis Redesign live ist. KG + Ownership gehen raus.
3. **„Redesign zurückstellen, Status quo mergen"** → #443 wiederbeleben, per-chunk atoms akzeptieren, den Konzeptionsbruch dokumentieren und irgendwann in v3 anpacken.
