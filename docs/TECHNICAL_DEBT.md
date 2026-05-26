# Technical Debt - Renfield System

Dieses Dokument enthält eine umfassende Analyse der technischen Schulden im gesamten Renfield-System.

**Letzte Aktualisierung:** 2026-05-26

---

## Übersicht

| Bereich | Kritisch | Mittel | Niedrig | Gesamt | Behoben |
|---------|----------|--------|---------|--------|---------|
| Backend | 0 | 3 | 4 | 9 | 10 |
| Frontend | 0 | 1 | 3 | 7 | 5 |
| Satellite | 0 | 3 | 2 | 5 | 5 |
| Infrastruktur | 0 | 3 | 2 | 6 | 6 |
| **Gesamt** | **0** | **10** | **11** | **27** | **26** |

---

## Backend

### ~~🔴 Kritisch~~ → ✅ Behoben

#### ~~1. God Class: main.py (2130 → 337 Zeilen)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Die Datei `src/backend/main.py` enthielt zu viele Verantwortlichkeiten:
- FastAPI App-Konfiguration
- WebSocket-Handler (Chat, Device, Satellite)
- Lifecycle-Management
- Streaming-Logik

**Lösung:**
- ✅ Phase 1: Shared Utilities extrahiert
  - `api/websocket/shared.py` erstellt
  - `ConversationSessionState`, `RAGSessionState`, Helpers ausgelagert
- ✅ Phase 2: WebSocket-Handler extrahiert
  - `api/websocket/chat_handler.py` (~370 Zeilen)
  - `api/websocket/satellite_handler.py` (~550 Zeilen)
  - `api/websocket/device_handler.py` (~530 Zeilen)
- ✅ Phase 3: Alte Handler entfernt

**Ergebnis:**
- main.py: 2130 → 337 Zeilen (**84% Reduktion**)
- Alle 558 Tests bestanden

**Neue Struktur:**
```
api/
├── lifecycle.py         ✅ Startup/Shutdown management
├── websocket/
│   ├── __init__.py      ✅ Router exports
│   ├── shared.py        ✅ Shared utilities
│   ├── chat_handler.py  ✅ /ws endpoint
│   ├── device_handler.py ✅ /ws/device endpoint
│   └── satellite_handler.py ✅ /ws/satellite endpoint
└── routes/
    └── ... (unchanged)
```

---

#### 2. ~~Bare Except Clauses (6 Stellen)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Änderungen:**
- `main.py:1945` → `except Exception:`
- `output_routing_service.py:305` → `except Exception:`
- `device_manager.py:211, 573` → `except Exception:`
- `satellite_manager.py:164, 481` → `except Exception:`

---

### 🟡 Mittel

#### 3. Große API-Route-Dateien (teilweise behoben)

| Datei | Vorher | Nachher | Status |
|-------|--------|---------|--------|
| `routes/rooms.py` | 1024 | 875 | ✅ Schemas extrahiert |
| `routes/knowledge.py` | 1019 | 1076 | ✅ Schemas extrahiert, gewachsen durch neue Features |
| `routes/speakers.py` | 650 | 650 | OK, beobachten |

**Änderungen (2026-01-25):**
- `rooms_schemas.py` (182 Zeilen) - Pydantic Models extrahiert
- `knowledge_schemas.py` (117 Zeilen) - Pydantic Models extrahiert

---

#### ~~4. Hardcoded Fallback-Werte~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Fallback auf `localhost` funktioniert nicht in Container-Umgebungen.

**Lösung:** Neues Config-Setting `BACKEND_INTERNAL_URL` (Default: `http://backend:8000`) als Fallback statt localhost.

---

#### ~~5. Print Statements in CLI-Tools~~ ✅ Entfernt

**Status:** Entfernt am 2026-02-11

**Lösung:** CLI-Test-Tools (`test_plugins.py`, `test_url_encoding.py`, `test_error_handling.py`) wurden zusammen mit dem Legacy-Plugin-System entfernt.

