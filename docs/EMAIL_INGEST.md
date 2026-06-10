# Email Auto-Ingest

New mail lands in a watched IMAP mailbox → its attachments are ingested into the
knowledge base **and** Paperless automatically. The email analog of
[Folder Auto-Ingest](FOLDER_INGEST.md); it reuses the same backend bridge and
adds **per-mailbox, server-authoritative** sphere routing.

> **Status.** **Deployed to production (backend `v2.15.0`, 2026-06-09)** and live
> against `buchhaltung@x-idra.de`. The dedicated watcher
> [`renfield-mcp-email-ingest`](https://github.com/ebongard/renfield-mcp-email-ingest)
> (`renfield/email-ingest-mcp:v0.1.1`) holds the IMAP credentials and pushes
> attachments over REST. Ships behind `EMAIL_INGEST_ENABLED`.

## Architecture

```
renfield-mcp-email-ingest (dedicated, owns the IMAP creds — NOT the backend)
  • watches each mailbox via IMAP IDLE (event-driven, NEVER polling; RFC 2177 renewal)
  • on a new message → extract attachments (attachments only, skip inline images)
  • PUSH each attachment: HTTP multipart to the backend (Bearer + mailbox_id)
  • aggregates the per-attachment results → moves the EMAIL:
        ≥1 ingested → processed_folder ("Verarbeitet") ; failed → failed_folder
        ("Fehler") ; transient → leave UNSEEN + retry ; no docs → mark \Seen
        │  POST /api/email-ingest/document   [multipart: file + metadata json]
        ▼
backend  services/email_ingest.py  (per-mailbox routing) → services/folder_ingest.py (shared bridge)
  resolve mailbox_id → owner/tier/kb (SERVER-SIDE) → dedup vs the Document row →
  race-safe create + enqueue on the Redis doc stream → Paperless leg →
  record email_ingest_log ledger → respond 4-state
        ▼
  document worker (async): OCR / chunk / embed + KG / Schicht-A hooks
```

**Hard constraints:** the IMAP credentials live **only** in the watcher's Secret
(never in the backend image); detection is **event-driven IMAP IDLE — no polling,
no WebSocket**; and the **sphere is server-authoritative** — the watcher sends only
a routing `mailbox_id`, never a tier/owner, so a leaked push token cannot escalate
a mailbox's filing tier.

## Enable it

1. Set the config (see `docs/ENVIRONMENT_VARIABLES.md` → *Email Auto-Ingest*):

   ```bash
   EMAIL_INGEST_ENABLED=true
   # The per-mailbox routing table (server-authoritative). One entry per mailbox:
   #   id    = the routing key the watcher sends (NOT a credential)
   #   owner = username/id the docs are owned by (empty → ownerless)
   #   tier  = circle tier at create (0=self … 4=public)
   #   kb    = target knowledge base (auto-created)
   EMAIL_INGEST_MAILBOXES_JSON='[{"id":"buchhaltung","owner":"evdb","tier":0,"kb":"xidra"}]'
   EMAIL_INGEST_TO_PAPERLESS=true
   ```

2. Mint the Bearer token (admin, `settings.manage`). It lives in `SystemSetting`,
   not `.env`, so it is revocable without a redeploy — and is **separate** from the
   folder-ingest token (the two watchers are independently revocable):

   ```bash
   curl -X POST https://<host>/api/email-ingest/token \
        -H "Authorization: Bearer <your-admin-jwt>"
   # → {"token": "…"}   # store this in the email-ingest watcher's secret
   ```

3. Point the watcher at the backend (`RENFIELD_URL` + the token) and at the
   mailboxes to watch (`mailboxes.yaml`: IMAP connection + the routing `id` only —
   **no** owner/tier/kb; those are server-side). See the watcher repo's README.

## Server-authoritative sphere routing

The watcher knows only an opaque `mailbox_id` per mailbox. The backend's routing
table (`EMAIL_INGEST_MAILBOXES_JSON` → `settings.email_ingest_mailboxes`) maps that
id to the real **owner / tier / knowledge-base**. `resolve_mailbox_target()` is
defensive — a malformed entry is skipped (never crashes routing for other
mailboxes), the tier is clamped to 0–4, and the KB defaults to `Eingang`. An
**unknown `mailbox_id` → `failed`**, so a stray or forged id never files anywhere.

## The 4-state response contract

Identical to folder-ingest (the backend reuses `ingest_document` + `IngestStatus`).
The push response body's `status` is load-bearing — but because one email fans out
into N attachment pushes, the watcher **aggregates** the per-attachment statuses
into one decision for the *email*. All four `status` values are HTTP 200.

| `status`    | meaning                                                        |
|-------------|----------------------------------------------------------------|
| `ingested`  | new row created + enqueued                                     |
| `duplicate` | row exists, completed, Paperless leg settled                   |
| `retry`     | worker down, or row pending/processing, or Paperless unsettled |
| `failed`    | terminal reject (bad ext, empty, oversize, malformed metadata) |

Per-email aggregation (watcher side, `engine.py::aggregate`):

| email outcome | when | watcher action |
|---|---|---|
| **processed** | ≥1 attachment `ingested`/`duplicate` (no transient, no fatal) | move → `processed_folder` |
| **failed** | real docs all terminally rejected, OR every attachment gate-rejected | move → `failed_folder` |
| **leave** | any attachment transient (`retry` / 503 / network) | leave UNSEEN, bounded-backoff retry |
| **skip** | no ingestable attachments | mark `\Seen`, leave in place |
| **(fatal)** | any `401`/`403` | stop the mailbox, notify — never move |

Gate-rejected attachments (wrong extension, oversize, empty) are noise (logos,
signatures) and **never fail an email on their own**. Transport codes the watcher
maps separately: `401`/`403` → fatal config error (stop, don't move); `503`
(`feature_disabled` / `worker_unavailable`) → retry. Every response carries
`contract_version`; the watcher sends its own in the `X-Email-Ingest-Contract`
header — a mismatch is logged as a skew WARNING and processed leniently.

## Health handshake

The watcher pings `GET /api/email-ingest/health` (same Bearer token) to catch
config drift — it confirms its configured `mailbox_ids` are known to the backend
routing table (the email analog of folder-ingest's `kb_resolved`):

```json
{ "enabled": true, "mailbox_ids": ["buchhaltung"], "to_paperless": true,
  "token_ok": true, "max_file_size_mb": 50, "allowed_extensions": ["pdf", …],
  "contract_version": "1" }
```

A wrong token → `401`/`403`. When the feature is **disabled** health still returns
`200` with `enabled: false` (distinct from the push route's transient `503`).

## Behavior notes

- **Attachments only, inline images skipped.** A leaf MIME part with a filename is
  an attachment unless it is an inline *image* — inline **documents** (a real PDF
  sent `Content-Disposition: inline`, common from Apple Mail / forwards) are kept.
- **Idempotency.** The `email_ingest_log` ledger keys on `(mailbox_id, message_id,
  content-sha256)`; the bridge dedups on `(file_hash, kb)`. Re-pushing a whole email
  (e.g. after a transient `retry`) is safe — already-ingested attachments return
  `duplicate`. Two mailboxes routing to different KBs never cross-dedup or
  cross-record.
- **Owner / tier.** Resolved server-side per mailbox (see above). With
  `AUTH_ENABLED=false` (single-user) the circle filter short-circuits, so tier/owner
  are inert — but are still recorded correctly for when auth is enabled.
- **Paperless leg + correspondent auto-create.** Identical to folder-ingest (shared
  `services/folder_ingest_paperless.py`): non-blocking upload, consume-verdict await,
  resolve-or-create the correspondent against the full taxonomy. A Paperless
  duplicate is terminal success; Paperless filing never fails the KB ingest.
- **Move semantics.** The watcher prefers IMAP `MOVE` (RFC 6851); on a server
  without it, it falls back to `COPY` + `UID EXPUNGE` (UIDPLUS) and **never** issues
  a bare `EXPUNGE` (which would purge all `\Deleted` mail). See the watcher README's
  *Known limitations* for the UIDVALIDITY + no-MOVE/no-UIDPLUS edge cases.
- **Reconnect self-heal (`v0.1.1+`).** The watcher uses two IMAP connections — one
  dedicated to IDLE, one for `SEARCH`/`FETCH`/`MOVE`. A server `BYE timeout` (IONOS
  ends a long IDLE roughly every ~30 min) can drop both. On any disconnect the watch
  loop now **resets both connections** and reconnects with backoff, then reconciles
  `UNSEEN` so mail that arrived during the gap is still picked up. *(Before `v0.1.1`
  only the IDLE connection was reset; the dead command connection made every
  `SEARCH UNSEEN` throw, so the watcher silently stopped detecting mail until a pod
  restart — fixed by resetting the command connection too.)*

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Watcher logs `401`/`403` on every push | token missing / wrong | re-mint via `POST /api/email-ingest/token`, update the watcher secret |
| Push gets `503 feature_disabled` | `EMAIL_INGEST_ENABLED=false` | enable the flag + restart the backend |
| Push gets `503 worker_unavailable` | document worker pod down | check the worker; the email stays UNSEEN and is re-pushed when it recovers |
| Email lands in `Fehler` | a real document failed / all attachments gate-rejected | check `ALLOWED_EXTENSIONS` + `MAX_FILE_SIZE_MB`; inspect the attachment |
| `mailbox_id … unknown` → `failed` | the watcher's mailbox id isn't in `EMAIL_INGEST_MAILBOXES_JSON` | add the routing entry (id/owner/tier/kb) + restart the backend |
| Watcher idle, mail not picked up | IMAP IDLE not firing / connection wedged | check the pod logs — `v0.1.1+` self-heals on disconnect (resets both connections + reconciles `UNSEEN`). To force recovery: `kubectl -n renfield rollout restart deploy/renfield-mcp-email-ingest` (reconciles `UNSEEN` on boot — so if you already opened the mail in a client, mark it **unread** first). `renfield-mcp-email-ingest-scan <id>` does a no-side-effect dry-run |
| Repeating `IMAP watch disconnected:` (empty reason), no pushes | on `v0.1.0` the command connection wasn't reset on reconnect — watcher wedged after a server `BYE timeout` | upgrade the watcher to `v0.1.1+`; restart to recover immediately |

## Where it lives

- Routing + bridge: `services/email_ingest.py` (mailbox routing, ledger, token helpers)
- Shared bridge: `services/folder_ingest.py` (`ingest_document`: dedup, 4-state, owner/tier)
- Shared token/user helpers: `services/ingest_common.py` (used by both ingest bridges)
- Routes: `api/routes/email_ingest.py` (`POST /document`, `GET /health`, `POST /token`)
- Ledger model + token key: `models/database.py` (`EmailIngestLog`, `SETTING_EMAIL_INGEST_TOKEN`); migration `pc20260614`
- Watcher: [`renfield-mcp-email-ingest`](https://github.com/ebongard/renfield-mcp-email-ingest) (`renfield/email-ingest-mcp`)
