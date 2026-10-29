---
paths:
  - "src/backend/services/circle_*.py"
  - "src/backend/services/atom_service.py"
  - "src/backend/services/*_retrieval.py"
  - "src/backend/services/polymorphic_atom_store.py"
  - "src/backend/services/kb_shares_service.py"
  - "src/backend/api/routes/atoms.py"
  - "src/backend/api/routes/circles.py"
  - "src/backend/services/rag_service.py"
---
# Circles v1 — access tiers on every retrievable row

Loaded only when a circle / atom / retrieval service is read. Long form: `docs/CIRCLES.md` (model, data tables, full
route + frontend-page inventory), `docs/SECOND_BRAIN.md` (the four subsystems circles protect). Sibling rules:
`documents-facts.md`, `obligations.md`, `notes-wissen.md`.

| tier | name | meaning |
|---|---|---|
| 0 | self | owner-only |
| 1 | trusted | 1-3 closest people |
| 2 | household | family / housemates |
| 3 | extended | named outsiders |
| 4 | public | anyone |

## The access rule — one filter, in SQL
- Access to a source row = **OWNER** OR **tier == public** OR **explicit grant** (`atom_explicit_grants`) OR
  **tier-reach through circle membership** (`circle_memberships`). `rag_retrieval`, `kg_retrieval`,
  `memory_retrieval` push this 4-branch filter into SQL via `services/circle_sql.py` — never add a second, unfiltered
  read path next to it.
- `AUTH_ENABLED=false` short-circuits the filter (single-user mode sees everything).
- **Callers MUST pass `user_id=asker_id`** — to memory retrieval and to every `rag.search()` call. `None` reduces to
  public-tier-only when auth is on (silently: no error, just missing results).
- The document owner-branch in `circle_sql` has an atom-owner fallback so null-KB / global-RAG docs reach their owner.
- Behavioural change vs pre-circles: `ConversationMemoryService.retrieve()` respects circle reach — tier-2 household
  peers see each other's household-tier memories (it used to filter strictly `user_id == asker_id`).

## Der föderierte Zweig (`PeerScope`)
- Föderiert = `peer=PeerScope(owner_id, tier)` bis in `circles_filter_clause`. Owner- und
  Grant-Zweig entfallen; es bleibt `(public) OR (owner = :peer_owner AND :peer_tier <= tier)`.
- 🛑 **Beide Hälften tragen.** Ohne die Eigentümer-Bindung sähe der Peer seine Stufe bei JEDEM
  Eigentümer. Die Stufe steht an der PAARUNG (`peer_users.granted_tier`), nie in `circle_memberships`.
- 🛑 **Im peer-Zweig entsteht KEINE `:asker_id`** — es gibt dort keine fragende lokale Person.
  Wer sie wieder einführt, baut F-ID-3 neu: bis 2026-10-27 war sie `PeerUser.remote_user_id`,
  eine fremde Zahl in einer Spalte mit FK auf `users.id`; das brach x-idras Paarung am
  Fremdschlüssel und band im Haushalt still die falsche Person (BL-0135).
- 🛑 `skill_service.py:443ff` trägt eine ZWEITE, handgeschriebene Kopie des Prädikats — ein
  Umbau hier erfasst sie nicht. Hergang: `docs/design/federation-identity-mapping.md`.

## Writing source rows
- Tables: `atoms` (polymorphic registry), `circles` (per-user dimension config), `circle_memberships`,
  `atom_explicit_grants`. Denormalized `circle_tier` + `atom_id` live on `document_chunks`, `kg_entities`,
  `kg_relations`, `conversation_memories`, `notes`, and — since auth-on §8.1 — `conversations` and `meetings`
  (both `atom_id` NULLABLE there: a row nobody owns has no owner to hang an atom on; see `chat-branching.md`).
- Create a source row via `AtomService.create_with_source`, **never a direct INSERT** (the denormalized columns and
  `atoms.policy` drift apart otherwise). Change a tier only through `AtomService` — it owns the **tier cascade**
  (entity → incident relations; `kb_document` → its facts `WHERE NOT tier_overridden`).
