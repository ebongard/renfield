# Federation — pairing two Renfield instances

How to connect two Renfield instances so one can query the other's knowledge
base ("federation"). This is the **user-facing runbook**. The internal design
lives in [`design/federation-identity-mapping.md`](design/federation-identity-mapping.md);
multi-peer topologies in [`FEDERATION_MULTI_PEER.md`](FEDERATION_MULTI_PEER.md).

Pairing is a **two-person, three-message handshake** between an **Initiator (A)**
and a **Responder (B)**. Each side ends up with a stored *peer* record and grants
the other a circle tier. No servers talk directly during the handshake — you carry
three signed JSON blobs (as QR codes or copy/paste) between the two browsers.

---

## 0. Prerequisites (operator, once per instance)

An instance can only federate if two things are true. On the household + xidra
instances these are **already done** — this section is for any new instance.

1. **Durable federation identity.** Each instance signs pairings with an Ed25519
   key. If that key is ephemeral it regenerates on every restart and *every
   existing pairing silently breaks on the next deploy*. Provision a persisted
   key (etcd-backed k8s Secret) once:

   ```bash
   bin/provision_federation_identity.py --namespace <ns> --verify
   ```

   The manifest mounts the `federation-identity` Secret read-only and points
   `FEDERATION_IDENTITY_PERSISTED_KEY_PATH` at it. Set
   `FEDERATION_REQUIRE_PERSISTENT_IDENTITY=true` so the backend **fails to boot**
   (loudly) rather than come up with a throwaway key. Rotating the key is
   destructive to all pairings — same class as rotating `SECRET_KEY`.

2. **A reachable advertised URL.** The peer needs to know where to send queries
   *back*. Set `FEDERATION_ADVERTISED_URL` per instance:

   | instance | value |
   |---|---|
   | household (`renfield` ns) | `http://backend.renfield.svc.cluster.local:8000` |
   | xidra (`renfield-xidra` ns) | `http://backend.renfield-xidra.svc.cluster.local:8000` |

   Same-cluster instances use internal service DNS (as above). Cross-internet
   federation uses a public `https://…` URL instead. Without this the pairing
   still completes but the *first query* dies with **"Peer has no usable transport
   endpoint"** (`_select_endpoint` returned `None`).

Verify both live:

```bash
kubectl -n <ns> exec deploy/backend -c backend -- python3 -c "
from api.routes.federation_pairing import _resolve_advertised_endpoints
import services.federation_identity as f
f.get_federation_identity()
print('advertised:', _resolve_advertised_endpoints([]))
print('pubkey:', f._instance.public_key_hex()[:16], 'from_disk:', f._loaded_from_disk)"
```

`from_disk: True` = the identity is persisted (good). A stable pubkey across a
pod restart is the real proof.

---

## 1. The pairing walkthrough

Open **both** instances, each in its own browser tab, and on each go to
**Einstellungen → Kreise** (`/settings/circles`). Decide which side is A and
which is B — it's symmetric, either instance can initiate.

### Step 1 — Initiator (A) creates the invite

On instance **A**: click **"Neue Kopplung starten"**.

- Leave **Erreichbare URL (optional)** blank to use A's configured
  `FEDERATION_ADVERTISED_URL` (the normal case). Only fill it to override the
  endpoint for this one pairing (e.g. a specific public hostname).
- A signed **offer** appears as a QR code + copyable JSON. Hand it to B (scan
  the QR, or copy the JSON and paste it into B's tab).

### Step 2 — Responder (B) accepts

On instance **B**: click **"Kopplungs-Einladung annehmen"**.

1. Paste A's offer JSON.
2. Review A's identity (display name + pubkey) and pick the **circle tier** to
   grant A — see *"What the tier does"* below.
3. Optionally set B's own **Erreichbare URL** (same rule as A's).
4. Click **"Kopplung annehmen"**. B's side is now **saved**, and a signed
   **response** appears as a QR + JSON. Hand it back to A.

### Step 3 — Initiator (A) completes

Back on instance **A**: paste B's response JSON, pick the **tier** to grant B,
and finish. The handshake is now **live on both sides**.

### Step 4 — Verify

On each instance, open **Kreise → Peers** (`/settings/circles/peers`). Each side
now lists the other peer with its pubkey and the tier you granted. The connection
is established.

---

## 2. What the tier does (and what's still deferred)

At pairing each side grants the other a **circle tier** (0 self … 4 public). This
records a `CircleMembership` for the remote peer — the same ladder that gates
local sharing (see [`CIRCLES.md`](CIRCLES.md)).

A federated peer's queries run **peer-scoped**: the filter keeps
`(public) OR (content shared to the tier you granted them)` and **drops** the
owner-equality and explicit-grant branches (`circle_sql.circles_filter_clause(
peer_scoped=True)`). So the tier is honored today — grant **household (2)** and
the peer reaches what you've shared at household level; grant **public (4)** and
they see public only. This is the collision-safe default: a federated `asker_id`
must never be treated as the local owner.

> [!IMPORTANT]
> **What is *not* yet reachable: the same person's full private brain across
> instances.** Peer-scoped reach only exposes what the *owner shared to that
> tier* — it does **not** let `evdb` logged into xidra read everything `evdb`
> can read at home (their own private, owner-tier, explicitly-granted content).
> That "act as the same person" escalation is the **person-link** (Piece 3 /
> F-ID-2): the mapped path runs the query *as* the linked local user with
> `enforce_circles=False`. It is designed but **not yet user-establishable** —
> today a link can only be created by an admin DB write, so it's deferred. This
> handshake therefore proves the transport and gives you public + granted-tier
> reach; it is not yet a full personal-brain bridge.

---

## 3. Managing & revoking peers

- **List:** `/settings/circles/peers` — shows granted tier + last-seen.
- **Re-tier:** change the tier from the peers page.
- **Revoke:** `DELETE /api/federation/peers/{peer_id}` (or the peers-page
  control) — removes the peer; the other side still holds its record until it
  revokes too (revocation is per-side).

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| First query: **"Peer has no usable transport endpoint"** | `FEDERATION_ADVERTISED_URL` unset on the side being queried → `transport_config.endpoints=[]` | Set the advertised URL (§0.2), re-pair (endpoints are baked in at pair time). |
| Pairing worked, breaks after a redeploy | Ephemeral identity key regenerated → pubkey changed | Provision the persisted-identity Secret (§0.1) and re-pair. |
| **"Pairing handshake failed"** on complete | Wrong/edited/expired JSON, or offer↔response nonce mismatch | Restart the handshake from Step 1 with a fresh offer. |
| Peer reaches only public content | Expected — see §2 | Person-link (Piece 3) not shipped yet. |

---

## 5. API reference (for scripted pairing)

All under `/api/federation`, authenticated as the local user:

| step | endpoint | body | returns |
|---|---|---|---|
| A step 1 | `POST /pair/offer` | `{offered_endpoints: [{url}]}` (or `[]` for the default) | signed offer |
| B step 2 | `POST /pair/accept` | `{offer, my_tier_for_you, accepted_endpoints}` | signed response (B saved) |
| A step 3 | `POST /pair/complete` | `{response, their_tier_for_me}` | peer record (live) |
| both | `GET /peers` · `DELETE /peers/{id}` | — | list / revoke |
| both | `GET /identity` | — | this instance's pubkey |

The offer, response, and the advertised endpoints are all Ed25519-signed; the
receiving side verifies the signature before storing anything.
