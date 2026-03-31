# Agent Evolution Plan: Parallel Tools + Isolierte Sub-Agents

*Erstellt: 31. März 2026*
*Basierend auf: docs/private/react-vs-agent-to-agent.md (aktualisiert)*

## Ziel

Zwei konkrete Verbesserungen am Agent-System die zusammen ~50% Latenz-Reduktion und bessere Qualität bei Multi-Domain-Anfragen bringen. Kein volles A2A-Protokoll, aber 80% des Nutzens.

---

## Phase 1: Parallele Tool-Ausführung

### Problem

"Wie wird das Wetter, was steht im Kalender, und gibt es News?" erzeugt 3 sequentielle Tool-Calls a ~3-5s = 9-15s. Die Tools sind unabhängig voneinander und könnten parallel laufen.

### Lösung

Der Agent gibt in einem Step mehrere Actions zurück. `agent_service.py` erkennt das Array und führt alle parallel aus.

```
VORHER:
LLM → action: weather.get_current    → 3s warten → result
LLM → action: calendar.get_today     → 3s warten → result
LLM → action: news.get_headlines     → 3s warten → result
LLM → final_answer                   → 3s
TOTAL: ~12s (4 LLM-Calls + 3 Tool-Calls sequentiell)

NACHHER:
LLM → actions: [weather, calendar, news]  → 3s (parallel) → results
LLM → final_answer                        → 3s
TOTAL: ~6s (2 LLM-Calls + 1 parallel Tool-Batch)
```

### Dateien

| Datei | Änderung |
|-------|----------|
| `src/backend/services/agent_service.py` | Multi-Action-Parsing im ReAct-Loop, `asyncio.gather()` für parallele Ausführung |
| `src/backend/prompts/agent.yaml` | Prompt-Erweiterung: LLM darf `actions: [...]` Array zurückgeben |
| `src/backend/config/agent_roles.yaml` | Optional: `parallel_tools: true` pro Rolle |
| `tests/backend/test_agent_parallel.py` | Tests für Multi-Action-Parsing und parallele Ausführung |

### Prompt-Erweiterung

```yaml
# Neues Ausgabe-Format (zusätzlich zum bestehenden):
# Wenn mehrere unabhängige Tools benötigt werden:
{
  "thinking": "Ich brauche Wetter, Kalender und News. Alle sind unabhängig.",
  "actions": [
    {"action": "weather.get_current", "parameters": {"location": "Berlin"}},
    {"action": "calendar.get_today", "parameters": {}},
    {"action": "news.get_headlines", "parameters": {"country": "de"}}
  ]
}

# Einzelne Action bleibt wie bisher:
{
  "thinking": "...",
  "action": "weather.get_current",
  "parameters": {"location": "Berlin"}
}
```

### Implementierungsdetails

Im ReAct-Loop (`agent_service.py`, ~Zeile 838):

```python
# Nach JSON-Parsing:
if "actions" in parsed and isinstance(parsed["actions"], list):
    # Multi-Action: parallel ausführen
    tasks = [self._execute_tool(a["action"], a["parameters"]) for a in parsed["actions"]]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Alle Ergebnisse in History aufnehmen
    for action_data, result in zip(parsed["actions"], results):
        history.append({"role": "tool", "tool": action_data["action"], "result": result})
elif "action" in parsed:
    # Single Action: wie bisher
    ...
```

### Fallback

Wenn das LLM kein Array liefert sondern wie bisher eine einzelne Action, ändert sich nichts. 100% rückwärtskompatibel.

### Risiken

- **GPU/CPU-Contention**: 3 parallele MCP-Calls sind I/O-bound (HTTP/stdio), kein LLM-Problem
- **Fehler-Handling**: Wenn einer von 3 parallelen Calls fehlschlägt, die anderen trotzdem nutzen
- **LLM-Qualität**: Qwen3:8b muss zuverlässig entscheiden können, welche Tools unabhängig sind. Risiko: Agent packt abhängige Tools in ein Array (z.B. "suche Datei" + "sende Datei per Mail"). Mitigation: Prompt-Instruktion + Validierung

### Tests

1. Multi-Action JSON korrekt geparst
2. Parallele Ausführung via asyncio.gather
3. Fehler in einem parallelen Call → andere Ergebnisse bleiben erhalten
4. Fallback auf Single-Action wenn Array nicht geliefert wird
5. Abhängige Tools werden NICHT parallel ausgeführt (Validierung)

---

## Phase 2: Isolierte Sub-Agent-Sessions

### Problem

Multi-Domain-Anfragen ("Licht an und such News") laufen sequentiell durch den Orchestrator. Sub-Queries teilen sich den Context. Bei komplexen Anfragen wird der Context voll.

### Lösung

