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

3. **Optional: a name for the instance.** `FEDERATION_INSTANCE_NAME` is what the
   *other* side shows for this instance ("Instance A"). Unset, the host of the
   advertised URL is sent instead. See §3a.

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
- A signed **offer** appears as a QR code + the JSON as selectable text. Hand
  it to B (scan the QR, or copy the JSON and paste it into B's tab).
  The copy button needs a *secure context*: on an instance served over plain
  HTTP, or one whose self-signed certificate the browser does not trust, the
  browser refuses the clipboard. The dialog then says so and selects the text;
  copy it with Ctrl+C / Cmd+C (before 2026-11-08 the refusal was silent and
  the JSON was shown only as a QR code).

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

The pairing also registers an MCP server `peer_<id>` exposing one tool,
`mcp.peer_<id>.query_brain`, which is how the agent loop reaches the other
instance. 🛑 **A pairing the agent loop cannot see is not a pairing.** Until
2026-10-28 `sync_peers` ran only at app startup and on revoke, so a pairing made
while the backend was running stayed invisible until the next restart: the row
sat in `peer_users`, the tool was absent from the manager's index, and a
federated question failed exactly as if the handshake had never happened.
Measured on both instances that day — pods up 13:35 UTC, pairings live 14:22
UTC, no tool registered. All three mutating routes (`pair/accept`,
`pair/complete`, `revoke`) now go through `_resync_peer_registry`, and
`TestPeerRegistryResync` fails if a fourth one forgets.

---

## 2. What the tier does (and what's still deferred)

At pairing each side grants the other a **circle tier** (0 self … 4 public). This
records a `CircleMembership` for the remote peer — the same ladder that gates
local sharing (see [`CIRCLES.md`](CIRCLES.md)).

> [!CAUTION]
> **The ladder runs the opposite way to the intuition: a LOWER number grants
> MORE.** The SQL is `cm.value <= atom.tier` (`circle_sql.py:196`) — a member
> placed at tier 0 reaches atoms at tier 0 and wider, which is everything;
> a member at tier 4 reaches only public. So granting `0` opens the whole
> instance and granting `4` is the most restrictive choice. Anyone reaching for
> "0 = least" here opens their brain to the peer. Nearly made that mistake on
> 2026-10-27; this note is the result.

A federated peer's queries run **peer-scoped**: the filter keeps
`(public) OR (this owner AND the tier you granted them)` and **drops** the
owner-equality and explicit-grant branches (`circle_sql.circles_filter_clause(
peer=PeerScope(owner_id, tier))`). Seit F-ID-3 (2026-10-28) kommt die Stufe aus
`peer_users.granted_tier` — der Paarungszeile selbst — statt aus einer
`CircleMembership`, die auf der ENTFERNTEN Nutzerkennung keyte. So the tier is honored today — grant **household (2)** and
the peer reaches what you've shared at household level; grant **public (4)** and
they see public only. This is the collision-safe default: a federated `asker_id`
must never be treated as the local owner.

> [!IMPORTANT]
> **What is *not* yet reachable: the same person's full private brain across
> instances.** Peer-scoped reach only exposes what the *owner shared to that
> tier* — it does **not** let `<user>` logged into xidra read everything `<user>`
> can read at home (their own private, owner-tier, explicitly-granted content).
> That "act as the same person" escalation is the **person-link** (Piece 3 /
> F-ID-2): the mapped path runs the query *as* the linked local user — since
> F-ID-3 (2026-10-28) that is `mapped=True` on the pending record, which makes
> `_peer_scope_for` return `None`, i.e. no peer scoping at all (it was
> `enforce_circles=False` before). Today an ADMIN can create/list/delete links via
> `GET/POST/DELETE /api/federation/user-links` (`federation_user_links.py`); their
> effect is gated on `FEDERATION_IDENTITY_LINKS_ENABLED`, **on in both instances
> since 2026-10-27** (renfield #1418 / x-ren !31) — it was a no-op at the time
> of flipping, since both carried zero link rows, and it takes effect with the
> first link. The responder additionally requires `AUTH_ENABLED=true` and a
> `querier_ref` on the request, and refuses to map under auth-off
> (`federation_query_responder.py:308-318`). The deferred part is
> ~~the **user-establishable consent handshake** (F-ID-2).~~ **Built and shipped
> 2026-10-27.** A person now establishes their own link through the
> double-login handshake under *Verknüpfte Identitäten* on the peers page —
> signed in on both instances, each side signing its own half. The admin CRUD
> above stays as break-glass only.
>
> So this pairing handshake proves the transport and gives public + granted-tier
> reach; the **person link** on top of it is what turns that into a same-person
> bridge, and it is now self-service rather than an admin assertion.

---

## 3. Managing & revoking peers

- **List:** `/settings/circles/peers` — shows granted tier + the time of the
  peer's last request to us (not a connectivity state). By default an account
  sees only the pairings it owns, although every local account can USE every
  active pairing. With `FEDERATION_PEERS_VISIBLE_TO_ALL=true` every account sees
  all active pairings of the instance, who paired each one, and which reach
  applies to itself (linked: asks as itself; unlinked: the far side's pairing
  decides). The granted tier on a row is what THIS instance gives the far side,
  not what the viewer receives over there.
  The time label reads "last request … ago" / "never asked" REGARDLESS of the
  flag: that wording is a copy correction (the value always was a request
  time), not part of the gated feature.
- **Re-tier:** change the tier from the peers page.
- **Revoke:** `DELETE /api/federation/peers/{peer_id}` (or the peers-page
  control) — removes the peer; the other side still holds its record until it
  revokes too (revocation is per-side). Owner only; with
  `FEDERATION_PEERS_VISIBLE_TO_ALL=true` also an account holding `admin`.

---

## 3a. What a pairing is called, and whether it is reachable

### The instance name

A pairing row carried one name: the **person** who paired on the far side. When
one person owns both instances, each side showed the other under that person's
name. A pairing now also carries the far **instance's** own name, shown first:
"Instance B · alex".

- **Sent by:** `FEDERATION_INSTANCE_NAME`; unset, the host of
  `FEDERATION_ADVERTISED_URL`; neither, nothing. Never a person's name.
- **Carrier:** two extra top-level fields, `instance_name` and
  `instance_name_signature`, on the pairing offer and response, on
  `query_brain/initiate` and the terminal `query_brain/retrieve` answer, and on
  the reachability ping and its answer. They sit **outside** every envelope's
  signed payload; the second is an Ed25519 signature by the same key over
  `{"purpose": "renfield.federation.instance_name.v1", "instance_name",
  "pubkey", "envelope_signature"}`, which binds the name to the one envelope it
  rode on.
- **Mixed versions, both directions:** an older instance drops the two unknown
  fields and verifies the envelope exactly as before; a newer instance that
  receives nothing keeps the fallback label. An instance with no name to send
  emits the same JSON it always did. Existing pairings learn the name with the
  next query or ping — no re-pairing.
- **Accepted only** after the envelope verified against a paired, non-revoked
  key. An attachment that does not verify is dropped and the request stands.
- **Cleaned before it is stored:** NFKC; control and format characters, blank
  fillers (U+3164, U+115F, U+1160, U+FFA0, U+2800, U+034F) and variation
  selectors dropped; at most two combining marks per base character and none
  without one; whitespace collapsed; 64 characters. A name with no **visible**
  character left counts as not sent.
- **Renamed at most once per pairing per 10 minutes**
  (`FEDERATION_INSTANCE_RENAME_MIN_INTERVAL_SECONDS`, default 600 — a choice,
  not a measurement). The far side picks the text and sends it with every
  request; a change inside the interval is ignored, an unchanged name costs
  no statement and no registry resync. Each accepted change is logged at INFO
  with the pairing id and the two lengths, never the name.
- 🛑 **Display text only.** Never an identifier, never a lookup key, never part
  of a server or tool name — `peer_<id>` and the pubkey remain the only keys.
- **Shown as** instance name → host of the stored endpoint → the person alone,
  on the peers page, the audit view and the chat status line. **The kiosk never
  shows a host:** the verified instance name, else the person alone — its
  payload is content-free and a host name is an address. The audit view shows
  the name the **row recorded** when it was written
  (`federation_query_log.peer_instance_name_snapshot`); rows from before carry
  none and show the person alone.
- 🛑 **The name is NOT in the agent's prompt unless this instance says so.** A
  paired peer chooses its name and can change it without local consent, and a
  tool description is part of the local agent's prompt. The named variant of
  the description is therefore behind its own flag,
  `FEDERATION_PEER_NAME_IN_TOOL_DESCRIPTION_ENABLED`, **default off**: without
  it the description is byte for byte the measured text, whatever name is
  stored. With it, only an allow-list form goes in (NFKC; letters, digits,
  `_`, space, `.`, `-`; 32 characters; never the display string, never the
  endpoint-host fallback; nothing usable left → the unnamed variant). The
  named wording is pinned in `test_peer_tool_description.py` but
  **unmeasured; measure with `bin/measure-preselect.py` before enabling.**
- **Migrate before the rollout:** revision `pc20261113_peer_instance_name` adds
  `peer_users.remote_instance_name` and
  `federation_query_log.peer_instance_name_snapshot`
  (`bin/deploy-production.sh … --migrate`, each instance).

### Reachability (`FEDERATION_REACHABILITY_ENABLED`, default off)

Three statements that used to be one light:

| | from |
|---|---|
| **paired** | the row exists and is not revoked |
| **recently active** | a real request in either direction within 5 minutes (`last_seen_at` inbound, the newest `asked` audit row outbound) |
| **reachable / unreachable / unknown** | a signed round trip, `POST /api/federation/peer/ping` |

The ping: `{version, asker_pubkey, nonce, timestamp, signature}`, signed over
`{"kind": "ping", …}`. It is answered only to a paired, non-revoked key, inside
a ±60 s window, once per nonce, and under a budget of its own
(`FEDERATION_PING_RATE_PER_MINUTE`, both sides). Everything else gets the same
uniform 400 a stranger's query gets; with the flag off the route answers 404
whatever is posted. The answer `{version, status: "ok", nonce, answered_at,
responder_pubkey, responder_signature}` is checked like a query's — paired key,
signature, **our** nonce — so a recorded answer cannot be replayed.

The route is reachable without a pairing (a signature can only be checked once
the body is there), so an outsider must not be able to make it hold or write
anything:

- **Body cap, 4096 bytes, both directions.** A declared `Content-Length` above
  it is refused unread; a body without one is read only up to the cap. The
  asking side reads the answer under the same cap.
- **Order of the checks: signature → timestamp → pairing → rate bucket →
  nonce.** Everything that writes comes after the pairing lookup: an unpaired
  key leaves no nonce (the nonce store is capped — written earlier, a stream
  of unpaired pings could push real nonces out of it), no bucket and no row.

A ping is not a request: it does not move `last_seen_at`, writes no audit row
and spends no query budget.

**Who probes, and when:**

- **a consumer-gated periodic check, every 5 minutes per pairing while at
  least one kiosk is connected.** This is a sanctioned exception to the
  project's no-polling rule, approved by the owner on 2026-11-14: whether
  another instance is reachable cannot be known without asking it. Its bounds:
  consumer-gated (with no display connected, nothing runs), 5 minutes, one
  small signed request per pairing, a claim per pairing plus the outbound ping
  budget. Mechanically: the kiosk refresher's 60 s timer probes a pairing once
  its reading is older than 300 s, plus one round when a display connects (in
  the background; the snapshot never waits for another instance);
- a person: "check now" on the peers page (`POST /peers/{id}/check` — the owner,
  an `admin` holder, or with `FEDERATION_PEERS_VISIBLE_TO_ALL` every account),
  and once when the page shows a row that has no reading. The request's
  database session is released before the round trip goes out.

Readings live in Redis under a 15-minute TTL, not in the database: an
observation nobody refreshed ceases to exist and the pairing reads `unknown`
again. A claim (`SET NX EX 30`) per pairing makes several backend processes —
or a refresher and an impatient person — send one ping, not several.

🛑 `unsupported` (the far side answers 404: one release behind, or its flag is
off) is **unknown**, never unreachable. Both sides need the flag for a verdict.

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| First query: **"Peer has no usable transport endpoint"** | `FEDERATION_ADVERTISED_URL` unset on the side being queried → `transport_config.endpoints=[]` | Set the advertised URL (§0.2), re-pair (endpoints are baked in at pair time). |
| Pairing worked, breaks after a redeploy | Ephemeral identity key regenerated → pubkey changed | Provision the persisted-identity Secret (§0.1) and re-pair. |
| **"Pairing handshake failed"** on complete | Wrong/edited/expired JSON, or offer↔response nonce mismatch | Restart the handshake from Step 1 with a fresh offer. |
| Peer reaches only public content | Expected — see §2 | Person-link (Piece 3) not shipped yet. |
| Peers page / kiosk: **reachability unknown**, reason "does not answer reachability checks" | The far side is one release behind or has `FEDERATION_REACHABILITY_ENABLED` off | Switch it on there too (§3a). The pairing itself may be fine. |
| **Unreachable**, reason "certificate is no longer the one remembered" | The far side's TLS certificate was renewed; the pin from pairing is stale | Queries fail the same way. Re-pair, or refresh the pin — waiting does not help. |
| **Unreachable**, reason "rejects the signed check" | Pairing revoked on the far side, clocks more than a minute apart, or its ping budget is spent | Check the far side's peers page and both clocks. |
| A peer is shown under a host name | It has not sent a name yet, or sets none | Set `FEDERATION_INSTANCE_NAME` there; the label follows with the next query or ping. |

---

## 5. API reference (for scripted pairing)

All under `/api/federation`, authenticated as the local user:

| step | endpoint | body | returns |
|---|---|---|---|
| A step 1 | `POST /pair/offer` | `{offered_endpoints: [{url}]}` (or `[]` for the default) | signed offer |
| B step 2 | `POST /pair/accept` | `{offer, my_tier_for_you, accepted_endpoints}` | signed response (B saved) |
| A step 3 | `POST /pair/complete` | `{response, their_tier_for_me}` | peer record (live) |
| both | `GET /peers` · `DELETE /peers/{id}` | — | list / revoke |
| both | `POST /peers/{id}/check` | — | `{state, reason, checked_at}` — one signed round trip now (404 while `FEDERATION_REACHABILITY_ENABLED` is off) |
| both | `GET /identity` | — | this instance's pubkey |

The offer, response, and the advertised endpoints are all Ed25519-signed; the
receiving side verifies the signature before storing anything.