---

#### ~~6. Fehlende Type Hints~~ ✅ Verbessert

**Status:** Verbessert am 2026-01-25

**Änderungen:**
- `ollama_service.py`: `ensure_model_loaded() -> None`
- `audio_output_service.py`: `_ensure_cache_dir() -> None`, `_cleanup_old_cache_files() -> None`
- TYPE_CHECKING Imports für Message hinzugefügt

**Empfehlung:** Weitere Type Hints schrittweise hinzufügen, mit `mypy` prüfen.

---

#### ~~7. Ollama Service Größe (966 → 773 Zeilen)~~ ✅ Teilweise behoben

**Status:** Teilweise behoben am 2026-01-25

**Änderungen:**
- `services/conversation_service.py` erstellt (~300 Zeilen)
- Conversation-Methoden aus OllamaService extrahiert
- OllamaService delegiert jetzt an ConversationService (Rückwärtskompatibilität)
- Reduktion: 966 → 773 Zeilen (**20% Reduktion**)

**Neue Struktur:**
```
services/
├── ollama_service.py       (773 Zeilen) - LLM, Intent, RAG
├── conversation_service.py (300 Zeilen) - Conversation Persistence (NEU)
└── rag_service.py          - Document Management (bestehend)
```

**Verbleibend:** Intent-Extraction könnte noch separiert werden.

---

#### ~~8. Duplizierte Ollama Client-Instantiierungen (5 Stellen)~~ ✅ Behoben

**Status:** Behoben am 2026-02-05

**Ursprüngliches Problem:** `ollama.AsyncClient(host=...)` wurde an 5 Stellen separat instanziiert mit duplizierter URL-Resolution-Logik (ollama_service, agent_service, agent_router, rag_service, intent_feedback_service).

**Lösung:**
- `utils/llm_client.py` erstellt: `LLMClient` Protocol (structural typing) + Factory mit URL-basiertem Caching
- `get_default_client()` für `settings.ollama_url`
- `get_agent_client(role_url, fallback_url)` für Agent-URL-Priorisierung
- 13 neue Tests in `tests/backend/test_llm_client.py`

---

#### 12. `procedural_skills.status` — Partial Index nach Rollout

