# LLM Model Guide

Empfehlungen fuer lokale LLM-Modelle (Ollama) fuer jede Renfield-Funktion.
Keine Cloud-LLMs — alles laeuft lokal auf eigener Hardware.

**Ziel-Hardware:** NVIDIA RTX 5070 Ti 16 GB + NVIDIA RTX 5060 Ti 16 GB (32 GB gesamt)

---

## Inhaltsverzeichnis

1. [Aktueller Stand (Produktion)](#aktueller-stand-produktion)
2. [Alle LLM-Funktionen im Detail](#alle-llm-funktionen-im-detail)
3. [Embedding-Modell](#8-embeddings)
4. [Hardware-Architektur: Dual-Ollama](#hardware-architektur-dual-ollama)
5. [Konfiguration](#konfiguration)
6. [Qualitaetsvergleich](#qualitaetsvergleich-qwen314b-vs-30b-a3b)
7. [Migrations-Reihenfolge](#migrations-reihenfolge)
8. [VRAM-Referenz](#vram-referenz)

---

## Aktueller Stand (Produktion)

| Setting | Aktuell | GPU | Genutzt von |
|---------|---------|-----|-------------|
| `ollama_chat_model` | `qwen3:14b` | RTX 5070 Ti (cuda.local) | Chat, Fallback |
| `ollama_rag_model` | `qwen3:14b` | RTX 5070 Ti (cuda.local) | RAG-Antworten |
| `ollama_intent_model` | `qwen3:8b` | RTX 5070 Ti (cuda.local) | Intent-Erkennung |
| `ollama_embed_model` | `qwen3-embedding:4b` | RTX 5070 Ti (cuda.local) | Alle Embeddings (5 Services, 2560 dim) |
| `agent_model` | `qwen3:14b` | RTX 5060 Ti (renfield.local) | Agent Loop, Router |
| `proactive_enrichment_model` | `None` (Fallback auf chat_model) | — | Notification-Enrichment |

**Qwen3-Familie** — Vollstaendig migriert (Februar 2026). Alle Rollen nutzen Qwen3-Modelle mit `think=False` fuer schnelle, deterministische Antworten. Exzellentes Deutsch, zuverlaessiges JSON, starkes Tool-Calling.

**Vorher:** `gpt-oss:latest` (OpenAI GPT-4o-mini als lokales MoE-Modell, 20B Parameter). Code-Defaults sind noch `llama3.2` fuer Kompatibilitaet mit kleiner Hardware.

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

**Aktuell (PRD):** `qwen3:8b` — Exzellentes strukturiertes JSON, 100+ Sprachen, `think=False` fuer maximale Geschwindigkeit.

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

**Aktuell (PRD):** `qwen3:14b` — Dense 14B, `think=False`, sehr gutes Deutsch, ~40-60 tok/s auf 5070 Ti.

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

**Aktuell (PRD):** `qwen3:14b` — Gleiches Modell wie Chat, sehr gute Kontexttreue, wenig Halluzination.

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

**Aktuell (PRD):** `qwen3:14b` auf eigener GPU (RTX 5060 Ti, renfield.local) — `think=False`, ~30-40 tok/s, zuverlaessiges Tool-Calling, blockiert nicht Chat.

**WARNUNG: `mistral-small3.2:24b` passt NICHT auf 16 GB!** Ollama laedt nur 32/41 Layers auf die GPU, 4.2 GB Gewichte + 1 GB KV-Cache werden auf die CPU ausgelagert. Resultat: 79s Ladezeit, jeder Agent-Step laeuft in Timeout. MoE-Modelle wie `gpt-oss` (20B gesamt, nur 3.6B aktiv, ~13 GB) sind die bessere Wahl fuer 16 GB Karten.

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

**Aktuell (PRD):** Nutzt `agent_model` (`qwen3:14b`) auf Agent-GPU, `think=False`.

**Moeglicher Upgrade: `qwen3:0.6b`** (~0.5 GB VRAM) — Ultra-schnell, fuer einfache Kategorisierung ausreichend.

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

**Aktuell (PRD):** `ollama_model` = `qwen3:14b` auf Primary GPU, `think=False`. Exzellentes strukturiertes JSON.

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

**Aktuell (PRD):** Faellt auf `ollama_model` (`qwen3:14b`) zurueck.

**Moeglicher Upgrade: `qwen3:4b`** (~3 GB) — Fuer 1-Wort-Klassifikation und kurze Anreicherung ausreichend.

---

### 8. Embeddings

| Aspekt | Detail |
|--------|--------|
| **Config** | `ollama_embed_model` |
| **Services** | `ollama_service`, `rag_service`, `conversation_memory_service`, `intent_feedback_service`, `notification_service` |
| **Aufgabe** | Text → 768-dim Vektor fuer Cosine-Similarity (pgvector) |
| **Anforderung** | Deutsch+Englisch, konsistente Qualitaet, immer geladen |

**Aktuell (PRD):** `qwen3-embedding:4b` (~3 GB VRAM, 2560 dim) — \#1 auf MTEB Multilingual Leaderboard, exzellentes Deutsch, mit `EMBEDDING_DIMENSION=2560` und halfvec HNSW-Indexes.

**Budget:** `granite-embedding:278m` (~0.4 GB) — Deutlich besser auf Deutsch als nomic bei aehnlichem VRAM.

---

## Hardware-Architektur: Dual-Ollama

Zwei separate Ollama-Instanzen auf unterschiedlichen Hosts, jede mit eigener GPU. Renfield routet ueber `OLLAMA_URL` (Primary) und `AGENT_OLLAMA_URL` (Agent).

**Status: AKTIV IN PRODUKTION** seit Februar 2026.

### Netzwerk-Topologie

```
cuda.local (192.168.1.227)                renfield.local (Docker Host)
┌──────────────────────────┐              ┌──────────────────────────┐
│ Ollama (Host-Install)    │              │ Ollama (Host-Install)    │
│ RTX 5070 Ti 16 GB        │              │ RTX 5060 Ti 16 GB        │
│ 896 GB/s Bandwidth       │              │ 448 GB/s Bandwidth       │
│ Port 11434               │              │ Port 11434               │
└──────────┬───────────────┘              └──────────┬───────────────┘
           │                                         │
           │ LAN (1 Gbit)                            │ host.docker.internal
           │                                         │
     ┌─────┴─────────────────────────────────────────┴──────┐
     │ renfield-backend (Docker Container)                   │
     │ OLLAMA_URL=http://cuda.local:11434                    │
     │ AGENT_OLLAMA_URL=http://host.docker.internal:11434    │
     │ OLLAMA_FALLBACK_URL=http://host.docker.internal:11434 │
     └───────────────────────────────────────────────────────┘
```

**Hinweis:** cuda.local ist per Docker-DNS erreichbar (IP-basiert), nicht per Avahi/mDNS. Der Backend-Container loest `cuda.local` korrekt auf. `host.docker.internal` zeigt auf den Docker-Host (renfield.local), wo die zweite Ollama-Instanz laeuft.

### Warum kein GPU-Split?

Ein einzelnes grosses Modell (z.B. 30B) ueber zwei GPUs gesplittet hat Nachteile:
- GPU-zu-GPU Kommunikation ueber PCIe addiert Latenz bei **jedem** Token
- Bei MoE-Modellen besonders schlecht: Experts auf verschiedenen GPUs → staendiger Transfer
- Zwei separate 16 GB Instanzen bieten **echte Parallelitaet** statt serielle Verarbeitung

### Warum Dual-Ollama?

- **Parallelitaet:** Chat + Agent laufen gleichzeitig auf separater Hardware
- **Keine Blockierung:** Ein Agent-Loop (5-8 Schritte × 4-9s) blockiert keine Chat-Antworten
- **Multi-User:** Entscheidend fuer einen Household-Assistenten mit mehreren Nutzern
- **Fallback:** Wenn cuda.local offline, uebernimmt renfield.local automatisch (`OLLAMA_FALLBACK_URL`)
- **Bandbreite:** 896 + 448 = 1.344 GB/s gesamt — deutlich schneller als z.B. Mac Mini M4 Pro (273 GB/s)

### GPU-Zuweisung (Produktion — Qwen3)

```
Ollama Primary (cuda.local — RTX 5070 Ti 16 GB, 896 GB/s)
├── qwen3:14b          ~10 GB  [Chat + RAG + Memory]  keep_alive=5m
├── qwen3:8b            ~6 GB  [Intent]               keep_alive=5m
└── qwen3-embedding:4b  ~3 GB  [Embeddings, 2560 dim] keep_alive=-1

Ollama Agent (renfield.local — RTX 5060 Ti 16 GB, 448 GB/s)
└── qwen3:14b          ~10 GB  [Agent Loop + Router]  keep_alive=5m
```

Alle Modelle nutzen `think=False` fuer schnelle, deterministische Antworten. Dense-Architektur = vorhersagbare Latenz. Ollama laedt Modelle on-demand; qwen3:14b und qwen3:8b teilen sich den VRAM auf der Primary GPU (nur eines aktiv, `keep_alive=5m`).

### Host-Ollama vs Docker-Ollama

Die Produktion nutzt **Host-installiertes Ollama** (nicht Docker-Container):
- Einfacher: `systemctl start ollama` auf beiden Hosts
- Kein NVIDIA Container Toolkit noetig (Ollama nutzt GPU direkt)
- Modelle liegen in `~/.ollama/models/` auf dem Host-Filesystem
- Update: `ollama update` auf dem jeweiligen Host

---

## Konfiguration

### .env (Produktion — aktiv, Qwen3)

```bash
# === Ollama Primary (GPU 0 — RTX 5070 Ti 16 GB auf cuda.local) ===
OLLAMA_URL=http://cuda.local:11434
OLLAMA_CHAT_MODEL=qwen3:14b
OLLAMA_RAG_MODEL=qwen3:14b
OLLAMA_INTENT_MODEL=qwen3:8b
OLLAMA_EMBED_MODEL=qwen3-embedding:4b
OLLAMA_MODEL=qwen3:14b
EMBEDDING_DIMENSION=2560

# === Ollama Agent (GPU 1 — RTX 5060 Ti 16 GB auf renfield.local) ===
AGENT_ENABLED=true
AGENT_MODEL=qwen3:14b
AGENT_OLLAMA_URL=http://host.docker.internal:11434
AGENT_STEP_TIMEOUT=120.0
AGENT_TOTAL_TIMEOUT=600.0

# Fallback: Wenn cuda.local offline, nutze renfield.local's GPU
OLLAMA_FALLBACK_URL=http://host.docker.internal:11434
```

**`host.docker.internal`** wird von Docker zu der IP des Host-Systems aufgeloest. Da das Renfield-Backend als Docker-Container auf renfield.local laeuft, zeigt `host.docker.internal` auf die lokale Ollama-Instanz (RTX 5060 Ti).

**Fallback-Verhalten:** Wenn `OLLAMA_FALLBACK_URL` gesetzt ist und die primaere Ollama-Instanz (`cuda.local`) nicht erreichbar ist (ConnectError/ConnectTimeout innerhalb von 10s), wird der gleiche Request automatisch auf der Fallback-URL wiederholt. Implementiert in `utils/llm_client.py` (`_FallbackLLMClient`).

**Thinking-Mode:** Alle Qwen3-Aufrufe nutzen `think=False` (via `get_classification_chat_kwargs()` in `llm_client.py`). Household-Assistent braucht schnelle Antworten, nicht langes Nachdenken. `extract_response_content()` ist als Failsafe fuer den ollama-python 0.6.1 Bug implementiert.

### Modell-Vorladung

```bash
#!/bin/bash
# Modelle auf cuda.local (Primary GPU)
ssh cuda.local 'ollama pull qwen3:14b && ollama pull qwen3:8b && ollama pull qwen3-embedding:4b'

# Modelle auf renfield.local (Agent GPU)
ollama pull qwen3:14b
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

| Prio | Aktion | Impact | Aufwand | Status |
|------|--------|--------|---------|--------|
| ~~2~~ | ~~**Dual-Ollama aufsetzen**~~ | ~~Agent blockiert nicht mehr Chat~~ | ~~Mittel (Infra)~~ | ERLEDIGT |
| ~~1~~ | ~~**Embedding-Modell** → `qwen3-embedding:4b`~~ | ~~Deutsch-Retrieval massiv besser~~ | ~~Hoch (Re-Embedding)~~ | ERLEDIGT |
| ~~2~~ | ~~**Chat/RAG** → `qwen3:14b`~~ | ~~Natuerlichere Konversation~~ | ~~Gering (Config + think=False)~~ | ERLEDIGT |
| ~~3~~ | ~~**Intent** → `qwen3:8b`~~ | ~~Zuverlaessigeres JSON~~ | ~~Gering (Config)~~ | ERLEDIGT |
| ~~4~~ | ~~**Agent** → `qwen3:14b` auf zweiter GPU~~ | ~~Echtes Multi-Step Reasoning~~ | ~~Gering (Config + think=False)~~ | ERLEDIGT |

### Hinweise zur Embedding-Migration (abgeschlossen)

Die Migration von `nomic-embed-text` (768 dim) zu `qwen3-embedding:4b` (2560 dim) wurde durchgefuehrt:

1. **DB-Spalten:** Bereits auf `vector(2560)` resized (Alembic-Migration)
2. **HNSW-Indexes:** Nutzen `halfvec(2560)` Cast (pgvector 0.8.1 hat 2000-dim Limit fuer regulaere `vector`)
3. **Config:** `EMBEDDING_DIMENSION=2560` in `.env` gesetzt
4. **Re-Embedding:** Via `POST /admin/reembed` nach Modellwechsel (RAG-Chunks, Memories, Corrections, Suppressions)

---

## VRAM-Referenz

Ungefaehre Werte fuer Q4_K_M Quantisierung:

| Modell | Parameter | Architektur | VRAM (Gewichte) | + KV-Cache (8K) | Gesamt |
|--------|-----------|-------------|-----------------|-----------------|--------|
| **gpt-oss:latest** | **20B (3.6B aktiv)** | **MoE** | **~13 GB** | **~0.2 GB** | **~13.2 GB** |
| qwen3:0.6b | 0.6B | Dense | ~0.5 GB | ~0.1 GB | ~0.6 GB |
| qwen3:4b | 4B | Dense | ~3 GB | ~0.2 GB | ~3.2 GB |
| qwen3:8b | 8B | Dense | ~6 GB | ~0.3 GB | ~6.3 GB |
| qwen3:14b | 14B | Dense | ~10 GB | ~0.5 GB | ~10.5 GB |
| qwen3:30b-a3b | 30B (3B aktiv) | MoE | ~20 GB | ~0.2 GB | ~20.2 GB |
| qwen3:32b | 32B | Dense | ~22 GB | ~1.0 GB | ~23 GB |
| mistral-small3.2:24b | 24B | Dense | ~15 GB | ~1.7 GB | **~16.7 GB** |
| qwen3-embedding:4b | 4B | — | ~3 GB | — | ~3 GB |
| qwen3-embedding:0.6b | 0.6B | — | ~0.5 GB | — | ~0.5 GB |
| nomic-embed-text | 137M | — | ~0.3 GB | — | ~0.3 GB |

**16 GB GPU Limit:** Modelle bis ~14B Dense passen komfortabel. MoE-Modelle wie `gpt-oss` (20B gesamt, 3.6B aktiv, ~13 GB) nutzen 16 GB optimal — genug Headroom fuer KV-Cache bei hoher Qualitaet.

**WARNUNG zu 24B Dense auf 16 GB:** `mistral-small3.2:24b` ueberschreitet mit KV-Cache die 16 GB. Ollama laedt nur 32/41 Layers auf GPU, der Rest wird auf CPU ausgelagert → 79s Ladezeit, massive Latenz, in der Praxis unbrauchbar fuer Echtzeit-Anwendungen.

---

## Alternative: Budget-Setup (einzelne 16 GB GPU)

Falls nur eine GPU verfuegbar:

| Rolle | Modell | VRAM |
|-------|--------|------|
| Alles (Chat/RAG/Intent/Agent) | `qwen3:8b` | ~6 GB |
| Embeddings | `qwen3-embedding:0.6b` | ~0.5 GB |
| **Gesamt** | | **~6.5 GB** |

Qualitativ schwaecher, aber bereits deutlich besser als der aktuelle `llama3.2:3b` Stand.
