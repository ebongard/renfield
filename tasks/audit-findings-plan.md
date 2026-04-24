# Audit Findings & Improvement Plan

Consolidated results from 4 systematic audits (DB Performance, Config Hardcodes, Config Architecture, Frontend Modernization).

---

## Severity Overview

| Severity | Count | Category |
|----------|-------|----------|
| KRITISCH | 7 | Must fix — performance bottlenecks, security gaps |
| WICHTIG | 14 | Should fix — inconsistencies, missing optimizations |
| EMPFEHLUNG | 18 | Nice to have — modernization, cleanup |
| GUT | 12 | Already well-implemented |

---

## KRITISCH (7)

### K1. N+1 Query: KB Permissions Listing
- **Datei:** `api/routes/knowledge.py:760-790`
- **Problem:** Loop queries User table per KBPermission (2 queries per iteration)
- **Fix:** Use `selectinload(KBPermission.user)` or explicit JOIN
- **Impact:** 10 permissions = 20 extra queries

### K2. N+1 Query: KB Access Check
- **Datei:** `api/routes/knowledge.py:487-503`
- **Problem:** Queries KBPermission table per KB in loop
- **Fix:** Batch-load all user permissions in one query upfront
- **Impact:** 50 KBs = 50 extra queries

### K3. N+1 Query: Conversation List
- **Datei:** `services/conversation_service.py:254-268`
- **Problem:** 2 queries per conversation (count + first message)
- **Fix:** Window functions or aggregation in single query
- **Impact:** 50 conversations = 100 extra queries

### K4. .env.example covers only 17% of Settings
- **Datei:** `.env.example` (22 of 126 variables documented)
- **Problem:** New deployments lack guidance for 104 configuration options
- **Fix:** Expand .env.example with all variables, grouped and commented

### K5. No SecretStr for sensitive fields
- **Datei:** `utils/config.py`
- **Problem:** Passwords, tokens, API keys typed as `str` — visible in logs/repr
- **Fix:** Change to `SecretStr` for postgres_password, secret_key, all tokens

### K6. Production Docker Secrets incomplete
- **Datei:** `docker-compose.prod.yml`
- **Problem:** Email passwords, JELLYFIN_USER_ID, SEARXNG credentials missing from secrets
- **Fix:** Add all sensitive values to Docker Secrets

### K7. EXTERNAL_URL / EXTERNAL_WS_URL undefined
- **Datei:** `docker-compose.prod.yml`
- **Problem:** Frontend references `${EXTERNAL_URL}` but it's never defined anywhere
- **Fix:** Document in .env.example, add to Settings class

---

## WICHTIG (14)

### W1. No DB connection pool tuning
- **Datei:** `services/database.py`
- **Problem:** Uses SQLAlchemy defaults (pool_size=5, no pool_recycle, no pool_pre_ping)
- **Fix:** Add configurable pool_size, max_overflow, pool_recycle, pool_pre_ping

### W2. IVFFlat instead of HNSW for vector indexes
- **Datei:** `models/database.py`, Alembic migrations
- **Problem:** IVFFlat has lower recall than HNSW for similarity search
- **Fix:** Migration to switch to HNSW (m=16, ef_construction=64)

### W3. Single-insert loop for document chunks
- **Datei:** `services/rag_service.py:147-172`
- **Problem:** Adds chunks one-by-one in loop (500-1000 INSERTs per document)
- **Fix:** Collect all chunks and use `bulk_insert_mappings()` or `insert().values()`

### W4. N+1 in conversation search
- **Datei:** `services/conversation_service.py:314-337`
- **Problem:** Queries Conversation per match after message search
- **Fix:** JOIN Conversation in initial message query

### W5. 23 hardcoded timeouts across integrations
- **Dateien:** `integrations/homeassistant.py`, `frigate.py`, `n8n.py`, `internal_tools.py`
- **Problem:** Timeouts (5s, 10s, 15s, 30s) hardcoded in each integration
- **Fix:** Add HA_TIMEOUT, FRIGATE_TIMEOUT, N8N_TIMEOUT to Settings

### W6. LLM options hardcoded in Python override YAML config
- **Dateien:** `services/agent_service.py:529-532`, `services/agent_router.py:219`
- **Problem:** temperature, top_p, num_predict hardcoded in Python; YAML prompt config ignored
- **Fix:** Read LLM options from prompt_manager instead of hardcoding

### W7. Circuit breaker thresholds hardcoded
- **Datei:** `utils/circuit_breaker.py:189-202`
- **Problem:** failure_threshold=3, recovery_timeout=30/60 not configurable
- **Fix:** Add CIRCUIT_BREAKER_* settings

### W8. Cache TTLs hardcoded (4 instances)
- **Dateien:** `homeassistant.py:21,33`, `satellite_update_service.py:37`, `intent_feedback_service.py:34`
- **Problem:** TTLs (60s, 300s) hardcoded, can't tune without code change
- **Fix:** Add to Settings or use unified cache config

### W9. No React code splitting
- **Datei:** `src/frontend/src/App.jsx`
- **Problem:** All 14 pages loaded eagerly — no React.lazy/Suspense
- **Fix:** Lazy-load admin pages (users, roles, settings, satellites, integrations, intents)

### W10. TypeScript coverage only 23%
- **Datei:** Frontend src/
- **Problem:** 38 of 52 files are .jsx/.js (no type safety)
- **Fix:** Gradual migration, start with pages that handle complex state

### W11. No Prettier configured
- **Datei:** Frontend root
- **Problem:** No .prettierrc — formatting inconsistencies
- **Fix:** Add Prettier config + pre-commit hook

