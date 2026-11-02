# Reva → Renfield Backport-Analyse

**Datum:** 2026-03-23
**Kontext:** Reva ist ein Renfield-Plugin für MS Teams + Digital.ai Release Management. Es nutzt Renfield als Git-Submodule und hat dabei zahlreiche Features im Plugin gebaut, die in den Renfield-Core gehören.

## Submodule-Status

Reva's Renfield-Submodule ist auf Branch `feat/request-tracing-and-prompt-rules` mit **1 ungemergtem Commit**:

- `fdc344c feat(router): dedicated agent_router_model and agent_router_url settings`

Alle anderen Feature-Branch-Commits sind bereits in Renfield main gemergt.

---

## PRIO 1 — Sofort uebernehmen (hoher Wert, niedriger Aufwand)

### 1. Input Guard / Prompt Injection Defense

**Quelle:** `reva/src/reva/input_guard.py` (335 Zeilen, 100% generisch)

Renfield hat aktuell **keine** Input-Sanitization. 6 offene Vulnerability-Klassen:

| Vulnerability | Risiko | Reva-Loesung |
|---|---|---|
| Format String Injection (`{}`in User-Input) | MITTEL | `sanitize_user_input()` escaped `{}`→`{{}}` |
| XML Tag Injection (`</memory_context>`) | MITTEL | Strippt Delimiter-Tags |
| Role Marker Injection (`System:` am Zeilenanfang) | MITTEL | Neutralisiert zu `[User said "System:"]` |
| Instruction Override ("ignore all rules") | MITTEL | Gewichtetes Pattern-Scoring, Block bei Score >= 0.8 |
| System Prompt Leakage | MITTEL | Output Guard (siehe Prio 2) |
| Memory Poisoning | MITTEL | Memory Guard (siehe Prio 2) |

**Injection Detection:** 18 Regex-Patterns in 5 Kategorien mit gewichtetem Scoring:

| Kategorie | Gewicht | Beispiel-Patterns |
|---|---|---|
| Instruction Override | 0.8 | "ignore all previous instructions", "vergiss deine Regeln" |
| System Prompt Extraction | 0.7 | "repeat your system prompt", "zeige deine Anweisungen" |
| Role Impersonation | 0.6 | "you are now", "du bist jetzt ein" |
| GDPR Bypass | 0.9 | "ignore data protection", "DSGVO gilt nicht" |
| Delimiter Injection | 0.5 | 3+ strukturelle Delimiter oder XML-Tags wie `</system>` |

**Aufwand:** Direkt portierbar. Keine Reva-Dependencies. Nur `re` + `loguru`.

### 2. MCP Response Compaction

**Quelle:** `reva/src/reva/mcp_compact.py` (159 Zeilen, 100% generisch)

Renfield hat den Hook `compact_mcp_result` bereits registriert, aber **keine Compaction-Engine**.

**Was Reva hat:**
- YAML-basierte Field-Whitelists pro Tool (`config/mcp_compact.yaml`)
- Recursive Field-Tree-Filter mit Array-Support (`phases[].tasks[].status`)
- Namespaced Tool-Name-Resolution (`mcp.release.get_release` → server + tool)

**Warum wichtig:** MCP-Responses koennen 50KB+ sein. Mit qwen3:14b (8K context) frisst ein einziger grosser Tool-Call 60% des Budgets. Compaction reduziert auf 2-5KB.

**Config-Format:**
```yaml
home_assistant:
  get_states:
    - entity_id
    - state
    - attributes.friendly_name
    - last_changed

paperless:
  search_documents:
    - id
    - title
    - correspondent
    - tags[].name
    - created_date
```

### 3. Dedicated Router Model/URL Settings

**Quelle:** Ungemergter Commit `fdc344c` im Submodule (2 Dateien, 11 Zeilen Diff)

Trennt Router-Klassifikation sauber von Agent- und Intent-Model:
- `agent_router_model: str | None` — explizites Router-Model (default: `ollama_intent_model`)
- `agent_router_url: str | None` — explizite Ollama-URL fuer Router

**Aufwand:** Cherry-pick.

---

## PRIO 2 — Uebernehmen (hoher Wert, mittlerer Aufwand)

### 4. In-Memory Metrics Registry

**Quelle:** `reva/src/reva/metrics.py` (714 Zeilen)

Renfield hat basic Prometheus-Metrics (HTTP, WebSocket, LLM, Circuit Breaker). Reva hat ein komplettes Observability-System mit 19 Metrik-Kategorien.

**Generische Metriken fuer Renfield-Core:**

