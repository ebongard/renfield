# LLM Model Guide

Empfehlungen fuer lokale LLM-Modelle (Ollama) fuer jede Renfield-Funktion.
Keine Cloud-LLMs — alles laeuft lokal auf eigener Hardware.

**Ziel-Hardware:** NVIDIA RTX 5070 Ti 16 GB + NVIDIA RTX 5060 Ti 16 GB (32 GB gesamt)

---

## Inhaltsverzeichnis

1. [Aktueller Stand](#aktueller-stand)
2. [Alle LLM-Funktionen im Detail](#alle-llm-funktionen-im-detail)
3. [Embedding-Modell](#8-embeddings)
4. [Hardware-Architektur: Dual-Ollama](#hardware-architektur-dual-ollama)
5. [Empfohlene Konfiguration](#empfohlene-konfiguration)
6. [Qualitaetsvergleich](#qualitaetsvergleich-qwen314b-vs-30b-a3b)
7. [Migrations-Reihenfolge](#migrations-reihenfolge)
8. [VRAM-Referenz](#vram-referenz)

---

## Aktueller Stand

| Setting | Aktuell | Genutzt von |
|---------|---------|-------------|
| `ollama_chat_model` | `llama3.2:3b` | Chat, Fallback |
| `ollama_rag_model` | `llama3.2:latest` | RAG-Antworten |
| `ollama_intent_model` | `llama3.2:3b` | Intent-Erkennung |
| `ollama_embed_model` | `nomic-embed-text` | Alle Embeddings (5 Services) |
| `agent_model` | `None` (Fallback auf chat_model) | Agent Loop, Router |
| `proactive_enrichment_model` | `None` (Fallback auf chat_model) | Notification-Enrichment |

**Empfohlen:** Qwen3-Familie (siehe Abschnitte unten). Code-Defaults sind noch `llama3.2` fuer Kompatibilitaet mit kleiner Hardware.

---

## Alle LLM-Funktionen im Detail

### 1. Intent-Erkennung

| Aspekt | Detail |
|--------|--------|
| **Config** | `ollama_intent_model` |
| **Service** | `ollama_service.py` → `extract_intent()`, `extract_ranked_intents()` |
| **Aufgabe** | User-Message → JSON mit 1-3 Intents + Confidence + Parameter |
| **Output** | `{"intents": [{"intent": "mcp.ha.turn_on", "confidence": 0.9, ...}]}` |
| **LLM Options** | temp=0.0, top_p=0.1, num_predict=500 |
| **Anforderung** | Schnell, deterministisch, zuverlaessiges JSON, Deutsch+Englisch |

**Aktuell:** `llama3.2:3b` — Funktioniert, aber schwach bei Deutsch und JSON-Zuverlaessigkeit.

**Empfehlung: `qwen3:8b`** (~6 GB VRAM)
- Exzellentes strukturiertes JSON-Output
- 100+ Sprachen inkl. Deutsch
- Non-thinking Mode fuer maximale Geschwindigkeit
- Deutlich bessere Klassifikations-Qualitaet als Llama 3.2:3b

**Budget-Alternative:** `qwen3:4b` (~3 GB) — Ueberraschend stark bei strukturierter Extraktion.

---

### 2. Chat / Konversation

| Aspekt | Detail |
|--------|--------|
| **Config** | `ollama_chat_model` |
| **Service** | `ollama_service.py` → `chat()`, `chat_stream()` |
| **Aufgabe** | Natuerliche Konversation, Persoenlichkeit, Streaming |
| **Output** | Freitext (Deutsch/Englisch) |
| **LLM Options** | temp=0.7, top_p=0.9, num_predict=1500 |
| **Anforderung** | Natuerlich, persoenlich, bilingual, schnelles Streaming |

**Aktuell:** `llama3.2:3b` — Zu klein fuer natuerliche Konversation, schwach auf Deutsch.

**Empfehlung: `qwen3:14b`** (~10 GB VRAM)
- Passt auf eine einzelne 16 GB GPU (kein Split noetig)
- Sehr gutes Deutsch
- Dense-Architektur = vorhersagbare Latenz
- Stark genug fuer persoenliche, natuerliche Konversation

**Auf 24 GB Single-GPU waere besser:** `qwen3:30b-a3b` (MoE, ~20 GB) — aber passt nicht auf 16 GB.

**Budget:** `qwen3:8b` (~6 GB) — Bestes Preis-Leistungs-Verhaeltnis fuer bilingualen Chat.

---

### 3. RAG-Antworten

| Aspekt | Detail |
|--------|--------|
| **Config** | `ollama_rag_model` |
| **Service** | `ollama_service.py` → `chat_with_rag()`, `chat_stream_with_rag()` |
| **Aufgabe** | Antwort auf Basis von abgerufenem Kontext (Dokumente) |
| **Output** | Freitext, faktenbasiert |
| **LLM Options** | temp=0.3, top_p=0.8, num_predict=2000 |
| **Anforderung** | Kontexttreue, wenig Halluzination, Deutsch |

**Aktuell:** `llama3.2:latest` (~3B) — Zu klein fuer zuverlaessige Kontextnutzung.

**Empfehlung: `qwen3:14b`** (~10 GB VRAM)
- Sehr gute Kontexttreue (wenig Halluzination)
- Starkes Deutsch
- Gleiches Modell wie Chat → kein zusaetzlicher VRAM noetig

---

### 4. Agent / ReAct Loop

| Aspekt | Detail |
|--------|--------|
| **Config** | `agent_model` + `agent_ollama_url` |
| **Service** | `agent_service.py` → `run()`, `_build_summary_answer()` |
| **Aufgabe** | Multi-Step Tool-Calling, Planung, JSON-Actions |
| **Output** | `{"action": "tool_name", "parameters": {...}, "reason": "..."}` |
| **LLM Options** | temp=0.1, top_p=0.2, num_predict=2048 |
| **Anforderung** | Zuverlaessiges JSON, Tool-Calling, mehrstufiges Reasoning |

**Aktuell:** Faellt auf `chat_model` zurueck (`llama3.2:3b`) — viel zu schwach fuer Agent-Loops.

**Empfehlung: `qwen3:14b`** (~10 GB VRAM) auf separater GPU
- Auf eigener GPU (5060 Ti) → blockiert nicht Chat
- Thinking-Mode fuer komplexe Planungsschritte moeglich
- Zuverlaessiges Tool-Calling und JSON-Output

**Auf 24 GB Single-GPU:** `qwen3:30b-a3b` waere staerker, passt aber nicht auf 16 GB.

**Alternative:** `mistral-small3.1:24b` (~16 GB) — passt knapp auf eine 16 GB Karte, native Function-Calling, 150 tok/s. Aber wenig Headroom fuer KV-Cache.

---

### 5. Agent Router

| Aspekt | Detail |
|--------|--------|
| **Config** | `agent_model` / `ollama_intent_model` (Fallback) |
| **Service** | `agent_router.py` → `classify()` |
| **Aufgabe** | User-Message → Kategorie (smart_home, documents, media, etc.) |
| **Output** | `{"role": "smart_home", "reason": "..."}` |
| **LLM Options** | temp=0.0, top_p=0.1, num_predict=128, num_ctx=4096 |
| **Anforderung** | Ultra-schnell, einfache Klassifikation |

**Aktuell:** Faellt auf Intent-Model zurueck.

**Empfehlung: `qwen3:0.6b`** (~0.5 GB VRAM)
- Ultra-schnell, minimaler VRAM
- Fuer einfache Kategorisierung mehr als ausreichend
- Kann permanent geladen bleiben (`keep_alive: -1`)

**Alternative:** Gleicher Intent-Model (`qwen3:8b`) — wenn kein separates Modell gewuenscht.

---

### 6. Memory-Extraktion

| Aspekt | Detail |
|--------|--------|
| **Config** | `ollama_model` (generischer Fallback) |
| **Service** | `conversation_memory_service.py` → `extract_and_save()` |
| **Aufgabe** | Dialog → JSON-Array mit Fakten, Praeferenzen, Anweisungen |
| **Output** | `[{"content": "...", "category": "preference", "importance": 0.8}]` |
| **LLM Options** | temp=0.1, top_p=0.2, num_predict=500, num_ctx=4096 |
| **Anforderung** | Zuverlaessiges JSON, Background-Task (Latenz unkritisch) |

**Aktuell:** `ollama_model` (Fallback auf `llama3.2:3b`).

**Empfehlung: `qwen3:8b`** (~6 GB) im Non-thinking Mode
- Exzellentes strukturiertes JSON
- Laeuft als Background-Task → Geschwindigkeit weniger kritisch
- Starkes Deutsch fuer Fakten-Extraktion

**Budget:** `qwen3:4b` (~3 GB) — Fuer Background-Tasks mit minimalem VRAM-Impact.

---

### 7. Notification-Enrichment

| Aspekt | Detail |
|--------|--------|
| **Config** | `proactive_enrichment_model` |
| **Service** | `notification_service.py` → `_enrich_message()`, `_auto_classify_urgency()` |
| **Aufgabe** | Urgency-Klassifikation (1 Wort) + Nachricht anreichern (1-2 Saetze) |
| **Output** | `"critical"` / `"info"` / `"low"` bzw. kurzer Freitext |
| **LLM Options** | temp=0.0/0.3, num_predict=10/200 |
| **Anforderung** | Schnell, minimal, optional (Phase 2) |

**Aktuell:** Faellt auf `ollama_model` zurueck.

**Empfehlung: `qwen3:4b`** (~3 GB)
- Fuer 1-Wort-Klassifikation und kurze Anreicherung ausreichend
- Schnell genug fuer Echtzeit-Notifications

---

### 8. Embeddings

| Aspekt | Detail |
|--------|--------|
| **Config** | `ollama_embed_model` |
| **Services** | `ollama_service`, `rag_service`, `conversation_memory_service`, `intent_feedback_service`, `notification_service` |
| **Aufgabe** | Text → 768-dim Vektor fuer Cosine-Similarity (pgvector) |
| **Anforderung** | Deutsch+Englisch, konsistente Qualitaet, immer geladen |

**Aktuell:** `nomic-embed-text` (137M, 768 dim, ~0.3 GB) — Primaer Englisch, schwach auf Deutsch.

**Empfehlung: `qwen3-embedding:4b`** (~3 GB VRAM)
- \#1 auf MTEB Multilingual Leaderboard (Score 70.58)
- Exzellentes Deutsch (massiv besser als nomic)
- Matryoshka-Dimensionen (768 kompatibel, oder hoeher bei Bedarf)
- Sollte mit `keep_alive: -1` permanent geladen bleiben

**Budget:** `granite-embedding:278m` (~0.4 GB) — Deutlich besser auf Deutsch als nomic bei aehnlichem VRAM.

**Hinweis:** Ein Embedding-Modell-Wechsel erfordert Re-Embedding aller existierenden Vektoren (RAG-Chunks, Memories, Intent-Corrections, Notification-Suppressions).

---

## Hardware-Architektur: Dual-Ollama

Mit 2x 16 GB GPUs ist die beste Strategie: **zwei separate Ollama-Instanzen**, eine pro GPU. Renfield unterstuetzt das bereits ueber `agent_ollama_url`.

### Warum kein GPU-Split?

Ein einzelnes grosses Modell (z.B. 30B) ueber zwei GPUs gesplittet hat Nachteile:
- GPU-zu-GPU Kommunikation ueber PCIe addiert Latenz bei **jedem** Token
- Bei MoE-Modellen besonders schlecht: Experts auf verschiedenen GPUs → staendiger Transfer
- Ein **14B Dense-Modell auf einer GPU** ist schneller als ein 30B MoE ueber zwei

### Warum Dual-Ollama?

- **Parallelitaet:** Chat + Agent laufen gleichzeitig auf separater Hardware
- **Keine Blockierung:** Ein Agent-Loop (8-12 Schritte × 30s) blockiert keine Chat-Antworten
- **Multi-User:** Entscheidend fuer einen Household-Assistenten mit mehreren Nutzern

### GPU-Zuweisung

```
Ollama Primary (GPU 0 — RTX 5070 Ti 16 GB)
├── qwen3:14b          ~10 GB  [Chat + RAG]      keep_alive=5m
├── qwen3:8b            ~6 GB  [Intent]           keep_alive=5m
├── qwen3:0.6b         ~0.5 GB [Router]           keep_alive=-1
└── qwen3-embedding:4b  ~3 GB  [Embeddings]       keep_alive=-1

Ollama Agent (GPU 1 — RTX 5060 Ti 16 GB)
├── qwen3:14b          ~10 GB  [Agent Loop]       keep_alive=5m
└── qwen3:4b            ~3 GB  [Extraction/Enrichment]  keep_alive=0
```

Ollama laedt/entlaedt Modelle automatisch. Auf GPU 0 sind nie alle gleichzeitig geladen — Intent und Chat/RAG wechseln sich ab. Embeddings und Router bleiben permanent im VRAM.

### Docker Compose

```yaml
ollama-primary:
  image: ollama/ollama:latest
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            device_ids: ['0']       # RTX 5070 Ti
            capabilities: [gpu]
  ports:
    - "11434:11434"
  volumes:
    - ollama-primary:/root/.ollama
  environment:
    OLLAMA_MAX_LOADED_MODELS: 3     # Chat/RAG + Embedding + Router
    OLLAMA_KEEP_ALIVE: 5m

ollama-agent:
  image: ollama/ollama:latest
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            device_ids: ['1']       # RTX 5060 Ti
            capabilities: [gpu]
  ports:
    - "11435:11434"
  volumes:
    - ollama-agent:/root/.ollama
  environment:
    OLLAMA_MAX_LOADED_MODELS: 2     # Agent + Extraction
    OLLAMA_KEEP_ALIVE: 5m
```

---

## Empfohlene Konfiguration

### .env

```bash
# === Primary Ollama (GPU 0 — RTX 5070 Ti) ===
OLLAMA_URL=http://ollama-primary:11434
OLLAMA_CHAT_MODEL=qwen3:14b
OLLAMA_RAG_MODEL=qwen3:14b
OLLAMA_INTENT_MODEL=qwen3:8b
OLLAMA_EMBED_MODEL=qwen3-embedding:4b

# === Agent Ollama (GPU 1 — RTX 5060 Ti) ===
AGENT_OLLAMA_URL=http://ollama-agent:11434
AGENT_MODEL=qwen3:14b

# === Optionale spezialisierte Modelle ===
# PROACTIVE_ENRICHMENT_MODEL=qwen3:4b   # Auf Agent-Ollama
# Router nutzt automatisch OLLAMA_INTENT_MODEL als Fallback
```

### Modell-Vorladung (Startup-Script)

```bash
#!/bin/bash
# Modelle auf Primary GPU vorziehen
ollama pull qwen3:14b
ollama pull qwen3:8b
ollama pull qwen3:0.6b
ollama pull qwen3-embedding:4b

# Modelle auf Agent GPU vorziehen
OLLAMA_HOST=http://localhost:11435 ollama pull qwen3:14b
OLLAMA_HOST=http://localhost:11435 ollama pull qwen3:4b
```

---

## Qualitaetsvergleich: qwen3:14b vs 30b-a3b

Da 30b-a3b nicht auf eine einzelne 16 GB Karte passt, hier der Vergleich:

| Aufgabe | 14b Dense (1 GPU) | 30b-a3b MoE (2 GPU Split) | Fazit |
|---------|-------------------|---------------------------|-------|
| Chat (Deutsch) | Sehr gut | Exzellent | 14b reicht fuer Household-Assistent |
| Intent JSON | Sehr gut | Exzellent | 8b uebernimmt das ohnehin |
| RAG Kontexttreue | Sehr gut | Exzellent | 14b ausreichend fuer Dokument-QA |
| Agent Tool-Calling | Gut | Sehr gut | 14b auf eigener GPU kompensiert durch Latenz-Vorteil |
| Wissen/Breite | Gut | Besser | MoE hat breiteres Wissen, aber 14b deckt Alltag ab |
| **Latenz** | **Schnell** | **Langsam (Split)** | **14b gewinnt klar** |
| **Parallelitaet** | **Chat + Agent gleichzeitig** | **Blockiert sich gegenseitig** | **14b gewinnt klar** |

Der moderate Qualitaetsverlust 30B→14B wird durch keine Split-Latenz und volle Parallelitaet mehr als kompensiert.

---

## Modell-Uebersicht: Deutsch-Qualitaet

| Modell | Deutsch Fluency | Deutsch Instructions | Deutsch RAG | Anmerkung |
|--------|-----------------|---------------------|-------------|-----------|
| Qwen3 (alle Groessen) | Exzellent | Exzellent | Exzellent | 100+ Sprachen |
| Qwen2.5 (alle Groessen) | Gut | Gut | Gut | 29 Sprachen |
| Mistral Small 3.1 | Gut | Gut | Gut | Starke europaeische Sprachen |
| Llama 3.3 | Ausreichend | Ausreichend | Ausreichend | Primaer Englisch |
| Gemma 3 | Moderat | Moderat | Moderat | Englisch-fokussiert |
| Phi-4 | Moderat | Gut | Moderat | Passables Multilingual |

---

## Modell-Uebersicht: JSON / Function-Calling

| Modell | Native JSON | Schema Enforcement | Function Calling | Tool Use |
|--------|-------------|-------------------|------------------|----------|
| Qwen3 (alle) | Exzellent | Ollama `format` | Exzellent | Nativ |
| Qwen2.5 (alle) | Exzellent | Ollama `format` | Sehr gut | Nativ |
| Mistral Small 3.1 | Exzellent | Ollama `format` | Exzellent | Nativ |
| Llama 3.x | Gut | Ollama `format` | Gut | Nativ |
| Gemma 3 | Gut | Ollama `format` | Moderat | Begrenzt |
| DeepSeek-R1 | Moderat | Ollama `format` | Schwach | Nicht dafuer gebaut |

---

## Migrations-Reihenfolge

| Prio | Aktion | Impact | Aufwand |
|------|--------|--------|---------|
| 1 | **Embedding-Modell** → `qwen3-embedding:4b` | Deutsch-Retrieval massiv besser | Hoch (Re-Embedding aller Vektoren) |
| 2 | **Dual-Ollama aufsetzen** (docker-compose.prod.yml) | Agent blockiert nicht mehr Chat | Mittel (Infra) |
| 3 | **Chat/RAG** → `qwen3:14b` | Natuerlichere Konversation, besseres Deutsch | Gering (Config) |
| 4 | **Intent** → `qwen3:8b` | Zuverlaessigeres JSON | Gering (Config) |
| 5 | **Agent** → `qwen3:14b` auf zweiter GPU | Echtes Multi-Step Reasoning | Gering (Config) |

### Hinweise zur Embedding-Migration

Der Wechsel von `nomic-embed-text` (768 dim) zu `qwen3-embedding:4b` erfordert:

1. **Dimensionen pruefen:** Qwen3-Embedding unterstuetzt Matryoshka (flexible Dimensionen). 768 kann beibehalten werden — kein Schema-Aenderung noetig.
2. **Re-Embedding:** Alle existierenden Vektoren muessen neu berechnet werden:
   - RAG Document Chunks (`document_chunks.embedding`)
   - Conversation Memories (`conversation_memories.embedding`)
   - Intent Corrections (`intent_corrections.embedding`)
   - Notification Suppressions (`notification_suppressions.embedding`)
3. **Downtime:** Re-Embedding kann im Hintergrund laufen, aber waehrend der Migration sind Similarity-Suchen inkonsistent (alte vs neue Vektoren).

---

## VRAM-Referenz

Ungefaehre Werte fuer Q4_K_M Quantisierung:

| Modell | Parameter | VRAM (Gewichte) | + KV-Cache (8K) | Gesamt |
|--------|-----------|-----------------|-----------------|--------|
| qwen3:0.6b | 0.6B | ~0.5 GB | ~0.1 GB | ~0.6 GB |
| qwen3:4b | 4B | ~3 GB | ~0.2 GB | ~3.2 GB |
| qwen3:8b | 8B | ~6 GB | ~0.3 GB | ~6.3 GB |
| qwen3:14b | 14B | ~10 GB | ~0.5 GB | ~10.5 GB |
| qwen3:30b-a3b | 30B (3B aktiv) | ~20 GB | ~0.2 GB | ~20.2 GB |
| qwen3:32b | 32B | ~22 GB | ~1.0 GB | ~23 GB |
| mistral-small3.1:24b | 24B | ~16 GB | ~0.7 GB | ~16.7 GB |
| qwen3-embedding:4b | 4B | ~3 GB | — | ~3 GB |
| qwen3-embedding:0.6b | 0.6B | ~0.5 GB | — | ~0.5 GB |
| nomic-embed-text | 137M | ~0.3 GB | — | ~0.3 GB |

**16 GB GPU Limit:** Modelle bis ~14B Dense passen komfortabel. 24B ist grenzwertig (wenig KV-Cache Headroom). Alles ueber 16 GB erfordert GPU-Split.

---

## Alternative: Budget-Setup (einzelne 16 GB GPU)

Falls nur eine GPU verfuegbar:

| Rolle | Modell | VRAM |
|-------|--------|------|
| Alles (Chat/RAG/Intent/Agent) | `qwen3:8b` | ~6 GB |
| Embeddings | `qwen3-embedding:0.6b` | ~0.5 GB |
| **Gesamt** | | **~6.5 GB** |

Qualitativ schwaecher, aber bereits deutlich besser als der aktuelle `llama3.2:3b` Stand.