### W12. alembic.ini has hardcoded credentials
- **Datei:** `src/backend/alembic.ini:9`
- **Problem:** `changeme` password hardcoded, breaks if password differs
- **Fix:** Use env var substitution or generate from config.py

### W13. No config validation (ranges, formats)
- **Datei:** `utils/config.py`
- **Problem:** No Field(ge=, le=) constraints, no URL validation, no "changeme" detection
- **Fix:** Add Pydantic validators for thresholds (0-1), URLs, required secrets

### W14. Inconsistent boolean naming
- **Datei:** `utils/config.py`
- **Problem:** Mix of `_ENABLED`, `ALLOW_`, `REQUIRE_`, `AUTO_` for boolean fields
- **Fix:** Standardize to `{FEATURE}_ENABLED` where possible

---

## EMPFEHLUNG (18)

### E1. Speaker embeddings fully loaded
- `services/whisper_service.py:269` — loads ALL speakers with ALL embeddings every call
- Fix: Filter by active speakers, limit embeddings per speaker

### E2. selectinload loads all documents per KB
- `services/rag_service.py:686` — eager-loads all documents when listing KBs
- Fix: Remove selectinload, use lazy or count-only

### E3. Missing FK indexes
- `Message.conversation_id`, `SpeakerEmbedding.speaker_id`, `User.role_id`, `RoomDevice.room_id`
- Fix: Add `index=True` to FK columns (or verify implicit indexes exist)

### E4. MCP response size limit hardcoded (40KB)
- `services/mcp_client.py:42` — MAX_MCP_RESPONSE_SIZE = 40 * 1024
- Fix: Add to Settings

### E5. MCP backoff constants hardcoded
- `services/mcp_client.py:46-49` — 4 backoff constants
- Fix: Add to Settings (or keep as sensible defaults)

### E6. Agent history limit hardcoded (20 steps)
- `services/agent_service.py:99` — max agent history in prompt
- Fix: Derive from agent_max_steps or add setting

### E7. Agent response truncation limits
- `services/agent_service.py:203` — 2000 char truncation limit
- Fix: Add to Settings

### E8. Embedding dimension hardcoded (768)
- `models/database.py:433` — can't change without migration
- Fix: Document dependency on nomic-embed-text, or make configurable with migration

### E9. Similarity threshold inconsistency
- `intent_feedback_service.py:133` uses 0.75 (param), line 266 uses 0.80 (hardcoded)
- Fix: Unify to single configurable threshold

### E10. Frontend hardcoded localhost fallbacks
- `utils/axios.ts:5`, `useChatWebSocket.js:34`, `useDeviceConnection.ts:171`
- Fix: Add validation/warning when VITE_API_URL not set

### E11. React Query / SWR for data fetching
- All pages use raw `apiClient.get()` + `useState` + `setLoading`
- Fix: Adopt React Query for caching, deduplication, error retry

### E12. i18n: 13 hardcoded German strings
- `ErrorBoundary.jsx` (5), `ConfirmDialog.jsx` (4), `ChatMessages.jsx` (1), logs (3)
- Fix: Move to i18n translation files

### E13. ChatPage prop drilling (12+ props)
- `ChatPage/index.jsx` passes 12 props to ChatInput
- Fix: Extract chat input state into Context

### E14. ESLint React version hardcoded as 18.2
- `.eslintrc.cjs` says 18.2 but React 19.2.3 is installed
- Fix: Update to `detect` or `19`

### E15. tsconfig strict mode disabled
- `tsconfig.json` has `strict: false`
- Fix: Enable strict mode gradually (start with new files)

### E16. Legacy config fields (dead code)
- `ollama_model` (replaced by ollama_chat_model), `piper_voice` (replaced by piper_voices)
- `plugins_enabled`, `plugins_dir`, `music_enabled`, `spotify_*` (MCP replaced plugins)
- Fix: Deprecate and remove in next major version

### E17. Redis URL not parameterized in docker-compose
- `docker-compose.yml:77` — hardcoded `redis://redis:6379`
- Fix: Use `${REDIS_URL:-redis://redis:6379}`

### E18. Frigate MQTT defaults hardcoded
- `integrations/frigate.py:74` — broker="localhost", port=1883
- Fix: Pull from Settings

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
- [ ] K1-K3: Fix N+1 queries (knowledge.py, conversation_service.py)
- [ ] K5: SecretStr for sensitive Settings fields
- [ ] K6-K7: Complete Docker Secrets + define EXTERNAL_URL
- [ ] W1: Configure DB connection pool
- [ ] W12: Fix alembic.ini credentials

### Phase 2: Konfiguration aufraumen
- [ ] K4: Expand .env.example to 100% coverage
- [ ] W5: Extract hardcoded timeouts to Settings
- [ ] W6: Agent/Router LLM options from YAML, not Python hardcodes
- [ ] W7-W8: Circuit breaker + cache TTLs configurable
- [ ] W13-W14: Validation + naming consistency

### Phase 3: DB Optimierung
- [ ] W2: Migrate IVFFlat → HNSW indexes
- [ ] W3: Bulk insert for document chunks
- [ ] W4: Fix conversation search N+1
- [ ] E1-E3: Speaker loading, eager load cleanup, FK indexes

### Phase 4: Frontend Modernisierung
- [ ] W9: React.lazy code splitting for admin pages
- [ ] W10: Gradual TypeScript migration (strict mode)
- [ ] W11: Add Prettier
- [ ] E11: React Query for data fetching
- [ ] E12: i18n hardcoded strings
- [ ] E14: ESLint React version

### Phase 5: Cleanup
- [ ] E16: Remove legacy config fields
- [ ] E4-E9: Remaining hardcoded values to Settings
- [ ] E13: ChatPage prop drilling → Context
- [ ] E15: Enable TypeScript strict mode
