# Design — Person-scoped federation (cross-instance identity mapping)

**Status:** DESIGN — not implemented. Spike/design only, no product code yet.
**Motivating case:** linking a personal (household) Renfield instance to a business instance so that a *person* who has an account on both sees, from either side, exactly what that person is entitled to see on the other — while everyone else sees only public.
**Depends on:** the peer-scoped federation fix (`services/circle_sql.py::peer_scoped` + `enforce_circles`, shipped in PR #957) is the FALLBACK path this builds on. Read that first.

---

## 1. The gap

Federation today is **instance-to-instance, single-tier**:

- Pairing writes one `PeerUser` per direction (keyed on `remote_pubkey`) plus one `CircleMembership` granting the whole peer instance a single tier.
- `FederationQueryResponder` resolves the asker to that one tier and runs `PolymorphicAtomStore.query(asker_id=peer.remote_user_id, enforce_circles=True)` — peer-scoped (public + pairing-tier-membership only).
- The query envelope is signed by the *instance's* one Ed25519 identity. It carries **no per-person identity** — the responder cannot tell which human on M1 is asking.

The desired model is **person-scoped**:

> Default: M1 sees only M2's public atoms (and vice versa).
> Exception: if the person querying from M1 **also has an account on M2**, that person sees everything **they personally** are entitled to see on M2 (their own atoms + their circle reach), as if they had logged into M2.
> A querier with no M2 account gets the guest/public fallback.

Two hard blockers in the current system:

1. **No per-person identity on the personal instance.** It runs `AUTH_ENABLED=false` (single anonymous user), so it has no "persons" to carry across the boundary at all.
2. **No identity carried or mapped across the link.** Even with per-person auth, the envelope vouches for the *instance*, not the user, and there is no M1-user ↔ M2-user mapping.

## 2. Target model

```
 M1 (personal, auth-on)                         M2 (business, auth-on)
 ─────────────────────                          ──────────────────────
 person U asks Q  ──sign(instance key,          resolve envelope:
                    {querier_ref:U, Q, …})──▶     1. verify M1 instance signature (paired peer)
                                                  2. map U → local user X via federation_user_links
                                                     ├─ mapped   → query AS X  (full circle reach,
                                                     │              owner+grants+memberships — X's own view)
                                                     └─ unmapped → peer-scoped fallback tier
                                                        (public/guest — the existing pairing grant)
                                                  3. synthesize answer, return (redacted provenance)
```

Key insight: **the fix already shipped is exactly the unmapped fallback path.** `enforce_circles=True` / `peer_scoped` = "a stranger peer sees only public + the granted tier." The new work is the **mapped path**: resolve the querying person to a local user and run a *normal authenticated query as that user* (owner branch INCLUDED — they are that user).

| Querier | Responder query | Visibility |
|---|---|---|
| Mapped to local user X | `query(asker_id=X, enforce_circles=False)` under X's auth context | X's own atoms + X's circle reach (as if X logged in) |
| Unmapped | `query(asker_id=<peer fallback id>, enforce_circles=True)` | public + pairing fallback tier only |

## 3. Prerequisites

**P0 — the personal instance must run auth-on.** Person-scoped federation is meaningless without per-person identity on both ends. This is the "big UX change" (logins, voice identity, per-user circles) and is a project in itself. Standing up an auth-on instance has known traps — see the deploy-production skill, "Standing up a NEW instance (auth-on)" (SECRET_KEY in every backend-image workload, no alembic-upgrade on a fresh DB, `AUTH_ENABLED`+`WS_AUTH_ENABLED` flip together). The business instance is already auth-on and can be the responder before the personal side flips; but the *exception* (personal→business person-mapping) only works once the personal side has real accounts.

## 4. Architecture

### 4.1 Identity vouching (envelope change)

The querying user's identity rides in the **signed** query envelope, vouched for by the sending instance:

- Add `querier_ref` to `QueryBrainInitiateRequest` and into `initiate_canonical_payload` (so it is covered by the existing Ed25519 signature — an attacker cannot strip or forge it without the instance key).
- `querier_ref` is a **stable, opaque per-person identifier** minted at link time (NOT a raw local user id, NOT an email — see collision + privacy notes). A random per-(peer,user) token stored on both sides.
- Trust statement: "M2 trusts that M1 correctly authenticated the human behind `querier_ref`." This is acceptable **only between fully-trusted paired peers** (the user's own instances). A compromised M1 could impersonate any of its own linked users to M2 — bounded by (a) read-only `query_brain`, (b) the explicit, revocable link, (c) M1 being the user's own box. Document this as the load-bearing trust assumption.

### 4.2 The cross-instance person link

New table `federation_user_links` on each instance (the responder's copy is authoritative for *its* mapping):

```
federation_user_links
  id
  peer_id            FK → peer_users.id      (which paired peer this link is for)
  querier_ref        text                    (the opaque per-person token in the envelope)
  local_user_id      FK → users.id           (who, on THIS instance, that querier is)
  created_by         FK → users.id           (who established the link)
  created_at
  UNIQUE(peer_id, querier_ref)
```

**Establishing a link must prove ownership of BOTH accounts** — you can only say "my household account = my business account" if you control both. Options (pick in a follow-up):
- **A. Double-login handshake** (recommended): a link-pairing flow mirroring the instance-pairing QR dance, but run while authenticated as U on M1 AND as X on M2 — each side signs "I am U/X and I consent to link." Neither instance can forge the other's half.
- **B. Admin assertion on M2:** an M2 admin manually maps `querier_ref` → local user. Simpler, but trusts the admin, not a proof of same-person.

Do **not** auto-match on email/name claims — M1 vouching for an email is weaker than a consent-signed link and invites impersonation.

### 4.3 Responder resolution

`FederationQueryResponder._handle_initiate` gains, after signature/nonce/peer checks:

```
link = lookup federation_user_links(peer_id, querier_ref)
if link:
    asker_id       = link.local_user_id
    enforce_circles = False           # act AS the mapped local user — full circle reach
    # (runs under that user's own access; owner + grant + membership branches all apply)
else:
    asker_id       = <peer fallback identity>   # today's peer.remote_user_id path
    enforce_circles = True                       # peer-scoped fallback (public + granted tier)
```

Everything downstream (`_run_query`, `PolymorphicAtomStore.query`, synthesis, redacted provenance) is unchanged — the mapped path just flips `enforce_circles` off and swaps in the mapped local user id. **The shipped peer_scoped fix is the safe default; the link is the deliberate escalation to "this really is user X."**

### 4.4 Guest/public fallback

The unmapped path is exactly the current PR-#957 behavior. The instance-level `CircleMembership` granted at pairing becomes the **guest tier** for unmapped queriers (set it to `public` (4) for "unmapped sees only public", or a higher-reach tier for "unmapped sees household-level"). This is the "xidra persons not in household see only guest information" rule.

## 5. Security model

- **Trust boundary:** M2, on the mapped path, reads out a full local user X's private brain to a query vouched-for by M1. This is a real escalation over the peer-scoped fallback — it is only sound because (a) the link required proof of both-account ownership, (b) M1 is the user's own trusted instance, (c) it stays read-only `query_brain` with redacted provenance (no raw atoms, no writes). Spell this out; make the link revocable (`DELETE` → immediate; purge in-flight requests like `revoke_peer` does).
- **Fail-closed:** unknown `querier_ref` → fallback (never "act as some default user"). Missing/blank `querier_ref` → fallback. A link whose `local_user_id` was deleted → fallback (FK `ON DELETE SET NULL` → treat NULL as unmapped).
- **No collision reintroduction:** `querier_ref` is opaque and unique per `(peer_id, querier_ref)` — it does NOT reuse the raw `remote_user_id` integer that the TODOS "multi-peer remote_user_id collision" item is about. Fixing this design and that item together is natural (both are the "federated identity is not a local integer" problem).
- **Envelope integrity:** `querier_ref` MUST be inside the signed canonical payload; verify before the link lookup.

## 6. Phasing

1. **P0 (prereq):** personal instance → auth-on with real accounts. Separate track.
2. **F-ID-1:** schema (`federation_user_links`) + envelope `querier_ref` (signed) + responder mapped/fallback branch (mapped path reuses `enforce_circles=False`). Dark behind a flag; no `querier_ref` sent → 100% fallback = today's behavior.
3. **F-ID-2:** the link-establishment handshake (double-login consent, §4.2 option A) + admin UI to view/revoke links.
4. **F-ID-3:** unify with the multi-peer `remote_user_id` identity-namespace TODO (one opaque per-peer identity model for both fallback and mapped paths).

## 7. Open questions

- **Link handshake UX** (§4.2 A vs B) — consent-signed double-login vs admin assertion. A is safer; B is faster to ship.
- **`querier_ref` privacy:** it's a stable per-person token crossing the wire on every query — acceptable (opaque, per-peer-scoped), but confirm it can't be correlated across peers (mint per `(peer, user)`, not one global token).
- **Directionality:** the motivating rule is asymmetric (personal→business person-mapping; business→personal public-only). The design is symmetric-capable; each instance independently decides whether to honor links inbound. Business side may choose NOT to install links for personal→business but DO for business→personal, etc. Make link-honoring a per-peer, per-direction toggle.
- **Does the business side even want to expose full per-user reach to the personal box?** That's the user's call per person; the link is opt-in per person, so a person maps themselves only if they want their business view reachable from home.

---

*Nothing here is built. The shipped peer_scoped fix (PR #957) is the fallback foundation; this doc is the plan for the person-mapping exception on top of it.*
