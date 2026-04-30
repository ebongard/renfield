# Audit Findings & Improvement Plan

Consolidated results from 4 systematic audits (DB Performance, Config Hardcodes, Config Architecture, Frontend Modernization).

---

## Severity Overview

| Severity | Count | Resolved | Category |
|----------|-------|----------|----------|
| KRITISCH | 7 | 7 / 7 | Must fix — performance bottlenecks, security gaps |
| WICHTIG | 14 | 14 / 14 | Should fix — inconsistencies, missing optimizations |
| EMPFEHLUNG | 18 | 16 / 18 | Nice to have — modernization, cleanup |
| GUT | 12 | — | Already well-implemented |

**Status (2026-04-30):** All KRITISCH and WICHTIG items closed. EMPFEHLUNG closed: E1, E2, E3, E4, E5, E6, E7, E8, E9, E10, E11, E12, E13, E14, E16, E17, E18. Open: **E15** (TS strict mode — 31 errors to fix, ~70% null-check additions; warrants its own dedicated session). See `TODOS.md` P2 for active queue.

---

## KRITISCH (7)

Status nach Branch `audit/k1-k7` (PR aus diesem Branch schliesst K1-K7 komplett).

### K1. N+1 Query: KB Permissions Listing — RESOLVED (pre-existing fix)
- **Datei:** `api/routes/knowledge.py::list_kb_permissions` (heute ~Zeile 1124)
- **Status:** Die Ursprungs-Beobachtung war zum Auditzeitpunkt bereits behoben. Die heutige Implementation lädt alle referenzierten User in **einer** Abfrage per `select(User).where(User.id.in_(all_user_ids))`; pro Share gibt es keine zweite User-Query.
- **Verifikation:** `tests/backend/test_kb_shares_service.py::test_list_kb_shares_*` deckt die Aggregat-SQL ab.

### K2. N+1 Query: KB Access Check — FIXED
- **Datei:** `api/routes/knowledge.py::list_knowledge_bases` (~Zeile 761) + `services/kb_shares_service.py`
- **Problem:** `get_user_kb_permission(kb, user, db)` wurde je KB in der Response-Schleife aufgerufen → pro KB eine `atom_explicit_grants`-Query.
- **Fix:** Neuer Batch-Helper `get_user_kb_permission_levels(db, user_id, kb_ids)` lädt alle Grants mit einem `GROUP BY` in **einer** Query. Die uebrigen Permission-Regeln (Owner / KB_ALL / public+KB_SHARED) kommen aus In-Memory-Daten ohne zusaetzliche DB-Rundtrips.
- **Regression-Guard:** `tests/backend/test_kb_shares_service.py::test_get_user_kb_permission_levels_*` plus `tests/backend/test_knowledge.py::TestKnowledgeBaseAPI::test_list_knowledge_bases_batches_permission_lookups`.

### K3. N+1 Query: Conversation List — RESOLVED (pre-existing fix)
- **Datei:** `services/conversation_service.py::list_all` (~Zeile 434)
- **Status:** Ebenfalls bereits gefixt: `message_count` kommt aus einem Count-Subquery, der `preview` aus einem `ROW_NUMBER() OVER (...)`-Window. Eine einzige SQL-Anweisung pro Seite.
- **Verifikation:** `tests/backend/test_conversation_service.py::TestListAll` deckt Pagination, Sortierung, User-Filter und Preview-Semantik ab.

### K4. .env.example covers only 17% of Settings — FIXED
- **Datei:** `.env.example`
- **Fix:** Komplett-Rewrite als gruppierte Referenz aller ~240 Felder (Platform + `HaGlueSettings`). Jede Section kommentiert die Default-Werte und Produktions-Hinweise (Docker Secrets, SecretStr, Profile-Toggles).

### K5. No SecretStr for sensitive fields — FIXED
- **Datei:** `utils/config.py`, `ha_glue/utils/config.py`
- **Status:** Passwoerter, Tokens und API-Keys sind durchgehend `SecretStr`:
  `postgres_password`, `secret_key`, `default_admin_password`, `mail_primary_password`, `n8n_api_key` (platform) sowie `home_assistant_token`, `paperless_api_token`, `jellyfin_api_key`, `jellyfin_token` (ha_glue). Restliche Luecke `presence_webhook_secret` wurde auf `SecretStr | None` umgestellt; Consumer (`ha_glue/services/presence_webhook.py`) ruft `.get_secret_value()`.