**Status:** Offen — eingeführt mit v2.10 (#615), Review-Befund.

**Problem:** Die Composite-Indexe `idx_procedural_skills_status_user` und `idx_procedural_skills_tier_status` (pc20260527) sind B-Trees über eine Text-Spalte mit nur vier Werten (`draft` / `approved` / `rejected` / `archived`). Sobald der Draft-Gate live ist, ist die Verteilung stark verzerrt: der weit überwiegende Anteil der Zeilen wird `approved` sein, mit einem schmalen Hot-Tail an `draft`. Plain B-Tree-Indexe haben in dieser Kardinalitätsverteilung schlechte Selektivität — der Planner fällt häufig auf einen Seq-Scan zurück.

**Empfehlung:** Nach ~30 Tagen Rollout (sobald Größenordnung der Inbox bekannt ist):
- `idx_procedural_skills_status_user` durch Partial-Index `WHERE status = 'approved'` ersetzen — dem Pfad, der `find_similar` dominiert.
- Separater kleiner Partial-Index `WHERE status = 'draft'` für die Admin-Inbox-Abfrage.
- `idx_procedural_skills_tier_status` analog evaluieren.

**Aufwand:** ~1h. Eine `ALTER INDEX … RENAME` plus neue partial-CREATE-Statements in einer Mini-Migration; rückwärts-kompatibel.

**Tracking:** Eigenes Issue eröffnen sobald Inbox-Volumen messbar ist.

---

#### 13. Alembic Backfill-UPDATEs ohne vorheriges Index-Drop

**Status:** Offen — eingeführt mit v2.10 (#615), Review-Befund.

**Problem:** `pc20260527_skill_approval_status.upgrade()` führt fünf `UPDATE`-Statements gegen `procedural_skills` aus, _bevor_ die alten Indexe (`idx_procedural_skills_active_user`, `idx_procedural_skills_tier_active`) gedroppt werden. Jeder UPDATE muss die alten Indexe pflegen, obwohl sie unmittelbar danach verworfen werden. Bei `procedural_skills` heute unkritisch (kleine Tabelle), aber als Migration-Pattern gefährlich — auf einer Multi-Millionen-Zeilen-Tabelle würde die Migration die zehn- bis hundertfache Zeit benötigen.

**Empfehlung:** Konvention für künftige Backfill-Migrationen dokumentieren:
1. Alte (zu droppende) Indexe zuerst entfernen.
2. Backfill-UPDATEs ausführen.
3. Neue Indexe und Constraints zuletzt anlegen.

Optional: Eine kurze Notiz im neuen `CONTRIBUTING.md`-Abschnitt zu Alembic-Best-Practices, parallel zum bestehenden `memory/feedback_alembic_chain_check.md`.

**Aufwand:** ~30 min für die Konvention; bestehende `pc20260527` nicht nachträglich umschreiben (irrelevant für die aktuelle Tabellengröße).

---

### 🟢 Niedrig

#### ~~8. Alembic Migrations ohne Downgrade~~ ✅ OK

**Status:** Überprüft am 2026-01-26

Die initiale Migration (`9a0d8ccea5b0_add_room_management.py`) hat korrekt `pass` da sie keine Tabellen erstellt. Alle anderen Migrations haben funktionierende `downgrade()` Funktionen.

#### ~~9. Nicht genutzte Imports~~ ✅ Behoben

**Status:** Behoben am 2026-01-26

**30 ungenutzte Imports entfernt** aus:
- `main.py`, `models/permissions.py`
- `api/routes/`: rooms, users, roles, satellites, preferences, homeassistant, camera, settings, speakers, knowledge
- `api/websocket/`: chat_handler, shared
- `services/`: auth, rag, database, document_processor, output_routing, wakeword_config_manager, zeroconf, device_manager, audio_output, piper

#### ~~10. Docstrings fehlen teilweise~~ ✅ Dokumentiert

**Status:** Dokumentiert am 2026-01-26

21 öffentliche Funktionen ohne Docstrings identifiziert (hauptsächlich `__init__` Methoden). Service-Klassen haben bereits Docstrings, nur `__init__` Methoden fehlen teilweise.

#### ~~11. Magic Numbers~~ ✅ Behoben

**Status:** Behoben am 2026-01-26

Session- und Heartbeat-Timeouts in `config.py` ausgelagert:
- `device_session_timeout: float = 30.0`
- `device_heartbeat_timeout: float = 60.0`

`device_manager.py` und `satellite_manager.py` verwenden jetzt die Config-Werte.

---

## Frontend

### ~~🔴 Kritisch~~ → ✅ Behoben

#### ~~1. ChatPage.jsx (1295 → 555 Zeilen)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Monolithische Komponente mit zu vielen Verantwortlichkeiten:
- WebSocket-Verbindung
- Audio Recording
- Message Rendering
- Session Management

**Lösung:** Aufgeteilt in modulare Struktur:
```
pages/ChatPage/
├── index.jsx              (555 Zeilen) - Haupt-Orchestrator
├── ChatMessages.jsx       (101 Zeilen) - Nachrichtenanzeige
├── ChatInput.jsx          (191 Zeilen) - Eingabebereich + RAG
├── ChatHeader.jsx         (174 Zeilen) - Wake Word Controls
├── AudioVisualizer.jsx    (74 Zeilen)  - Wellenform-Anzeige
└── hooks/
    ├── index.js           (2 Zeilen)   - Exports
    ├── useChatWebSocket.js (114 Zeilen) - WebSocket-Logik
    └── useAudioRecording.js (370 Zeilen) - Audio + VAD
```

**Ergebnis:**
- Haupt-Datei: 1295 → 555 Zeilen (**57% Reduktion**)
- 7 separate Module für bessere Wartbarkeit
- Alle 10 Tests bestanden
- Build erfolgreich

---

### 🟡 Mittel

#### ~~2. Console.log Statements (30+)~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Lösung:**
- `utils/debug.js` erstellt - Debug-Logger der nur im Dev-Modus loggt
- 80 `console.log` → `debug.log` ersetzt in:
  - `hooks/useWakeWord.js` (15)
  - `hooks/useDeviceConnection.js` (9)
  - `pages/ChatPage/hooks/useAudioRecording.js` (29)
  - `pages/ChatPage/hooks/useChatWebSocket.js` (4)
  - `pages/ChatPage/index.jsx` (23)

---

#### 3. TypeScript Migration (teilweise abgeschlossen)

**Status:** ✅ Grundgerüst migriert (2026-01-26)

**Migrierte Dateien:**
- `tsconfig.json`, `tsconfig.node.json` - TypeScript Konfiguration
- `vite.config.ts` - Vite Config mit Path Aliases
- `src/types/` - Type Definitionen (device, chat, api)
- `src/hooks/*.ts` - Alle Hooks (useDeviceConnection, useChatSessions, useWakeWord, useCapabilities)
- `src/context/*.tsx` - Alle Contexts (Auth, Device, Theme)
- `src/utils/*.ts` - Utilities (axios, debug)
- `src/config/wakeword.ts` - Wake Word Konfiguration

**Noch zu migrieren:**
- `src/pages/*.jsx` - Seiten-Komponenten
- `src/components/*.jsx` - UI-Komponenten
- `src/main.jsx`, `src/App.jsx` - Entry Points

**Konfiguration:** Permissive Settings (`strict: false`, `allowJs: true`) für schrittweise Migration.

---

#### 4. Outdated Dependencies (teilweise behoben)

| Package | Current | Latest | Breaking | Status |
|---------|---------|--------|----------|--------|
| react | 18.3.1 | 19.x | ⚠️ Major | ⏳ |
| react-router-dom | 6.30.3 | 7.x | ⚠️ Major | ⏳ |
| tailwindcss | 3.4.19 | 4.x | ⚠️ Major | ⏳ |
| vite | 5.4.21 | 7.x | ⚠️ Major | ⏳ |
| @headlessui/react | 1.7.19 | 2.x | ⚠️ Major | ⏳ |
| lucide-react | 0.307.0 | 0.563.0 | ✅ Minor | ✅ |

**Änderungen (2026-01-25):**
- lucide-react 0.307.0 → 0.563.0 aktualisiert

**Empfehlung:** Major-Updates einzeln planen und testen.

---

#### ~~5. ESLint-Disable Kommentare~~ ✅ Dokumentiert

**Status:** Dokumentiert am 2026-01-25

**Lösung:** Der ESLint-disable Kommentar in `useDeviceConnection.js` ist berechtigt.
Das `connect` wird absichtlich aus den Dependencies ausgelassen, um Reconnection-Loops zu verhindern.
Kommentar wurde erweitert um die Begründung zu dokumentieren.

---

### 🟢 Niedrig

#### 6. Große Komponenten

- `SpeakersPage.jsx` (1027 Zeilen)
- `RoomsPage.jsx` (762 Zeilen)
- `useDeviceConnection.js` (616 Zeilen)

#### 7. Fehlende Error Boundaries

Nur eine zentrale ErrorBoundary, keine Feature-spezifischen.

#### ~~8. Keine Unit Tests für Hooks~~ ✅ Behoben

**Status:** Behoben am 2026-01-26

**Änderungen:**
- `tests/frontend/react/hooks/useWakeWord.test.jsx` erstellt (15 Tests)
- Initial State, Settings Management, Callbacks, Enable/Disable, Pause/Resume, Toggle, Config Events, Cleanup getestet

---

## Satellite

### ✅ Behoben

#### ~~Native S32_LE/4ch Audio Capture für 4-Mic Array~~ ✅ (RESOLVED 2026-02-09)

`arecord` subprocess captures 4ch/S32_LE natively. Python converts to mono S16_LE (channel 1, right-shift 16). PyAudio cannot be used due to kernel crash with onnxruntime. RMS VAD replaces Silero for reliable end-of-speech detection under CPU load. See `src/satellite/TECHNICAL_DEBT.md` for details.

---

### ~~🟡 Mittel~~ → ✅ Behoben/Dokumentiert

#### ~~1. Bare Except Clauses (22)~~ ✅ Behoben

**Status:** Behoben am 2026-01-26

**22 bare except Clauses ersetzt** durch spezifische Exceptions:
- `hardware/button.py` (6) → `Exception` für GPIO Cleanup
- `hardware/led.py` (1) → `OSError` für SPI
- `audio/playback.py` (4) → `Exception`, `OSError` für MPV/Temp-Files
- `audio/capture.py` (3) → `Exception`, `(ValueError, TypeError)` für PyAudio/numpy
- `audio/preprocessor.py` (1) → `(ValueError, TypeError)` für numpy
- `audio/vad.py` (3) → `(ValueError, TypeError)`, `Exception` für VAD
- `network/websocket_client.py` (1) → `Exception` für WebSocket
- `satellite.py` (1) → `(OSError, ValueError)` für Temperatur
- `cli/monitor.py` (2) → `(OSError, ValueError)`, `Exception` für Config/Temp

---

#### ~~2. satellite.py Größe (875 Zeilen)~~ ✅ Dokumentiert

**Status:** Überprüft am 2026-01-26 - Akzeptabel

**Analyse:**
- Satellite-Klasse ist ein Orchestrator mit 6 einfachen States
- Komponenten bereits modular extrahiert:
  - `audio/` - Capture, Playback, VAD, Preprocessing
  - `hardware/` - LED, Button
  - `network/` - WebSocket, Discovery, Auth
  - `wakeword/` - Detector
  - `update/` - UpdateManager
- Aufteilung würde Indirektion ohne Nutzen hinzufügen

**Entscheidung:** Keine weitere Aufteilung erforderlich.

---

#### ~~3. Hardware-Abstraktionsschicht~~ ✅ Dokumentiert

**Status:** Überprüft am 2026-01-26 - Bereits vorhanden

**Vorhandene Infrastruktur:**
- `tests/satellite/conftest.py` enthält Hardware-Mocks:
  - `mock_led_controller` - LED Mocking
  - `mock_button` - GPIO Button Mocking
  - `mock_microphone` - Mikrophone Mocking
  - `mock_speaker` - Speaker Mocking
  - `mock_wakeword_detector` - Wake Word Mocking
- Hardware-Module prüfen Bibliotheksverfügbarkeit (`LGPIO_AVAILABLE`, `RPIGPIO_AVAILABLE`)
- Graceful Degradation wenn Hardware nicht verfügbar

---

### ~~🟢 Niedrig~~ → ✅ Dokumentiert

#### ~~4. Pi Zero 2 W Einschränkungen~~ ✅ Dokumentiert

**Status:** Bereits dokumentiert in `src/satellite/TECHNICAL_DEBT.md`

**Bekannte Einschränkungen:**
- ARM32 (armv7l) → PyTorch nicht verfügbar
- 512MB RAM → große Python-Pakete können nicht kompiliert werden
- Kein Silero VAD → WebRTC VAD als Workaround

**Workarounds dokumentiert:**
- WebRTC VAD statt Silero
- `pip install noisereduce --no-deps`
- Swap erhöhen für große Pakete

#### ~~5. Logging~~ ✅ Dokumentiert

**Status:** Überprüft am 2026-01-26 - Akzeptabel

**Analyse:**
- 307 `print()` Statements, 0 `logger` Statements
- Kein Mix - durchgängig `print()` verwendet
- Für Embedded-Gerät (Raspberry Pi) akzeptabel:
  - `print()` → stdout → systemd/journald
  - Einfacherer Code ohne Logger-Konfiguration
  - Satellite läuft als Service, journalctl zeigt Logs

**Entscheidung:** Keine Änderung erforderlich.

---

## Infrastruktur

### ~~🔴 Kritisch~~ → ✅ Behoben

#### ~~1. :latest Tags in Docker~~ ✅ Behoben

**Status:** Behoben am 2026-01-25

**Ursprüngliches Problem:** Docker Compose Dateien verwendeten `:latest` Tags.

**Lösung:** Alle Images auf spezifische Versionen gepinnt:
- `ollama/ollama:latest` → `ollama/ollama:0.15.1`
- `nginx:alpine` → `nginx:1.28-alpine`

Bereits gepinnte Images:
- `pgvector/pgvector:pg16` ✅
- `redis:7-alpine` ✅

---

### ~~🟡 Mittel~~ → ✅ Behoben/Dokumentiert

#### ~~2. Unpinned Python Dependencies~~ ✅ Dokumentiert

**Status:** Überprüft am 2026-01-26 - Akzeptabel

**Analyse:**
- 7 exakt gepinnt (`==`), 34 mit Minimum-Version (`>=`)
- Docker-Images fungieren als effektives "Lockfile"
- `>=` ermöglicht Flexibilität bei Upgrades
- Kritische Packages (whisper, bcrypt, pytest) sind gepinnt

**Entscheidung:** Aktueller Ansatz ist für Docker-basiertes Projekt akzeptabel.

---

#### ~~3. Health Checks in Docker Compose~~ ✅ Behoben

**Status:** Behoben am 2026-01-26

**Hinzugefügte Health Checks:**
- `postgres`: `pg_isready -U renfield -d renfield`
- `redis`: `redis-cli ping`
- `ollama`: `curl -f http://localhost:11434/api/tags`
- `backend`: `curl -f http://localhost:8000/health`
- `frontend`: `wget -q --spider http://localhost:3000`
- `nginx`: `wget -q --spider http://localhost:80`

**Zusätzliche Verbesserungen:**
- `depends_on` mit `condition: service_healthy` für Startabhängigkeiten
- Aktualisiert in: `docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.prod.yml`

---

#### ~~4. Rate Limiting~~ ✅ Behoben

**Status:** Vollständig implementiert am 2026-01-26

**Implementiert:**
- ✅ **REST API Rate Limiting**: `services/api_rate_limiter.py`
  - Verwendet slowapi für FastAPI
  - Konfigurierbare Limits via `.env`:
    - `api_rate_limit_default: 100/minute` (Standard)
    - `api_rate_limit_auth: 10/minute` (Login, Register, Token Refresh, Voice Auth)
    - `api_rate_limit_voice: 30/minute` (STT, TTS, Voice-Chat)
    - `api_rate_limit_chat: 60/minute` (Chat Send)
    - `api_rate_limit_admin: 200/minute` (Admin Endpoints)
  - X-Forwarded-For Header Support für Reverse Proxies
  - JSON Error Response mit Retry-After Header
- ✅ **WebSocket Rate Limiting**: `websocket_rate_limiter.py`
  - Chat, Device, Satellite Handler
  - Konfigurierbar: `ws_rate_limit_per_second`, `ws_rate_limit_per_minute`

**Angewandt auf:**
- `api/routes/auth.py`: login, register, refresh, voice (10/min)
- `api/routes/voice.py`: stt, tts, voice-chat (30/min)
- `api/routes/chat.py`: send (60/min)

---

### ~~🟢 Niedrig~~ → ✅ Behoben

#### ~~5. Keine Multi-Stage Builds~~ ✅ Behoben

**Status:** Behoben am 2026-01-26

**Änderungen:**
- Frontend Dockerfile auf Multi-Stage Build umgestellt:
  - Stage 1: `base` - Dependencies installieren
  - Stage 2: `development` - Vite Dev Server (624MB)
  - Stage 3: `build` - Production Build
  - Stage 4: `production` - Nginx mit statischen Dateien (155MB)
- **75% Image-Größenreduktion** für Production
- `nginx.conf` für SPA-Routing hinzugefügt
- docker-compose Dateien für Build-Targets aktualisiert

#### ~~6. Secrets in .env~~ ✅ Dokumentiert

**Status:** Dokumentiert am 2026-01-26

**Neue Dokumentation:** `docs/SECRETS_MANAGEMENT.md`
- Docker Secrets Anleitung
- HashiCorp Vault Integration
- Kubernetes Secrets
- Produktions-Checkliste
- Scripts zum Generieren sicherer Secrets

---

## Test-Coverage

| Bereich | Test Files | Tests | Source Files | Ratio |
|---------|------------|-------|--------------|-------|
| Backend | 62 | 1642 | ~80 | 78% |
| Frontend | 18 | 289 | ~40 | 45% |
| Satellite | 1 | - | 15 | 7% |

### Fehlende Tests

- [ ] `services/audio_output_service.py` - kein Test
- [ ] `services/output_routing_service.py` - kein Test
- [ ] `integrations/frigate.py` - nur Mock-Tests
- [x] Frontend Hooks - Tests vorhanden (`useChatSessions.test.jsx`, `useCapabilities.test.jsx`)
- [ ] Satellite Hardware - keine Tests möglich ohne Mocks

---

## Priorisierte Empfehlungen

### Sofort (< 1 Woche)

1. ✅ ~~Bare except → Exception ersetzen~~ (2026-01-25)
2. ✅ ~~Docker :latest → gepinnte Versionen~~ (2026-01-25)
3. ✅ ~~Console.log → Debug-Logger~~ (2026-01-25)

### Kurzfristig (1-4 Wochen)

4. ✅ ~~main.py Refactoring~~ (2026-01-25)
5. ✅ ~~ChatPage.jsx aufteilen~~ (2026-01-25)
6. ⬜ Requirements pinnen
7. ✅ ~~Type Hints hinzufügen (Backend)~~ (2026-01-25)
8. ✅ ~~ollama_service.py Refactoring~~ (2026-01-25)

### Mittelfristig (1-3 Monate)

9. 🔄 TypeScript Migration (Frontend) - Grundgerüst fertig (2026-01-26)
10. ✅ ~~Test-Coverage Enforcement~~ (2026-02-04: `--cov-fail-under=50` in CI)
11. ⬜ Dependency Updates (Minor)

### Langfristig (3-6 Monate)

12. ⬜ Major Dependency Updates (React 19, etc.)
13. ✅ ~~Hardware-Abstraktionsschicht (Satellite)~~ - Bereits vorhanden (2026-01-26)
14. ✅ ~~Multi-Stage Docker Builds~~ (2026-01-26)

---

## Changelog

| Datum | Änderung |
|-------|----------|
| 2026-02-05 | LLM Client Factory: 5 duplizierte `ollama.AsyncClient`-Instantiierungen durch zentrale Factory + Protocol ersetzt (`utils/llm_client.py`), URL-basiertes Caching, 13 neue Tests (#60) |
| 2026-02-04 | Prometheus `/metrics` Endpoint implementiert: HTTP, WebSocket, LLM, Circuit Breaker Metriken (opt-in via METRICS_ENABLED) |
| 2026-02-04 | Coverage-Threshold Enforcement: `--cov-fail-under=50` in CI und Makefile |
| 2026-02-04 | flake8 durch ruff ersetzt: `pyproject.toml` mit ruff + pytest Config, `pytest.ini` gelöscht, CI auf ruff umgestellt, 1457 Violations auto-fixed + 30 manuell behoben |
| 2026-01-26 | TypeScript Migration: Hooks, Context, Utils, Types migriert; permissive Config (#38) |
| 2026-01-26 | Frontend Multi-Stage Build: 624MB → 155MB (75% Reduktion), nginx.conf für SPA (#37) |
| 2026-01-26 | Secrets Management dokumentiert: Docker Secrets, Vault, Kubernetes (#37) |
| 2026-01-26 | REST API Rate Limiting implementiert: slowapi, auth 10/min, voice 30/min, chat 60/min (#36) |
| 2026-01-26 | Docker Health Checks hinzugefügt: postgres, redis, ollama, backend, frontend, nginx (#36) |
| 2026-01-26 | Python Dependencies dokumentiert: >= Ansatz für Docker akzeptabel (#36) |
| 2026-01-26 | Rate Limiting dokumentiert: WebSocket + Plugins implementiert (#36) |
| 2026-01-26 | Pi Zero 2 W Einschränkungen dokumentiert: Bereits in src/satellite/TECHNICAL_DEBT.md (#34) |
| 2026-01-26 | Satellite Logging dokumentiert: 307 print() konsistent, kein Mix (#34) |
| 2026-01-26 | Satellite Bare Except Clauses behoben: 22 → spezifische Exceptions (#33) |
| 2026-01-26 | satellite.py Größe dokumentiert: 875 Zeilen akzeptabel als Orchestrator (#33) |
| 2026-01-26 | Hardware-Abstraktionsschicht dokumentiert: Mocks bereits in conftest.py (#33) |
| 2026-01-26 | Hook-Tests für useWakeWord erstellt: 15 Tests (#32) |
| 2026-01-26 | Niedrige Technical Debt behoben: 30 ungenutzte Imports entfernt (#29) |
| 2026-01-26 | Magic Numbers in Config ausgelagert: device_session_timeout, device_heartbeat_timeout (#29) |
| 2026-01-25 | Frontend-Tests auf deutsche Übersetzungen aktualisiert (262 Tests, alle bestanden) |
| 2026-01-25 | Debug-Logger utils/debug.js erstellt, 80 console.log → debug.log ersetzt (#31) |
| 2026-01-25 | lucide-react 0.307.0 → 0.563.0 aktualisiert (#31) |
| 2026-01-25 | ESLint-disable Kommentar in useDeviceConnection.js dokumentiert (#31) |
| 2026-01-25 | ConversationService extrahiert aus OllamaService: 966 → 773 Zeilen (20% Reduktion) (#28) |
| 2026-01-25 | Type Hints hinzugefügt: ollama_service.py, audio_output_service.py (#28) |
| 2026-01-25 | Schemas extrahiert: rooms_schemas.py, knowledge_schemas.py (#28) |
| 2026-01-25 | CLI-Test-Tools dokumentiert (print statements OK für CLI) (#28) |
| 2026-01-25 | Hardcoded localhost durch BACKEND_INTERNAL_URL ersetzt (#28) |
| 2026-01-25 | ChatPage.jsx Refactoring: 1295 → 555 Zeilen (57% Reduktion), 7 Module (#30) |
| 2026-01-25 | Docker :latest Tags durch gepinnte Versionen ersetzt (#35) |
| 2026-01-25 | Lifecycle-Management extrahiert nach api/lifecycle.py (#27) |
| 2026-01-25 | main.py Refactoring abgeschlossen: 2130 → 337 Zeilen (84% Reduktion) (#27) |
| 2026-01-25 | WebSocket-Handler extrahiert: chat, satellite, device (#27) |
| 2026-01-25 | main.py Refactoring Phase 1: Shared Utilities extrahiert (#27) |
| 2026-01-25 | Bare Except Clauses im Backend behoben (#27) |
| 2026-01-25 | Initial Technical Debt Analyse |