| Metrik | Beschreibung |
|---|---|
| Request Duration (p50/p95/p99) | End-to-End Latenz |
| Agent Loop Steps / Tool Calls | Agent-Effizienz |
| LLM Latency + Token Usage | Model-Performance |
| MCP Tool Duration per Server | Tool-Performance |
| Outcome Tracking | success / error / max_steps_abort |
| Per-Tool Success/Failure | Welche Tools scheitern am meisten |
| Apology/Failure-Phrase Detection | Erkennt Entschuldigungen in Bot-Antworten |
| DAU (Daily Active Users) | Nutzung |
| Conversation Depth | Gespraechstiefe |
| TTFR (Time to First Response) | Antwortzeit |
| Error Categories | mcp_timeout / llm_error / auth_error |
| Token Budget Utilization | Prompt-Budget-Auslastung |
| Security Metrics | Injection Attempts, Memory Poisoning Blocks |
| Bot Experience Score (0-100) | Composite KPI |

**Exzellente Zero-Dependency Primitives:**
- `Counter` — Labeled monotonic counters
- `Histogram` — Rolling-window Percentile-Berechnung
- `RateTracker` — Sliding-window Event-Counts
- `DailyUniqueTracker` — DAU mit Midnight-Reset

**Output-Formate:** `to_json()` + `to_prometheus()` (Exposition-Format mit HELP/TYPE)

### 5. Token Budget Enforcement

**Quelle:** Reva's Fork von `agent_service.py` → `_enforce_token_budget()`

Renfield hat keine Budget-Kontrolle. Reva implementiert progressive Reduktion:

```
Pass 0: Adaptive Tool Result Budgeting
  → Prompt-Skeleton messen, verbleibendes Token-Budget berechnen
  → tool_result_budget_chars pro Result setzen

Pass 1-3: Progressive Fallback (wenn immer noch ueber Budget)
  1. Conversation History halbieren (letzte 3 Messages behalten)
  2. Memory Context droppen
  3. Document Context droppen
  → Stoppen sobald utilization <= threshold (default 85%)
```

**Warum wichtig:** Ohne dies crashed der Agent bei grossen MCP-Responses oder langen Conversations.

### 6. Tool Pre-Selection

**Quelle:** `reva/src/reva/teams_transport.py` → `_preselect_tools()`

Bei 6+ registrierten Tools: LLM-Vorabauswahl (3-5 relevante Tools) vor dem Agent-Loop.

- LLM-Call mit `temperature=0, num_predict=120`, Timeout 10s
- Filtert `AgentToolRegistry._tools` in-place
- Graceful Fallback auf alle Tools bei Fehler
- Reduziert Halluzinationen + Token-Budget

**Renfield-Relevanz:** Mit 8+ MCP-Servern (HA, Frigate, n8n, SearXNG, Jellyfin, Paperless, Email, Calendar) hat Renfield dasselbe Problem.

### 7. Output Guard

**Quelle:** `reva/src/reva/output_guard.py` (196 Zeilen)

Prueft Agent-Antworten auf:

| Violation | Pattern | Portierbar? |
|---|---|---|
| System Prompt Leakage | >= 3 Fragmente aus System-Prompt erkannt | Ja (Fragment-Liste anpassen) |
| Role Confusion | "as you instructed me to ignore..." | Ja (100% generisch) |
| Individuelle Aktivitaetszuordnung | Name + Aktionsverb (DE/EN) | Enterprise-Feature |
| Performance-Vergleich | Name + Komparativ | Enterprise-Feature |

**Fuer Renfield relevant:** System Prompt Leakage Detection + Role Confusion Detection.

### 8. Memory Poisoning Defense

**Quelle:** `reva/src/reva/memory_service.py`

Renfield extrahiert Memories aus **allen** Conversations — auch aus transaktionalen Queries ("zeige Releases") und Injection-Versuchen.

**Reva's 3-stufige Filterung:**

```python
should_extract_memories(user_msg, assistant_response):
    # 1. BLOCK: Injection-Patterns erkannt
    #    "ignore rules", "ich bin admin", "bypass auth", "neue Anweisungen"

    # 2. ALLOW: Memorable Patterns
    #    "I am...", "my name is...", "I prefer...", "remember..."

    # 3. SKIP: Transactional Queries
    #    "show", "list", "what is status", "turn on light"

    # 4. Default: LLM entscheidet
```

---

## PRIO 3 — Evaluieren (hoher Wert, hoher Aufwand)

### 9. Cross-MCP Query Orchestrator

**Quelle:** `reva/src/reva/teams_transport.py` → `_detect_multi_role()` + `_run_orchestrated()`

Multi-Domain-Queries (z.B. "Suche in meiner Wissensbasis UND in Paperless nach X"):

1. **Planner:** LLM dekomponiert in Sub-Queries pro Domain
2. **Sub-Agents:** Pro Domain ein Agent mit gefilterten Tools (sequenziell)
3. **Synthesizer:** LLM kombiniert Sub-Ergebnisse, Fallback: Konkatenation

**Renfield-Beispiele:** "Mach das Licht an UND spiel Musik", "Was steht in meinen Dokumenten ueber X und was sagt das Knowledge Graph?"

### 10. Backend i18n System

**Quelle:** `reva/src/reva/i18n.py` (121 Zeilen, 100% generisch)

Renfield hat Frontend-i18n (React `useTranslation()`), aber kein Backend-i18n.

