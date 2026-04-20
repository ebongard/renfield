# Federation — multi-Renfield topologies

Status: **open / revisit**. Captured during F4c design so we can come back to it.

## Why this note exists

The v2 federation work (lanes F1–F4) was scoped around a **single asker ↔ single responder** handshake. Once pairing is stable and chat relay is wired, two topologies become realistic that we have NOT fully designed for:

1. **Fan-out** — one user's agent loop queries *N* paired peers in the same request.
2. **Transitive** — a responder's own agent loop, while answering the asker, calls out to a third Renfield instance. The chain can in principle be N-deep.

Both are cases where "asking Mom's brain…" stops being the full picture.

## What works today (F4b as shipped)

| Scenario | Behavior |
|---|---|
| 1 asker → 1 peer | Full streaming. `ProgressChunk` vocabulary `{waking_up, retrieving, synthesizing, complete, failed}` is locked. UI renders one status line. |
| 1 asker → N peers (fan-out) | **Works at backend** — agent loop invokes the federation MCP tool once per peer; `ProgressChunk` each carry `detail.peer = remote_display_name`. F4c frontend therefore keys status lines by `peer.remote_pubkey`, not by step. |
| A → B → C (transitive) | **Works by design, but opaque.** B's internal sub-query to C is absorbed into B's own `retrieving` / `synthesizing` labels from A's point of view. This is intentional — the locked vocabulary prevents side-channel leaks about B's peer graph. |

So there is nothing broken today. Everything below is **what we have not decided**.

## Open questions — fan-out

1. **Parallelism.** The agent loop serializes tool calls via `executor.execute`, but a parallel path exists (`_exec_parallel` + `asyncio.gather`) that fans out to multiple peers at once. "Ask all my trusted peers" is today either N sequential tool calls with additive latency OR N concurrent ones depending on how the agent's plan step emits them. Do we want a first-class fan-out primitive (one tool call that dispatches to a set of peers and merges results)?
2. **Concurrent sink writes to the chat WebSocket.** F4c serializes `websocket.send_json` calls with an `asyncio.Lock` inside the chat handler's sink — Starlette's WebSocket does NOT guarantee frame-level serialization on concurrent sends, so two in-flight sinks would otherwise interleave bytes and corrupt the stream. The lock is per-request (per connection), so fan-out is safe today. Open: if we ever allow multi-tab / multi-device concurrent streams, the lock needs to widen, and at that point we should evaluate a single-writer task with a bounded queue instead.
3. **Sink backpressure.** `await progress_sink(...)` blocks `FederationQueryAsker._run` if the chat WS is slow. In fan-out, one slow client stalls all peers' relay (they serialize on the lock). This is tolerable because the locked vocabulary rate-limits sink volume — label transitions are rare (≤4–5 per request per peer), not per-poll. The rate-limit-at-source is what makes sink backpressure harmless; note this explicitly as a design invariant before we add any per-poll progress detail.
4. **Sink lifetime under agent cancellation.** If the user sends a second query (or the WS drops) while a federation call is mid-flight, the agent loop receives `CancelledError`. The sink may then be called on a half-torn-down WebSocket, raising `RuntimeError: Cannot call "send" once a close message has been sent`. The swallowed-error path handles it, but in fan-out (N peers mid-flight), cancellation can produce N log lines. Not broken; worth a future cleanup to dedupe via a single "connection closed" flag.
5. **UI density.** Five peers × five labels = a lot of live status lines. When does the UI start collapsing them (e.g., `3 peers retrieving, 1 synthesizing`)? At what count?
6. **Timeout and partial-result semantics.** If 4/5 peers answer in 3s and the 5th is at 30s timeout, does the agent wait or synthesize with what it has? Today: single-peer 60s timeout hard-coded in `FederationQueryAsker._retrieve()`. Fan-out needs a budget policy.
7. **Result weighting.** If peers disagree, whose answer wins? Tier? Recency? User's own confidence? Out of scope for F4c, on the roadmap for later.

## Open questions — transitive

1. **Depth limit.** Nothing stops A→B→C→D→…. A runaway chain could exhaust timeouts or burn battery on a Pi. Need a `max_federation_depth` field in the signed query envelope, decremented at each hop. Default 1? 2?
2. **Cycle detection.** A→B→A is possible if both are paired. The envelope should carry a `path` set of pubkeys already visited; reject if own pubkey is in it.
3. **Audit trail.** From A's POV, all we see is B's progress. The user who ran the query has no way to know that C was consulted. Privacy-preserving and A-trust-boundary-correct, but also: compliance-opaque. If we ever want "show me which brains answered this," we need a post-hoc metadata path (B could voluntarily report "answered with help from 1 other peer" without naming C). Note that asker-side audit (planned for F4d — `/brain/audit`) and responder-side audit (B logs that it answered A) are TWO different features with different privacy boundaries; don't conflate them. Asker audit is "who did I ask"; responder audit is "who asked me, and did I fan out further". Transitive-propagation policy goes on top.
4. **Rate limits stack.** B's responder rate limit applies once. If C also rate-limits B, the combined error surface is confusing. Needs a "reason" chain in failure responses.
5. **Cost/trust asymmetry.** A pays B for the query (in latency and trust). B pays C. If A is a tier-4 stranger to B but B is a tier-2 to C, the implicit trust graph is weirder than it looks. Worth a design pass before we ship transitive at scale.

## What to do when we revisit

- Before F5 hardening: decide on **depth limit + cycle detection** at minimum. Those are correctness issues, not feature issues.
- Fan-out UX: prototype with 2–3 peers first, measure what the UI actually looks like, then decide on collapsing rules.
- Weighting / synthesis across peers: defer until we have real data from 2+ deployed households.

## Relevant code references

- `src/backend/services/federation_query_asker.py` — streaming protocol, progress label remapping.
- `src/backend/services/mcp_streaming.py` — `ProgressChunk` + `FEDERATION_PROGRESS_LABELS` locked vocabulary.
- `src/backend/services/mcp_client.py` → `_execute_federation_streaming()` — where fan-out or transitive would plug in.
- `src/backend/models/database.py` → `PeerUser` — identity is `remote_pubkey` (stable), not display name.
