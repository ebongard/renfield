# Speaker-Rec Integration mit Speaches — Architektur-Analyse

**Status:** Spike-Material, nicht final. Wird in v1.4 von `VOICE_PIPELINE_DESIGN.md` eingearbeitet.

## Ausgangslage

- Speaches unterstützt nativ **keine** Speaker-Rec / Diarization (verifiziert auf der Repo-Doku).
- Wir haben ECAPA-TDNN ONNX (cosine=1.000000 vs SpeechBrain Ground-Truth, D4-Probe).
- Speaker-Rec ist **non-negotiable** (User-Aussage 2026-05-04).

## Drei Integrations-Pfade

### (a) Audio-Fork-Proxy + ECAPA-Sidecar

```
Frontend ──WS──▶ renfield-voice-proxy ─┬──▶ Speaches /v1/realtime
                  (audio fork in mem)  └──▶ ecapa-sidecar /embed
                                            │
                                            ▼
                                        Backend /api/voice/identify
```

- **Aufwand:** ~2 Tage (Proxy in Python/FastAPI, ECAPA-Container, k8s-Manifeste)
- **Fragilität:** Proxy = neuer Code = neue Bug-Surface. Audio-Fork ist in-memory, also kein Latenz-Hit.
- **Update-Burden:** Speaches kann unabhängig aktualisiert werden, ECAPA-Container ebenso. Proxy ist eigener Code.
- **Frontend:** Kann standard OpenAI-Realtime-SDK nutzen (Proxy ist transparent).
- **Latenz-Impact:** Vernachlässigbar (Single-Hop, In-Memory-Fork).

### (b) Speaches-Fork mit ECAPA inline

```
Frontend ──WS──▶ Speaches-Fork (STT + TTS + ECAPA inline)
                                              │
                                              ▼
                                  custom WS event "speaker_id"
```

- **Aufwand:** ~3-5 Tage initial, **ongoing forever** für Rebases.
- **Fragilität:** Forks rotten. Jedes Speaches-Release = ein Rebase oder verlorene Features.
- **Update-Burden:** Höchste — wir werden Speaches-Downstream-Maintainer.
- **Frontend:** Muss auf custom Events lauschen → SDK-Vorteil weg.
- **Latenz-Impact:** Beste (Single-Buffer, In-Process).

### (c) Backend-side Speaker-Rec, Audio doppelt geschickt

```
Frontend ──WS──▶ Speaches /v1/realtime  (STT/TTS)
         ──WS──▶ Backend /api/voice/identify  (ECAPA)
```

- **Aufwand:** ~0.5 Tag (Frontend dual-stream + Backend-Endpoint).
- **Fragilität:** Zwei Streams = zwei Fehlerquellen. Bandwidth-Doppelung. Sync-Problem (Welche Speaker-ID gehört zu welchem Transcript?).
- **Update-Burden:** Niedrigste.
- **Frontend:** Komplexer (dual-stream Capture).
- **Latenz-Impact:** Speaker-Rec async möglich, Barge-In aber komplizierter.

## Bewertung

| Kriterium | (a) Sidecar+Proxy | (b) Fork | (c) Dual-Stream |
|---|---|---|---|
| Initial-Aufwand | 2 d | 3-5 d | 0.5 d |
| Ongoing-Burden | niedrig | **hoch** | niedrig |
| Frontend-SDK-Kompat | ✓ | ✗ | ✓ |
| Speaker-Rec-Qualität | gleich | gleich | gleich |
| Sync zwischen STT+Speaker | ✓ einfach | ✓ trivial | **✗ schwierig** |
| Bandwidth-Effizienz | ✓ | ✓ | ✗ doppelt |

**Empfehlung:** (a) Audio-Fork-Proxy.

## Kritischer Reality-Check

Wenn wir (a) nehmen, schreiben wir trotzdem einen `renfield-voice-proxy`. Das ist der WebSocket-Server, dessen Vermeidung der Hauptgrund für "Buy Speaches" war.

**Frage:** Was bleibt vom Speaches-Vorteil übrig?

1. **STT/TTS-Plumbing nicht selbst schreiben** — ja, das bleibt. Speaches kümmert sich um faster-whisper Init, Piper-Voice-Loading, VRAM-Management, Modell-Eviction.
2. **OpenAI-Realtime-API als Wire-Protocol** — ja, das bleibt, bekannte SDK auf Frontend-Seite.
3. **Battle-tested durch andere User** — ja, das bleibt.

Aber:

1. Wir schreiben einen Proxy mit Audio-Fork — nicht trivial weniger Code als ein Voice-Server, der STT/TTS direkt aufruft.
2. Wir orchestrieren 3 Container statt 1 (Proxy + Speaches + ECAPA-Sidecar).
3. Wir sind auf Speaches' Realtime-API-Stabilität angewiesen — die ist in der Doku als "unstable" markiert (TODO im Code).

**Net:** Spar-Potenzial gegenüber Custom-Plan ist **nicht 2-3 Tage**, eher **0.5-1 Tag**, plus zusätzliche operative Komplexität. Die Speaches-Hypothese muss empirisch validiert werden — wenn die Realtime-API gut funktioniert und stabil ist, kippt das Bild zugunsten von Speaches. Wenn sie zickig ist, ist der Custom-Plan überlegen.

**Abhängige Empirik:**
- Latenz first-byte-out (Speaches Realtime-API)
- Stabilität unter Last (Reconnect, Modell-Eviction-Verhalten)
- Audio-Format-Kompatibilität (Browser MediaRecorder → Speaches)