- `services/kb_shares_service.py`: a KB-level share is exploded into per-chunk explicit grants.
- **A membership has a DIRECTION.** `cm.circle_owner_id` keys on the ROW's owner, `cm.member_user_id` on the asker —
  so "A is in B's circle" lets A read B's atoms, never the reverse. A shared artifact owned by a DEVICE account
  (a satellite room history, `chat-branching.md`) therefore needs members inside the DEVICE's circle; the
  reverse row only lets the device read. `bin/backfill_household_tiers.py` writes both.
- `services/circle_resolver.py` = `PolicyEvaluator` + cache, for access checks outside the SQL paths.
- `services/polymorphic_atom_store.py` = cross-source RRF; it emits per-entity `kg_node` + per-relation `kg_edge`
  atoms (`KGRetrieval.get_relevant_atoms`), while the agent's string context (`get_relevant_context`) is unchanged.

## Extraction WRITES are ownership-gated separately from retrieval
`_apply_update_v2` / `_apply_delete_v2` and the v1 contradiction path take the row to mutate from an LLM-supplied
`target_id`, and `services/memory_ops.validate_against_candidates` checks **membership in the candidate set only —
never ownership**. So the apply layer enforces it:
- an identified turn is scoped by `user_id == asker_id` (WHERE clause in v2, `_extraction_target_owned` re-check in v1);
- a turn with **no identity is refused outright while auth is on** (`_identity_scoped_write_denied`) — `user_id` is
  `None` for every device / satellite / unidentified-voice turn, and the candidate filter then degrades to public-tier,
  i.e. other users' rows.
- `AUTH_ENABLED=false` is one trust domain (filter bypassed, every row carries the same `_resolve_owner_user_id`
  fallback owner) and keeps its unscoped behaviour. Test: `tests/backend/test_memory_ownership_guard.py`.

## Das Prüfprotokoll hält die IDENTITÄTSGRUNDLAGE fest
`federation_query_log` trägt seit 2026-10-29 `direction` (`asked`/`answered`) und `identity_basis`
(`mapped`/`peer_tier`). 🛑 Vorher schrieb **nur die Fragerseite** (`mcp_client.py`, ein einziger Aufrufer), und damit
stand nirgends, ob eine Antwort mit der vollen Reichweite der verknüpften Person oder nur auf der gewährten Stufe
zustande kam — beim ersten erfolgreichen Zug war das hinterher nur noch zu erschliessen, weil der Pending-Satz mit
`mapped` im Arbeitsspeicher liegt.
🛑 **Der ANTWORTENDE schreibt es**, nicht der Frager: die Instanz, deren Daten gelesen wurden, verzeichnet den
Zugriff. Ein Vermerk auf der Fragerseite wäre die Behauptung der Gegenseite über ihre eigene Zugriffsentscheidung.
🛑 `user_id` heisst durchgehend „die lokale Person, deren Reichweite benutzt wurde" — gemappt die verknüpfte Person,
im Rückfall der Eigentümer der Paarung. Darum bleibt die Spalte NOT NULL; `identity_basis` ist nullable, weil
Bestandszeilen und Frager-Zeilen es nicht WISSEN und ein Vorgabewert Unwissen als Tatsache ausgäbe.
🛑 **Die Ansicht (`/brain/audit`) darf aus Unwissen keine Behauptung machen.** Fehlt `identity_basis`, zeigt sie
**nichts** — nicht „als fremde Instanz“, denn das unterstellte die ENGERE Stufe für Züge mit voller Reichweite.
Ein unbekannter dritter Wert wird **roh** angezeigt, nie in den `peer_tier`-Zweig geleitet; darum trägt der
TypeScript-Typ `(string & {})` und `basisHelpKey()` einen dritten Zweig.
Die FARBE ist aus demselben Grund dreiwertig (`basisBadgeClass()`): dasselbe Grau wie `peer_tier` waere die
engere Behauptung im Bild statt im Wort, und die Farbe liest man zuerst.
🛑 Der Antwortende fuehrt `peer_display_name` auf dem Pending-Satz MIT — ein Schnappschuss des Zeitpunkts,
kein Nachschlagen beim Schreiben (die Paarung kann spaeter umbenannt oder widerrufen sein). Zeilen vor dem
2026-10-29 tragen dort leer; die Ansicht faellt dann auf den Schluessel zurueck, nie auf einen Platzhalter.
