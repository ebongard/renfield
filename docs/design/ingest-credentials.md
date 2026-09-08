# Per-integration ingest credentials with self-rotation

**Status:** DESIGN (2026-09-08). Not implemented.
**Flag:** `INGEST_CREDENTIALS_ENABLED` (dark by default; legacy path byte-identical when off)
**Related:** `docs/FOLDER_INGEST.md`, `docs/EMAIL_INGEST.md`, `docs/design/scanner-ingest.md`

## Problem

An ingest MCP authenticates to the backend with a Bearer token. Today that token
has three defects, and adding a third pushing client (the scanner) makes all
three concrete rather than theoretical.

**1. One secret, three copies, synchronised by hand.**

| Where | Variable / key | Role |
|---|---|---|
| Backend env | `FOLDER_INGEST_TOKEN` | optional; re-seeds the DB at every boot |
| Backend DB | SystemSetting `folder_ingest.token` | what is actually checked per push |
| MCP env | `RENFIELD_INGEST_TOKEN` | the client's copy |

Rotating means editing two deployments and hoping they agree. The 2026-07
incident recorded in `config.py` is this failure: a DB wipe cleared the DB copy
and every push began returning 403.

**2. One token per backend, shared by every client of that route.**
`SETTING_FOLDER_INGEST_TOKEN` is a single key, so `renfield-mcp-filesystem` and
`renfield-mcp-scanner` present the *same* credential. Neither can be revoked
without breaking the other, and the backend cannot tell which one pushed.

**3. Minting is an undocumented API call.** `POST /api/folder-ingest/token`
exists and is admin-gated, but there is no UI, so provisioning a machine
credential means hand-crafting a request.

## Requirements (user-fixed)

1. The token is **generated and managed in Renfield**, through the UI, on the
   surface where MCP integrations are managed (`IntegrationsPage`).
2. **Each MCP / integration has its own token, rotatable separately.**
3. **MCP credentials and satellite credentials stay separate surfaces.** (They
   already are: satellites have their own table, flags and per-device tokens.
   This design does not touch them.)
4. Rotation should be possible **from the client**, authenticated with the token
   it currently holds — so the two copies cannot drift.

## Architecture

### Data model — mirror `satellites`, do not invent

The satellite-enrollment table (`pc20260624_satellite_enrollment`) already
solves "per-device machine credential" in this codebase. Reuse its shape:

```
ingest_credentials
  id                     int pk
  client_id              str   unique   -- "files", "scanner", "email-ingest"
  label                  str             -- shown in the UI
  route                  str             -- folder_ingest | email_ingest
  token_hash             str             -- bcrypt. NEVER the plaintext.
  created_by_user_id     fk users.id
  created_at             datetime
  last_authenticated_at  datetime        -- drives "last seen" in the UI
  rotated_at             datetime
  revoked_at             datetime
  is_enabled             bool default true
```

Storing a **bcrypt hash, never the plaintext**, is the material improvement over
the current SystemSetting, which holds the token in the clear.

### Auth path

`verify_folder_ingest_token(db, token)` becomes
`resolve_ingest_client(db, route, token) -> IngestClient | None`: it walks the
enabled, unrevoked credentials for that route and returns the matching client.
Push handlers gain the client's identity.

Cost to note honestly: bcrypt-per-push over N credentials is slower than one
constant-time compare. With a handful of clients this is irrelevant next to the
document processing that follows, but it is a real change and belongs in the
plan rather than discovered later.

### Knowing the client unlocks server-authoritative sphere routing

This is the part worth building for even setting rotation aside. Once the
backend knows **which client** pushed, it has exactly what the scanner design
needs: `client -> (owner, tier, kb)`, resolved server-side. Email-ingest already
does this per mailbox; folder-ingest cannot, because every client is
indistinguishable. `scan_profile_id` in the scanner's push metadata is currently
**inert for this reason** — the route files into one configured destination.

The rotation requirement and the routing design want the same table.

### Self-rotation (requirement 4)

```
POST /api/ingest-credentials/rotate
Authorization: Bearer <current token>
  -> 200 { "token": "<new plaintext, shown once>" }
```

Authenticated by the credential being rotated: no admin session, no second
channel. The old token is invalidated only after the client acknowledges, so a
crash mid-rotation cannot lock the client out — **both tokens are accepted
during a bounded overlap window** (`INGEST_CREDENTIAL_ROTATION_GRACE_SECONDS`),
after which the old one is dropped. Without the overlap, a client that dies
between "server rotated" and "client persisted" is permanently locked out and
needs manual re-provisioning.

### Client-side persistence: a writable credential file (DECIDED 2026-09-08)

