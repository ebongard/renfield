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
   | reached over the LAN | `https://<name>.local` |
   | reached over the internet | `https://<public name>` |
   | same cluster, in-cluster service DNS | `http://backend.<ns>.svc.cluster.local:8000` — **only** with `FEDERATION_ALLOW_HTTP_ENDPOINTS=true` on both instances |

   The value must pass the endpoint rules in §3b: `https://`, no credentials,
   no query, no fragment. Plain `http://` is refused by default, here (the
   offer is not created) and on the far side. Without an advertised URL the pairing
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

### How a question reaches the peer

Five layers sit between a chat message and `query_brain`, and each one can
lose the question on its own:

1. **Router** (`agent_router.classify`, one call to the intent model) picks a
   role. While at least one pairing is registered, the router prompt carries
   one extra rule behind the category list (`prompts/router.yaml`:
   `peer_rule` / `peer_rule_named`): a message that wants a paired instance or
   a person's Renfield to be **asked** goes to the federation role, whatever
   the question is about. The role is read from the configuration — the first
   role by name with `federation: true` (today `documents`), else a role with
   `mcp_servers: null` — never a literal.
2. **Planner** (`orchestrator.detect_multi_domain`, the main model; runs for
   every turn that is not `conversation`/`knowledge` while
   `AGENT_ORCHESTRATOR_ENABLED` is on) may split the turn into sub-queries
   for several roles. It builds its own role list, so it carries its own rule
   (`prompts/agent.yaml`: `orchestrator_peer_rule` / `…_named`), again only
   while a pairing is registered: everything that is to be asked of another
   instance stays in ONE sub-task for the federation role.
3. **Role filter** (`agent_tools.py`): the role must be offered the peer's
   tool — `federation: true`, see `.claude/rules/internal-tools.md`.
4. **Tool pre-selection** (`_preselect_tools`): a further model call that reads
   the tool **description**; neither rule reaches it.
5. **Agent**: chooses among the pre-selected tools.

Why the rule exists (measured on the household, 2026-11-15): "Frag
\<instance\>, welche Dokumente es dort zum Thema X gibt" was classified as
`conversation` — a role without tools, so no pre-selection, no agent loop, no
federated call, and a plain answer. No role description mentioned asking
another instance; only the topic "documents" could attract such a question to
the one role that reaches a peer. Asking another instance is not a topic, so
it is a rule of its own and not part of a role description.

- **Without a pairing the router prompt is byte for byte unchanged** (the rule
  is appended to the category block, the template is untouched). Checked
  against `origin/main` and held by `test_router_reaches_paired_instance.py`.
- **Live, not at start-up:** the router asks the peer registry on every turn,
  so a pairing made or revoked while the backend runs takes effect at once.
- **Not per account.** Every local account can use every active pairing, and
  the router prompt is shown to nobody; a rule narrower than the tool would
  leave the other accounts with the gap it closes.
  `FEDERATION_PEERS_VISIBLE_TO_ALL` concerns the peers page only.
- **Names:** both rules list instance names only under
  `FEDERATION_PEER_NAME_IN_ROUTER_PROMPT_ENABLED` (default off) and only in
  the allow-list form (§3a). That switch is **separate** from the one for the
  tool description, in both directions: the router prompt is built for every
  routed turn of every account, a tool description only biases the
  pre-selection inside an agent turn. One function decides both
  (`peer_mcp_registry._prompt_instance_name`). Without names the rules still
  cover "frag \<jemand\>s Renfield" and "die andere Instanz"; a bare
  instance name ("frag Nordhaus …") is recognised reliably only with the
  switch on.
