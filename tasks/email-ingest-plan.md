# Plan: Email-mailbox auto-ingest (`accounting@example.com` → KB + Paperless)

Status: PROPOSED (nothing implemented). Mirrors the shipped **folder-ingest** feature
(SMB share → KB + Paperless) for an IMAP mailbox.

## Goal
A new PDF (or allowlisted) **attachment** arriving at any **watched mailbox** is
auto-filed into the Knowledge Base **and** Paperless — **event-driven via IMAP IDLE**
(the email analog of inotify / SMB `CHANGE_NOTIFY`). **NO polling** (hard constraint,
same as folder-ingest).

**Multi-mailbox, per-mailbox sphere routing.** The watcher handles **N mailboxes**,
exactly as `renfield-mcp-filesystem` handles N roots via `roots.yaml`. Each mailbox is
its own entry with its own IMAP creds + folders + allowlist **and its own target
owner / tier / KB**, so company invoices (`accounting@example.com`) and private invoices
(personal address) file into **separate spheres**. First addresses: `accounting@example.com`
(x-idra/company) + a private mailbox (self/household).

## Locked design decisions
1. **Watcher = a NEW dedicated `renfield-mcp-email-ingest` service** (async IMAP IDLE), mirroring `renfield-mcp-filesystem`. *(Revised by /plan-eng-review finding A1: the existing `email` MCP is `transport: stdio` = a subprocess in the backend pod, which has no always-on daemon lifecycle to host an IDLE push-watcher — the exact "stdio can't host a daemon" wall we hit with the filesystem MCP — and would leave the company IMAP creds in the backend image, forgoing the cred-isolation that motivated folder-ingest's dedicated MCP. A dedicated async service fixes all three.)* The interactive `renfield-mcp-mail` (stdio) stays untouched; reuse its attachment-parsing code as reference only.
2. **Attachments only** (PDF + the existing allowlist; skip inline images/signatures). No email-body rendering.
3. **Correspondent = OCR-only**, exactly as folder-ingest — reuse the existing Paperless leg verbatim, no email-sender hint.
4. **Owner / tier / target-KB = PER-MAILBOX, server-authoritative** (supersedes the earlier "deferred single global owner/tier"). The sphere lives in the **backend** routing table `email_ingest_mailboxes` (`{id, owner, tier, kb}`), keyed by `mailbox_id`; the watcher's `mailboxes.yaml` holds only IMAP creds + the matching `id`. So the sphere is config (not a global) AND the watcher can't dictate it (security). The *separate-instances* decision only changes **where** each sphere lives: one instance routing both mailboxes via their ids, or — if x-idra splits to its own instance later — that instance configs `buchhaltung@` and the personal instance configs the private address. Zero redesign either way. Feature still ships dark behind `EMAIL_INGEST_ENABLED`.

## Architecture
```
accounting@example.com   privat@…        (N mailboxes, mailboxes.yaml)
   │  IMAP IDLE          │  IMAP IDLE     (one watch loop each; push; no poll)
   ▼                     ▼
renfield-mcp-email-ingest  (NEW dedicated async service; mirrors renfield-mcp-filesystem)
                                  ── holds all IMAP creds in its OWN secret (backend must NOT)
   │  per new message: MIME-walk, extract allowlisted attachments; per attachment, push
   │  WITH the mailbox's sphere routing (mailbox-id → owner/tier/kb):
   ▼
POST /api/email-ingest/document   [multipart: file + email-meta + mailbox/owner/tier/kb JSON; Bearer]
   ▼
services/email_ingest.py  ── thin wrapper ──▶ services/folder_ingest.ingest_document()
                                              REUSED unchanged:
                                              - hash dedup (D2 matrix)
                                              - target-KB resolve + create
                                              - owner + circle tier
                                              - 4-state contract
                                              - Paperless leg (make_paperless_leg,
                                                incl. correspondent resolve-or-create)
   ▼  4-state: ingested | duplicate | retry | failed
watcher moves the email:  Verarbeitet (done) · Fehler (failed) · leave in INBOX (retry)
```

## Reuse map (what is NOT rebuilt)
- `services/folder_ingest.py::ingest_document` (the core bridge, ~line 191) — called as-is.
- `services/folder_ingest_paperless.py::make_paperless_leg` — passed through unchanged (OCR correspondent + the resolve-or-create guardrail shipped in v2.14.1).
- `resolve_target_kb` / `resolve_owner_user_id`, the `IngestMeta`/`IngestStatus`/4-state types.
- The Bearer-token + `POST /token` + `GET /health` pattern from `api/routes/folder_ingest.py`.
- The `renfield-mcp-mail` IMAP plumbing (`_get_imap_connection`, `_fetch_full_email`, attachment parsing).

## New components
**Backend (renfield):**
- `services/email_ingest.py` — wraps `ingest_document`; resolves the push's `mailbox_id` → `owner/tier/kb` via the **server-authoritative routing table** (NOT from the push body); injects email provenance (Message-ID, sender, subject, date) into `IngestMeta`/metadata.
- `api/routes/email_ingest.py` — `POST /document`, `GET /health`, `POST /token`; 4-state, contract-version header skew check (copy folder-ingest). Unknown `mailbox_id` → `failed`.
- `utils/config.py` — `email_ingest_enabled` (default False), `email_ingest_to_paperless` (global), and **`email_ingest_mailboxes`** = server-authoritative routing table, a list of `{id, owner, tier, kb}` (served from the `renfield-mcp-config` ConfigMap, like `mcp_servers.yaml`). The backend decides the sphere from `mailbox_id`; the watcher never sends owner/tier.
- `email_ingest_log(mailbox_id, message_id, attachment_sha256)` table + migration — keyed by mailbox so two spheres never cross-dedup; idempotency beyond the hash dedup.
- Tests: route (status codes, contract, unknown-mailbox→failed), wrapper (mailbox_id→sphere resolution, provenance), dedup ledger (per-mailbox), integration vs real PG.

**Watcher (NEW `renfield-mcp-email-ingest`, async, mirrors `renfield-mcp-filesystem`):**
- **One async IMAP IDLE watch loop per mailbox** in `mailboxes.yaml` (the email analog of `roots.yaml`): renew IDLE ≤29 min (RFC 2177), per-mailbox reconnect/backoff, one-shot startup reconciliation (process existing UNSEEN once, then IDLE — not a poll). Hosted in an async main like the filesystem MCP's `run_streamable_http_async` (a stdio server can't host this daemon — A1).
- Attachment extraction (extension + size gate; skip `Content-Disposition: inline` / tiny images).
- Push client: multipart + Bearer + the mailbox's `mailbox_id` + 4-state handling → move the email (`Verarbeitet`/`Fehler`/leave).
- `mailboxes.yaml` = list of `{id, imap{host,port,user,pass_env}, folder, processed/failed folders, allowlist, size_cap}`. **Holds IMAP creds + the `id` only — NOT owner/tier/kb** (the backend's routing table owns the sphere, keyed by the same `id`). Dynamic reload like `roots.yaml`. Creds in the service's own secret.

## Phases
- **Phase 1 — backend** (sphere-independent except deferred owner/tier): route + `email_ingest` wrapper + config + dedup ledger + migration + tests. Ships dark.
- **Phase 2 — watcher**: new `renfield-mcp-email-ingest` repo/image — async IDLE loop + attachment extraction + push + move-to-folder + reconnect/reconcile + startup reconciliation. Scaffold from the `renfield-mcp-filesystem` repo (same engine/pusher/contract/health/dry-run shape; swap the provider from SMB/local → IMAP).
- **Phase 3 — deploy**: build the new image → Harbor → k8s deployment (own mailbox secret + push token); register; E2E vs the real mailbox (send a test invoice → KB + Paperless).

## Key risks / open questions for review
1. **IMAP IDLE library — RESOLVED by A1.** New dedicated async service → use `aioimaplib` (native async IDLE) or `imap_tools`/`imapclient`. No more sync-`imaplib`/threading wrinkle. (Pick the lib in Phase 2; `aioimaplib` is the closest async-IDLE fit.)
2. **Backend reachability / push model — RESOLVED by A1.** The new service is a deployment of its own (like `renfield-mcp-filesystem`), with the same `RENFIELD_URL` + Bearer-token push wiring; no stdio-in-backend conflict.
3. **Dedup correctness.** Message-ID can be absent/duplicated; same attachment across two emails; the same email re-delivered. The `(message_id, attachment_sha)` ledger + the existing hash-dedup should cover it — validate the interaction (a genuinely new email with a previously-seen attachment hash → `duplicate`, which is correct).
4. **Move-to-folder semantics.** Requires the IMAP server to support folder create/move + the account to have write access; some providers/quotas differ. Fallback: `\Seen` + a processed-ledger if move fails.
5. **Cross-sphere routing (decision #4, security).** Multi-mailbox means a `buchhaltung@` (company) and a private invoice must NOT cross into each other's sphere. Mitigated by **server-authoritative** routing (`mailbox_id` → `owner/tier/kb` decided by the backend, never the push) + the per-mailbox dedup key. The watcher can't escalate tier even with a valid push token. Keep dark until the per-mailbox table is set.
6. **Idempotency on crash** between push-accepted and email-moved (at-least-once) — the backend ledger + hash-dedup make a re-push a no-op `duplicate`, so safe.

## Done = 
A real invoice emailed to `buchhaltung@` lands in the KB + Paperless (correspondent via OCR resolve-or-create), the email moves to `Verarbeitet`, re-delivery is a `duplicate` no-op, and `EMAIL_INGEST_ENABLED=false` is byte-identical to today.

## What already exists (reuse, don't rebuild)
- `services/folder_ingest.py::ingest_document` + `resolve_target_kb` / `resolve_owner_user_id` — the core bridge, called as-is.
- `services/folder_ingest_paperless.py::make_paperless_leg` + `resolve_or_create_correspondent` — Paperless leg reused verbatim (OCR correspondent).
- `IngestStatus` / `IngestMeta` / 4-state contract + the `/token` + `/health` route pattern — **share, don't fork** (code-quality C1: email_ingest imports these from `folder_ingest`, doesn't copy them).
- `renfield-mcp-filesystem` repo — the **template** for the new `renfield-mcp-email-ingest` (engine/pusher/contract/gate/health/dry-run/Dockerfile/k8s); swap the provider SMB/local → IMAP IDLE.
- `renfield-mcp-mail` IMAP/attachment-parsing code — reference for MIME walk + attachment extraction (copy the parsing logic, not the server).

## NOT in scope (deferred, with rationale)
- **Email-body-as-document** — accounting is attachment-driven (decision #2); revisit only if body-only invoices appear.
- **Sender → correspondent hint** — decision #3 chose OCR-only; the From-sender enhancement is a clean later add (the leg already resolves-or-creates).
- **Multi-mailbox is IN scope** (config-driven, `mailboxes.yaml` + backend routing table). Adding the Nth mailbox is config, not code.
- **Owner/tier/KB values for each mailbox** — the per-mailbox routing *mechanism* ships now; the concrete owner/tier/kb per mailbox is set when the sphere is decided (ties to separate-instances). Ships dark until then.

## Test coverage (plan must ship with these)
Backend (mirror `tests/backend/test_folder_ingest*.py`): route status/contract/header-skew · **unknown `mailbox_id` → `failed`** · `email_ingest` wrapper (`mailbox_id` → correct owner/tier/kb from the routing table; provenance) · **per-mailbox dedup ledger** (`(mailbox_id, message_id, attachment_sha)`: same attachment in two mailboxes does NOT cross-dedup; re-delivered email → `duplicate`; absent Message-ID → synthesized key) · **cross-sphere isolation** (mailbox A's push never files at mailbox B's tier) · integration vs **real PG** (ledger + KB filing, per the no-sqlite rule).
New watcher service (mirror the filesystem MCP tests): MIME walk extracts only allowlisted/non-inline attachments · 4-state → move mapping (`Verarbeitet`/`Fehler`/leave) · startup reconciliation dispatches existing unseen once · IDLE reconnect/backoff (mock IMAP).

## Failure modes (each: test? error-handling? silent?)
- IDLE connection drops mid-fetch → reconnect+backoff+reconcile (test: yes / handled / not silent).
- Push accepted but email-move fails (crash window) → at-least-once; backend `(message_id,attachment_sha)` ledger + hash-dedup make re-push a `duplicate` no-op (test: yes / handled / not silent — wasted work only).
- Can't MOVE processed email (IMAP perms/quota) → fallback `\Seen` + ledger; worst case re-process → backend `duplicate` (not a silent dup). **Not critical.**
- Absent/duplicate Message-ID → synthesized key (uid+folder+attachment-sha) (test: yes / handled).
- Oversize / disallowed / malformed attachment → size+ext gate before push, like folder-ingest (handled / not silent).
- Paperless down during the leg → reused transient/terminal handling (handled / not silent).
- **No critical gaps** (no failure mode is both untested AND silent AND unhandled).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR (PLAN) | A1 architecture (resolved: dedicated service) · C1 code-quality (share contract) · S1 security (server-authoritative routing, from the multi-mailbox refinement) · test coverage specified · 0 critical failure-gaps |
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX | 0 | — | n/a (no UI) |

- **Step 0 scope:** accepted as-is — right-sized (reuses `ingest_document` + Paperless leg verbatim; under the 8-file/2-service threshold). Multi-mailbox added post-review as config (mirrors `roots.yaml`), not new services.
- **A1 (architecture, resolved):** watcher moved from the stdio-in-backend `email` MCP to a **new dedicated async `renfield-mcp-email-ingest`** service — dissolves the IDLE-on-sync-imaplib, push-reachability, and cred-isolation risks at once.
- **C1 (code quality):** share `IngestStatus`/`IngestMeta`/4-state + `/token`/`/health` with folder-ingest; don't fork.
- **S1 (security, multi-mailbox):** sphere routing is **server-authoritative** — backend resolves `mailbox_id` → `owner/tier/kb`; the watcher never sends tier/owner, so a leaked push token can't file company invoices at an arbitrary tier. Per-mailbox dedup key prevents cross-sphere collision.
- **UNRESOLVED:** the concrete owner/tier/kb *values* per mailbox (decision #4) — the per-mailbox routing mechanism is designed; values set when the sphere/instance topology is decided. Ships dark behind `EMAIL_INGEST_ENABLED`.
- **VERDICT:** ENG CLEARED (plan-stage) — Phase 1 (backend, incl. the per-mailbox routing table + dedup) is implementation-ready; Phase 2/3 follow. Only the per-mailbox sphere *values* wait on the separate-instances decision.
