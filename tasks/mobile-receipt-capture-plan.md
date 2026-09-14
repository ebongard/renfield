<!-- /autoplan restore point: /Users/evdb/.gstack/projects/ebongard-renfield/worktree-agent-a78527ccc1adfd1ac-autoplan-restore-20260914-162627.md -->
# Plan: Mobile receipt capture — native iOS app (revision 4)

Status: PROPOSED — nothing implemented. Design: `docs/design/mobile-receipt-capture.md` (revision 4).
R2: native iOS app, TestFlight, xidra first, APNs push. R3: projects + travel expense reports.
R4: all product decisions made (see design "Decided (2026-09-14)"); per diems + mileage in v1 (dark,
tax-advisor gate), owner settles, ZIP export. R5: travel expense reports are the `expense_reports`
PLUGIN — not loaded/mounted/migrated/shown unless listed in `PLUGIN_MODULES`; its code sits unused in the
shared image on other instances (decided R5-1 = a, like ha_glue). Every phase dark behind
its flag. A phase starts only after the previous one is verified.

## Locked constraints
- A capture enters exactly ONE instance, only after its destination is settled; the app never
  relays one instance's capture through another.
- Outbox deletes bytes only on `ingested|duplicate`.
- No public endpoint. No polling (one bounded background-retry exception, gone in Phase 3).
- Sphere (owner/tier/kb) from the credential row only.
- Push payload: loc-keys + random ids only. Coordinates never stored or transmitted (also not for mileage).
- Details/notes = untrusted user text: never in chunks or extraction prompts.
- Ingest credential: own captures (read status, edit details/assignments), own active projects + open trips
  (read), own open trips (create/end/times/days/legs), device token. Never submit/settle/confirm/export.
- Per-diem/mileage amounts only when `EXPENSE_REPORTS_PER_DIEM_ENABLED` AND the rate set is reviewed (hard gate).

## Prerequisites (start immediately, external lead time)
- [ ] Request D-U-N-S number for xidra; enrol the Apple Developer Program as organization (gates M2 TestFlight)
- [ ] Ask the tax advisor for the review slot + send the [StB] checklist (gates 1.5b go-live only)

## Phase 0 — measurement spike (gate)
Capture quality
- [ ] VisionKit prototype app (throwaway branch in the iOS repo) rendering PDF at 2–3 quality/DPI settings
- [ ] Real receipts incl. faded thermal: `pdfimages -list` + `pdfinfo` per setting; page size from pixels+DPI verified
- [ ] Push each through a xidra-equivalent pipeline on a test KB; record OCR coverage, VLM fallback, Schicht-A output
- [ ] Phantom-obligation baseline on real till receipts (count, examples by id only)
iOS behaviour (minimum iOS 18 provisional)
- [ ] Background URLSession: task on cellular waits for Wi-Fi; foreign Wi-Fi failure behaviour; force-quit cancels; relaunch completion
- [ ] TLS: pin check in background-session auth challenge vs private-CA profile — pick one
- [ ] Keychain `AfterFirstUnlockThisDeviceOnly` readable on background relaunch; not in encrypted backup
- [ ] APNs environment of TestFlight builds; permission prompt timing
- [ ] Location: whenInUse on first toggle; precise vs approximate; MapKit POI suggestion quality; GeoNames offline size/quality
- [ ] Confirm iOS 18 minimum against the APIs used (VisionKit, background session, notifications)
Desk capture (R6)
- [ ] Inventory the cameras the user actually has: iPhone model (Desk View needs iPhone 11+, not 16e/SE), Mac model (built-in Desk View only MacBook Pro 2024+ / MacBook Air 2025+ / iMac 2024+ / Studio Display 2026), any UVC document camera, iPad + overhead stand
- [ ] Prototype capture with `AVCaptureDevice` Desk View device: confirm device type name, macOS version, still-photo vs video-frame-only output, max format
- [ ] Measure A4 letters + receipts (synthetic/consented) per camera: pixels on short edge, OCR coverage through the real pipeline, glare share, low-contrast (white on white) detection; record frame sequences as fixtures
- [ ] Derive stability tolerance/hold time, sharpness/glare/pixel-density thresholds from the recordings
- [ ] TestFlight for Mac with the org account + internal testers; macOS Keychain device-only item + background URLSession behaviour
Network / Apple / xidra
- [ ] iPhone on LAN resolves the xidra name (no mDNS dependency); cert trusted or pinned
- [ ] Ingress path `/api/mobile-capture/*` to xidra backend; netpol ingress OK
- [ ] Relay namespace: egress only to APNs hosts, ingress only from instance backends; xidra backend egress → relay
- [ ] APNs key scoping options checked
- [ ] xidra Paperless custom fields `Anlass/Teilnehmer/Ort/Notiz` exist (admin creates); deferred PATCH sets them on a test doc
- [ ] Review-flow hook path exercised with a `mobile_capture` test document
- [ ] Findings + measured values written back into the design doc

## Phase 1 — MVP on xidra
### M1 — backend core (this repo)
- [ ] `ROUTE_MOBILE` in `services/ingest_credentials.py` + `_ROUTES` in routes; cross-route rejection tests
- [ ] `MOBILE_CAPTURE_SOURCE` constant; test processed-file rename ignores it
- [ ] Config keys (design §Configuration) + `docs/ENVIRONMENT_VARIABLES.md`
- [ ] **R5** `extensions` field in upload + PATCH metadata: JSON object ≤ 8 KB, namespaced keys `^[a-z][a-z0-9_]{0,63}$`, object values; structurally validated, NOT interpreted, NOT stored; dropped + logged (no dispatch until 1.5-0); tests incl. oversize/invalid key → `failed/invalid_extensions`, valid → ignored with log
- [ ] Migration: `mobile_capture_log` (unique `(client_id, capture_id)`, `document_id`, `state`, `notified_state`, `version`), `capture_details`, `mobile_capture_devices`; pending/active state on credential — real `alembic upgrade` on Postgres
- [ ] `services/mobile_capture.py`: metadata parse, PDF magic-byte check, `capture_id_conflict`, details validation (NFC, control chars, limits → `invalid_details`); unit tests incl. injection-looking strings stored verbatim as data
- [ ] `POST /api/mobile-capture/document` (flag 503, 401/403, worker 503, size cap, 4-state + capture_id, details row); `tests/backend/test_mobile_capture.py`
- [ ] `GET /health`, `GET /captures?ids|since` (own client only; other device → not returned); tests
- [ ] **R4** `PATCH /captures/{capture_id}` (details / project_id / trip_ref): own client only (404 otherwise), upload-identical validation, `If-Match` → 412, re-mirror to Paperless; tests incl. foreign capture, stale version, invalid project → `assignment_invalid`
- [ ] Status aggregation over PDF-Split children; tests
- [ ] Pairing: self-service mint behind login + CSRF (owner = caller; admin for others), pending credential + single-use code (GETDEL, TTL), `POST /pair` activates + returns token once, rate-limited; tests: non-admin can't pair for others, replay/expiry fail, pending credential expires
- [ ] Tier test: capture lands at credential tier, invisible to a lower-reach xidra user
- [ ] Review flow: source condition → configurable allowlist (default `folder_ingest`); tests allowed/not allowed
- [ ] Paperless reconciler: custom fields by name in deferred PATCH (existing only); missing fields reported in health + `internal.ingest_status`; re-apply on PATCH; tests
- [ ] `document_search`: capture-details FTS signal through the fused circle gate; test no leak across owners
- [ ] `internal.knowledge_search`: fenced details data block; test it never enters Schicht-A/KG/PDF-Split inputs
- [ ] Frontend: "Mobile Geräte" settings page (pair QR, device list, revoke, live activation via user events); i18n de+en, dark mode; RTL+MSW tests; `npm run build`
- [ ] Backend suite green on .159
### M2 — app v1 without push (private repo `renfield-ios`; needs organization enrolment)
- [ ] Repo + SwiftPM packages `RenfieldCore`, `CaptureFeature`, `OutboxEngine`, `HistoryFeature`; shell with AccountSwitcher + SectionRegistry (credential kinds); deployment target iOS 18
- [ ] Contract fixtures consumed from this repo; contract header + skew handling
- [ ] **R6** Keep `RenfieldCore`/`OutboxEngine` platform-neutral (no UIKit in core); CI builds the core for macOS too
- [ ] CredentialStore (Keychain, per account, kind-typed); XCTest
- [ ] APIClient per account with TLS pin; pinned health probe; typed errors
- [ ] Pairing: QR scan → pin validate → `/pair` → Keychain → account
- [ ] Capture: VisionKit → PDF assembly with Phase 0 parameters; XCTest on fixture images asserting page size/DPI
- [ ] Capture type Beleg/Bewirtung; details form with server-mirrored validation
- [ ] Location toggle: whenInUse on first use, MapKit suggestion with disclosure, offline town-level setting, coordinates never persisted (test)
- [ ] Outbox: files excluded from backup, state machine, background URLSession upload-from-file, re-enqueue on launch/path change/failure with bounded backoff; delete only on ingested|duplicate (tests)
- [ ] History: per account, refresh on open/pull/upload completion; **thumbnails 30 days, device only, excluded from backup** (test purge + backup flag)
- [ ] **R4** Edit screen for a processed capture (details, project, trip) → `PATCH`; conflict (412) and locked (409) handling
- [ ] fastlane lanes `test`, `beta`, `expiry_check`; first TestFlight build; **internal testers only**
### M3 — xidra dry run → go-live (private config)
- [ ] xidra config: DNS name, cert/pin, ingress, netpol, test KB (tier self, filing off, review opt-in off), flag on
- [ ] Device E2E: pair, capture offline, come home, history → processed; edit details after processing → Paperless updated; revoke → 403, capture stays in outbox
- [ ] Browser E2E of pairing + device list on xidra
- [ ] Flip to real KB + Paperless filing + review allowlist `mobile_capture`; one real business-meal receipt end-to-end (user drives)
### M4 — push
- [ ] Relay (private repo): `/send` with template-key enum, per-instance credential, rate limit, APNs token auth, 410 feedback, no persistence; tests
- [ ] Relay deployment: **own namespace**, egress APNs only, ingress from instance backends only
- [ ] Backend: `PUT/DELETE /device`; revoke deletes token; tests
- [ ] Emitters (after-commit, best-effort) at ingest-complete, worker terminal failure, PDF-Split review proposal, review-flow proposal → `XADD renfield:tasks:mobilepush`; tests
- [ ] Consumer group in API pods; conditional `notified_state` update; ack after relay accept; pending-claim with bounded age; tests incl. redelivery → no double push
- [ ] App: permission after first pairing, token registration, loc-key texts de+en, tap → re-fetch `/captures?ids=`
- [ ] Build-age: `X-Client-Build` recorded; Scheduled Task → `ops_alert` at 75 days; in-app banner
- [ ] Docs sweep (CLAUDE.md, FEATURES.md, ENVIRONMENT_VARIABLES.md, new `docs/MOBILE_CAPTURE.md`)

## [R3] Project picker (inside MVP: backend in M1, app in M2)
- [ ] Migration `document_project_links` (document_id, project_id SET NULL, assigned_by, source; unique per document) — real Postgres upgrade
- [ ] `GET /api/mobile-capture/lookups` → active projects visible to credential owner (same owner-only rule as `/api/projects`) + open trips, ids+names only; features absent when flags off; tests incl. other user's project/trip not listed
- [ ] Upload + PATCH `project_id` validation: invalid/inactive/foreign → no link + `needs_review/assignment_invalid`; tests (never rejected)
- [ ] PDF-Split children inherit link; test
- [ ] Project timeline: linked-documents source; test
- [ ] App: picker from cached lookups, refresh at home foreground + after upload; hidden when feature absent

## [R5] Phase 1.5-0 — plugin substrate (core; before 1.5a; human 2–2.5 wk / CC 3–4 d)
- [ ] `services/plugin_host/`: `HOST_CONTRACT_VERSION` (semver), typed payload dataclasses, `HostAPI` protocol (db_session, auth deps incl. ingest client, documents.visible_ids/meta/open_bytes, projects.visible_active, facts.amount_suggestion, events.publish_user_event, ops.alert, settings_namespace); unit tests
- [ ] `utils/hooks.py`: add events `capture_extensions_apply`, `capture_lookups`, `plugin_features`, `capture_state_changed`, `document_deleted`, `paperless_filing_metadata`; `register_hook` rejects non-coroutine handlers (test: sync handler → error at registration, not silent)
- [ ] Plugin loader: pass `HostAPI` to `register(host)` (backward-compatible with zero-arg register); contract major mismatch → load failure; all failed plugins → `ops_alert` + `internal.system_health`; tests
- [ ] `extensions` dispatch after core commit per namespace; no handler → drop + log; reject/raise/timeout → `needs_review/assignment_invalid`, never a failed upload; tests with a fake plugin
- [ ] `GET /lookups` merges `capture_lookups` under `plugins.<ns>`; `/api/config/features` gains `plugins: dict[str, PluginFeature]` from `plugin_features`; mobile health lists plugins; tests (no plugin → empty, byte-identical other fields)
- [ ] Fire `document_deleted` (after commit in `RAGService.delete_document`), `capture_state_changed` (push emitter), `paperless_filing_metadata` (filing leg, merge existing-only fields); tests
- [ ] Alembic: extract bootstrap + configure (transaction_per_migration, bootstrap commit, version_num width) into a shared helper `plugin_host.migrations`; core `env.py` uses it unchanged in behaviour; add `include_object` excluding `plugin_*` tables and `alembic_version_*` tables; test core autogenerate emits no drop for plugin tables
- [ ] Plugin migration template: own `alembic.ini`/`env.py`, `version_table=alembic_version_<plugin>`, `REQUIRES_CORE_REVISION` check against core script dir; `k8s/alembic-upgrade-plugin-job.yaml` (no `namespace:` field, runs after core job); deploy script runs it only when the plugin is in `PLUGIN_MODULES`
- [ ] Spike: cross-MetaData FKs (`plugin_* → documents/users/projects`, `ON DELETE SET NULL`) render in `op.create_table`; else `op.create_foreign_key`/raw DDL; test core migrations on a DB with the plugin schema present
- [ ] Import-isolation tests: no core module imports `plugins.*`; plugin imports only `utils.hooks`, `services.plugin_host.*`, third-party
- [ ] Frontend `plugins/registry.ts` (namespace, nav items, lazy routes, lazy i18n namespace); `App.tsx`/`Layout.tsx` render entries only when `features.plugins[ns]` present + compatible; RTL test: no plugin → no nav/route and no chunk request
- [ ] iOS `SectionRegistry`: plugin sections shown only when instance health lists the plugin with a compatible contract version; XCTest
- [ ] Docs: plugin/hook API doc + `/add-hook` skill references updated (new events, HostAPI, migrations)

## [R3/R4/R5] Phase 1.5a — travel expense reports as the `expense_reports` plugin (after 1.5-0; can go live alone)
- [ ] Package `src/backend/plugins/expense_reports/` (`plugin.py:register(host)`, own settings namespace `EXPENSE_REPORTS_*`, own SQLAlchemy Base, routers under `/api/plugins/expense-reports/…`) + `src/frontend/src/plugins/expense_reports/`; `register` verifies its version table is at head before mounting (else fail-load + alert)
- [ ] Plugin migrations (own environment/version table): `plugin_expense_reports_reports` (+ `times_recorded`, `timezone`, `per_diem_requested`, `version`), `_items` (FK documents SET NULL + snapshot of date/category/amount/file hash), `_days`, `_mileage_legs`, `_computed_lines`, `_events` — real Postgres upgrade via the plugin job; downgrade base tested
- [ ] Deactivation test: remove from `PLUGIN_MODULES` → no routes, no features entry, tables/data intact; reactivation resumes
- [ ] `capture_extensions_apply` handler (trip validation), `capture_lookups` (open trips), `plugin_features` (version + capabilities incl. `per_diem`), `document_deleted` (event + frozen snapshot), `paperless_filing_metadata` (report tag + custom fields); contract tests
- [ ] Service + web REST: create (idempotent `client_ref`), edit header/items/days/legs while open, confirm amount, submit (freeze), withdraw, **settle by owner**, **reopen by admin only with reason**; every transition → `expense_report_events`; status tests incl. locks after submit, non-owner settle refused, owner reopen refused
- [ ] App-scoped REST (ingest credential): own open trips only — create, end, times, days, legs; submitted/settled → 409; tests incl. foreign trip, stolen-token scope (cannot submit/settle/confirm/export)
- [ ] Times validation: `per_diem_requested` → end-trip and submit require times + `end > start` (`422 times_required`); tests
- [ ] Visibility: owner + admin only; items through document circle filter ("not visible" placeholder); test report never exposes a hidden document
- [ ] Upload/PATCH `trip_ref` validation: foreign/non-open → no link + `assignment_invalid`; trip's project as default; tests
- [ ] Schicht-A total as suggestion only; totals over confirmed amounts per currency; tests
- [ ] Export PDF + CSV (header, items, not-included list, computed lines or "not calculated"); golden-file tests
- [ ] **ZIP export**: pre-flight per-document circle check + bytes availability (recovery copy → Paperless fallback); measure realistic report size → set `EXPENSE_REPORTS_EXPORT_ZIP_MAX_MB`; 413 above bound; streaming STORED writer, no temp file, `no-store`; template filenames `belege/<nn>_<date>_<category>.pdf`; rate limit; `exported` event; tests incl. hidden doc listed as not included, filenames contain no merchant/participant/purpose/destination, memory stays flat on a large report
- [ ] Verify retention of the ingest recovery byte copy (ZIP byte source)
- [ ] Paperless mirror: report tag `Reisekosten <yyyy-mm> #<id>` + custom fields (existing only), best-effort; tests
- [ ] Web page "Reisekosten" (flag-gated, DESIGN.md, dark mode, i18n de+en, reuse `ProjectSelect`; event log view); RTL+MSW tests; `npm run build`
- [ ] App: "Reise starten/beenden" with offline `client_ref`, per-diem toggle, required-time prompts, per-day country/meals, mileage legs (no location), default assignment + per-receipt override, trip list in history
- [ ] Plugin not loaded on household/association: no routes, no `plugins.expense_reports` in features/health, no plugin tables, `extensions.expense_reports` dropped + logged; core suite green without the plugin; tests
- [ ] xidra E2E: start trip offline, 3 receipts (one Bewirtung), end trip, confirm amounts on web, submit, settle, export PDF/CSV/ZIP; browser + device E2E

## [R4] Phase 1.5b — per diems + mileage calculation (built with 1.5a, dark until tax-advisor review)
- [ ] Rate-file schema + placeholder example in this repo; loader with schema validation, non-overlapping validity, checksum; invalid → calculation off + health + `ops_alert`; tests
- [ ] Gate: flag AND reviewed rate set covering the dates; checksum change re-closes gate; tests
- [ ] Pure calculation (Decimal): absence per calendar day in report timezone, tier rules, day-country rule, meal reductions with floor, mileage per leg, rounding per rate set; golden tests (single-day thresholds, multi-day, border days, all meal combos, DST change, rate-set boundary in trip, unknown vehicle, unreviewed set)
- [ ] Traceable lines (inputs, rate key/value, set id/version/validity/checksum, fixed formula template); live preview while open, frozen at submit, recompute on admin reopen; tests
- [ ] Export renders computed lines + formulas; golden-file tests
- [ ] Web + app UI: "Berechnung nicht freigegeben" state; preview of lines; localized validation problems
- [ ] Tax-advisor review package: specification section + golden cases + rate set → review recorded in rate file `review` block (xidra private config)
- [ ] Only then: `EXPENSE_REPORTS_PER_DIEM_ENABLED=true` on xidra; E2E with a real trip (user drives)
- [ ] Effort check vs estimate (1.5-0 human 2–2.5 wk / CC 3–4 d; 1.5a human 4–5 wk / CC 7–9 d; 1.5b human 3–4 wk / CC 6–8 d; total R5 human ~9–11 wk / CC ~3–4 wk)

## [R6] Phase 1.6 — desk scan on Mac and iPad (after M4; parallel to Phase 1.5; human ~6–7.5 wk / CC ~2–2.5 wk)
macOS target (human 1.5–2 wk / CC 3–4 d)
- [ ] macOS destination of the SwiftUI project; shell + AccountSwitcher + shared capture UI (Beleg/Bewirtung, project picker, trip picker when plugin reported)
- [ ] Keychain device-only item on macOS; outbox + background URLSession; TLS-pin home detection unchanged
- [ ] Pairing: custom-URL-scheme deep link from the pairing page ("In der Renfield-App öffnen", carries only one-time code + URL + label + pin) + manual code field; backend pairing page renders the link; tests (code single-use, no token in link)
- [ ] APNs registration on macOS per credential
`DeskScanKit` (human 3–4 wk / CC 6–8 d)
- [ ] Camera selection: Desk View → UVC → other; per-device remembered choice; highest format; still-photo where supported
- [ ] Live detection (`VNDetectDocumentSegmentationRequest`, fallback rectangles) on downscaled preview + overlay
- [ ] Auto-capture on stable corners + sharpness + no motion (Phase 0 thresholds); manual shutter fallback; re-detect on full-resolution still
- [ ] Perspective correction exactly once (`CIPerspectiveCorrection`); no second deskew/rotation; no destructive filter
- [ ] Quality gate (Laplacian sharpness, short-edge pixel density proxy, glare share, coverage) with localized hints; failing page not accepted
- [ ] Multi-page: page-change detection + perceptual-hash duplicate guard; "Fertig" ends document
- [ ] PDF assembly: measured JPEG quality, page size from pixels + explicit DPI, no HEIC/EXIF; outbox + same upload route
- [ ] Privacy: session stops on close/background/lock/sleep (tests); frames never written to disk (test); in-app "Kamera aktiv"; permission on first desk scan
- [ ] Fixture tests from recorded Phase 0 sequences (detection, stability, page change, gate, PDF geometry)
iPad stand mode (human 3–5 d / CC 1–2 d)
- [ ] Universal app: desk mode on rear camera using `DeskScanKit`; handheld stays VisionKit; front camera not offered for documents
Distribution (human 2–3 d / CC ~1 d)
- [ ] fastlane macOS lane; TestFlight for Mac (org account, internal testers); build-age alert covers the macOS build
- [ ] Device E2E on xidra: Mac Desk View multi-page A4 letter + receipt → history processed; iPad stand scan
(Web capture in the PWA: deferred by decision R6-1 — no items. macOS distribution: TestFlight for Mac on the xidra org account, decided R6-2.)

## Phase 2 — household, then association instance
- [ ] Household pairing with auth off: any LAN device may pair (decided); test the pairing page works without login
- [ ] Per-instance relay credential + Paperless fields; device E2E per instance
- [ ] Multi-account switching E2E (three accounts on one phone)

## Phase 3 — on the road (ops)
- [ ] Self-hosted WireGuard on-demand, split DNS, same names; private runbook
- [ ] Verify uploads from cellular via tunnel; backoff path dormant
- [ ] Lost-phone runbook: tunnel key + credentials

## Phase 4 — "Automatisch" via capture router
- [ ] Router HTTP intake, device registry, per-(person, instance) credentials, L3 + review floor reuse, staging escalation
- [ ] App target "Automatisch"; router `held` state in history

## Phase 5 — receipt facts
- [ ] Eval: phantom obligations on real till receipts; implement paid-receipt suppression only if shown; VAT/payment-method kinds; no regression on existing Schicht-A eval

## Phases 6–8 — app sections (design each separately)
- [ ] 6 Fristen · 7 Dokumente/Wissen · 8 Chat — each starts with the `session` credential kind

## Review
_(filled in after each phase)_

---

# /autoplan review — 2026-09-14 (base `main` @ 02667283)

