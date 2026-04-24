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
- [x] K1: verified pre-existing fix (batched user lookup in list_kb_permissions)
- [x] K2: batched permission lookup in list_knowledge_bases
- [x] K3: verified pre-existing fix (single-query list_all with window function)
- [x] K5: SecretStr for sensitive Settings fields (presence_webhook_secret closed the last gap)
- [x] K6: complete Docker Secrets (jellyfin_user_id, presence_webhook_secret)
- [x] K7: define EXTERNAL_URL / EXTERNAL_WS_URL in .env.example
- [ ] W1: Configure DB connection pool
- [ ] W12: Fix alembic.ini credentials

### Phase 2: Konfiguration aufraumen
- [x] K4: Expand .env.example to 100% coverage
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