Der Orchestrator spawnt isolierte `AgentService`-Instanzen mit eigenem Context:

```
Orchestrator
    │
    ├── detect_multi_domain("Licht an und such News")
    │   → ["Mach das Licht an", "Such mir die News"]
    │
    ├── spawn_isolated(role="smart_home", query="Mach das Licht an")
    │   └── Eigene AgentService-Instanz
    │       └── Eigene Message-History (leer)
    │       └── Eigene Tool-Liste (nur HA)
    │       └── Max 4 Steps
    │
    ├── spawn_isolated(role="research", query="Such mir die News")
    │   └── Eigene AgentService-Instanz
    │       └── Eigene Message-History (leer)
    │       └── Eigene Tool-Liste (nur search/news)
    │       └── Max 6 Steps
    │
    ├── await asyncio.gather(agent_1, agent_2)  ← PARALLEL
    │
    └── synthesize(results) → LLM-Call für Zusammenfassung
```

### Dateien

| Datei | Änderung |
|-------|----------|
| `src/backend/services/orchestrator.py` | Parallele Sub-Agent-Spawning mit isoliertem Context |
| `src/backend/services/agent_service.py` | Factory-Methode `create_isolated()` für Sub-Agents ohne geteilte History |
| `tests/backend/test_orchestrator_parallel.py` | Tests für parallele isolierte Ausführung |

### Implementierungsdetails

```python
# orchestrator.py
async def execute_multi_domain(self, sub_queries: list[dict]) -> str:
    tasks = []
    for sq in sub_queries:
        # Jeder Sub-Agent bekommt eine frische Session
        agent = AgentService.create_isolated(
            role=sq["role"],
            db=self.db,
            user_id=self.user_id,
        )
        tasks.append(agent.run_single(sq["query"]))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Fehler filtern, erfolgreiche Ergebnisse sammeln
    successful = [r for r in results if not isinstance(r, Exception)]

    # Synthese
    return await self._synthesize(sub_queries, successful)
```

### Streaming

Sub-Agents können nicht direkt an den WebSocket streamen (parallele Streams würden sich überlappen). Stattdessen:
- Jeder Sub-Agent sammelt seine Steps in einer Liste
- Nach Abschluss aller Sub-Agents: Steps werden sortiert und als Batch gestreamt
- Oder: Ein "progress" Event pro Sub-Agent ("Smart Home: erledigt", "News: läuft...")

### Risiken

- **Ollama-Contention**: 2+ parallele LLM-Calls auf demselben Ollama = langsamer pro Call. Mitigation: Semaphore auf max 2 parallele Agent-Loops
- **Streaming-UX**: Parallele Agents können nicht sinnvoll in Echtzeit gestreamt werden. Lösung: Progress-Events statt Step-Streaming
- **Context-Verlust**: Sub-Agents sehen nicht den Konversations-Verlauf. Mitigation: Relevanten Kontext (letzte 3 Messages) als System-Prompt mitgeben

### Tests

1. Zwei Sub-Agents laufen parallel und liefern Ergebnisse
2. Fehler in einem Sub-Agent → anderer liefert trotzdem
3. Synthese kombiniert beide Ergebnisse sinnvoll
4. Streaming-Events korrekt für parallele Ausführung
5. Ollama-Semaphore begrenzt parallele LLM-Calls

---

## Phase 3: Proaktiver Background-Agent (Ausblick)

Nicht Teil der sofortigen Implementierung. Skizziert für Planung.

### Konzept

Agent-Loop der per Scheduler (Cron) getriggert wird:
- "Jeden Morgen um 7:00: Wetter prüfen, Kalender-Termine zusammenfassen, Briefing erstellen"
- "Alle 30 Minuten: Wenn Temperatur im Arbeitszimmer < 20°C → Heizung auf 22°C"

### Voraussetzungen

- Isolierte Sub-Agent-Sessions (Phase 2)
- Persistente Agent-Sessions (nicht an WebSocket gebunden)
- Notification-System für Agent-Ergebnisse
- Scheduler-Integration (Cron oder n8n)

---

## Priorisierung

| Phase | Impact | Aufwand (CC) | Risiko | Empfehlung |
|-------|--------|-------------|--------|------------|
| 1. Parallel Tools | ~50% Latenz-Reduktion bei Multi-Tool | ~30min | Niedrig | Sofort |
| 2. Isolierte Sub-Agents | Bessere Qualität bei Multi-Domain | ~45min | Mittel | Nach Phase 1 |
| 3. Proaktiver Agent | Neue Usecase-Kategorie | ~1-2h | Mittel | Wenn Bedarf entsteht |

Phase 1 und 2 zusammen: ~75min CC-Zeit, kein Breaking Change, rückwärtskompatibel.
