# LLM-driven Paperless Metadata Extraction

**Status:** Design proposal, revision 3.1 (2026-04-22, implementation-ready)
**Owner:** evdb
**Related:** [`services/chat_upload_tool.py`](../../src/backend/services/chat_upload_tool.py),
[`renfield-mcp-paperless/server.py`](https://github.com/ebongard/renfield-mcp-paperless/blob/main/renfield_mcp_paperless/server.py),
[v2.1.0 release notes](../../CHANGELOG.md#v210--2026-04-22)

---

## Problem

The archive flow for chat-attached files has three user-visible outcomes that
matter and one the current implementation delivers:

| Outcome | Current behavior |
|---|---|
| Document title | Mostly filename, sometimes agent-suggested. OK. |
| Korrespondent, Dokumenttyp, Tags | Nothing is passed at upload. Paperless's post-upload regex / ML classifier has to guess. |
| `storage_path` (Speicherpfad) | Never set. Every document lands in Paperless's root. User manually moves it later. |

The third one is the one users feel. "Der Schrieb vom Finanzamt soll in
`/steuer/2025/`" is what the user actually wants. Today that requires three
clicks in the Paperless UI after every upload. Paperless's own auto-matcher
can match a regex on the OCR'd text, but storage-path rules in particular
need training data the user rarely supplies consistently.

Two deeper weaknesses in the current path:

1. **The LLM does nothing.** The agent just reasoned its way to "user wants
   to archive this" and has full access to document understanding, but hands
   off to a downstream regex engine. The reasoning gets thrown away.
2. **Paperless's ML classifier is post-hoc.** It classifies after OCR, and
   only from training data the user has manually labeled. Cold-start is bad
   and corrections feed back slowly.

The fix: extract metadata at upload time using the LLM, pick values from the
existing Paperless taxonomy, and make storage_path a first-class field. Treat
Paperless's own classifier as a third-line fallback for whatever the LLM
couldn't fill.

## Current flow (for reference)

```
chat attachment
    │
    ▼
internal.forward_attachment_to_paperless(attachment_id [, title])
    │   (src/backend/services/chat_upload_tool.py)
    ▼
read bytes from ChatUpload.file_path
    │
    ▼
mcp.paperless.upload_document(title, file_content_base64, ...no metadata...)
    │   (renfield-mcp-paperless/server.py:upload_document)
    ▼
POST /api/documents/post_document/
    │
    ▼
Paperless consume queue → OCR → ML classifier → partial metadata
```

`storage_path`, `created_date`, and `custom_fields` aren't exposed by
`upload_document` at all today.

## Proposed flow

Metadata extraction runs inline inside `forward_attachment_to_paperless`,
with a user-confirmation step that's ON by default:

```
chat attachment
    │
    ▼
internal.forward_attachment_to_paperless(attachment_id [, title, skip_metadata])
    │   1. Load ChatUpload bytes
    │   2. Vision-LLM extraction against the live Paperless taxonomy
    │      (text-layer shortcut for clean PDFs / docx / md)
    │   3. Validate every field against the taxonomy
    │   4. Propose new taxonomy entries for user approval (never silent-create)
    │   5. Present extracted metadata to user for confirm
    │       (on by default; agent setting paperless_confirm=false disables)
    │
    ▼
mcp.paperless.upload_document(title, bytes, correspondent, document_type, tags,
                              storage_path, created_date, custom_fields)
    │   (MCP tool issues an internal PATCH for storage_path / custom_fields
    │    after the initial post returns the document id — agent sees one call)
    ▼
Paperless consumes. Fields the LLM filled are honored; any still-null
field gets Paperless's built-in classifier as the fallback.
```

**v1 scope is LLM extraction + Paperless classifier fallback, two tiers.**
The originally-considered kNN-over-existing-Paperless-docs tier is deferred
to v2 behind a metrics gate — see [Appendix: kNN tier, deferred](#appendix-knn-tier-deferred)
for the rationale and the condition under which we'd build it.

## Design decisions

### 1. Inline, not a separate tool

**Decision:** Extraction runs inline inside `forward_attachment_to_paperless`.
A `skip_metadata: bool = false` parameter lets the agent opt out when
the upload is clearly not document-like.

| Option | Pros | Cons |
|---|---|---|
| Inline (picked) | One tool call, deterministic. No "did the agent forget to pass the fields through" failure mode. The happy path is the only path. | Extra ~2 seconds and ~1 k tokens on every upload, even when the user wanted a summary instead of an archive. Mitigated by `skip_metadata` opt-out. |
| Separate `extract_metadata` tool | Agent decides per message. Extraction result is visible to the agent before commit. | Relies on agent sequencing two tool calls correctly. Current local models still fumble this — they call extract, get the result, then forget to pass the fields into the forward call. Silent failure mode. |

Tool-call sequencing reliability is lower than LLM-call cost. Pick the
reliable shape.

**When the agent sets `skip_metadata=true`.** The agent prompt includes
a deterministic heuristic — not a judgement call:

```
Set skip_metadata=true when ANY of:
  (a) file MIME is image/* AND file size < 500 KB
  (b) filename matches (case-insensitive) screenshot|Screen.?Shot|IMG_\d+|photo|meme
  (c) user explicitly says "ohne Metadaten" / "no metadata" in the same turn
Otherwise: skip_metadata=false (default).
```

PDF/docx/txt/md never trigger (a) regardless of size — assumed to be
real documents. The heuristic is implemented in `forward_attachment_to_paperless`
itself as a pre-check that can override `skip_metadata=false` if the
file shape trivially matches (a) or (b) — defensive against the agent
forgetting. User-specified (c) is parsed from the agent's reasoning,
not the tool params.

### 2. Vision-first, Docling fallback

**Decision:** Use a local vision model (`qwen2.5-vl:7b` or similar) as
the primary OCR+understanding path. Fall back to Docling when the vision
model isn't available or when the document is plain text that's cheaper to
handle without vision.

| Option | Pros | Cons |
|---|---|---|
| Vision-first (picked) | Handles image-PDFs, scanned originals, colored backgrounds, watermarks, handwritten annotations. German typed documents (Rechnungen, Mahnungen) from utilities, municipalities, and health insurers routinely have layouts that break Tesseract. | Extra GPU seconds per upload. For 1-5 uploads/day at household scale this is trivial; for high-volume SaaS it wouldn't be. |
| Docling / Tesseract only | Already a backend dep. Fastest on plain typed PDFs. | Quality falls off a cliff on scans, colored layouts, and anything with visual complexity. Silent OCR failures → LLM hallucinations from garbage input. The failure mode is "Korrespondent inferred from OCR noise." |

The cost calculus that justifies text-only OCR is SaaS-shaped. Renfield
runs on a household GPU that already serves vision models for other
features. Use them.

**Text-only shortcut:** when the file is clearly text (docx, md, txt, or a
PDF whose text layer extracts cleanly), skip the vision model — Docling
with the text layer is strictly better on pure text. The service picks the
modality based on file type + PDF text-layer presence.

### 3. Taxonomy: pick from existing; propose (don't silently create) new

**Decision:** The LLM must choose each field from the current Paperless
taxonomy OR return `null`. When the LLM is confident a new entry is
warranted and returns `null` with a `new_entry_proposal`, that proposal
surfaces to the user during the confirm step. The user approves → the
system creates it in Paperless (via new MCP tools `create_correspondent`,
`create_document_type`, `create_tag`, `create_storage_path`) and then uses
it. The user declines → Paperless's classifier handles the null field
post-upload.

Taxonomy is cached 60 s in-process. Cache is invalidated on successful
user-approved creation.

**Why "propose, don't silently create":** correspondents, tags, and
storage paths are long-lived shared state. A hallucinated
`"Stadtwerke Köln GmbH Kundenservice Nord"` entry stays in Paperless
forever and splits future documents from the real `"Stadtwerke Köln"`.
Human-in-the-loop on creation is cheap and catches every hallucinated
proposal before it pollutes.

**Why not "never create":** a household Paperless grows maybe 1–2 new
correspondents per month. Forcing the user to pre-create every one in
Paperless admin before the agent can use it makes the archive flow
second-class for exactly the case where it's most valuable (first invoice
from a new supplier).

Prompt shape:

```text
Use only entries from the current Paperless taxonomy below. If none fits,
return null for that field AND optionally return new_entry_proposals: [
  {"field": "correspondent", "value": "Stadtwerke Köln",
   "reasoning": "Document header names them; not in taxonomy."}
]. Do not invent entries without a proposal.

correspondents: Stadtwerke Korschenbroich, Finanzamt Neuss, Telekom, ...
document_types: Rechnung, Mahnung, Vertrag, Nebenkostenabrechnung, ...
tags: steuer-2025, wichtig, wohnung, ...
storage_paths: /steuer/2025, /wohnung/betriebskosten, ...
```

### 4. Failure mode: fall back to bare upload

**Decision:** When any step in the stack fails, log it and continue with
whatever fields are filled so far. Metadata is enrichment, not a
correctness gate. The user's file must not stay in limbo because the LLM
timed out.

Concrete fallbacks:

| Failure | Behavior |
|---|---|
| Vision model connection timeout | Try Docling on the text layer if available, else upload bare. |
| Both OCR paths fail | Upload bare. Surface in user message: "konnte das Dokument nicht lesen, ohne Metadaten hochgeladen." |
| LLM returns malformed JSON | Log the raw response, upload bare. |
| LLM returns a field not in taxonomy and no `new_entry_proposal` | Drop that field only. Keep the valid ones. |
| Taxonomy fetch fails | Upload bare — no taxonomy, no validation, so no metadata. |
| Paperless `update_document` fails after upload (storage_path patch) | Log, leave user message mentioning the document is in Paperless root. |

### 5. Cold-start-only confirm (first N uploads), then auto-off

**Decision:** The confirm step runs for the first **N = 10** uploads per
user. After that, the system trusts itself — extraction runs silently,
the upload proceeds with whatever metadata the stack filled, and the
user's own corrections in Paperless UI feed the correction feedback loop
as the secondary signal.

**Rationale.** A permanent confirm step has three problems:

1. **Friction > value after calibration.** Once the user has seen 10
   clean extractions, the confirm step is pure tax on every subsequent
   upload.
2. **Metric trap.** Permanent free-text confirm discourages corrections
   (typing German edits in chat is tedious → user accepts wrong data
   → Paperless-UI-edit rate stays artificially low → "v1 metrics look
   fine" → v2 interactive card never ships → user lives with free-text
   confirm forever). Self-defeating loop.
3. **State machine cost.** Permanent confirm requires tracking
   pending-confirm ChatUploads across sessions, timing them out after
   24 h, handling abandoned flows. Cold-start-only with N = 10 is
   bounded and doesn't need that machinery.

Cold-start gives the feedback loop what it needs (10 corrections per
user at minimum), then gets out of the way. The Paperless-UI-edit
sweeper (secondary signal source, PR 4) handles mistakes the silent
post-N flow misses.

**How N is tracked.** A per-user counter on `users.paperless_confirms_used`.
The counter **increments only on successful upload**, in the same
transaction that records the Paperless document id:

```
BEGIN
  INSERT INTO paperless_upload_records (...)
  UPDATE users SET paperless_confirms_used = paperless_confirms_used + 1 WHERE id = :user_id
COMMIT
```

Important cases:
- User says "ja" but the upload itself fails (Paperless down, PATCH all
  three retries exhausted) → counter does NOT advance. The cold-start
  allowance is preserved so the next attempt still gets a confirm.
- User says "nein" / aborts → counter does NOT advance.
- User edits the fields then says "ja" → counter advances on success.

Admin can reset via a config flag for users who want recalibration after
a prompt or model change.

**Opt-back-in.** If the user ever types "zeig mir nochmal was du ablegst"
or similar, the agent re-enables confirm for the next upload. Easy lever
to get trust back.

#### v1 confirm UX: free-text in chat

For the first N uploads, the agent posts a German confirm template and
waits for a free-text response. Accept `ja` / `j` / `ok` / `passt` →
proceed. Accept `nein` / `abbrechen` → abort the upload, delete the
ChatUpload placeholder. Accept anything else → parse as a correction
instruction, re-present the updated fields, re-ask.

Confirm message template:

```
Ich möchte das Dokument so ablegen:

  Titel:          Nebenkostenabrechnung 2025 - Stadtwerke Korschenbroich
  Korrespondent:  Stadtwerke Korschenbroich
  Dokumenttyp:    Nebenkostenabrechnung
  Tags:           wohnung, nebenkosten-2025
  Speicherpfad:   /wohnung/betriebskosten
  Ausstellungsdatum: 2026-02-14

Neu anzulegen:    keine

Passt das so? (ja / nein / was ändern willst du)
```

**Known limitations of the free-text form:**
- No per-field edit affordance (user has to type "ändere den Korrespondent auf Stadtwerke Köln")
- No tree-picker for storage_path
- All-or-nothing approval of new taxonomy proposals

These only apply during the 10-upload cold-start window, so the pain is
bounded. Mitigated further by the interactive card in PR 5 (below).

#### PR 5: interactive confirm card (still conditional, rationale shifts)

A chat-embedded card with per-field controls:
- Inline-edit title
- Tag chips with remove-X buttons
- Storage-path tree picker (fed from Paperless's current tree)
- Per-proposal accept/decline buttons for new taxonomy entries
- One "Alles gut — ablegen" button

Existing infrastructure: Renfield already has Adaptive Cards for the
orchestrator ([#374](https://github.com/ebongard/renfield/pull/374)) —
same rail. The card renders, the user clicks, the backend receives a
structured payload instead of free text.

**When to build.** Because confirm is cold-start-only, PR 5's priority
is **improving the first 10 uploads**, not replacing permanent confirm.
Build it if: (a) cold-start quality signal is noisy because users
rubber-stamp free-text confirms they can't easily edit, or (b) first
impressions matter enough that "type a German correction" is the wrong
first experience for new users.

If the N = 10 window closes cleanly without much correction activity,
PR 5 isn't needed.

## Extraction stack (v1)

Two stages.

### Stage 1: Vision LLM extraction

The whole-document pass. Vision-first for scanned / image-PDFs / colored
layouts; Docling text-layer shortcut when the file is clearly text (docx,
md, txt, or a PDF whose text layer extracts cleanly).

The prompt carries a **pruned view** of the current Paperless taxonomy
plus the three worked examples in [§ Prompt template](#prompt-template).
The LLM returns a structured JSON response matching `PaperlessMetadata`
— every field either a taxonomy hit, `null`, or accompanied by a
`new_entry_proposal`.

**Taxonomy pruning.** A mature household Paperless has 50+ correspondents,
100+ tags, maybe 20 document_types, 30 storage_paths. Sending the full
lists on every call pushes the prompt past 10 k tokens, where local 7-14B
models start losing track of the middle of the context. Pruning rules:

- **correspondents:** top 20 by most-recent use. Rare correspondents drop
  out of the prompt and land as `new_entry_proposals` if the LLM spots
  them in the document — which is the correct long-tail behavior.
- **tags:** top 20 by most-recent use. Same logic.
- **document_types:** include all (typically small, 10-30 entries).
- **storage_paths:** include all (typically small, < 30 entries).

**Recency computation.** Once per taxonomy cache refresh (every 10 min
or on invalidation), fetch:

```
GET /api/documents/?ordering=-modified&page_size=100&fields=correspondent,tags
```

Count correspondent IDs across the 100 most-recently-modified documents;
top 20 IDs by frequency become the pruned correspondent list. Same for
tag IDs. One additional API call per cache refresh. **Never computed
per-extraction** — that would add ~150 ms latency to every upload.

Edge case: brand-new Paperless install with < 100 documents. Fetch
returns everything; pruning is a no-op until the archive grows past 20
entries per dimension.

Validation runs in the extractor service:

1. **Pydantic shape check.**
2. **Fuzzy match layer.** Before the strict taxonomy membership check,
   normalise the LLM's output (casefold + whitespace-strip + Unicode
   NFKC) and run Levenshtein distance ≤ 2 against every taxonomy entry.
   - Exactly one candidate within threshold → silently accept the
     canonical taxonomy spelling. Fixes "Stadtwerke Korschenbroich GmbH"
     → "Stadtwerke Korschenbroich", "finanzamt neuss" → "Finanzamt Neuss",
     and similar near-misses that the LLM routinely emits at ~5-15 %
     rate.
   - Zero candidates within threshold → treat as `new_entry_proposal`
     (not a hallucination to drop).
   - Multiple candidates within threshold → ambiguous; fall through to
     proposal and surface all candidates in the confirm step.
3. **Strict taxonomy membership check** for correspondent / document_type
   / tags / storage_path. After fuzzy match, mismatches become proposals
   or get dropped.
4. **`created_date` range sanity** (10 years past, 1 year future —
   reject OCR errors like 1847 or 2189).
5. **Cap tags at 5;** drop the tail.

### Stage 2: Paperless built-in classifier (fallback)

Whatever Stage 1 left null goes to Paperless as-is. Paperless's
post-upload classifier runs on the OCR'd content and auto-fills any field
still null, using the user's existing regex rules and ML training. This
is zero-cost because it already runs — we're just explicitly positioning
it as the backstop.

## Correction feedback loop

Two signal sources, in order of quality:

### Primary: confirm-time diffs

The confirm step already asks the user "is this right?" If they adjust
a field (either via free-text "ändere den Korrespondent auf X" in v1 or
via card clicks when the interactive surface lands), the diff between
what the LLM extracted and what the user approved is a **clean**
extraction-correction signal:

- Definitionally scoped to extraction ("is this right?" = "did we
  extract it right?"); no confusion with later taxonomy refactors.
- Immediate — no 24 h sweep delay.
- No heuristic filter needed.

Every confirm-with-correction writes a `(document_ocr_text, llm_output,
user_approved_output, source='confirm_diff', created_at)` row into
`paperless_extraction_examples`. The delta fields are what a future
prompt augmentation will learn from.

**What counts as a diff.** Computed field-by-field, **after** the
fuzzy match layer has run. Concretely:

- LLM emits `"Stadtwerke Korschenbroich GmbH"` → fuzzy rewrites to
  `"Stadtwerke Korschenbroich"` (canonical taxonomy) before the confirm
  is shown. User says "ja" unchanged. **No diff row written.** The
  fuzzy correction isn't a user-observed correction, it's
  pre-validation.
- LLM emits `"Telekom"` → fuzzy finds no near-match, emits as taxonomy
  hit `"Telekom"`. User edits to `"Deutsche Telekom"`. **Diff row
  written** with `llm_output.correspondent = "Telekom"` and
  `user_approved.correspondent = "Deutsche Telekom"`.
- LLM emits `null` for storage_path. User fills `"/wohnung/betriebskosten"`.
  **Diff row written.** `null → value` transitions are the highest-signal
  training examples — they capture cases where the LLM couldn't decide.
- User says "ja" without edits to anything post-fuzzy. **No diff row
  written** (empty diff = no learning signal).

Diff detection runs on the committed-to-Paperless field set, not on
every intermediate preview during a multi-turn edit ("ändere X, ok now
ändere Y" → one row at final commit, not two).

### Secondary: post-upload Paperless-UI edits

When the user corrects a document's metadata in Paperless itself after
upload (caught something the confirm step missed, or the cold-start
window was already closed and the upload went through silently), an
hourly sweep captures the change.

Signal is noisier — could be a real extraction correction, could be the
user refactoring taxonomy (splitting a tag, renaming a correspondent).
Two filters to reduce false-training:

- **Time filter:** only count edits within 1 h of upload. Taxonomy
  refactors happen in bulk days/weeks later.
- **No-re-edit filter:** if the same field is re-edited again later,
  treat as taxonomy drift and remove from the examples table.

Rows get `source='paperless_ui_sweep'` so the consumer can down-weight
them vs `source='confirm_diff'`.

### Consumption

Future LLM prompts prepend the 3 most relevant tuples (by document
similarity) as additional in-context examples, capped at 5 total
examples including the 3 seed examples baked into the YAML.

### Scope

- **PR 2:** `paperless_extraction_examples` table + confirm-diff capture.
  Consumption not yet wired — table populates from day 1 so PR 3 has
  real data to work against.
- **PR 3:** prompt augmentation reads from the table.
- **PR 4:** Paperless-UI sweeper.

## Prompt template

Structured-output prompts need worked examples. No examples → local 7B-14B
models hallucinate JSON keys and field shapes roughly 5–15 % of the time
in informal testing. With 2-3 examples, malformed JSON drops to < 1 %.

`prompts/paperless_metadata.yaml`:

```yaml
de:
  system: |
    Du hilfst beim Ablegen von Dokumenten in Paperless-NGX. Lies den
    extrahierten Dokumenttext und wähle die passendsten Felder aus der
    vorhandenen Paperless-Taxonomie.

    Regeln:
    - Verwende nur Werte aus der unten aufgeführten Taxonomie. Wenn nichts
      passt, gib null zurück — erfinde nichts.
    - title: beschreibend und dateisystemtauglich. Format: "<Dokumenttyp>
      <Zeitraum/Bezug> - <Korrespondent>".
    - created_date: das Ausstellungsdatum laut Dokument (Rechnungsdatum,
      Ausstellungsdatum, Vertragsdatum — nicht Zahlungsziel, nicht das
      heutige Datum). Format YYYY-MM-DD.
    - tags: maximal 3, thematisch relevant.
    - confidence: 0.0–1.0 pro Feld, deine Selbsteinschätzung.
    - new_entry_proposals: optional. Nur wenn du sehr sicher bist, dass
      der Wert nicht in der Taxonomie steht und einen neuen Eintrag
      rechtfertigt.

    Antworte ausschließlich mit einem JSON-Objekt.

  user: |
    Paperless-Taxonomie:
    correspondents: {correspondents}
    document_types: {document_types}
    tags: {tags}
    storage_paths: {storage_paths}

    Beispiele:

    ---
    Dokument: "Stadtwerke Korschenbroich GmbH ... Nebenkostenabrechnung
    für den Zeitraum 01.01.2025 - 31.12.2025 ... Gesamtbetrag: 1.842 EUR
    ... Rechnungsdatum: 14.02.2026"

    Antwort:
    {
      "title": "Nebenkostenabrechnung 2025 - Stadtwerke Korschenbroich",
      "correspondent": "Stadtwerke Korschenbroich",
      "document_type": "Nebenkostenabrechnung",
      "tags": ["wohnung", "nebenkosten-2025"],
      "storage_path": "/wohnung/betriebskosten",
      "created_date": "2026-02-14",
      "confidence": {"title": 0.95, "correspondent": 0.98,
                     "document_type": 0.95, "storage_path": 0.85,
                     "created_date": 0.98}
    }

    ---
    Dokument: "Finanzamt Neuss ... Einkommensteuerbescheid für 2024 ...
    Bescheiddatum: 12.09.2025 ... Steuernummer: 123/456/78900"

    Antwort:
    {
      "title": "Einkommensteuerbescheid 2024 - Finanzamt Neuss",
      "correspondent": "Finanzamt Neuss",
      "document_type": "Steuerbescheid",
      "tags": ["steuer-2024", "wichtig"],
      "storage_path": "/steuer/2024",
      "created_date": "2025-09-12",
      "confidence": {"title": 0.92, "correspondent": 0.98,
                     "document_type": 0.94, "storage_path": 0.88,
                     "created_date": 0.99}
    }

    ---
    Dokument: "Schreiner Meier ... Rechnung Nr. 2026-0042 ...
    Einbau Fensterbank Esszimmer ... 380,00 EUR ... Rechnungsdatum:
    18.03.2026"

    (Taxonomie enthält "Schreiner Meier" nicht.)

    Antwort:
    {
      "title": "Rechnung 2026-0042 - Schreiner Meier",
      "correspondent": null,
      "document_type": "Rechnung",
      "tags": ["wohnung", "handwerker"],
      "storage_path": "/wohnung/handwerker",
      "created_date": "2026-03-18",
      "confidence": {"title": 0.9, "document_type": 0.97,
                     "storage_path": 0.7, "created_date": 0.99},
      "new_entry_proposals": [
        {"field": "correspondent", "value": "Schreiner Meier",
         "reasoning": "Rechnungskopf nennt den Korrespondenten eindeutig;
                       nicht in Taxonomie."}
      ]
    }

    ---
    Jetzt das eigentliche Dokument:

    {document_text}

    Antwort:

en:
  # parallel EN version, same structure
```

Prompt validation at load time: assert every example parses as valid JSON
matching the `PaperlessMetadata` pydantic model. Prevents silent prompt
rot.

## Validation

```python
class NewEntryProposal(BaseModel):
    # Proposals are always singletons. Three new tags → three proposals.
    field: Literal["correspondent", "document_type", "tag", "storage_path"]
    value: str
    reasoning: str

class PaperlessMetadata(BaseModel):
    title: str | None = None
    correspondent: str | None = None
    document_type: str | None = None
    tags: list[str] = Field(default_factory=list)
    storage_path: str | None = None
    created_date: date | None = None
    confidence: dict[str, float] = Field(default_factory=dict)
    new_entry_proposals: list[NewEntryProposal] = Field(default_factory=list)
```

Validation at response time, in order:

1. **Parse LLM response** against the pydantic model. Malformed JSON →
   return empty metadata + error flag (falls through to bare upload).
2. **Fuzzy match each taxonomy field** (see [Stage 1 fuzzy match layer](#stage-1-vision-llm-extraction)).
   - Use `rapidfuzz` (~10× faster than `python-Levenshtein`, the Python-ecosystem default for this problem).
   - Casefold + whitespace-strip + Unicode NFKC normalise.
   - `rapidfuzz.distance.Levenshtein.distance(normalised_llm_output, normalised_taxonomy_entry) <= 2` against every live taxonomy entry.
   - One candidate within threshold → silently rewrite to canonical.
   - Zero or multiple candidates → skip to step 3.
3. **Strict taxonomy membership check.** Values that pass step 2 are
   taxonomy hits by construction. Values that didn't pass go to
   `new_entry_proposals` if the LLM scored them high-confidence (>0.6),
   else dropped silently.
4. **Clamp `created_date`** to a reasonable range (10 years past, 1 year
   future). Documents dated 1847 or 2189 are OCR errors.
5. **Cap `tags` at 5 entries.** Drop the tail.

## Implementation

### Confirm flow state machine

The confirm step is the central state-machine of the feature. It must
integrate with Renfield's existing agent loop without requiring new
multi-turn tool continuation infrastructure. The pattern below is
**two cooperating tools + one transient state store** — both tools are
regular synchronous agent tools; the pause-and-wait-for-user-response
happens naturally in the agent loop between them.

#### Two-tool split

```
Agent turn 1
    │
    ▼
Tool call: internal.forward_attachment_to_paperless(attachment_id, skip_metadata)
    │   1. Load ChatUpload, run OCR, call LLM, validate + fuzzy + taxonomy pruning
    │   2. If skip_metadata=true OR extraction fails → call upload_document
    │      directly, return normal result. END.
    │   3. Check user's cold-start counter:
    │      - counter >= N (10) → skip confirm, call upload_document with
    │        extracted fields, return normal result. END.
    │      - counter < N → persist extraction state, return confirm-required.
    │
    ▼
Persist confirm state:
    INSERT INTO paperless_pending_confirms
      (confirm_token, attachment_id, session_id, user_id,
       llm_output, post_fuzzy_output, proposals, created_at)
    VALUES ('<uuid>', ...)
    │
    ▼
Return to agent:
    {
      "success": true,
      "action_required": "paperless_confirm",
      "confirm_token": "<uuid>",
      "preview": {
        "titel": "...", "korrespondent": "...", "dokumenttyp": "...",
        "tags": [...], "speicherpfad": "...", "ausstellungsdatum": "...",
        "neu_anzulegen": [{"field": "correspondent", "value": "..."}]
      }
    }

Agent loop receives action_required="paperless_confirm"
    │
    ▼
Agent emits the German confirm template to the user as its turn output,
mentioning the confirm_token in an internal agent-memory note (not
shown to user). Agent turn 1 ENDS.

[User sees the confirm message. Their next chat message is their response.]

Agent turn 2 (fired by user's response "ja" / "nein" / "ändere X")
    │
    ▼
Tool call: internal.paperless_commit_upload(confirm_token, user_response_text)
    │   1. SELECT FROM paperless_pending_confirms WHERE confirm_token = :token
    │      AND session_id = :session  (ownership scope per #442)
    │   2. If no row found → "sorry, die Bestätigung ist abgelaufen, bitte
    │      noch einmal hochladen" (user walked away, state was swept)
    │   3. Parse user_response_text:
    │      - "ja" / "j" / "ok" / "passt" → proceed with post_fuzzy_output
    │      - "nein" / "abbrechen" → delete ChatUpload bytes + pending-confirms
    │        row, return aborted
    │      - anything else → parse as correction instruction (see below)
    │   4. If proposals approved → call mcp.paperless.create_correspondent /
    │      create_document_type / create_tag / create_storage_path as needed
    │   5. Call mcp.paperless.upload_document with final fields (may include
    │      storage_path → triggers internal PATCH in the MCP tool)
    │   6. If upload succeeded → compute diff(post_fuzzy_output, final_fields);
    │      if non-empty, INSERT INTO paperless_extraction_examples
    │      (source='confirm_diff')
    │   7. If upload succeeded → increment users.paperless_confirms_used
    │   8. DELETE FROM paperless_pending_confirms WHERE confirm_token = :token
    │   9. Return normal completion result
    │
    ▼
Agent turn 2 ENDS. User sees "abgelegt als ..." success message.
```

#### Correction parsing in the commit tool

When `user_response_text` isn't a clean "ja" or "nein," parse it with a
small LLM call (same classification model, cheap) that returns a
structured edit:

```json
{
  "action": "edit",
  "changes": {"korrespondent": "Stadtwerke Köln"},
  "approve_after_edit": true
}
```

If `approve_after_edit: true`, apply the edits and proceed to step 4-7
above. If the user's message implies "show me again first" (e.g. "wart,
ich will nochmal schauen"), set `approve_after_edit: false`, update the
pending_confirms row, and return another `action_required="paperless_confirm"`
with the updated preview. Up to 3 edit rounds before the tool forces a
binary "ja oder nein?" prompt to prevent indefinite loops.

#### Transient state table

```sql
CREATE TABLE paperless_pending_confirms (
    confirm_token      VARCHAR(36) PRIMARY KEY,       -- uuid4
    attachment_id      INTEGER NOT NULL REFERENCES chat_uploads(id),
    session_id         VARCHAR(64) NOT NULL,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    llm_output         JSONB NOT NULL,                -- raw LLM dict
    post_fuzzy_output  JSONB NOT NULL,                -- after fuzzy + validation
    proposals          JSONB NOT NULL,                -- new_entry_proposals[]
    edit_rounds        INTEGER NOT NULL DEFAULT 0,    -- prevents infinite loop
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX paperless_pending_confirms_session_idx
    ON paperless_pending_confirms (session_id, created_at);
```

Migration included in PR 2. Rows live at most 24 h (sweeper in PR 4).

#### Why this shape (not other alternatives)

- **Why not one tool with a blocking "wait for user" primitive?** Renfield's
  agent loop is synchronous-per-tool; each tool call returns a dict and
  the agent loop decides the next action. Adding "pause and wait for
  user response" to the tool machinery is a substantial change to
  agent_service.py and affects every other tool. The two-tool-with-state
  pattern uses only existing infrastructure.
- **Why not `action_required` be a new tool protocol?** `action_required`
  is just a key in the return dict that the agent system prompt tells
  the LLM to handle by emitting the `preview` content as a message and
  waiting for the user's reply. No new loop-level machinery needed —
  the agent prompt does the coordination.
- **Why a token, not attachment_id?** Attachment_id alone would let any
  caller in the same session commit any pending upload. A token scopes
  the commit to the specific extraction that produced the preview.

### New files (Renfield)

```
src/backend/services/paperless_metadata_extractor.py   # Stage 1, validation
tests/backend/test_paperless_metadata_extractor.py
prompts/paperless_metadata.yaml                        # shown above
alembic/versions/XX_paperless_extraction_examples.py   # PR 3 table
```

### Modified files (Renfield)

```
src/backend/services/chat_upload_tool.py   # inline extraction in forward tool
```

### `renfield-mcp-paperless` additions

The MCP server needs five additions. **This is the blocking prerequisite
for everything else** — without these, Renfield-side extraction has
nowhere to send the fields it extracts. Ship first.

1. `upload_document` — accept `storage_path`, `created_date`,
   `custom_fields`; when `storage_path` or `custom_fields` is set, issue
   an internal `PATCH /api/documents/{id}/` after the initial post
   returns the new document id. Agent sees one tool call.
2. `create_correspondent(name: str) -> dict`
3. `create_document_type(name: str) -> dict`
4. `create_tag(name: str, color: str | None = None) -> dict`
5. `create_storage_path(name: str, path: str) -> dict`

All creation tools validate against existing entries to prevent
duplicates (`_resolve_name_to_id` returns an id → reject as "already
exists" instead of creating a second one).

### Database tables

**Table 1 (PR 2):** captures correction signals from day 1 so PR 3 has
real data to work against when it turns on prompt augmentation.

```sql
CREATE TABLE paperless_extraction_examples (
    id                  SERIAL PRIMARY KEY,
    doc_text            TEXT NOT NULL,
    llm_output          JSONB NOT NULL,        -- what the LLM extracted
    user_approved       JSONB NOT NULL,        -- what the user confirmed (after edits)
    source              VARCHAR(32) NOT NULL,  -- 'confirm_diff' | 'paperless_ui_sweep' | 'seed'
    superseded          BOOLEAN NOT NULL DEFAULT false,  -- set by no-re-edit filter (PR 4)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX paperless_extraction_examples_source_idx
    ON paperless_extraction_examples (source, superseded, created_at);
```

The `superseded` flag lets the post-upload sweeper (PR 4) soft-delete
rows when a user re-edits the same document later (= likely taxonomy
drift, not an extraction correction). PR 2 never sets this flag —
confirm-diffs are always trusted.

**Table 2 (PR 2):** per-user cold-start counter. A column on the existing
`users` table suffices; no new table needed.

```sql
ALTER TABLE users ADD COLUMN paperless_confirms_used INTEGER NOT NULL DEFAULT 0;
```

### Security

The same ownership check from [#442](https://github.com/ebongard/renfield/pull/442)
(session-scoped `ChatUpload` lookup) applies. User A cannot trigger
metadata extraction on user B's attachment. The `paperless_commit_upload`
tool additionally scopes the confirm_token lookup by session — a token
leaked across sessions is inert.

### Eval corpus

PR 2 cannot merge without an eval baseline. Sourcing + format + runner:

**Sourcing.** The owner (evdb) provides ~20 of their own archived
Paperless documents — real user data, no privacy-sharing concern. Mix:

- 5× Rechnung (mix of image-scan and native-PDF)
- 3× Mahnung
- 3× Nebenkostenabrechnung
- 3× Steuerbescheid / Finanzamt
- 2× Vertrag
- 2× Handwerker-Rechnung (likely includes a new-correspondent case)
- 2× weird / edge (multi-page, handwritten annotation, unusual layout)

**Storage.** `tests/backend/fixtures/paperless_eval/` is `.gitignore`'d.
Developers pull the corpus from a shared NFS/object-store location
(path documented in `tests/backend/fixtures/paperless_eval/README.md`
— the README itself IS committed). Corpus is NOT in git because:
(a) the documents contain personal data; (b) they're large binaries
that don't belong in source.

For initial development before real corpus is available: 5 synthetic
documents generated with a small LaTeX-or-reportlab script. Enough to
exercise the pipeline, not enough for a quality baseline — real eval
waits for real corpus.

**Ground-truth format.** One YAML file per PDF, same basename:

```
tests/backend/fixtures/paperless_eval/
  nebenkostenabrechnung_stadtwerke_2025.pdf
  nebenkostenabrechnung_stadtwerke_2025.expected.yaml
```

```yaml
# nebenkostenabrechnung_stadtwerke_2025.expected.yaml
correspondent: "Stadtwerke Korschenbroich"
document_type: "Nebenkostenabrechnung"
tags: ["wohnung", "nebenkosten-2025"]
storage_path: "/wohnung/betriebskosten"
created_date: "2026-02-14"
title_match_keywords: ["Nebenkostenabrechnung", "2025", "Stadtwerke"]
# Optional: if this doc SHOULD trigger a new-entry proposal
expected_proposals: []
```

`title_match_keywords` accepts any title containing all keywords
(case-insensitive). Exact title match is too strict because titles are
free-form. Other fields are exact-match.

**Eval runner.** `tests/backend/eval/test_paperless_extraction_quality.py`
marked `@pytest.mark.eval`. **Not run in regular CI** (would require
corpus on build runners + ~5 min runtime). Run manually with
`pytest -m eval` or in a weekly scheduled CI job.

**Metrics.**

```python
# For each document in the corpus:
#   - Run extractor with a simulated Paperless taxonomy fixture
#   - Compare extractor output to .expected.yaml
#   - Record per-field result:
#       correct     — exact match (or keyword-contained for title)
#       near_miss   — fuzzy-distance ≤ 2 from expected but not exact
#                     (happens when the taxonomy fixture doesn't have
#                      the expected value — legitimate proposal case)
#       wrong       — LLM emitted a value that's not correct and not
#                     a plausible proposal
#       missing     — LLM emitted null when expected non-null
#       hallucinated — LLM emitted non-null when expected null
#
# Aggregate:
#   field_accuracy     = correct / (correct + wrong + missing + hallucinated)
#   hallucination_rate = hallucinated / total
#   proposal_accuracy  = correct_proposals / (correct_proposals + spurious_proposals)
```

**Baseline-to-beat.** Before PR 2 merges, run the corpus through the
current bare-upload + Paperless-classifier path. Capture per-field
accuracy. Commit the baseline as
`tests/backend/fixtures/paperless_eval/baseline.json`. PR 2's
extraction pipeline must score at least 15 percentage points above
baseline on `correspondent` and `storage_path` accuracy (the two
highest-value fields) to justify shipping. If it doesn't, either the
prompt or the taxonomy pruning needs more work before merge.

**Regression gate.** Subsequent PRs (prompt tweaks, model upgrades)
re-run the eval; if field accuracy regresses more than 5 pp below the
PR 2 merge score, the PR blocks until investigated.

## Open questions

1. **Vision model availability detection.** Renfield's `llm_client` already
   has per-model routing — can we cleanly ask "does the configured agent
   client have a vision model available" without spawning probe calls?
2. **Multilingual docs.** Most archival docs will be German. What about the
   rare English-language invoice from an international service? Current
   design uses `lang` from the ChatUpload record — works if lang is set.
   If not, default to German and let the LLM handle mixed-language text
   (LLMs are fine at this).
3. **Multi-document files.** A PDF with two unrelated invoices stapled
   together. Out of scope for v1 — treat the whole file as one document.
   Flag as a known limitation in the release entry.
4. **Taxonomy cache invalidation across pods.** 10-minute TTL +
   invalidate-on-write handles the single-pod case. Cross-pod (backend
   pod creates a new correspondent, document-worker pod still has stale
   cache for up to 10 minutes) accepted as v1 limitation. Redis-backed
   cache with pubsub invalidation is the clean fix; note as a deferred
   option if the inconsistency bites.
5. **Abandoned-confirm cleanup** during the N = 10 cold-start window.
   If the user walks away mid-confirm within the first 10 uploads, the
   ChatUpload row sits around. Sweeper deletes pending-confirm uploads
   older than 24 h. PR 4 item — bounded impact because cold-start is at
   most 10 uploads per user.
6. **Concurrent new-entry proposals.** Two extractions in parallel both
   propose "Stadtwerke Köln" independently. Mitigation: re-validate the
   proposal against the live taxonomy at confirm-time (after the first
   approval fires `create_correspondent`, the second confirm's proposal
   resolves against the now-existing entry, no duplicate error).
   Implemented in PR 2 as part of the confirm flow.

## Implementation plan

**PR 1** — `renfield-mcp-paperless` additions (`feat: storage_path +
create-taxonomy tools`):
  - `upload_document` accepts storage_path / created_date / custom_fields
    with internal PATCH follow-up. Retry-with-backoff (3 tries,
    exponential) on the PATCH; surface failure explicitly if all retries
    exhausted.
  - `create_correspondent`, `create_document_type`, `create_tag`,
    `create_storage_path` tools
  - Parallelise the 4 taxonomy fetches in `_ensure_caches` via
    `asyncio.gather` — saves ~350ms on cold cache.
  - Cache invalidation on successful `create_*` — flush the affected
    dimension's cache.

Ships first. Without this PR, Renfield can extract storage_path all it
wants, but the upload API silently ignores it. Everything else builds
on this.

**PR 2** — Renfield-side core (`feat(paperless): inline LLM metadata extraction`):
  - `paperless_metadata_extractor.py` + tests
  - `paperless_metadata.yaml` prompt with the worked examples
  - Inline extraction in `forward_attachment_to_paperless` with
    `skip_metadata` opt-out
  - **Fuzzy match layer** (casefold + strip + Levenshtein ≤ 2) before
    strict taxonomy membership check
  - **Taxonomy pruning:** top 20 correspondents + top 20 tags by recency
    of use; full doc_types + storage_paths
  - **Cold-start confirm (N = 10)** flow + `users.paperless_confirms_used`
    counter
  - **Confirm-diff capture** into `paperless_extraction_examples` table
    (migration included)
  - Pydantic validation, taxonomy-check, fallback chain
  - Paperless taxonomy cache helper (10 min TTL + invalidate-on-write)
  - Progress message: agent emits "Ich prüfe das Dokument..." before the
    confirm appears so the user doesn't think the chat is frozen
  - Explicit user-visible German error messages for each failure mode
    (per the failure-mode table)
  - Re-validate new-entry proposals against live taxonomy at confirm-time
    (handles concurrent-proposal race)
  - **Eval suite:** 20-doc seed corpus, field-level accuracy + hallucination
    rate + proposal correctness, baseline-to-beat is bare upload +
    Paperless classifier alone. **Blocks merge.**

**PR 3** — Prompt augmentation from examples (`feat(paperless): learn from
corrections`):
  - Reads `paperless_extraction_examples` (populated by PR 2 confirm-diffs
    from day 1)
  - Prepends top-3 examples (by document similarity) to future prompts,
    capped at 5 total in-context examples (3 baked + 2 learned, max)
  - Similarity via embedding index on `doc_text`

**PR 4** — Secondary signal + cold-start cleanup (`feat(paperless):
Paperless-UI-edit sweeper + abandoned-confirm cleanup`):
  - Hourly sweep of Paperless-edited documents within 1 h of upload.
    Writes rows with `source='paperless_ui_sweep'`.
  - No-re-edit filter: if the same field is edited again later, mark the
    original sweep row `superseded=true` (treated as taxonomy drift).
  - Sweeper for ChatUploads with pending confirm older than 24 h during
    cold-start window.

**PR 5** — Interactive confirm card (conditional, improves cold-start UX):
  - In-chat card with per-field controls, tag chips, storage-path tree
  - Structured-payload callback instead of free-text
  - Only build if cold-start signal from PR 2 shows users rubber-stamp
    free-text confirms they can't easily edit, OR if first-impression
    quality of the cold-start window becomes a stated concern.

**Target for v1 feature-complete:** PRs 1 + 2 shipped. PR 3 adds learning,
cheap to do once PR 2's examples table is populating. PR 4 is cleanup
and secondary signal. PR 5 is conditional UX polish. The kNN tier (see
appendix) is even further out.

## Appendix: kNN tier, deferred

The original design considered a pre-LLM kNN tier — embed each new
upload, find the k nearest documents already in Paperless, and copy the
dominant metadata pattern when the top-k agree.

**Why it's not in v1:**

1. **Cold start is long at household scale.** At 1-5 uploads/day, it
   takes ~6 months to accumulate enough archive to make kNN usable.
   That's most of the addressable deployment window before we'd know
   whether the feature works at all.
2. **Wrong failure mode on the documents that matter most.** First
   invoice from a new correspondent kNN-matches to whatever utility
   bill looks layout-similar and confidently mis-labels it. Embedding
   2000 chars of OCR captures topic (energy bill), not sender
   (Stadtwerke vs. E.ON). Vision-LLM with taxonomy-in-prompt handles
   this case correctly — it can say "correspondent not in taxonomy,
   here's a proposal." The kNN voter cannot.
3. **"Self-improving" is a SaaS-scale story.** At household scale,
   the archive grows too slowly for the improvement to land within
   the feature's useful life.

**Condition under which we'd build it:**

- v1 has been live for 3+ months with a populated archive (200+
  documents).
- Metrics show Stage 1 LLM latency is the bottleneck for user
  experience (upload-to-confirmed > 5 s p50).
- Correction rate on `correspondent` / `document_type` is low enough
  that kNN voting against existing docs would likely be correct.

**If all three hold**, revisit. The architecture in this doc is
compatible: a kNN stage would slot in before Stage 1 LLM, filling
fields confidently where the vote is clean and falling through to the
LLM for the rest.

Otherwise, don't build it. Simpler is better.

## Reviewer concerns

**Revision 3** incorporated /plan-eng-review + outside-voice subagent
feedback. Key changes from rev 2:

- **Fuzzy match layer added before strict taxonomy check** (§ Validation,
  Stage 1). Local 7-14B models emit near-matches at 5-15% — silently
  dropping them as non-taxonomy would defeat the LLM-extraction premise
  with a string-compare bug. Casefold + whitespace-strip + Unicode NFKC
  + Levenshtein ≤ 2 against every taxonomy entry before membership
  check.
- **Taxonomy pruning** (Stage 1). Full taxonomy at household scale
  pushes prompt past 10 k tokens, where local models lose track of the
  middle. Top 20 correspondents + top 20 tags by recency of use; full
  doc_types + storage_paths (small enough). Rare entries fall out and
  land as proposals, which is correct long-tail behavior.
- **Correction feedback loop rewritten** (§ Correction feedback loop).
  Primary signal is now confirm-time diffs (clean, immediate, no
  heuristic filter), captured from day 1 in PR 2. Secondary signal is
  the Paperless-UI-edit sweep with 1 h + no-re-edit filter, still PR 4.
  Rev 2's sweep-only design conflated "extraction was wrong" with
  "user refactored taxonomy" — caught by outside voice.
- **Cold-start-only confirm (N = 10)** replaces rev 2's
  permanent-until-metrics confirm. Rev 2's design was a self-defeating
  metric trap (permanent free-text confirm discourages corrections →
  UI-edit rate stays low → "v1 metrics look fine" → v2 card never
  ships). Cold-start gives the feedback loop 10 corrections per user
  then gets out of the way. Abandoned-confirm state machine becomes
  bounded to the cold-start window.
- **`storage_path` PATCH retry + explicit user-visible failure.**
  Rev 2 said "log and leave a message." Rev 3 requires 3-try
  exponential backoff and, on final failure, a specific German
  message to the user. Silent partial success is the worst UX for
  archive flows — user trusts what they approved happened.
- **Taxonomy fetch parallelised** via `asyncio.gather` in PR 1's
  `_ensure_caches`. Saves ~350ms on cold cache.
- **Progress message** during extraction ("Ich prüfe das Dokument...")
  so the chat doesn't appear frozen during the 5-10 s compute window.
- **Eval suite blocks PR 2 merge.** 20-doc seed corpus, field-level
  accuracy + hallucination rate + proposal correctness, baseline-to-beat
  is bare upload + Paperless classifier. Without this eval, "is the LLM
  doing better than Paperless alone?" is unanswerable.
- **Signature unifications.** `NewEntryProposal.field` is always
  singular (three tag proposals = three rows). LLM extraction model
  settings key named explicitly (`settings.paperless_extraction_model`,
  falls back to `ollama_vision_model` then `ollama_chat_model`).
- **`paperless_extraction_examples` table moves from PR 3 to PR 2**
  (captures confirm-diffs from day 1). Adds `llm_output` + `user_approved`
  columns for clean diff storage. `superseded` flag set by PR 4's
  no-re-edit filter.

### Revision 2 changes (rev 1 → rev 2, first cold-read)

- Cut the kNN tier from v1 (moved to appendix with explicit revisit
  condition). Rev 1 framed kNN as "cheap and self-improving"; the cold
  read pointed out this is a SaaS-scale property that doesn't hold at
  household scale.
- Flipped PR ordering: `renfield-mcp-paperless` additions ship first
  (PR 1). Without storage_path accepted in `upload_document`, the
  Renfield-side extractor has no sink for the field the user cares
  about most.
- Expanded § 5 (User confirm loop) with v1/v2 split (further revised
  in rev 3 to cold-start-only).

### Remaining tensions not resolved in this revision

- **Cross-pod taxonomy cache consistency.** 10 min TTL +
  invalidate-on-write is the v1 compromise. Redis-backed pubsub
  invalidation is cleaner but requires infra work. Accept corner case;
  revisit if it bites.
- **Strategic miscalibration challenge from outside voice.** The voice
  argued 15 Paperless regex rules + manual setup would deliver 80% of
  value for household scale. User sovereign call: committing to the
  LLM path because regex rules require ongoing human maintenance and
  the LLM approach gets better as models improve. Accepted as design
  direction; not revisited.

Pending: after PR 1+2 ship, revisit this doc with real usage data.
