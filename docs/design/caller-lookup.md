# Caller lookup: who is calling? (builds on #1240 contact points)

**Status:** draft v3, 2026-11-26. No go to build yet.
**Depends on:** [`kg-contact-points.md`](kg-contact-points.md) (#1240). That design defines where
numbers live: `kg_entity_contact_points`. This one defines how a caller number is **resolved**, plus
the sources and amendments #1240 needs for that. It deliberately adds **no table of its own for
numbers**.
**Related:** `docs/sip-integration-plan.md` (telephony gateway, invariants).

## 1. Goal

An incoming call (`incoming_call` / `voicemail`) shows who is calling, per recipient:
"Anruf von Dr. Beispiel (0170 …) an 02161 …". The name comes from the contact points of KG
persons and organizations, circle-filtered for the recipient.

## 2. Invariants

1. Resolution happens in Renfield, never in the DMZ gateway (numbers only).
2. CallerID can be spoofed. A name is a **hint, never an identity**:
   - it grants nothing (Phase 5: `telephony_guest`);
   - the UI marks it as "nicht geprüft";
   - it never goes into TTS or an LLM prompt (contact names are untrusted text).
3. The raw number is always shown next to the name.
4. Resolution is per recipient: one lookup in the existing fan-out loop, with `user_id = recipient`.
5. Data leaves the instance only through the external reverse lookup (opt-in, off by default).
6. Dark by default. With the flag off, the notification text stays byte-identical.

## 3. Measured starting point (2026-11-26, counts only)

| | household | xidra |
|---|---|---|
| live person/org entities with a number in `description` | 4-5 | 7-8 |
| documents with a phone-like string | 217 / 420 | 420 / 587 |
| of those with an `aussteller` fact | 210 | 408 |

The KG has no place for numbers today. The document corpus already holds them.

## 4. Lookup (`services/caller_lookup.py`)

```
resolve(raw, recipient_id):
  normalize (phonenumbers, CONTACT_POINTS_DEFAULT_REGION); withheld/invalid -> none
  SELECT … FROM kg_entity_contact_points cp JOIN kg_entities e ON e.id = cp.entity_id
   WHERE cp.normalized = :e164 AND cp.kind IN ('phone','mobile') AND cp.valid_to IS NULL
     AND e.is_active AND e.canonical_id IS NULL
     AND contact_points_circles_filter(recipient)  AND kg_entities_circles_filter(recipient)
  rank: manual > vcard/contacts > chat > document; confidence; created_at
  person + its arbeitet_bei organization -> one hit (the person)
  >1 distinct entity at top rank -> "A oder B" (max. 2, then "und weitere")
  miss -> offline geocode hint ("Festnetz Mönchengladbach" / "Mobilfunk")
```

- **Integration:** `build_notification` (`telephony_client.py`) does not depend on the recipient, so resolution moves into the per-user loop of `notify_users`.
- **Failure handling:** a timeout of about 200 ms, wrapped in try/except. Any failure falls back to the plain number. It never raises, because a raise would trigger a retry and duplicate notifications.
- **Source label:** a weak source is named in the text, e.g. "Stadtwerke (laut Rechnung)".
- **Snapshot:** the name is stored in the notification as a snapshot. A rejected contact point clears it in open notifications for that number.

## 5. Sources (extends #1240 §8)

#1240 already covers four sources: vCard upload (stage 2), chat (3), documents (4) and mail senders (4). Caller lookup adds three:

- **5.1 Address-book sync: iCloud CardDAV and Exchange.**
  - This is the extension #1240 §8.1 calls "later", using the same mapping as the vCard import.
  - A new MCP server, `renfield-mcp-contacts`, runs as its own deployment and holds the credentials.
  - Its tool `contacts_changed(account, sync_token)` is **not** in any agent role. Only the backend sync calls it.
  - Each `contacts_account` has exactly one `owner_user_id` and one tier:
    - a personal address book gets `self`;
    - a shared family address book gets household tier, owned once by its owner;
    - the Exchange GAL gets team tier on xidra.
  - `contact_links(account_id, vcard_uid, entity_id, etag)` makes re-syncs idempotent. It also covers cards without a number, renames, merges and deletions. A deleted card ends its contact points with `valid_to`; the entity stays.
  - Trigger: periodic, with a CardDAV sync token, is an exception to the no-polling rule and needs the owner's approval. The alternative is manual "jetzt abgleichen" only.
- **5.2 Manual "Wer war das?"** in the notification bell. You assign the number to an existing or new person or organization. This writes `source_type=manual`, which beats every automatic source.
- **5.3 External reverse lookup.**
  - Runs only on a miss, asynchronously, and never on the incoming-call path.
  - Results go to a Redis cache (30 days), **not into the KG**. Only a manual assignment turns one into a contact point.
  - Sends the bare number and nothing else.
  - Provider still open: tellows (spam score) or a phone-book reverse search. Terms and API are still to be checked.
  - On xidra this needs a data-processing agreement (Art. 28 DSGVO).

## 6. Amendments to #1240 (from the 2026-11-26 architecture review)

These apply to #1240 itself, not only to caller lookup. **They need a decision before #1240 stage 1.**

| # | #1240 today | Amendment | Reason |
|---|---|---|---|
| A1 | `circle_tier = LEAST(entity, source)` stored; cascade "for `kg_node`" | Either the cascade **also** runs on a `kb_document` tier change (into the contact points of that document), or the document tier is derived live through a `document_id` FK join | `AtomService.update_tier` today cascades documents only into chunks and facts, and entities only into relations. A stored MIN without a document cascade goes stale as soon as a document is made more private. That is exactly the drift `kg_edge_tier.py` was built to stop. |
| A2 | `source_ref TEXT` (message_id / document_id / …) | Real nullable FKs `document_id` (ON DELETE CASCADE) and `contact_link_id`; keep `source_ref` for the rest | Re-extraction and deletion of a document otherwise leave orphan values behind. |
| A3 | Merge not mentioned | Add a step in `merge_entities` under FOR UPDATE: reparent, fold unique-index collisions (a rejected or ended value wins), repoint `contact_links.entity_id`; pin it with a merge-suite test | Without it, contact points stay on the tombstone. |
| A4 | §7: "attach via `resolve_entity`" | Use a **counting query** (exact name plus surface forms, same type, same owner): exactly 1 → attach, 0 → create, >1 → review list | `resolve_entity` returns `.first()` across tiers and bumps `mention_count` and the description. A sync would inflate both. |
| A5 | §7: shared values never merge | Unchanged, and stated more sharply: an address-book card with a number that already exists elsewhere attaches only if the names pass `_names_related`; otherwise it becomes a new entity plus a review proposal | Person guard: a family landline or a company switchboard must not glue different people together. |
| A6 | §8.3: documents via the KG document path (LLM field) | Keep it, plus three guards: the number must stand in the letterhead or footer (not the body); document values never count as "confirmed", they rank below vCard and manual; no number shared across several issuers | Collection agencies, property managers and law firms put their own number on letters written for their client. Repetition is not independent evidence. |
| A7 | R5: soft-delete needs an explicit cascade | Unchanged; the caller lookup additionally filters `is_active AND canonical_id IS NULL` | Tombstones must never match. |

## 7. Flags

| Flag | What it gates |
|---|---|
| `CALLER_LOOKUP_ENABLED` | resolution in `telephony_client` |
| `CONTACTS_SYNC_ENABLED` | CardDAV/Exchange sync and its MCP server |
| `CALLER_LOOKUP_EXTERNAL_PROVIDER` | external lookup; empty means off |

The flags for the #1240 sources stay in #1240.

## 8. Order

1. Stage 1 of #1240 with amendments A1-A5, A7: table, filter, tier, merge, manual edits.
2. The caller lookup on top of it (§4), plus "Wer war das?" (§5.2). Useful right away, because every manual assignment improves the next call.
3. Stage 2 of #1240: vCard import. Then the CardDAV/Exchange sync (§5.1).
4. Stage 4 of #1240: documents with A6, plus a backfill over the existing documents.
5. External lookup (§5.3).

## 9. Open questions

- Exchange: Microsoft 365 (Graph) or on-prem (EWS)?
- Phone: iPhone with iCloud contacts, or Android (Google would be a third backend)?
- Sync trigger: periodic (an exception to the no-polling rule) or manual?
- A1: stored tier plus document cascade, or live derivation?
- Provider for the external lookup.