- **Regression-Guard:** `tests/backend/test_presence_webhook.py` verwendet jetzt `SecretStr("my-secret-token")`.

### K6. Production Docker Secrets incomplete — FIXED
- **Dateien:** `docker-compose.prod.yml`, `docker-compose.prod-cpu.yml`, `bin/generate-secrets.sh`
- **Fix:** `jellyfin_user_id` und `presence_webhook_secret` zu Secrets-Liste und `secrets:`-Block in beiden Compose-Dateien hinzugefuegt. `generate-secrets.sh` prompt't jetzt auch fuer `jellyfin_token`, `jellyfin_base_url`, `jellyfin_user_id`, `n8n_api_key`, `paperless_api_token`, `mail_primary_password` und auto-generiert `presence_webhook_secret`.

### K7. EXTERNAL_URL / EXTERNAL_WS_URL undefined — FIXED
- **Dateien:** `.env.example`, `docker-compose.prod*.yml`
- **Fix:** Variablen sind dokumentiert in `.env.example` (Section "Frontend build-time variables") mit Beispiel-Werten + Erklaerung, dass es Vite-Build-Args sind, die in das PWA-Bundle einkompiliert werden. Kein Settings-Feld noetig (kein Backend-Consumer); leere Werte sind default-fallback fuer Same-Origin-Deploys hinter Nginx.

---

## WICHTIG (14) — ALL RESOLVED

All 14 WICHTIG items closed as of 2026-04-27. Re-verified 2026-04-30 against current code; the per-item notes below reference the actual landed solution rather than the original audit framing. Verification commands are included so a future sweep can re-check independently.

### W1. DB connection pool tuning — RESOLVED
- `services/database.py:18-21` reads `pool_size`, `max_overflow`, `pool_recycle` from Settings and sets `pool_pre_ping=True`. Settings fields with `Field(ge=…, le=…)` constraints live in `utils/config.py:59-61` (`db_pool_size`, `db_max_overflow`, `db_pool_recycle`).
- Verified via #486 audit-vs-current-code re-check; confirmed 2026-04-30.

### W2. IVFFlat → HNSW for vector indexes — RESOLVED (#485)
- HNSW migration shipped earlier; #485 cleared the doc rot in `docs/RAG.md` and removed a stale model comment. Production uses HNSW with `halfvec` cast for the 2560-dim qwen3 embeddings (see `MEMORY.md` § pgvector index limits).

### W3. Single-insert loop for document chunks — RESOLVED (#483)
- `services/rag_service.py:350` and `:549` use `db.add_all(chunk_objects)` / `db.add_all(parents)` for bulk insert instead of the per-chunk loop the audit flagged.

### W4. N+1 in conversation search — RESOLVED
- `services/conversation_service.py::search` (line ~515) now joins Conversation in the initial query: `.join(Conversation, Message.conversation_id == Conversation.id)` (line ~534). No per-match Conversation lookup.

### W5. Hardcoded timeouts across integrations — RESOLVED (#484)
- Timeouts pulled into Settings: `home_assistant_timeout`, `frigate_timeout`, `n8n_timeout`, etc. Integration files read from `settings`/`ha_glue_settings` instead of literals.

### W6. LLM options hardcoded in Python override YAML config — RESOLVED (#482)
- `services/agent_service.py` and `services/agent_router.py` read `temperature` / `top_p` / `num_predict` from `prompts/agent.yaml` via `prompt_manager` instead of hardcoding. Defaults remain in Settings (`agent_default_temperature`, `agent_default_num_predict`) for fallback.

### W7. Circuit breaker thresholds hardcoded — RESOLVED
- `utils/circuit_breaker.py:211-218` constructs `llm_circuit_breaker` and `agent_circuit_breaker` with `settings.cb_failure_threshold`, `settings.cb_llm_recovery_timeout`, `settings.cb_agent_recovery_timeout`. The constructor's literal defaults are only fallbacks for ad-hoc test instances.

