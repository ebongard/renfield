# Twin-Härtung + #1104-Follow-ups + Metriken — feat/twin-hardening-ctx-followups

Plan: ~/.claude/plans/buzzing-crafting-meerkat.md (genehmigt; anon-Policy=Auto via AUTH_ENABLED, B3=jetzt)

- [ ] C. Metriken: METRICS_ENABLED beide ConfigMaps + Endpoint-Label-Normalisierung (Kardinalität) + Test
- [ ] B1. Ollama-Primary num_ctx-Forwarding in `_llm_options_or_default` + Test
- [ ] B2. Soft-Prompt-Target `AGENT_PROMPT_TARGET_TOKENS` (Pass-0-only-Zweig) + ConfigMaps 65536 + Tests
- [ ] A1. Guard bedingungslos registrieren + Prefix-Match `TWIN_MCP_BINDING` (Adapter, Privat-Repo)
- [ ] A1b. `k8s/backend.yaml`: TWIN_INGEST_TOKEN aus Secret twin-secrets `optional: true` + twin/DEPLOY.md
- [ ] A2. Host: pre_mcp_call Chaining statt First-Dict-wins (action_executor + hooks-Doku + Test-Umbau)
- [ ] A3. anon-Policy Auto via AUTH_ENABLED + TWIN_ANON_POLICY-Override (Adapter)
- [ ] A4. Shared httpx-Client + TWIN_RECALL_TIMEOUT (1s) + Cooldown-Latch (Adapter)
- [ ] A5. lang durchreichen (chat_handler Pfad A + Adapter build_event)
- [ ] A6. Adapter-Tests (Privat-Repo) + TWIN_*-Doku in ENVIRONMENT_VARIABLES.md + Docstring-Fixes
- [ ] B3. Middle-Cut-Utility + Fallback-seitige Prompt-Kürzung in _prepare_fallback + Tests
- [ ] Re-Stage Adapter kanonisch → src/backend/twin_adapter/
- [ ] Suite .159 (voll + PG) + Adapter-Tests + Lint-Diff
- [ ] /review high → Fixes → Push/PR/Merge NUR nach Freigabe → Deploy + Smoke inkl. Metrics-Nachweis

## Review
(wird ergänzt)
