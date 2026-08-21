# Großen LLM-Kontext (256k llama-server) ausnutzen — feat/exploit-large-llm-context

Plan: ~/.claude/plans/buzzing-crafting-meerkat.md (genehmigt, Zuschnitt: Moderat)

- [ ] 1. Neue Settings in `utils/config.py` (`llm_openai_num_ctx`, `agent_history_message_max_chars`, `agent_tool_result_text_max_chars`, `knowledge_context_chunk_chars`)
- [ ] 2. `effective_agent_num_ctx()` in `utils/llm_client.py` + Budget-Umstellung in `agent_service._enforce_token_budget`
- [ ] 3. Content-Caps auf Settings umstellen (`_compress_history_message`, `step.content[:8000]`, knowledge_tool `[:500]`) + stale Kommentare
- [ ] 4. Fallback-Oversize-WARNING in `_OpenAICompatFallbackClient`
- [ ] 5. token_counter `_detect_content_type` Sampling-Fix (4096-Zeichen-Sample)
- [ ] 6. ConfigMaps beide Instanzen (`k8s/configmap.yaml`, `k8s/xidra/renfield-env.configmap.yaml`)
- [ ] 7. Tests: test_token_budget (Mocks + neue Fälle), test_llm_client (Fallback-Warnung), Caps-Tests, test_token_counter (Sampling); test_routine_agent Mock ergänzen
- [ ] 8. Doku: ENVIRONMENT_VARIABLES.md + .env.example
- [ ] 9. /verify-tests auf .159 grün
- [ ] 10. /review + Docs-Sweep + Commit (kein Push ohne Freigabe)

## Review
(wird nach Abschluss ergänzt)
