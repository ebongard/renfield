# ReAct Agent Loop vs. Agent-to-Agent Sessions

*Erstellt: 27. Januar 2026*
*Aktualisiert: 31. März 2026 — Status-Update nach Implementierung Stage 1-4*

## Kontext

Dieses Dokument vergleicht zwei Architektur-Ansätze für mehrstufige AI-Workflows:

1. **ReAct Agent Loop** — Implementiert in Renfield (Issue #49, abgeschlossen)
2. **Agent-to-Agent Sessions** — Implementiert in Moltbot

Beide ermöglichen komplexe, mehrstufige Aufgaben. Sie unterscheiden sich fundamental in Intelligenz-Verteilung, Kontext-Management und Skalierbarkeit.

**Modell-Basis Renfield:** Qwen3 (8b Chat, 14b Media-Agent) via Ollama. Lokale Inferenz, kein Cloud-LLM.

---

## Aktueller Stand (März 2026)

### Implementierter Evolutionspfad

```
Single Intent (2025)                     ✅ done
    ↓
ReAct Agent Loop (#49)                   ✅ done (agent_service.py, 1429 Zeilen)
    ↓
MCP-Client Integration                   ✅ done (110+ Tools, 11 MCP Server)
    ↓
Spezialisierte Agent-Rollen              ✅ done (8 Rollen + Router)
    ↓
Sequentieller Orchestrator               ✅ done (orchestrator.py, Multi-Domain)
    ↓
Parallele Tool-Ausführung                ← NÄCHSTER SCHRITT
    ↓
Isolierte Sub-Agent-Sessions             ← danach
    ↓
Volles Agent-to-Agent Protokoll          ← wenn Modelle besser werden
    ↓
Inter-Renfield Federation (#22)          ← langfristig
```

### Architektur-Kennzahlen

| Komponente | Umfang |
|-----------|--------|
| agent_service.py | 1.429 Zeilen, vollständiger ReAct-Loop |
| mcp_client.py | 1.444 Zeilen, 11 MCP Server |
| Verfügbare Tools | ~110+ (HA, Search, News, Jellyfin, Radio, DLNA, n8n, Paperless, Email, Calendar, interne) |
| Agent-Rollen | 8 spezialisiert + 1 general |
| Max Steps | 4-12 pro Rolle (konfigurierbar) |
| Streaming | Echtzeit-AgentStep via WebSocket |
| Sicherheitsnetze | Infinite-Loop-Detection, Empty-Result-Detection, Per-Step + Total Timeout |

---

## Übersicht

| | **ReAct Agent Loop** | **Agent-to-Agent Sessions** |
|---|---|---|
| **Kern-Idee** | Ein Agent ruft iterativ Tools auf | Mehrere autonome Agents kommunizieren |
| **Analogie** | Ein Handwerker mit Werkzeugkasten | Mehrere Handwerker, die sich absprechen |
| **Intelligenz** | Zentralisiert (1 LLM) | Verteilt (N LLMs) |
| **Kontext** | Geteilt — Agent sieht alles | Isoliert — nur explizit Geteiltes |
| **Komplexität** | 2-12 Schritte (konfiguriert) | Unbegrenzt skalierbar |
| **Latenz** | N x 1 LLM-Call + N Tool-Calls | N x M LLM-Calls (Agents x Steps) |
| **Modell-Anforderung** | Mittel (Qwen3:8b reicht) | Hoch (Koordination erfordert starkes Reasoning) |

---

## ReAct Agent Loop (Implementiert)

### Architektur

```
User Message
    ↓
AgentRouter.classify()     ← LLM bestimmt Rolle (smart_home/research/media/...)
    ↓                        Filtert Tools auf Rolle (z.B. nur HA-Tools für smart_home)
AgentService.run()         ← ReAct-Loop startet
    ↓
┌── Loop: step 1 bis max_steps ─────────────────────────────┐
│                                                            │
│  1. Prompt bauen (Message + History + Tools + Context)     │
│  2. LLM aufrufen (Qwen3:8b, per-step timeout 30s)        │
│  3. JSON parsen → action + parameters                      │
│  4. Tool validieren (Kurzname → voller MCP-Pfad)          │
│  5. Tool ausführen via ActionExecutor → MCP Server         │
│  6. Ergebnis in History aufnehmen                          │
│  7. AgentStep yielden (Streaming via WebSocket)            │
│  8. Terminierung prüfen:                                   │
│     - action == "final_answer"? → Antwort zurückgeben     │
│     - Max Steps erreicht? → LLM fasst zusammen            │
│     - Leere Ergebnisse 2x? → Fehlermeldung               │
│     - Infinite Loop (3x gleicher Call)? → Abbruch         │
│                                                            │
└────────────────────────────────────────────────────────────┘
    ↓
Streaming-Antwort an User
```

### Rollen-System (agent_roles.yaml)

| Rolle | Max Steps | MCP Server | Modell |
|-------|-----------|------------|--------|
| conversation | - (kein Loop) | - | Default |
| knowledge | - (RAG direkt) | - | Default |
| smart_home | 4 | homeassistant | qwen3:8b |
| research | 6 | search, news, weather | Default |
| documents | 8 | paperless, email | Default |
| media | 6 | jellyfin | qwen3:14b |
| workflow | 10 | n8n | Default |
| presence | 2 | interne Tools | Default |
| general | 12 | alle | Default |

### Stärken (bestätigt durch Produktion)

- Zuverlässig mit Qwen3:8b für 2-6 Step Aufgaben
- Streaming-Feedback gibt User sofort Einblick in den Denkprozess
- Infinite-Loop-Detection verhindert endlose Wiederholungen
- Tool-Filtering pro Rolle reduziert Halluzinationen
- MCP-Architektur macht Tools erweiterbar ohne Agent-Code-Änderung

### Grenzen (bestätigt durch Produktion)

1. **Sequentiell** — Jeder Tool-Call blockiert. "Wetter + Kalender + News" = 3 Schritte = ~9-15s statt ~3-5s parallel
2. **Kein Kontext-Isolation** — Step 8 sieht den ganzen Context von Steps 1-7. Sliding Window (32 Steps) hilft, aber verliert ältere Info
3. **Einmalige Rollen-Zuordnung** — Router wählt EINE Rolle am Start. Cross-Domain-Anfragen brauchen den Orchestrator
4. **Keine Agent-Zusammenarbeit** — Ein Agent kann keinen anderen Agent aufrufen
5. **Context Window wächst** — Tool-Ergebnisse (besonders JSON) füllen den Context schnell

---

## Agent-to-Agent Sessions (Moltbot-Referenz)

### Architektur

Mehrere autonome Agents mit eigenem Kontext und eigenen Tools, verbunden über ein Message-Passing-Protokoll:

```
User: "Refactore mein Repo und schreib eine Zusammenfassung"
         ↓
    ┌─ Chat Agent ─────────────────────────────────────┐
    │  System-Prompt: Generalist                       │
    │  Tools: sessions_send, sessions_list             │
    │                                                  │
    │  "Das ist eine Coding-Aufgabe"                   │
    │  → sessions_send(coding_agent, aufgabe)          │
    └──────────────────────────────────────────────────┘
         ↓
    ┌─ Coding Agent (eigene Session) ──────────────────┐
    │  System-Prompt: Code-Spezialist                  │
    │  Tools: read_file, write_file, run_tests         │
    │                                                  │
    │  Arbeitet autonom: liest Code, refactort         │
    │  Führt eigene ReAct-Loop durch                   │
    │  → sessions_send(chat_agent, ergebnis)           │
    └──────────────────────────────────────────────────┘
         ↓
    Antwort an User
```

### Moltbot-Protokoll (3 Primitives)

| Primitive | Funktion |
|-----------|----------|
| `sessions_send(target, message)` | Nachricht an anderen Agent senden |
| `sessions_list()` | Verfügbare Agent-Sessions auflisten |
| `sessions_history(session_id)` | Konversationsverlauf einer Session lesen |

### Stärken

- Kontext-Isolation verhindert Context Window Overflow
- Spezialisierte Prompts = bessere Qualität pro Domäne
- Parallelisierbar: Agents können gleichzeitig arbeiten
- Erweiterbar: Neue Agents ohne Änderung am Orchestrator

### Grenzen

- Hoher Koordinations-Overhead (jede Delegation = LLM-Call)
- Debugging schwierig: Verteiltes System mit asynchronen Messages
- Erfordert starke Reasoning-Fähigkeit für Orchestration
- Moltbot nutzt Cloud-LLMs (Claude, GPT-4) — diese haben die nötige Qualität
- Qwen3:8b/14b hat nicht genug Reasoning-Power für zuverlässige Agent-Koordination

---

## Aktueller Orchestrator: Sequential Multi-Domain

Renfield hat einen Zwischenschritt implementiert, der kein volles A2A ist:

```
User: "Mach das Licht an und such mir die News"
    ↓
QueryOrchestrator.detect_multi_domain()
    ↓ Ja → Zerlege in Sub-Queries
    ├── Sub-Query 1: "Mach das Licht an" → smart_home Agent (4 Steps)
    ├── Sub-Query 2: "Such mir die News" → research Agent (6 Steps)
    ↓
Synthese: LLM fasst beide Ergebnisse zusammen
    ↓
Antwort an User
```

**Was der Orchestrator NICHT kann:**
- Sub-Agents laufen sequentiell, nicht parallel
- Kein isolierter Context pro Sub-Agent (teilen Sliding Window)
- Kein Message-Passing zwischen Sub-Agents
- Sub-Agent kann keinen anderen Sub-Agent aufrufen
- Opt-in via `AGENT_ORCHESTRATOR_ENABLED=true`

---

## Nächste Schritte: Pragmatischer Evolutionspfad

### Schritt 1: Parallele Tool-Ausführung (innerhalb ReAct-Loop)

**Problem:** "Wetter + Kalender + News" dauert 9-15s sequentiell.

**Lösung:** Wenn der Agent in einem Step mehrere unabhängige Tool-Calls plant, alle parallel ausführen. Kein neues Architekturkonzept, nur `asyncio.gather()` auf der Tool-Execution-Ebene.

```
AKTUELL (sequentiell):
Step 1: weather.get_current()     → 3s
Step 2: calendar.get_today()      → 3s
Step 3: news.get_headlines()      → 3s
Step 4: final_answer              → 3s
TOTAL: ~12s

MIT PARALLEL EXECUTION:
Step 1: [weather + calendar + news]  → 3s (parallel)
Step 2: final_answer                 → 3s
TOTAL: ~6s
```

**Implementierung:** Agent-Prompt erweitern um Multi-Tool-Output-Format. LLM gibt Array von Actions zurück statt einer einzelnen. `agent_service.py` führt alle parallel aus.

**Risiko:** Niedrig. Fallback auf sequentiell wenn LLM kein Array liefert.

**Aufwand:** CC ~30min

### Schritt 2: Isolierte Sub-Agent-Sessions (im Orchestrator)

**Problem:** Multi-Domain-Anfragen teilen sich den Context und laufen sequentiell.

**Lösung:** Jeder Sub-Agent bekommt seinen eigenen isolierten ReAct-Loop mit:
- Eigener Message-History (kein geteilter Sliding Window)
- Eigenem Tool-Filter (nur relevante MCP Server)
- Eigenem Step-Budget
- Eigenem LLM-Call-Context

```
Orchestrator
    ├── spawn Sub-Agent "smart_home" (isoliert, max 4 steps)
    │   └── Eigener Context, eigene History
    ├── spawn Sub-Agent "research" (isoliert, max 6 steps)
    │   └── Eigener Context, eigene History
    ↓
    await asyncio.gather(sub_agent_1, sub_agent_2)  ← PARALLEL
    ↓
    Synthese-LLM-Call → Zusammenfassung
```

**Das ist 80% des A2A-Benefits ohne das Moltbot-Protokoll.** Die Sub-Agents sind isoliert, parallel, spezialisiert. Aber sie kommunizieren nicht miteinander, nur über den Orchestrator.

**Implementierung:** Orchestrator startet mehrere `AgentService.run()` Instanzen mit je eigener Session. Ergebnisse werden am Ende zusammengeführt.

**Risiko:** Mittel. Parallele LLM-Calls auf lokalem Ollama = GPU/CPU-Contention.

**Aufwand:** CC ~45min

### Schritt 3: Proaktiver Background-Agent

**Problem:** Kein Agent kann autonom handeln (z.B. Cron-basiert Wetter prüfen → Heizung anpassen).

**Lösung:** Neuer Agent-Typ der per Scheduler getriggert wird:
- Eigene Session (persistent, nicht an User-Conversation gebunden)
- Eigene ReAct-Loop mit vordefiniertem Ziel
- Ergebnis wird als Notification an User gepusht

**Abhängig von:** Schritt 2 (isolierte Sessions)

**Aufwand:** CC ~1h

### Schritt 4: Volles Agent-to-Agent Protokoll

**Problem:** Sub-Agents können nicht miteinander kommunizieren.

**Lösung:** Moltbot-ähnliches Message-Passing:
- `agent_send(target_agent, message)` als Tool im Agent-Loop
- Agent-Registry: welche Agents existieren, was können sie
- Asynchrones Message-Passing mit Callback

**Voraussetzung:** Stärkeres lokales Modell (Qwen3:32b oder besser) für zuverlässige Orchestration. Qwen3:8b ist für Agent-Koordination zu unzuverlässig.

**Aufwand:** CC ~2h, aber Modell-Abhängigkeit macht es riskant

### Wann welcher Schritt

| Schritt | Trigger | Abhängigkeit |
|---------|---------|-------------|
| 1. Parallel Tools | Sofort sinnvoll, reduziert Latenz um ~50% | Keine |
| 2. Isolierte Sub-Agents | Sobald Multi-Domain häufiger genutzt wird | Schritt 1 optional |
| 3. Proaktiver Agent | Wenn Automatisierungs-Usecases kommen | Schritt 2 |
| 4. Volles A2A | Wenn lokale Modelle besser werden (32b+) | Schritt 2 + besseres Modell |

---

## Die 5 entscheidenden Unterschiede (aktualisiert)

### 1. Intelligenz-Verteilung

- **ReAct**: Zentralisiert — ein LLM trifft alle Entscheidungen
- **A2A**: Verteilt — jeder Agent hat eigenes LLM + Prompt
- **Renfield heute**: Hybrid — ein LLM pro Rolle, aber Router wählt Rolle zentral

### 2. Kontext-Isolation

- **ReAct**: Geteilter Kontext mit Sliding Window (32 Steps)
- **A2A**: Vollständig isoliert
- **Renfield heute**: Geteilt (Orchestrator-Sub-Queries teilen Window)
- **Nächster Schritt**: Isolierte Sub-Agent-Sessions (Schritt 2)

### 3. Tool vs. Agent

- **ReAct**: Tools sind passive Funktionen (MCP Server geben JSON zurück)
- **A2A**: Agents sind aktive Entscheider mit eigenem Reasoning
- **Renfield heute**: Reine passive Tools, Agent entscheidet allein

### 4. Kommunikations-Muster

- **ReAct**: Synchron, sequentiell (Step 1 → 2 → 3)
- **A2A**: Asynchron, parallel
- **Renfield heute**: Sequentiell (auch der Orchestrator)
- **Nächster Schritt**: Parallele Tool-Ausführung (Schritt 1)

### 5. Fehler-Behandlung

- **ReAct**: Lokal — Tool-Fehler → Agent entscheidet nächsten Schritt ✅ robust
- **A2A**: Komplex — Agent-Fehler → Orchestrator muss re-delegieren
- **Renfield heute**: Infinite-Loop-Detection, Empty-Result-Detection, Timeouts ✅ produktionsreif

---

## Modell-Einfluss auf Architektur-Entscheidung

| Fähigkeit | Qwen3:8b (aktuell) | Qwen3:14b (Media) | Qwen3:32b+ (Zukunft) |
|-----------|--------------------|--------------------|----------------------|
| Structured JSON Output | Zuverlässig | Zuverlässig | Zuverlässig |
| Function Calling (1 Tool) | Gut | Gut | Gut |
| Multi-Tool pro Step | Grenzwertig | Möglich | Zuverlässig |
| Bedingte Logik | Zuverlässig | Zuverlässig | Zuverlässig |
| Multi-Step Reasoning (12 Steps) | Funktioniert | Gut | Komfortabel |
| Agent-Koordination/Delegation | Nicht zuverlässig | Grenzwertig | Möglich |
| Task-Dekomposition | Basisch | Funktioniert | Zuverlässig |

**Fazit:** Parallele Tool-Ausführung (Schritt 1) und isolierte Sub-Agents (Schritt 2) sind mit Qwen3:8b/14b machbar. Volles A2A mit Agent-Koordination braucht stärkere Modelle.

---

## Referenzen

- Issue #49: ReAct Agent Loop Implementation (abgeschlossen)
- Issue #22: Inter-Renfield Communication (noch offen)
- `src/backend/services/agent_service.py`: ReAct-Loop (1.429 Zeilen)
- `src/backend/services/orchestrator.py`: Multi-Domain Orchestrator (224 Zeilen)
- `src/backend/services/agent_router.py`: Rollen-Router (398 Zeilen)
- `src/backend/config/agent_roles.yaml`: 8 spezialisierte Rollen
- `docs/private/renfield-vs-moltbot-analyse.md`: Vollständiger Feature-Vergleich
