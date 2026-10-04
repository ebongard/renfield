---
paths:
  - "src/backend/services/pdf_split*.py"
  - "src/backend/workers/pdf_split_worker.py"
  - "src/backend/api/routes/pdf_split*.py"
---
# PDF-Split

Loaded only when a PDF-split file is read. Long form: `docs/design/pdf-split.md`.
`PDF_SPLIT_ENABLED` — opt-in / dark by default. Multi-document batch scans are detected + split at ingest as a
document-worker **pre-stage before Docling**, so ALL batch entry points are covered without new upload paths.

## Never
- **Page count is NEVER a gate, cap or signal.** Batches are arbitrary mixes of 1-page docs and long contracts;
  boundaries come from content evidence via ONE strict-JSON text-model call, context-window-batched by
  `PDF_SPLIT_WINDOW_CHARS`. The VLM lane likewise has deliberately NO page cap — cost is bounded by a per-call
  timeout, not by skipping pages.
- **Approve NEVER splits in the API pod.** It parks `split_pending` + enqueues; the worker replays the stored plan.
- **Never re-run the boundary LLM on resume.** A confident split persists its plan as an *approved*
  `pdf_split_proposals` row **BEFORE** executing; crash-resume replays it verbatim (the LLM is nondeterministic).
- 🛑 **Der archivierte Elternteil verliert Abschnitte UND Fakten** (#1374). Die Abschnitte wurden immer abgeräumt,
  die Schicht-A-Fakten nicht — bei einem Posteingang aus neun Schreiben sind sie zusammengerührt und liegen daneben
  noch einmal richtig bei den Kindern (gemessen an doc 460: 21 gegen 157). 🛑 Das Abräumen läuft über
  `AtomPurgeService.purge`, **nie** über ein direktes DELETE: die Faktenzeile hängt per CASCADE am Atom, und dort
  sitzt die `legal_hold`-Unterscheidung für `wb_field_provenance` (`test_no_direct_atom_delete.py` verbietet den
  kurzen Weg). Scheitert ein einzelner Purge, läuft der Schnitt weiter — die Kinder sind dann schon materialisiert.
- 🛑 **Die KG-Beiträge des Elternteils bleiben und sind nicht identifizierbar** — `kg_entities`/`kg_relations`
  führen keine Dokumentspalte, `source=doc:N` ist eine Protokollzeile, und `atom_id` zeigt auf die eigene Zeile,
  nicht auf das Dokument. Der Schaden ist Doppelzählung (`mention_count`), nicht falsches Wissen. Eine
  Herkunftsspalte ist Voraussetzung für #875 (expire statt delete) und dort zu entscheiden.
- **Never let the archived original come back.** The combined original is archived (`split_archived`, chunkless,
  Paperless-settled) behind a **flag-INDEPENDENT** worker guard + a reindex-409 — neither a flag-off rollback nor a
  reindex click may resurrect it.

- 🛑 **Eine LUECKE wird repariert, eine UEBERLAPPUNG nie** (#1368). Das Modell liess bei einem duplex eingezogenen
  Posteingang die leeren Rueckseiten weg (38 Seiten, 9 erkannte Dokumente, Seiten 6/12/18/20/30/34/38 fehlten) — die
  exakte Pruefung verwarf daran **alle neun** Grenzen und fiel auf „ein Dokument" zurueck. Bei einem Duplex-Stapel
  ist das der Normalfall. `validate_boundaries(..., absorb_gaps=True)` schlaegt uebersprungene Seiten dem
  **vorangehenden** Dokument zu — genau so, wie der Prompt es dem Modell vorschreibt. Der Prompt war NICHT die
  Ursache und wurde nicht angefasst; er sagt es bereits in beiden Sprachen, das Modell haelt sich nicht daran.
- 🛑 **Die geschlossene Lücke ist auf `_MAX_ABSORBED_GAP` = 2 Seiten begrenzt.** Unbegrenzt war sie ein Datenfehler:
  gemessen ergab `(1,2),(30,38)` über 38 Seiten `1-29, 30-38` — aus einem zweiseitigen Brief wurde ein 29-seitiges
  Dokument, 27 Seiten fremder Post lagen unter dessen Titel. Eine Warnung NACH der Ablage repariert keine Ablage.
  Zwei Seiten fangen ein beidseitig leeres Trennblatt; darüber fehlt ein ganzes Dokument, und dann ist der Rückfall
  „ein Dokument" richtig — er ist sichtbar falsch, eine falsche Zuordnung sieht wie ein geglückter Schnitt aus.
- 🛑 **`absorb_gaps` ist OPT-IN und gilt nur dem Modell-Pfad.** Die Freigabe durch den MENSCHEN
  (`pdf_split_proposals`, `/api/pdf-split`) bleibt streng und antwortet mit **422** — eine still reparierte
  Handeingabe verbirgt den Fehler dessen, der sie gemacht hat. Ein fehlender Kopf und jede Ueberlappung bleiben
  auch mit Reparatur ein Grund zum Verwerfen. Jede Reparatur wird protokolliert.

## Decision path
- Whole-file confidence gate `PDF_SPLIT_AUTO_THRESHOLD`: confident → auto split; uncertain → OWNER REVIEW on
  `/brain/review` (`pdf_split_proposals` + `/api/pdf-split`).
- Review resolution is durable via a conditional UPDATE. Reject = permanent treat-as-single, consulted by detection.
  A same-action retry idempotently re-enqueues (the Redis-blip strand recovery). NULL-owner proposals are resolvable
  by admins.
- Garbled scans → the dedicated VLM split lane (`workers/pdf_split_worker.py`, stream `renfield:tasks:pdfsplit`, row
  heartbeat `documents.split_heartbeat_at`, replicas:1). Routed **ONLY while its worker heartbeat is fresh AND
  `OLLAMA_VISION_MODEL` is set**, else status-quo single ingest. EVERY unreadable page is VLM-transcribed.
- **Fail-safe = hand back as ONE document.** Flag-off parks the lane's backlog in the PEL.
- Transient failures raise `SplitTransientError` → the worker's PEL-retry (not the fail-safe).

## Children
- Children re-enter via `folder_ingest.ingest_document` (dedup / tier / Paperless / enqueue for free).
- Filenames are deterministic and parent-hash-prefixed — they are the **resume keys**; do not change the scheme
  casually. Lineage is `split_from_document_id`.