Mode: background run, every intermediate question auto-decided using the 6 principles. User decisions R2–R6 are the
default. Where the primary review and the independent voice both recommend changing one of them, the item is
queued as a **User Challenge** for the final gate and the plan is **not** changed.
**Voices:** Codex CLI is not installed on this machine, so every phase runs `[subagent-only]`: the primary review
plus one independent Claude subagent with no prior context. That is the same model family, not an outside model.
**UI scope:** yes (web "Mobile Geräte" page, web "Reisekosten" page, iOS/macOS app screens).
**DX scope:** yes (host contract `HostAPI`, hook events, plugin migration environment, mobile REST contract +
fixtures).

## Phase 1 — CEO review (SELECTIVE EXPANSION)

### System audit
- **Branch and changes:** the worktree is at `main` and the plan is still a proposal. The only new file is the
  untracked design doc. Recent history is heavy on ingest work: ingest credentials Phases 1+4 (#1218–1222) and
  scanner scan jobs (#1243). These are exactly the seams this plan builds on, and they are all fresh.
- **TODOS.md:** no mobile, receipt, expense or plugin items. The plan neither blocks nor unblocks a listed TODO.
- **Prior learnings applied:**
  - `verify-mcp-capabilities-before-recommending-async` (10/10). The ZIP export's "Paperless original via the
    Paperless MCP" fallback was checked against `services/mcp_client.py:1961`. `execute_tool(..., truncate=True)`
    is the default, and binary or large payloads are truncated unless the caller passes `truncate=False`.
  - `kubectl-set-env-leaves-manifest-drift` (10/10). This applies to the new plugin migration job, which is applied
    on deploy.
- **Taste references:**
  - Good patterns to copy:
    - `services/ingest_credentials.py`: self-identifying token, timing equalisation, bcrypt in a thread.
    - `services/sso_handoff_store.py`: single-use code with `GETDEL`.
    - `services/user_events.py`: cross-process emitter via Redis publish, plus one subscriber.
  - Anti-patterns to avoid:
    - `run_hooks` swallows exceptions and has **no timeout** (`utils/hooks.py:404-419`).
    - `_load_one_plugin` calls `fn()` with no arguments and records failures silently
      (`api/lifecycle.py:503-532`).
- **Landscape check:** web search was not run for this phase. It used in-distribution knowledge plus the design's
  own researched facts (Apple Desk View and VisionKit availability, Safari capture limits).

### 0A. Premise challenge
| # | Premise (stated or assumed) | Verdict | Why |
|---|---|---|---|
| P1 | Receipts are lost or fade between the moment of payment and scanning; capture at the moment fixes it | **valid** | Thermal fading is physical; the scanner doc shows capture quality matters |
| P2 | "On the road" is the core value | **partly** | The MVP uploads only on the home LAN, so value on the road is "doesn't fade", not "is filed". The design is honest about this, but WireGuard sits in Phase 3, behind push, reports and desk scan (→ UC4) |
| P3 | Arriving home with the app closed cannot trigger an upload without "Always" location | **likely wrong** | A Shortcuts personal automation on "joins Wi-Fi" can run an App Intent the app ships, without region monitoring. Not verified on the target iOS version → added as a Phase 0 check + M2 task (auto-approved expansion) |
| P4 | A native app is required | **valid (user decision R2)** | Keychain, background URLSession, APNs and multi-account are real needs. But the zero-install alternatives were never compared: iOS Files scan → SMB watch folder (existing folder ingest), and scan → mail to the existing email-ingest mailbox. They would size the value of M2 (recorded in 0C-bis, not a challenge) |
| P5 | Business-instance users can reach the business LAN | **unstated** | If colleagues never sit on that LAN, the MVP never uploads for them. Needs a named user list + networks in Phase 0 (auto-approved Phase 0 item) |
| P6 | Receipt volume justifies ~20+ human-weeks (MVP + 1.5 + 1.6) | **unstated** | No baseline volume, no success metric, no stop criterion between phases → Phase 0 baseline (auto-approved) + the gating part is UC3 |
| P7 | A per-instance, person-maintained rate file is an acceptable source for per diems | **questionable** | The design itself names the residual "confidently wrong numbers"; the instance's accounting intake channel (a planned change, private) may make an in-house calculation redundant → UC1 |
| P8 | A general plugin substrate is needed to satisfy "not present" | **partly** | "Not present" (R5) needs: own migration environment + `PLUGIN_MODULES` loading + a features entry. It does not need a full `HostAPI` + six hook events + frontend registry shaped by one consumer → UC2 |
| P9 | Mac/iPad desk scan is needed in addition to the existing USB scanner route | **unproven** | `renfield-mcp-scanner` already routes ADF scans to 1..n instances from the operator Mac; Desk View OCR quality is itself a Phase 0 unknown → UC3 |
| P10 | Owner self-settlement without four eyes is acceptable | **valid for a sole director, unclear for employees** | Nothing restricts self-settle to the admin/owner role → UC5 |

### 0B. Existing code leverage map
| Sub-problem | Existing code | Plan reuses? |
|---|---|---|
| Per-device machine credential, hashing, revocation, timing equalisation | `services/ingest_credentials.py` (`mint/rotate/revoke/resolve_ingest_client`, `rfi.<client_id>.<secret>`) | yes (`ROUTE_MOBILE`). **Gaps:** `mint_credential` hardcodes `route in (ROUTE_FOLDER, ROUTE_EMAIL)` (:134); `api/routes/ingest_credentials.py:_ROUTES` likewise; **no pending state**, and `rotate_credential` re-enables a revoked row (:184-186) |
| Sphere → owner user | `folder_ingest.resolve_owner_user_id(db, owner)` accepts username **or numeric id** (:487) | implicit — pairing must write the numeric user id, not a username (rename-safe) |
| Ingest bridge, 4-state result, recovery copy, dedup, PDF-Split, Paperless pending | `services/folder_ingest.ingest_document(..., source=, file_to_paperless=)` (:197) | yes |
| Push route shape, worker-alive gate, rate limit | `api/routes/email_ingest.py` (`/document`, `/health`), `@limiter.limit(settings.api_rate_limit_ingest)` (`folder_ingest.py:95`) | yes |
| Single-use pairing code | `services/sso_handoff_store.py` (`issue_handoff_code` / `consume_handoff_code`, `GETDEL`) | yes (pattern); note `consume` logs and returns `None` on a Redis error, which is indistinguishable from "invalid code" (:120-122) |
| Credential UI, token shown once, QR | `components/integrations/IngestCredentials.tsx`, `components/satellites/SatelliteEnrollment.tsx`, `components/PairInitiatorModal.tsx` (`qrcode.react` already a dependency), `components/presence/IrkPairing.tsx` | **not named in the plan** → auto-approved: build "Mobile Geräte" from these |
| Live activation feedback | `services/user_events.py` (`publish_user_event`, `/ws/user`) | yes |
| After-commit document emitters | `rag_service.py` ingest-complete (:517-525), `delete_document` (:963, :1023) | yes |
| Redis stream + consumer group | `services/task_queue.py` (only place using `XREADGROUP`, worker side) | partly. **API pods consuming a stream is a new pattern**, so its lifecycle, supervision and health have no precedent |
| Review-flow source gate | the instance review hook keys on `source == 'folder_ingest'` + `.pdf` | yes (allowlist) |
| Paperless custom fields in deferred PATCH | `services/paperless_finalize_reconciler.py` | yes |
| Plugin loading + status | `api/lifecycle.py::_load_one_plugin`, `failed_plugins()` | extended (`register(host)`) |
| Autogenerate footgun for foreign tables | `alembic/env.py` `PLUGIN_METADATA_MODULES` (:127-144), bootstrap SQL (:64-90), `transaction_per_migration=True` (:206) | yes (helper extraction) |
| Scheduled alerting | Scheduled Tasks engine + `services/ops_alert.py` | yes (build-age alert) |
| Desk document capture | `renfield-mcp-scanner` (ADF, 1..n routing) + `bin/scan.sh` | **no** — Phase 1.6 builds a parallel capture path (→ UC3) |

### 0C. Dream state
```
CURRENT                                THIS PLAN                                   12-MONTH IDEAL
paper receipts in wallets; capture     native multi-account app; home-LAN upload;  any receipt/document anywhere reaches
only via USB scanner, watch folder     push nudge; project link; expense-report    the right instance within minutes
or mailbox; no expense workflow;       plugin (+ per diems dark); Mac/iPad desk    (tunnel), routed automatically with a
no plugin substrate                    scan; tunnel only in Phase 3                review floor; the accountant receives
                                                                                    what they actually ingest, with no
                                                                                    retyping
```
**Dream state delta:** after all phases through 1.6, the "within minutes, anywhere" part is still missing (Phase 3), the
routing is still manual (Phase 4), and the hand-over format is unvalidated against the accountant's real intake
(UC1). The plan moves toward the ideal on capture quality, provenance and structured details. On the accounting
side, it risks building a parallel calculation the ideal would not contain.

### 0C-bis. Implementation alternatives
```
APPROACH A: Zero-install capture (minimal viable)
  Summary: iOS Files/Notes document scanner → save to the business SMB watch folder, or share → mail to the
           existing email-ingest mailbox. No app, no new backend route.
  Effort:  S        Risk: Low (tech) / High (adoption, no feedback)
  Pros:    ships in days; reuses folder/email ingest routing, dedup, Paperless; server-authoritative sphere per share/mailbox
  Cons:    no per-capture status or push; no structured business-meal details; mail path transits a mail provider;
           SMB needs VPN/LAN anyway; no multi-instance account model
  Reuses:  folder_ingest, email_ingest, ingest credentials Phase 4

APPROACH B: Plan as written (native app + M1–M4 + plugin substrate + 1.5a/b + 1.6)   ← user direction
  Summary: everything in R2–R6.
  Effort:  XL (MVP + ~9–11 wk + ~6–7.5 wk human)      Risk: Med-High (scope, external gates)
  Pros:    complete trust model, history, push, growth path; all product decisions settled
  Cons:    largest surface lands on the business instance first; three speculative layers (substrate, per-diem engine, desk scan)
  Reuses:  as mapped in 0B

APPROACH C: Native app core, evidence-gated extensions (ideal trajectory per both voices)
  Summary: M1–M3 + App Intent upload trigger; tunnel before push; expense reports built after the advisor names the
           intake format; plugin substrate reduced to what "not present" requires; desk scan only after Phase 0 quality
           + measured need.
  Effort:  L        Risk: Med
  Pros:    same trust model; value on the road earlier; less speculative code
  Cons:    reorders several user decisions; push later
  Reuses:  as B
```
**RECOMMENDATION:** B stays the plan, because the user decided it and principle P1 (completeness) does not argue for
cutting scope. The C deltas are surfaced as User Challenges UC1–UC5. A is recorded as the comparison baseline F8
asked for. It is not a challenge, because both reviewers agree the native app wins on status, details and multi-instance support.

### 0D. Selective expansion — hold-scope analysis + cherry-picks (auto-decided)
- **Complexity check:** this is far above 8 files and 2 services: roughly 15 new backend modules, 6 new tables core
  + 6 plugin, a relay service, and two app targets. That is a smell, but it is already phased into independently
  verifiable milestones, so no scope cut was made (P2; cutting user-decided scope is a challenge, not an auto-decision).
- **Minimum set for the stated goal:** M1 + M2 + M3. Everything after M3 is extension.

| # | Candidate | Effort | Decision | Principle |
|---|---|---|---|---|
| E1 | App Intent "Belege hochladen" + documented Shortcuts Wi-Fi automation (verify in Phase 0) | S (CC <1d) | **ACCEPTED** → Phase 0 + M2 | P2 in blast radius (app being built), removes the largest MVP residual |
| E2 | Stuck-capture detection: capture `accepted`/`processing` > N h → `ops_alert` + history badge | S | **ACCEPTED** → M4 | P1, zero silent failures |
| E3 | "Mobile Geräte" list shows last seen + build + build age per device | S | **ACCEPTED** → M1 frontend | P2, data already recorded |
| E4 | Build "Mobile Geräte" from `IngestCredentials.tsx` / `SatelliteEnrollment.tsx` / `PairInitiatorModal` QR | S | **ACCEPTED** → M1 frontend | P4 DRY |
| E5 | Phase 0 baseline (receipts/month, users, networks, time lost) + usage measurement after M3 | S | **ACCEPTED** → Phase 0 + after M3 | P1, missing success metric; threshold = UC3 |
| E6 | Evaluation of Apple custom/private app distribution vs TestFlight | S | **DEFERRED** → TODOS (was accepted; changed after spec review round 2) | P3, changes neither timeline nor R2 |

_Label note: A1–A3, S1–S4 and Q1–Q5 in this review are review findings. User decisions from the design are cited
with their round, e.g. `R4-A2` (statutory scope) and `R4-A6` (who settles)._
| E7 | iOS share-sheet extension (PDF/image from Mail/Files into the same outbox) | M (new target) | **DEFERRED** → TODOS | P3, outside M2 blast radius |
| E8 | Zero-install fallback doc (Files → watch folder / mail → mailbox) for users without the app | S | **DEFERRED** → TODOS | P3 |
| E9 | Batch capture session (N receipts → N captures) instead of relying on PDF-Split | M | **DEFERRED** → TODOS | P3 |

CEO plan persisted: `~/.gstack/projects/ebongard-renfield/ceo-plans/2026-09-14-mobile-receipt-capture.md`.

### 0E. Temporal interrogation (decisions to settle now)
```
HOUR 1 (foundations): credential lifecycle — is "pending" a column or is_enabled=False? (→ column; rotate re-enables)
                      owner on the credential row — username or numeric id? (→ numeric id)
                      is ingest_credentials_enabled on for the business instance? (→ M3 config prerequisite)
HOUR 2-3 (core):      ledger row on the DUPLICATE path and on crash between create and ledger upsert
                      extensions hook timeout value; what "raises" means when run_hooks swallows exceptions
                      PDF-Split child aggregation when a child is split_archived / superseded
HOUR 4-5 (integr.):   API-pod stream consumer lifecycle (startup, shutdown drain, replica scale-down, supervisor)
                      Paperless byte fallback path (truncate=False + sha256 check) for the ZIP
                      contract fixture format + where the app CI fetches it
HOUR 6+ (polish):     golden-file format for exports; fixture recording for DeskScanKit; iOS min version lock
```
(With CC, the human hours above compress to roughly 30–60 minutes each. The decisions are the same.)

### 0F. Mode
SELECTIVE EXPANSION (autoplan override; this is an enhancement of the existing ingest system).

### Step 0.5 — dual voices (CEO)
**CODEX SAYS (CEO — strategy challenge):** `[codex-unavailable: binary not found]`.

**CLAUDE SUBAGENT (CEO — strategic independence)** `[subagent-only]`, 12 findings:
- **F1** (critical): no quantified business case or stop criterion.
- **F2** (critical): "on the road" is fixed last; move WireGuard ahead of push and 1.5.
- **F3** (high): an App Intent + Shortcuts Wi-Fi automation removes the "arrive home with app closed" residual.
- **F4** (critical, CHALLENGES A2): per-diem/mileage engine duplicates what accounting software maintains; ask the
  advisor's intake channel first.
- **F5** (high): paper stays original (GoBD), so the report workflow is heavy for a secondary artifact.
- **F6** (high, CHALLENGES R5): substrate for one plugin; ship like ha_glue until a second plugin exists.
- **F7** (high, CHALLENGES R6): desk scan duplicates the USB scanner route; cut 1.6.
- **F8** (high): Paperless apps and Files → watch-folder not compared; consider a pilot.
- **F9** (medium): TestFlight used as production distribution; evaluate Apple custom apps.
- **F10** (medium): run capture on the household first.
- **F11** (medium, CHALLENGES A6): self-settle only for the admin role.
- **F12** (medium): phone write scope grows before the tunnel.

**Primary-review positions on the voice:**
- **F1:** agree (E5 + UC3).
- **F2:** agree (UC4).
- **F3:** agree (E1).
- **F4 + F5:** agree on sequencing (UC1). Disagree on "drop the calculation": that decision belongs to the advisor.
- **F6:** partly agree. The subagent's own fix (core tables like ha_glue) **violates R5's explicit "not present"
  requirement**, so the primary review recommends a narrower substrate instead (UC2).
- **F7:** agree on deferring behind evidence, not on cutting (UC3).
- **F8:** agree on a comparison (0C-bis A), disagree on a pilot gate (no challenge).
- **F9:** agree (E6).
- **F10:** disagree. The business instance has projects and the dry run on a test KB mitigates the risk → TASTE.
- **F11:** agree (UC5).
- **F12:** mostly already true in the plan, because trip mutations are plugin routes in 1.5 and `extensions` is
  dropped in the MVP → no change.

```
CEO DUAL VOICES — CONSENSUS TABLE:            [subagent-only]
═══════════════════════════════════════════════════════════════════════
  Dimension                            Primary  Subagent  Codex  Consensus
  ──────────────────────────────────── ──────── ───────── ────── ─────────
  1. Premises valid?                   partly   partly    N/A    CONFIRMED (partly: P2/P3/P5-P9)
  2. Right problem to solve?           yes*     partly    N/A    DISAGREE → capture yes; accounting layer unproven
  3. Scope calibration correct?        no       no        N/A    CONFIRMED (over-scoped beyond M3) → UC1-UC3
  4. Alternatives sufficiently explored? no     no        N/A    CONFIRMED → 0C-bis A added
  5. Competitive/market risks covered? partly   no        N/A    CONFIRMED gap on per-diem/export layer → UC1
  6. 6-month trajectory sound?         partly   no        N/A    CONFIRMED risk (tunnel late, substrate/desk scan speculative)
═══════════════════════════════════════════════════════════════════════
CONFIRMED = primary and subagent agree. DISAGREE → taste decision. Codex N/A (not installed).
```

### Sections 1–10 (+11)

**§1 Architecture.**
System diagram (from the design, validated against code):
```
 iPhone/iPad/Mac app ──(home LAN, TLS pin)──► ingress ──► API pod: api/routes/mobile_capture.py
   │ outbox (file)                                          │ resolve_ingest_client(ROUTE_MOBILE)
   │                                                        ▼
   │                                          services/mobile_capture.py ──► folder_ingest.ingest_document
   │                                                        │ mobile_capture_log / capture_details / devices
   │                                                        ▼
   │                                       document worker (PDF-Split → OCR → Schicht-A → KG)
   │                                                        │ after-commit
   │                                                        ▼
   │                          Redis stream renfield:tasks:mobilepush ──► consumer group (API pods)
   │                                                        ▼
   └◄────── APNs ◄──── push relay (own namespace) ◄─────────┘
                        plugin expense_reports (own migrations) ◄── hooks: capture_extensions_apply …
```
Findings (all auto-decided, logged):
- **A1: pending credential state has no home** (`IngestCredential` has only `is_enabled`/`revoked_at`), and
  `rotate_credential` re-enables revoked rows. An admin "rotate" on an expired pending row would activate it without
  pairing. → **add an explicit `status` (pending|active|revoked) + `pending_expires_at`, and make rotate refuse
  pending** (P5).
- **A2: the new route value is hardcoded in two places** (`ingest_credentials.py:134`,
  `api/routes/ingest_credentials.py:47-51`) → one `ROUTES` tuple in the service, imported by the route (P4).
- **A3: the API-pod stream consumer is a new process shape** with no supervisor or health signal. `task_queue`
  consumers live in worker pods. → consumer as a supervised lifecycle task (restart with backoff), lag/pending gauge
  in `internal.system_health`, drain on shutdown (P1).
- **A4: coupling.** The core gains a dependency on a new external service (the relay). Push is best-effort, so it is
  justified. The core → plugin direction is enforced by the import-isolation test (already in the plan). OK.
- **Scaling:** at 10× captures, the bottleneck is bcrypt per request (~150ms in a thread, already off-loop) and the
  document worker, not the route. Rollback: flag off + credential revoke; plugin removal from `PLUGIN_MODULES`.

**§2 Error & Rescue Map.** See the registry below. 14 codepaths, **5 GAPS**.

**§3 Security & threat model.**
Threats examined: stolen token, QR replay, CSRF on pairing, IDOR on `/captures` and PATCH, extension-payload
injection, ZIP concentration, relay abuse, prompt injection via details.

New findings:
- **S1: pairing page CSRF and QR timing are covered, but the unauthenticated `POST /pair` shares the limiter's
  keying.** Phones behind one NAT or the ingress IP could throttle each other, and the attacker's view is one global
  bucket. → rate-limit per pairing code **and** per client IP, with a small global cap (Med/Low; P1).
- **S2: `capture_extensions_apply` receives untrusted JSON.** Its size is capped, but depth is not. → cap nesting
  depth (≤ 4) and total keys in the structural validator (Low/Med; P1).
- **S3: ZIP fallback via the Paperless MCP truncates binary by default.** Without `truncate=False` and a sha256
  check against `documents.file_hash`, a corrupted PDF could ship silently inside a financial export (Med/High) →
  **GAP**, task added.
- **S4: `resolve_ingest_client` returns `None` for wrong route, revoked and wrong secret alike (deliberate for
  timing).** The plan's "401/403" distinction must not reintroduce an oracle → always 401 (P5).

Already mitigated: IDOR (own `client_id` scope, 404), tier from the credential row, details never in chunks, loc-key-only push.

**§4 Data flow & interaction edge cases.**
Upload flow shadow paths:
```
 capture bytes ─► validate(meta,details,extensions) ─► ingest_document ─► ledger upsert ─► 4-state response
    │nil file        │invalid → failed/*               │retry (disk/pool)   │crash here → doc exists, no ledger ◄ GAP
    │empty PDF       │oversize → 413 failed            │duplicate           │duplicate path must upsert ledger ◄ GAP
    │HEIC renamed    │non-PDF magic → failed           │PDF-Split children  │extensions hook hangs (no timeout) ◄ GAP
```
| Interaction | Edge case | Handled? | How / fix |
|---|---|---|---|
| Upload | same capture sent twice (background retry) | yes | `(client_id, capture_id)` unique + hash dedup |
| Upload | same capture_id, different bytes | yes | `failed/capture_id_conflict` |
| Upload | 0-byte or truncated PDF | **partly** | magic-byte check passes on a truncated file → add "trailer `%%EOF` present" check (P1) |
| Pairing | QR scanned twice / code expired mid-flow | yes | `GETDEL`, TTL |
| Pairing | Redis down | **no** | `consume_handoff_code` returns None → user sees "invalid code" → **distinct 503** (task) |
| PATCH | edited on web meanwhile | yes | `If-Match` 412 |
| History | capture stuck in `processing` for hours | **no** | E2 stuck-capture alert |
| Push | relay down for > bounded age | yes | dropped, history still correct |
| Revoke | outbox still holds captures | yes | 403 keeps bytes; app shows "Gerät widerrufen" (needs copy, see Design) |

**§5 Code quality.**
- **Q1 (DRY):** a second route list (A2).
- **Q2 (DRY):** a single-use code store. `sso_handoff_store` is PKCE-shaped. Extract a generic
  `one_time_code_store` (issue/consume with `GETDEL` + a typed Redis-error result) rather than cloning it (P4;
  auto-approved, <1d).
- **Q3:** `capture_extensions_apply` via `run_hooks` cannot tell "rejected" from "crashed" from "no handler"
  (`run_hooks` docstring :409). → use `run_hooks_with_errors` + explicit handler count (P5).
- **Q4 (over-engineering):** `HostAPI` has 8 members for one consumer (→ UC2).
- **Q5 (naming):** there are three names for one thing (`trip_ref`, `extensions.expense_reports.trip_ref`, "Reise").
  Fine once R5 wording is applied; stale R3 text in the design keeps `trip_ref` top-level → recorded in design
  "supersedes" note already. OK.

**§6 Test review.** The test diagram is produced in Phase 3 (Eng). CEO-level gaps:
- no test for the ledger on the duplicate path;
- no hook-timeout test;
- no Redis-down pairing test;
- no ZIP byte-integrity test;
- no API-pod consumer restart test.

All five were added as tasks. For LLM changes: Phase 5 touches Schicht-A prompts → the existing Schicht-A eval must
run (the plan says so).

**§7 Performance.**
- `GET /captures?since=` needs an index on `mobile_capture_log (client_id, updated_at)` → add to the migration (P1).
- Status aggregation over PDF-Split children is N+1 if done per capture → one query joining `split_from_document_id`
  (P3).
- ZIP streaming is STORED + chunked (good); the pre-flight sum must not load bytes.

**§8 Observability.**
- **Present:** `last_authenticated_at`, build-age alert, failed-plugin alert, push consumer.
- **Missing:**
  - an ingest counter per `source=mobile_capture` and state;
  - consumer lag/pending gauge (A3);
  - stuck-capture alert (E2);
  - relay 4xx/5xx counters;
  - a runbook line per failure mode in `docs/MOBILE_CAPTURE.md`.

  All were added to M4 / the docs sweep (P1).

**§9 Deployment & rollout.**
- **Migrations:** additive, so zero-downtime is fine.
- **Plugin job ordering** is enforced by `REQUIRES_CORE_REVISION` (good).
- **Flags:** `MOBILE_CAPTURE_ENABLED`, `MOBILE_CAPTURE_PUSH_ENABLED`, per-diem flag.
- **Risks:**
  - `ingest_credentials_enabled` must be **on** on the business instance, or every `rfi.` token falls through to
    legacy and fails (`ingest_credentials.py:231`) → M3 checklist item (P1);
  - the new deploy step for the plugin job must go into `bin/deploy-production.sh` in the same PR as the job
    manifest (prior learning: manifest drift).
- **Post-deploy:** browser E2E of pairing (already planned) + `/api/mobile-capture/health` probe.

**§10 Long-term trajectory.**
- **Reversibility: 3/5.** The backend is flag-reversible; the app on devices and APNs registration are sticky; plugin
  data survives deactivation by design.
- **Debt:**
  - the relay repository;
  - yearly rate-file maintenance (→ UC1);
  - a second Alembic environment (→ UC2);
  - a macOS target + camera pipeline (→ UC3).
- **Platform potential:** high for the credential-kind + section registry (Phases 6–8), and for
  `document_project_links`.

**§11 Design & UX (CEO lens).** UI scope is confirmed and a full pass runs in Phase 2. CEO-level concern: the
states "revoked device", "build expired", "not home" and "assignment_invalid" are user-visible trust moments that
the plan names only as backend states.

### Error & Rescue Registry (Phase 1)
```
CODEPATH                          | WHAT CAN GO WRONG                     | RESCUED? | ACTION                                   | USER SEES
----------------------------------|---------------------------------------|----------|------------------------------------------|---------------------------
POST /document                    | flag off                              | Y        | 503                                      | outbox keeps, "Instanz deaktiviert"
                                  | token invalid/revoked/wrong route     | Y        | 401 (uniform)                            | "Gerät widerrufen – neu koppeln"
                                  | worker dead                           | Y        | 503 retry                                | queued
                                  | DB pool exhausted / disk full         | Y        | bridge → retry                           | queued
                                  | ledger upsert fails after create      | N ← GAP  | idempotent upsert on retry + dup path    | (silent: no status) ← fix
                                  | extensions handler hangs              | N ← GAP  | asyncio.wait_for per namespace           | (request hang) ← fix
POST /pair                        | code expired / replayed               | Y        | 400 code_invalid                         | "Code abgelaufen"
                                  | Redis unavailable                     | N ← GAP  | 503 pairing_unavailable                  | (misleading "invalid") ← fix
PATCH /captures/{id}              | stale version / locked report         | Y        | 412 / 409                                | conflict sheet
push consumer                     | relay down / 429                      | Y        | pending retry, bounded age               | none (history correct)
                                  | consumer task crashes                 | N ← GAP  | supervised restart + gauge               | (silent: no pushes) ← fix
paperless custom fields           | field missing / Paperless 5xx         | Y        | reported in health / reconciler retry    | admin health note
ZIP export                        | Paperless fallback truncated/corrupt  | N ← GAP  | truncate=False + sha256 verify           | (corrupt PDF in ZIP) ← fix
rate file                         | invalid / overlapping / unreviewed    | Y        | calculation off + ops_alert              | "Berechnung nicht freigegeben"
```

### Failure Modes Registry (Phase 1)
```
CODEPATH             | FAILURE MODE                               | RESCUED? | TEST? | USER SEES?          | LOGGED?
---------------------|--------------------------------------------|----------|-------|---------------------|--------
ledger upsert        | crash between create and ledger / dup path | N        | N     | Silent (no status)  | N   ← CRITICAL GAP
push consumer        | task dies, no restart                      | N        | N     | Silent (no pushes)  | Y
ZIP fallback bytes   | MCP truncation → corrupt PDF               | N        | N     | Silent (bad export) | N   ← CRITICAL GAP
extensions hook      | handler hang                               | N        | N     | request hang/retry  | N
pairing consume      | Redis down read as invalid code            | Y*       | N     | misleading message  | Y
pending credential   | rotate re-enables expired pending row      | N        | N     | Silent activation   | Y
TestFlight expiry    | build > 90 d                               | Y        | Y     | banner              | Y
```
**2 CRITICAL GAPS** (silent + unrescued + untested): ledger-on-duplicate/crash; ZIP byte integrity. Both are now
plan tasks (see "Autoplan-added tasks").

### NOT in scope (Phase 1)
- **E7 share-sheet extension:** new app target, outside M2 → TODOS.
- **E8 zero-install fallback doc:** useful but not blocking → TODOS.
- **E9 batch capture session:** PDF-Split is the safety net → TODOS.
- **A household-first pilot (F10):** kept business-instance first; it is a taste choice at the gate.
- **A Files → watch-folder adoption pilot before M2 (F8):** comparison recorded; a pilot gate is not adopted.

### What already exists
See 0B. **Unnecessary rebuilds found:**
- a one-time code store (Q2);
- credential/QR UI (E4);
- desk capture next to the existing scanner route (UC3).

### Completion summary (CEO)
```
+====================================================================+
|            MEGA PLAN REVIEW — COMPLETION SUMMARY                   |
+====================================================================+
| Mode selected        | SELECTIVE EXPANSION                          |
| System Audit         | fresh ingest-credential/scanner seams; no TODO overlap |
| Step 0               | 10 premises (5 questioned); 3 approaches; B kept |
| Section 1  (Arch)    | 4 issues (A1-A4)                             |
| Section 2  (Errors)  | 14 paths mapped, 5 GAPS                      |
| Section 3  (Security)| 4 issues, 1 High (S3)                        |
| Section 4  (Data/UX) | 9 edge cases mapped, 3 unhandled             |
| Section 5  (Quality) | 5 issues                                     |
| Section 6  (Tests)   | 5 gaps (diagram in Phase 3)                  |
| Section 7  (Perf)    | 3 issues                                     |
| Section 8  (Observ)  | 5 gaps                                       |
| Section 9  (Deploy)  | 2 risks                                      |
| Section 10 (Future)  | Reversibility 3/5, 4 debt items              |
| Section 11 (Design)  | 1 concern → Phase 2                          |
+--------------------------------------------------------------------+
| NOT in scope         | written (5 items)                            |
| What already exists  | written                                      |
| Dream state delta    | written                                      |
| Error/rescue registry| 14 paths, 5 GAPS                             |
| Failure modes        | 7 total, 2 CRITICAL GAPS                     |
| TODOS.md updates     | 3 items proposed (not written — run is plan-file-only) |
| Scope proposals      | 9 proposed, 6 accepted, 3 deferred           |
| CEO plan             | written                                      |
| Outside voice        | ran (claude subagent; codex unavailable)     |
| Lake Score           | 14/15 recommendations chose complete option  |
| Diagrams produced    | 3 (system, dream state, upload shadow paths) |
| Stale diagrams found | 0                                            |
| Unresolved decisions | 5 User Challenges + 1 taste (to gate)        |
+====================================================================+
```

> **Phase 1 complete.** Codex: unavailable. Claude subagent: 12 findings. Consensus: 5/6 confirmed, 1 disagreement → surfaced at the gate. Passing to Phase 2.

## Phase 2 — Design review

### Step 0 — design scope
- **0A. Initial rating: 4/10.** The plan names screens (capture, history, edit, trip, "Mobile Geräte", "Reisekosten")
  and backend states, but it never says what a person sees first, what each state looks like, or which words carry
  the trust moments: device revoked, not home, build expired, receipt not assigned, calculation not released.
  - **What a 10 looks like here:**
    - a navigation map for app + web;
    - a state table per surface;
    - fixed German/English copy for the trust moments;
    - token-level DESIGN.md mapping for the web;
    - an explicit visual language for the native apps;
    - accessibility (a11y) specs for Dynamic Type and VoiceOver.
- **0B. DESIGN.md exists.**
  - The web pages calibrate against it. It predefines `.pairing-qr-modal`, `.empty-state`, `.toast` and the tier
    visual language.
  - **Gap:** DESIGN.md is web-only. It says nothing about the SwiftUI apps (→ D-T1 taste decision).
- **0C. Existing design leverage:**
  - `IngestCredentials.tsx` (token once, status badges `Badge color=red|yellow|green`);
  - `SatelliteEnrollment.tsx`;
  - `PairInitiatorModal.tsx` (3-step modal, `QRCodeSVG`);
  - `components/meetings/StatusBadge.tsx` + the MeetingDetailPage draft-nudge banner (a pattern for report status);
  - `ProjectSelect.tsx`;
  - `TierPicker` / `TierBadge` (tier choice at pairing);
  - `.empty-state` utility (`index.css:261`);
  - the undo `.toast`.
- **0D. Focus:** all 7 passes (auto-decided, P1).
- **Step 0.5 mockups:** the design binary is present, but no image-model credential is configured (neither
  `~/.gstack/openai.json` nor the `OPENAI_API_KEY` environment variable), so it falls back to text wireframes below.
  No comparison board.

### Step 0.5 — dual voices (design)
**CODEX SAYS (design — UX challenge):** `[codex-unavailable: binary not found]`.

**CLAUDE SUBAGENT (design — independent review)** `[subagent-only]`. It reviewed with no prior-phase context; 21
findings, of which 5 critical:
- **Hierarchy:**
  - type choice before scanning costs a decision at the till → scan first, type after (high);
  - history exposes seven engineering states → four user buckets (**critical**);
  - trip screen is a form wall → next-missing-input + drill-downs (high);
  - "Reisekosten" page is unspecified (**critical**).
- **States:**
  - TLS pin mismatch after a certificate rotation looks like "not home" forever (**critical**);
  - device revoked: no path for bytes left in the outbox (**critical**);
  - build expiry needs a user countdown, not only an admin alert (**critical**);
  - `needs_review` from PDF-Split or the review flow is a dead end in the app → "Auf dem Web prüfen" + URL;
  - "Berechnung nicht freigegeben" reads as an error → neutral info;
  - outbox/disk low → warn before scanning;
  - permission-denied screens;
  - quality gate with no escape → "Trotzdem aufnehmen" after N tries;
  - pairing error copy; 412/409 copy; empty states; purged thumbnail row; contract-too-old banner.
- **Journey:**
  - processed → report breaks: nothing brings the user to the web to confirm amounts and submit → summary card after
    "Reise beenden" + a content-free push when a trip's receipts are all processed;
  - forgotten active trip silently assigns receipts.
- **Ambiguities:**
  - native tokens undecided;
  - pairing success signalled by colour only;
  - revoke confirm vs undo;
  - per-diem toggle forced at trip start;
  - multi-account target must show on the capture sheet;
  - ZIP 413 copy;
  - missing de/en status vocabulary.

**Primary-review position:**
- It agrees with 19 of the findings. The fixes below are applied to the plan as structural (P5 + P1).
- Two findings change a sentence the design doc states and are taste decisions:
  - **D-T3** scan first vs type first;
  - **D-T4** the quality-gate escape hatch.

```
DESIGN LITMUS SCORECARD (web pages, plan as originally written)      [subagent-only]
═══════════════════════════════════════════════════════════════════════════════
  Check                                         Primary  Subagent  Codex  Consensus
  ──────────────────────────────────────────── ──────── ───────── ────── ─────────
  1. Brand/product unmistakable in first screen? NO       NO        N/A    CONFIRMED (fixed: H1 + lede, Pass 1)
  2. One strong visual anchor present?           NO       NO        N/A    CONFIRMED (fixed: device table / report totals header)
  3. Understandable by scanning headlines only?  NO       NO        N/A    CONFIRMED (fixed: named section headings)
  4. Each section has one job?                   NO       NO        N/A    CONFIRMED (fixed: Belege / Pauschalen / Export / Verlauf split)
  5. Are cards actually necessary?               NO       NO        N/A    CONFIRMED (tables, no card mosaic)
  6. Does motion improve hierarchy?              NO       NO        N/A    CONFIRMED (one motion: pairing activation, reduced-motion safe)
  7. Premium with decorative shadows removed?    YES      YES       N/A    CONFIRMED (DESIGN.md default)
═══════════════════════════════════════════════════════════════════════════════
Consensus 7/7 confirmed. Codex N/A (not installed).
```

### Design fixes adopted from the independent voice (structural, auto-applied)
- **History buckets (replaces the raw 7-state list in Pass 1/2):**
  ```
  ENGINE STATE                         → USER BUCKET (de / en)                              ORDER
  queued, uploading                    → "Sicher gespeichert – wird im Heimnetz gesendet"   2
                                         / "Saved – sends on your home network"
  accepted, processing (stuck=false)   → "In Arbeit" / "Processing"                        3
  processed                            → "Erledigt" (+ Paperless mark) / "Done"            4
  needs_review, failed, stuck=true,    → "Du bist dran: <reason>" / "Needs you: <reason>"  1 (top)
  assignment_invalid, 401/403 revoked
  ```
- **Trip card:** shows state + the single next missing input + a completeness counter ("2 von 4 Tagen erfasst").
  Tage and Fahrten are drill-downs.
- **New states:**
  - **TLS pin mismatch:** its own account state, "Zertifikat geändert – neu koppeln".
  - **Device revoked:** account banner "Gerät widerrufen – N Belege gespeichert" with **Neu koppeln und senden** or
    **Belege löschen** (explicit, confirm dialog).
  - **Build age:** in-app countdown from day 75 with a TestFlight link. The plan's risk list gains "an expired build
    does not launch; outbox stranded until reinstall".
  - **`needs_review` without an in-app fix:** "Auf dem Web prüfen" + instance URL.
  - **"Berechnung nicht freigegeben":** info semantic (`accent-500`), with copy "Pauschalen werden später berechnet;
    Belege sind vollständig".
  - **Storage low:** warning before scanning.
  - **Permission denied** (camera, location, notifications): explanation + Settings deep link + free-text fallback.
  - **Contract too old:** per-account banner instead of silent hiding.
  - **Purged thumbnail:** text row with a document symbol.
- **Journey:**
  - After "Reise beenden": summary card "3 Belege, 2 Beträge zu bestätigen – im Web abschließen" + instance URL.
  - New content-free push template `push.trip.ready_for_review` when all receipts of an ended trip are processed.
    The plugin emits it via the `capture_state_changed` observer.
  - An active trip older than 3 days shows "Reise noch aktiv seit <Datum>" on the capture sheet.
- **Per-diem toggle:** can be switched on later while the trip is `open`. Times are then required at "Reise beenden"
  and submit (A10 unchanged).
- **Capture sheet:** always states the target account ("an <Instanz>") above "Senden". This makes the routing
  invariant visible at the moment of the choice.
- **Pairing success:** text, not colour: "Gekoppelt: <Gerätename>, 14:02 – nicht Sie? Widerrufen".
- **ZIP 413 copy:** "Export zu groß (<n> MB) – nach Kategorie oder Zeitraum exportieren". Split export → TODOS
  (proposed).
- **Status vocabulary:** one de/en loc-key table for states, reasons and trip/report status, shared by web and app
  through the contract fixtures.

**Taste decisions from Phase 2** (surfaced at the gate):
- **D-T1 native visual language:**
  - recommended: platform conventions + brand palette mapping, no Cormorant in the app;
  - alternative: port the DESIGN.md typography into SwiftUI.
- **D-T2 per-day entry:**
  - recommended: day list with "wie Vortag";
  - alternative: a single week grid.
- **D-T3 capture order:**
  - recommended: keep the type segmented control *before* the scan, per the design doc, because it is one
    remembered tap and switches VisionKit into the Bewirtung flow;
  - alternative, the subagent's: scan first, choose the type on the send sheet.
- **D-T4 quality-gate escape:**
  - recommended: add "Trotzdem aufnehmen" after 3 consecutive rejections, marked low quality in metadata;
  - alternative: keep "a failing page is not accepted" as the design states.

_(Voice summary for Phase 2: Codex unavailable; Claude subagent 21 findings, 5 critical; litmus consensus 7/7
confirmed; 2 disagreements D-T3/D-T4 → gate. The Phase 2 transition line follows the passes below.)_

### Pass 1 — Information architecture: 3/10 → 8/10
**App (iPhone/iPad; macOS mirrors with a sidebar):**
```
┌ Account bar (always visible): ● colour dot + instance label + "3 in Warteschlange" ─────────┐
│  tap → account switcher sheet (paired accounts, "Gerät koppeln", per-account status)          │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ Tabs:  [Erfassen]   [Verlauf]   [Reise]*        *only when the account's health lists the plugin│
│                                                                                              │
│ ERFASSEN (first)   1. capture type: Beleg | Bewirtung (segmented, remembers last)            │
│                    2. primary button "Scannen" (VisionKit / desk mode on Mac, iPad stand)    │
│                    3. context chips: Projekt ▾ · Reise ▾ (active trip pre-selected, removable)│
│ after scan         details sheet (Bewirtung only: Anlass*, Teilnehmer, Ort [Ort vorschlagen]) │
│                    → "Gespeichert – wird zu Hause an <Instanz> übertragen" (queued state)     │
│ VERLAUF            grouped Heute / Diese Woche / Älter; row = thumbnail · type · local date · │
│                    place · state pill (icon+text) · Paperless mark; filter chip "Probleme"    │
│ REISE              active trip card (Zweck, Ziel, since, receipts count, missing-data badge) │
│                    → Tage · Fahrten · "Reise beenden"; past trips list (read-only after submit)│
│ ⚙ Einstellungen    accounts · Ortsvorschlag (Apple / offline Stadt / aus) · Vorschaubilder   │
└──────────────────────────────────────────────────────────────────────────────────────────────┘
```
Constraint worship: the capture screen shows three things (account, type, scan). Everything else is one tap away.

**Web "Einstellungen → Mobile Geräte":**
```
H1 "Mobile Geräte"  ·  lede: one sentence what a paired device can and cannot do
[Gerät koppeln] (btn-primary)
Table (not cards): Gerät · Status pill · Stufe (TierBadge) · zuletzt gesehen · Build (age warning) · [Widerrufen]
Pairing = .pairing-qr-modal, 3 steps: 1 Stufe wählen (TierPicker, default = user default)
                                      2 QR + "In der Renfield-App öffnen" + code  (expiry countdown)
                                      3 live: "Warte auf Gerät…" → "Gekoppelt: <device name>" (user event)
```
**Web "Reisekosten" (plugin):**
```
List: sections Offen / Eingereicht / Abgerechnet (table rows: Zeitraum · Zweck · Ziel · Belege · Summe bestätigt · Status)
Detail: header (Zweck, Ziel, Zeitraum, Projekt, StatusBadge, primary action by state: Einreichen | Abrechnen | —)
        1 Belege (table; unconfirmed amount = inline input with Schicht-A suggestion "Vorschlag 23,40 €" + ✓)
        2 Pauschalen & Kilometer (lines with formula disclosure, or banner "Berechnung nicht freigegeben")
        3 Export (PDF · CSV · ZIP) — enabled only when submitted/settled or owner
        4 Verlauf (event log, collapsed)
```

### Pass 2 — Interaction state coverage: 1/10 → 8/10
```
FEATURE          | LOADING               | EMPTY                                  | ERROR                                        | SUCCESS                         | PARTIAL
-----------------|-----------------------|----------------------------------------|----------------------------------------------|---------------------------------|-------------------------------
Capture          | scanner opening       | no account → "Koppeln Sie zuerst ein   | camera denied → explain + "Einstellungen     | "Gespeichert – wird zu Hause    | details invalid → inline field
                 |                       | Gerät" + pair CTA                      | öffnen"; storage full → "Speicher voll"      | übertragen" (queued)            | errors before save
Outbox/upload    | "Überträgt 2 von 5"   | "Alles übertragen" (quiet)             | not home: "Nicht im Heimnetz – startet zu    | row → Angenommen                | some accepted, some kept
                 |                       |                                        | Hause"; revoked 401: "Gerät widerrufen – neu | (processing)                    | (per-row pills)
                 |                       |                                        | koppeln" (bytes kept); build expired banner  |                                 |
History          | skeleton rows         | "Noch keine Belege" + Scannen CTA      | refresh failed → last-synced time + retry    | pills: Verarbeitet · Prüfung    | split receipt: "3 Belege
                 |                       |                                        |                                              | nötig · Fehlgeschlagen          | erkannt" child count
Assignment       | lookups refreshing    | no projects → picker hidden            | assignment_invalid → amber pill "Zuordnung   | chip shows project/trip         | —
                 |                       |                                        | prüfen" → edit screen                        |                                 |
Edit capture     | saving                | —                                      | 412 → "Im Web geändert – neu laden"; 409 →   | toast "Gespeichert"             | —
                 |                       |                                        | "Abrechnung eingereicht – gesperrt"          |                                 |
Trip (app)       | syncing offline trip  | "Keine aktive Reise" + Reise starten   | times missing (per diem) → card badge +      | "Reise beendet"                 | days/legs incomplete →
                 |                       |                                        | pickers; trip no longer open → read-only     |                                 | checklist on card
Mobile Geräte    | table skeleton        | .empty-state: "Noch kein Gerät         | code expired → step 2 "Code abgelaufen –     | step 3 "Gekoppelt" (live)       | pending device row "wartet
(web)            |                       | gekoppelt" + Koppeln CTA               | neuen Code"; Redis 503 → "Kopplung gerade    |                                 | auf Kopplung" + expiry
                 |                       |                                        | nicht möglich"                               |                                 |
Reisekosten (web)| skeleton              | .empty-state: "Noch keine Reise" +     | submit 422 times_required → field anchors;   | StatusBadge change + event row  | hidden receipt: "1 Beleg für
                 |                       | hint "In der App starten"              | export 413 → size message; rate file invalid |                                 | Sie nicht sichtbar"
                 |                       |                                        | → banner "Berechnung nicht freigegeben"      |                                 |
Desk scan (Mac)  | camera starting       | no camera → which cameras are          | quality gate: "näher heran" / "Blendung" /   | page counter "Seite 2 erfasst"  | duplicate page skipped hint
                 |                       | supported + UVC hint                   | "unscharf" (camera stays live)               |                                 |
```

### Pass 3 — User journey and emotional arc: 3/10 → 7/10
```
STEP | USER DOES                          | USER FEELS              | PLAN SPECIFIES?
-----|------------------------------------|-------------------------|------------------------------------------------
1    | pays a business lunch, opens app   | hurried, at the table   | now: 3-thing capture screen, type remembered
2    | scans, types Anlass/Teilnehmer     | slightly awkward socially| now: Anlass only required; Teilnehmer optional; ≤ 20 s budget
3    | closes app, travels                | "did it save?"          | now: explicit queued confirmation naming the instance
4    | back home, app closed              | forgets                 | E1 Wi-Fi automation (if Phase 0 passes); else residual
5    | push "Beleg verarbeitet"           | relief                  | yes (push) — history is truth
6    | "Prüfung nötig" / "Zuordnung prüfen"| mild worry             | now: pill + one-tap edit screen, reason in plain words
7    | on the web: confirm amounts, submit| diligence               | now: Schicht-A suggestion inline, one-click confirm
8    | settles / exports ZIP              | done, trusts it         | now: formula disclosure + "not calculated" banner
```
Time horizons:
- **5 seconds:** the account colour makes the instance unmistakable (the routing invariant made visible).
- **5 minutes:** outbox honesty ("nicht im Heimnetz").
- **5 years:** exports that can be recomputed by hand.

### Pass 4 — AI slop risk: 5/10 → 8/10
- **Classifier:** App UI.
- **Risks:**
  - "device list" and "report list" defaulting to card mosaics;
  - status as colour-only dots;
  - a hero-style empty state.
- **Fixes (auto, P5):**
  - tables for lists;
  - pills always icon + text;
  - `.empty-state` = one sentence + one CTA (no illustration blob);
  - section headings state the area ("Belege", "Pauschalen & Kilometer");
  - no emoji in UI copy.

### Pass 5 — Design system alignment: 5/10 → 8/10
- **Web tokens:**
  - status pills: success = `accent-600`, warning = `primary-300` on cream with `primary-700` text,
    error = `primary-700` on `primary-50`, info = `accent-500`;
  - buttons `.btn-primary`/`.btn-secondary`;
  - focus ring `accent-500`;
  - Cormorant only for H1 ("Mobile Geräte", "Reisekosten", ≥24px);
  - tables use tabular nums for money;
  - stage tier = `TierBadge` symbol + label.
- **New class names:** reuse `.pairing-qr-modal` (already in the DESIGN.md vocabulary); add `.money-input`
  (inline confirm amount) only if the existing `.input` cannot carry tabular nums + suffix; add to DESIGN.md in the
  same PR.
- **Native apps: DESIGN.md is silent → D-T1 (taste):**
  - recommended: follow Apple's platform conventions (SF fonts, Dynamic Type, system materials);
  - map the brand palette into the asset catalog (crimson = tint/primary, turquoise = focus/info, cream = light
    grouped background accent);
  - account colours use a separate 8-colour, colour-blind-safe set, always with a text label;
  - Cormorant is not used in the app.

### Pass 6 — Responsive and accessibility: 2/10 → 7/10
- **Pairing when the web page is open on the same iPhone (auto-approved, P2 <1d):**
  - the QR cannot scan its own screen, and the R6 deep link exists only for the Mac;
  - use the same custom-URL-scheme "In der Renfield-App öffnen" link on iPhone/iPad Safari, plus the manual code field.
- **Web:**
  - "Mobile Geräte" and "Reisekosten" tables collapse to stacked rows below 640px (label/value pairs, action last);
  - the pairing modal becomes full-screen on phones;
  - QR ≥ 240px.
- **Native:**
  - Dynamic Type up to AX5 (history rows wrap, pills stay icon + text);
  - VoiceOver label per history row ("Bewirtung, 14. Okt., Prüfung nötig");
  - 44pt targets;
  - the desk-scan overlay honours Reduce Motion (static corners, no pulsing);
  - the capture countdown is announced.
- **Colour never alone** for state or account (text + icon), per DESIGN.md §Accessibility.

### Pass 7 — Unresolved design decisions (auto-decided)
```
DECISION                                   | IF DEFERRED                                   | AUTO-DECISION
-------------------------------------------|-----------------------------------------------|------------------------------------------
Native visual language                     | three platforms drift, brand lost or HIG broken| D-T1 TASTE: HIG + brand palette mapping
Account colour set                         | two accounts same hue → wrong-instance tap    | 8 colour-blind-safe colours + label, unique per phone
Where "assignment_invalid" is fixed        | users ignore amber pills                      | one-tap from pill to edit screen; web mirror
Copy for trust moments (de+en)             | engineer ships "401 Unauthorized"             | fixed loc keys listed in Pass 2 table
Per-diem data entry density                | 10-day trip = 10 forms                        | D-T2 TASTE: day list with "wie Vortag" copy-forward
Revoke UX                                  | accidental revoke, no undo possible           | confirm dialog naming device; no undo toast (irreversible)
Report primary action by state             | three competing buttons                       | one primary per state (Einreichen / Abrechnen)
Mac desk-scan window                       | preview competes with form                    | preview left, page strip + Fertig right; details after Fertig
```

### NOT in scope (design)
- Visual mockups: no image-model credential. Rerun `/plan-design-review` with the key set to produce a comparison board.
- A web capture UI (deferred by R6-1).
- An app icon and branding for TestFlight: needed before M2, but outside this review → TODOS (proposed).

### What already exists (design)
See Step 0C. Reuse `.pairing-qr-modal`, `.empty-state`, `.toast`, `Badge`, `StatusBadge`, `ProjectSelect`,
`TierPicker`/`TierBadge`, and the Modal component.

### Completion summary (design)
```
+====================================================================+
|         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
+====================================================================+
| System Audit         | DESIGN.md present (web only); UI scope app+web |
| Step 0               | 4/10 initial; all 7 passes; mockups n/a (no key)|
| Pass 1  (Info Arch)  | 3/10 → 8/10                                 |
| Pass 2  (States)     | 1/10 → 8/10                                 |
| Pass 3  (Journey)    | 3/10 → 7/10                                 |
| Pass 4  (AI Slop)    | 5/10 → 8/10                                 |
| Pass 5  (Design Sys) | 5/10 → 8/10                                 |
| Pass 6  (Responsive) | 2/10 → 7/10                                 |
| Pass 7  (Decisions)  | 8 resolved (2 taste), 0 deferred            |
+--------------------------------------------------------------------+
| NOT in scope         | written (3 items)                           |
| What already exists  | written                                     |
| TODOS.md updates     | 1 proposed (app icon/branding; not written) |
| Approved Mockups     | 0 generated (no image credential)           |
| Decisions made       | 8 added to plan                             |
| Decisions deferred   | 0                                           |
| Overall design score | 4/10 → 7.5/10                               |
+====================================================================+
```

> **Phase 2 transition (end).** Design outputs above are complete; DX follows.

## Phase 2.5 — DX review (DX POLISH)

**Product type:** Platform/Library, i.e. an in-repo plugin host contract, plus an API/Service contract (the mobile
REST routes + JSON fixtures consumed by a separate private iOS repository). The primary surface for this review is
the plugin contract.

### 0A. Developer persona (auto-decided, P6)
```
TARGET DEVELOPER PERSONA
========================
Who:       a maintainer of this self-hosted project — or an AI coding agent working for them — adding the 2nd..nth
           in-repo plugin (the first being expense_reports)
Context:   months after 1.5-0 shipped; has CLAUDE.md, the /add-hook skill, and the design doc; no one to ask
Tolerance: ~30 min to a running hello-world plugin; an agent will copy whatever example exists verbatim
Expects:   a template to copy, one migrate command, loud errors that name the fix, a fake host for tests
```
**Secondary persona:** the iOS developer in the private repo. They expect versioned fixtures they can pin, and
server-side enums that match the app.

### 0B. Empathy narrative (grounded in the repo)
> I open `.claude/skills/add-hook/SKILL.md`. Its Quick Start is three steps: a module with `def register()` that
> calls `register_hook`, then `PLUGIN_MODULE=my_plugin.hooks:register`, done. In five minutes I have a hook. Then I
> need a table, a route, a nav entry. The design says `register(host)`, but `api/lifecycle.py:523` still calls
> `fn()` with no arguments. My tables need their own `alembic.ini` + `env.py` + `version_table` +
> `REQUIRES_CORE_REVISION` + a `plugin_<ns>_` prefix, and I copy `expense_reports` because nothing else exists. I
> name the frontend entry, the route prefix, the env prefix and the version table by hand, and get one of them
> wrong. My handler has no `**kwargs`. A minor host release adds a field, `run_hooks` swallows the `TypeError`
> (`utils/hooks.py:419`), and my plugin is silently dead. The status says `TypeError: ...` and nothing else.
> Half a day in, it works on my machine, and I discover the k8s job hardcodes `plugins/expense_reports/alembic.ini`.

### 0C. Competitive DX benchmark
Web search was not run; these are reference benchmarks from in-distribution knowledge.
```
COMPETITIVE DX BENCHMARK
=========================
Tool                                   | TTHW     | Notable DX choice                                  | Source
Home Assistant custom integration      | ~15 min  | manifest.json + scaffold script (script.scaffold)  | reference
Django reusable app                    | ~10 min  | `startapp` scaffold, per-app migrations, one migrate| reference
Backstage plugin                       | ~10 min  | `yarn new` generator, typed plugin API, dev harness| reference
Renfield hook-only plugin (today)      | ~5 min   | /add-hook Quick Start, 3 steps                      | .claude/skills/add-hook/SKILL.md
Renfield full plugin (plan as written) | 4–8 h    | copy expense_reports, 14 manual steps, no harness  | current plan
```
Target auto-decided: **Competitive tier ≤ 30 min** for a full plugin (route + table + nav), and **≤ 5 min** stays
for hook-only plugins (P5, the persona's tolerance).

### 0D. Magical moment
`bin/new_plugin.py hello` generates `plugins/hello/` + `src/frontend/src/plugins/hello/` from `plugins/_example`,
then `bin/plugin_migrate.py upgrade hello`. After a restart with `PLUGIN_MODULES=plugins.hello.plugin:register`,
`/api/plugins/hello/ping` answers, its table exists, and a "Hello" nav entry renders. Delivery vehicle: **B,
copy-paste command** (P5, lowest effort that reaches the tier).

### 0E. Mode
DX POLISH (autoplan override; enhancement of the existing hook system).

### Step 0.5 — dual voices (DX)
**CODEX SAYS (DX — developer experience challenge):** `[codex-unavailable: binary not found]`.

**CLAUDE SUBAGENT (DX — independent review)** `[subagent-only]`, 14 findings, of which 2 critical:
- **Getting started:** no scaffold, about 14 manual steps (**critical**); no local migration loop, and the k8s job is
  hardcoded per plugin (high).
- **API ergonomics:**
  - `fn(**kwargs)` dispatch breaks handlers on additive fields, and `run_hooks` hides it (**critical**);
  - one plugin identity spelled seven ways, with no namespace ownership (high);
  - global hooks used for per-namespace dispatch, with no timeout (high);
  - two registration paths, the host plus the global `register_hook` (medium).
- **Errors:**
  - load errors lack cause + fix + doc (high);
  - the naive async check rejects partials and wrappers, and would break an out-of-repo plugin (high);
  - `reason_code` is free text and invalid-extensions errors don't say which key (high);
  - contract skew is invisible to the phone → `426` + `min_supported` (high).
- **Docs and tests:**
  - no `FakeHost` / conformance harness (high);
  - fixture location, format and sync are undefined (high).
- **Upgrade:**
  - `HostAPI` lacks what `expense_reports` needs: a rate limiter, admin/permission checks, scheduled tasks, byte
    access with fallback (high);
  - no version-evolution rules or surface snapshot (high);
  - core `registry.ts` names plugins (medium).

**Primary-review position:** agrees with all 14.

**Cross-phase tension:** Phase 1 (UC2) says the substrate is **overbuilt for one consumer**, while DX says the
contract is **incomplete for that same consumer**. Both are true at once: the generic surface (6 events,
registry) is speculative, and the concrete needs (permissions, rate limit, scheduled tasks) are missing. UC2's
framing therefore becomes "a narrower but complete contract", and the fixes below apply to whichever size the user
picks.

```
DX DUAL VOICES — CONSENSUS TABLE:            [subagent-only]
═══════════════════════════════════════════════════════════════════════
  Dimension                            Primary  Subagent  Codex  Consensus
  ──────────────────────────────────── ──────── ───────── ────── ─────────
  1. Getting started < 5 min?          no       no        N/A    CONFIRMED (4–8 h → target ≤ 30 min)
  2. API/CLI naming guessable?         no       no        N/A    CONFIRMED (manifest derives all names)
  3. Error messages actionable?        no       no        N/A    CONFIRMED (PluginLoadError code/cause/fix/doc)
  4. Docs findable & complete?         no       no        N/A    CONFIRMED (docs/PLUGINS.md + _example first)
  5. Upgrade path safe?                no       no        N/A    CONFIRMED (payload dataclasses + surface snapshot)
  6. Dev environment friction-free?    no       no        N/A    CONFIRMED (plugin_migrate CLI + FakeHost)
═══════════════════════════════════════════════════════════════════════
Consensus 6/6 confirmed. Codex N/A.
```

### 0F. Developer journey map (after fixes)
```
STAGE           | DEVELOPER DOES                                   | FRICTION POINTS (before)                    | STATUS
----------------|--------------------------------------------------|---------------------------------------------|--------
1. Discover     | reads docs/PLUGINS.md from CLAUDE.md + /add-hook | no plugin doc; add-hook covers hooks only   | fixed (doc + skill link)
2. Evaluate     | decides hook-only vs full plugin                 | no decision guide                           | fixed (doc section)
3. Install      | bin/new_plugin.py <ns>                           | copy expense_reports by hand                | fixed (scaffold from _example)
4. Hello World  | plugin_migrate upgrade <ns>; restart; ping route | 14 steps, hardcoded job path                | fixed (≤ 30 min target)
5. Integrate    | HostAPI members, payload dataclasses, extensions | kwargs breakage; missing host members       | fixed (dataclasses, host.captures.register_extension_handler, members)
6. Debug        | reads internal.system_health / ops_alert         | "TypeError: msg" only                       | fixed (PluginLoadError)
7. Upgrade      | bumps REQUIRES_HOST_CONTRACT                     | no rules, no changelog                      | fixed (snapshot test + changelog + deprecation rule)
8. Scale        | 2nd–nth plugin, namespace collisions             | any plugin can claim any namespace          | fixed (host binds namespace from manifest)
9. Migrate      | deactivate/remove plugin, keep data              | documented in design                        | ok
```

### 0G. First-time developer roleplay
```
FIRST-TIME DEVELOPER REPORT (plan as written)
Persona: maintainer / coding agent adding plugin #2
T+0:00  Reads /add-hook Quick Start — hooks only, zero-arg register(). No mention of HostAPI.
T+0:10  Finds services/plugin_host/; register(host) documented only in the design doc.
T+0:40  Copies plugins/expense_reports/; renames 7 identifiers by hand; misses the alembic version_table name.
T+1:30  Plugin env.py refuses: REQUIRES_CORE_REVISION message without the current revision or command.
T+2:30  k8s job path hardcoded to expense_reports; writes a second manifest.
T+3:30  Handler without **kwargs silently dead after a host minor bump in CI; status shows "TypeError".
T+4:00  Gives up on tests: no fake host; writes an integration test against a live DB.
```
All 7 confusion points are addressed by the tasks below (auto-decided: "all of them", P1).

### Passes 1–8 (0-10, before → after)
- **Pass 1, Getting started: 2 → 8.**
  - Fix: `plugins/_example` (tested), `bin/new_plugin.py`, `bin/plugin_migrate.py`, a quickstart in
    `docs/PLUGINS.md` written first in 1.5-0.
  - Why not 10: no interactive playground (N/A for an in-repo plugin).
- **Pass 2, API: 4 → 8.**
  - A `PluginManifest` (name, version, `requires_host`, `requires_core_revision`, migrations path, reason codes)
    derives the route prefix, settings prefix, table prefix, version table and feature key.
  - Every new event gets one frozen payload dataclass; additive fields are optional.
  - `host.hooks.on()` is the single registration path, so the owner is known.
  - `host.captures.register_extension_handler(ns, fn)` with its own timeout.
  - Zero-arg `register` is detected via `inspect.signature`.
- **Pass 3, Errors: 3 → 8.**
  - `PluginLoadError(code, cause, fix, doc_anchor)` in `_plugin_status`, `internal.system_health` and `ops_alert`.
    Codes: `contract_major_mismatch`, `migration_not_at_head` (DB rev, code rev, exact command),
    `core_revision_too_old`, `sync_handler` (handler qualname + "declare async def"), `namespace_conflict`.
  - Upload errors: `failed/invalid_extensions` returns `{namespace, key, code}`.
  - Declared reason codes are localised in the app.
  - Mobile contract: the server echoes `X-Mobile-Capture-Contract` + `min_supported`; below the minimum → `426
    upgrade_required` → app state "App aktualisieren".
- **Pass 4, Docs: 3 → 8.**
  - `docs/PLUGINS.md`: hook-only vs full plugin, quickstart, HostAPI reference, migrations, frontend registry,
    testing, contract changelog.
  - `/add-hook` skill links it.
  - `contracts/mobile-capture/v1/` holds JSON Schemas generated from the Pydantic models, plus request/response
    examples.
- **Pass 5, Upgrade: 2 → 8.**
  - Surface snapshot test of `HostAPI` + payload fields that fails unless `HOST_CONTRACT_VERSION` changed.
  - Rule: deprecate for ≥ 1 minor, remove only at a major.
  - Async-only registration warns for one release, then rejects. It unwraps `functools.partial`/wrappers and checks
    async `__call__`. Existing in-repo and staged out-of-repo handlers are audited first.
  - The iOS repo pins a fixture tag.
- **Pass 6, Dev environment: 3 → 8.**
  - `plugin_host.testing.FakeHost` + pytest fixtures.
  - A conformance test loads `_example` in the core suite.
  - One generic migration job with a `PLUGIN` env var, driven by the manifest.
  - Local `docker exec … bin/plugin_migrate.py`.
- **Pass 7, Community: 5 → 6.** N/A beyond in-repo contributors. The generator writes the `registry.ts` entry
  (accepted as a documented core touchpoint).
- **Pass 8, DX measurement: 1 → 6.** The conformance test's run time and the scaffold-to-green time are recorded
  when 1.5-0 lands (the boomerang for `/devex-review`).

```
+====================================================================+
|              DX PLAN REVIEW — SCORECARD                             |
+====================================================================+
| Dimension            | Score  | Prior  | Trend  |
|----------------------|--------|--------|--------|
| Getting Started      | 8/10   | 2/10   | ↑      |
| API/CLI/SDK          | 8/10   | 4/10   | ↑      |
| Error Messages       | 8/10   | 3/10   | ↑      |
| Documentation        | 8/10   | 3/10   | ↑      |
| Upgrade Path         | 8/10   | 2/10   | ↑      |
| Dev Environment      | 8/10   | 3/10   | ↑      |
| Community            | 6/10   | 5/10   | ↑      |
| DX Measurement       | 6/10   | 1/10   | ↑      |
+--------------------------------------------------------------------+
| TTHW                 | ≤30 min| 4–8 h  | ↑      |
| Competitive Rank     | Competitive (after) / Red Flag (before)     |
| Magical Moment       | designed via copy-paste scaffold command    |
| Product Type         | Platform/Library (+ API contract)           |
| Mode                 | POLISH                                      |
| Overall DX           | 7.5/10 | 2.9/10 | ↑      |
+====================================================================+
| DX PRINCIPLE COVERAGE                                               |
| Zero Friction      | covered (scaffold + migrate CLI)               |
| Learn by Doing     | covered (_example + FakeHost)                  |
| Fight Uncertainty  | covered (PluginLoadError, 426, reason codes)   |
| Opinionated + Escape Hatches | covered (manifest defaults; host.experimental) |
| Code in Context    | covered (expense_reports is the real example)  |
| Magical Moments    | covered (bin/new_plugin.py hello)              |
+====================================================================+
```

```
DX IMPLEMENTATION CHECKLIST
============================
[ ] Time to hello-world plugin ≤ 30 min (measured when 1.5-0 lands)
[ ] One scaffold command (bin/new_plugin.py) + one migrate command (bin/plugin_migrate.py)
[ ] First run produces a mounted route, a migrated table, a nav entry
[ ] Every PluginLoadError has code + cause + fix + doc anchor
[ ] Plugin names derived from one PluginManifest
[ ] Payload dataclasses for every new hook event; additive fields optional
[ ] docs/PLUGINS.md quickstart copy-paste complete; /add-hook links it
[ ] HOST_CONTRACT_VERSION surface snapshot test + changelog + deprecation rule
[ ] FakeHost + conformance test in the core suite
[ ] contracts/mobile-capture/v1 schemas generated + validated against live responses; 426 min_supported
```

### NOT in scope (DX)
- A runtime/remote plugin loader: rejected by the design (CSP, skew).
- A public plugin marketplace or third-party packaging: in-repo only (R5-1).
- An interactive playground.

### What already exists (DX)
- `.claude/skills/add-hook/SKILL.md` + `references/hook-events.md`: the hook-only quickstart.
- `utils/hooks.py`:
  - `HOOK_EVENTS` whitelist (a typo raises);
  - `get_hook_handlers` priorities;
  - `run_hooks_with_errors`.
- `api/lifecycle.py`: `get_plugin_status()` / `failed_plugins()`.
- `alembic/env.py`: `PLUGIN_METADATA_MODULES`.
- `tests/backend/test_document_worker_isolation.py`: the pattern for the import-isolation test.

> **Phase 2.5 complete.** DX overall: 2.9/10 → 7.5/10. TTHW: 4–8 h → ≤ 30 min. Codex: unavailable. Claude subagent: 14 findings. Consensus: 6/6 confirmed, 0 disagreements (1 cross-phase tension with UC2 → gate). Passing to Phase 3 (Eng).

## Phase 3 — Eng review (FULL_REVIEW; runs on the plan as amended by Phases 1–2.5)

### Step 0 — Scope challenge (with code)
1. **Existing code per sub-problem.** See Phase 1 §0B. New facts from reading the code:
   - `services/task_queue.py` already has a consumer-group class (`ensure_group` :177, `read_one` → `xreadgroup`
     :209-213, `ack` :250, `reclaim_stale` → `xautoclaim` :254-265, `pending_count` :337). The push consumer must
     reuse it instead of hand-rolling `XREADGROUP`.
   - `api/lifecycle.py::_schedule_user_events_subscriber` (:292-310) is the precedent for a long-lived API-pod task.
   - **`services/folder_ingest_paperless.py::make_paperless_leg`: its post-consume PATCH (:616-634) carries only
     `created_date` + content.** `_settle_from_outcome` (:122-136) says explicitly that this PATCH cannot run on the
     re-poll path. **Custom fields are not "already applied"**, contrary to the design doc; they are new code. On a
     slow Paperless (the re-poll path), a mirror bolted onto the upload-time PATCH would silently never happen.
   - `document_search.search_documents` fuses three lists (`_rrf([name_ids, fact_ids, chunk_ids])` :199), so a
     capture-details signal is a fourth list through the same `_visible_ids` gate (:151).
   - `knowledge_tool` builds `context_parts` + a FAKTEN block (:210-278), which is where the fenced details block goes.
   - `documents.split_from_document_id` is indexed (:585-590), so split aggregation is one indexed join.
   - `documents.file_hash` (:529) is the sha256 for the ZIP integrity check.
2. **Minimum set:** M1–M3. Everything else extends it.
3. **Complexity check:** triggers (≫ 8 files, ≫ 2 services). Autoplan rule: never reduce scope in Eng (P2). Scope
   questions live in UC1–UC4.
4. **Search check:** web search not run, in-distribution only.
   - [Layer 1] Redis stream consumer groups + `XAUTOCLAIM` for at-least-once delivery: correct, already in the repo.
   - [Layer 1] APNs token (JWT) auth over HTTP/2: correct for the relay.
   - [Layer 1] Alembic multiple environments with a custom `version_table`: supported.
   - [Layer 3] A streamed STORED ZIP from async FastAPI: stdlib `zipfile` writes to unseekable streams (data
     descriptors), but it is sync, so it needs a bounded thread/queue bridge or a small well-scoped dependency. Per
     the reuse ladder, stdlib + bridge first.
5. **TODOS cross-reference:** no overlap. New deferred items are collected at the gate (TODOS.md is not written in
   this run).
6. **Completeness:** the plan's test list is broad; gaps are listed in the test diagram below.
7. **Distribution check:**
   - iOS/macOS: fastlane lanes on the operator Mac (planned).
   - Plugin migration: same backend image (planned).
   - **Push relay: the plan has a private repo + deployment, but no image build/publish pipeline**, no registry
     target, no versioning → added as an M4 task.

### Architecture (new components and their relationships)
```
                                   ┌──────────── renfield-ios (private repo) ────────────┐
                                   │ Shell/AccountSwitcher · SectionRegistry              │
                                   │ RenfieldCore(APIClient+pin, CredentialStore, Push)   │
                                   │ CaptureFeature · DeskScanKit · OutboxEngine · History│
                                   └──────┬──────────────────────────────┬────────────────┘
                   contracts/mobile-capture/v1 (schemas+examples, pinned tag)   │ APNs token
                                          │ HTTPS (LAN, pinned)                   ▼
┌───────────────────────── backend image (this repo) ───────────────────────────────┐   ┌── push relay (private) ──┐
│ api/routes/mobile_capture.py ──► services/mobile_capture/{ingest,pairing,status}.py│   │ /send template enum      │
│      │  deps: ingest_credentials(ROUTES, status) · one_time_code_store · limiter   │   │ per-instance credential  │
│      ▼                                                                             │   │ no persistence → APNs    │
│ folder_ingest.ingest_document ──► document worker (PDF-Split → OCR → Schicht-A)    │   └───────────▲──────────────┘
│      │ after-commit emitters (rag_service ingest-complete / terminal / proposals)  │               │
│      ▼                                                                             │               │
│ Redis stream renfield:tasks:mobilepush ──► services/mobile_push.py (task_queue      │───────────────┘
│      consumer class, supervised lifecycle task, conditional notified_state)        │
│ capture_details ──► document_search (4th RRF list) · knowledge_tool (fenced block) │
│ capture_details ──► NEW paperless_details_mirror (own reconciler pass by paperless │
│                     id, mirrored_version < version) ──► mcp.paperless.update_document│
│ scheduled_tasks: mobile_capture_stuck · mobile_build_age                            │
│ services/plugin_host/{host,manifest,payloads,errors,migrations,testing}.py         │
│      ▲ register(host)             ▲ run via host.hooks.on / register_extension_handler│
│ plugins/expense_reports (own Base, own alembic env, routes /api/plugins/expense-reports)│
└────────────────────────────────────────────────────────────────────────────────────┘
Coupling added: core → relay (HTTP, best-effort); core → plugin_host (interface only); plugin → plugin_host only.
```

### Section 1 — Architecture findings
- **[P1] (confidence 9/10) `services/folder_ingest_paperless.py:122-136, 616-634`: the custom-field mirror has no
  code path today.**
  - The upload-time PATCH only exists on the fresh-upload branch.
  - A PATCH from the app after filing needs a PATCH-by-`paperless_document_id` path.
  - **Fix:** a separate idempotent mirror pass. `capture_details.version` vs `mirrored_version`; runs when
    `paperless_document_id` is known; existing fields only; retried by the existing reconciler schedule.
  - Tests: fresh-upload path, re-poll path, app PATCH after filing, missing field.
  - The design doc's sentence "already applies a deferred post-consume PATCH" is wrong for custom fields. Noted
    here; the design doc is not edited in this run.
- **[P2] (confidence 9/10) `services/task_queue.py:158-352`:** reuse the existing stream consumer class for
  `mobilepush` (DRY); the supervision wrapper follows `_schedule_user_events_subscriber` (`api/lifecycle.py:292`).
- **[P2] (confidence 8/10):** module split. One `services/mobile_capture.py` would hold ingest, pairing, status
  aggregation and PATCH. **Fix:** a package `services/mobile_capture/` with `ingest.py`, `pairing.py`, `status.py`,
  `edits.py`, plus a separate `services/mobile_push.py` (explicit over clever).
- **[P2] (confidence 8/10):** relay distribution undefined. **Fix:** image build + push to the instance registry,
  version tag, rollout step in the private runbook; the backend relay client sends `X-Relay-Contract` and tolerates
  `426`.
- **[P3] (confidence 7/10):** `api/lifecycle.py` is touched by M4 (push consumer) and 1.5-0 (plugin loader). This is
  a merge-conflict hotspot (see Parallelization).

### Section 2 — Code quality findings
- **DRY:** carried over from earlier phases: ROUTES tuple, one-time code store, credential/QR UI, stream consumer.
  New this phase: **status vocabulary defined once**, in the contract schema (Phase 2.5), imported by the backend
  enum and generated into the i18n keys.
- **Error handling:** `run_hooks` catch-all (`utils/hooks.py:419`) must not be used for `capture_extensions_apply`;
  use `run_hooks_with_errors` / the per-namespace registry.
- **Over-engineering:** a `HostAPI` with 8 members + 6 events for one consumer (UC2).
- **Under-engineering:** the ZIP writer is described as "streaming, no temp file" with no sync/async bridge design.
  Add a bounded queue + thread writer, back-pressure, and abort on client disconnect, with a test.
- **Stale diagrams:** none in touched files. New inline ASCII diagrams are required in:
  - `services/mobile_capture/status.py` (the state aggregation);
  - `services/mobile_push.py` (the stream → conditional update → relay → ack);
  - `plugins/expense_reports/service.py` (the report state machine);
  - `plugin_host/migrations.py` (the job order).

### Section 3 — Test review
Framework: pytest (backend, `tests/backend/`, run on the build box), Vitest + RTL + MSW (`tests/frontend/react/`),
XCTest (iOS repo).
```
CODE PATHS                                                         USER FLOWS
[+] api/routes/mobile_capture.py  POST /document                    [+] Pair a phone (web + app)
  ├── [PLANNED ★★★] flag 503 / 401 / worker 503 / size / 4-state    ├── [PLANNED ★★] self-service pair, non-admin for others refused
  ├── [PLANNED ★★★] capture_id_conflict                             ├── [GAP] [→E2E] same-device pairing link (iPhone Safari)
  ├── [GAP]  ledger on DUPLICATE path + crash-between retry  (P1)   ├── [GAP] code expired mid-step → new code
  ├── [GAP]  truncated PDF (no %%EOF)                               └── [GAP] Redis down → 503 pairing_unavailable
  └── [GAP]  uniform 401 for revoked/wrong-route/wrong-secret      [+] Capture → history
[+] services/mobile_capture/status.py                                ├── [PLANNED ★★] device E2E offline → home → processed
  ├── [PLANNED ★★] split children aggregation                       ├── [GAP] [→E2E] revoked device: bytes kept, re-pair and send
  ├── [GAP]  split_pending counts as processing; archived parent    ├── [GAP] TLS pin mismatch state
  └── [GAP]  superseded child (document dedupe) excluded            └── [GAP] build-expired banner / 426 upgrade_required
[+] PATCH /captures/{id}                                            [+] Edit after processing
  ├── [PLANNED ★★★] foreign 404 / 412 / assignment_invalid          ├── [PLANNED ★★] details → Paperless updated (device E2E)
  └── [GAP]  PATCH after Paperless filing → mirror pass (P1)        └── [GAP] 409 locked report copy
[+] paperless details mirror (NEW)                                  [+] Push
  ├── [GAP]  fresh-upload path        ├── [GAP] re-poll path (P1)   ├── [PLANNED ★★] redelivery → no double push
  └── [GAP]  missing field reported, never auto-created             ├── [GAP] consumer crash → supervised restart
[+] services/mobile_push.py                                         └── [GAP] 50 stuck captures → one grouped alert
  ├── [PLANNED ★★★] conditional notified_state, ack after accept   [+] Reisekosten (plugin)
  ├── [GAP]  relay 429 / 5xx backoff; 410 deletes token             ├── [PLANNED ★★] xidra E2E trip → submit → settle → export
  └── [GAP]  pending entry older than bound → dropped + counter     ├── [GAP] [→E2E] ZIP with hidden doc + corrupt fallback byte
[+] document_search 4th signal / knowledge_tool fenced block        └── [GAP] 413 copy + category split hint
  ├── [PLANNED ★★] no leak across owners                          [+] Plugin host
  └── [GAP] [→EVAL] injection strings in details never steer agent  ├── [PLANNED ★★★] import isolation, deactivation, not-loaded
[+] plugin_host                                                     ├── [GAP] payload dataclass additive field keeps old handler alive
  ├── [PLANNED ★★] contract major mismatch, async-only             ├── [GAP] migration_not_at_head message + no routes mounted
  ├── [GAP]  partial/wrapper handlers accepted                      └── [GAP] _example conformance test in core suite
  ├── [GAP]  namespace_conflict                                   [+] Per diems (1.5b)
  └── [GAP]  surface snapshot fails without version bump            ├── [PLANNED ★★★] golden files incl. DST, rate boundary, unreviewed
[+] ZIP writer                                                      └── [GAP] checksum change after review re-closes gate (listed) ✓ planned
  ├── [GAP]  client disconnect aborts thread writer, no leak
  └── [GAP]  sha256 mismatch → "Datei beschädigt" line (P1)
[+] scheduled tasks mobile_capture_stuck / build_age
  └── [GAP]  restart-safe dedup via stuck_alerted_at
LLM integration: [GAP] [→EVAL] Phase 5 Schicht-A receipt changes → existing Schicht-A eval + phantom-obligation set
                 [GAP] [→EVAL] fenced capture-details block → agent prompt-injection regression case
COVERAGE (plan as written): ~19/49 paths planned (39%) | code paths 13/31 | user flows 6/18
QUALITY of planned: ★★★ 7  ★★ 11  ★ 1 | GAPS: 30 (5 E2E, 2 eval) → all added as test tasks below
```
**Regressions (iron rule):** four existing behaviours change for existing callers, so each gets a regression test
(CRITICAL, auto-added):
1. `register_hook` async-only check → an existing handler still registers (in-repo `ha_glue` + staged plugin
   signatures).
2. The review-flow source allowlist → the default `folder_ingest` is byte-identical.
3. `/api/config/features` gains `plugins` → the existing fields are byte-identical.
4. `alembic/env.py` `include_object` + extracted bootstrap helper → core autogenerate emits no drop, and the
   upgrade chain is unchanged.

Test plan artifact:
`~/.gstack/projects/ebongard-renfield/evdb-worktree-agent-a78527ccc1adfd1ac-eng-review-test-plan-20260914-165000.md`.

### Section 4 — Performance
- **Per-request bcrypt:** every app call (health, captures, lookups, PATCH, device) costs one bcrypt round
  (~150ms, off-loop via `asyncio.to_thread`, `ingest_credentials.py:51-55`).
  - An app open with 3 accounts × 3 calls ≈ 9 rounds ≈ 1.3 s of thread CPU. Fine at household/SMB scale.
  - **Fix (P3):** the app calls `/health` only on a path change, and lookups only on foreground-at-home. Do not add
    a verified-token cache (it would weaken revocation).
- **`last_authenticated_at` throttled commit (:256-260):** runs on the request session, not an issue.
- **Status aggregation:** one indexed join on `split_from_document_id`. **N+1 risk** if the route loops per capture
  → a batch query for `?ids=` (≤ 100 ids cap) (P2).
- **ZIP:** the pre-flight size must come from file stat / Paperless metadata and never read the bytes. The thread
  bridge queue is bounded (e.g. 4 chunks) so memory stays flat (P2, test planned).
- **Push consumer:** one blocking read per API replica. A consumer group means N replicas share load, which is fine.

### Failure modes (Phase 3, per new codepath)
```
CODEPATH                    | REALISTIC FAILURE                                | TEST? | HANDLED? | USER SEES           | CRITICAL?
----------------------------|--------------------------------------------------|-------|----------|---------------------|----------
POST /document ledger       | crash after create, before ledger; dup path      | added | added    | status restored     | was CRITICAL → task
paperless details mirror    | re-poll path never PATCHes custom fields         | added | added    | fields appear later | was CRITICAL (silent) → task
push consumer               | task dies                                        | added | added    | late push           | no
ZIP fallback bytes          | MCP truncation / checksum mismatch               | added | added    | "Datei beschädigt"  | was CRITICAL → task
ZIP writer                  | client disconnect leaves thread writing          | added | added    | —                   | no
extensions dispatch         | handler hang / raise hidden by run_hooks         | added | added    | needs_review        | no
plugin payload evolution    | additive kwarg → TypeError swallowed             | added | added    | load error visible  | was CRITICAL (silent) → task
status aggregation          | superseded/split_archived child miscounted       | added | added    | correct pill        | no
pairing consume             | Redis down read as invalid code                  | added | added    | 503 message         | no
stuck detector              | alert flood on worker stall                      | added | added    | one alert           | no
```
**Critical gaps found in this phase: 2 new** (the Paperless mirror on the re-poll path, and plugin payload
evolution) plus 2 carried over from Phase 1. All four are now tasks, so **0 remain unaddressed in the amended plan**.

### Worktree parallelization
| Step | Modules touched | Depends on |
|---|---|---|
| C: contract schemas + fixtures | `contracts/`, `src/backend/api/routes/` (models) | — |
| M1a: credentials, pairing, ledger | `src/backend/services/`, `alembic/versions/`, `api/routes/` | C |
| M1b: search/KB/Paperless mirror | `src/backend/services/` (document_search, knowledge_tool, paperless) | M1a (tables) |
| M1c: "Mobile Geräte" web | `src/frontend/src/`, `tests/frontend/react/` | C (MSW mocks) |
| M2: app v1 | private iOS repo | C |
| M4-relay | private relay repo | relay `/send` contract |
| M4-backend push | `src/backend/services/`, `api/lifecycle.py` | M1a |
| 1.5-0 substrate | `services/plugin_host/`, `utils/hooks.py`, `api/lifecycle.py`, `alembic/env.py`, `src/frontend/src/plugins/` | M1a |
| 1.5a/b plugin | `src/backend/plugins/`, `src/frontend/src/plugins/` | 1.5-0 |
| 1.6 desk scan | private iOS repo | M4 (+ UC3) |

- **Lane A:** C → M1a → M1b (sequential, shared `services/`).
- **Lane B:** M1c (parallel after C, mocks).
- **Lane C:** M2 (separate repo, parallel after C).
- **Lane D:** M4-relay (separate repo, parallel anytime).
- **Lane E:** M4-backend push → 1.5-0 → 1.5a/b (sequential; both touch `api/lifecycle.py`).
- **Lane F:** 1.6 (iOS repo, after M4).

**Execution:** launch A + B + C + D in parallel worktrees, merge A, then E; F follows M4.
**Conflict flag:** M4-backend and 1.5-0 both edit `api/lifecycle.py`. Keep them sequential in Lane E.

### NOT in scope (eng)
- ZIP64 / exports > 4 GB: bounded by `EXPENSE_REPORTS_EXPORT_ZIP_MAX_MB`.
- More than one device per credential: by construction one.
- A verified-token cache to save bcrypt: weakens revocation.
- Household auth-off pairing tests: Phase 2.
- Phases 3–8.
- Editing the design doc's wrong "already applies" sentence: this run is plan-file-only. The finding is recorded;
  the docs sweep should fix it.

### What already exists (eng)
Phase 1 §0B, plus:
- the `task_queue` stream consumer class;
- the `_schedule_user_events_subscriber` lifecycle pattern;
- the `document_search` RRF + single visibility gate;
- the `knowledge_tool` context/FAKTEN builder;
- `split_from_document_id` index;
- `documents.file_hash`;
- `run_hooks_with_errors`;
- `test_document_worker_isolation.py` (isolation-test pattern).

**Parallel builds found:** a hand-rolled stream consumer (avoid), and a Paperless custom-field mirror that does not
exist yet (must be built, not "reused").

### Step 0.5 — dual voices (eng)
**CODEX SAYS (eng — architecture challenge):** `[codex-unavailable: binary not found]`.

**CLAUDE SUBAGENT (eng — independent review)** `[subagent-only]`: 12 main findings + P3 list. Each claim was
re-verified against the code before it was accepted (quoted lines checked):
- **[P1] Cross-user duplicate links one user's capture to another user's document.** `folder_ingest.py:128-140`
  dedups on `Document.file_hash == file_hash, Document.knowledge_base_id == kb_id` and returns
  `IngestResult(DUPLICATE, document_id=existing.id)` (:279-281). With one shared mobile-capture KB, a second user's
  identical bytes resolve to the first user's document, so the ledger row lets them read its status and PATCH its
  details. **Verified.**
- **[P1] The shared KB owner sees everyone's tier-0 captures.** `circle_sql.py:338-347`
  `document_chunks_circles_filter` passes `owner_table_alias=kb_alias`, so the owner branch is the KB owner. The
  design's "the KB is the instance's mobile-capture KB" makes one person see every capture. **Verified.**
- **[P1] Plugin hooks fired in worker pods never run.** No worker entrypoint loads `PLUGIN_MODULES` (no match in
  `workers/*.py`; only `api/lifecycle.py:535-553`). `capture_state_changed` (ingest-complete in the worker),
  `document_deleted` (also called by retention/split paths) and `paperless_filing_metadata` (filing leg) would be
  silent no-ops. **Verified.**
- **[P1] Pairing brute force / lockout.** The typed manual code (R6) needs defined entropy, and the limiter takes the
  left-most `X-Forwarded-For` when `TRUSTED_PROXIES` is empty (`services/api_rate_limiter.py:66-80`), so it is
  spoofable or one shared bucket. **Verified.**
- **[P2] Push retry is dead by construction.** Setting `notified_state` before the relay call makes redelivery skip.
  Fix: lease (`notify_attempt_at`) → send → set `notified_state` on 2xx.
- **[P2] Ledger and upload are not atomic.** `ingest_document` commits internally (:277 and others) and enqueues. The
  worker can finish before the ledger row exists, and concurrent retries race `capture_id_conflict`. Fix: insert the
  ledger row FIRST (`ON CONFLICT (client_id, capture_id)`, store sha256), set `document_id` after; the consumer
  re-derives state from the document. **Verified.**
- **[P2] Split aggregation holes.** A duplicate child is deliberately not lineage-stamped (`pdf_splitter.py:332-336`),
  the parent ends `split_archived`, and children inherit `parent.source` (:275), so they are `mobile_capture`, not
  `pdf_split` as the design says. Fix: store child ids from the split plan on the ledger; map `split_archived` and
  `split_review` explicitly. **Verified.**
- **[P2] No single worker-terminal-failure seam.** `failed` is written in several places. Fix: one `mark_failed`
  helper that emits after commit.
- **[P2] The Paperless custom-field mirror is weaker than the design.** The PATCH is once, best-effort, only on
  success; the 60/min rate limit and Paperless duplicates skip it; field name→id needs new MCP work. Fix: durable
  `paperless_metadata_dirty` + its own reconciler pass with backoff. (This is the same root as the primary [P1]
  above.)
- **[P2] Plugin job vs rolling deploy.** Pods started before the plugin job disagree on routes and features. Fix: the
  deploy script waits for the job; the plugin re-checks and mounts idempotently; alert on mixed replicas.
- **[P2] ZIP byte sources.**
  - `delete_document` removes the recovery copy (`rag_service.py:980-983`, verified).
  - The MCP fallback truncates and holds the whole file in memory.
  - The size is unknown before the 413 pre-flight.

  Fix: a streaming Paperless download outside `execute_tool` (or local copies only); store the byte size on the item
  snapshot.
- **[P2] Hook contract gaps.** No timeout, no coroutine check, a zero-arg loader. The fix aligns with Phase 2.5.
- **[P3] items:**
  - the bootstrap SQL hardcodes `alembic_version` (the helper needs a parameter);
  - a sweeper for pending credential rows;
  - bytes are held in RAM on bursty home arrival, so a 429 → outbox retry, never failed;
  - APNs 410 delete must match the token value (race with a refresh);
  - the per-diem day boundary for a foreign timezone → golden test + tax-advisor checklist item;
  - test gaps: 2 replicas, hooks fired from the worker, cross-owner duplicate, KB-owner visibility, a split with a
    duplicate child, rate-limited PATCH, `/pair` brute-force burn.

**Primary position:** agrees with every finding. The cross-user duplicate and KB-owner visibility are **security
defects in the design as written, not preferences**. They are fixed as plan tasks (per-user mobile-capture KB via
the existing credential `kb_name` override + cross-owner duplicate never links a document). They are not queued as
user challenges, because no R2–R6 decision covers the KB layout, but they are **flagged at the gate**.

```
ENG DUAL VOICES — CONSENSUS TABLE:            [subagent-only]
═══════════════════════════════════════════════════════════════
  Dimension                           Primary  Subagent  Codex  Consensus
  ──────────────────────────────────── ──────── ───────── ────── ─────────
  1. Architecture sound?               partly   partly    N/A    CONFIRMED (mirror path missing; worker hooks dead)
  2. Test coverage sufficient?         no       no        N/A    CONFIRMED (30+ gaps; 2 replicas, worker hooks, cross-owner)
  3. Performance risks addressed?      partly   partly    N/A    CONFIRMED (bcrypt per call ok; ZIP memory; burst uploads)
  4. Security threats covered?         no       no        N/A    CONFIRMED (cross-user dup, KB-owner visibility, pair brute force)
  5. Error paths handled?              no       no        N/A    CONFIRMED (push retry dead, non-atomic ledger, hook no-ops)
  6. Deployment risk manageable?       partly   partly    N/A    CONFIRMED (plugin job vs rolling deploy; relay pipeline)
═══════════════════════════════════════════════════════════════
Consensus 6/6 confirmed. Codex N/A (not installed).
```

### Failure modes — additions from the eng voice
```
CODEPATH                    | REALISTIC FAILURE                                     | TEST? | HANDLED? | USER SEES                | CRITICAL?
----------------------------|-------------------------------------------------------|-------|----------|--------------------------|----------
dedup on shared KB          | user B's capture links to user A's document           | added | added    | —                        | CRITICAL (silent exposure) → task
circle filter, shared KB    | KB owner reads all tier-0 captures                    | added | added    | —                        | CRITICAL (silent exposure) → task
hooks fired in workers      | expense snapshot / push observer / filing metadata no-op | added | added | nothing                  | CRITICAL (silent) → task
push consumer ordering      | relay down → redelivery skips → push lost             | added | added    | late or no push          | no (history truth)
ledger vs worker race       | processed emitted before ledger row exists            | added | added    | status stuck "In Arbeit" | was silent → task
split duplicate child       | child not counted → capture "processed" too early     | added | added    | wrong bucket             | no
plugin job vs rollout       | replicas disagree on routes/features                  | added | added    | intermittent 404         | no
ZIP bytes after delete      | recovery copy removed with the document               | added | added    | "nicht verfügbar"        | no
```
**Critical gaps in the amended plan:** 7 identified across phases (2 in Phase 1, 2 in Phase 3 primary, 3 from the
eng voice). All seven have tasks; **0 unaddressed**.

### Completion summary (eng)
- **Step 0: Scope Challenge.** Scope accepted as-is; autoplan never reduces scope in Eng. Complexity smell recorded;
  scope questions go to UC1–UC4.
- **Architecture review:** 5 primary + 4 voice issues (2 P1 security, 1 P1 worker hooks).
- **Code quality review:** 5 issues.
- **Test review:** diagram produced; 30 gaps + 8 voice gaps; 4 regression tests (CRITICAL).
- **Performance review:** 4 issues.
- **NOT in scope:** written. **What already exists:** written.
- **TODOS.md updates:** 0 new from eng (all findings are plan tasks); the list from earlier phases is at the gate.
- **Failure modes:** 7 critical gaps flagged, all with tasks.
- **Outside voice:** ran (Claude subagent; Codex unavailable).
- **Parallelization:** 6 lanes: 4 parallel (A, B, C, D) / 2 sequential (E, F).
- **Lake Score:** 21/22 recommendations chose the complete option.

> **Phase 3 complete.** Codex: unavailable. Claude subagent: 12 findings + 8 P3/test gaps (4 P1). Consensus: 6/6 confirmed, 0 disagreements. Passing to Phase 4 (Final Gate).

### From Phase 3 (Eng) — tasks
- [ ] **M1 (P1, security)** mobile-capture sphere per person:
  - pairing mints the credential with a per-user KB (`kb_name` override on the credential row);
  - a duplicate whose document owner ≠ credential owner returns `duplicate` with NO document reference, and details/links key on `(client_id, capture_id)`;
  - tests: two users, same bytes; KB-owner-as-other-user cannot see a tier-0 capture.
- [ ] **M1 (P1)** insert the ledger row first (`ON CONFLICT (client_id, capture_id)`, sha256 stored) → then `ingest_document` → set `document_id`:
  - the consumer/aggregator re-derives state from the document;
  - tests: worker finishes before `document_id` is set; concurrent retries with the same capture_id.
- [ ] **M1 (P1)** pairing code entropy:
  - QR code ≥ 128 bits; typed code ≥ 10 base32 chars;
  - per-code attempt counter in Redis burns the code after N failures; TTL ≤ 5 min;
  - M3 checklist: `TRUSTED_PROXIES` set to the ingress network on the business instance;
  - tests: brute-force burn, replay.
- [ ] **M1 (P1)** Paperless details mirror as its own durable pass:
  - `capture_details.version` vs `mirrored_version` (or `paperless_metadata_dirty`);
  - runs when `paperless_document_id` is known; field name→id resolution via MCP; existing fields only;
  - backoff on the 60/min rate limit;
  - tests: fresh upload, re-poll path, Paperless duplicate, rate-limited, app PATCH after filing.
- [ ] **1.5-0 (P1)** hooks that fire in worker pods: load `PLUGIN_MODULES` in every worker entrypoint via the same loader, OR the worker publishes to a Redis stream consumed by API pods (pick one, document it):
  - tests: each of `capture_state_changed`, `document_deleted`, `paperless_filing_metadata` fired from the worker process reaches a fake plugin.
- [ ] **M1 (P2)** status aggregation from stored child ids (from the split plan), explicit mapping for `split_archived` / `split_review`:
  - test: split with a duplicate child;
  - docs sweep: children keep `source=mobile_capture` (the design says `pdf_split`).
- [ ] **M4 (P2)** push delivery order: lease `notify_attempt_at` → relay → set `notified_state` on 2xx → ack. Tests:
  - relay down then up delivers once;
  - two API replicas in the group;
  - an APNs 410 deletes only a matching token value.
- [ ] **M4 (P2)** one `mark_failed` helper used by every terminal-failure writer (`rag_service`, document worker, poison-pill quarantine), emitting after commit
- [ ] **M4 (P2)** relay image build + registry push + version tag + rollout step; backend relay client sends `X-Relay-Contract`, tolerates 426
- [ ] **1.5-0 (P2)** deploy script waits for the plugin migration job before rolling API pods; the plugin re-checks head and mounts routes idempotently; alert on mixed replica plugin state
- [ ] **1.5-0 (P3)** shared migration helper takes the version table name as a parameter (bootstrap SQL hardcodes `alembic_version`, `alembic/env.py:64-90`); pending-credential sweeper
- [ ] **1.5a (P2)** ZIP bytes:
  - a streaming Paperless download outside `execute_tool` (or local copies only);
  - byte size + sha256 stored on the item snapshot at add time (the recovery copy is deleted with the document);
  - bounded thread/queue ZIP writer, abort on disconnect; tests.
- [ ] **M1 (P2)** batch status query for `?ids=` (cap 100); an app burst after arriving home maps 429 → outbox retry, never failed
- [ ] **1.5b (P3)** golden test + tax-advisor checklist item: day boundary for a foreign day in a different timezone
- [ ] **Regression tests (CRITICAL):**
  - `register_hook` still accepts existing in-repo + staged handlers;
  - the review-flow allowlist default is byte-identical;
  - `/api/config/features` existing fields are byte-identical;
  - core autogenerate with the plugin schema present emits no drop and the chain is unchanged.
- [ ] **Evals:** Schicht-A eval + phantom-obligation set for Phase 5; a prompt-injection regression case for the fenced capture-details block.
- [ ] **Docs sweep:** correct the design's "already applies a deferred post-consume PATCH" (custom fields are new) and "children `source=pdf_split`".

## Gate outcome (user, 2026-09-14) — applied
- **User challenges UC1–UC5: all rejected.** R2–R6 stand; no plan change.
- **Taste decisions:**
  - W1 xidra first (kept).
  - W2 Apple conventions + Renfield palette, no Cormorant.
  - W3 day list with "wie Vortag".
  - W5 "Trotzdem aufnehmen" after 3 rejections → `quality: low` → review.
  - W6 plugin scaffold/manifest/payload dataclasses/FakeHost/`docs/PLUGINS.md` in 1.5-0.
  - **W4 (new):** scan first + on-device type suggestion. Design: `docs/design/mobile-receipt-capture.md`, section "On-device document type suggestion — scan first [R7]".
- **Design doc changes (R7):**
  - revision log;
  - status line;
  - new W4 section;
  - Business-meal capture type (after scan; adds Rechnung/Sonstiges hints);
  - desk-scan quality gate escape;
  - pairing KB → per user + no cross-owner duplicate link;
  - split children keep `source=mobile_capture`;
  - Paperless custom fields are new work (3 places: details section, "what exists today", hook table);
  - `MOBILE_CAPTURE_KB_NAME` meaning;
  - Phases (M2b);
  - "Decided from R7" table.

### Tasks from the gate
- [ ] **Phase 0 (W4) go/no-go:**
  - on-device pinning of the Foundation Models session (no Private Cloud Compute / third-party routing possible) on iOS and macOS;
  - OS version + API for image attachments;
  - German output quality;
  - availability on devices set to an EU region;
  - `RecognizeDocumentsRequest` minimum OS;
  - latency p50/p95 on the slowest Tier A/B device;
  - thermal impact over 20 captures.
  - **Fail on any of these → Tier C (manual) everywhere.**
- [ ] **Phase 0 (W4, D-W4a) eval set ≥ 100 per type, before M2.** Design: "Eval set — 100+ real receipts per type before M2".
  - **Go/no-go:** check the legal basis/notice with the instance's data-protection responsibility before collection.
  - **Capture:** from the paper archive + new receipts, printed front only, handwritten participant notes covered.
  - **Storage:** only on the operator device or an encrypted, access-restricted own folder. Never git/CI/cloud AI/chat.
  - **Labels:** owner labels (type, printed merchant/date/total/currency, strata); a second person re-labels a 20% sample; κ ≥ 0.8 on type, otherwise revise the guide and re-label.
  - **Strata minimums:** thermal ≥ 20, handwritten ≥ 10, foreign-language ≥ 10, poor light ≥ 20, multi-page Rechnung ≥ 20.
  - **Runner:** `EvalRunner` on device (Tier B text logic also in the simulator). JSON report per (type, tier, device, OS, rules_version): Wilson 95% bounds, confusion incl. Bewirtung→Beleg, hint-field precision, latency, memory. No images/text/names in the report.
  - **Threshold → `SuggestionPolicy`:**
    - pre-select a type iff Wilson lower bound ≥ 0.95 (Beleg also needs Bewirtung→Beleg ≤ 1%);
    - hint field `preselected` iff lower bound ≥ 0.90.
  - **Retention:** delete images + OCR text ≤ 30 days after the report is accepted; record the deletion in the private runbook.
  - **Contingency:** a type short of 100 by the Phase 0 end date → rank first, no pre-selection.
- [ ] **Phase 0 (W4) superseded note:** the earlier 30-per-type sizing is replaced by D-W4a.
  - precision/recall per type and tier, agreement-rule precision, field exact-match;
  - set and raw results stay private; the public repo holds only the harness + synthetic examples + aggregates.
- [ ] **M1 contract (W4/W5):**
  - `capture_type` enum `beleg|bewirtung|rechnung|sonstiges`;
  - optional telemetry is merged into `client_hints` (per-field `confidence` enum + `type.state ∈ {preselected, ordered, late, timeout, unavailable, disabled}` + `rules_version`), enums validated, never interpreted;
  - `quality: low` flag → `needs_review/low_capture_quality`;
  - health feature `type_suggestion` (kill switch);
  - tests incl. an invalid enum and a `quality: low` routing test.
- [ ] **M2 (W4):**
  - Erfassen opens the camera immediately (setting, default on);
  - send sheet after the scan with 4 chips + target account, never blocked on a model;
  - chips with the on-device suggestion per `SuggestionPolicy` (D-W4d). Manual chips only on Tier C devices or under the Phase 0 contingency.
- [ ] **M2 (W5):** "Trotzdem aufnehmen" after 3 consecutive quality rejections (iPhone retake + desk scan), metadata `quality: low`, history reason.
- [ ] **M2 (W4, moved from M2b per D-W4d)** on-device type suggestion (human ~2.5–3.5 wk / CC ~5–7 d)
  - **Gated on the Phase 0 gates:** (1) on-device guarantee, (2) image input, (3) German quality, (4) eval set.
  - **Contingency:** if gate 1, 2 or 3 fails, M2 ships manual chips; if only gate 2 fails, Tier B ships.
  - **Supersedes:** where the list below still says "field suggestions shown, NOT uploaded", read "sent as `client_hints`" (D-W4c).
  - **Scope:**
  - tier detection A/B/C with runtime availability states (unavailable / not enabled / downloading / region);
  - OCR (`RecognizeDocumentsRequest` → `VNRecognizeTextRequest`);
  - guided generation into a closed enum + bounded fields;
  - agreement confidence (model type ∧ deterministic evidence rule);
  - pre-select only for types whose **holdout Wilson lower bound** of agreement precision is ≥ 0.95, gated at runtime on the evaluated OS major version (an unknown OS → rank first);
  - first + last page only;
  - async with the user's tap winning;
  - field suggestions sent as `client_hints` when the instance advertises them (D-W4c), plus the Bewirtung `place_name` editable pre-fill;
  - VoiceOver copy;
  - Settings line "Typvorschlag nicht verfügbar";
  - debug eval screen;
  - tests:
    - session pinned to the on-device model;
    - no network call during classification (URLProtocol trap);
    - late suggestion never overrides a user tap;
    - injection text on the receipt cannot produce an out-of-enum type;
    - low confidence → no pre-selection.
- [ ] **1.6 (W4):** desk scan runs the suggestion after "Fertig" with the same tiers; macOS pinning verified in Phase 0.
- [ ] **M1 (D-W4c) `client_hints` backend** (human ~1–1.5 wk / CC ~2–3 d):
  - **Advertisement:** health `client_hints: {version: 1}`; the Pydantic block is `extra=forbid`.
  - **Validation:**
    - merchant ≤ 120 chars, NFC, no control chars;
    - date calendar-valid within [today − 10 y, today + 1 d];
    - total decimal regex, |value| ≤ 1 000 000;
    - currency from the ISO 4217 list;
    - type/state enums.
  - **Invalid block** → drop the block only + ledger `client_hints_invalid`; the upload is never rejected.
  - **Storage:** table `capture_client_hints`:
    - document FK CASCADE;
    - tier inherited, read only through the document circle filter;
    - never `document_facts`, never chunks, never agent or extraction prompt text.
  - **Comparator** (deterministic, no LLM), after Schicht-A:
    - `hint_agreement` per field;
    - a total mismatch → "Du bist dran" + review display "App erkannte X, Server Y";
    - date/merchant mismatches are informational;
    - never sets facts or `amount_confirmed`.
  - **Tests:**
    - injection strings in merchant;
    - Feb 30, far-future date, 3-decimal/overflow/letters total, bad currency;
    - missing block;
    - old instance without advertisement;
    - tier inheritance;
    - cascade delete;
    - comparator normalisation;
    - mismatch shown in review + app.
- [ ] **M2 (D-W4c)** the app sends `client_hints` only when advertised; Tier C sends no block; fields truncated client-side
- [ ] **Plan dates (D-W4d):** Phase 0 ≈ 3–5 calendar weeks if the archive covers all four types, longer where new business-meal receipts are needed. M2 starts at Phase 0 end. D-U-N-S still gates the first TestFlight independently.
- [ ] **1.5a app (W3):** trip days as a day list with a "wie Vortag" copy-forward action.
- [ ] **DESIGN.md (W2):** a native appendix with Apple conventions + Renfield palette mapping + account colour set; no Cormorant in the apps.
- [ ] **1.5-0 (W6):** DX tasks from Phase 2.5 are in scope (≈ +1.5–2 CC days on the 1.5-0 estimate).

## Autoplan-added tasks (by phase; the checkboxes above stay the source of truth for R2–R6 scope)
### From Phase 1 (CEO)
- [ ] **Phase 0 pass/fail (E1), on iOS 18:**
  - A Shortcuts "joins Wi-Fi" personal automation set to "Run Immediately" runs the App Intent without opening the app.
  - The intent hands the outbox to the app's background `URLSession`. Test the shared container and the session
    identifier when the intent runs out of process.
  - The intent returns within 10 s.
  - **Fallback if it fails:** keep the documented MVP residual. Add no polling.
- [ ] **Phase 0** usage baseline: receipts/month, named users + their networks, minutes lost per receipt (E5)
- [ ] **After M3** usage measurement: captures/week per pilot user over 4 weeks (E5). The threshold that gates 1.5/1.6 is UC3, set at the gate.
- [ ] **M1** record the build and last seen per credential on every authenticated request (E3). Header `X-Client-Build: <build-number>;<ISO build date>` (the app embeds its build date) → `mobile_capture_devices.client_build` + `client_build_date`. The alert stays in M4.
- [ ] **M4** stuck detection (E2):
  - **Migration:** `mobile_capture_log.stuck_alerted_at`.
  - **Config:** `MOBILE_CAPTURE_STUCK_HOURS` (default 6).
  - **Detector:** Scheduled Task built-in `mobile_capture_stuck`, hourly, self-gated.
  - **Rule:** the aggregated status (PDF-Split children; `split_pending` = processing) stays `accepted`/`processing` longer than the threshold.
  - **Alert:** mark `stuck_alerted_at`, then ONE grouped `ops_alert` per run listing the count. No flood when the worker stalls.
  - **App:** `/captures` returns `stuck`.
  - **Tests:** split parent, restart dedup, 50 stuck → one alert.

### From Phase 2 (Design)
- [ ] **M2** history as four user buckets ("Du bist dran" on top, "Sicher gespeichert – wird im Heimnetz gesendet", "In Arbeit", "Erledigt"), de/en
- [ ] **M2** account states:
  - TLS pin mismatch ("Zertifikat geändert – neu koppeln");
  - device revoked banner with "Neu koppeln und senden" / "Belege löschen" (confirm);
  - build-age countdown from day 75 with TestFlight link;
  - contract-too-old banner.
- [ ] **M2** other states and prompts:
  - permission-denied screens (camera/location/notifications) with Settings deep link;
  - storage-low warning before scan;
  - capture sheet names the target account;
  - `needs_review` without app fix → "Auf dem Web prüfen" + URL;
  - purged-thumbnail text row.
- [ ] **M1 contract** shared de/en loc-key table for capture states, reasons, trip/report status, published with the contract fixtures
- [ ] **M1 frontend** "Mobile Geräte" layout:
  - H1 + lede;
  - device table, stacked below 640px;
  - `.pairing-qr-modal` in 3 steps (tier via TierPicker → QR + open-in-app link + code with expiry countdown → text success "Gekoppelt: <Gerät>, <Zeit> – nicht Sie? Widerrufen");
  - confirm-dialog revoke;
  - `.empty-state`.
- [ ] **M1 frontend** same-device pairing: custom-URL-scheme "In der Renfield-App öffnen" also on iPhone/iPad Safari + manual code
- [ ] **1.5a web** "Reisekosten":
  - list grouped Offen/Eingereicht/Abgerechnet (rows, not cards);
  - detail = totals-per-currency header + "N Beträge unbestätigt" + one primary action per state;
  - Belege (inline confirm with Schicht-A suggestion) → Pauschalen & Kilometer (lines with formula disclosure, or info banner "Berechnung nicht freigegeben – Belege sind vollständig");
  - Export (PDF/CSV/ZIP; 413 copy with category/period hint) → Verlauf (collapsed).
- [ ] **1.5a app** trip card:
  - next missing input + completeness counter;
  - days/legs drill-downs;
  - per-diem toggle switchable while open;
  - "Reise noch aktiv seit <Datum>" on the capture sheet after 3 days;
  - end-trip summary card with instance URL.
- [ ] **1.5a plugin** content-free push template `push.trip.ready_for_review` when all receipts of an ended trip are processed (via `capture_state_changed`)
- [ ] **Risks** add: an expired TestFlight build does not launch, so the outbox is stranded until reinstall.

### From Phase 2.5 (DX) — apply to whichever substrate size UC2 settles on
- [ ] **1.5-0 (first item)** tested `plugins/_example` + `src/frontend/src/plugins/_example` (route + table + nav), `bin/new_plugin.py <ns>`, quickstart in `docs/PLUGINS.md`. Target: hello-world plugin ≤ 30 min
- [ ] **1.5-0** `PluginManifest` (name, version, `requires_host`, `requires_core_revision`, migrations path, reason codes). Derives route/settings/table/version-table/feature names; the host binds the namespace; conflict → `namespace_conflict`
- [ ] **1.5-0** one frozen payload dataclass per new hook event; additive fields optional (no `**kwargs` breakage)
- [ ] **1.5-0** `host.hooks.on()` as the single registration path; `host.captures.register_extension_handler(ns, fn)` with its own timeout; zero-arg `register` detected via `inspect.signature`
- [ ] **1.5-0** `PluginLoadError(code, cause, fix, doc_anchor)`:
  - codes: `contract_major_mismatch`, `migration_not_at_head` (DB + code revision + exact command), `core_revision_too_old`, `sync_handler`, `namespace_conflict`;
  - surfaced in `_plugin_status`, `internal.system_health`, `ops_alert`.
- [ ] **1.5-0** async-only check: unwraps `functools.partial`/wrappers and async `__call__`; warns for one release, then rejects; audit existing in-repo and staged out-of-repo handlers first
- [ ] **1.5-0** `bin/plugin_migrate.py upgrade <ns>|--active` + ONE generic job with a `PLUGIN` env var (manifest-driven) instead of a per-plugin manifest
- [ ] **1.5-0** HostAPI members the first consumer needs: rate limiter, permission check (admin), scheduled-task registration, document bytes with verified fallback; an explicitly unstable `host.experimental`
- [ ] **1.5-0** `HOST_CONTRACT_VERSION` surface snapshot test (fails unless the version changed) + deprecation rule (≥ 1 minor) + contract changelog; `plugin_host.testing.FakeHost` + a conformance test loading `_example` in the core suite
- [ ] **M1 contract** `contracts/mobile-capture/v1/`:
  - JSON Schemas generated from the Pydantic models + examples; a backend test validates live responses;
  - all state/reason enums in the schema;
  - `X-Mobile-Capture-Contract` echo + `min_supported` → `426 upgrade_required`;
  - `failed/invalid_extensions` returns `{namespace, key, code}`;
  - the iOS repo pins a fixture tag.
- [ ] **M1** `IngestCredential.status` (pending|active|revoked) + `pending_expires_at`; `rotate_credential` refuses pending (A1)
- [ ] **M1** one `ROUTES` tuple in `services/ingest_credentials.py`, imported by `api/routes/ingest_credentials.py` (A2)
- [ ] **M1** pairing stores the numeric user id in `credential.owner` (rename-safe)
- [ ] **M1** ledger upsert on the duplicate path and idempotently on retry after a crash between create and ledger; test both (CRITICAL GAP)
- [ ] **M1** generic `one_time_code_store` (issue/consume, typed Redis error); `/pair` → 503 `pairing_unavailable` on Redis failure; test (Q2)
- [ ] **M1** `/pair` rate limit per code + per IP + global cap (S1); `extensions` depth ≤ 4 + key-count cap (S2); uniform 401 for any rejected token (S4)
- [ ] **M1** PDF trailer check (`%%EOF`) besides magic bytes; index `mobile_capture_log (client_id, updated_at)`
- [ ] **M1 frontend** extract shared primitives "secret shown once" + "QR panel" from `IngestCredentials.tsx` / `SatelliteEnrollment.tsx` / `PairInitiatorModal` (`qrcode.react`) and compose them on "Mobile Geräte"; columns label, status, last seen, build, build age (E3/E4)
- [ ] **M2** App Intent "Belege hochladen" (E1)
- [ ] **M3** config checklist: `INGEST_CREDENTIALS_ENABLED=true` on the business instance
- [ ] **M4** push consumer as a supervised lifecycle task (restart with backoff, drain on shutdown) + pending/lag gauge in `internal.system_health`; restart test (A3)
- [ ] **M4** stuck-capture alert (`accepted`/`processing` > N h → `ops_alert` + history badge); per-source ingest counters; relay error counters; runbook lines (E2, §8)
- [ ] **1.5-0** `capture_extensions_apply`: per-namespace `asyncio.wait_for` + `run_hooks_with_errors` so rejected / crashed / no-handler are distinct; timeout test
- [ ] **1.5-0** plugin migration step added to `bin/deploy-production.sh` in the same PR as `k8s/alembic-upgrade-plugin-job.yaml`
- [ ] **1.5a** ZIP byte source: Paperless fallback with `truncate=False` + sha256 check against `documents.file_hash`; mismatch → "nicht enthalten (Datei beschädigt)"; test (CRITICAL GAP)

<!-- AUTONOMOUS DECISION LOG -->
## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|-------|----------|----------------|-----------|-----------|----------|
| 1 | 0 | Skip the gstack upgrade prompt (1.79 → 1.84) | Mechanical | conservative | Background run; changes outside plan file / ~/.gstack are forbidden | upgrade now |
| 2 | 0 | UI scope = yes, DX scope = yes | Mechanical | P1 | UI grep 8 hits (screens, forms, nav); DX grep 38 hits (API, endpoint, REST, hook, plugin) | — |
| 3 | 0.5 | Codex unavailable → Claude subagent only in every phase | Mechanical | P6 | `codex` binary not found | — |
| 4 | 0 | No /office-hours offer | Mechanical | P6 | Design doc + DESIGN.md exist | run office-hours |
| 5 | 1 | Mode SELECTIVE EXPANSION | Mechanical | autoplan override | Enhancement of the existing ingest system | EXPANSION, HOLD, REDUCTION |
| 6 | 1 | Keep approach B (plan as written); record A and C | Mechanical | P1 + user direction | Scope cuts on user-decided items are challenges, not auto-decisions | A, C |
| 7 | 1 | E1 App Intent + Wi-Fi automation → Phase 0 + M2 | Mechanical | P2 | In blast radius, <1d CC, removes the main MVP residual | defer |
| 8 | 1 | E2 stuck-capture alert → M4 | Mechanical | P1 | Silent stuck captures otherwise | skip |
| 9 | 1 | E3 device list last seen / build | Mechanical | P2 | Data already recorded | skip |
| 10 | 1 | E4 reuse credential/QR components | Mechanical | P4 | Existing `IngestCredentials.tsx`, `SatelliteEnrollment.tsx`, `PairInitiatorModal` | new components |
| 11 | 1 | E5 usage baseline in Phase 0 | Mechanical | P1 | No success metric exists | skip |
| 12 | 1 | E6 evaluate Apple custom app distribution (R2 unchanged) | Mechanical | P1 | Evaluation only, cheap | skip |
| 13 | 1 | E7 share-sheet extension → TODOS | Mechanical | P3 | New target outside M2 | build now |
| 14 | 1 | E8 zero-install fallback doc → TODOS | Mechanical | P3 | Not blocking | build now |
| 15 | 1 | E9 batch capture session → TODOS | Mechanical | P3 | PDF-Split covers it | build now |
| 16 | 1 | F10 household-first pilot not adopted | Taste | P6 + user direction | Business instance has the need (projects); test-KB dry run mitigates | household first |
| 17 | 1 | F12 phone write scope: no change | Mechanical | P5 | Trip mutations already live in plugin routes (1.5); MVP drops `extensions` | restrict PATCH |
| 18 | 1 | F8 comparison recorded, no pilot gate | Mechanical | P6 | Both reviewers agree the native app wins on status/details/multi-instance | Files→watch-folder pilot |
| 19 | 1 | A1 explicit credential status column | Mechanical | P5 | `rotate_credential` re-enables revoked rows | reuse `is_enabled` |
| 20 | 1 | A2 single ROUTES tuple | Mechanical | P4 | Two hardcoded route lists | duplicate list |
| 21 | 1 | A3 supervised API-pod consumer + gauge | Mechanical | P1 | New process shape without supervision | unsupervised task |
| 22 | 1 | Ledger upsert on the duplicate/crash path | Mechanical | P1 | CRITICAL GAP (silent missing status) | — |
| 23 | 1 | ZIP bytes: truncate=False + sha256 | Mechanical | P1 | CRITICAL GAP (MCP truncation) | trust MCP bytes |
| 24 | 1 | Per-namespace hook timeout + `run_hooks_with_errors` | Mechanical | P5 | `run_hooks` hides crashes, no timeout | plain `run_hooks` |
| 25 | 1 | Generic one-time code store + 503 on Redis error | Mechanical | P4 + P1 | Avoid cloning the PKCE store; avoid a misleading "invalid code" | clone sso store |
| 26 | 1 | Pairing stores the numeric user id | Mechanical | P5 | Username rename breaks the sphere | username |
| 27 | 1 | `/pair` rate limit per code + IP + global; uniform 401; extensions depth cap | Mechanical | P1 | Timing oracle and NAT throttling | single IP bucket |
| 28 | 1 | PDF trailer check + since-index | Mechanical | P1 | Truncated uploads; `?since=` scans | magic bytes only |
| 29 | 1 | `INGEST_CREDENTIALS_ENABLED` in the M3 checklist; plugin job in the deploy script | Mechanical | P1 | `rfi.` tokens fall to legacy when the flag is off | — |
| 30 | 1 | TODOS.md not edited; deferred items listed at the gate | Mechanical | run constraint | Run may only change the plan file + ~/.gstack | write TODOS.md |
| 31 | 1 | **UC1** per diems/mileage sequencing behind the advisor's intake channel (R4-A2) | User Challenge | — | Primary + subagent agree | — |
| 32 | 1 | **UC2** narrow the plugin substrate (R5) — reframed in Phase 2.5 as "narrower but complete" | User Challenge | — | Primary + subagent agree it is overbuilt (different fixes) | — |
| 33 | 1 | **UC3** gate Phase 1.5 and 1.6 on measured MVP use; 1.6 also on Phase 0 desk quality (R4-A7, R6) | User Challenge | — | Primary + subagent agree | — |
| 34 | 1 | **UC4** tunnel before push M4 / Phase 1.5 (R2 sequencing) | User Challenge | — | Primary + subagent agree | — |
| 35 | 1 | **UC5** owner self-settle only for the admin/owner role (R4-A6) | User Challenge | — | Primary + subagent agree | — |
| 36 | 1 | CEO spec-review loop: round 1 (6/10, 10 issues) and round 2 (7/10, 8 issues) fixed in the CEO plan artifact | Mechanical | P1 | Issues differed between rounds (no convergence stop) | — |
| 37 | 1 | E6 (custom app distribution evaluation) moved ACCEPTED → DEFERRED | Mechanical | P3 | Changes neither the timeline nor R2 (spec review round 2) | keep in Phase 0 |
| 38 | 1 | E3 build header recorded from M1, alert stays in M4 | Mechanical | P5 | Device list columns otherwise empty until M4 | columns in M4 |
| 39 | 1 | E2 mechanism: Scheduled Task built-in + DB `stuck_alerted_at` + aggregated split status | Mechanical | P1 | In-memory ops_alert ledger resets on restart | ledger-only dedup |
| 40 | 1 | E5 go/no-go threshold folded into UC3 (gates 1.5 and 1.6) | Mechanical | P6 | The threshold gates user-decided phases → user decides | auto-threshold |
| 41 | 2 | Mockups skipped → text wireframes | Mechanical | P6 | Design binary present, no image-model credential | configure a key (outside run scope) |
| 42 | 2 | Focus: all 7 passes | Mechanical | P1 | — | subset |
| 43 | 2 | Web lists as tables, pills icon + text, `.empty-state` one sentence + CTA | Mechanical | P5 | App UI rules, AI-slop blacklist | cards |
| 44 | 2 | History as four user buckets (subagent critical) | Mechanical | P5 | Seven engineering states are not user language | raw states |
| 45 | 2 | New states: pin mismatch, revoked with re-pair/delete, build countdown, contract-too-old, permissions, storage low | Mechanical | P1 | Missing trust-moment states | — |
| 46 | 2 | End-trip summary card + `push.trip.ready_for_review` template | Mechanical | P1 | Journey break processed → report | web-only discovery |
| 47 | 2 | Per-diem toggle switchable while the trip is open | Mechanical | P1 | A10 unchanged; avoids a forced tax decision at start | start-only |
| 48 | 2 | Same-device pairing link on iPhone/iPad Safari | Mechanical | P2 | QR cannot scan its own screen; <1d | Mac-only deep link |
| 49 | 2 | Revoke = confirm dialog, no undo toast | Mechanical | P5 | Irreversible action | undo toast |
| 50 | 2 | Pairing success as text + "nicht Sie? Widerrufen" | Mechanical | P5 | DESIGN.md colour-never-alone | green only |
| 51 | 2 | Shared de/en status vocabulary via contract fixtures | Mechanical | P4 | One table for web + app | per-client strings |
| 52 | 2 | **D-T1** native visual language = HIG + brand palette mapping | Taste | P5 | DESIGN.md is web-only | port DESIGN.md typography |
| 53 | 2 | **D-T2** per-day entry = day list with "wie Vortag" | Taste | P5 | Phone width | week grid |
| 54 | 2 | **D-T3** capture type before scan (design doc kept) | Taste | P6 | One remembered tap; subagent prefers scan-first | scan first |
| 55 | 2 | **D-T4** quality gate escape "Trotzdem aufnehmen" after 3 rejections | Taste | P1 | User otherwise stuck with a live camera; changes a design-doc sentence | no escape |
| 56 | 2 | Split ZIP export by category/period → TODOS (proposed); app icon/branding → TODOS (proposed) | Mechanical | P3 | Not blocking | build now |
| 57 | 2.5 | Product type Platform/Library + API contract; persona = maintainer or coding agent adding plugin #2 | Mechanical | P6 | Most common developer for an in-repo plugin | external third-party author |
| 58 | 2.5 | Target TTHW ≤ 30 min (full plugin), keep ≤ 5 min hook-only; magical moment = scaffold command | Mechanical | P5 | Lowest-effort vehicle reaching the competitive tier | playground |
| 59 | 2.5 | Mode DX POLISH | Mechanical | autoplan override | Enhancement of the existing hook system | EXPANSION, TRIAGE |
| 60 | 2.5 | Address all 7 roleplay confusion points | Mechanical | P1 | — | subset |
| 61 | 2.5 | `_example` plugin + `bin/new_plugin.py` + `docs/PLUGINS.md` first in 1.5-0 | Mechanical | P1 + P5 | No scaffold, ~14 manual steps | copy expense_reports |
| 62 | 2.5 | `PluginManifest` derives all names; host binds namespace | Mechanical | P5 | Seven spellings of one identity | manual naming |
| 63 | 2.5 | Frozen payload dataclass per event | Mechanical | P1 | Additive kwarg silently kills handlers | `**kwargs` |
| 64 | 2.5 | `PluginLoadError(code, cause, fix, doc)` | Mechanical | P1 | Load errors lack cause/fix | raw exception text |
| 65 | 2.5 | Async check unwraps partials/wrappers; warn one release, then reject | Mechanical | P3 | Avoid breaking staged out-of-repo handlers | immediate reject |
| 66 | 2.5 | One generic plugin migrate CLI + job | Mechanical | P4 | Per-plugin manifest duplication | one job per plugin |
| 67 | 2.5 | HostAPI gains rate limiter, permissions, scheduled tasks, verified bytes; `host.experimental` | Mechanical | P1 | First consumer needs them | core imports |
| 68 | 2.5 | Surface snapshot test + deprecation rule + FakeHost + conformance test | Mechanical | P1 | No evolution rules or harness | — |
| 69 | 2.5 | Contract schemas generated from Pydantic, `min_supported` + 426, `{namespace,key,code}` errors | Mechanical | P1 + P4 | Fixture drift, invisible skew | examples only |
| 70 | 2.5 | DX fixes add ~1.5–2 CC days to 1.5-0 | Taste | P1 vs P3 | Borderline size in blast radius; recommended include | defer DX polish |
| 71 | 3 | Complexity check triggered, scope kept | Mechanical | P2 (Eng never reduces) | Scope questions are UC1–UC4 | reduce |
| 72 | 3 | Paperless details mirror as its own durable pass | Mechanical | P1 | Custom fields not applied today; re-poll path never PATCHes | piggyback upload PATCH |
| 73 | 3 | Reuse `task_queue` stream consumer class | Mechanical | P4 | Existing XREADGROUP/XAUTOCLAIM class | hand-rolled consumer |
| 74 | 3 | Package `services/mobile_capture/` split by concern | Mechanical | P5 | One module would mix 4 concerns | single module |
| 75 | 3 | Relay image pipeline + `X-Relay-Contract` | Mechanical | P1 | Distribution check | undefined |
| 76 | 3 | Per-user mobile-capture KB; cross-owner duplicate never links (security) | Mechanical (security) | P1 | Verified cross-user link + KB-owner visibility | shared KB |
| 77 | 3 | Ledger row inserted first | Mechanical | P5 | `ingest_document` commits internally | same-transaction assumption |
| 78 | 3 | Pairing code entropy + attempt burn + `TRUSTED_PROXIES` on the instance | Mechanical | P1 | Spoofable/shared limiter bucket | IP limit only |
| 79 | 3 | Worker-fired hooks must reach plugins (loader in workers or stream relay; choose at 1.5-0) | Mechanical | P1 | Verified: workers load no plugins | ignore |
| 80 | 3 | Aggregation from stored split child ids | Mechanical | P5 | Duplicate children are not lineage-stamped | lineage query |
| 81 | 3 | Push lease → send → mark → ack | Mechanical | P5 | Retry dead by construction | mark before send |
| 82 | 3 | Single `mark_failed` helper | Mechanical | P4 | Several terminal writers | per-site emits |
| 83 | 3 | Deploy waits for plugin job; idempotent mount; mixed-replica alert | Mechanical | P1 | Rolling deploy disagreement | startup-only check |
| 84 | 3 | ZIP bytes via streaming download + size/sha256 snapshot + bounded writer | Mechanical | P1 | Recovery copy deleted with document; MCP truncation | trust MCP |
| 85 | 3 | 4 regression tests flagged CRITICAL | Mechanical | iron rule | Existing behaviour changes | — |
| 86 | 3 | Design doc left unedited; its two inaccuracies go to the docs sweep | Mechanical | run constraint | Plan-file-only run (superseded by row 99) | edit design now |
| 87 | gate | UC1 per diems/mileage stay in v1 | User Challenge rejected by user 2026-09-14 | user | Direction stands; 1.5b as planned, dark until the tax-advisor gate | sequence behind intake channel |
| 88 | gate | UC2 general plugin substrate as designed | User Challenge rejected by user 2026-09-14 | user | 8 HostAPI members, 6 hook events, frontend registry kept | narrower contract |
| 89 | gate | UC3 no usage gate before 1.5/1.6 | User Challenge rejected by user 2026-09-14 | user | Order as planned | usage threshold gate |
| 90 | gate | UC4 tunnel stays Phase 3 | User Challenge rejected by user 2026-09-14 | user | After push/1.5 | tunnel earlier |
| 91 | gate | UC5 owner settles own report (R4-A6) | User Challenge rejected by user 2026-09-14 | user | As decided | admin-only self-settle |
| 92 | gate | W1 xidra first | Taste — user accepted recommendation | user | — | household first |
| 93 | gate | W2 Apple conventions + Renfield palette, no Cormorant | Taste — user accepted recommendation | user | — | port DESIGN.md typography |
| 94 | gate | W3 day list with "wie Vortag" | Taste — user accepted recommendation | user | — | week grid |
| 95 | gate | W5 "Trotzdem aufnehmen" after 3 rejections → low quality → review; design sentence changed | Taste — user accepted recommendation | user | — | no escape |
| 96 | gate | W6 plugin scaffold/manifest/dataclasses/FakeHost/docs in 1.5-0 | Taste — user accepted recommendation | user | +1.5–2 CC days | defer DX polish |
| 97 | gate | **W4 scan first + on-device type suggestion** (deviates from both offered options) | User decision | user | New design section R7 | type before scan; plain scan-first |
| 98 | gate | Per-user KB + no cross-owner dedup link written into the design doc | Mechanical (security) | P1 | Design said "the instance's mobile-capture KB" | shared KB |
| 99 | gate | Design doc corrected: split children keep `source=mobile_capture`; Paperless custom fields are new work (3 places) | Mechanical | P5 | User asked to correct; verified in code | leave for docs sweep |
| 100 | W4 | Suggestion is an optional capability in tiers A/B/C; minimum iOS 18 unchanged | Mechanical | user constraint | R2-4 stands | raise minimum OS |
| 101 | W4 | Tier C = manual only, no own Core ML classifier in v1 | Taste (surfaced) | P3 + P5 | Needs private training data + own eval + update path | bundled classifier |
| 102 | W4 | Suggested merchant/date/total NOT uploaded in v1 (display + editable Bewirtung pre-fill only) | Taste (surfaced) | P1 privacy + P5 | Schicht-A stays truth; less injection surface | upload as hints |
| 103 | W4 | Confidence from agreement (model ∧ deterministic evidence), not model self-report; pre-select at ≥ 95% eval precision | Mechanical | P5 | LLM self-confidence is uncalibrated | model score threshold |
| 104 | W4 | Fail closed to Tier C if on-device pinning cannot be guaranteed | Mechanical | user privacy rule | Framework also spans PCC / third-party models | best effort |
| 105 | W4 | Placement M2b (after first M2 TestFlight, parallel to M3); M2 ships scan-first manual | Taste (surfaced) | P6 | Partly verified API off the critical path | inside M2 |
| 106 | W4 | capture_type enum gains `rechnung`, `sonstiges` (hints only) | Taste (surfaced) | P1 | Model can propose invoice/other; server extraction decides | keep 2 types |
| 107 | W4 | Per-instance `type_suggestion` kill switch via health | Mechanical | P1 | Model drift across OS updates | app-build only |
| 108 | 3-rerun | `ensure_user_capture_kb` (user-id name, owner set, insert-or-reselect) instead of `resolve_target_kb` | Mechanical (security) | P1 + P5 | Verified: global unique name (`database.py:293`), ownerless racy get-or-create (`folder_ingest.py:447-470`) | reuse resolve_target_kb |
| 109 | 3-rerun | On-device pinning as CI test + runtime assert, fail to tier C | Mechanical | P1 | A silent privacy regression on an OS change | Phase 0 observation only |
| 110 | 3-rerun | Versioned SuggestionRules + synthetic fixtures; eval keyed by (tier, rulesVersion, OS) | Mechanical | P5 | Rule drift | unversioned rules |
| 111 | 3-rerun | Health `contract.capture_types`; server accepts superset | Mechanical | P1 | App/backend skew | fixed enum |
| 112 | 3-rerun | `quality: low` is a rate-limited hint; server OCR coverage stays the gate | Mechanical | P5 | Client-supplied flag is untrusted | trust the flag |
| 113 | 3-rerun | Suggestion runs after VisionKit dismisses, downscaled, one in flight, cancelled on background; fields cleared + redacted | Mechanical | P1 | Memory/thermal on older devices; leakage into logs | run during capture |
| 114 | 3-rerun | Design doc per-user KB sentence aligned with the verified constraints | Mechanical | P5 | — | — |
| 115 | 3-rerun | KB lookup by owner + purpose, id on credential, refuse on owner mismatch (squatting) | Mechanical (security) | P1 | Verified `knowledge.py:1045-1052` | name lookup |
| 116 | 3-rerun | Replace network-trap test with an on-device-only model type in code + build check + Phase 0 packet capture | Mechanical | P1 | Framework traffic bypasses the app network stack | URLProtocol trap |
| 117 | 3-rerun | `low_capture_quality` as a sticky ledger reason + "Geprüft" acknowledge | Mechanical | P1 | Verified: no `needs_review` in the backend | review-flow proposal |
| 118 | 3-rerun | Owner check on all bridge return paths | Mechanical (security) | P1 | Verified RETRY/REINGEST/winner paths | duplicate path only |
| 119 | 3-rerun | Never pre-select Beleg with a Bewirtung signal; track the confusion rate | Mechanical | P1 | The costly error for business meals | symmetric rules |
| 120 | 3-rerun | Eval gate on the lower 95% bound | Mechanical | P5 | 30/type cannot support 95% | point estimate |
| 121 | 3-rerun | **D-W4a eval size** (≥100 per type vs order-first only until data exists) | Needs user | — | Collecting private receipts has a real cost | — |
| 122 | 3-rerun | Session hygiene, kill switch = most restrictive + fail closed, telemetry state enum, health-advertised enums | Mechanical | P1 + P5 | Voice P2 findings | — |
| 123 | 3-rerun | User deletion revokes credentials + archives the capture KB | Mechanical | P1 | Orphaned KB re-adoption | leave orphan |
| 124 | gate-2 | **D-W4a ≥ 100 real captures per type before M2**, pre-selection from the first build where Wilson LB ≥ 0.95 | **User decision — deviates from recommendation** | user | Supersedes row 121 and the 30/type sizing | rank first until data exists |
| 125 | gate-2 | D-W4b manual choice without Apple Intelligence | User decision (= recommendation) | user | — | own classifier |
| 126 | gate-2 | **D-W4c suggested fields sent as `client_hints`** | **User decision — deviates from recommendation** | user | Supersedes row 102 | display only |
| 127 | gate-2 | **D-W4d suggestion inside M2** (no M2b); M2 gated on Phase 0 gates 1–4; manual-chips contingency | **User decision — deviates from recommendation** | user | Supersedes row 105 | M2b |
| 128 | gate-2 | D-W4e `rechnung`, `sonstiges` hint types, only when advertised | User decision (= recommendation) | user | — | two types |
| 129 | W4c | Hints stored in a separate table, tier-inherited, cascade-deleted; comparator is deterministic code, never prompt text; money mismatch raises "Du bist dran" | Mechanical | P1 + P5 | Injection channel + no automatic trust | hints as facts |
| 130 | W4c | Invalid hint block drops only the block, never the upload | Mechanical | P1 | A hint must never lose a receipt | reject upload |
| 131 | W4a | Eval privacy: printed front only, private storage, 30-day deletion after report, second labeller on 20% with κ ≥ 0.8, strata minimums, Wilson bounds, per-type `SuggestionPolicy` | Mechanical | P1 | Implements the user's decision | — |
| 132 | W4a | Data-protection check before collection is a Phase 0 go/no-go item | Mechanical | P1 | Third-party data on business-meal receipts; not assumed | assume legitimate interest |
| 133 | W4d | Contingency: gate 1/2/3 fail → manual chips; only gate 2 fails → Tier B; a type short of 100 → rank first | Mechanical (documented contingency) | P6 | Keeps M2 from blocking on one gate | wait |
| 134 | rerun-2 | Comparator at the end of the Schicht-A hook + sweep; explicit `pending`/`n/a` reasons | Mechanical | P1 | Fire-and-forget, table-only skip, flag, lock skip | separate hook |
| 135 | rerun-2 | Comparator inputs: `document_date`, `issuer`, total selection rule + tip | Mechanical | P5 | No stable total/date fact | naive total |
| 136 | rerun-2 | `client_hints` as raw JSON + separate validation | Mechanical | P1 | Outer forbid would 422 the receipt | outer forbid |
| 137 | rerun-2 | Honest Wilson maths on agreeing predictions (≥ 73/73), Beleg guard on the upper bound; keep threshold 0.95 | Mechanical | P1 (money/tax risk) | 99/100 → 0.946 fails; the user's fallback (rank first) covers the rest | lower threshold to 0.90 |
| 138 | rerun-2 | Dev set ~25/type + frozen rules + independently labelled holdout ≥ 100/type (≈ 125/type collected) | Mechanical | P1 | Label leakage; stays within the user's "100+" | tune on holdout |
| 139 | rerun-2 | Runtime OS-major gating of `SuggestionPolicy` | Mechanical | P1 | Model changes with the OS; images deleted after 30 days | trust the old eval |
| 140 | rerun-2 | Hints tier by join; no hints on other-owner dedup; money mismatch "Geprüft" + auto-clear; merchant never in push/TTS | Mechanical | P1 | Voice P2 findings | — |
| 141 | rerun-2 | M1 hints backend dark until the Phase 0 result; contingency adds the data-protection and κ gates | Mechanical | P6 | No active surface if gates fail | build live |
| 142 | rerun-2 | Four contradicting plan checklist lines edited in place | Mechanical | P5 | Supersede notes insufficient | annotate only |

## Phase 3 re-run — Eng on the amended plan (after gate B2, 2026-09-14)

Scope of the re-run: everything changed at the gate (W4 scan-first + on-device suggestion, W5 quality escape, W6
DX scaffold in 1.5-0, per-user KB, the two design corrections). The unchanged R2–R6 scope keeps the Phase 3
findings above. The rejected UCs change nothing in the tasks.

### Architecture delta
```
iPhone / iPad / Mac app
 Erfassen ─► camera (immediate) ─► VisionKit / DeskScanKit ─► pages
                                            │
                     ┌──────────────────────┴───────────────────────┐
                     ▼ (async, never blocks Senden)                 ▼
          TypeSuggestionEngine (NEW, CaptureFeature)       SendSheet: 4 chips + "an <Instanz>"
           ├─ CapabilityProbe: tier A | B | C  ◄── health.features.type_suggestion (kill switch)
           ├─ OCR: RecognizeDocumentsRequest → VNRecognizeTextRequest (first + last page)
           ├─ OnDeviceModel (Foundation Models, session pinned on-device; else tier C)
           │    guided generation → {type ∈ enum, evidence[], merchant?, date?, total?}
           └─ AgreementRules (deterministic evidence) → high | low | none
                     │ suggestion (in-memory only; never persisted, never uploaded except enums)
                     ▼
          OutboxEngine ─► POST /document  metadata: capture_type, quality?, type_suggestion{enums}
                                               │
backend  services/mobile_capture/ingest.py ─► validate enums ─► quality=low → needs_review/low_capture_quality
         services/mobile_capture/pairing.py ─► ensure_user_capture_kb(user_id)  (NEW; owner_id set, unique-safe)
                                               └► credential.kb_name = per-user KB name
         folder_ingest.ingest_document (dedup per (hash, per-user KB)) ─► cross-owner dup never linked
```
**New coupling:** the app → the Apple Foundation Models / Vision frameworks (OS-owned, versioned by the OS). The
backend coupling is unchanged apart from the enum fields.

### Findings (primary, code-verified)
- **[P1] (confidence 9/10) `models/database.py:293` `name = Column(String(255), nullable=False, unique=True)` and
  `services/folder_ingest.py:447-470` `resolve_target_kb` — a get-or-create with no `owner_id` and no handling of a
  concurrent unique violation.**
  - Per-user KBs built on it would be ownerless: the KB-owner branch is empty and visibility falls back to the atom
    owner.
  - Two first pairings of one user racing → `IntegrityError` → 500.
  - Name collisions: KB names are global, and users can be renamed.
  - **Fix:** a new `ensure_user_capture_kb(db, user_id)`:
    - name `"<MOBILE_CAPTURE_KB_NAME> · u<user_id>"`, stable id, not the username;
    - `owner_id=user_id`; `default_circle_tier` = the user's mobile-capture default;
    - `INSERT … ON CONFLICT (name) DO NOTHING` + re-select;
    - hidden from generic KB pickers via a `purpose='mobile_capture'` marker, or listed under the user only.
  - Tests: concurrent first pairing, admin pairing for another user (owner = target user), a second device reuses
    the KB, user rename.
- **[P1] (confidence 7/10) Tier-A/B pinning is an unverified premise that the whole privacy promise rests on.** The
  design already fails closed to Tier C. The added requirement: the pinning check must be a **build-time test +
  runtime assert**, not only a Phase 0 observation. A future OS or framework change to default routing must not
  silently enable cloud processing. A CI XCTest asserts the session's model type; the runtime re-checks before
  each classification.
- **[P2] (confidence 8/10) Confidence from agreement needs a closed, versioned rule set.** Evidence rules are
  German/receipt-specific and will drift.
  - **Fix:** rules live in `CaptureFeature/SuggestionRules.swift` with a `rulesVersion` in `type_suggestion`
    telemetry, unit-tested on synthetic OCR text fixtures per type.
  - The Phase 0 eval records precision per `(tier, rulesVersion, osVersion)`.
- **[P2] (confidence 7/10) Contract back-compat for `capture_type`.** Older app builds send only
  `beleg|bewirtung`; the server must accept the superset. A newer app must not send `rechnung` to an older
  backend.
  - **Fix:** the health `contract.capture_types` list; the app offers only the types the instance lists.
  - Test: old-app payload still accepted; the app hides `rechnung` when the server does not list it.
- **[P2] (confidence 7/10) `quality: low` produced client-side is untrusted.** A client could set it to force review
  spam, or omit it. It is only a hint: the server maps it to `needs_review/low_capture_quality`, rate-limited per
  client. The stuck/alert logic ignores it. The server-side OCR coverage signal (existing VLM coverage fallback)
  remains the real quality gate.
- **[P2] (confidence 6/10) Memory and thermal on older supported devices.** Loading the model plus running OCR on a
  12 MP page while VisionKit holds buffers.
  - **Fix:** run the suggestion only after VisionKit dismisses; downscale pages to the OCR working size; cancel on
    background; one in-flight classification at a time. Phase 0 measures peak memory.
- **[P3] Suggested fields are never uploaded, but they sit in memory while the sheet is open.** Clear them when the
  sheet is dismissed. Test that nothing is written to the outbox, logs or crash reports: fields are redacted in
  analytics.
- **[P3] Kill switch semantics:** `type_suggestion=false` disables pre-selection **and** skips running the model
  (battery). The health value is cached per account at the next foreground; not real time. Documented.

### Test diagram delta (new paths)
```
[+] App: TypeSuggestionEngine                                   [+] Scan-first user flow
  ├── [GAP→PLANNED ★★★] tier probe A/B/C incl. AI off / region  ├── [PLANNED ★★] camera immediate; sheet immediate
  ├── [GAP→PLANNED ★★★] session pinned on-device (CI + runtime) ├── [PLANNED ★★★] late suggestion never overrides tap
  ├── [GAP→PLANNED ★★]  no network during classification        ├── [PLANNED ★★] low confidence → no pre-select
  ├── [GAP→PLANNED ★★★] injection text → in-enum only           └── [GAP→PLANNED ★★] VoiceOver "Vorschlag: …"
  ├── [GAP→PLANNED ★★]  rules fixtures per type + rulesVersion [+] Quality escape
  ├── [GAP→PLANNED ★]   memory: one in-flight, cancel on bg      ├── [PLANNED ★★] appears after 3 rejections only
  └── [GAP→PLANNED ★★]  suggested fields never in outbox/logs    └── [PLANNED ★★] quality:low → needs_review (server)
[+] Backend                                                     [→EVAL] Phase 0 on-device eval per (tier, rules, OS)
  ├── [GAP→PLANNED ★★★] capture_type superset + health capture_types; old app accepted
  ├── [GAP→PLANNED ★★]  type_suggestion enums validated, never interpreted; quality:low rate-limited
  └── [GAP→PLANNED ★★★] ensure_user_capture_kb: concurrent, admin-for-other, reuse, rename, owner_id set
COVERAGE of new paths: 17/17 planned after this re-run (before: 9/17)
```

### Failure modes delta
```
CODEPATH                    | REALISTIC FAILURE                                   | TEST? | HANDLED? | USER SEES                   | CRITICAL?
----------------------------|-----------------------------------------------------|-------|----------|-----------------------------|----------
on-device pinning           | OS update changes default routing to cloud          | added | added    | falls to manual (tier C)    | was CRITICAL (silent privacy) → task
per-user KB create          | concurrent first pairing → unique violation 500     | added | added    | pairing succeeds            | no
per-user KB ownership       | KB created ownerless via resolve_target_kb          | added | added    | —                           | was CRITICAL (silent visibility) → task
suggestion rules drift      | precision drops after OS/model update               | added | added    | fewer pre-selections        | no (eval + kill switch)
capture_type skew           | new app sends rechnung to old backend               | added | added    | type hidden                 | no
quality:low abuse           | review-queue spam from a client                     | added | added    | rate-limited                | no
```
**New critical gaps:** 2 (pinning regression, ownerless per-user KB). Both are tasks; **0 unaddressed**.

### Dual voices (Eng re-run)
**CODEX:** `[codex-unavailable: binary not found]`.

**CLAUDE SUBAGENT (independent; amendments only)** `[subagent-only]`: 3 P1, 7 P2, 3 P3. Primary re-verified the
code-backed claims:
- **[P1] KB name squatting (verified `api/routes/knowledge.py:1045-1052`).** Any user with `kb.own` creates a KB
  with any free name and becomes its owner. A name-based lookup lets an attacker pre-create another user's
  mobile-capture KB name and read that user's tier-0 receipts. → Look up by `(owner_id, purpose)`, store the KB id
  on the credential, refuse on an owner mismatch. **Design updated.**
- **[P1] The in-app network trap cannot detect Private Cloud Compute routing** (framework traffic goes through
  system processes). → Allow only the on-device model type in code + a build-time check + Phase 0 airplane-mode and
  router packet capture. **Design updated;** the plan task T-URLProtocol is replaced.
- **[P1] `needs_review` exists nowhere in the backend** (verified: no match), and the review flow accepts only
  folder-ingest PDFs. `low_capture_quality` would have no producer and no resolution, and "processed" aggregation
  would overwrite it. → Sticky ledger reason + "Geprüft" acknowledge + aggregation precedence; processing/filing
  continue. **Design updated.**
- **[P2] Cross-owner check on every bridge return path** (verified RETRY `folder_ingest.py:282-286` returns an
  existing row, REINGEST `:305-309` re-points an existing row, concurrent winner `:330-335`) + admin credential KB
  change. **Design updated.**
- **[P2] Evidence rule "VAT id + Summe" matches restaurant receipts**, so a Bewirtung gets pre-selected as Beleg.
  → Never pre-select Beleg with any Bewirtung signal; track the Bewirtung→Beleg confusion rate. **Design updated.**
- **[P2] 30 captures per type cannot support a 95% threshold** (29/30 → ~83% lower bound). → Gate on the lower
  bound; needs ~100+/type or no pre-selection. **User decision D-W4a.**
- **[P2] Session hygiene** (fresh session per capture, serialise, cancel, re-check availability, token budget).
  **Design updated.**
- **[P2] Kill switch scope** before the account is chosen: most restrictive of paired accounts, fail closed on
  stale health, and the model does not run. **Design updated.**
- **[P2] Enum/quality skew** with old instances: send only when health advertises them; forbid unknown fields;
  contract bump. **Design updated.**
- **[P2] Telemetry** `accepted: bool` → state enum + `rulesVersion`; retention with the ledger row; tier disclosure
  noted. **Design updated.**
- **[P2/P3] User deletion** leaves an orphaned KB that a same-name user could adopt → revoke credentials + archive the
  KB, never re-adopt by name. **Design updated.**
- **[P3] Remaining items:**
  - first/last page disagreement → no suggestion;
  - field strings truncated on the client;
  - quality = worst page;
  - admin KB lists: hide or group per-user capture KBs;
  - project KB counts exclude linked captures (documented).

**Primary position:** agrees with all findings; one needs the user (D-W4a).

```
ENG RE-RUN DUAL VOICES — CONSENSUS TABLE:        [subagent-only]
═══════════════════════════════════════════════════════════════
  Dimension                           Primary  Subagent  Codex  Consensus
  ──────────────────────────────────── ──────── ───────── ────── ─────────
  1. Architecture sound?               partly   partly    N/A    CONFIRMED (KB by owner id; sticky review reason)
  2. Test coverage sufficient?         no       no        N/A    CONFIRMED (network trap useless; eval size)
  3. Performance risks addressed?      partly   partly    N/A    CONFIRMED (session/memory/token budget)
  4. Security threats covered?         no       no        N/A    CONFIRMED (KB squatting; all bridge return paths)
  5. Error paths handled?              no       no        N/A    CONFIRMED (needs_review had no producer)
  6. Deployment risk manageable?       partly   partly    N/A    CONFIRMED (contract skew for new enums)
═══════════════════════════════════════════════════════════════
Consensus 6/6 confirmed.
```

### Failure modes delta (voice)
```
CODEPATH                 | FAILURE                                          | TEST? | HANDLED? | USER SEES             | CRITICAL?
-------------------------|--------------------------------------------------|-------|----------|-----------------------|----------
per-user KB lookup       | squatted KB name captures a victim's receipts    | added | added    | pairing refused       | was CRITICAL (silent exposure) → task
pinning test             | cloud routing passes the in-app network trap     | added | added    | manual chips          | was CRITICAL (false assurance) → task
low_capture_quality      | no producer/resolution, overwritten by processed | added | added    | "Du bist dran"        | was CRITICAL (silent) → task
bridge RETRY/REINGEST    | cross-owner row returned or re-pointed           | added | added    | —                     | no (per-user KB) but tested
Bewirtung→Beleg          | wrong pre-selection, occasion never recorded     | added | added    | Bewirtung chip        | no (eval gate)
```
**Critical gaps in the amended plan:** 12 identified in total, **0 unaddressed** (the 3 above are now tasks).

### Tasks from the Eng re-run
- [ ] **M1 (P1)** `ensure_user_capture_kb`:
  - lookup by `(owner_id, purpose=mobile_capture)`; KB id stored on the credential;
  - name `"<prefix> · u<user_id>"` with insert-or-reselect on unique clash; `owner_id` set; default tier from the user;
  - refuse pairing on an owner mismatch;
  - tests: squatted name, concurrent pairing, pairing racing a push, admin-for-other, reuse on re-pair, rename.
- [ ] **M1 (P1)** owner check on DUPLICATE, RETRY, concurrent-winner and REINGEST results + admin credential KB change validation; tests per path
- [ ] **M1 (P1)** sticky ledger reason `low_capture_quality`: "Geprüft" acknowledge route (own client / web owner), aggregation precedence over processed incl. split children, per-client rate limit; tests
- [ ] **M1 (P2)** health advertises `capture_types` + `quality_flag`; contract version bump; request model `extra=forbid`; tests with old-server/new-app and new-server/old-app payloads
- [ ] **M1 (P2)** `type_suggestion {suggested, state, tier, rulesVersion}` validated, stored with the ledger row, deleted with it
- [ ] **M2 (P1)** on-device-only model type in code + build-time check (no cloud/third-party model types compile in); runtime re-check; replaces the URLProtocol-trap test
- [ ] **M2 (P2)** session per capture, serialised requests, cancel on dismiss/background, availability re-check per capture, token-budget truncation; tests
- [ ] **M2 (P2)** rules:
  - never pre-select Beleg with any Bewirtung signal;
  - first/last page disagreement → no suggestion;
  - fields truncated;
  - kill switch = most restrictive of paired accounts, fail closed on stale health, model not run when off;
  - tests.
- [ ] **Phase 0 (P1)** pinning verified in airplane mode + router packet capture; eval gate on the lower 95% bound + Bewirtung→Beleg confusion rate (eval size: D-W4a)
- [ ] **M1 (P2)** user deletion revokes mobile credentials + archives the capture KB; admin KB lists hide/group per-user capture KBs; test

## Cross-phase themes

## Phase 3 re-run 2 — Eng on gate-2 decisions (D-W4a/c/d)

### Architecture delta
```
app: SuggestionPolicy (from Phase 0 eval report) ─► send sheet ─► upload { capture_type, client_hints? (if advertised) }
backend M1: mobile_capture/ingest ─► validate client_hints (extra=forbid) ─► capture_client_hints (doc FK CASCADE, tier = doc)
worker:     post_document_ingest hooks: schicht_a_post_document_ingest_hook (facts: issuer, amount_value/amount_currency, dates)
            └─► hint_comparator (NEW, deterministic, runs AFTER Schicht-A commit; re-runs on re-extraction)
                   └─► hint_agreement per field ─► ledger reason total_mismatch ─► "Du bist dran" + review display
Phase 0:    private eval set (device / encrypted own folder) ─► EvalRunner (device) ─► report JSON (aggregates) ─► SuggestionPolicy
```

### Primary findings (code-verified)
- **[P2] (confidence 8/10) Comparator ordering.**
  - Schicht-A runs as a `post_document_ingest` hook in the worker (`services/schicht_a_extractor.py:746`). It emits
    `issuer` (:469-473), and amounts come from `amount_value`/`amount_currency` on facts (:357-359).
  - A comparator registered as a separate hook has no guaranteed order relative to Schicht-A (`run_hooks` is
    unordered).
  - **Fix:** call the comparator at the end of the Schicht-A hook's commit path. Re-run it on every re-extraction.
  - With `schicht_a_extraction_enabled=false` → `hint_agreement = n/a` (never "mismatch").
  - Tests: ordering, re-extraction, flag off.
- **[P2] (confidence 7/10) One hint block vs PDF-Split children.**
  - The hints describe what the app saw; PDF-Split may produce N children.
  - **Fix:** compare only when the capture results in exactly one non-archived document. With several children,
    the result is `n/a` and the review shows "mehrere Belege erkannt".
  - Test: a split capture never raises a total mismatch.
- **[P3] Money normalisation:**
  - locale decimal comma and thousands separators (`12.345,67`) → Decimal with an explicit locale rule;
  - currency mismatch → `partial`, not `mismatch`;
  - a tip line makes "total" ambiguous → a difference within the tip tolerance → `partial`.
  - Golden tests.

### Dual voices (re-run 2)
**CODEX:** unavailable.
**CLAUDE SUBAGENT** `[subagent-only]`: 6 P1, 8 P2, 2 P3. The primary review checked the maths by hand (Wilson
99/100 → 0.9455, 100/100 → 0.963, 96/100 → 0.9016) and agrees with all findings.

**Fixed in design + plan:**
- **P1: comparator had no reliable trigger.**
  - Schicht-A is fire-and-forget, skips table-only documents, is flag-gated, and can skip on its lock.
  - Fix: call at the end of the hook + a sweep; `pending` vs `n/a` with a reason.
- **P1: no stable total/date fact.** Fix: `documents.document_date`, the issuer fact, a total selection rule +
  tip handling, `no_candidate`.
- **P1: an outer `extra=forbid` would 422 the receipt.** Fix: raw JSON + separate block validation.
- **P1: the Wilson maths was wrong.**
  - Fix: honest numbers; the denominator is agreeing predictions; ≥ 73/73 needed.
  - The Beleg guard uses the upper bound.
- **P1: label leakage.** Fix: a dev set of ~25/type + frozen `rules_version` + an independently labelled
  holdout ≥ 100/type (≈ 125/type collected).
- **P1: drift mitigation was hollow.** Fix: runtime OS-major gating → rank first on an unevaluated OS.
- **P2: contract inconsistency.** Fix: per-field `confidence`; telemetry merged into `client_hints`; types from
  the advertisement.
- **P2: split, re-extraction, dedup.** Fix: `n/a/split`, recompute on every rewrite, no hints on another owner's
  dedup hit.
- **P2: tier copy.** Fix: join through `documents`, no tier column.
- **P2: money mismatch had no resolution.** Fix: "Geprüft" + auto-clear on a later match.
- **P2: merchant text in push/TTS.** Fix: escaped rendering, never in notifications, value-free logs,
  rate-limited flags.
- **P2: bias/strata.** Fix: strata reported, not gated; per type/tier on the reference device; the Bewirtung/Beleg
  intent κ is documented.
- **P2: schedule contingency.** Fix: covers the data-protection go/no-go and κ non-convergence; the M1 hints
  backend is built dark until the Phase 0 result.
- **P2: plan lines contradicting D-W4a/c/d.** The four checklist lines were edited, not just annotated.
- **P3:** ambiguous `1.234` amounts rejected; ISO list reused; simulator export only to the encrypted folder.

```
ENG RE-RUN 2 — CONSENSUS TABLE:  [subagent-only]
  1. Architecture sound?          partly / partly  CONFIRMED (comparator trigger + inputs)
  2. Test coverage sufficient?    no / no          CONFIRMED (pending states, split, tier change, Wilson vectors)
  3. Performance risks addressed? yes / yes        CONFIRMED (sweep bounded, raw JSON ≤ 4 KB)
  4. Security threats covered?    partly / partly  CONFIRMED (422 path, merchant in push, dedup hints)
  5. Error paths handled?         no / no          CONFIRMED (silent n/a → explicit pending/reason)
  6. Deployment risk manageable?  partly / partly  CONFIRMED (backend dark until Phase 0; OS gating)
Consensus 6/6.
```

### Failure modes delta (re-run 2)
```
CODEPATH             | FAILURE                                         | TEST? | HANDLED? | USER SEES               | CRITICAL?
---------------------|-------------------------------------------------|-------|----------|-------------------------|----------
hint comparator      | extraction never runs/skips → silent n/a        | added | added    | "wird verglichen" pending | was CRITICAL (silent) → task
upload metadata      | stray hint field 422s the whole receipt         | added | added    | upload succeeds         | was CRITICAL (receipt lost in outbox) → task
eval policy          | wrong bound maths / leakage → over-confident    | added | added    | rank first              | was CRITICAL (silent wrong pre-select) → task
SuggestionPolicy     | new OS model drift                              | added | added    | rank first              | no
hints tier           | copied tier stale after tier change             | added | added    | —                       | no
total mismatch       | never resolvable                                | added | added    | "Geprüft" clears        | no
```
**Critical gaps overall:** 18 identified across all phases and re-runs, **0 unaddressed**.

### Tasks (re-run 2)
- [ ] **M1 (P1)** hint comparator:
  - called at the end of the Schicht-A hook after commit + scheduled sweep;
  - states `pending | n/a(reason) | match | partial | mismatch`;
  - inputs `document_date`, `issuer`, total selection rule with tip handling;
  - `n/a/split`; recompute on re-extraction;
  - tests: flag off, table-only, no facts, lock skip, split, re-extraction, multiple amounts, tip.
- [ ] **M1 (P1)** `client_hints` accepted as raw JSON (≤ 4 KB) + separate forbid validation; value-free logs:
  - tests: unknown outer field never 422s the receipt; invalid block dropped only; Wilson not involved.
- [ ] **M1 (P2)** hints read via a join through `documents` (no tier column); no hints stored on a dedup hit of another owner; sticky money-mismatch reason with "Geprüft" + auto-clear; per-client mismatch rate limit:
  - tests: tier change after upload, dedup-other-owner, resolution.
- [ ] **M1 (P2)** `client_hints` backend dark (not advertised) until the Phase 0 result
- [ ] **Phase 0 (P1)** eval:
  - dev set ~25/type for rule tuning; freeze `rules_version`; holdout ≥ 100/type labelled by a non-author;
  - Wilson on agreeing predictions (unit vectors 99/100 fail, 100/100 pass, 73/73 pass);
  - Beleg guard on the upper bound;
  - strata reported, not gated;
  - simulator export only to the encrypted folder.
- [ ] **M2 (P1)** `SuggestionPolicy` gated on the evaluated OS major version, else rank first; per-field `confidence`; telemetry merged into `client_hints`; merchant never in push/TTS text
- [ ] **Contingency** also covers the data-protection go/no-go and κ non-convergence (design updated)

## Cross-phase themes
- **Silent no-ops at seams.** Flagged in Phase 1 (`run_hooks` swallows, pairing Redis error read as "invalid"),
  Phase 2.5 (additive payload field kills handlers), and Phase 3 (worker-fired hooks never run, push retry dead).
  High-confidence signal: every new seam needs a "can this silently do nothing?" test.
- **The plugin substrate is mis-sized.** Phase 1 (UC2: overbuilt for one consumer) and Phase 2.5 (incomplete for
  that consumer) are two views of the same defect: generic breadth without the specific depth the consumer needs.
- **Paperless mirror is new work, not reuse.** Phase 1 (S3 byte truncation), Phase 3 primary (no custom-field path,
  re-poll path) and the Phase 3 voice (rate limit, duplicates, id resolution). Three independent sightings.
- **Trust moments need user-visible states.** Phase 1 §11, Phase 2 (pin mismatch, revoked, build expiry) and
  Phase 2.5 (426 upgrade required).
- **The sphere is the invariant.** Phase 1 (credential owner as numeric id, pending state) and Phase 3 (shared KB
  cross-user link and KB-owner visibility). The routing invariant in the design covers *which instance*; the plan
  must equally guard *which person* inside it.

## Pre-gate verification
- **Phase 1 (CEO):**
  - [x] premise challenge with named premises (P1–P10)
  - [x] sections 1–11 with findings or examined notes
  - [x] Error & Rescue Registry
  - [x] Failure Modes Registry
  - [x] NOT in scope
  - [x] What already exists
  - [x] dream state delta
  - [x] Completion Summary
  - [x] dual voices (subagent-only, Codex unavailable)
  - [x] consensus table
- **Phase 2 (Design):**
  - [x] all 7 passes scored
  - [x] issues auto-decided
  - [x] voices
  - [x] litmus scorecard
- **Phase 2.5 (DX):**
  - [x] 8 dimensions scored
  - [x] journey map
  - [x] empathy narrative
  - [x] TTHW target
  - [x] DX checklist
  - [x] voices
  - [x] consensus table
- **Phase 3 (Eng):**
  - [x] scope challenge with code
  - [x] architecture ASCII
  - [x] test diagram
  - [x] test plan artifact on disk
  - [x] NOT in scope
  - [x] What already exists
  - [x] failure modes with critical gaps
  - [x] Completion Summary
  - [x] voices
  - [x] consensus table
- **Cross-phase:** [x] themes.
- **Audit trail:** [x] 86 rows.
- **Not produced, by design of this run:**
  - visual mockups (no image-model credential);
  - TODOS.md edits (plan-file-only run);
  - completion review logs (only after gate approval).