### W8. Cache TTLs hardcoded (4 instances) — RESOLVED
- All four call sites now read from Settings:
  - `ha_glue/integrations/homeassistant.py:58` — `ha_glue_settings.ha_cache_ttl`
  - `ha_glue/services/satellite_update_service.py:39` — `ha_glue_settings.satellite_package_cache_ttl`
  - `services/intent_feedback_service.py:34` — `settings.intent_feedback_cache_ttl`

### W9. No React code splitting — RESOLVED
- `src/frontend/src/App.tsx:1` imports `Suspense, lazy` from React; lines 15-23+ lazy-load admin pages (TasksPage, CameraPage, HomeAssistantPage, SpeakersPage, RoomsPage, KnowledgePage, MemoryPage, UsersPage, RolesPage, …).

### W10. TypeScript coverage — RESOLVED (#487)
- Migrated all `.jsx`/`.js` under `src/frontend/src/` to `.tsx`/`.ts` with explicit types — no `as any`, no `@ts-nocheck`. Surfaced 9 real silent-failure bugs (Layout role render, Alert no-op `onClose` × 6 sites, multiple `confirmText` typos, `confirm()` called with positional string in 3 places, no-op `role` prop on Alert).

### W11. Prettier configured — RESOLVED
- `src/frontend/.prettierrc` + `src/frontend/.prettierignore` exist; `package.json` exposes `"format": "prettier --write \"src/**/*.{js,jsx,ts,tsx,css,json}\""`.

### W12. alembic.ini hardcoded credentials — RESOLVED
- `src/backend/alembic.ini:9` is now a placeholder URL (`postgresql+asyncpg://placeholder:placeholder@localhost/placeholder`); `src/backend/alembic/env.py:139-142` overrides `sqlalchemy.url` from `settings.database_url` at runtime. No real credentials in the ini file.

### W13. Config validation (ranges, formats) — RESOLVED (#484)
- `utils/config.py` has `Field(ge=…, le=…)` constraints on numeric thresholds (DB pool sizes, agent step counts, port range). `_CHANGEME_FIELDS` tuple drives a `warn_on_changeme_defaults()` validator that flags placeholder values in real environments.

### W14. Boolean naming consistency — RESOLVED
- 35 fields in `utils/config.py` use the canonical `_enabled: bool` suffix. Only 2 use `allow_` / `require_` (`allow_registration`, `require_email_verification`) — those are semantic English-grammar exceptions, not naming inconsistencies. The mixed-prefix problem the audit flagged is no longer present.

---

## EMPFEHLUNG (18)

