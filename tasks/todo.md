# Twin-Härtung + #1104-Follow-ups + Metriken — feat/twin-hardening-ctx-followups

Plan: ~/.claude/plans/buzzing-crafting-meerkat.md (genehmigt; anon-Policy=Auto via AUTH_ENABLED, B3=jetzt)

- [x] C. Metriken: METRICS_ENABLED beide ConfigMaps + Endpoint-Label-Normalisierung (Kardinalität) + Test
- [x] B1. Ollama-Primary num_ctx-Forwarding in `_llm_options_or_default` + Test
- [x] B2. Soft-Prompt-Target `AGENT_PROMPT_TARGET_TOKENS` (Pass-0-only-Zweig) + ConfigMaps 65536 + Tests
- [x] A1. Guard bedingungslos registrieren + Prefix-Match `TWIN_MCP_BINDING` (Adapter, Privat-Repo)
- [x] A1b. `k8s/backend.yaml`: TWIN_INGEST_TOKEN aus Secret twin-secrets `optional: true` + twin/DEPLOY.md
- [x] A2. Host: pre_mcp_call Chaining statt First-Dict-wins (action_executor + hooks-Doku + Test-Umbau)
- [x] A3. anon-Policy Auto via AUTH_ENABLED + TWIN_ANON_POLICY-Override (Adapter)
- [x] A4. Shared httpx-Client + TWIN_RECALL_TIMEOUT (1s) + Cooldown-Latch (Adapter)
- [x] A5. lang durchreichen (chat_handler Pfad A + Adapter build_event)
- [x] A6. Adapter-Tests (Privat-Repo) + TWIN_*-Doku in ENVIRONMENT_VARIABLES.md + Docstring-Fixes
- [x] B3. Middle-Cut-Utility + Fallback-seitige Prompt-Kürzung in _prepare_fallback + Tests
- [x] Re-Stage Adapter kanonisch → src/backend/twin_adapter/
- [x] Suite .159 (5686 + 355 PG passed, 0 failed; Adapter 35 passed; Lint-Diff 0 neu) (voll + PG) + Adapter-Tests + Lint-Diff
- [ ] /review high → Fixes → Push/PR/Merge NUR nach Freigabe → Deploy + Smoke inkl. Metrics-Nachweis

## Review
- Renfield-Commit b6edd7cc (Branch feat/twin-hardening-ctx-followups) · Twin-Repo-Commit 85362d0 (lokal)
- /review high läuft
