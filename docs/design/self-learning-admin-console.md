# Self-Learning Admin Console — Implementation Plan

**Status:** scoped, not started
**Target release:** v2.10
**Source:** `/plan-eng-review` session 2026-05-26 (post-v2.9.1 deploy)
**Outside voice:** Claude subagent (Codex not installed) — 5 tensions surfaced, 2 incorporated, 3 reaffirmed
**Pickup notes:** All architectural decisions are locked. Pick up by branching `feat/self-learning-admin-console` off main and following the task list at the bottom. The eng review's JSONL task artifact is at `~/.gstack/projects/ebongard-renfield/tasks-eng-review-20260526-002004.jsonl` (17 tasks, 15 P1 + 2 P2).

---

## What's shipping in v2.10

A single bundled PR delivering all four admin surfaces for the self-learning feedback loop that shipped in v2.9.1:

1. **Skills Inbox** — owner approves auto-extracted procedural skills before they're injected into agent prompts
2. **Tool-Health dashboard** — per-user/per-tool success/failure stats
3. **Trajectories inspector** — agent turn capture with JSONL export
4. **Curator runbook** — manual curator trigger + run-history audit

Estimated size: ~35 files / ~5000 LoC including tests + 800 LoC Playwright E2E suite.

## Why a single bundle (vs scope-reduction)

The eng review flagged the 8-file complexity smell and proposed two slim alternatives (single-page approval queue, or extending BrainReviewPage with `procedural_skill` AtomType). The outside voice also argued for slimming pending burn-in data. User explicitly reaffirmed full bundle twice — once at Step 0 and once after outside voice. Treat that as a settled decision.

## Locked architectural decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | Single `status` enum (`draft`/`approved`/`rejected`/`archived`) replacing existing `is_active` boolean | Avoids parallel state flags antipattern. Migration drops `is_active`, adds `status`. |
| 2 | NavBadge with pending draft count in sidebar | Discovery mechanism so drafts don't accumulate unseen. Polls a count endpoint with react-query staleTime ≥60s. |
| 3 | Curator + auto-demote filter `status='approved'` | Drafts excluded from cosine self-join in `find_duplicate_pairs`, `find_stale_skills`, and the auto-demote pass in `_post_turn_skill_bookkeeping`. |
| 4 | Both `/admin/skills` (admin household view) AND `/brain/skills` (owner self-view) | Admin sees all users' drafts when caller has admin permission; owner sees own. |
| 5 | Granular backfill rules in `pc20260527` upgrade | seed/manual → approved · auto_extracted + (updated_at > created_at + 60s) → approved · auto_extracted + pinned → approved · auto_extracted untouched → draft · is_active=false → archived |
| 6 | Add `skill_curator_runs` table in same migration | Curator runbook page needs structured audit data. v2.9.1's curator only logs to stdout. |
| 7 | Single migration drops is_active in same step it adds status | Accept ~30-90s outage during rolling backend restart. Renfield has 1 replica + no SLA. |
| 8 | Extract `AdminListPageShell` component | DRY shell across 4 admin pages. ~120 LoC shell + ~80 LoC/page vs ~200 LoC × 4 duplicated. |
| 9 | Include Playwright E2E suite in this PR | Build Playwright infra + 3 specs (approval loop, admin multi-user, curator merge). ~800 LoC. |
| 10 | Approval is owner-private (NOT cascade-vote for circle peers) | Simpler mental model. Cascade-vote filed as P3 TODO, gated on ≥10 household-tier auto-extracted skills observed. |
| 11 | Add `would_have_injected` shadow log | Outside voice finding. Measures the recall hit from the draft-pool gate so we can validate the precision/recall tradeoff after 2 weeks. Removal is filed as P3 TODO. |

## State machine