### E1. Speaker embeddings fully loaded — RESOLVED
- Per-speaker embedding cap is enforced at write time: `whisper_service._add_embedding_to_speaker` (line 412-415) skips inserts once a speaker has 10 embeddings, so the DB never holds more than 10 per speaker via continuous learning.
- Read-side defensive slice at `whisper_service.py:281` (`MAX_EMBEDDINGS_PER_SPEAKER = 10`) still applies as a safety net for any speakers manually enrolled via `/api/speakers/{id}/embeddings` (which doesn't currently enforce the cap; speakers with hand-tuned multi-sample profiles are the edge case).
- Identification path at line 284 already gates on `if speaker.embeddings:` so empty speakers don't pollute the matcher list. The auto-enroll path (line 333-338) deliberately needs the full speaker list to count "Unbekannter Sprecher #N" — so a server-side filter would break that.
- **Marginal optimization deferred:** moving the per-speaker LIMIT into SQL via window function would save N×10 row hydration on each STT call. Low value at typical scale (≤20 speakers); revisit if STT latency becomes a bottleneck.

### E2. selectinload loads all documents per KB — RESOLVED
- `services/rag_service.py::list_knowledge_bases` (line 746-768) explicitly avoids `selectinload`, using a correlated count subquery to attach `_document_count` as a transient attribute on each KB. The audit's reference to line 686 was stale.

### E3. Missing FK indexes — RESOLVED
- All four FK columns the audit flagged carry `index=True`:
  - `Message.conversation_id` — `models/database.py:56`
  - `SpeakerEmbedding.speaker_id` — `models/database.py:107`
  - `User.role_id` — `models/database.py:552`
  - `RoomDevice.room_id` — `ha_glue/models/database.py:185`
- The migration `j0k1l2m3n4o5_add_fk_indexes_and_hnsw.py` explicitly creates `ix_speaker_embeddings_speaker_id` (line 32). Audit was stale.

### E4. MCP response size limit hardcoded (40KB) — RESOLVED (already)
- `services/mcp_client.py:72` reads `settings.mcp_max_response_size` (Setting added at `utils/config.py:164`, default raised to 128KB to accommodate real `list_correspondents` payloads). Audit's 40KB framing was stale.

### E5. MCP backoff constants hardcoded — RESOLVED
- 4 backoff constants in `services/mcp_client.py:76-79` now read from Settings (`mcp_backoff_initial_delay`, `mcp_backoff_max_delay`, `mcp_backoff_multiplier`, `mcp_backoff_jitter`). Module-level constants stay as the binding point so downstream `ExponentialBackoff(...)` callers don't need to change. Defaults match the previous values; ranges validated.

### E6. Agent history limit hardcoded (20 steps) — RESOLVED (already)
- `services/agent_service.py:132` reads `settings.agent_history_limit` (Setting at `utils/config.py:176`, range 1-100).

### E7. Agent response truncation limits — RESOLVED (already)
- `_truncate()` in `services/agent_service.py:286` and 2 other call sites read `settings.agent_response_truncation` (Setting at `utils/config.py:177`, range 100-50000).

### E8. Embedding dimension hardcoded (768) — RESOLVED (already)
- `EMBEDDING_DIMENSION = settings.embedding_dimension` at `models/database.py:216`; Setting at `utils/config.py:183` (range 128-4096). Used at all `Vector(EMBEDDING_DIMENSION)` declaration sites. Resize still requires a migration but the source-of-truth is configurable.

### E9. Similarity threshold inconsistency — RESOLVED (with re-classification)
- The two thresholds were intentionally different — 0.75 for general past-correction matching, 0.80 for the stricter "is this query simple or complex?" routing decision (fewer false positives wanted on complexity routing). The audit framed this as a unify-into-one fix; that would have collapsed two real decision bars.
- Fix: Both thresholds promoted to Settings (`intent_feedback_similarity_threshold` = 0.75, `intent_feedback_complexity_threshold` = 0.80). `find_similar_corrections(threshold=None)` falls back to the general bar; the complexity-routing call site explicitly passes the complexity bar. Operators can now tune recall/precision per environment.

### E10. Frontend hardcoded localhost fallbacks — RESOLVED
- New `src/frontend/src/utils/env.ts` centralizes the fallback with `getApiBaseUrl()` and `getWebSocketUrl()`. Both warn on console (error level in PROD builds, warn in DEV) when the env var is unset, and warn at most once per page load.
- All three call sites migrated: `utils/axios.ts:5`, `pages/ChatPage/hooks/useChatWebSocket.ts:141`, `hooks/useDeviceConnection.ts:170`. The `VITE_WS_URL` "includes-/ws" convention (per `.env.example` + 4 compose files) is preserved.

### E11. React Query / SWR for data fetching — RESOLVED (all 23 list-fetching surfaces migrated)
- #504 on 2026-04-30 landed `@tanstack/react-query` v5 with hardened defaults (mutations.retry: 0, queries.retry bails on 4xx, refetchOnWindowFocus: false), `src/api/queryClient.ts` + `keys.ts` (centralized factories with STALE.{LIVE,DEFAULT,CONFIG} taxonomy) + `hooks.ts` (`useApiQuery`/`useApiMutation` wrappers binding `extractApiError`/`extractFieldErrors` + i18n into RQ's surface).
- Provider order: `ErrorBoundary → ThemeProvider → AuthProvider → QueryClientProvider → DeviceProvider` so `AuthContext.tsx:226-263` interceptors install before any RQ fetcher fires.
- Reference pages from #504: `MemoryPage`, `RolesPage`, `IntentsPage`, `SettingsPage`, `MaintenancePage`.
- 2026-04-30 follow-up (this branch, 7 commits): migrated **17 additional pages + 4 components + useChatSessions** — Tasks, Brain, BrainReview, CirclesPeers, CirclesSettings, Integrations, FederationAudit, RoutingDashboard, Presence, Satellites, Camera, HomeAssistant, Users, Rooms, Knowledge, Speakers, KnowledgeGraph, plus RoomOutputSettings, DeviceSetup, LanguageSwitcher, AnalyticsTab. New resource files under `src/frontend/src/api/resources/`: `tasks.ts`, `brain.ts`, `circles.ts`, `federation.ts`, `integrations.ts`, `routing.ts`, `presence.ts`, `satellites.ts`, `cameras.ts`, `homeAssistant.ts`, `users.ts`, `rooms.ts`, `roomOutputs.ts`, `knowledge.ts`, `speakers.ts`, `knowledgeGraph.ts`, `preferences.ts`, `chatSessions.ts`. Existing spec suites stay green: 16 IntegrationsPage, 6 FederationAuditPage, 15 UsersPage, 21 of 22 RoomsPage (1 pre-existing dual-output failure), 17 of 19 SpeakersPage (2 pre-existing i18n mismatch failures), 25 LanguageSwitcher, 14 useChatSessions.
- Test-suite delta: 343 pass / 39 fail (vs 330/52 at this branch's start) — net +13 passing, no new regressions. All remaining failures are pre-existing and unrelated to E11.
- `useChatSessions`: rewritten on top of React Query at `src/frontend/src/api/resources/chatSessions.ts` with the public shape preserved exactly (`{ conversations, loading, error, refreshConversations, deleteConversation, loadConversationHistory, addConversation, updateConversationPreview }`). Mutations use `setQueryData` for optimistic add/update/delete. `src/frontend/src/hooks/useChatSessions.ts` is now a thin re-export shim so consumers (`ChatContext`, `ChatSidebar`) need no changes; `groupConversationsByDate` lives there.
- Convention: error fallbacks. `useApiQuery`/`useApiMutation` return server-side `detail` when present, falling back to the i18n key only when the server didn't supply one. Two existing tests (RoomsPage, SpeakersPage) sent `{ detail }` in 500 responses and expected the localized fallback — those were updated to send a null body so the fallback path actually runs (consistent with FederationAuditPage's pattern from #504).
- Out of scope (skipped intentionally): `useDocumentPolling.ts`, `useDocumentUpload.ts`, `useChatWebSocket.ts`, `AuthContext` core, `PairResponderModal`, `PairInitiatorModal`, `knowledge-graph/GraphView` — multi-step state machines, file-upload `onUploadProgress`, or WebSocket-driven update models that don't fit RQ's request/response shape.
- 2026-04-30 final: `PaperlessAuditPage` (1450 LOC, 7 tabs) migrated on its own branch `e11-paperless-audit`. Status query uses `refetchInterval` driven by the query's own `running` flag (polls 2s while running, idle otherwise — no manual setInterval). Review/OCR/Completeness tabs share the `/results` endpoint with distinct queryKey segments. Review search is debounced via a separate `debouncedSearch` state so the query key only changes after 300ms. Stats/DuplicateGroups/CorrespondentClusters gated by `activeTab`. 503 ("Paperless not configured") detection inspects each query's `AxiosError` so the page collapses to a banner when the integration isn't wired up.
- Plan: `~/.claude/plans/pure-questing-blanket.md` (eng review CLEAR + outside-voice addressed).

### E12. i18n: 13 hardcoded German strings — RESOLVED
- ErrorBoundary (5) and ConfirmDialog (4) — resolved during W10 TypeScript migration (#487); both files now use `useTranslation()` for every user-facing string.
- ChatMessages alt text (1) — `alt="Album Art"` → `t('chat.albumArt')`; new key added to both `de.json` (Albumcover) and `en.json` (Album art).
- German `console.error` logs (5 found, audit said 3) — converted to English in `CameraPage.tsx`, `HomeAssistantPage.tsx`, `TasksPage.tsx`. Logs are dev-facing, so English consistency with the rest of the codebase is the right fix (no i18n needed for dev logs).
- Out of E12 scope: `RoomOutputSettings.tsx` has its own substantial i18n debt (~10 hardcoded German strings); filed separately as it was not in the original audit list.

### E13. ChatPage prop drilling (12+ props) — RESOLVED
- `src/frontend/src/pages/ChatPage/context/ChatContext.tsx` is the shared Context provider; `ChatProvider` wraps `ChatPage` (`index.tsx:8`).
- `ChatInput` now takes **zero props** and pulls everything from `useChatContext()` — same for `ChatMessages`, `ChatHeader`, `AttachmentQuickActions`. The 12-prop drill the audit flagged is gone. Verified by reading `ChatInput.tsx` — destructures 16 fields directly from context.

### E14. ESLint React version hardcoded as 18.2 — RESOLVED (already)
- Verified during E17 sweep: `src/frontend/.eslintrc.cjs:22` already reads `version: 'detect'`. The audit's "hardcoded 18.2" claim was stale; nothing to change.

### E15. tsconfig strict mode disabled
- `tsconfig.json` has `strict: false`
- Fix: Enable strict mode gradually (start with new files)

### E16. Legacy config fields (dead code) — RESOLVED
- `plugins_enabled`, `plugins_dir`, `music_enabled`, `spotify_*` — already removed from `config.py` and `.env.example` in earlier work; no usages remained.
- `piper_voice` — renamed to `piper_default_voice` (env: `PIPER_DEFAULT_VOICE`) to make its role explicit: fallback voice when the requested language has no entry in `piper_voices`. Not dead code, just a misleading name.
- `ollama_model` — re-classified: NOT dead. It is the global model fallback referenced by 13+ services as `settings.X_model or settings.ollama_model` (chat_handler, knowledge_graph_service, kg_retrieval, conversation_memory_service, agent_service, agent_router, orchestrator, notification_service, federation_query_responder, paperless_audit_service, ollama_service, main health check). The original audit's claim that `ollama_chat_model` replaces it is wrong — the two coexist as fallback-chain roles.

### E17. Redis URL not parameterized in docker-compose — RESOLVED
- All 6 platform `REDIS_URL` instances across `docker-compose.yml` (2), `docker-compose.prod.yml` (2), `docker-compose.prod-cpu.yml` (1), `docker-compose.dev.yml` (1) now use `${REDIS_URL:-redis://redis:6379}`, matching the existing `OLLAMA_URL` pattern.
- Out of scope: Evolution-API's `CACHE_REDIS_URI: redis://redis:6379/3` (docker-compose.yml:267) keeps its service-specific DB-index suffix; parameterizing it as `${REDIS_URL:-…}/3` would break a future external-Redis deploy where `REDIS_URL` already includes a DB index. If that ever lands, introduce a dedicated `EVOLUTION_REDIS_URI` setting.
- Out of scope: `k8s/configmap.yaml:11` keeps `redis://redis:6379` hardcoded — k8s configmaps are environment-specific by convention; override per environment if needed.
- Out of scope: `.github/workflows/ci.yml` uses `redis://localhost:6379` for GitHub Actions service containers, which is correct for that runtime.

### E18. Frigate MQTT defaults hardcoded — RESOLVED
- New Settings fields `frigate_mqtt_broker` (default `"localhost"`) and `frigate_mqtt_port` (default `1883`, range 1-65535) in `ha_glue/utils/config.py`.
- `FrigateClient.setup_mqtt()` defaults its arguments to `None` and falls back to those settings; explicit args still override. Documented in `.env.example` and `docs/ENVIRONMENT_VARIABLES.md`.

---

## GUT UMGESETZT (12)

- [x] Pydantic Settings with `secrets_dir="/run/secrets"` for production
- [x] Docker Secrets for 10 sensitive values in prod compose
- [x] `expire_on_commit=False` in async session factory
- [x] All useEffect hooks have proper cleanup functions
- [x] 3 well-designed Context providers (Theme, Auth, Device)
- [x] ErrorBoundary component with dev-only stack traces
- [x] PWA with service worker, offline support, OWASP security headers
- [x] Tailwind v4 with custom utility classes (btn, card, input)
- [x] i18n with react-i18next (German + English)
- [x] Custom hooks well-structured (useDeviceConnection, useWakeWord, etc.)
- [x] MCP backoff with exponential backoff + jitter
- [x] Hybrid RAG search with configurable BM25/Dense weights

---

## Priorisierte Roadmap

### Phase 1: Performance & Security (DB + Config)
- [x] K1: verified pre-existing fix (batched user lookup in list_kb_permissions)
- [x] K2: batched permission lookup in list_knowledge_bases
- [x] K3: verified pre-existing fix (single-query list_all with window function)
- [x] K5: SecretStr for sensitive Settings fields (presence_webhook_secret closed the last gap)
- [x] K6: complete Docker Secrets (jellyfin_user_id, presence_webhook_secret)
- [x] K7: define EXTERNAL_URL / EXTERNAL_WS_URL in .env.example
- [x] W1: DB connection pool — `pool_size`/`max_overflow`/`pool_recycle`/`pool_pre_ping` from Settings in `services/database.py:18-21`
- [x] W12: alembic.ini placeholder + runtime override from `settings.database_url` in `alembic/env.py:139-142`

### Phase 2: Konfiguration aufraumen
- [x] K4: Expand .env.example to 100% coverage
- [x] W5: Hardcoded timeouts to Settings (#484)
- [x] W6: Agent/Router LLM options from YAML, not Python hardcodes (#482)
- [x] W7-W8: Circuit breaker thresholds + cache TTLs read from Settings
- [x] W13-W14: `Field(ge/le=…)` constraints + `warn_on_changeme_defaults` validator (#484); 35 `_enabled` bool fields, 2 grammar-justified `allow_/require_` exceptions

### Phase 3: DB Optimierung
- [x] W2: HNSW indexes shipped earlier; #485 cleared the doc rot
- [x] W3: `db.add_all()` bulk insert in `rag_service.py:350,549` (#483)
- [x] W4: `search()` joins Conversation in initial query — `conversation_service.py:534`
- [x] E1-E3: Speaker per-speaker embedding cap on write + read-side gate; KB listing uses count subquery; all 4 FK columns carry `index=True` — verified in current code, audit was partially stale

### Phase 4: Frontend Modernisierung
- [x] W9: `React.lazy` + `Suspense` for admin pages — `App.tsx:1,15-23+`
- [x] W10: TypeScript migration — 100% `.tsx`/`.ts` coverage in `src/frontend/src/` (#487)
- [x] W11: `.prettierrc` + `.prettierignore` + `format` script in `package.json`
- [x] E11: React Query for data fetching — all 23 list-fetching surfaces migrated (#504 reference pages + e11-react-query bulk + e11-paperless-audit final)
- [x] E12: i18n hardcoded strings — ErrorBoundary/ConfirmDialog cleared by W10; ChatMessages alt + 5 dev logs translated; RoomOutputSettings filed as separate follow-up
- [x] E14: ESLint React version — verified `'detect'` already in `.eslintrc.cjs:22` (audit was stale)

### Phase 5: Cleanup
- [x] E16: Legacy config fields — `plugins_*`/`music_enabled`/`spotify_*` already gone; `piper_voice` renamed to `piper_default_voice`; `ollama_model` re-classified as intentional fallback infrastructure (not dead)
- [x] E4-E9: Hardcoded values to Settings — E4/E6/E7/E8 already done in earlier work; E5 (MCP backoff) + E9 (intent feedback thresholds, both bars) closed
- [x] E10: Frontend localhost fallbacks centralized in `utils/env.ts` with PROD/DEV warnings
- [x] E17: Redis URL parameterization — 6 platform compose entries use `${REDIS_URL:-redis://redis:6379}`
- [x] E18: Frigate MQTT broker/port from Settings — defensive hygiene before MQTT consumer ships
- [x] E13: ChatPage prop drilling — ChatProvider wraps the page, ChatInput takes 0 props (verified, audit was stale)
- [ ] E15: Enable TypeScript strict mode (~31 errors mostly null-checks; deferred to dedicated session)
- [x] E11: React Query for data fetching — all 23 list-fetching surfaces migrated (#504 reference pages + e11-react-query bulk + e11-paperless-audit final)