Self-rotation is only useful if the new token survives a client restart. Every
ingest MCP reads its token from env at startup, and a k8s pod cannot write the
Secret its env came from — so it would receive a new token, use it, restart,
re-read the OLD env value and start failing with 403.

**Decision: the env var becomes BOOTSTRAP-ONLY and the durable copy is a file
the client owns.**

    startup:  credential file exists and is non-empty  -> use it
              otherwise                                -> use env, then write it
                                                          to the file
    rotation: write the new token to the file ATOMICALLY (temp + rename, 0600)
              BEFORE reporting success to the backend

Rejected alternatives: having the backend patch the k8s Secret would put cluster
write access behind a document-ingest route and does nothing for the scanner,
which is not in Kubernetes at all; dropping self-rotation would keep the
hand-synchronisation between two places that caused the 2026-07 drift.

**The k8s volume must be PERSISTENT, not `emptyDir`.** This is the trap in this
approach and it must be caught in review, not in production. An `emptyDir` is
lost on reschedule, so a rotated client would fall back to its env value — which
after a rotation is the STALE token — and 403 until an operator intervenes. The
scanner on the operator Mac writes an ordinary file and is unaffected.

**Cutover is acknowledgement-based, not purely timed.** The previous token stays
valid until the client successfully authenticates with the new one (proving it
persisted the value), with `INGEST_CREDENTIAL_ROTATION_GRACE_SECONDS` as an
upper bound rather than the primary mechanism. A timer alone would cut a client
off that had not yet managed to write its file; waiting for proof of use does
not.

### UI (requirement 1)

A credentials section on `IntegrationsPage.tsx` — the page whose own header
already reads *"Admin page for managing MCP server integrations"*. Per row:
label, route, created, last-seen, state; actions mint / rotate / revoke.

Mirror `components/satellites/SatelliteEnrollment.tsx`, which already solves the
show-once UX **including the failure case** — it detects a silently-failed
clipboard write, because a token shown once and not copied is lost.

Two things the UI must show that the current one-token model cannot:

- **Blast radius on rotate.** Name the client that will stop working until its
  copy is updated. A bare Rotate button on a shared credential is a footgun.
- **Source of truth.** If `FOLDER_INGEST_TOKEN` is set in the backend env, the
  boot reconciler re-seeds the legacy token from it, so a UI rotation of the
  legacy credential silently reverts at the next restart. The UI must render
  that credential read-only and say where it is managed, rather than offering an
  action it cannot make stick.

### Backward compatibility

The legacy SystemSetting token keeps working for the whole transition:
`resolve_ingest_client` falls back to it and reports a synthetic `legacy`
client. Flag off ⇒ the legacy path only, byte-identical. Clients migrate one at
a time; the legacy credential is removed only once no client has used it, which
`last_authenticated_at` makes observable rather than a guess.

## Configuration

| Key | Default | Meaning |
|---|---|---|
| `INGEST_CREDENTIALS_ENABLED` | `false` | Dark. Off ⇒ legacy path only |
| `INGEST_CREDENTIAL_ROTATION_GRACE_SECONDS` | `300` | Old-token overlap window |
| `INGEST_CREDENTIAL_SELF_ROTATION_ENABLED` | `false` | Phase 3 |
| `INGEST_CREDENTIAL_FILE` | *(client-side)* | Durable token path; env is bootstrap-only |

## Phasing

- **Phase 1 — credentials table + auth path.** Migration, `resolve_ingest_client`,
  legacy fallback, `last_authenticated_at`. No UI, no behaviour change.
- **Phase 2 — UI.** Mint / rotate / revoke on `IntegrationsPage`, blast-radius
  warning, env-managed read-only state. **This alone satisfies requirements
  1-3.**
- **Phase 3 — self-rotation.** The rotate endpoint, acknowledgement-based
  cutover, and the credential file. Requires MCP-side changes in each sibling
  repo plus a persistent volume for the k8s ones.
- **Phase 4 — sphere routing on client identity.** `client -> owner/tier/kb`,
  which makes the scanner's `scan_profile_id` meaningful and brings folder-ingest
  level with email-ingest.

## Risks / accepted residuals

- **bcrypt per push, over N credentials**, replaces one constant-time compare.
  Negligible beside document processing at this scale; revisit if the credential
  count ever grows large.
- **A rotation grace window means two valid tokens briefly.** Deliberate: the
  alternative is locking a client out on a mid-rotation crash. Bounded and
  configurable.
- **A lost credential file falls back to a stale env token**, which means 403
  until an operator intervenes. Mitigated by requiring a persistent volume (never
  `emptyDir`) and by acknowledgement-based cutover; not eliminated. This is the
  cost of keeping the backend cluster-agnostic.
- **The legacy token stays valid** through the transition; the window closes only
  when the credentials table shows no client using it.