- 🛑 **An instance named like a person.** With names on, "frag \<Name\>, ob
  sie heute Zeit hat" goes to the far instance when a pairing carries that
  name — the question leaves this instance. The far side chooses its name, so
  this is also what the switch opens to a peer that wants it. No wording can
  tell a person from an instance of the same name, and there is deliberately
  no keyword logic in code. The bench plays the case as variant CLASH,
  unscored. Asking a person who is **not** an instance ("frag \<Person\>,
  ob …") is a scored control.
- **Not diverted:** telling a *person* something ("sag ihm/ihr …") stays with
  `presence`, and "frag mich …" is not a question to an instance; the router
  rule says both, and names no role but its target.
- **Why the planner has the rule, not a skip.** The router's rule made a new
  path reachable: before it such a question was `conversation` and never met
  the planner. "Frag \<instance\>, welche Termine anstehen und wer zuhause
  ist" would be split by topic, the presence sub-query would run on a role
  without the federation switch, be answered from THIS house, and be
  synthesised as the far instance's answer. Skipping the planner whenever the
  router picked the federation role would be wrong the other way: the router
  returns a role, not its reason, and an ordinary document question on that
  role must still be split. Only the model that reads the message can tell.
- **Measured** (2026-11-15, `bin/measure-router.py`, `qwen3:8b`, the rule
  wording of commit `f6010447`; 41 cases × 3 repetitions per variant,
  interleaved, 375 calls). Peer-addressed turns reaching a role that can ask:

  | | no pairing | name-free rule | named rule |
  |---|---|---|---|
  | peer-addressed | 0/45 | 24/24 scored; bare instance names 15/21 | 39/45 |
  | controls | 69/78 | 72/78 | 72/78 |

  No control regressed. Relay, broadcast, "Wer ist gerade zuhause?", the four
  "frag \<Person\>, ob …" controls, "frag mich …", smart home, research,
  media and routine stayed where they were in all three. One control
  **moved**: "Welche Dokumente gibt es zum Thema Heizung?" went from `general`
  to `documents` with the rule present — inside its expected set, but the
  rule's mere presence shifted a turn that addresses no instance. Two
  controls miss in every variant (not caused by the rule). CLASH sent both
  marked cases ("frag \<Name\>, ob …" with an instance of that name) to the
  federation role 3/3, as described above. An earlier wording (commit
  `400451fc`) was measured too; it lacked the sentence about a topic's
  category and scored 18/18 name-free and 36/39 named.
- 🛑 **The NAMED variant is weaker on presence questions than the name-free
  one.** "frag \<jemand\>s Renfield, wer bei ihm gerade zuhause ist" reaches
  the federation role 3/3 with the name-free rule and stays with `presence`
  3/3 with the named rule; "frag \<instance\>, wer gerade zuhause ist" stays
  with `presence` 3/3 in both. A turn that stays with `presence` is answered
  from THIS house. Together with the name clash above, this is a reason to
  leave `FEDERATION_PEER_NAME_IN_ROUTER_PROMPT_ENABLED` off until the named
  text has been reworked and measured again. The name-free rule's remaining
  misses are bare instance names it cannot know ("Was weiss \<instance\>
  ueber …" → `knowledge`, the presence question above).
- **The planner's rule, measured** (2026-11-15, `bin/measure-planner.py`,
  the main model on llama-server, rule wording of commit `608e3ca2`; 17 cases
  × 3 variants × 3 repetitions, interleaved, 153 calls, 0 errors). A
  multi-topic question to an instance counts as right when it is NOT spread
  over roles that cannot ask — no plan, or every sub-task for the federation
  role:

  | | no pairing | name-free rule | named rule |
  |---|---|---|---|
  | multi-topic question to an instance | 0/30 | 15/15 scored; bare instance names 12/15 | 30/30 |
  | ordinary two-domain request, must be split | 15/15 | 15/15 | 15/15 |
  | single domain, must stay whole | 6/6 | 6/6 | 6/6 |

  Without the rule the planner split **every** such question by topic
  (presence, smart_home, documents, research, media) — the confidently wrong
  answer described above was the regular case, not a corner. With the rule
  it keeps them whole, and what should be split still is. One of the 30 named
  answers was a plan of two sub-tasks, both for the federation role; that
  counts.
- **Residual, name-free rule:** a question that names the instance only by
  its bare name can still be split, because without
  `FEDERATION_PEER_NAME_IN_ROUTER_PROMPT_ENABLED` the planner does not know
  the name: "Was weiss \<instance\> ueber die naechste Wartung und ueber das
  Wetter am Wochenende?" came back as `documents` + `research` 3/3. The named
  rule closes that for the planner — while the named ROUTER rule has the
  presence weakness above, and both hang on the one switch.
- **Measure before changing a word**: the model follows example strings, which
  is why the rules use placeholders only.
- **Not covered — what still decides a turn before the router model:**
  - *Role hint* (palette, "Korrigieren & neu beantworten"): takes the hinted
    role as is.
  - *Entity-id route*: a recognised entity id routes to its domain at once.
  - *Continuity*: a short follow-up (score ≥ 0.6) stays in the active domain
    — "und was ist mit dem Mietvertrag?" after a peer question stays on the
    federation role only because that role was the active domain; after a
    smart-home turn it stays there.
  - *Semantic fast path*: an embedding match against the roles' example
    utterances returns a role without the model seeing the rule. A
    peer-addressed message that resembles an utterance of another role is
    routed there.
  None of these knows the rule; the bench bypasses all of them and measures
  the model call alone.
- **Not covered after the router:** the planner follows its rule only as far
  as a model follows a prompt; there is **no deterministic backstop** against
  a split. Tool pre-selection and the agent's choice between the peer's tool
  and local document tools are measured separately
  (`bin/measure-preselect.py`), not for these turns.
- **Considered and rejected: a backstop in code.** In `chat_handler`, after
  the planner returns two or more sub-queries: if a pairing is registered and
  any sub-query targets the federation role, drop the plan and run the single
  role the router chose. Rejected on the measurement — the planner kept every
  scored peer question whole, so the backstop would buy nothing there — and
  on its cost: the federation role is `documents`, so while a pairing exists
  every genuine "documents + something else" request would stop being split
  and one half would go undone. It would also not have caught a plan spread
  entirely over roles that cannot ask, nor a turn the router already sent
  elsewhere. Reconsider only if a re-measurement shows the planner splitting
  peer questions with the rule in place.

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
> `enforce_circles=False` before). An ADMIN can list and delete links, and assert
> one for its OWN account, via `GET/POST/DELETE /api/federation/user-links`
> (`federation_user_links.py`, bounds in §3b); their effect is gated on `FEDERATION_IDENTITY_LINKS_ENABLED`, **on in both instances
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
  Revoking deletes every person link of the pairing and is recorded (§3b).