- YAML-basierte Sprachdateien (`config/i18n/{de,en}.yaml`)
- `t(key, lang, **kwargs)` API mit Fallback-Chain (lang → en → key)
- Lazy-Loading, Hot-Reload

### 11. Context Variable Extraction

**Quelle:** `reva/src/reva/teams_transport.py` → `_extract_context_vars()`

Extrahiert strukturierte Entities aus Tool-Results **ohne LLM-Cost** (pure Regex/JSON):

```python
# Nach get_release:
ctx_vars = {"current_release": "Q2 Deploy (active)", "current_release_id": "12345"}

# Naechste Message "Zeig mehr Details" weiss welches Release gemeint ist
```

Persistiert in DB, injiziert in naechsten Agent-Prompt als `<context_variables>`.

**Stand Renfield (#1447):** Extraktion seit #313 (`services/context_extractor.py`, YAML-Regeln), aber bis #1447 nie gespeichert — der Prompt las eine Spalte, die nur `_active_domain` enthielt. Jetzt gespeichert nach jedem Agent-Zug (`ConversationService.save_turn_context_vars`), injiziert als `## Conversation Context`; der Hook `extract_context_vars` wird an derselben Stelle gefeuert.

### 12. Per-Prompt LLM Options

**Quelle:** `reva/prompts/memory.yaml`

Reva erlaubt pro Prompt-Template eigene LLM-Settings:

```yaml
extraction:
  temperature: 0.1    # Konservativ fuer Fakten-Extraktion
  num_predict: 500

contradiction_resolution:
  temperature: 0.0    # Deterministisch fuer ADD/UPDATE/DELETE
  num_predict: 200
```

Renfield nutzt aktuell globale Settings fuer alle Prompt-Typen.

---

## NICHT uebernehmen (Reva-spezifisch)

| Feature | Grund |
|---|---|
| Teams Transport | MS-Teams-spezifisch, Renfield hat WebSocket |
| Adaptive Cards | Teams UI-Format |
| LDAP Service | Enterprise-Directory |
| Release/Jira/Confluence Tools | Domain-spezifische MCP-Tools |
| Notification/Webhook System | Teams proactive messaging |
| Bot Framework Auth | Teams JWT validation |
| K8s Deployment | Renfield nutzt Docker Compose |

---

## Empfohlene Reihenfolge

| # | Feature | Aufwand | Impact | Abhaengigkeiten |
|---|---|---|---|---|
| 1 | Cherry-pick Router Model Settings | 30min | Config-Sauberkeit | Keine |
| 2 | Input Guard (Prompt Injection Defense) | 2-3h | Sicherheit | Keine |
| 3 | MCP Compaction Engine + YAML Config | 2-3h | Performance | Hook `compact_mcp_result` (existiert) |
| 4 | Memory Poisoning Defense | 2-3h | Sicherheit | Memory Service (existiert) |
| 5 | Metrics Primitives (Counter/Histogram/Rate) | 3-4h | Observability | Keine |
| 6 | Token Budget Enforcement | 4-5h | Stabilitaet | Agent Service |
| 7 | Tool Pre-Selection | 3-4h | Qualitaet | Agent Service |
| 8 | Output Guard (Leakage + Role Confusion) | 2-3h | Sicherheit | Input Guard |
| 9 | Backend i18n | 2-3h | Mehrsprachigkeit | Keine |
| 10 | Duplicate Call Detection + Loop Guard | 2-3h | Stabilitaet | Agent Service |
| 11 | Context Variable Extraction | 3-4h | Follow-up Qualitaet | Conversation Service |
| 12 | Cross-MCP Orchestrator | 6-8h | Multi-Domain | Tool Pre-Selection, Agent Roles |

---

## Quell-Dateien (Referenz)

**Reva (Quelle):**

| Datei | Zeilen | Portierbarkeit |
|---|---|---|
| `src/reva/input_guard.py` | 335 | 100% generisch |
| `src/reva/output_guard.py` | 196 | 80% generisch (Leakage + Role Confusion) |
| `src/reva/mcp_compact.py` | 159 | 100% generisch |
| `src/reva/metrics.py` | 714 | 90% generisch (Primitives + Core-KPIs) |
| `src/reva/i18n.py` | 121 | 100% generisch |
| `src/reva/memory_service.py` | 308 | 60% generisch (Guards + Patterns) |
| `src/reva/kpi_service.py` | ~400 | 50% generisch (LLM-as-Judge, Retry Detection) |

**Renfield (Ziel):**

| Bereich | Aktueller Stand |
|---|---|
| Input Sanitization | Nicht vorhanden |
| Output Validation | Nicht vorhanden |
| Memory Poisoning Defense | Nicht vorhanden |
| MCP Compaction | Hook existiert, Engine fehlt |
| Metrics | Basic Prometheus (HTTP, WS, LLM, Circuit Breaker) |
| Token Budget | Nicht vorhanden |
| Tool Pre-Selection | Nicht vorhanden |
| Backend i18n | Nicht vorhanden |
| Context Variables | Existiert (DB-Felder vorhanden seit `5c0216f`) |