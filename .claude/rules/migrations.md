---
paths:
  - "src/backend/alembic/**"
---
# Alembic migrations

Loaded only when a migration file is read. Deploy order and the migration Job: `.claude/rules/deploy.md`.

## Never
- **Never edit a committed migration.** A new change is a new revision file.
- **Never trust file names or visual inspection for `down_revision`.** `versions/` holds 100+ files with overlapping
  naming schemes. Query the LIVE database and use that string verbatim:
  `kubectl -n <namespace> exec deploy/backend -c backend -- alembic heads` (must be ONE revision; several = the chain is
  already forked — stop and fix that first) and `alembic current`.
- Never run `alembic upgrade head` on an EMPTY database: a fresh DB self-bootstraps (`init_db()` runs
  `Base.metadata.create_all` + stamps HEAD). The chain from empty fails on `room_output_devices` (FK to `rooms`, which
  only `create_all` creates) — the ROOT revision `9a0d8ccea5b0` is an empty `pass` stub, so the chain has never been a
  complete description of the schema.
- 🛑 **The consequence of that stamp: raw-SQL DDL in a migration NEVER reaches a fresh install.** `create_all` builds
  tables and ORM-declared indexes; anything a migration adds via `op.execute("CREATE INDEX …")` is skipped, and the
  stamp then claims it was applied, so nothing ever backfills it. Measured 2026-09-24, 37 data points without one
  exception: every raw-SQL index from a migration dated up to 2026-04-02 is ABSENT on both instances, every one from
  2026-04-25 on is present; the household DB was created 2026-04-18. Nine indexes were missing, five of them HNSW —
  every semantic search ran as a seq scan (measured: 46 ms vs 0.57 ms on 5 000 rows). Repaired by
  `pc20260924_restore_idx`; the CAUSE still stands, so a new raw-SQL index today is lost again on the next fresh
  install. Declare it in the ORM as well, or accept that it is repair-migration material.
- 🛑 **An HNSW index on `(embedding::halfvec(N)) halfvec_cosine_ops` is an EXPRESSION index — the query must match
  it SYNTACTICALLY or the planner ignores it.** `ORDER BY embedding <=> CAST(:e AS vector)` does not match; it needs
  `ORDER BY embedding::halfvec(N) <=> CAST(:e AS halfvec(N))`. Measured 2026-09-24 on 5 000 rows: no cast → Seq Scan
  46 ms, cast → Index Scan 0.57 ms. Creating such an index without fixing the queries ships write cost and zero read
  benefit — #1336 nearly shipped four of them. Only the **ORDER BY** decides index usage; leave the `1 - (…)` select
  expression on full `vector` (more precise, free at LIMIT scale). A weighted ranking (`(1-dist) * importance`) and a
  `(1 - dist) DESC` ordering are NOT ANN-indexable at all — flip the latter to `dist ASC`, and leave the former with a
  comment rather than a cast that fakes index usage. `tests/backend/test_vector_query_index_usage.py` guards the class.
- 🛑 **…and the cast is only free when the planner ACTUALLY picks the index — which depends on TABLE SIZE.** Measured
  on live household data 2026-09-25: `kg_entities` (4 813 rows) 4.6 ms with the cast (hnsw) vs 34 ms without — a 7×
  win; `document_chunks` (2 122 rows) **101 ms with the cast (seq scan!) vs 16 ms without** — a 6× LOSS, because the
  halfvec conversion runs per row while the index is never chosen. `ANALYZE` does not change it, and forcing the
  index was slower still (159 ms). A synthetic benchmark lies here: 5 000 RANDOM vectors showed 0.57 ms vs 46 ms,
  because random vectors are far apart while real embeddings cluster and HNSW must explore far more. **Measure on
  real data, per table, before adding the cast.** Tables below the threshold deliberately do NOT cast
  (`SCALE_EXEMPT_FILES` in the guard test), and the daily `vector_index_threshold` task ASKS THE PLANNER — via
  `EXPLAIN (FORMAT JSON)`, no execution — whether it would take the index if the query cast, and warns only then.
  🛑 Not a row count: that does not predict it. xidra picks the index on `kg_entities` at 1 953 rows but not on
  `document_chunks` at 3 165; heap pages and index shape decide (416 pages for 2 847 rows vs 150 for 4 813, estimated
  HNSW startup 8x apart). An exemption without a trigger goes silently stale: nothing breaks, it just gets slow —
  and a trigger that measures the wrong quantity is a second trap, not a safeguard.
