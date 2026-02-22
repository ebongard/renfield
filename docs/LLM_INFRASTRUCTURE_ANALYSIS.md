# LLM Infrastructure Analysis

**Datum:** 2026-02-22
**Autor:** Claude Opus 4.6

---

## Inhaltsverzeichnis

1. [Aktuelles Produktions-Setup](#1-aktuelles-produktions-setup)
2. [Teil A: Mac Mini M4 Pro als Alternative](#2-teil-a-mac-mini-m4-pro-als-alternative)
3. [Teil B: Ollama-Ersatz auf dem Dual-GPU-Setup](#3-teil-b-ollama-ersatz-auf-dem-dual-gpu-setup)
4. [Vergleich aller LLM-Serving-Frameworks](#4-vergleich-aller-llm-serving-frameworks)
5. [Renfield Ollama-Integration & Migrationsaufwand](#5-renfield-ollama-integration--migrationsaufwand)
6. [Empfehlung](#6-empfehlung)
7. [Quellen](#7-quellen)

---

## 1. Aktuelles Produktions-Setup

Renfield nutzt ein Dual-Ollama-Setup mit zwei dedizierten NVIDIA GPUs auf separaten Hosts. Beide laufen als `systemctl`-Services mit host-installiertem Ollama.

### Hardware

| | GPU 1 (cuda.local) | GPU 2 (renfield.local) |
|---|---|---|
| **GPU** | NVIDIA RTX 5070 Ti | NVIDIA RTX 5060 Ti |
| **VRAM** | 16 GB dediziert | 16 GB dediziert |
| **Bandwidth** | 896 GB/s | 448 GB/s |
| **Gesamt** | **32 GB VRAM, 1.344 GB/s kombiniert** | |

### Modell-Verteilung

| GPU | Modell | Aufgabe | VRAM | tok/s (geschätzt) |
|-----|--------|---------|------|--------------------|
| cuda.local | qwen3:14b | Chat, RAG, Memory Extraction | ~10 GB | 40-60 |
| cuda.local | qwen3:8b | Intent Recognition | ~6 GB | 50-70 |
| cuda.local | qwen3-embedding:4b | Embeddings (2560 dim) | ~3 GB | ~100 emb/s |
| renfield.local | qwen3:14b | Agent Loop, Router Classification | ~10 GB | 30-40 |

### Architektur-Vorteile

- **Echte Parallelität:** Chat auf GPU 1, Agent auf GPU 2 — kein Blocking
- **Graceful Degradation:** Automatischer Fallback auf GPU 2 wenn cuda.local offline
- **Isolierte Workloads:** Embedding-Generierung blockiert nie den Agent Loop
- Alle Modelle mit `think=False` für maximale Geschwindigkeit

### Konfiguration

```bash
# Primary (cuda.local — RTX 5070 Ti)
OLLAMA_URL=http://cuda.local:11434
OLLAMA_CHAT_MODEL=qwen3:14b
OLLAMA_RAG_MODEL=qwen3:14b
OLLAMA_INTENT_MODEL=qwen3:8b
OLLAMA_EMBED_MODEL=qwen3-embedding:4b

# Agent (renfield.local — RTX 5060 Ti)
AGENT_OLLAMA_URL=http://host.docker.internal:11434
AGENT_MODEL=qwen3:14b

# Fallback
OLLAMA_FALLBACK_URL=http://host.docker.internal:11434
```

---

## 2. Teil A: Mac Mini M4 Pro als Alternative

**Fragestellung:** Würde ein LLM auf vLLM oder LM Studio auf dem Mac Mini M4 Pro (64 GB RAM) eine bessere Performance als das aktuelle Produktions-Setup garantieren?

**Ergebnis:** Nein. Das aktuelle Dual-GPU-Setup ist in allen relevanten Metriken deutlich überlegen.

### Spezifikationen M4 Pro

| Eigenschaft | Wert |
|---|---|
| **CPU** | Apple M4 Pro (14-Core) |
| **Memory** | 64 GB Unified Memory (geteilt mit OS + GPU) |
| **Memory Bandwidth** | 273 GB/s |
| **GPU Cores** | 20 GPU Cores (Metal) |
| **Nutzbar für LLM** | ~48 GB (75% von 64 GB) |

### Performance-Vergleich

| Metrik | Dual-GPU (aktuell) | M4 Pro 64GB | Faktor |
|--------|-------------------|-------------|--------|
| **Memory Bandwidth** | 1.344 GB/s | 273 GB/s | **~5x zugunsten Dual-GPU** |
| **qwen3:14b tok/s** | 40-60 | 18-25 (MLX Q4) | **~2-3x zugunsten Dual-GPU** |
| **qwen3:8b tok/s** | 50-70 | 35-45 (MLX Q4) | **~1.5x zugunsten Dual-GPU** |
| **Parallelität** | Chat + Agent gleichzeitig | Seriell / Time-Shared | **kein Vergleich** |
| **Dedizierter VRAM** | 32 GB (nur LLM) | ~48 GB (geteilt mit OS) | M4 Pro hat mehr Kapazität |

### vLLM auf Apple Silicon — Status

- **vLLM Core**: Kein GPU-Support auf macOS. CPU-only = ~1-2 tok/s. Unbrauchbar.
- **vllm-mlx**: Community-Projekt, pre-1.0. MLX-Backend, ~20-30% schneller als Ollama auf Apple Silicon. Aber: ein Modell pro Instanz, kein Ollama-API, Embedding-Support limitiert.
- **vllm-metal**: Experimentell, keine Benchmarks, kein Embedding-Support.

### LM Studio auf Apple Silicon — Status

- **MLX-Backend**: 20-40% schneller als Ollama (llama.cpp) auf Apple Silicon
- **Multi-Modell**: Ja, mit Auto-Evict
- **OpenAI-API**: Ja, aber Renfield nutzt `ollama.AsyncClient` — Migration nötig
- **qwen3-embedding Bug**: Klassifikations-Bug in MLX-Format (Bug #808), GGUF inkonsistent (Bug #696)
- **Concurrent Batching (MLX)**: Erst seit Feb 2026, sehr frisch

### Fazit Teil A

Der M4 Pro kann das Dual-GPU-Setup nicht ersetzen. Sinnvolle Einsatzmöglichkeiten:
- Fallback-Server wenn beide GPUs ausfallen
- Entwicklungs-/Testumgebung
- Dedizierter Embedding-Server oder Background-Task-Runner (KG Extraction, Paperless Audit)

---

## 3. Teil B: Ollama-Ersatz auf dem Dual-GPU-Setup

**Fragestellung:** Welche Vorteile würde ein Wechsel von Ollama auf vLLM oder andere Frameworks auf dem bestehenden Dual-GPU-Setup bringen? Welche Alternativen gibt es?

### 3.1 Ollamas Schwächen

| Schwäche | Auswirkung auf Renfield |
|----------|------------------------|
| **Kein Continuous Batching** | Requests werden serialisiert. User #2 wartet auf User #1. |
| **Kein PagedAttention** | KV-Cache-Verschwendung durch zusammenhängende Allokation (~20-30% Overhead) |
| **Kein Prefix Caching** | Der identische System-Prompt bei jeder Intent-Erkennung wird jedes Mal neu berechnet |
| **Kein Speculative Decoding** | Keine Möglichkeit, durch Draft-Modelle 1.5-2.8x Speedup zu erzielen |
| **Begrenzte Quantisierung** | Nur GGUF-Varianten. Kein FP8, NVFP4, AWQ, GPTQ mit optimierten Kernels |
| **Keine CUDA Graphs** | CPU-Overhead bei jeder Token-Generierung |

### 3.2 vLLM — Der Hauptkandidat

#### Performance vs. Ollama (NVIDIA)

| Szenario | Ollama | vLLM | Faktor |
|----------|--------|------|--------|
| **Single User** | 40-60 tok/s | 45-65 tok/s | ~1.1x |
| **5 Concurrent Users** | ~40 tok/s (serialisiert) | ~200 tok/s (batched) | **~5x** |
| **128 Concurrent** | Kollaps | 3.554 tok/s (API-short) | **~90x** |
| **P99 Latency** | 673 ms | 80 ms | **~8x besser** |

Quellen: Red Hat Benchmarks (Aug 2025), arXiv:2601.09527 (Jan 2026)

#### Features die Ollama fehlen

| Feature | Beschreibung | Renfield-Relevanz |
|---------|-------------|-------------------|
| **Continuous Batching** | Neue Requests werden dynamisch in laufende Batches eingefügt | **Hoch** — Mehrbenutzer-Haushalt |
| **PagedAttention** | KV-Cache wie Virtual Memory — 19-27% weniger VRAM-Verschwendung | **Mittel** — Mehr Platz für Kontext |
| **Prefix Caching** | Hash-basiertes KV-Cache-Reuse für identische Prompt-Präfixe. TTFT sinkt von ~4s auf ~0.6s | **Sehr hoch** — Renfields System-Prompt ist bei jeder Intent-Erkennung identisch |
| **Speculative Decoding** | Draft-Modell generiert Tokens spekulativ, Hauptmodell validiert. 1.5-2.8x Speedup | **Hoch** — Schnellere Chat-Antworten |
| **NVFP4 Quantisierung** | Native Blackwell 4-bit mit Hardware-Beschleunigung. 1.6x Throughput über BF16 | **Hoch** — RTX 50-Serie kann das nativ |
| **FP8 W8A8** | 8-bit Weight+Activation, <1% Qualitätsverlust | **Hoch** — Fast lossless Kompression |
| **Structured Output** | Token-level JSON-Schema-Enforcement | **Mittel** — Garantiert valides JSON |
| **Chunked Prefill** | Überlappung von Prefill und Decode für niedrigere TTFT | **Mittel** |

#### Multi-GPU-Support

| Methode | Beschreibung | Für Renfield? |
|---------|-------------|---------------|
| **Tensor Parallelism** | Modell-Layer über GPUs aufteilen (gleiche Maschine) | **Nein** — Erfordert identische GPUs. RTX 5070 Ti ≠ RTX 5060 Ti |
| **Pipeline Parallelism** | Modell sequentiell über GPUs aufteilen (auch über Hosts) | **Bedingt** — Funktioniert, aber bottlenecked an der langsameren GPU |
| **Separate Instanzen** | Ein Modell pro GPU (wie aktuell mit Ollama) | **Ja** — Empfohlen für heterogene GPUs |

#### Nachteile

| Nachteil | Details |
|----------|---------|
| **Ein Modell pro Instanz** | Renfield braucht 4 Modelle → 3-4 separate vLLM-Prozesse |
| **Startup: 30-100 Sekunden** | CUDA Graph Capture + torch.compile vs. Ollama's 2-5s |
| **Kein GGUF-Ökosystem** | Braucht SafeTensors/AWQ/GPTQ/FP8 von HuggingFace |
| **~4 GB VRAM Overhead** | PyTorch + vLLM Runtime pro Instanz |
| **Operationale Komplexität** | Docker Compose mit 3-4 Instanzen vs. 1 Ollama-Prozess |

### 3.3 SGLang — Der aufsteigende Star

SGLang (von LMSYS, den Chatbot-Arena-Machern) ist zunehmend der Benchmark-Leader.

| Eigenschaft | Wert |
|---|---|
| **Throughput vs. vLLM** | ~29% höher (16.215 vs. 12.553 tok/s auf H100) |
| **TTFT** | 79ms vs. vLLM's 103ms (Mean) |
| **RadixAttention** | Intelligentes KV-Cache-Reuse über Requests hinweg |
| **Model Gateway** | Multi-Modell-Routing mit Load Balancing |
| **Embedding-Support** | Ja (e5-mistral, gte, etc.) |
| **GGUF-Support** | Ja (aber sekundär, SafeTensors primär) |
| **Quantisierung** | FP4, FP8, AWQ, GPTQ, Marlin, MXFP4, GGUF, bitsandbytes |
| **Maturity** | Hoch, schnelles Wachstum, Day-0 Support für neue Modelle |

**Vorteil über vLLM:** Bessere Performance unter hoher Last, integrierter Model Gateway für Multi-Modell.

**Nachteil:** Etwas jünger als vLLM, weniger Enterprise-Referenzen.

### 3.4 llama.cpp Server — Der pragmatische Weg

Die Engine die Ollama bereits nutzt — direkt, ohne Abstraktions-Overhead.

| Eigenschaft | Wert |
|---|---|
| **Performance vs. Ollama** | +20-30% (Ollama's Overhead entfällt) |
| **GGUF-Kompatibilität** | Identisch — selbe Modelle wie aktuell |
| **Multi-Modell** | Via llama-swap (Go-basierter Proxy mit Hot-Swapping) |
| **Embedding-Support** | Ja (`--embeddings` Flag, `/v1/embeddings`) |
| **Blackwell-Optimierungen** | MXFP4, CUDA Graphs, Flash Attention nativ |
| **Startup** | Sekunden (kompiliertes C++, kein Python/PyTorch) |
| **VRAM-Overhead** | Minimal (~0.5 GB vs. vLLM's ~4 GB) |

**Vorteil:** Niedrigstes Risiko. Selbe Modelle, selbe Engine, nur ohne Ollama-Overhead. llama-swap gibt Multi-Modell-Fähigkeit.

**Nachteil:** Kein Continuous Batching auf dem Niveau von vLLM/SGLang. Serialisierte Requests unter Last.

### 3.5 Weitere Alternativen

| Framework | Stärke | Schwäche | Empfehlung |
|-----------|--------|----------|------------|
| **LocalAI** | Multi-Backend Swiss Army Knife, volle OpenAI-API, Docker-ready, Multi-Modell built-in | Performance = Backend-abhängig, keine eigene Optimierung | Möglich als Ollama-Ersatz mit mehr Flexibilität |
| **TensorRT-LLM** | Absolute Peak-Performance auf NVIDIA. Native FP4/FP8 | Extrem komplex. Modell-Kompilierung nötig. Multi-Modell nur via Triton Server | Overkill für Home-Assistant |
| **Aphrodite** | Breiteste Quantisierungs-Unterstützung (FP2-FP12, GGUF, GPTQ, AWQ, EXL2, ...) | Kein Multi-Modell, Embedding-Support unklar | Nische |
| **TabbyAPI** | Schnellstes EXL2-Serving, Multi-GPU Tensor Parallelism, Embedding-Support | Kleines EXL2-Ökosystem, kein GGUF | Interessant wenn EXL2-Qualität gewünscht |
| **ExLlamaV2** | Beste Qualität/VRAM-Ratio durch Mixed-Precision EXL2 (2-8 bit per Layer) | Library, kein Server (nutze TabbyAPI) | Via TabbyAPI |
| **TGI (HuggingFace)** | Ehemals Referenz | **Maintenance Mode seit Dez 2025.** Keine neuen Features. | Nicht empfohlen |
| **KTransformers** | 671B-Modelle auf Consumer-Hardware (CPU/GPU Hybrid) | Nur für MoE-Modelle die nicht in VRAM passen | Irrelevant für 8B-14B Modelle |

---

## 4. Vergleich aller LLM-Serving-Frameworks

### Performance-Matrix

| Framework | Single User | 5 Concurrent | Startup | VRAM Overhead |
|-----------|------------|--------------|---------|---------------|
| **Ollama** (aktuell) | 40-60 tok/s | ~40 tok/s (serialisiert) | 2-5s | ~0.5 GB |
| **llama.cpp server** | 50-75 tok/s | ~50 tok/s | 2-5s | ~0.5 GB |
| **vLLM** | 45-65 tok/s | ~200 tok/s | 30-100s | ~4 GB |
| **SGLang** | 50-70 tok/s | ~260 tok/s | 30-60s | ~4 GB |
| **TensorRT-LLM** | 60-100 tok/s | ~300 tok/s | Minuten | ~3 GB |
| **TabbyAPI/EXL2** | 55-80 tok/s | ~55 tok/s | 5-10s | ~1 GB |
| **LocalAI** | = Backend | = Backend | 5-15s | ~1 GB |

### Feature-Matrix

| Feature | Ollama | llama.cpp | vLLM | SGLang | LocalAI | TensorRT | TabbyAPI |
|---------|--------|-----------|------|--------|---------|----------|---------|
| **Continuous Batching** | Nein | Nein | Ja | Ja | Backend | Ja | Begrenzt |
| **Prefix Caching** | Nein | Nein | Ja (auto) | Ja (RadixAttn) | Nein | Ja | Nein |
| **Speculative Decoding** | Nein | Nein | Ja | Ja | Nein | Ja | Nein |
| **Multi-Modell** | Ja (auto) | Via llama-swap | Nein (1/Instanz) | Model Gateway | Ja (built-in) | Via Triton | Hot-Swap |
| **Embedding-API** | Ja | Ja | Ja | Ja | Ja | Ja | Ja |
| **GGUF** | Ja | Ja | Experimentell | Ja (sekundär) | Ja | Nein | Nein |
| **AWQ/GPTQ** | Nein | Nein | Ja (optimiert) | Ja | Via Backend | AWQ | GPTQ |
| **FP8/NVFP4** | Nein | MXFP4 | Ja (nativ) | Ja | Nein | Ja (nativ) | Nein |
| **EXL2** | Nein | Nein | Nein | Nein | Via Backend | Nein | Ja |
| **OpenAI API** | Ja (/v1/) | Ja (/v1/) | Ja | Ja | Ja (Drop-in) | Ja | Ja |
| **Docker** | Offiziell | Verfügbar | Offiziell | Offiziell | Offiziell | NGC | Nein |
| **Setup-Komplexität** | Sehr niedrig | Niedrig | Mittel | Mittel | Niedrig | Sehr hoch | Mittel |
| **Produktion** | Erprobt | Erprobt | LinkedIn, Amazon, Stripe | LMSYS | Community | NVIDIA Datacenter | Community |

---

## 5. Renfield Ollama-Integration & Migrationsaufwand

### Architektur der LLM-Anbindung

Renfield hat eine **gut designte Abstraktionsschicht** in `utils/llm_client.py`:

```python
@runtime_checkable
class LLMClient(Protocol):
    async def chat(self, model, messages, *, stream, options, **kwargs) -> Any: ...
    async def embeddings(self, model, prompt, *, options, **kwargs) -> Any: ...
```

**Nur 1 Datei** importiert direkt `ollama` — alle 11 Services nutzen die Protocol-Factory.

### Betroffene Services

| Service | Methode | Aufwand |
|---------|---------|--------|
| OllamaService | `chat()`, `embeddings()`, `generate()`, `list()`, `pull()` | Mittel |
| AgentService | `chat()` | Niedrig (via Protocol) |
| AgentRouter | `chat()` | Niedrig (via Protocol) |
| RAGService | `embeddings()` | Niedrig (via Protocol) |
| KnowledgeGraphService | `chat()`, `embeddings()` | Niedrig (via Protocol) |
| IntentFeedbackService | `embeddings()` | Niedrig (via Protocol) |
| ConversationMemoryService | `embeddings()` | Niedrig (via Protocol) |
| NotificationService | `generate()` (2x) | Mittel (Refactor zu `chat()`) |
| PaperlessAuditService | Indirekt via MCP | Kein Aufwand |

### Ollama-spezifische API-Methoden

| Methode | Nutzung | Migrationsaufwand |
|---------|---------|-------------------|
| `chat()` | ~15 Aufrufe in 8 Services | Niedrig — OpenAI-API ist äquivalent |
| `embeddings()` | 6 Services | Niedrig — `/v1/embeddings` Standard |
| `generate()` | 2 Aufrufe in NotificationService | Mittel — Refactor zu `chat()` |
| `list()` | 2 Aufrufe (Model-Check) | Backend-spezifisch |
| `pull()` | 1 Aufruf (Model-Download) | Backend-spezifisch |

### Geschätzter Migrationsaufwand

| Phase | Beschreibung | Aufwand |
|-------|-------------|--------|
| 1. Abstraktionsschicht erweitern | `LLMClient` Protocol + Factory anpassen | 0.5 Tage |
| 2. Client-Instantiierung | `create_llm_client()` für neues Backend | 0.5 Tage |
| 3. Backend-spezifische Methoden | `list()`, `pull()`, `generate()` ersetzen | 1-2 Tage |
| 4. Konfiguration | Neue Env-Vars, Docker Compose | 0.5 Tage |
| 5. Testing | Unit-Tests, Integration, E2E | 1-2 Tage |
| **Gesamt** | | **3-5 Tage** |

### Dank des LLMClient Protocols: 11 von 12 Services brauchen keine Code-Änderungen.

---

## 6. Empfehlung

### Tier 1: Empfohlene Optionen

#### Option A: Hybrid — vLLM für Chat/Agent + Ollama für Embeddings/Utility

```
RTX 5070 Ti: vLLM (qwen3:14b FP8) → Chat, RAG, Agent
RTX 5060 Ti: Ollama (qwen3:8b GGUF + qwen3-embedding:4b) → Intent, Embeddings
```

**Vorteile:**
- Continuous Batching + Prefix Caching für die latenz-kritischen Workloads
- Prefix Caching: TTFT für Intent-Erkennung sinkt von ~4s auf ~0.6s (selber System-Prompt)
- FP8-Quantisierung: <1% Qualitätsverlust, ~1.5x mehr Throughput
- Ollama bleibt für unkritische Workloads (einfach, bewährt)
- Migrationsaufwand ~3 Tage (nur Chat/Agent-Pfad umstellen)

**Nachteile:**
- Zwei verschiedene Systeme zu warten
- vLLM Startup-Zeit 30-100s (Sleep Mode hilft)

#### Option B: llama.cpp server + llama-swap (Minimal-Migration)

```
RTX 5070 Ti: llama-server + llama-swap (qwen3:14b, qwen3:8b, qwen3-embedding:4b)
RTX 5060 Ti: llama-server (qwen3:14b für Agent)
```

**Vorteile:**
- Selbe GGUF-Modelle, selbe Engine — nur Ollama-Overhead entfällt (+20-30%)
- Startup in Sekunden (wie Ollama)
- Minimaler VRAM-Overhead
- llama-swap gibt Multi-Modell mit Hot-Swapping
- Blackwell MXFP4 + CUDA Graphs + Flash Attention nativ
- Migrationsaufwand ~2 Tage

**Nachteile:**
- Kein Continuous Batching (serialisiert wie Ollama)
- Kein Prefix Caching
- Manuelles Model-Management statt `ollama pull`

### Tier 2: Alternativen

#### Option C: SGLang (Maximum Performance)

- Beste Throughput-Zahlen aller Frameworks
- Model Gateway für Multi-Modell
- RadixAttention für aggressives KV-Cache-Reuse
- Aufwand: ~4 Tage (ähnlich wie vLLM)
- Risiko: Jünger als vLLM, weniger Enterprise-Referenzen

#### Option D: LocalAI (Drop-in Ollama-Ersatz)

- Multi-Model, Full OpenAI-API, Docker-ready
- Nutzt llama.cpp als Backend (gleiche Performance)
- Zusätzlich: Bild-Generierung, Audio, etc.
- Aufwand: ~1-2 Tage (OpenAI-API statt Ollama-API)
- Risiko: Weniger production-erprobt

### Nicht empfohlen

| Option | Grund |
|--------|-------|
| **TensorRT-LLM** | Overkill. Modell-Kompilierung, Triton für Multi-Modell — zu komplex für Home-Assistant |
| **TGI** | Maintenance Mode seit Dez 2025. Keine neuen Features. |
| **KTransformers** | Nur für MoE-Modelle >100B die nicht in VRAM passen |

### Gesamtempfehlung

**Kurzfristig (jetzt):** Option B — llama.cpp server + llama-swap. Niedrigstes Risiko, +20-30% Performance, selbe Modelle, 2 Tage Aufwand.

**Mittelfristig (Q2 2026):** Option A — Hybrid vLLM + Ollama. Wenn Multi-User-Last zunimmt und Prefix Caching für Intent-Erkennung gewünscht ist. 3 Tage Aufwand, signifikanter Concurrent-Performance-Gewinn.

**Langfristig (Q3+ 2026):** SGLang beobachten. Wenn Model Gateway und RadixAttention ausgereift sind, könnte SGLang die beste All-in-One-Lösung werden.

---

## 7. Quellen

### Benchmarks & Vergleiche
- [Red Hat: Ollama vs vLLM Deep Dive (Aug 2025)](https://developers.redhat.com/articles/2025/08/08/ollama-vs-vllm-deep-dive-performance-benchmarking)
- [Joshua8.AI: vLLM vs SGLang vs Ollama on Blackwell (Jan 2026)](https://joshua8.ai/llm-inference-benchmark/)
- [Private LLM Inference on Consumer Blackwell GPUs (arXiv:2601.09527)](https://arxiv.org/html/2601.09527v1)
- [SGLang vs vLLM Benchmark](https://medium.com/@saidines12/sglang-vs-vllm-part-1-benchmark-performance-3231a41033ca)
- [Ollama vs llama.cpp 2025](https://neuralnet.solutions/ollama-vs-llama-cpp-which-framework-is-better-for-inference)
- [Llama.cpp vs Ollama 2026](https://www.openxcell.com/blog/llama-cpp-vs-ollama/)
- [GPTQ vs AWQ vs EXL2 vs llama.cpp Comparison](https://oobabooga.github.io/blog/posts/gptq-awq-exl2-llamacpp/)

### Framework-Dokumentation
- [vLLM Docs](https://docs.vllm.ai/en/stable/) — [Parallelism](https://docs.vllm.ai/en/stable/serving/parallelism_scaling/) — [Quantization](https://docs.vllm.ai/en/latest/features/quantization/) — [Prefix Caching](https://docs.vllm.ai/en/stable/design/prefix_caching/) — [Speculative Decoding](https://docs.vllm.ai/en/latest/features/spec_decode/) — [Sleep Mode](https://blog.vllm.ai/2025/10/26/sleep-mode.html)
- [SGLang Docs](https://docs.sglang.io/) — [Quantization](https://docs.sglang.ai/advanced_features/quantization.html) — [Embedding Models](https://docs.sglang.ai/supported_models/embedding_models.html)
- [llama.cpp](https://github.com/ggml-org/llama.cpp) — [llama-swap](https://github.com/mostlygeek/llama-swap)
- [LocalAI](https://localai.io/) — [GPU Acceleration](https://localai.io/features/gpu-acceleration/)
- [TensorRT-LLM](https://github.com/NVIDIA/TensorRT-LLM) — [Quick Start](https://nvidia.github.io/TensorRT-LLM/quick-start-guide.html)
- [ExLlamaV2](https://github.com/turboderp-org/exllamav2) — [TabbyAPI](https://github.com/theroyallab/tabbyAPI)
- [Aphrodite Engine](https://github.com/aphrodite-engine/aphrodite-engine)
- [KTransformers](https://github.com/kvcache-ai/ktransformers)

### Apple Silicon
- [Apple M4 Pro Spezifikationen](https://www.apple.com/newsroom/2024/10/apple-introduces-m4-pro-and-m4-max/)
- [vllm-project/vllm-metal](https://github.com/vllm-project/vllm-metal)
- [waybarrios/vllm-mlx (EuroMLSys '26)](https://arxiv.org/html/2601.19139v2)
- [LM Studio 0.4.2 — MLX Batching](https://lmstudio.ai/changelog/lmstudio-v0.4.2)
- [Gemma 3: LM Studio vs Ollama (M3 Ultra)](https://medium.com/google-cloud/gemma-3-performance-tokens-per-second-in-lm-studio-vs-ollama-mac-studio-m3-ultra-7e1af75438e4)
- [Production LLM on Apple Silicon (arXiv:2511.05502)](https://arxiv.org/abs/2511.05502)

### Production References
- [vLLM: LinkedIn, Amazon, Stripe, Roblox](https://blog.vllm.ai/2025/01/10/vllm-2024-wrapped-2025-vision.html)
- [vLLM Large Scale Serving (Dec 2025)](https://blog.vllm.ai/2025/12/17/large-scale-serving.html)
- [NVIDIA: Open Source AI Tool Upgrades on RTX PCs](https://developer.nvidia.com/blog/open-source-ai-tool-upgrades-speed-up-llm-and-diffusion-models-on-nvidia-rtx-pcs/)