```
                  ┌─────────────┐
       create     │             │   owner/admin: approve
   ──────────────▶│    draft    │──────────────┐
   (auto_extract) │             │              │
                  └──────┬──────┘              ▼
                         │             ┌───────────────┐
                         │             │   approved    │◀──── seed boot,
                         │             │ (visible to   │      manual POST,
                         │  owner:     │  retrieval)   │      migration
                         │  reject     └──┬────────┬───┘      backfill
                         │                │        │
                         ▼                │        │ curator merge
                  ┌─────────────┐         │        │ OR auto-demote
                  │  rejected   │         │        │ OR owner delete
                  │ (terminal,  │◀────────┘        │
                  │  no retriev)│  (rare: owner    ▼
                  └─────────────┘   rejects an     ┌─────────────┐
                                    already-       │  archived   │
                                    approved skill)│ (no retriev,│
                                                   │  no curator)│
                                                   └─────────────┘
```

## Backend changes

### Migration `pc20260527_skill_approval_status.py`

- Add `procedural_skills.status` enum column (`draft`/`approved`/`rejected`/`archived`), default `'draft'`
- Index on `status` for cheap NavBadge count queries
- Backfill UPDATEs (6 rules per decision #5 above)
- Drop `procedural_skills.is_active` column (accept rollout outage per decision #7)
- Create `skill_curator_runs` table: `run_id` (UUID PK), `started_at`, `finished_at`, `user_id` (nullable, NULL = global), `duplicates_found`, `merges_performed`, `stale_archived`, `error` (text)
- Index on `started_at DESC` for "latest run" lookup
- Downgrade: reverse all (status → is_active mapping; drop curator_runs)

### Models (`src/backend/models/database.py`)

- Add `SKILL_STATUS_DRAFT/APPROVED/REJECTED/ARCHIVED` constants
- Remove `is_active` field on `ProceduralSkill`
- Add `status` field
- New `SkillCuratorRun` model

### Services

- `services/skill_service.py`:
  - `find_similar()`: add `status='approved'` filter (REGRESSION)
  - `create_auto_extracted()`: set `status='draft'` (REGRESSION)
  - NEW `approve(skill_id, caller_id)`: owner OR admin, transition `draft → approved`, 409 if non-draft, audit log
  - NEW `reject(skill_id, caller_id, reason)`: owner OR admin, transition `draft → rejected`, audit log
  - All existing methods using `is_active` updated to use `status`
- `services/skill_curator_service.py`:
  - `find_duplicate_pairs()`: add `status='approved'` filter (REGRESSION)
  - `find_stale_skills()`: add `status='approved'` filter (REGRESSION)
  - NEW `_write_curator_run()`: writes one `skill_curator_runs` row per invocation with counters
- `services/agent_service.py::_post_turn_skill_bookkeeping`:
  - auto-demote query: add `status='approved'` filter (REGRESSION)
- NEW `would_have_injected` shadow query inside `find_similar()`: dual-query during rollout, logs delta as metric (per decision #11)

### Routes

- `api/routes/skills.py`:
  - NEW `POST /api/skills/{id}/approve` (owner OR admin, rate-limited)
  - NEW `POST /api/skills/{id}/reject` (owner OR admin, rate-limited)
  - NEW `GET /api/skills?admin_view=true` (admin-only override of the owner filter)
  - NEW `GET /api/skills/draft-count` (cheap count for NavBadge, owner-scoped or admin-aggregate)
- NEW `api/routes/curator.py`:
  - `GET /api/curator/runs` (admin-only, paginated)

## Frontend changes

### Shared

- `components/admin/AdminListPageShell.tsx` — generic wrapper (header / filter chips / list / detail drawer / pagination state)
- `components/NavBadge.tsx` — pending draft count badge in sidebar
- `components/skills/SkillCard.tsx` — render variants per status (draft/approved/rejected/archived), action enablement matrix
- `components/skills/SkillEditModal.tsx` — body_md textarea (8000 char cap), trigger chips, tool_sequence read-only
- `components/skills/SkillStatusBadge.tsx` — new status badge using DESIGN.md tokens
- `components/trajectories/StepTimeline.tsx` — vertical step list with tool/result/error rendering

### Pages

- `pages/admin/AdminSkillsPage.tsx` — admin household-view of all users' drafts
- `pages/brain/BrainSkillsPage.tsx` — owner self-view of own drafts
- `pages/admin/AdminToolHealthPage.tsx` — table + sort by failure rate + warn-threshold band (charts deferred to P2 TODO)
- `pages/admin/AdminTrajectoriesPage.tsx` — list + StepTimeline drawer + JSONL export
- `pages/admin/AdminCuratorPage.tsx` — run-history table + "Run Now" button

### Data layer

- `api/resources/skills.ts` — list/detail/patch/delete/pin/approve/reject/tier hooks
- `api/resources/toolHealth.ts` — list + warnings preview
- `api/resources/trajectories.ts` — list, detail, flag-gold, JSONL stream-download, stats
- `api/resources/curator.ts` — run + latest-report

### Routing + nav

- `App.tsx` — 5 new routes (`/admin/skills`, `/admin/tool-health`, `/admin/trajectories`, `/admin/curator`, `/brain/skills`)
- `components/Sidebar.tsx` — new "Selbstlernen" collapsible subsection under Admin (4 links) + NavBadge on /admin/skills
- `i18n/locales/{de,en}.json` — strings under `selfLearning.*` namespace

## Tests

### Backend (mandatory regressions per IRON RULE)

- `tests/backend/test_skill_approval_flow.py` — NEW
  - State machine: draft → approved (owner)
  - State machine: draft → approved (admin, owner mismatch)
  - State machine: draft → rejected
  - 403 on non-owner non-admin
  - 409 on archived/rejected approve attempts
  - 6 REGRESSIONS: find_similar gating, create_auto_extracted default, curator dedup filter, curator staleness filter, auto-demote filter, migration backfill
- `tests/backend/test_skills_routes.py` — EXTEND
  - POST /approve endpoint contract
  - POST /reject endpoint contract
  - admin_view=true filter behavior
- `tests/backend/test_skill_curator_service.py` — EXTEND
  - find_duplicate_pairs filters status='approved'
  - find_stale_skills filters status='approved'
  - _write_curator_run produces row per invocation
- `tests/backend/test_alembic_pc20260527.py` — NEW
  - Each of the 6 backfill rules tested against seeded rows
  - Forward + reverse idempotency

### Frontend

- `tests/frontend/react/components/AdminListPageShell.test.tsx`
- `tests/frontend/react/components/SkillCard.test.tsx`
- `tests/frontend/react/components/SkillEditModal.test.tsx`
- `tests/frontend/react/components/SkillStatusBadge.test.tsx`
- `tests/frontend/react/components/NavBadge.test.tsx`
- `tests/frontend/react/pages/AdminSkillsPage.test.tsx`
- `tests/frontend/react/pages/BrainSkillsPage.test.tsx`
- `tests/frontend/react/pages/AdminToolHealthPage.test.tsx`
- `tests/frontend/react/pages/AdminTrajectoriesPage.test.tsx`
- `tests/frontend/react/pages/AdminCuratorPage.test.tsx`

### E2E (new Playwright suite)

- `tests/e2e/playwright.config.ts` — setup, auth fixtures, base URL config
- `tests/e2e/specs/approval-loop.spec.ts` — complex agent turn → draft → owner approves → next matching turn injects
- `tests/e2e/specs/admin-multi-user.spec.ts` — admin sees other users' drafts (auth_enabled=true mode)
- `tests/e2e/specs/curator-merge.spec.ts` — two near-duplicate approved skills → curator merge → both archived/winner

Open question: the v2.9.1 post-deploy smoke hit a Chromium DNS quirk that `/etc/hosts` didn't fix. The E2E suite needs a working Playwright `--host-resolver-rules` config — likely `--config <path>` with `browser.launchOptions.args = ["--host-resolver-rules=MAP renfield.local 192.168.1.230"]`. My first attempt at the schema didn't take effect (the user reconnected MCP but DNS errors persisted). Investigation needed: confirm actual schema from Playwright MCP source before relying on it. Tracked as P2 TODO.

## Implementation lanes (worktree parallelization)

```
Lane A (sequential):  T1 migration → T2 backfill → T4 curator_runs table
                                                 → T3 curator filter → T5 routes → T6 shadow log
                                                 → T13 backend tests
Lane B (sequential):  T7 AdminListPageShell + AdminSkillsPage → T8 BrainSkillsPage
                                                                  → T9 ToolHealth → T10 Trajectories
                                                                  → T11 Curator → T12 NavBadge
                                                                  → T14 frontend tests
Lane C (sequential):  T15 E2E setup → 3 specs (depends on full stack deployed)

Launch A and B in parallel after T1 lands. C waits for both.
```

Lane B depends on Lane A's routes being deployable to a dev cluster (or running locally via docker compose).

## NOT in scope (explicitly deferred, filed in TODOS.md)

| Item | Tier | Trigger |
|---|---|---|
| Bulk approve / multi-select in Skills Inbox | P2 | ≥2 weeks burn-in evidence the queue grows fast enough |
| Tool-Health trend charts | P2 | ≥30 days of `tool_outcome_stats` data |
| Trajectory v1/v2 diff view against `memory_v2_shadow_log` | P2 | Phase B flip (`memory_extraction_v2_authoritative=True`) |
| Playwright `--host-resolver-rules` config for CI | P2 | When CI Playwright runner is set up — local dev uses /etc/hosts |
| Remove `would_have_injected` shadow log | P3 | ≥14 days post-v2.10 + owner verdict on precision/recall |
| Household cascade-vote approval | P3 | ≥10 household-tier auto-extracted skills observed |

## Failure modes flagged

1. **Rolling restart outage** (~30-90s during alembic migration → backend rollout) — accepted, no SLA. Monitor for recovery once new pod is Ready.
2. **Backfill on production data** — currently 0 rows in prod, but the migration is irreversible if it goes wrong. Recommended: run the migration on `.159` against a prod-cloned DB first.
3. **`would_have_injected` shadow log unbounded growth** — needs retention policy (probably 30 days). Add a job to prune. The P3 TODO captures the removal trigger.

## Deploy notes

Standard `/deploy-production` flow applies — see `.claude/skills/deploy-production/SKILL.md`. Migration `pc20260527` runs in the alembic-upgrade Job BEFORE the rolling restart per the skill's instruction.

Image bump: backend only (no frontend/voice-server changes). v2.10.0 tag.

Feature flags already set in `renfield-env` ConfigMap from v2.9.1 (SKILLS_ENABLED, TRAJECTORY_CAPTURE_ENABLED, TOOL_HEALTH_TRACKING_ENABLED, SKILL_CURATOR_ENABLED). No new env vars needed unless the shadow log gets a config (e.g., `SHADOW_LOG_RETENTION_DAYS`).

## Pickup checklist

When resuming:

1. `git checkout main && git pull`
2. Verify v2.9.1 is still the latest deployed release (`kubectl --context renfield-private -n renfield get deploy backend -o jsonpath='{.spec.template.spec.containers[0].image}'`)
3. Check `agent_trajectories`, `tool_outcome_stats`, `procedural_skills` for any real burn-in data accumulated since v2.9.1 deployed — this informs whether the outside voice's "you don't have data yet" concern still applies
4. Branch: `git checkout -b feat/self-learning-admin-console`
5. Start with Lane A T1 (migration). Read `~/.gstack/projects/ebongard-renfield/tasks-eng-review-20260526-002004.jsonl` for the full per-task scope.
6. Backend tests run on `.159` per `memory/reference_test_runner_159.md`. CI is intentionally non-functional.
7. Once T13 + T14 backend+frontend tests are green, set up Lane C (Playwright) — investigate the host-resolver-rules config schema before committing time to specs.
