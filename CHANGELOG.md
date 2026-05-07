# Changelog

Alle markanten Änderungen an Renfield, seit Release `v1.2.0`. Format lehnt sich an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/) an; Versionierung folgt [SemVer](https://semver.org/lang/de/).

---

## [v2.5.0] — 2026-05-07

Zwei strukturelle Migrationen, die zusammen die Inferenz-Topologie des Clusters umkrempeln. Erstens: der Text-LLM-Tier zieht von Ollama auf einen llama.cpp-Server mit Qwen3.6-35B-A3B (MoE, 35B Parameter / ~3B aktiv) auf k8s-gpu-1 mit Tensor-Split über zwei GPUs ([#527](https://github.com/ebongard/renfield/pull/527)). Zweitens: die komplette Voice-Pipeline (STT, TTS, Speaker-Embedding) wandert vom Backend-Pod auf einen GPU-residenten `voice-server`-Microservice auf k8s-gpu-3, mit Streaming-WebSocket statt der bisherigen Request-Response-REST. Frontend-UI für Voice ist jetzt streaming-fähig — Live-Transcripts während des Sprechens, Sentence-Streaming-TTS, Sprecher-Identitäts-Threading via biometrischem Embedding ([#531](https://github.com/ebongard/renfield/pull/531) – [#536](https://github.com/ebongard/renfield/pull/536)).

Empirisch validiert: Voice-Query "Wann war die erste Mondlandung?" nach Phase B → letzte Audio-Ausgabe 7,1 s nach `final_transcript` (Browser-Timing-Diagnostics, einmaliger Test). ECAPA-TDNN-Embeddings vom voice-server bestehen Cosine-Parity-Test mit ≥ 0,999999 vs. in-process speechbrain auf 10 deutschen Fixtures (`tests/backend/test_speaker_service.py::TestEcapaOnnxParity`).

### Hinzugefügt

- **Text-LLM-Tier auf llama-server** — Neue k8s-Deployments `llama-server-agent` (k8s-gpu-1, dual-GPU mit `--tensor-split 16,16`) für Chat/Agent/RAG/Intent/KG/Memory mit Qwen3.6-35B-A3B Q4_K_XL, sowie `llama-server-embed` (k8s-gpu-2, CPU-only) für Embeddings mit Qwen3-Embedding-4B Q4_K_M. Backend bekommt einen neuen `OpenAICompatibleClient`-Adapter (`utils/llm_client.py`), der das bestehende `LLMClient`-Protocol erfüllt; per-Tier-Routing via `LLM_OPENAI_FOR_*`-ENV-Variablen erlaubt graduelle Umstellung. Ollama-Service kann komplett entfernt werden, sobald alle Tiers umgestellt sind ([#527](https://github.com/ebongard/renfield/pull/527)).
- **`voice-server` Microservice** — Neues Python-Package `voice-server/` (FastAPI + faster-whisper + Piper + ECAPA-TDNN ONNX) als eigenes k8s-Deployment auf k8s-gpu-3 (RTX 4060 Ti). Stateless: keine DB, kein Redis. Liefert STT mit streaming Partial-Transcripts (`vad_filter=False` auf dem Partial-Pass, Konfidenz-Floor 0,5 schützt gegen near-silence-Halluzinationen), TTS mit Sentence-Streaming und 24-Byte-RFWA-Header pro Binary-Frame, sowie ECAPA-TDNN-Speaker-Embeddings (192-dim float32) per Utterance ([#531](https://github.com/ebongard/renfield/pull/531)).
- **Streaming-WebSocket `/ws/voice`** — Frontend-Hook `useVoiceStream` ersetzt die Request-Response-Paarung aus `useAudioRecording` + REST-`speakText`. Ein bidirektionaler WebSocket transportiert MediaRecorder-Chunks (audio/webm;codecs=opus) zum Server und empfängt `partial_transcript`-Events während des Sprechens, `final_transcript` mit Speaker-Embedding bei VAD-Stop, sowie RFWA-getaggte WAV-Chunks für TTS. Inter-Utterance-Protokoll: jede Sprach-Aufnahme sendet ein neues `session_start`, voice-server räumt Decoder + State zwischen Utterances auf — mehrere Sprach-Queries hintereinander funktionieren ohne Fehler-Spam ([#534](https://github.com/ebongard/renfield/pull/534), [#536](https://github.com/ebongard/renfield/pull/536)).
- **Sentence-Streaming-Auto-TTS** — Sobald während des Chat-Streams ein Satz-Ende (`[.!?]\s`) erkannt wird, dispatch das Frontend ein `tts_request` für genau diesen Satz. Voice-server synthetisiert die Anfragen parallel (asyncio-Task pro Request, 4-Sätze-Antwort gemessen: alle vier Binary-Frames innerhalb von 43 ms). End-to-end-Latenz auf Browser-Diagnostics-Sample (Mondlandungs-Query, einmaliger Test): 7,1 s vom `final_transcript` bis zum letzten Audio-Frame; ein vorhergehender Test mit derselben Anfrage und voller Response-then-TTS-Architektur lag bei 29,4 s — Variabilität durch LLM-Streaming-Geschwindigkeit ist groß, mehrfache Messungen würden eine Bandbreite zeigen ([#536](https://github.com/ebongard/renfield/pull/536)).
- **Browser-seitige Silence-Auto-Stop** — `useVoiceStream` baut einen AnalyserNode-RMS-Loop auf, der nach 1,5 s Stille den Recorder stoppt und `stt_flush` sendet — wie der Legacy-`useAudioRecording`-Hook. Kombiniert mit der server-seitigen VAD (Safety-Net nach 2,0 s Stille) ist das Ende einer Äußerung jetzt zuverlässig auto-detektiert ([#536](https://github.com/ebongard/renfield/pull/536)).
- **Listening-Indicator + Live-Partial-Transcript** — Während der Sprach-Aufnahme zeigt das ChatInput jetzt sowohl die Audio-Pegel-Visualisierung (RMS-getrieben) als auch eine kursive blaue Bubble mit dem aktuellen Partial-Transcript. AudioLevel-State auf 15 Hz gedrosselt (vom 60-Hz-rAF-Loop), damit Context-Re-Renders die `setPartialText`-Updates nicht verschlucken ([#534](https://github.com/ebongard/renfield/pull/534), [#536](https://github.com/ebongard/renfield/pull/536)).
- **Sprecher-Identitäts-Threading (Option B)** — Wenn das Wire-Embedding einen Speaker mit verlinktem User auflöst (via `User.speaker_id`-FK) UND `speaker_confidence ≥ voice_auth_min_confidence` (0,7, gleiche Schwelle wie `/api/auth/voice`), benutzt der Chat-Handler dessen `User.id` als request-scoped Identity. Schließt die Lücke, dass Sprach-Queries mit `AUTH_ENABLED=false` an public-tier-only-RAG hängen geblieben wären, obwohl das System den Sprecher biometrisch erkannt hat. Gäste mit nicht-verlinktem Auto-Enrol-Speaker bleiben weiterhin auf `user_id=None` — Option C (pro-Speaker-Identity-Refactor mit eigenem Identity-Value-Object für Speaker-id-backed-Sessions) wurde im Design diskutiert und zurückgestellt; kein Tracking-Issue, kein Datum ([#534](https://github.com/ebongard/renfield/pull/534)).
- **`speaker_resolver`-Service** — Neuer Backend-Service `services/speaker_resolver.py` extrahiert die find-or-auto-enroll-Logik (vorher inline in `whisper_service.transcribe_bytes_with_speaker`) als wiederverwendbare Funktion. Wird sowohl vom Chat-WS-Handler (Wire-Embedding aus dem Frontend) als auch vom REST-Voice-Chat-Orchestrator verwendet. Continuous-Learning-Append begrenzt auf 10 Embeddings pro Speaker ([#531](https://github.com/ebongard/renfield/pull/531)).
- **`voice_server_client`-HTTP-Adapter** — Neuer Backend-Service `services/voice_server_client.py` mit `stt()` + `tts()`-Funktionen für die HTTP-Kommunikation Backend → voice-server. JWT via Bearer-Token weitergereicht; voice-server validiert lokal mit dem gleichen `SECRET_KEY`. `whisper_service.transcribe_bytes` und `piper_service.synthesize_to_bytes` delegieren bei gesetztem `VOICE_SERVER_URL` an voice-server, behalten aber den in-process-Pfad als Dev-Fallback ([#532](https://github.com/ebongard/renfield/pull/532)).
- **`/ws/chat`-Envelope: `speaker_embedding`** — Das Chat-WebSocket-Message-Schema (`models/websocket_messages.py::WSChatMessage`) bekommt ein optionales `speaker_embedding: list[float]`-Feld (Pydantic min_length = max_length = 192). Frontend forwarded das Embedding aus `useVoiceStream.onFinal`; Backend-Chat-Handler löst den Sprecher via `speaker_resolver` auf und reicht `speaker_info` an den `chat_context_established`-Hook weiter, sodass Domain-Consumer (ha_glue, Reva-Tenancy) auf den richtigen Sprecher reagieren ([#531](https://github.com/ebongard/renfield/pull/531)).
- **`/api/voice/voice-chat` als Backend-Orchestrator** — Die Satellite-fassende REST-Route ist jetzt ein Orchestrator: STT und TTS werden an voice-server delegiert, Backend behält Sprecher-Auflösung + LLM-Agenten-Loop in der Mitte. Selbe Response-Shape wie vorher — Satellite-Firmware sieht keine Änderung. Aktiviert über `VOICE_SERVER_URL`-Configmap-Eintrag; ohne diesen läuft der Legacy-in-process-Pfad ([#531](https://github.com/ebongard/renfield/pull/531), [#532](https://github.com/ebongard/renfield/pull/532)).
- **ECAPA-TDNN ONNX-Export-Pipeline** — One-time-Skript `voice-server/scripts/export_ecapa_onnx.py` exportiert das `embedding_model` aus speechbrain/spkrec-ecapa-voxceleb nach ONNX (Opset 17). Nur das embedding_model — die Full-Pipeline scheitert an der PyTorch-STFT-ONNX-Limitation ("STFT does not currently support complex types"). voice-server repliziert `compute_features` + `mean_var_norm` in Python (~5 ms CPU pro Utterance) vor dem ONNX-Call. Parity-Test in `tests/backend/test_speaker_service.py::TestEcapaOnnxParity` mit 10 deutschen Fixtures, Cosine ≥ 0,999999 vs in-process speechbrain ([#531](https://github.com/ebongard/renfield/pull/531)).
- **Voice-Tier-Auth-Modus** — voice-server `auth_required: bool` (default `True`); für die Single-User-Household-Deployment auf `False` gesetzt (`AUTH_REQUIRED` ENV in der Configmap), passend zur bereits bestehenden `AUTH_ENABLED=false`-Posture des Backends. Tokens werden weiterhin validiert wenn vorhanden, sodass dasselbe Image für AUTH_ENABLED=true-Deployments (z.B. Reva) ohne Code-Änderung läuft ([#534](https://github.com/ebongard/renfield/pull/534)).
- **Voice-Pipeline-Design-Doc + Build-vs-Buy-Spike** — `docs/VOICE_PIPELINE_DESIGN.md` (~880 Zeilen, Version v1.4) dokumentiert die Phase-B-Architektur, sechs gelockte Entscheidungen (D1-D6), VRAM-Budget, WebSocket-Protokoll mit komplettem Message-Table, Phase-C-Speech-to-Speech-Analyse, Build-vs-Buy-Spike gegen Speaches (Verdict: build, da Speaches' Realtime-API keine Partial-Transcripts liefert) ([#530](https://github.com/ebongard/renfield/pull/530)).
- **Konfidenz-Floor-Logging** — Sprach-Resolves unterhalb der Identitäts-Schwelle (0,7 cosine) loggen jetzt explizit `voice match below identity threshold: ... (conf=X.XX < 0.70)`, damit legitime-Nutzer-Denials beim Tunen sichtbar sind statt stumm zu fallen ([#534](https://github.com/ebongard/renfield/pull/534)).
- **Voice-Timing-Diagnostics** — `useVoiceStream` und ChatContext schreiben optionale Performance-Logs (`localStorage.setItem('renfield_voice_timing', '1')`-getriggert) für jede Pipeline-Stage (`stt_flush_sent`, `final_transcript`, `sendMessage`, `first_chat_token`, `autoTTS_fired`, `tts_request_sent`, `binary_frame`, `tts_done`). Macht die Latenz-Aufschlüsselung im Browser-Console reproduzierbar ([#536](https://github.com/ebongard/renfield/pull/536)).
- **Frontend-TS-Test-Migration komplett** — Komplette Test-Suite unter `tests/frontend/react/` auf TypeScript migriert plus dediziertes `tsconfig.json` für die Test-Files mit `tsc --noEmit`-Gate ([#520](https://github.com/ebongard/renfield/pull/520), [#521](https://github.com/ebongard/renfield/pull/521)).
- **i18n-Cleanup in `RoomOutputSettings`** — E12-Follow-up; verbleibende deutsche Strings auf das `useTranslation()`-Pattern und auf beide Locale-Files (`de.json` + `en.json`) verlegt ([#522](https://github.com/ebongard/renfield/pull/522)).

### Geändert

- **`whisper_service` + `piper_service` als Thin-Clients** — Bei gesetztem `VOICE_SERVER_URL` delegieren beide Services per HTTP an voice-server. In-process-faster-whisper / -Piper bleiben als Fallback für Dev-Umgebungen ohne voice-server-Deployment. Dropt `initial_prompt` auf dem Wire-Pfad (voice-server akzeptiert noch keine per-Request-Bias) — Satellite-Routen bleiben über die Backend-REST-Schicht unverändert ([#532](https://github.com/ebongard/renfield/pull/532)).
- **Configmap-Cleanup `WHISPER_*` / `PIPER_*`** — Die ENV-Variablen `WHISPER_MODEL`, `PIPER_VOICES`, `PIPER_DEFAULT_VOICE` aus `k8s/configmap.yaml` entfernt — der Voice-Tier läuft seit B.1 in voice-server, Backend-Code-Pfade greifen nicht mehr darauf zu ([#533](https://github.com/ebongard/renfield/pull/533)).
- **Pre-existing `lang=lang` NameError im chat_handler behoben** — In der `chat_context_established`-Hook-Aufrufstelle wurde ein nicht-existentes `lang`-Symbol referenziert, das nur dann auffiel wenn der Hook tatsächlich gefeuert hätte (room_context resolved + user_id ≠ None). Auf `ollama.default_lang` korrigiert — derselbe Wert, den umliegender Code im selben Handler bereits konsistent verwendet. Pre-existing aber von B.4.a's Edit aufgedeckt ([#534](https://github.com/ebongard/renfield/pull/534)).
- **WhisperPromptBuilder-Pfad in voice-server-Welt klarstellen** — Die in v2.4.1/v2.4.2 eingeführte `WhisperPromptBuilder` + `speaker_vocabulary_service`-Maschinerie speist `initial_prompt` in faster-whisper. Bei gesetztem `VOICE_SERVER_URL` läuft STT auf voice-server, der noch keine per-Request-Bias-Prompts akzeptiert — die Vocabulary-Capture läuft weiter (Speaker-Vokabular wird gesammelt), aber wird auf dem Wire-Pfad nicht in Whisper eingespeist. Auf dem Dev-Fallback-in-process-Pfad bleibt es aktiv. Folge-PR könnte `initial_prompt` zum `voice_server_client.stt()`-Call hinzufügen.

### Behoben

- **`/api/voice/stt` lehnte Browser-webm-Uploads ab** — voice-server's REST-Endpoint nutzte den Streaming-`AudioDecoder` mit `-f webm`-Codec-Hint von ffmpeg, der für vollständige MediaRecorder-Output-Dateien zu strikt war (manchmal `produced no PCM`-Fehler trotz gültigem Input). Neuer one-shot-Decoder `voice_server/services/audio_oneshot.py` lässt ffmpeg auto-detect und captured stderr für Diagnose. Streaming-WS-Pfad bleibt auf `AudioDecoder` mit Codec-Hint, da partielle Chunks keine Auto-Detection erlauben (RISK-2 aus dem Design) ([#535](https://github.com/ebongard/renfield/pull/535)).
- **SemanticRouter Substring-Matching auf Word-Boundary korrigiert** — `'board' in 'dashboard'` triggerte den Jira-Keyword-Boost auf Messages wie "Mein Dashboard" und stahl `release/my_dashboard`-Sub-Intent-Routing. Auf Whole-Word-Matching umgestellt: `re.findall(r"\w+", message.lower())` plus Set-Membership. `\w+` matched ö/ä/ü/ß korrekt; umlautlastige Keywords wie `störung` funktionieren weiterhin. Aufgedeckt von Reva's E2E nach Qwen3.5→3.6-Upgrade (Test passierte vorher zufällig durch einen Stylistik-Unterschied; die Routing-Bug war pre-existing). Drei neue Tests in `TestSemanticRouterKeywordBoostBoundary` ([#523](https://github.com/ebongard/renfield/pull/523)).
- **`useVoiceStream`-Race + Decoder-Closed-Spam** — Fünf gestapelte Design-Lücken im End-of-Utterance-Handling repariert: (1) `useVoiceStream` hatte keinen Silence-Auto-Stop wie der Legacy-Hook → User mussten den Mic-Button manuell finden. (2) voice-server's Server-VAD-Signale wurden nicht für `_finalize` verwendet. (3) Nach `_finalize` blieb der Decoder permanent geschlossen — die nächste Äußerung produzierte Endlos-Fehler-Spam. (4) `MAX_UTTERANCE_S`-Timeout closed den WebSocket nicht — Browser streamte Audio in einen toten Decoder. (5) Frontend-Recorder reagierte nicht auf `final_transcript`-Events vom Server. Inter-Utterance-Protokoll: nach `_finalize` wartet Server auf neues `session_start` vom Client; Client sendet das beim Start jeder neuen Aufnahme; Server räumt zwischen Utterances explizit auf. Mehrere Sprach-Queries hintereinander funktionieren jetzt ohne Fehler ([#536](https://github.com/ebongard/renfield/pull/536)).
- **AudioLevel-State-Churn überdeckte Partials** — Das Throttling der RMS-State-Updates auf 15 Hz (vom 60-Hz-rAF-Loop) war nötig, weil die Context-Re-Render-Kaskade die `setPartialText`-Updates verschluckte. Der Audio-Pegel-Bar bleibt visuell flüssig, der Live-Transcript ist sichtbar ([#536](https://github.com/ebongard/renfield/pull/536)).
- **Voice-Server-Ingress in Traefik nicht erkannt** — Drei Fehler in einem: (1) fehlende `router.entrypoints: websecure` + `router.tls: "true"`-Annotations → wss-Connections schlossen mit Code 1006. (2) `/ws` PathPrefix der `renfield-https`-Ingress fing `/ws/voice` ab → Backend-uvicorn 404 statt voice-server. `router.priority: 100` macht den längeren Match prioritär. (3) `ingressClassName: traefik` zeigte auf eine nicht-existente IngressClass — Traefik im Default-Class-Mode ignoriert solche Ingresses. Field entfernt ([#534](https://github.com/ebongard/renfield/pull/534)).
- **ECAPA lief auf CPU statt CUDA** — voice-server hatte zwar `onnxruntime-gpu` installiert, aber die transitiven Dependencies von `faster-whisper`/`speechbrain`/`piper-tts` zogen `onnxruntime` (CPU) als Co-Install nach, was die CUDA-EP-Registrierung beim Import maskierte. Plus fehlte cuDNN im CUDA-Runtime-Base-Image. Fix: explizites `nvidia-cudnn-cu12 ≥ 9.1` plus rm-rf-and-reinstall der `onnxruntime`-Dist-Info-Verzeichnisse als letzten pip-Step im Dockerfile (`pip uninstall` allein lässt die `.dist-info`-Records bei Co-Install bestehen). Empirisch verifiziert: `get_available_providers()` enthält jetzt `CUDAExecutionProvider`; `speaker service warm` loggt `providers=['CUDAExecutionProvider', 'CPUExecutionProvider']` ([#531](https://github.com/ebongard/renfield/pull/531)).

---

## [v2.4.4] — 2026-05-04

Hotfix für das deployte k8s-Frontend-Bundle: ohne explizites `VITE_API_URL`-Build-Arg fiel der Production-Build auf `http://localhost:8000` zurück, was im Browser zu Mixed-Content-Blocking auf `https://renfield.local` führte (`/admin/satellites` zeigte "Satelliten konnten nicht geladen werden", DevTools-Console zeigte `XMLHttpRequest cannot load http://localhost:8000/api/satellites due to access control checks`). Backend war unverändert erreichbar; das Bundle zeigte nur auf den falschen Host.

### Behoben

- **Frontend Same-Origin-Default** — `getApiBaseUrl()` in `src/frontend/src/utils/env.ts` liefert in Production-Builds (`import.meta.env.PROD`) bei fehlendem oder leerem `VITE_API_URL` jetzt einen leeren String. axios verwendet dadurch relative URLs, die im Same-Origin-Reverse-Proxy-Setup (Traefik routet `/api/*` und `/ws` auf demselben Host wie `/`) automatisch korrekt aufgelöst werden — ohne dass der Build-Schritt einen `--build-arg VITE_API_URL=...` setzen muss. Das frühere Verhalten — Fallback auf `http://localhost:8000` mit Console-Warning — bleibt im Dev-Modus (`npm run dev`) erhalten. `deploy-production`-Skill um die Erläuterung ergänzt, dass der Build-Arg jetzt nur noch für echte Cross-Origin-Deployments nötig ist.

---

## [v2.4.3] — 2026-05-02

Brücke zur Reva-Kompatibilität. Schließt die letzten zwei kosmetischen Lücken aus dem Reva-Compat-Audit (`reva/docs/architecture/renfield-compatibility-requirements.md`); die anderen neun von elf Items waren in vorigen Sprints bereits restauriert. Nach diesem Release kann Reva sein Renfield-Submodul auf `main` bumpen und seine 75 E2E-Tests gegen die hier liegende Codebasis fahren.

### Hinzugefügt

- **`prompt_hashes` auf `/health`** — Der `/health`-Endpoint liefert nun `{"status": "ok", "prompt_hashes": {...}}`, mit zwölf-Zeichen-SHA-256-Präfixen pro geladener Prompt-YAML. Reva nutzt das in seinem Audit-Trail und bei Deploy-Verifikation, um sicherzustellen, dass ein Release tatsächlich die Prompts geändert hat. Der Handler toleriert PromptManager-Fehler mit leerem Dict, sodass der Load-Balancer-Healthcheck nie kaputtgeht ([#518](https://github.com/ebongard/renfield/pull/518)).
- **Kanonische Token-Budget-Logzeile** — `_enforce_token_budget` emittiert nun beim Eintritt eine Zeile der Form `Token budget: <used>/<max> (<%>)`, mit einer Nachkommastelle Präzision. Reva's `test_token_budget_logged` E2E-Assertion sucht nach genau diesem Substring; bestehende `Budget pass N (...)`-Zeilen pro Reduktion bleiben für Observability erhalten ([#518](https://github.com/ebongard/renfield/pull/518)).

### Behoben

- **E15-Strict-Mode-Tail geschlossen** — Die in v2.2.0 zurückgebliebenen fünf strict-Mode-Errors aus dem E15-Audit jetzt eliminiert: zwei `NotificationMessage`-Variant-Errors (`useDeviceConnection.ts`), zwei `useWakeWord.ts`-Errors (fehlende `openwakeword-wasm-browser`-Type-Declaration + `modelFiles`-Option), ein `platform.ts`-Error (`@capacitor/core`-Declaration). Zwei neue ambient `.d.ts`-Files spiegeln nur die tatsächlich genutzte Surface ([#519](https://github.com/ebongard/renfield/pull/519)).
- **NotificationMessage-Shape-Tightening** — Die im selben PR eingeführte WebSocket-NotificationMessage-Variante hatte `urgency: string` und `created_at: string | null` deklariert, beides loser als der Downstream-Konsument `useNotifications.ts` annimmt (Literal-Union `'critical' | 'info' | 'low'`, non-null `created_at`). Auf den Konsumenten-Vertrag verengt — verhindert, dass künftige WebSocket-Nutzer Shapes akzeptieren, die der Renderer nicht stylen kann ([#519](https://github.com/ebongard/renfield/pull/519)).

---

## [v2.4.2] — 2026-05-02

Voice Pipeline Phase B-3 Follow-ups: hook-broadcasted Cache-Invalidierung der Whisper-Prompt-Cache und per-Nutzer-frequenz-gerankte Vokabular-Vorspannung für STT. Plus Migration-Chain-Fix, der den Deploy zwischenzeitlich blockiert hatte.

### ⚠ Aufwärtskompatibilität

- **Migration `a0b1c2d3e4f5_add_speaker_vocabulary`** — Zwei neue Tabellen: `speaker_vocabulary_corpus` (rohe bestätigte Sprecher-Transkripte mit `circle_tier=0` self-tier per Default) und `speaker_vocabulary` (berechnete Term-Frequenzen). Beide tragen `circle_tier`-Spalten von Tag 1, sodass das Vokabular niemals zwischen Sprechern leckt. Eigentumsgranularität ist strikt per Nutzer; die Tabellen sind durch FK-CASCADE an `users.id` gebunden.
- **Migration kettet vom tatsächlichen DB-Head** (`pc20260426_paperless_upload_tracking`) statt vom semantisch-naheliegenden `z9a0b1c2d3e4` — Letzterer hatte bereits ein Kind und führte zu `Multiple head revisions are present` beim ersten Deploy-Versuch. Hotfix-PR #516 re-chained pre-Tag.

### Hinzugefügt

- **Hook-Broadcast `household_graph_changed`** — Neuer Event-Typ in `utils/hooks.py`. Gefeuert von User/Room-Mutation-Routen (POST/PATCH/DELETE auf `/api/users`, `/api/rooms`) und vom HA-Area-Import. Der `WhisperPromptBuilder` registriert einen Fire-and-Forget-Handler, der seinen 5-Minuten-TTL-Cache verwirft, sodass umbenannte Räume oder neue Haushaltsmitglieder im allernächsten STT-Prompt landen statt erst nach Ablauf der TTL. Künftige Caches (Entity-Listen-Cache, Plugin-Biases) klinken sich hier ein ohne Änderungen an den Mutationssites ([#515](https://github.com/ebongard/renfield/pull/515)).
- **Per-Nutzer-Frequenz-Vokabular für STT-Bias** — `services/speaker_vocabulary_service.py`. Pipeline aus drei Stufen: (1) **Capture**: nach `transcribe_with_speaker` mit bestätigtem (nicht auto-enrolltem) Sprecher wird die verlinkte `User.id` über `User.speaker_id` aufgelöst und das Transkript an `speaker_vocabulary_corpus` angehängt — fire-and-forget via strong-referenced `asyncio.Task`-Set, damit der GC kurzlebige Tasks nicht vor dem DB-Commit einsammelt; (2) **Tokenize**: tägliche Lifecycle-Loop liest die Korpus-Zeilen der letzten 60 Tage, läuft einen Regex-Tokenizer (lowercase, kürzer als 3 Zeichen verworfen, DE+EN-Stoppwort-Sets), zählt pro `(user_id, language)` und schreibt die Top-200-Terme in `speaker_vocabulary`. Per-User-Commit innerhalb der Loop, damit ein Constraint-Fehler bei Nutzer N nicht die erfolgreichen Nutzer 1..N-1 zurückrollt; (3) **Bias**: `vocab_initial_prompt_handler` registriert auf `build_whisper_initial_prompt`, fragt die Top-30-Terme für aktiven Nutzer + Sprache ab, formatiert als `Sprecher: X. Häufige Begriffe: a, b, c, ...` (DE) bzw. die englische Variante, gekappt bei 220 Zeichen. Cold-Start (keine Korpus-Zeilen) liefert `None` → Plattform-Default greift transparent ([#515](https://github.com/ebongard/renfield/pull/515)).
- **Konfiguration:** `SPEAKER_VOCAB_CAPTURE_ENABLED=true` (Default), `SPEAKER_VOCAB_REBUILD_INTERVAL_SECONDS=86400` (täglich).

### Behoben

- **Alembic-Migration-Chain-Kollision** — `a0b1c2d3e4f5_add_speaker_vocabulary` hatte fälschlich `down_revision = "z9a0b1c2d3e4"` deklariert; `pc20260331_add_parent_chunk_id` chained bereits an dieser Stelle, was beim Deploy zu `Multiple head revisions are present` führte. Re-chained auf `pc20260426_paperless_upload_tracking` (den tatsächlichen Single-Head laut `alembic current`). Lessons-Learned in `.claude/skills/deploy-production/SKILL.md` aufgenommen: vor jeder neuen Migration `alembic heads` gegen die Live-DB prüfen ([#516](https://github.com/ebongard/renfield/pull/516), [#517](https://github.com/ebongard/renfield/pull/517)).

---

## [v2.4.1] — 2026-05-02

Voice Pipeline Phase B-3 — der Renfield-spezifische Anteil der Voice-Pipeline-Aufrüstung, der Reva nicht braucht: per-Haushalt-Initial-Prompt-Generierung und parallele Sprecher-Identifikation.

### Hinzugefügt

- **`WhisperPromptBuilder`-Service** — Baut pro Anfrage einen `initial_prompt` für faster-whisper aus Haushalts-Kontext. Fester Aufbau: `Sprecher: X. Raum: Y. Personen: ... Räume: ...` (~150-200 Zeichen, DE/EN-Labels, unbekannte Sprachen fallen auf Deutsch zurück). Cache pro `(user_id, room_id, language)` mit 5-Minuten-TTL. Plugins (z. B. Reva) gewinnen über das neue `build_whisper_initial_prompt`-Hook (erstes Non-None gewinnt); fallen sie durch, greift der Plattform-Default mit DB-Query auf `users` + `rooms`. ([#514](https://github.com/ebongard/renfield/pull/514)).
- **Hook `resolve_room_occupants`** — Reverse-Lookup, gegeben `room_id` liefert `list[user_id]` der aktuellen Anwesenden. ha_glue's Handler wickelt den BLE-Presence-Service. Der Whisper-Prompt-Builder konsultiert das Hook über `resolve_first_speaker_from_room()` BEVOR STT läuft — so kann der Prompt einen wahrscheinlichen Sprechernamen schon in Turn 1 seeden, bevor die Sprechererkennung lief.
- **Parallele Sprecher-Identifikation** — `transcribe_with_speaker` führt nun STT (`_transcribe_async`) und ECAPA-TDNN-Embedding-Extraktion (`_extract_embedding_async`, neu) gleichzeitig aus via `asyncio.gather`, beide in Worker-Threads. Netto-Latenz unverändert; speaker_id ~50-150 ms vor STT-Ende verfügbar — Downstream-Konsumenten (Notification-Routing etc.) sehen den Sprecher früher.
- **Quellseitige Type-Exports** (Einzeiler-Erweiterungen, kein Verhaltenswechsel): `AuthContextValue`, `AuthUser`, `ModalProps`, `RoomOutputSettingsProps`, `CreateRoomInput`, `CreateSpeakerInput`, `UseWakeWordResult`, `ChatUiMessage` — angefordert vom B-3-Test-Setup, wirken aber als Vertragsdokumentation für jeden Plugin-Konsumenten.

### Behoben

- **Voice-Routes-DB-Session-Hygiene** — `voice.py /stt` und `/voice-chat` öffnen jetzt eine separate `AsyncSessionLocal` für den Prompt-Build, damit dessen SELECT-Queries nicht in derselben Transaktion landen wie `transcribe_with_speaker`'s Mid-Flight-Commit auf dem Auto-Enroll-Pfad. Aktuell harmlos (kein Code danach), aber zukunftssicher ([#514](https://github.com/ebongard/renfield/pull/514) follow-up).

---

## [v2.4.0] — 2026-05-02

Voice Pipeline Phase B-1 + B-2 — TTS-Cache und Concurrency-Bound. Beide Items adressieren reale Multi-Satellite-Symptomatik, die Phase A nicht behoben hatte.

### Hinzugefügt

- **TTS-LRU-Cache** — Haushalts-TTS ist von kurzen wiederholten Bestätigungen dominiert (`Verstanden`, `Bestätigt`, `Wird erledigt`). Cache mit `(voice_name, text)` als Schlüssel und LRU-Bound (`TTS_CACHE_SIZE`, Default 256 Einträge ≈ 50 MB-Cap). Sowohl `synthesize_to_file` als auch `synthesize_to_bytes` treffen den Cache. Voice ist Teil des Schlüssels — `OK` auf Deutsch und Englisch kollidieren nicht. `speaker_id` und andere Per-Call-Synthese-Parameter werden bewusst NICHT unterstützt; sie würden den Cache-Schlüssel erweitern müssen ([#513](https://github.com/ebongard/renfield/pull/513)).
- **Thread-Offload und Concurrency-Bound** — `model.transcribe()` und `voice.synthesize()` liefen vorher synchron innerhalb der `async def`-Methoden und blockierten dabei den Event-Loop. Beide laufen nun in `asyncio.to_thread(...)`, gegated durch eine `asyncio.Semaphore` (`WHISPER_MAX_CONCURRENT=2`, `TTS_MAX_CONCURRENT=4`). Zwei Satelliten, die gleichzeitig sprechen, serialisieren nicht mehr; eine Burst von N Satelliten kann den Backend-Box nicht mehr OOM-en. Die Semaphores binden lazy an den laufenden Loop bei der ersten Nutzung, sodass Test-Fixtures, die den Service außerhalb des Event-Loops konstruieren, weiterhin funktionieren.
- **`initial_prompt`-Parameter durchgereicht** — Alle vier `transcribe_*`-Signaturen akzeptieren nun ein optionales `initial_prompt`, gereicht bis zu `_run_transcription` und faster-whisper. Override-wins / `None`-fällt-durch / Empty-String-deaktiviert-Bias-Semantik getestet. Ruhend bis Phase B-3 (v2.4.1) das Hook-System verdrahtete.

---

## [v2.3.0] — 2026-05-01

Voice Pipeline Phase A — Backend-Swap auf faster-whisper und in-process Piper. Dieselbe Blast-Radius wie Reva's empfehlene erste Schicht, plus Verifikation, dass Sprecher-Embeddings nach dem Engine-Wechsel weiterhin alignen. Latenz fällt von ~4 s auf ~1.5 s auf der GPU-PRD; deutsche Fachvokabel-WER fällt materiell.

### ⚠ Aufwärtskompatibilität

- **`openai-whisper`-Python-Paket entfernt**, ersetzt durch `faster-whisper>=1.0.0`. Der CTranslate2-Backend liefert ~4× GPU-Throughput und ein sauberes `device=cuda + compute_type=float16`-Setup. Manylinux-Wheels für amd64 + aarch64 — keine PyAV-Source-Builds mehr. Public API (`transcribe_file`, `transcribe_bytes`, `transcribe_with_speaker`, `transcribe_bytes_with_speaker`) ist signaturkompatibel.
- **In-process Piper** — `piper-tts>=1.2.0` Python-Bindings ersetzen den Per-Request `subprocess.Popen('piper', ...)`-Cold-Start (~150-300 ms gespart pro TTS-Aufruf). Voice-Modelle leben weiterhin unter `/usr/share/piper/voices/<voice>.onnx`; das CLI-Binary bleibt im Image als Fallback.
- **Singleton-Dedup für Whisper und Piper** — `voice.py` instantiierte `WhisperService()` direkt, während `api/websocket/shared.py:get_whisper_service` lazy einen zweiten erzeugte. Zwei Modell-Loads unter Last. Phase A behebt zusätzlich eine dritte Instantiierung in `ha_glue/api/websocket/device_handler.py`. Alle Aufrufer nutzen nun `get_whisper_service()` / `get_piper_service()` ([#509](https://github.com/ebongard/renfield/pull/509)).

### Hinzugefügt

- **Konfiguration**: `WHISPER_DEVICE` (cpu/cuda), `WHISPER_COMPUTE_TYPE` (int8 für CPU, float16 für GPU, int8_float16 für GPU-Low-Memory), `WHISPER_BEAM_SIZE` (Default 5).
- **Strategy-Skelett** — `docs/STRATEGY.md` mit Solo-Founder-Frame und neun [FOUNDER FILL-IN]-Platzhaltern dokumentiert das WHY hinter dem maximalistischen Circles-Plan, distinct vom HOW im Design-Doc ([#508](https://github.com/ebongard/renfield/pull/508)).
- **Voice-Pipeline-Plan** — `docs/voice-pipeline-plan.md` als Renfield-seitiges Companion-Doc zu Reva's `voice-pipeline-enhancements.md`, mit phasiertem Rollout-Plan und Vergleichsprotokoll ([#510](https://github.com/ebongard/renfield/pull/510)).
- **dlna-mcp `imagePullPolicy: Always`** — Vorher `IfNotPresent`; ein `kubectl rollout restart` auf `dlna-mcp` cachte stillschweigend das alte `:latest` weiter, selbst nach frischem Push. Verifiziert während v2.2.0-Deploy ([#507](https://github.com/ebongard/renfield/pull/507)).

### Behoben

- **Harbor-Push-Timeout auf der monolithischen 2.66-GB-Pip-Install-Schicht** — Wenn `requirements.txt` änderte, baute Docker eine 2.66 GB große Layer, die der externe HTTPS-Proxy vor `registry.treehouse.x-idra.de` (Telekom-IP `93.241.252.154`) reproducierbar mit `504 Gateway Timeout` / `Client Closed Request` nach 3.9 s und 45.9 MB ablehnte. Mitigation: Dockerfile teilt den pip-Install in fünf RUN-Stufen UND verschiebt die Heavy-Packages (torch, transformers, easyocr, docling*, speechbrain, cv2, ctranslate2, librosa) aus `/opt/venv` heraus in `/opt/staging/{torch,ml,audio}/`, die im Runtime-Stage einzeln zurück-COPY't werden. Resultat: 722 MB / 205 MB / 63 MB / 1.66 GB-Layer statt einer 2.65 GB. Upstream-Fix (`proxy_request_buffering off` o. ä. auf dem Harbor-Proxy) braucht Admin-Zugriff; Layer-Split umgeht das Problem deploy-seitig ([#511](https://github.com/ebongard/renfield/pull/511), [#512](https://github.com/ebongard/renfield/pull/512)).
- **`device_handler.py` Piper-Singleton-Bypass** — Code-Review-Fund: `device_handler.py:255-256` instantiierte `PiperService()` direkt statt `get_piper_service()` zu nutzen. Letzte Stelle, die noch zwei Voice-Singletons zerstörte ([#509](https://github.com/ebongard/renfield/pull/509)).

### Dokumentation

- Neu: [`docs/STRATEGY.md`](docs/STRATEGY.md), [`docs/voice-pipeline-plan.md`](docs/voice-pipeline-plan.md).
- Aktualisiert: `.claude/skills/deploy-production/SKILL.md` mit rsync-zu-Staging-Flow, Harbor-504-Mitigationsleitfaden, 12-Schritte-End-to-End-Checklist.

---

## [v2.2.0] — 2026-04-30

Stabilisierungs- und Aufräum-Release. Schließt den **WICHTIG-Audit-Sweep** (W1-W14, alle 14 Items resolved), die **EMPFEHLUNG-Audit-Items E1-E18**, einen kompletten **Frontend-TypeScript-Migration** (W10) und die **Paperless-Metadaten-LLM-Pipeline** (PR 2-4). Keine Architektur-Schritte; viel Schliff.

### ⚠ Aufwärtskompatibilität

- **Frontend ist nun 100% TypeScript** unter `src/frontend/src/` (~145 Dateien). Tests bleiben vorerst `.jsx` (separate Migration in v2.4.x). Strict-Mode aktiv (`E15` aus dem Audit-Sweep) — keine `as any`, keine `@ts-nocheck`. Konsumenten sehen nur typed exports; Plugin-seitiger Import-Pfad bleibt identisch ([#487](https://github.com/ebongard/renfield/pull/487), [#506](https://github.com/ebongard/renfield/pull/506)).
- **`piper_voice` → `piper_default_voice`** Settings-Feld umbenannt. `.env`-Dateien mit dem alten Namen müssen aktualisiert werden ([#495](https://github.com/ebongard/renfield/pull/495)).
- **Paperless-Upload-MCP auf v1.4.0 gepinnt** — der vorhergehende Stand hatte einen 400-Bug auf bestimmten Content-Type-Kombinationen ([#466](https://github.com/ebongard/renfield/pull/466)).

### Hinzugefügt

#### Paperless LLM-Metadaten-Extraktion

- **PR 2a + 2b** — LLM-Metadaten-Extraktor-Core mit Cold-Start-Confirm-Flow ([#456](https://github.com/ebongard/renfield/pull/456), [#457](https://github.com/ebongard/renfield/pull/457)).
- **PR 3** — Lernen aus Korrekturen via Prompt-Augmentation ([#458](https://github.com/ebongard/renfield/pull/458)).
- **PR 4** — UI-Edit-Sweeper + Abandoned-Confirm-Cleanup ([#459](https://github.com/ebongard/renfield/pull/459)).
- **Server-Side Taxonomy Resolution** — Taxonomy aus Prompt entfernt; Server löst per User-Wahl auf ([#476](https://github.com/ebongard/renfield/pull/476)).
- Design-Dokument: [`docs/design/paperless-llm-metadata.md`](docs/design/paperless-llm-metadata.md).

#### Frontend-Modernisierung

- **W10 — Full Frontend TypeScript Migration** (71 Dateien) — `src/frontend/src/` von `.jsx` auf `.tsx`, alle Pages + Hooks + Komponenten + Contexts mit echten Typen, kein Shortcut-Workaround ([#487](https://github.com/ebongard/renfield/pull/487)).
- **E11 — React Query** — alle 23 List-Fetching-Surfaces auf TanStack Query migriert ([#504](https://github.com/ebongard/renfield/pull/504), [#505](https://github.com/ebongard/renfield/pull/505)).
- **E12 — Hardcoded German Strings** — ChatMessages alt-text + 5 dev-logs durch `useTranslation()` und Englisch-Polish ([#496](https://github.com/ebongard/renfield/pull/496)).
- **E13 — ChatPage Prop-Drilling → Context** — verifiziert; ChatInput nimmt 0 Props ([#503](https://github.com/ebongard/renfield/pull/503)).
- **E15 — Strict Mode aktiviert** — 15 der 20 Type-Errors gefixt ([#506](https://github.com/ebongard/renfield/pull/506)). Fünf Residual-Fehler in `useDeviceConnection.ts`, `useWakeWord.ts` und `platform.ts` (fehlende ambient module-declarations + `NotificationMessage`-Variante) erst in v2.4.3 ([#519](https://github.com/ebongard/renfield/pull/519)) geschlossen.
- **E10 — VITE_API_URL/VITE_WS_URL Fallback** zentralisiert mit Warnings ([#501](https://github.com/ebongard/renfield/pull/501)).

#### Orchestrator + Hooks (Phase 1 / 1.5)

- **Sub-Agent-Hooks + Plugin-Role-Extension** — `pre_sub_agent`, `post_sub_agent`, `extend_orchestrator_roles` ([#488](https://github.com/ebongard/renfield/pull/488)).
- **Synthesis-Hooks für Plugin-Extension** — `build_synthesis_context`, `synthesis_prompt_override` ([#489](https://github.com/ebongard/renfield/pull/489)).
- **`pre_mcp_call`-Event** für Plugin-Tool-Call-Rewriting (z. B. Reva's Release-ID-Resolver) ([#491](https://github.com/ebongard/renfield/pull/491)).
- **MCP-Auto-Reconnect + universeller `probe_server()`** für streamable_http-Transport ([#492](https://github.com/ebongard/renfield/pull/492)).

#### WICHTIG / EMPFEHLUNG-Audit-Sweep

- **W2** — IVFFlat→HNSW-Switchover dokumentiert; stale Model-Comment entfernt ([#485](https://github.com/ebongard/renfield/pull/485)).
- **W3** — Batch-Parent-INSERTs in `_ingest_parent_child` ([#483](https://github.com/ebongard/renfield/pull/483)).
- **W5 + W13** — Config-Hygiene-Bundle: Timeouts in Settings, changeme-Default-Detection ([#484](https://github.com/ebongard/renfield/pull/484)).
- **W6** — Alle LLM-Optionen routen über `prompts/agent.yaml` ([#482](https://github.com/ebongard/renfield/pull/482)).
- **K1-K7 KRITISCH-Findings** — N+1, Secrets-Inventory, Env-Config ([#464](https://github.com/ebongard/renfield/pull/464), [#465](https://github.com/ebongard/renfield/pull/465)).
- **E5 + E9** — MCP-Backoff- und Intent-Feedback-Thresholds in Settings ([#500](https://github.com/ebongard/renfield/pull/500)).
- **E14, E16, E17, E18** — ESLint-React-Version, Settings-Field-Renaming, Compose-REDIS_URL-Parameter, Frigate MQTT-Defaults ([#495](https://github.com/ebongard/renfield/pull/495), [#497](https://github.com/ebongard/renfield/pull/497), [#499](https://github.com/ebongard/renfield/pull/499)).
- **E1-E3** — Speaker-Loading + Eager-Load-Cleanup + FK-Indexes verifiziert done ([#502](https://github.com/ebongard/renfield/pull/502)).

### Behoben

- **Alembic-Baseline-Width** — `alembic_version.version_num` auf VARCHAR(64) verbreitert (auto-widen + explicit migration) ([#477](https://github.com/ebongard/renfield/pull/477), [#462](https://github.com/ebongard/renfield/pull/462), [#478](https://github.com/ebongard/renfield/pull/478)).
- **Atoms-Tier-PATCH** — `PATCH /api/atoms/{id}/tier` 500: id-Cast auf Text im Cascade-Statement ([#470](https://github.com/ebongard/renfield/pull/470)).
- **Chat-Upload Duplicate-Reuse** — bei `(file_hash, kb_id)`-Kollision wird das bestehende Doc wiederverwendet statt 500 ([#472](https://github.com/ebongard/renfield/pull/472)).
- **Paperless-Cold-Start-Flow** — JSON-safe MCP-Truncation, ISO-Date-Serialization, Polling auf Consume-Task, drop von Vision-Model in Extraction-Fallback, loguru `%`-Format-Bugs ([#467](https://github.com/ebongard/renfield/pull/467), [#471](https://github.com/ebongard/renfield/pull/471), [#473](https://github.com/ebongard/renfield/pull/473), [#475](https://github.com/ebongard/renfield/pull/475)).
- **Agent action_required Short-Circuit** — `final_answer` wird nun NICHT emittiert, wenn ein Tool `action_required` zurückliefert ([#474](https://github.com/ebongard/renfield/pull/474)).
- **Chat-WebSocket-Handshake-Wait** — kurzes Warten auf WS-Handshake bevor REST-Fallback feuert ([#490](https://github.com/ebongard/renfield/pull/490)).
- **AgentTools async create() classmethod** ersetzt den `_hook_task`-Workaround ([#493](https://github.com/ebongard/renfield/pull/493)).
- **Orchestrator Synthesis-Fallback-Logging** — Timeout vom generischen Exception getrennt ([#494](https://github.com/ebongard/renfield/pull/494)).
- ~45 weitere kleinere Fixes; Details im Git-Log.

### Dokumentation

- **deploy-production-Skill** — komplette Überarbeitung für die k8s-Topologie: .159-Build-Box, Harbor, privates Cluster ([#461](https://github.com/ebongard/renfield/pull/461)).
- **Per-Area-E2E-Browser-Test-Scaffold** mit HTML-Report-Runner ([#469](https://github.com/ebongard/renfield/pull/469)).
- **TODOS-Konsolidierung** — `tasks/todo.md` in `TODOS.md` integriert ([#479](https://github.com/ebongard/renfield/pull/479)).

---

## [v2.1.0] — 2026-04-22

Stabilisierung von `v2.0.0` mit einer architektonischen Nachkorrektur und zwei zuvor unentdeckten Access-Control-Lücken. Die namensgebende Änderung — **Atoms per Document** — verschiebt die Eigentumsgranularität der Circles-Schicht vom Chunk zum Dokument. Inhaltlich semantisch sauberer (ein Dokument ist eine Informationseinheit, ein Chunk ist ein Retrieval-Fragment); technisch reduziert es die KB-Share-Explosion um zwei bis drei Größenordnungen.

### ⚠ Aufwärtskompatibilität

- **Migration `pc20260423_atoms_per_document`**: Neue Spalten `documents.atom_id` (FK → `atoms`, `ON DELETE SET NULL`) + `documents.circle_tier`. Per-Chunk-`atom_id` auf `document_chunks` entfällt; `circle_tier` bleibt dort als denormalisiertes Mirror für den Hot-Path-Filter. Bestand wird per `MIN(chunk.circle_tier)` konservativ auf das Dokument kollabiert. Eine Pre-Migration-Gate bricht den Upgrade ab, falls ein Dokument Chunks mit heterogenen Tiers besitzt — kein stiller Tier-Up-Leak. Downgrade rekonstruiert Per-Chunk-Atoms aus dem Dokument-Tier (verlustbehaftet für zwischenzeitliche Per-Chunk-Diversität; dokumentiert).
- **Atom-Typ `kb_chunk` ist zurückgezogen**: Nach dem Upgrade existiert kein `kb_chunk`-Atom mehr; Schreiber produzieren nur noch `kb_document`. Externe Tools, die `atom_explicit_grants` oder die `/api/atoms`-Liste parsen, sehen ab jetzt Document-anchored Rows.
- **KB-Share-Semantik**: `kb_shares_service.revoke_kb_share` liefert jetzt einen Rowcount pro Dokument, nicht pro Chunk (typisch zwei bis drei Größenordnungen kleiner). Aufrufer, die `removed > 0` prüfen, bleiben korrekt; Aufrufer, die den exakten Count inspizieren, müssen ihn neu kalibrieren.

### Hinzugefügt

- **Atoms-per-Document** (Kernbeitrag dieses Release) — Design-Dokument in [`docs/design/atoms-granularity.md`](docs/design/atoms-granularity.md). Retrieval aggregiert Chunk-Treffer nun am Dokument, damit ein langes Dokument den Cross-Source-RRF nicht mit eigenen Chunks überflutet ([#444](https://github.com/ebongard/renfield/pull/444)).
- **Per-Role Native Function Calling Toggle** (opt-in, default OFF) — `native_function_calling: true` in `config/agent_roles.yaml` aktiviert OpenAI-style `tools=[]` für eine Rolle. Zwei Benchmarks (2026-04-16 + 2026-04-21) zeigen ReAct weiterhin überlegen bei Tool-Selection-Accuracy, deshalb bleibt der Default aus. Scaffolding für zukünftige A/B-Tests ([#422](https://github.com/ebongard/renfield/pull/422)).
- **Routing-Dashboard im Admin-Nav** — `/admin/routing` war seit [#370](https://github.com/ebongard/renfield/pull/370) registriert, aber über die UI nicht erreichbar. Nav-Eintrag `GitBranch` unter `nav.routingDashboard`, permission-gated auf `admin` ([#452](https://github.com/ebongard/renfield/pull/452)).
- **Atoms-Review-Labels für KB-Dokumente** — `_resolve_review_labels` in `/api/circles/me/atoms-for-review` resolved `kb_document`-Atoms nun über die `documents`-Tabelle (Titel oder Dateiname + Preview aus dem ersten Chunk).

### Behoben

- **KG-Entitäten und -Relationen landen jetzt in der `atoms`-Registry**: Der Writer in `KnowledgeGraphService` hatte den Atoms-Insert nicht mit dem Source-Row-Insert verknüpft — frisch extrahierte Entitäten + Relationen waren deshalb für Circles-basierte Zugriffsprüfung unsichtbar, obwohl sie in `kg_entities` / `kg_relations` korrekt geschrieben wurden. Shared `AtomService.create_with_source` + `finalize_source_id` Helpers, gemeinsam genutzt von RAG-, KG- und Memory-Writern ([#441](https://github.com/ebongard/renfield/pull/441), closes [#438](https://github.com/ebongard/renfield/issues/438)).
- **Chat-Upload-Endpoints prüfen den Eigentümer**: `POST /api/chat/upload/{id}/paperless`, `/email`, `/index` suchten `ChatUpload` nur per id ohne Ownership-Check. In Multi-User-Setups konnte Nutzer A durch Raten der ID Dateien von Nutzer B an Paperless weiterleiten oder per Mail versenden. Neuer `_get_owned_upload`-Helper joint `chat_uploads → conversations` und filtert über `user_id`. Soft-404 auf Cross-User-Probe, nicht 403 (verrät nicht, dass die ID existiert) ([#442](https://github.com/ebongard/renfield/pull/442), closes [#434](https://github.com/ebongard/renfield/issues/434)).
- **Alembic-Migration-DDL-Safety**: `DROP INDEX IF EXISTS` auf Postgres-Pfad, weil `ix_document_chunks_atom_id` nur auf Dev-DBs existiert (über ORM create_all erzeugt), nicht auf Prod (dort wurde er nie explizit angelegt). Das alte `try/except` lag innerhalb von `op.batch_alter_table`, wo Batch-Mode die DDL bis `__exit__` zurückstellt — der Except fängt nur Fehler beim Anlegen des Ops, nicht beim Ausführen der gesammelten SQL ([#451](https://github.com/ebongard/renfield/pull/451)).
- **Duplikate Config-Dateien entfernt**: `src/backend/config/` enthielt eine veraltete Kopie von Dateien, die längst in den Haupt-Config-Pfaden lebten ([#439](https://github.com/ebongard/renfield/pull/439), closes [#437](https://github.com/ebongard/renfield/issues/437)).

### Entwicklung

- **Reference-Resolver-Tests** — 24 Unit-Tests für `services.reference_resolver` (load / compile / resolve, inklusive YAML-Fehler-Pfade und kreuzdomain-Ambiguität) ([#373](https://github.com/ebongard/renfield/pull/373)).
- **Follow-up-Issues** aus dem `/review` zu [#444](https://github.com/ebongard/renfield/pull/444) erfasst: Caller-Authz in `upsert_atom` + `share_kb` ([#445](https://github.com/ebongard/renfield/issues/445)), Placeholder-Orphan-Reaper für `create_with_source` ([#446](https://github.com/ebongard/renfield/issues/446)), Migration-Integration-Tests ([#447](https://github.com/ebongard/renfield/issues/447)), Owner-Resolver-Helper extrahieren ([#448](https://github.com/ebongard/renfield/issues/448)), `ATOM_TYPE_*` Konstanten an allen Call-Sites ([#449](https://github.com/ebongard/renfield/issues/449)), `DISTINCT ON` in `_resolve_review_labels` ([#450](https://github.com/ebongard/renfield/issues/450)).

---

## [v2.0.0] — 2026-04-21

Erste Major-Version seit `v1.0.0`. Der Sprung reflektiert drei generationelle Architektur-Schritte — **Circles / Second Brain**, **Federation v2**, **Async Worker-Split** — sowie die Umstellung auf **k8s** als Produktions-Topologie.

### ⚠ Aufwärtskompatibilität

- **Circles-Migration**: Bestehende Daten in `document_chunks`, `kg_entities`, `kg_relations`, `conversation_memories` erhalten über die Alembic-Migrationen aus Lane B automatisch `atom_id`- und `circle_tier`-Spalten. Default-Tier ist `2` (household) für Dokumente, `1` (trusted) für Chat-Memories. Single-User-Installationen (`AUTH_ENABLED=false`) sehen keine Verhaltensänderung — der Tier-Filter ist kurzgeschlossen.
- **Chat-Upload ist nun asynchron**: `POST /api/chat_upload` liefert sofort mit `status=pending` und einer `upload_id`; Frontend pollt `GET /api/chat_upload/{id}` auf `status=completed`. Synchrone Upload-Clients müssen auf Polling umgestellt werden.
- **Agent-visible Paperless-Tools**: `mcp.paperless.upload_document` wurde aus der Agent-Tool-Liste entfernt. Für den Upload angehängter Dokumente über den Chat existiert das neue `internal.forward_attachment_to_paperless` (keine Code-Änderung notwendig in nutzerseitigem Prompt — der Agent wählt das Tool automatisch).
- **Konversations-Memory respektiert Circles**: `ConversationMemoryService.retrieve()` filtert nun nach Tier-Reichweite. Aufrufer **müssen** `user_id=asker_id` übergeben; `None` im auth-enabled Modus reduziert auf Tier 4 allein.

### Hinzugefügt

#### Circles & Second Brain (neu)

- **Circles v1** — fünfstufige Zugriffsleiter (self, trusted, household, extended, public) pro Eigentümer, 4-Zweig-Zugriffsregel (OWNER ∨ PUBLIC ∨ EXPLICIT GRANT ∨ TIER-REACH). Lanes A/B/C in [#401](https://github.com/ebongard/renfield/pull/401), [#402](https://github.com/ebongard/renfield/pull/402), [#403](https://github.com/ebongard/renfield/pull/403).
- **Atoms-Registry** — polymorphe Identitätsschicht über `document_chunks`, `kg_entities`, `kg_relations`, `conversation_memories`. Denormalisierte `circle_tier`- und `atom_id`-Spalten auf den Quell-Tabellen für SQL-Filter-Performance.
- **Cross-Source-Suche** via Reciprocal Rank Fusion — `/api/atoms`, `/brain`-Page.
- **Review-Queue** — `/brain/review` zeigt neu klassifizierbare Atome mit menschenlesbaren Labels ([#427](https://github.com/ebongard/renfield/pull/427)).
- **KB-Share-Explosion** — `kb_shares_service` expandiert KB-Level-Shares in Per-Chunk-Grants.
- **Explicit Grants** — Notion/Drive-Ausnahmen über `atom_explicit_grants`.
- **Frontend-Seiten** — `/brain`, `/brain/review`, `/settings/circles`, `/settings/circles/peers`.
- Dokumentation: [`docs/CIRCLES.md`](docs/CIRCLES.md), [`docs/SECOND_BRAIN.md`](docs/SECOND_BRAIN.md).

#### Federation v2 — Multi-Peer

- **F1 MCP-Streaming-Surface** — Wire-Protokoll für streamende MCP-Server ([#406](https://github.com/ebongard/renfield/pull/406), [#407](https://github.com/ebongard/renfield/pull/407)).
- **F2 Pairing** — Ed25519-Identität + `peer_users` ([#408](https://github.com/ebongard/renfield/pull/408)).
- **F3 query_brain** — Responder-Backend ([#410](https://github.com/ebongard/renfield/pull/410)), Asker `RemoteBrainMCPClient` ([#411](https://github.com/ebongard/renfield/pull/411)), Agent-Loop-Integration mit Ollama-Synthese ([#412](https://github.com/ebongard/renfield/pull/412)).
- **F4 UX** — Peers-Seite + Revoke ([#413](https://github.com/ebongard/renfield/pull/413)), Pairing-QR-Modals ([#414](https://github.com/ebongard/renfield/pull/414)), Live-Progress-Relay *„frage Moms Brain…"* ([#415](https://github.com/ebongard/renfield/pull/415)), Audit-Feed unter `/brain/audit` ([#416](https://github.com/ebongard/renfield/pull/416)).
- **F5 Robustheit** — Depth + Cycle Detection ([#417](https://github.com/ebongard/renfield/pull/417)), Per-Peer + Per-Asker Rate-Limits ([#418](https://github.com/ebongard/renfield/pull/418)), Redis-backed Pending-Request-Store ([#419](https://github.com/ebongard/renfield/pull/419)), TLS-Fingerprint-Pinning ([#420](https://github.com/ebongard/renfield/pull/420)), TOFU Auto-Pinning beim Pairing ([#421](https://github.com/ebongard/renfield/pull/421)).
- Dokumentation: [`docs/FEDERATION_MULTI_PEER.md`](docs/FEDERATION_MULTI_PEER.md).

#### Asynchrone Document-Ingestion — Worker Split

- **PR A** — Infrastruktur für Async-Ingestion mit Status-Polling ([#388](https://github.com/ebongard/renfield/pull/388)).
- **PR B** — RAGService in `extractor` / `ingestor` aufgeteilt.
- **PR C1/C2** — Upload-Cutover, Polling-Frontend, A11y, HTTP-Semantik ([#391](https://github.com/ebongard/renfield/pull/391), [#393](https://github.com/ebongard/renfield/pull/393)).
- Eigener `document-worker`-Deployment (siehe [`docs/DOCUMENT_WORKER_SPLIT.md`](docs/DOCUMENT_WORKER_SPLIT.md)).

#### Kubernetes-Produktion

- **Private GPU-Cluster** als Ziel-Deploy ([#386](https://github.com/ebongard/renfield/pull/386)) — Manifeste in `k8s/`, Blackwell-GPU-Nodes (RTX 5070 Ti / 5060 Ti), Traefik-Ingress, Harbor-artiges Private Registry.
- Dokumentation: [`docs/KUBERNETES_DEPLOYMENT.md`](docs/KUBERNETES_DEPLOYMENT.md).

#### Agent & Routing

- **Orchestrator + Adaptive Cards** — parallele Sub-Agent-Koordination ([#374](https://github.com/ebongard/renfield/pull/374), [#384](https://github.com/ebongard/renfield/pull/384)).
- **Sub-Intent Dispatch** — feingranulare Routing-Entscheidungen via Hook ([#307](https://github.com/ebongard/renfield/pull/307), [#384](https://github.com/ebongard/renfield/pull/384)).
- **Context-aware Routing** — Entity-Pre-Routing + Keyword-Boosting ([#368](https://github.com/ebongard/renfield/pull/368)).
- **Parallel Tool Execution** — Multi-Tool-Calls in einem Agent-Step ([#328](https://github.com/ebongard/renfield/pull/328)).
- **Routing-Trace-Dashboard** — Admin-UI + `post_routing`-Hook ([#370](https://github.com/ebongard/renfield/pull/370)).
- **Token-Budget-Enforcement** + Tool-Preselection + Output-Guard ([#312](https://github.com/ebongard/renfield/pull/312)).
- **Routine-Agent** — Good-Night / Good-Morning-Sequenzen ([#271](https://github.com/ebongard/renfield/pull/271)).
- **Stale-Tool-Error-Marker** — `[VORHERIGE_FEHLGESCHLAGENE_AKTION]` verhindert, dass historische Fehler Re-Execution blockieren ([#430](https://github.com/ebongard/renfield/pull/430)).
- **Internal-Tool `forward_attachment_to_paperless`** — der Agent leitet angehängte Dateien an Paperless weiter, ohne jemals base64 zu sehen ([#433](https://github.com/ebongard/renfield/pull/433)).

#### Auth & Multi-Tenancy

- **Pluggable Authentication** via Hook-System ([#334](https://github.com/ebongard/renfield/pull/334)), `ProtectedRoute` für Chat ([#335](https://github.com/ebongard/renfield/pull/335)).
- **Voice Authentication** per Sprechererkennung (optional).
- **White-Label-Branding** via `VITE_APP_NAME` + `VITE_APP_LOGO_URL` ([#378](https://github.com/ebongard/renfield/pull/378), [#379](https://github.com/ebongard/renfield/pull/379)).

#### RAG-Qualität

- **Contextual Retrieval** + Reranking + Parent-Child-Chunking + Eval-Pipeline ([#324](https://github.com/ebongard/renfield/pull/324)).
- **Knowledge-Graph-Scopes** — konfigurierbare Entitätstypen ([#318](https://github.com/ebongard/renfield/pull/318)).

#### Memory

- **Episodic Lifecycle** — Confidence Decay, Trigger-Pattern, konfigurierbares Extraktions-Modell ([#331](https://github.com/ebongard/renfield/pull/331)).
- **Always-Inject Essential Memories** — wichtige Fakten landen unabhängig von Similarity-Score im Kontext ([#251](https://github.com/ebongard/renfield/pull/251)).
- **Per-User Personality Style** ([#276](https://github.com/ebongard/renfield/pull/276)).

#### Satellites

- **Visual Queries** — Satelliten-Kamera + Vision-LLM für Fragen zum Bild vor Ort.
- **XVF3800** USB-Array + Enviro pHAT ([#310](https://github.com/ebongard/renfield/pull/310)).
- **Whisplay HAT**-Support.
- **Konfigurierbare IDLE-LED-Farbe** pro Satellit.
- **Neue Satelliten**: Esszimmer ([#292](https://github.com/ebongard/renfield/pull/292)), Arbeitszimmer, BensZimmer.
- **Action-Success-Metadata** in Konversationshistorie — verhindert Fehler-Nachgeplapper in Follow-ups ([#431](https://github.com/ebongard/renfield/pull/431), [#432](https://github.com/ebongard/renfield/pull/432)).

#### Media

- **Media Follow Me** — Wiedergabe folgt dem Nutzer zwischen Räumen ([#240](https://github.com/ebongard/renfield/pull/240)).
- **TuneIn-Radio-Integration** ([#237](https://github.com/ebongard/renfield/pull/237)).
- **Genre-Suchhints** in Agent-Prompts ([#235](https://github.com/ebongard/renfield/pull/235)).
- **Room-Owner-Dropdown** in Admin-UI ([#240](https://github.com/ebongard/renfield/pull/240)).

#### Hook-System

- **`pre_agent_context`** + **`pre_save_message`**-Hooks, erweiterte History-Window ([#302](https://github.com/ebongard/renfield/pull/302)).
- **`execute_tool`**-Hook für Plugin-Tool-Dispatch.
- **`token_budget_info`** + **`token_usage_info`** ContextVars für Plugins ([#409](https://github.com/ebongard/renfield/pull/409)).

#### Mobile & PWA

- **iOS-Capacitor-Wrapper** mit PWA-Icons für iPhone-App ([#329](https://github.com/ebongard/renfield/pull/329)).

#### Admin

- **Conversation Summary** — LLM-basierte Zusammenfassung + `context_vars` ([#304](https://github.com/ebongard/renfield/pull/304)).
- **Admin-Maintenance-Page** — Knowledge-Graph-Qualität, Duplikat-Erkennung, Bulk-Cleanup.

#### Plugin-Infrastruktur

- **Alembic Plugin-Metadata-Discovery** ([#363](https://github.com/ebongard/renfield/pull/363)).
- **ha_glue-aware env.py** für Autogenerate ([#357](https://github.com/ebongard/renfield/pull/357)).

### Behoben

- **Paperless-Upload-Chain** — URL-Suffix (`/api/api/`, [#429](https://github.com/ebongard/renfield/pull/429)), MIME-Type (`application/octet-stream` → echte Typen, `renfield-mcp-paperless#3`), Base64-Validation (`renfield-mcp-paperless#4`), Agent-Halluzination-Vermeidung ([#433](https://github.com/ebongard/renfield/pull/433)).
- **Lifecycle AsyncSessionLocal-Shadow** — Import-Reihenfolge in `_init_mcp` ([#428](https://github.com/ebongard/renfield/pull/428)).
- **KG useEffect-Dependency-Typo** — `scopeFilter` → `tierFilter` ([#426](https://github.com/ebongard/renfield/pull/426)).
- **Circles Render-Fix** — `ConfirmDialogComponent` als Element, nicht Komponente ([#425](https://github.com/ebongard/renfield/pull/425)).
- **Auth-Disabled Guards** auf verbleibende Circle/Atom-Routen ([#424](https://github.com/ebongard/renfield/pull/424)).
- Weitere ~45 Fixes; Details im Git-Log.

### Sicherheit

- **Input-Guard**, **MCP-Kompaktierung**, **Memory-Defense** — Reva-Backport Prio 1 ([#311](https://github.com/ebongard/renfield/pull/311)).
- **TLS-Cert-Pinning** für Federation-Peers.
- **Session-Scoped Attachment-Lookup** — verhindert Cross-Session-Zugriff auf fremde Chat-Uploads ([#433](https://github.com/ebongard/renfield/pull/433) follow-up).

### Dokumentation

- Neu: [`docs/CIRCLES.md`](docs/CIRCLES.md), [`docs/SECOND_BRAIN.md`](docs/SECOND_BRAIN.md), [`docs/FEDERATION_MULTI_PEER.md`](docs/FEDERATION_MULTI_PEER.md), [`docs/KUBERNETES_DEPLOYMENT.md`](docs/KUBERNETES_DEPLOYMENT.md), [`docs/DOCUMENT_WORKER_SPLIT.md`](docs/DOCUMENT_WORKER_SPLIT.md).

---

## [v1.2.0] und früher

Keine CHANGELOG-Einträge vor `v2.0.0`. Vollständige Commit-Historie: [`git log v1.0.0..v1.2.0`](https://github.com/ebongard/renfield/compare/v1.0.0...v1.2.0).

---

[v2.4.3]: https://github.com/ebongard/renfield/compare/v2.4.2...v2.4.3
[v2.4.2]: https://github.com/ebongard/renfield/compare/v2.4.1...v2.4.2
[v2.4.1]: https://github.com/ebongard/renfield/compare/v2.4.0...v2.4.1
[v2.4.0]: https://github.com/ebongard/renfield/compare/v2.3.0...v2.4.0
[v2.3.0]: https://github.com/ebongard/renfield/compare/v2.2.0...v2.3.0
[v2.2.0]: https://github.com/ebongard/renfield/compare/v2.1.0...v2.2.0
[v2.1.0]: https://github.com/ebongard/renfield/compare/v2.0.0...v2.1.0
[v2.0.0]: https://github.com/ebongard/renfield/compare/v1.2.0...v2.0.0
