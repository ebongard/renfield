# Großen LLM-Kontext (256k llama-server) ausnutzen — feat/exploit-large-llm-context

Plan: ~/.claude/plans/buzzing-crafting-meerkat.md (genehmigt, Zuschnitt: Moderat) · Issue #1103 · Follow-ups #1104

- [x] 1-8. Implementierung + Tests + Doku (Commit `bac65900`)
- [x] 9. Suite auf .159 grün (5998 → nach Review-Fixes 6001 passed, 0 failed); Lint-Diff gegen main: 0 neue Verstöße
- [x] 10. Commit `bac65900` (Feature) 
- [x] 11. /review high abgearbeitet (2. Commit mit Review-Fixes):
  - F2 CONFIRMED: `AGENT_RESPONSE_TRUNCATION=32000` in beide ConfigMaps (Text-Ergebnisse wurden sonst schon bei Step-Erzeugung auf 2000 gekappt → Lese-Cap inert)
  - F4 CONFIRMED: Pass-0-Budget (`tool_result_budget_chars`) greift jetzt auch auf TEXT-Ergebnisse (min mit `agent_tool_result_text_max_chars`); Pass-4-500er-Floor als bewusster Notfall-Crush dokumentiert
  - F5 CONFIRMED: Fallback-Oversize-Warnung keyt auf das tatsächlich geforwardete `options.num_ctx` + content-aware `token_counter` statt `ollama_num_ctx*3` (beide Fehlrichtungen behoben)
  - F7 CONFIRMED: `llm_openai_num_ctx` mit Field-Bounds (ge=1024) — negativer Wert kann Budget nicht mehr still deaktivieren
  - F9/F10 cleanup: gemeinsames `_prepare_fallback`, toter `max_chars`-Param entfernt, loop-invariante Settings-Reads gehoistet
  - F3 PLAUSIBLE: token_counter sampelt Head+Tail (Code-Tail → konservative 3.0 statt German 4.5 → kein 33%-Undercount am Budget-Rand)
  - F1/F6/F8 PLAUSIBLE (bewusst vertagt, dokumentiert): Follow-up-Issue #1104 (Fallback-seitige Re-Reduktion, Ollama-Primary num_ctx-Forwarding, Soft-Prompt-Target); F6 zusätzlich als KNOWN GAP im Code kommentiert
  - Refuted: V3 (multimodal), V12 (tool_calls), Timing-Flake
- [ ] 12. Push/PR/Merge — NUR nach expliziter Freigabe
- [ ] 13. Deploy nach Freigabe (`bin/deploy-production.sh`, Backend-Image beide Instanzen + ConfigMap-Apply, keine Migration) + Browser-E2E + Log-Nachweis `Token budget: N/262144`

## Review
- Suite-Läufe isoliert auf .159: non-PG 5647 passed / 382 skipped, PG-markiert 354 passed / 12 skipped — 0 Failures (beide Commits).
- Ruff-Diff main↔branch nach beiden Commits: 0 neue Verstöße.
- Bewusste Tradeoffs: Fallback bleibt bei 32k (VRAM-Schutz, Outage = best-effort mit korrekter Warnung); Prefill-Latenz durch moderate Caps begrenzt, Soft-Target als Follow-up.
