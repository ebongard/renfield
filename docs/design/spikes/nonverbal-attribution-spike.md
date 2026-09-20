# T-ATTRIB-SPIKE — Gesture actuation attribution: BLE-single-occupant vs. face-ID

Status: **SPIKE COMPLETE (2026-06-27)**. Resolves Decision **D4** / Tension **T1** in
[`docs/design/non-verbal-communication.md`](../non-verbal-communication.md). Read-only
investigation against the live household DB; **no data was written**.

## The question

A command gesture is **silent** — it carries no speaker-ID, unlike a voice turn. But
gesture actuation (Head A) must know **who** gestured, because actuation reuses the
`device_action` fail-closed gate (D6): an unidentified turn is denied actuation when
auth is on. Two ways to identify a silent gesturer:

- **(A) BLE-single-occupant** — actuate only when exactly **one** BLE-identified person
  is in the room. Reuses the existing presence infra (`PresenceService`,
  `presence_events`). Multi-person rooms are **read-only**. Zero new subsystem.
- **(B) Face-ID subsystem** — recognize the gesturer's face from the camera frame.
  A real new subsystem (enrollment, encrypted embeddings, GPU model, consent surface).

**This spike must show whether (A)'s coverage is high enough to ship Head A on it
alone, deferring (B).**

---

## 1. Single-occupant coverage — real numbers

Source: `presence_events` table, live DB (namespace `renfield`, context
`renfield-private`, `postgres-0` pod). Window: **2026-06-10 → 2026-06-27** (17 days),
**2070 events** (1061 enter / 1008 leave / 1 voice-enter), all `source='ble'`.

### The decisive finding: the household has exactly ONE BLE-trackable person

| user_id | username | name | BLE devices (enabled) | IRKs (enabled) | presence_events |
|---|---|---|---|---|---|
| 1 | admin | — | 0 | 0 | 0 |
| 2 | user_a | Person A | **1** | **1** | **2070** |
| 3 | user_b | Person B | 0 | 0 | 0 |

Three users exist; **only `user_a` has any registered BLE device or IRK.** The other two
are **structurally invisible** to presence — no MAC, no IRK, so a satellite scan can
never resolve them to an identity. 100% of presence history (every one of the 2070
events) belongs to the single tracked user.

### Multi-occupant overlap test → ZERO