---

## 3b. Person links: who may create and remove one, and what is recorded

A person link lends the far side one local account's **full own reach** in
place of the pairing's granted tier. The bounds, decided on 2026-11-14:

| | who | how |
|---|---|---|
| create, by proof | the account that **owns the pairing**, for itself | the handshake `POST /links/offer` → `/links/accept` → `/links/complete` |
| create, by assertion | an `admin` holder, **for itself only**, on any active pairing | `POST /user-links` — the linked account is the signed-in one |
| remove | the linked account | `DELETE /links/{id}` |
| remove | an `admin` holder, any link | `DELETE /user-links/{id}` |
| remove all of a pairing | whoever may revoke the pairing | `DELETE /peers/{id}` |

- **Nobody links another account.** Until 2026-11-14 `POST /user-links` took
  `local_user_id` from the body: an `admin` holder could map a `querier_ref`
  to any local account, and the far side presenting that ref was served with
  that account's full reach, tier 0 included, without its consent. The field
  is still accepted; naming a different account is a 403.
- **`GET /user-links` shows a `querier_ref` only to the account it belongs
  to.** With another account's ref an admin could delete that link, re-create
  it for itself and ask the far side as that person.
- **An account that does not own the pairing cannot use the handshake.** It
  uses the pairing unlinked, on the far side's granted tier. Whether such an
  account should be able to link itself is an open product decision; today
  only the assertion above does that, and only for an `admin` holder.
- **A link ends with its pairing and with its account.** Revoking a pairing
  deletes its links — the row is kept and pairing again revives it under the
  same id, so a link left behind would be live again without anyone linking
  anew. The same cleanup runs when a REVOKED pairing is paired again and when
  an already revoked pairing is revoked once more: a pairing revoked before
  2026-11-14 still carries its links. Pairing again over an ACTIVE pairing
  keeps them. A deactivated account is not served through its link.
- **A refused handshake step answers the one uniform 400.** Until 2026-11-14
  it left as a 500 (the route read the signed-in account after the rollback).

**Every revocation is recorded** in `federation_query_log`, in the same
transaction as the revocation itself:

| column | value |
|---|---|
| `direction` | `revoke` |
| `final_status` | `pairing_revoked` or `link_revoked` |
| `identity_basis` | on whose authority: `owner`, `self`, or `admin` (acting on someone else's row) |
| `user_id` | always NULL (see below) |
| `peer_user_id`, `peer_pubkey_snapshot` | the pairing |
| `initiated_at` | when |
| `query_text` | JSON of ids: `action`, `actor_user_id`, `owner_user_id`, `subject_user_id` (the pairing's owner, or the account the link belonged to), `peer_id`, `as_admin`; a link row adds `link_id`, a pairing row `links_removed` |

Ids only: the display-name snapshots stay empty, since those are text the far
side chose. The rows are not queries — the audit view (`GET /audit`) leaves
them out, and the 90-day retention does not prune them. There is no view for
them yet; they are read in the database.

- **One row per link.** A pairing revoke removes links of accounts other than
  the one revoking, so each removed link gets its own `link_revoked` row with
  `cause` `pairing_revoked` (or `pairing_revived`, when a revoked pairing is
  paired again); the pairing row carries the count.
- **`user_id` is NULL on these rows.** The column references `users.id`
  without a delete rule and the rows are never pruned: filed under an account
  — the affected or the acting one — a single revocation would make that
  account undeletable for good. The accounts are named in the JSON.
- 🛑 **Migrate before the rollout:** revision `pc20261114_fed_log_user_null`
  drops the NOT NULL on `federation_query_log.user_id` and puts a CHECK in
  its place (`direction = 'revoke' OR user_id IS NOT NULL`): a query row
  without an account stays impossible in the database
  (`bin/deploy-production.sh … --migrate`, each instance). A new pod on an
  unmigrated database cannot write the record, and since the record shares
  the transaction of what it records, until the migration ran **every
  revoke of a pairing or a link fails, and so does pairing again with a
  revoked pairing that still carries links** (pairing again without such
  leftovers writes no record and works). The foreign key and its delete rule
  are unchanged. The downgrade refuses while rows without a `user_id` exist.

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
- **Cleaned before it is stored:** NFKC, with Thai and Lao SARA AM (U+0E33,
  U+0EB3) left as written — NFKC splits them and the nikhahit then lands
  behind a tone mark; control and format characters, blank fillers (U+3164,
  U+115F, U+1160, U+FFA0, U+2800, U+034F, U+FFFC, U+1D159) and variation
  selectors dropped; at most four combining marks per base character (pointed
  Hebrew takes four, Devanagari three) and none without one; whitespace
  collapsed; 64 characters. A name without at least one **letter or number**
  counts as not sent — a lone `.` or a row of dashes is not a name.
- **Renamed at most once per pairing per 10 minutes**
  (`FEDERATION_INSTANCE_RENAME_MIN_INTERVAL_SECONDS`, default 600 — a choice,
  not a measurement). The far side picks the text and sends it with every
  request; a change inside the interval is ignored, an unchanged name costs
  no statement and no registry resync. Each accepted change is logged at INFO
  with the pairing id and the two lengths, never the name.
- 🛑 **Display text only.** Never an identifier, never a lookup key, never part
  of a server or tool name — `peer_<id>` and the pubkey remain the only keys.
- **Shown as** the verified instance name, else the person alone — on the
  kiosk, in the chat status line, in the audit view and in the tool
  description. 🛑 **An endpoint host is an address, not a name:** it heads no
  label and is never stored. Only the peers-page data still carries it
  (`peer_instance_label`: `instance_name` on a pairing, `peer_instance_name`
  on a person link), always next to `instance_name_verified` /
  `peer_instance_name_verified`. Where that says `false`, the row is headed
  by the person with the hint "instance name not yet known" and the host is
  not shown. A check that comes back `reachable` reads the list once more, so
  the name it delivered replaces the hint in place. The audit view shows the
  name the **row recorded** when it was written
  (`federation_query_log.peer_instance_name_snapshot`) — a verified name or
  NULL; rows from before the instance name carry none and show the person
  alone. **Rows written between the instance-name release and this change may
  hold an endpoint host in that column; they are not rewritten** (no data
  migration) and keep showing it.
- 🛑 **The name is NOT in the agent's prompt unless this instance says so.** A
  paired peer chooses its name and can change it without local consent, and a
  tool description is part of the local agent's prompt. The named variant of
  the description is therefore behind its own flag,
  `FEDERATION_PEER_NAME_IN_TOOL_DESCRIPTION_ENABLED`, **default off**: without
  it the description is byte for byte the measured text, whatever name is
  stored. With it, only an allow-list form goes in (NFKC; characters of the
  Unicode categories Lu, Ll, Lt, Lo and Nd plus `_`, space and `-`; no `.`,
  so no sentence boundary; no modifier letters, which look like quotes and
  colons; 32 characters; never the display string, never the endpoint-host
  fallback; nothing usable left → the unnamed variant). 🛑 The allow-list
  bounds **form and length, not content** — a plain imperative of 32 letters
  passes it. That is why the flag is off. The named wording is pinned in
  `test_peer_tool_description.py` but **unmeasured; measure with
  `bin/measure-preselect.py` before enabling.** This flag covers the tool
  description only. The names in the **router's and the planner's** peer rule
  (§1, "How a question reaches the peer") are behind a second switch,
  `FEDERATION_PEER_NAME_IN_ROUTER_PROMPT_ENABLED`, default off — same
  allow-list form, wider reach.
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
uniform 400 a stranger's query gets; with the flag off the path answers 404
whatever is sent to it — any method, with or without a trailing slash — and is
absent from the published OpenAPI schema, exactly like a version that does
not have it. The answer `{version, status: "ok", nonce, answered_at,
responder_pubkey, responder_signature}` is checked like a query's — paired key,
signature, **our** nonce — so a recorded answer cannot be replayed.

The route is reachable without a pairing (a signature can only be checked once
the body is there), so an outsider must not be able to make it hold or write
anything:

- **Body cap, 4096 bytes ON THE WIRE, both directions.** A declared
  `Content-Length` above it is refused unread; a body without one is read only
  up to the cap. The asking side reads the answer under the same cap.
- 🛑 **A compressed body is refused, never inflated.** `Content-Length` counts
  compressed bytes, and a few kilobytes of gzip inflate to megabytes in one
  chunk — past any cap counted after decoding. The ping therefore goes out
  with `Accept-Encoding: identity`, its answer is read raw, and any
  `Content-Encoding` other than `identity` is no answer (`invalid_response`),
  a genuine small one included. The route treats a request body the same way
  (the uniform 400).
- **One deadline for the whole body, `FEDERATION_INBOUND_READ_TIMEOUT_SECONDS`
  (default 10 s).** The cap bounds bytes, not time: a caller sending one byte
  every few seconds stays inside any cap. The deadline covers the whole read,
  not each chunk, and applies to all three peer routes (`ping`,
  `query_brain/initiate`, `query_brain/retrieve`); the two query routes are
  capped at 64 KiB by the same reader. On expiry: the uniform 400.
- **Order of the checks: signature → timestamp → pairing → rate bucket →
  nonce — for the ping AND for `query_brain/initiate`.** Everything that
  writes comes after the pairing lookup: an unpaired key leaves no nonce (the
  nonce store is capped — written earlier, a stream of unpaired requests
  could push real nonces out of it), no bucket and no row.
- **A replay does not spend the pairing's budget.** Between the pairing
  lookup and the bucket stands a read-only look at the nonce store: an
  envelope whose nonce is already recorded is refused there, before a token
  is taken. The look writes nothing (so it cannot be used to fill the store)
  and runs only for an envelope that verified for a paired key (so it tells a
  stranger nothing). Recording the nonce stays the atomic claim and stays
  last.

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

## 3b. Which endpoint a peer may have

A peer's endpoint comes from the far side. It is signed — by the very party
the adversary-peer model is about — and this instance later sends requests to
it on its own: the TLS probe at pairing, every query, a ping every few
minutes. `services/federation_endpoint_guard.py` decides what is acceptable,
at two moments:

| when | what happens on a refusal |
|---|---|
| **pairing** (`/pair/accept`, `/pair/complete`), before the TLS probe and before the row is written | the uniform "Pairing handshake failed"; the reason is in the backend log (`Peer endpoint refused: …`). On `complete` the offer's nonce is not consumed. |
| **before every outbound request** (query, ping, and the certificate probe in front of both) | nothing is sent; the query fails with "Peer has no usable transport endpoint", the ping reads `no_endpoint`. Logged as `endpoint of peer <id> refused as a request target`. |
| **what this instance advertises** (`/pair/offer`, `/pair/accept`; the UI override and `FEDERATION_ADVERTISED_URL`) | 400 with a readable reason — form only, so the far side's refusal is not the first anyone hears of it. |

The rules:

- **`https://` only.** Plain `http://` is refused unless
  `FEDERATION_ALLOW_HTTP_ENDPOINTS=true` (default off). The envelope signature
  protects integrity, not the confidentiality of the question and the answer,
  and without TLS there is no certificate pin. Switch it on — on **both**
  instances — only where they talk over a network trusted as a whole (same
  cluster, overlay VPN). 🛑 A pairing stored with an `http://` endpoint before
  this rule existed stops being asked until the switch is on or it is re-paired
  over https.
- **No credentials, no query, no fragment**, no whitespace or backslash, at
  most 512 characters, a valid port, at most 8 entries per pairing message.
- **The host is a DNS name or an IP literal.** Numeric forms a C resolver
  reads as an address but that are no plain literal (`127.1`, `2130706433`,
  `0x7f.0.0.1`) are refused.
- **Never this machine or the link:** loopback (`127.0.0.0/8`, `::1`,
  `localhost`, `*.localhost`), link-local (`169.254.0.0/16` — the cloud
  metadata address — and `fe80::/10`), the IPv6 metadata address
  `fd00:ec2::254`, unspecified and the rest of `0.0.0.0/8`, multicast,
  reserved, deprecated site-local `fec0::/10`; also when wrapped in an
  IPv4-mapped, IPv4-compatible, 6to4 or NAT64 address. Checked on a literal,
  **and on every address a name resolves to** — the far side owns its DNS.
- 🛑 **A lookup that does not finish refuses.** Only a definite "no such name"
  (`EAI_NONAME`) passes unresolved — nothing can be sent there. A lookup that
  times out (3 s), fails temporarily (`EAI_AGAIN`) or fails any other way
  refuses the endpoint: a name server that answers late would otherwise pass
  the check every time, and the HTTP client then resolves the name afresh with
  a longer timeout. The price: during a DNS outage a pairing reads "no usable
  endpoint" (`no_endpoint`) instead of `unreachable`, and pairing with a peer
  whose name cannot be looked up right now fails.
- **Allowed on purpose:** private LAN ranges, `.local` names, single-label
  names. Household instances pair across a LAN.

### Known limits

Fixed on 2026-11-14 and no longer limits: `query_brain/initiate` recording a
nonce before the pairing lookup; a replayed ping spending the pairing's rate
bucket; the ping path answering 405 / a redirect with reachability off; the
inbound body read having no deadline; peer endpoints being stored and used
unchecked.

**A revoked pairing gets no answer.** The pairing is looked up when a request
is admitted, again by the background task before it retrieves and before it
stores its result, and again on every poll. The purge on revocation is final:
the pending store's `save` only updates an existing record, so a task that was
queued or in flight cannot bring a purged request back. A poll on a revoked
pairing is answered exactly like a poll for an unknown request. The responder
records such a query as `aborted`, stating whether data had been read before
the revocation arrived.

Reviewed on 2026-11-14 and left as they are:

- **A request can outlive the LINK it was admitted on, by one retrieval.** A
  link revocation commits and then discards the pending requests of that far
  side. A request that resolved its link just before the commit but was stored
  just after the purge can still be collected once, with the reach it was
  admitted on. The window is the gap between those two steps; closing it means
  carrying the link on the pending record and re-checking at retrieve time.
  For a revoked PAIRING this no longer holds, see below.
- **ZERO WIDTH JOINER and NON-JOINER are removed from a name**, with every
  other format character. Scripts that shape with them (Persian, several Indic
  conjuncts) and emoji sequences built on them lose that shaping; the letters
  stay.
- **The prompt form of a name carries no combining marks** (its allow-list has
  no mark category): a name in a script written with marks is thinned there,
  or left with nothing nameable — then the unnamed description applies. The
  display name is not affected.
- **The prompt allow-list is not an injection filter.** It bounds form and
  length; what 32 letters and spaces say is the far side's choice.
- **With reachability ON, a stranger can tell that it is on.** A refused ping
  is a 400, a path that does not exist a 404, and a GET on the ping path a 405.
  That difference is what lets the asking side tell `refused` (the pairing was
  revoked there, the clocks disagree, the budget is spent — red) from
  `unsupported` (older version or switched off — unknown), and a revoked
  pairing is the first thing a check has to find. Answering 404 to everything
  that is not a valid paired ping would hide the flag from a stranger and turn
  every revoked pairing into "unknown". What a stranger can NOT tell apart is
  "switched off" from "a version without the ping".
- **The flag is also readable from timing.** With reachability on, the route
  waits for a body (up to the read deadline) before it refuses; switched off,
  it answers at once.
- **`/openapi.json` and `/docs` are served by the backend** and list every
  other route of the running version. Hiding the ping path from the schema
  closes that for the ping only; whether the schema is reachable from outside
  is a question for the ingress.
- **Two copies of one fresh envelope racing each other** can both pass the
  read-only look; the second loses at the atomic record and has then spent one
  token. That takes a replay arriving while the original is still being
  handled and costs at most one token per original — a recorded envelope
  cannot be used for it twice.
- **Resolve-then-connect is not atomic.** A name can answer the endpoint check
  with one address and the connection with another (DNS rebinding). What that
  buys is bounded: a POST of a fixed-shape signed envelope to a fixed path,
  whose answer is discarded unless it carries the paired key's signature; with
  a certificate pin the connection is refused before anything is sent. A name
  the resolver definitely does not know passes the check — nothing can be
  sent to it.
- **`FEDERATION_ADVERTISED_URL` is checked for form only**, and only when an
  offer or an acceptance is made — not at boot.
- **A sleeping wall display.** The age of a reading is continued on the
  device's monotonic clock, which stands still on some tablets while they
  sleep. When the page comes back (`visibilitychange` with the monotonic clock
  seen standing still over the hidden period, a `pageshow` out of the
  back/forward cache, `online`) the kiosk shows every verdict as unknown /
  "checked, but the result is old" and reconnects for a fresh snapshot — one
  reconnect, no timer. What remains: a device that wakes **without** any of
  the three events keeps the too-young age until the next push (at most 10
  minutes on a live socket), and with the check off (the two-valued "asked us
  recently" light) nothing is degraded, since that rule cannot say unknown.

---

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| First query: **"Peer has no usable transport endpoint"** | `FEDERATION_ADVERTISED_URL` unset on the side being queried → `transport_config.endpoints=[]` | Set the advertised URL (§0.2), re-pair (endpoints are baked in at pair time). |
| **"Peer has no usable transport endpoint"** / reachability reason "no address" on a pairing that has one | The stored endpoint fails the endpoint rules (§3b) — typically `http://` from before 2026-11-14. Backend log: `endpoint of peer <id> refused as a request target` | Re-pair over `https://`, or set `FEDERATION_ALLOW_HTTP_ENDPOINTS=true` on both instances. |
| **"Pairing handshake failed"**, backend log `Peer endpoint refused: …` | The far side advertises an endpoint this instance does not accept (§3b) | Fix `FEDERATION_ADVERTISED_URL` on the far side and start again from Step 1. |
| Pairing worked, breaks after a redeploy | Ephemeral identity key regenerated → pubkey changed | Provision the persisted-identity Secret (§0.1) and re-pair. |
| **"Pairing handshake failed"** on complete | Wrong/edited/expired JSON, or offer↔response nonce mismatch | Restart the handshake from Step 1 with a fresh offer. |
| Peer reaches only public content | Expected — see §2 | Person-link (Piece 3) not shipped yet. |
| Peers page / kiosk: **reachability unknown**, reason "does not answer reachability checks" | The far side is one release behind or has `FEDERATION_REACHABILITY_ENABLED` off | Switch it on there too (§3a). The pairing itself may be fine. |
| **Unreachable**, reason "certificate is no longer the one remembered" | The far side's TLS certificate was renewed; the pin from pairing is stale | Queries fail the same way. Re-pair, or refresh the pin — waiting does not help. |
| **Unreachable**, reason "rejects the signed check" | Pairing revoked on the far side, clocks more than a minute apart, or its ping budget is spent | Check the far side's peers page and both clocks. |
| Peers page: **"instance name not yet known"** | The far side has not sent a name yet (pairing older than the name, no signed exchange since) | Press "check now"; the label follows as soon as a check comes back reachable. |
| A peer is shown under a host name | The far side sets no `FEDERATION_INSTANCE_NAME` and sends the host of its advertised URL as its name — or it is an audit row from before the host fallback was removed | Set `FEDERATION_INSTANCE_NAME` there; the label follows with the next query or ping. Old audit rows keep what they recorded. |

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