- **`atttypmod` for a `vector(N)` column IS N** — there is no `+4` varlena offset. `pc20260402:60` computes
  `atttypmod - 4` and would have built a `halfvec(2556)` index that queries casting to `halfvec(2560)` can never use:
  built, maintained on every write, dead. `y8z9a0b1c2d3` reads it raw and is correct.

## Transaction model
`alembic/env.py` runs with `transaction_per_migration=True` (online and offline). Each migration commits on its own: a
mid-chain failure leaves the preceding ones applied and `alembic_version` advanced to the last success. That is what
allows `op.get_context().autocommit_block()` for non-transactional DDL such as `CREATE INDEX CONCURRENTLY`.
Design every migration as EITHER fully transactional OR fully recoverable — e.g. `DROP INDEX IF EXISTS` before a
`CONCURRENTLY` create (see `pc20260528`). A `CONCURRENTLY` build waits on any leaked idle-in-transaction session.

## Backfill ordering
A migration that backfills rows AND changes indexes does it in this order: (1) drop the indexes that are going away,
(2) run the backfill `UPDATE`s, (3) create the new indexes and constraints last. Every `UPDATE` otherwise maintains
indexes that are dropped a moment later — harmless on a small table, 10–100× slower on a large one
(`pc20260527_skill_approval_status` did it the wrong way round; leave it, it is committed).

## Additive columns
Prefer nullable, no backfill, no server default unless an older pod must be able to write valid rows during a rolling
deploy (then give a server default). The ORM selects every column, so **a new pod before the migration fails every
query on that table with `UndefinedColumn`** — the migration must run BEFORE the rollout; an old pod after the
migration is safe.

## Prove it on real Postgres
SQLite and `create_all` prove nothing about a migration. Test the real upgrade on the dedicated `renfield_test`
database on the build box, never on a live DB: create the pre-migration state, `alembic stamp <previous>`,
`upgrade head`, check the column and an existing row, `downgrade -1`, `upgrade head` again. `create_all` does not build
GENERATED `search_vector` columns — add them explicitly when a test needs them. pgvector caps an indexed vector at
2000 dimensions: use `::halfvec(2560)`.

## The REVERSE path is exercised, never asserted
Applies to `downgrade()`, to a backfill's `--revert`, to any flag rollback — the forward path gets the care and
the reverse gets bolted on unrun. Two of them bit on one day:
- `pc20260922` deleted the atoms BEFORE dropping the columns that reference them; those columns carry
  `ON DELETE CASCADE`, so the downgrade took **every conversation and meeting** with it. The round trip found it;
  reading the code had not. **With FKs the reverse order is not the mirror of the forward order** — release the
  references first, delete the referenced second.
- `bin/backfill_household_tiers.py --revert` inferred what to undo from the CURRENT state. "Admin-owned node at
  tier 2" is exactly the shape of the hand-set tiers the forward run protects, so the revert would have destroyed
  what the forward run carefully spared.

**"Before" is not reconstructible from "after."** So: run the round trip (forward → reverse → forward) with
row counts on real Postgres; where the prior state cannot be derived, have the forward run WRITE A LOG and feed
the reverse only from it, refusing without one; and where even that is impossible (an ownerless row that now has
an owner — nothing records that it had none), say so in the output instead of faking a reverse path.