Reconstructing occupancy intervals per `(user, room)` (each `enter` paired with that
user's next chronological event via `lead()`), then self-joining for any two **distinct**
users whose intervals overlap in the same room:

```
 room_id | overlapping_pairs
---------+-------------------
(0 rows)
```

**There is not a single moment in 17 days where two identified people co-occupy any
room** — because there is only one identified person to begin with.

### Per-room occupancy (the single tracked user)

| room | enters | reconstructed occupied-hours\* |
|---|---|---|
| Arbeitszimmer | 424 | 199.8 |
| Wohnzimmer | 397 | 82.2 |
| Esszimmer | 18 | 62.2 |
| Kinderbad | 190 | 59.4 |
| Fitnessraum | 33 | 0.5 |

\* dwell = time from each `enter` to that user's next event; trailing intervals are
open-ended so absolute hours are soft (an idle "still in room" period inflates a single
interval — see the 2h-capped column in the source query). The **shape** is what matters,
not the absolute hours.

### What the coverage number actually is

- **For the identified subset (the one tracked user): single-occupant holds 100% of
  occupied time** — trivially, because no second person is ever identified.
- **As a measure of "fraction of real household room-occupancy that is single-occupant":
  the dataset cannot answer it.** The other two residents (and any guests) are BLE-dark.
  A room that is physically shared by Person A + Person B presents to the system as
  *single-occupant Person A* — the presence layer literally cannot see the second person.

**This is the load-bearing caveat for the whole decision (see §2).**

### Reproduce / extend

Run read-only from the `postgres-0` pod (no `psql` in the backend pod):

```bash
kubectl --context renfield-private -n renfield exec -i postgres-0 -- \
  bash -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U renfield -d renfield' <<'SQL'
-- per-user trackability + event volume
SELECT u.id, u.username,
  (SELECT count(*) FROM user_ble_devices d WHERE d.user_id=u.id AND d.is_enabled) AS ble_devices,
  (SELECT count(*) FROM user_ble_irks k   WHERE k.user_id=u.id AND k.is_enabled) AS irks,
  (SELECT count(*) FROM presence_events pe WHERE pe.user_id=u.id) AS events
FROM users u ORDER BY u.id;

-- multi-occupant overlap (expect 0 until >1 person is BLE-enrolled)
WITH ev AS (
  SELECT user_id, room_id, event_type, created_at,
         lead(created_at) OVER (PARTITION BY user_id ORDER BY created_at) AS next_ts
  FROM presence_events),
intervals AS (
  SELECT user_id, room_id, created_at AS s, COALESCE(next_ts, created_at) AS e
  FROM ev WHERE event_type='enter')
SELECT a.room_id, count(*) AS overlapping_pairs
FROM intervals a JOIN intervals b
  ON a.room_id=b.room_id AND a.user_id<b.user_id AND a.s<b.e AND b.s<a.e
GROUP BY a.room_id;
SQL
```

---

## 2. Presence model notes + caveats

Confirmed from `src/backend/ha_glue/services/presence_service.py` and
`presence_analytics.py`:

- **`UserPresence`** (in-memory) maps `user_id → {room_id, room_name, satellite_id,
  confidence, last_seen}`. `get_all_presence()` returns `dict[user_id, UserPresence]`;
  `get_room_occupants(room_id)` filters it. `is_user_alone_in_room(user_id)` already
  computes exactly the single-occupant predicate Option A needs
  (`len(get_room_occupants(room)) == 1`). **The attribution primitive already exists.**
- **Identity comes only from a known MAC or a resolved IRK** (`_user_for_key`): a
  sighting is dropped if its key matches no `UserBleDevice`/`UserBleIrk`. So presence
  sees **only enrolled BLE devices** — a phoneless person, a guest, or a resident who
  hasn't paired is **completely invisible**, not "present-unknown."
- **`presence_events`** persists `enter`/`leave` with `user_id, room_id, source,
  confidence, satellite_id, created_at`. It's an event log (not interval rows); occupancy
  must be reconstructed from enter/next-event pairs (done above).

### Caveats that bound the decision

1. **Single-occupant ≠ alone.** `is_user_alone_in_room` returns True whenever exactly one
   *tracked* user is present. If an **untracked** second person (Person B, a child, a guest)
   is physically in the room, the system still reports single-occupant and **would
   actuate on Option A** — attributing the gesture to the one tracked user. That is the
   exact failure D4 is trying to prevent (actuate-for-the-wrong-person), and BLE cannot
   detect it. **BLE-single-occupant is only as safe as BLE coverage is complete.**
2. **Coverage is currently 1-of-3 residents.** With two residents BLE-dark, "single
   tracked occupant" is a weak proxy for "actually alone" in this specific household
   *today*. This is a **deployment-completeness** gap, not an architecture gap — the IRK
   pairing flow exists; the other residents simply haven't enrolled.
3. **BLE room assignment has latency/hysteresis** (N consecutive scans to switch rooms,
   stale-timeout eviction). A just-entered or just-left person can be mis-roomed for a
   few scans — a small additional window where occupancy count is wrong.

---

## 3. Face-ID alternative — scope & effort in Renfield's architecture

Every building block a face-ID subsystem needs **already exists as a reusable pattern**;
the work is assembling them plus the model and the legal surface.

| Component | Reuse / pattern that already exists | New work |
|---|---|---|
| **Enrollment UI** | `components/presence/IrkPairing.tsx` — pick user + satellite, open a bounded capture window, store result. Mirror it exactly: "enroll my face on satellite X". | A new capture component + a few embeddings rows in the admin UI. ~2–3 d |
| **Encrypted embeddings at rest** | `services/secret_encryption.py` — Fernet from `SECRET_KEY`, the **identical** at-rest pattern already used for IRKs (`encrypt_secret`/`decrypt_secret`). A face embedding is a tracking secret like an IRK. New table `user_face_embeddings(user_id, embedding_encrypted, label, …)` + migration. | ~1–2 d (model + migration + load-into-cache, mirrors `_load_irks`) |
| **Recognition model on GPU** | The CUDA node already hosts voice-server; the gesture pipeline (D3) will already run a temporal model there. A face-embedding model (e.g. an ArcFace/InsightFace-class encoder) slots onto the same node. | Model selection + serving + a recognize() that returns `(user_id, score)` against enrolled embeddings; **shares/contends the GPU** (D3 `T-GPU-CAP` already flags this). ~3–5 d + an accuracy/threshold eval |
| **WS capture flow** | `capture_snapshot` + `irk_capture` request-response (`satellite_manager.request_irk_capture` / `resolve_irk_capture`, `satellite_handler.py` `irk_capture_result`). A face-recognize-on-frame request is the same shape; the gated gesture WS (D6) already moves frames/landmarks. | A `recognize_face` request/result message + wiring into the gesture window. ~2–3 d |
| **Consent / legal surface** | The consent UI is **already on the critical path** for the camera feature (T-CONSENT-UI). Biometric **face templates** are a materially higher legal bar (DE: biometric data is a special category — explicit per-person consent, retention/deletion, the right to be un-enrolled). | A distinct biometric-consent flow + deletion path + documentation. **Days of design + legal review, not just code.** This is the real cost, not the model. |
| **Eval / accuracy gate** | The `kg_extraction_eval` harness pattern (D8 already mandates an accuracy gate for the gesture heads). | A labeled recognize eval (FAR/FRR by household member + lighting/angle). ~2–3 d |

**Rough total effort: ~2–3 engineering weeks of build** (UI + table/migration + GPU
model + WS + eval), **plus a separate, serial biometric-consent + retention design/legal
track** that gates rollout independently of code-complete. It also **reintroduces a
biometric template at rest** — a surveillance surface that the Tier-1 "landmark-only,
nothing persisted" privacy story (Decisions 4 & 7) was specifically built to avoid. Face
embeddings are persistent identity, the opposite of the ephemeral, nothing-persisted
posture of the rest of the feature.

---

## 4. Recommendation

**Ship Head A on BLE-single-occupant (Option A). Defer the face-ID subsystem (Option B).
Do NOT build face-ID up front.**

Rationale, grounded in the data:

1. **Multi-person attribution is not a problem the data shows happening.** In 17 days of
   real history there are **zero** multi-identified-occupant intervals in any room. The
   "two people, whose gesture?" case has **no observed occurrence** — building a whole
   biometric subsystem up front to solve it would be solving a problem the household
   doesn't currently exhibit.
2. **The attribution primitive already exists** (`is_user_alone_in_room`) and the
   fail-closed posture the design already mandates is correct: **single identified
   occupant → attribute + actuate; otherwise read-only.** That is a few lines wiring an
   existing method into the D6 gate — not a subsystem.
3. **Face-ID is ~2–3 weeks of build plus a serial biometric-consent/legal track, and it
   reintroduces persistent biometric data** that contradicts the feature's
   nothing-persisted privacy spine. That cost is not justified by a zero-occurrence
   risk.

**BUT this recommendation carries one hard, non-negotiable condition**, because the
coverage number is a *deployment* artifact, not a safety guarantee:

- **The single-occupant gate is only safe if BLE coverage of the household is complete.**
  Today **2 of 3 residents are BLE-dark**, so "one tracked occupant" can mask an
  untracked second person and actuate for the wrong human — the precise D4 failure.
  **Before Head A actuation is enabled in any room, every resident who can be present
  in that room must be BLE/IRK-enrolled** (the IrkPairing flow already exists; this is
  an onboarding task, not engineering). Treat resident enrollment as a **rollout gate**
  for Head A, exactly like consent.
- **Keep fail-closed for guests by design.** A guest will never be BLE-enrolled, so a
  resident + guest looks like single-occupant. Mitigations that do **not** require
  face-ID: (a) confine the gesture **safe-action allowlist** (D7) to reversible,
  low-stakes actions so a wrong-attribution actuation is harmless; (b) optionally tie
  actuation to the gaze/attention signal Head B already plans (gesturer is looking at the
  device) rather than raw occupancy. These are cheaper than biometrics and stay within
  the existing design.
- **Leave face-ID as a documented, scoped fallback (it already is, in "NOT in scope").**
  If the household later becomes routinely multi-occupant *and* the safe-action
  allowlist + enrollment-gate prove insufficient, the §3 scoping shows it's a ~2–3 week
  build on existing patterns — buildable then, on evidence, not speculatively now.

**Bottom line: Head A is UNBLOCKED to proceed on BLE-single-occupant**, with
multi-person/unidentified rooms read-only (as the design already states), **gated on
full-household BLE enrollment per room** and a reversible-only safe-action allowlist.
Face-ID stays deferred.
