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
  only `create_all` creates).

## Transaction model
`alembic/env.py` runs with `transaction_per_migration=True` (online and offline). Each migration commits on its own: a
mid-chain failure leaves the preceding ones applied and `alembic_version` advanced to the last success. That is what
allows `op.get_context().autocommit_block()` for non-transactional DDL such as `CREATE INDEX CONCURRENTLY`.
Design every migration as EITHER fully transactional OR fully recoverable — e.g. `DROP INDEX IF EXISTS` before a
`CONCURRENTLY` create (see `pc20260528`). A `CONCURRENTLY` build waits on any leaked idle-in-transaction session.

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
