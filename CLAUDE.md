# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Renfield is a fully offline-capable, self-hosted AI assistant for smart home control, camera monitoring, and workflow automation. It uses local LLMs (Ollama), speech-to-text (Whisper), text-to-speech (Piper), and integrates with Home Assistant, Frigate, and n8n.

**Tech Stack:**
- Backend: Python 3.11 + FastAPI + SQLAlchemy
- Frontend: React 18 + TypeScript + Vite + Tailwind CSS + PWA
- Infrastructure: Docker Compose, PostgreSQL 16, Redis 7, Ollama
- Integrations: Home Assistant, Frigate (camera NVR), n8n (workflows)
- Satellites: Raspberry Pi Zero 2 W + ReSpeaker 2-Mics Pi HAT + OpenWakeWord

## KRITISCHE REGELN - IMMER BEACHTEN

### Git Push Verbot

**NIEMALS `git push` ohne explizite Erlaubnis des Benutzers ausführen!**

- Nach jedem Commit MUSS gefragt werden: "Soll ich pushen?"
- Erst nach ausdrücklicher Bestätigung ("ja", "push", etc.) darf gepusht werden
- Diese Regel gilt auch nach Session-Komprimierung (Compact)
- Bei Unsicherheit: IMMER fragen, NIEMALS automatisch pushen

---

## Development Guidelines

### Test-Driven Development (TDD)

**WICHTIG: Bei jeder Code-Änderung müssen passende Tests mitgeliefert werden.**

Beim Entwickeln neuer Features oder Bugfixes:

1. **Neue API-Endpoints**: Erstelle Tests in `tests/backend/test_<route>.py`
   - HTTP Status Codes testen
   - Request/Response Schemas validieren
   - Fehlerbehandlung (404, 400, 401, 403) testen
   - Edge Cases abdecken

2. **Neue Services**: Erstelle Tests in `tests/backend/test_services.py`
   - Unit-Tests für isolierte Funktionalität
   - Mocks für externe Dependencies (Ollama, HA, etc.)
   - Async-Funktionen mit `@pytest.mark.unit` markieren

3. **Datenbank-Änderungen**: Teste in `tests/backend/test_models.py`
   - Model-Erstellung und Constraints
   - Beziehungen (Relationships)
   - Mit `@pytest.mark.database` markieren

4. **Frontend-Komponenten**: Teste in `tests/frontend/react/`
   - Rendering-Tests mit React Testing Library
   - User-Interaktionen simulieren
   - API-Calls mit MSW mocken

**Backend Test-Beispiel für neuen Endpoint:**
```python
# tests/backend/test_<feature>.py
class TestNewFeatureAPI:
    @pytest.mark.integration
    async def test_create_item(self, async_client: AsyncClient, db_session):
        """Testet POST /api/items"""
        response = await async_client.post("/api/items", json={"name": "Test"})

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Test"
        assert "id" in data

    @pytest.mark.integration
    async def test_create_item_invalid(self, async_client: AsyncClient):
        """Testet Validierungsfehler"""
        response = await async_client.post("/api/items", json={})

        assert response.status_code == 422  # Validation error
```

**Frontend Test-Beispiel für neue Komponente:**
```jsx
// tests/frontend/react/components/NewFeature.test.jsx
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi } from 'vitest'
import { BrowserRouter } from 'react-router-dom'
import NewFeature from '../../../../src/frontend/src/components/NewFeature'

// Mock API calls
vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn()
  }
}))

describe('NewFeature', () => {
  it('renders correctly', () => {
    render(
      <BrowserRouter>
        <NewFeature />
      </BrowserRouter>
    )

    expect(screen.getByText('Feature Title')).toBeInTheDocument()
  })

  it('handles user interaction', async () => {
    render(
      <BrowserRouter>
        <NewFeature />
      </BrowserRouter>
    )

    fireEvent.click(screen.getByRole('button', { name: /submit/i }))

    await waitFor(() => {
      expect(screen.getByText('Success')).toBeInTheDocument()
    })
  })

  it('displays error on API failure', async () => {
    const axios = await import('../../../../src/frontend/src/utils/axios')
    axios.default.post.mockRejectedValueOnce(new Error('Network error'))

    render(
      <BrowserRouter>
        <NewFeature />
      </BrowserRouter>
    )

    fireEvent.click(screen.getByRole('button', { name: /submit/i }))

    await waitFor(() => {
      expect(screen.getByText(/error/i)).toBeInTheDocument()
    })
  })
})
```

**Frontend Test-Struktur:**
```
tests/frontend/
├── react/                     # React-Komponenten Tests (Vitest)
│   ├── setup.js               # Test-Setup mit jsdom, RTL
│   ├── components/            # Komponenten-Tests
│   │   ├── Layout.test.jsx
│   │   ├── DeviceSetup.test.jsx
│   │   ├── ChatSidebar.test.jsx
│   │   └── NewFeature.test.jsx
│   ├── pages/                 # Seiten-Tests
│   │   ├── HomePage.test.jsx
│   │   ├── ChatPage.test.jsx
│   │   └── RoomsPage.test.jsx
│   └── hooks/                 # Custom Hooks Tests
│       ├── useCapabilities.test.jsx
│       └── useChatSessions.test.jsx
└── test_api_contracts.py      # API Contract Tests (pytest)
```

**Frontend-Tests je nach Änderungstyp:**

| Änderung | Test-Datei | Was testen |
|----------|------------|------------|
| Neue Komponente | `tests/frontend/react/components/<Name>.test.jsx` | Rendering, Props, Events |
| Neue Seite | `tests/frontend/react/pages/<Name>.test.jsx` | Routing, API-Calls, State |
| Neuer Hook | `tests/frontend/react/hooks/<Name>.test.jsx` | Return-Werte, Side-Effects |
| API-Änderung | `tests/frontend/test_api_contracts.py` | Request/Response Schema |

**Nach dem Entwickeln:**
```bash
# Backend-Tests ausführen
make test-backend

# Frontend React-Tests ausführen
make test-frontend-react

# Alle Tests ausführen
make test

# Bei neuem Feature: Coverage prüfen
make test-coverage
```

### Git Workflow

**⚠️ KRITISCH: Diese Regeln gelten für ALLE Git-Operationen:**

1. **NIEMALS ohne Erlaubnis pushen** ⛔
   - `git push` NUR ausführen, wenn der Benutzer EXPLIZIT die Erlaubnis erteilt
   - Nach JEDEM Commit MUSS gefragt werden: "Soll ich pushen?"
   - Auf Bestätigung warten ("ja", "push", "ok") bevor git push ausgeführt wird
   - Diese Regel überlebt Session-Komprimierung und MUSS immer beachtet werden

2. **Issue-Nummer bei jedem Commit**
   - Vor jedem Commit nach der Issue-Nummer fragen
   - Format: `fix/feat/docs(scope): Beschreibung (#123)`
   - Beispiel: `feat(satellites): Add monitoring dashboard (#25)`

3. **Commit-Message Format**
   ```
   type(scope): Kurze Beschreibung (#issue)

   Längere Beschreibung falls nötig.

   Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>
   ```

4. **Typische Commit-Types**
   - `feat`: Neues Feature
   - `fix`: Bugfix
   - `docs`: Dokumentation
   - `refactor`: Code-Refactoring
   - `test`: Tests hinzufügen/ändern
   - `chore`: Wartung, Dependencies

## Development Commands

### Quick Start
```bash
# Start entire stack (including model download)
./bin/start.sh

# Update system
./bin/update.sh

# Debug mode with detailed logging
./bin/debug.sh

# Quick backend restart
./bin/quick-update.sh

# Deploy to GitHub
./bin/deploy.sh

# Deploy satellite code to Raspberry Pi
./bin/deploy-satellite.sh [hostname] [user]
```

### Makefile Commands

The project includes a Makefile for task orchestration:

```bash
# Show all available commands
make help

# Development
make dev              # Start development environment
make stop             # Stop all containers
make restart          # Restart all containers
make logs             # Show container logs

# Building
make build            # Build all components
make docker-build     # Build Docker images

# Testing
make test             # Run all tests
make test-backend     # Run backend tests only
make test-frontend    # Run frontend API contract tests
make test-frontend-react  # Run React component tests (Vitest)
make test-coverage    # Run tests with coverage report
make lint             # Lint all code

# Database
make db-migrate       # Create new migration
make db-upgrade       # Apply migrations
make db-downgrade     # Rollback last migration

# Ollama
make ollama-pull      # Pull/update Ollama model
make ollama-test      # Test Ollama connection

# CI/CD
make ci               # Run CI pipeline (lint + test)
make release          # Create a release tag
```

### Docker Compose Variants
```bash
# Development on Mac (no GPU)
docker compose -f docker-compose.dev.yml up -d

# Production with NVIDIA GPU
docker compose -f docker-compose.prod.yml up -d

# Standard (CPU only)
docker compose up -d
```

| File | Use Case | Features |
|------|----------|----------|
| `docker-compose.yml` | Standard | Basic setup, CPU-only |
| `docker-compose.dev.yml` | Development | Mac-friendly, debug ports exposed |
| `docker-compose.prod.yml` | Production | NVIDIA GPU, nginx with SSL |

### Backend Development
```bash
cd src/backend

# Install dependencies
pip install -r requirements.txt

# Run dev server with hot reload
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# View API docs
open http://localhost:8000/docs
```

### Frontend Development
```bash
cd src/frontend

# Install dependencies
npm install

# Run dev server
npm run dev

# Build for production
npm run build

# Preview production build
npm run preview

# Lint
npm run lint
```

### Docker Commands
```bash
# Start all services
docker compose up -d

# View logs
docker compose logs -f
docker compose logs -f backend  # specific service

# Restart service
docker compose restart backend

# Rebuild after code changes
docker compose up -d --build

# Stop all
docker compose down

# Reset everything (DELETES DATA)
docker compose down -v
```

### Database
```bash
# Create migration
docker exec -it renfield-backend alembic revision --autogenerate -m "description"

# Apply migrations
docker exec -it renfield-backend alembic upgrade head

# Rollback
docker exec -it renfield-backend alembic downgrade -1
```

### Ollama Model Management

#### Lokaler Docker-Container
```bash
# Start mit Ollama-Container
docker compose --profile ollama up -d

# Pull/update model
docker exec -it renfield-ollama ollama pull llama3.2:3b

# List installed models
docker exec -it renfield-ollama ollama list

# Remove model
docker exec -it renfield-ollama ollama rm llama3.2:3b
```

#### Externe Ollama-Instanz
```bash
# .env konfigurieren
OLLAMA_URL=http://cuda.local:11434

# Standard-Start (ohne Ollama-Container)
docker compose up -d

# Test externe Verbindung
docker exec renfield-backend curl http://cuda.local:11434/api/tags
```

**Dokumentation:** Siehe [EXTERNAL_OLLAMA.md](docs/EXTERNAL_OLLAMA.md) für vollständige Anleitung zur Nutzung externer Ollama-Instanzen.

## Architecture

### Request Flow
```
User Input → Frontend (React)
  ↓
WebSocket/REST → Backend (FastAPI)
  ↓
Intent Recognition → OllamaService.extract_intent()
  ↓
Action Execution → ActionExecutor.execute()
  ↓
Integration → HomeAssistantClient / N8NClient / FrigateClient
  ↓
Response → Frontend (streaming or JSON)
```

### Satellite Request Flow
```
Wake Word → Satellite (Pi Zero 2 W)
  ↓
Audio Streaming → Backend (WebSocket /ws/satellite)
  ↓
Whisper STT → Transcription
  ↓
Intent Recognition → OllamaService.extract_intent()
  ↓
Action Execution → ActionExecutor.execute()
  ↓
Response Generation → OllamaService.generate()
  ↓
Piper TTS → Audio Response
  ↓
Audio Playback → Satellite Speaker
```

### Intent Recognition System

The core of Renfield is the intent recognition system in `src/backend/services/ollama_service.py`:

1. **extract_intent()**: Uses Ollama LLM to parse natural language into structured intents
2. **Dynamic Keyword Matching**: Fetches device names from Home Assistant to improve accuracy
3. **Intent Types**:
   - `homeassistant.*` - Smart home control (turn_on, turn_off, get_state, etc.)
   - `n8n.*` - Workflow triggers
   - `camera.*` - Camera/Frigate actions
   - `general.conversation` - Normal chat (no action needed)

**Key Implementation Detail**: The system pre-loads Home Assistant entity names and friendly names as "keywords" to determine if a user query is smart-home related. See `HomeAssistantClient.get_keywords()` for the dynamic keyword extraction logic.

### Conversation Persistence

Renfield implements full conversation persistence with PostgreSQL across **all conversation types** (Chat, WebSocket, Satellite):

**Features:**
- Automatic message storage for all user/assistant interactions
- Context loading (last N messages) for conversation continuity
- **Follow-up question support** - understands "Mach es aus" or "Und morgen?" without explicit references
- Full-text search across all conversations
- Conversation statistics and analytics
- Automatic cleanup of old conversations

**Supported Channels:**
| Channel | Session ID Format | History Length | Persistence |
|---------|-------------------|----------------|-------------|
| REST API (`/api/chat/send`) | Client-provided | 20 messages | Immediate |
| WebSocket (`/ws`) | Client-provided via `session_id` field | 10 messages | Immediate |
| Satellite (`/ws/satellite`) | `satellite-{id}-{YYYY-MM-DD}` (daily) | 5 messages | Immediate |

**Key Implementation** (in `src/backend/main.py`):
- `ConversationSessionState` dataclass maintains in-memory state per WebSocket connection
- History is loaded from DB on first message with `session_id`
- All `chat_stream()` calls include conversation history for context
- Messages are saved to DB after each exchange

**Example Flow:**
```
User: "Schalte das Licht im Wohnzimmer an"
→ Intent: homeassistant.turn_on, entity_id: light.wohnzimmer
→ Response: "Ich habe das Licht eingeschaltet."
→ Saved to DB, history updated

User: "Mach es wieder aus"
→ LLM sees previous exchange in history
→ Understands "es" = light.wohnzimmer
→ Intent: homeassistant.turn_off, entity_id: light.wohnzimmer
→ Response: "Ich habe das Licht ausgeschaltet."
```

**Key Methods** (in `OllamaService`):
- `load_conversation_context(session_id, db, max_messages=20)` - Loads previous messages
- `save_message(session_id, role, content, db, metadata=None)` - Stores single message
- `chat_stream(message, history=None)` - Streaming chat with optional history
- `get_all_conversations(db, limit, offset)` - Lists all conversations
- `search_conversations(query, db, limit)` - Full-text search
- `delete_conversation(session_id, db)` - Deletes conversation with cascade

**API Endpoints:**
- `GET /api/chat/conversations` - List all conversations
- `GET /api/chat/history/{session_id}` - Get full history
- `GET /api/chat/conversation/{session_id}/summary` - Get summary
- `GET /api/chat/search?q=query` - Search in conversations
- `GET /api/chat/stats` - Global statistics
- `DELETE /api/chat/session/{session_id}` - Delete session
- `DELETE /api/chat/conversations/cleanup?days=30` - Cleanup old data

**Documentation:** See `src/backend/CONVERSATION_API.md` for detailed API documentation and usage examples.

### Speaker Recognition System

Renfield includes automatic speaker recognition using **SpeechBrain ECAPA-TDNN**:

**Features:**
- Automatic speaker identification on every voice input (Web & Satellite)
- Auto-discovery of unknown speakers with automatic profile creation
- Continuous learning - recognition improves with each interaction
- Multi-speaker support - unlimited speaker profiles
- Frontend management via `/speakers` page

**Key Implementation:**
- `SpeakerService` (`src/backend/services/speaker_service.py`): SpeechBrain model wrapper
- `WhisperService.transcribe_with_speaker()`: Combines STT with speaker identification
- 192-dimensional voice embeddings stored in PostgreSQL
- Cosine similarity for speaker matching (threshold: 0.25)

**API Endpoints:**
- `GET /api/speakers` - List all speakers with embedding counts
- `POST /api/speakers` - Create new speaker profile
- `POST /api/speakers/{id}/enroll` - Add voice sample
- `POST /api/speakers/identify` - Identify speaker from audio
- `DELETE /api/speakers/{id}` - Delete speaker and embeddings

**Documentation:** See `SPEAKER_RECOGNITION.md` for detailed documentation.

### Device Management System

Renfield supports multiple device types that can connect and interact with the system:

**Device Types:**
- `satellite` - Hardware Raspberry Pi satellites with wake word detection
- `web_panel` - Stationary web panels (e.g., wall-mounted tablets)
- `web_tablet` - Mobile tablets
- `web_browser` - Desktop/mobile browsers
- `web_kiosk` - Kiosk terminals

**Key Features:**
- **Unified WebSocket Endpoint** (`/ws/device`): All web devices connect through this endpoint
- **IP-based Room Detection**: Stationary devices are identified by IP address for automatic room context
- **Capability-based Features**: UI adapts based on device capabilities (microphone, speaker, wake word)
- **Persistent Registration**: Devices are stored in database and survive reconnects

**Key Implementation:**
- `DeviceManager` (`src/backend/services/device_manager.py`): In-memory device state management
- `RoomService` (`src/backend/services/room_service.py`): Database persistence for devices and rooms
- `RoomDevice` model: Stores device info including IP address, capabilities, online status

**Automatic Room Detection:**
When a client connects to `/ws` (chat), the backend checks their IP against registered stationary devices:
```python
room_context = await room_service.get_room_context_by_ip(ip_address)
# Returns: {"room_name": "Kitchen", "room_id": 1, "device_id": "...", "auto_detected": True}
```
This context is passed to intent recognition, allowing commands like "turn on the light" to work without specifying the room.

**API Endpoints:**
- `GET /api/rooms` - List rooms with all devices
- `GET /api/rooms/{id}/devices` - Get devices in a room
- `GET /api/rooms/devices/connected` - Get currently connected devices (real-time)
- `POST /api/rooms/{id}/devices` - Manually register a device
- `DELETE /api/rooms/devices/{device_id}` - Delete a device
- `PATCH /api/rooms/devices/{device_id}/room/{room_id}` - Move device to another room

### Audio Output Routing System

Renfield supports intelligent routing of TTS responses to the best available output device in a room:

**Features:**
- Configurable output devices per room with priority ordering
- Availability checking (on/off, busy/idle)
- Interruption preferences per device
- TTS volume control per device
- Automatic fallback to input device when no output device is available
- Supports Renfield devices (Satellites, Web Panels) and Home Assistant Media Players

**Key Implementation:**
- `OutputRoutingService` (`src/backend/services/output_routing_service.py`): Routing logic and device selection
- `AudioOutputService` (`src/backend/services/audio_output_service.py`): Audio delivery to devices
- `RoomOutputDevice` model: Database persistence for output device configurations
- TTS cache endpoint for HA media players (`/api/voice/tts-cache/{audio_id}`)

**Routing Algorithm:**
```
1. Get all configured output devices for room, sorted by priority
2. For each device (in priority order):
   a. Check availability via HA API / DeviceManager
   b. If available (idle/paused) → use it
   c. If busy AND allow_interruption=True → use it
   d. If busy AND allow_interruption=False → try next
   e. If off/unreachable → try next
3. If no configured device available → fallback to input device
4. If nothing available → no audio output
```

**API Endpoints:**
- `GET /api/rooms/{room_id}/output-devices` - Get configured output devices
- `POST /api/rooms/{room_id}/output-devices` - Add output device
- `PATCH /api/rooms/output-devices/{id}` - Update device settings
- `DELETE /api/rooms/output-devices/{id}` - Remove output device
- `POST /api/rooms/{room_id}/output-devices/reorder` - Reorder priorities
- `GET /api/rooms/{room_id}/available-outputs` - Get available devices (Renfield + HA)

**WebSocket Protocol:**
The `done` message includes a `tts_handled` flag:
```json
{"type": "done", "tts_handled": true}
```
- `tts_handled: true` → TTS was sent to external device, frontend skips local playback
- `tts_handled: false` → Frontend plays TTS locally (as before)

**Documentation:** See `OUTPUT_ROUTING.md` for detailed documentation.

### Authentication & Authorization System (RPBAC)

Renfield implements a **Role-Permission Based Access Control (RPBAC)** system for securing resources:

**Features:**
- JWT-based authentication (access + refresh tokens)
- Flexible role system with granular permissions
- Resource ownership (KnowledgeBase, Conversation)
- KB-level sharing with explicit permissions
- Voice authentication via Speaker Recognition
- **Optional by default** - Set `AUTH_ENABLED=true` to activate

**Permission Hierarchy:**
```
kb.all > kb.shared > kb.own > kb.none
ha.full > ha.control > ha.read > ha.none
cam.full > cam.view > cam.none
```

**Default Roles:**
| Role | Permissions | Use Case |
|------|-------------|----------|
| Admin | Full access to everything | System administrators |
| Familie | ha.full, kb.shared, cam.view | Family members |
| Gast | ha.read, kb.none, cam.none | Guests, limited access |

**Key Implementation:**
- `models/permissions.py`: Permission enum with 22+ granular permissions
- `services/auth_service.py`: JWT handling, password hashing, permission checks
- `api/routes/auth.py`: Login, register, token refresh, voice auth
- `api/routes/roles.py`: Role CRUD with permission management
- `api/routes/users.py`: User management, speaker linking

**Protected Endpoints:**
| Endpoint Group | Permission Required |
|----------------|---------------------|
| `/admin/*`, `/debug/*` | `admin` |
| `/api/homeassistant/states` | `ha.read` |
| `/api/homeassistant/turn_*` | `ha.control` |
| `/api/homeassistant/service` | `ha.full` |
| `/api/camera/events` | `cam.view` |
| `/api/camera/snapshot` | `cam.full` |
| `/api/knowledge/*` | `kb.*` + ownership check |
| `/api/roles/*` | `roles.view` / `roles.manage` |
| `/api/users/*` | `users.view` / `users.manage` |

**Configuration:**
```bash
AUTH_ENABLED=true                    # Enable authentication
ACCESS_TOKEN_EXPIRE_MINUTES=1440     # 24 hours
REFRESH_TOKEN_EXPIRE_DAYS=30
PASSWORD_MIN_LENGTH=8
VOICE_AUTH_ENABLED=true              # Enable voice login
VOICE_AUTH_MIN_CONFIDENCE=0.7
```

**Documentation:** See `ACCESS_CONTROL.md` for detailed documentation.

### Backend Structure

```
src/backend/
├── main.py                    # FastAPI app, WebSocket endpoints, lifecycle management
├── Dockerfile                 # CPU-only image
├── Dockerfile.gpu             # NVIDIA CUDA image for GPU acceleration
├── api/routes/                # REST API endpoints
│   ├── auth.py               # Authentication (login, register, token refresh, voice auth)
│   ├── roles.py              # Role management (CRUD, permissions)
│   ├── users.py              # User management (CRUD, speaker linking)
│   ├── chat.py               # Chat history, non-streaming chat
│   ├── voice.py              # STT, TTS, voice-chat endpoint
│   ├── speakers.py           # Speaker recognition management
│   ├── rooms.py              # Room and device management, HA sync
│   ├── homeassistant.py      # HA state queries, control endpoints (permission-protected)
│   ├── camera.py             # Frigate events, snapshots (permission-protected)
│   ├── knowledge.py          # Knowledge base management (ownership + sharing)
│   └── tasks.py              # Task queue management
├── services/                  # Business logic layer
│   ├── auth_service.py       # JWT tokens, password hashing, permission checks
│   ├── ollama_service.py     # LLM interaction, intent extraction (with room context)
│   ├── whisper_service.py    # Speech-to-text (with speaker recognition)
│   ├── speaker_service.py    # Speaker recognition (SpeechBrain ECAPA-TDNN)
│   ├── audio_preprocessor.py # Noise reduction, normalization for STT
│   ├── piper_service.py      # Text-to-speech
│   ├── satellite_manager.py  # Satellite session management
│   ├── device_manager.py     # Web device session management
│   ├── room_service.py       # Room and device CRUD, HA area sync
│   ├── output_routing_service.py  # Audio/visual output device routing
│   ├── audio_output_service.py    # TTS delivery to output devices
│   ├── action_executor.py    # Routes intents to appropriate integrations
│   ├── task_queue.py         # Redis-based task queue
│   └── database.py           # SQLAlchemy setup, init_db()
├── integrations/              # External service clients
│   ├── homeassistant.py      # Home Assistant REST API client (with area API)
│   ├── frigate.py            # Frigate API client
│   ├── n8n.py                # n8n webhook trigger client
│   └── plugins/              # YAML-based plugin system
├── models/                    # SQLAlchemy ORM models
│   ├── database.py           # Room, RoomDevice, User, Role, KBPermission models
│   └── permissions.py        # Permission enum and hierarchy
└── utils/
    └── config.py             # Pydantic settings (loads from .env)
```

### Satellite Structure

```
src/satellite/
├── README.md                  # Full satellite documentation
├── renfield_satellite/
│   ├── __init__.py
│   ├── __main__.py           # Entry point
│   ├── config.py             # Configuration loading
│   ├── satellite.py          # Main Satellite class, state machine
│   ├── audio/
│   │   ├── capture.py        # Microphone capture (PyAudio/ALSA)
│   │   └── playback.py       # Speaker output (mpv)
│   ├── wakeword/
│   │   └── detector.py       # OpenWakeWord wrapper
│   ├── hardware/
│   │   ├── led.py            # APA102 RGB LED control
│   │   └── button.py         # GPIO button
│   ├── network/
│   │   └── websocket_client.py
│   └── update/
│       └── update_manager.py # OTA update handling
└── config/
    └── satellite.yaml        # Example configuration
```

### Satellite OTA Updates

Satellites support Over-the-Air (OTA) updates via the Web-UI. See `docs/SATELLITE_OTA_UPDATES.md` for full documentation.

**Key Components:**
- `src/backend/services/satellite_update_service.py` - Backend update service
- `src/satellite/renfield_satellite/update/update_manager.py` - Satellite update manager
- `src/frontend/src/pages/SatellitesPage.jsx` - Update UI

**Configuration:**
```bash
# .env
SATELLITE_LATEST_VERSION=1.1.0
```

**Manual Deployment:**
```bash
./bin/deploy-satellite.sh [hostname] [user]
```

### Frontend Structure

```
src/frontend/
├── src/
│   ├── main.jsx              # React entry point
│   ├── App.jsx               # Router setup, main layout
│   ├── pages/                # Route components (JSX, gradual TS migration)
│   │   ├── HomePage.jsx      # Dashboard/landing
│   │   ├── ChatPage.jsx      # Chat interface with WebSocket, voice controls, sidebar
│   │   ├── SpeakersPage.jsx  # Speaker management and enrollment
│   │   ├── RoomsPage.jsx     # Room management with device list, HA sync
│   │   ├── HomeAssistantPage.jsx # Device browser and controls
│   │   ├── CameraPage.jsx    # Frigate events viewer
│   │   └── TasksPage.jsx     # Task queue viewer
│   ├── components/           # React components (JSX, gradual TS migration)
│   │   ├── Layout.jsx        # Navigation, responsive layout, ThemeToggle
│   │   ├── ThemeToggle.jsx   # Dark/Light/System theme dropdown
│   │   ├── ChatSidebar.jsx   # Conversation history sidebar with date grouping
│   │   ├── ConversationItem.jsx # Single conversation row in sidebar
│   │   ├── DeviceSetup.jsx   # Device registration modal
│   │   └── DeviceStatus.jsx  # Device/room status indicator for navbar
│   ├── context/              # React contexts (TypeScript)
│   │   ├── AuthContext.tsx   # Authentication state and JWT handling
│   │   ├── DeviceContext.tsx # App-wide device connection state
│   │   └── ThemeContext.tsx  # Dark Mode state (light/dark/system)
│   ├── hooks/                # Custom hooks (TypeScript)
│   │   ├── useDeviceConnection.ts  # WebSocket connection to /ws/device
│   │   ├── useChatSessions.ts      # Conversation list management and API
│   │   ├── useCapabilities.tsx     # Capability-based feature toggles
│   │   └── useWakeWord.ts          # Wake word detection (OpenWakeWord WASM)
│   ├── types/                # TypeScript type definitions
│   │   ├── index.ts          # Barrel export
│   │   ├── device.ts         # Device, WebSocket, Capabilities types
│   │   ├── chat.ts           # Chat, Conversation, Message types
│   │   └── api.ts            # API response types (Room, Speaker, Auth)
│   ├── config/               # Configuration (TypeScript)
│   │   └── wakeword.ts       # Wake word settings and keywords
│   ├── i18n/                 # Internationalization
│   │   ├── index.js          # i18next configuration
│   │   └── locales/
│   │       ├── de.json       # German translations (~400 keys)
│   │       └── en.json       # English translations (~400 keys)
│   └── utils/                # Utility functions (TypeScript)
│       ├── axios.ts          # Axios instance with base URL config
│       └── debug.ts          # Debug logger (dev-only)
├── tsconfig.json             # TypeScript config (permissive, allowJs)
├── tsconfig.node.json        # TypeScript config for Vite
├── Dockerfile
├── package.json
└── vite.config.ts            # Vite config with path aliases
```

### Frontend Connection Architecture

The frontend uses **two independent WebSocket connections**:

| Connection | Endpoint | Purpose | State Variable | Status Display |
|------------|----------|---------|----------------|----------------|
| **Chat WS** | `/ws` | Send/receive chat messages | `wsConnected` in ChatPage | "Verbunden/Getrennt" in chat window |
| **Device WS** | `/ws/device` | Device registration, room assignment, capabilities | `device.isConnected` in DeviceContext | Header status (room name or "Offline") |

**Key Points:**
- These connections are **completely independent** - the chat can work without device registration
- `DeviceStatus` component (header) shows device registration status, not chat connection
- `ChatPage` shows chat WebSocket status in the message area footer
- Users can chat without registering their device, but won't have room context for commands

**Features by Connection:**

| Feature | Chat WS Only | With Device Registration |
|---------|-------------|--------------------------|
| Send/receive messages | ✓ | ✓ |
| Room context for "turn on the light" | ✗ | ✓ |
| Device capabilities (mic, speaker) | ✗ | ✓ |
| Persistent device identity | ✗ | ✓ |

### WebSocket Protocol

#### Frontend Chat (`/ws`)

**Features:**
- Automatic room detection via IP address for registered stationary devices
- Room context passed to intent recognition
- **Conversation persistence** via `session_id` field for follow-up questions

**Client → Server:**
```json
{
  "type": "text",
  "content": "Schalte das Licht im Wohnzimmer ein",
  "session_id": "session-1234567890-abc123def",  // Optional: enables conversation persistence
  "use_rag": false,                               // Optional: enable RAG context
  "knowledge_base_id": null                       // Optional: specific knowledge base
}
```

**Server → Client (streaming):**
```json
{"type": "action", "intent": {...}, "result": {...}}  // Action executed
{"type": "stream", "content": "Ich habe..."}          // Response chunks
{"type": "stream", "content": " das Licht..."}
{"type": "done", "tts_handled": false}                // End of stream
```

**Conversation Persistence:**
When `session_id` is provided:
1. On first message: History is loaded from DB (up to 10 messages)
2. After each exchange: User message and assistant response are saved to DB
3. All LLM calls include conversation history for context understanding

#### Web Devices (`/ws/device`)

**Client → Server:**
```json
// Registration
{
  "type": "register",
  "device_id": "web-123-abc",
  "device_type": "web_panel",
  "room": "Kitchen",
  "device_name": "Kitchen iPad",
  "is_stationary": true,
  "capabilities": {"has_microphone": true, "has_speaker": true, "has_wakeword": true}
}

// Text message
{"type": "text", "content": "Turn on the lights", "session_id": "..."}

// Audio streaming
{"type": "audio", "chunk": "<base64 PCM>", "sequence": 1, "session_id": "..."}
{"type": "audio_end", "session_id": "...", "reason": "silence"}

// Heartbeat
{"type": "heartbeat", "status": "idle"}
```

**Server → Client:**
```json
// Registration ack
{"type": "register_ack", "success": true, "device_id": "...", "room_id": 1, "capabilities": {...}}

// State change
{"type": "state", "state": "idle|listening|processing|speaking"}

// Transcription
{"type": "transcription", "text": "Turn on the lights", "session_id": "..."}

// Response
{"type": "response_text", "text": "I turned on the kitchen light", "session_id": "..."}
{"type": "tts_audio", "audio": "<base64 WAV>", "is_final": true, "session_id": "..."}

// Session end
{"type": "session_end", "session_id": "...", "reason": "complete"}
```

#### Satellite (`/ws/satellite`)

**Features:**
- Daily conversation persistence for follow-up commands
- Session ID format: `satellite-{satellite_id}-{YYYY-MM-DD}`
- Shorter history (5 messages) optimized for voice commands

**Satellite → Server:**
```json
// Registration
{"type": "register", "satellite_id": "sat-living", "room": "Living Room", "capabilities": {...}}

// Wake word detected
{"type": "wakeword_detected", "keyword": "alexa", "confidence": 0.85, "session_id": "sat-living-abc123"}

// Audio streaming
{"type": "audio", "chunk": "<base64 PCM>", "sequence": 1, "session_id": "..."}

// End of speech
{"type": "audio_end", "session_id": "...", "reason": "silence"}

// Heartbeat
{"type": "heartbeat", "status": "idle"}
```

**Server → Satellite:**
```json
// Registration ack
{"type": "register_ack", "success": true, "config": {"wake_words": ["alexa"], "threshold": 0.5}}

// State change
{"type": "state", "state": "listening|processing|speaking|idle"}

// Transcription result
{"type": "transcription", "session_id": "...", "text": "Turn on the lights"}

// TTS audio response
{"type": "tts_audio", "session_id": "...", "audio": "<base64 WAV>", "is_final": true}
```

**Conversation Persistence:**
Satellites automatically maintain daily conversation context:
- History is loaded from DB on first command of the day
- Each command/response pair is saved with metadata (satellite_id, room, speaker)
- Enables follow-up commands like "Mach es aus" after "Schalte das Licht an"

### Key Configuration

All configuration is in `.env` and loaded via `src/backend/utils/config.py` using Pydantic Settings:

- `DATABASE_URL` - PostgreSQL connection string
- `REDIS_URL` - Redis connection
- `OLLAMA_URL` - Ollama API endpoint (usually `http://ollama:11434`)
- `OLLAMA_MODEL` - Model name (default: `llama3.2:3b`)
- `HOME_ASSISTANT_URL` - HA instance URL
- `HOME_ASSISTANT_TOKEN` - Long-lived access token from HA
- `FRIGATE_URL` - Frigate NVR URL
- `N8N_WEBHOOK_URL` - n8n webhook endpoint
- `WHISPER_MODEL` - Whisper model size (base/small/medium/large)
- `PIPER_VOICE` - Piper voice model (default: `de_DE-thorsten-high`)
- `WAKE_WORD_DEFAULT` - Default wake word for satellites (default: `alexa`)
- `WAKE_WORD_THRESHOLD` - Wake word detection threshold (default: `0.5`)
- `ADVERTISE_HOST` - Hostname/IP for Zeroconf and TTS URL (required for HA media player output)
- `ADVERTISE_PORT` - Port for advertise host (default: `8000`)
- `SPEAKER_RECOGNITION_ENABLED` - Enable speaker recognition (default: `true`)
- `SPEAKER_RECOGNITION_THRESHOLD` - Similarity threshold for identification (default: `0.25`)
- `SPEAKER_AUTO_ENROLL` - Auto-create profiles for unknown speakers (default: `true`)
- `SPEAKER_CONTINUOUS_LEARNING` - Add embeddings on each interaction (default: `true`)

## Common Development Patterns

### Adding a New Integration

1. Create client in `src/backend/integrations/your_service.py`:
   ```python
   class YourServiceClient:
       async def do_something(self):
           # Implementation
   ```

2. Add intent handling in `src/backend/services/action_executor.py`:
   ```python
   elif intent.startswith("yourservice."):
       return await self._execute_yourservice(intent, parameters)
   ```

3. Update intent recognition prompt in `src/backend/services/ollama_service.py` to include new intent types

4. Create API route in `src/backend/api/routes/yourservice.py`

5. Register route in `src/backend/main.py`:
   ```python
   app.include_router(yourservice.router, prefix="/api/yourservice", tags=["YourService"])
   ```

### Adding a New Frontend Page

1. Create page component in `src/frontend/src/pages/YourPage.jsx`

2. Add route in `src/frontend/src/App.jsx`:
   ```jsx
   <Route path="/your-page" element={<YourPage />} />
   ```

3. Add navigation link in `src/frontend/src/components/Layout.jsx`

### Dark Mode Styling

All frontend components must support both light and dark mode using Tailwind CSS classes.

**Pattern**: Use light-first with `dark:` variants:
```jsx
// Background colors
className="bg-gray-50 dark:bg-gray-900"       // Page backgrounds
className="bg-white dark:bg-gray-800"         // Cards, modals
className="bg-gray-100 dark:bg-gray-700"      // Input backgrounds, hover states

// Text colors
className="text-gray-900 dark:text-white"     // Primary text
className="text-gray-600 dark:text-gray-300"  // Secondary text
className="text-gray-500 dark:text-gray-400"  // Muted text

// Borders
className="border-gray-200 dark:border-gray-700"  // Standard borders
className="border-gray-300 dark:border-gray-600"  // Input borders

// Buttons - use component classes from index.css
className="btn btn-primary"                   // Primary action
className="btn btn-secondary"                 // Secondary action (auto dark support)
```

**Component Classes** (defined in `src/frontend/src/index.css`):
- `.card` - Cards with proper light/dark backgrounds and borders
- `.input` - Input fields with focus states
- `.btn-primary` - Primary buttons
- `.btn-secondary` - Secondary buttons with dark mode support

**Configuration** (`tailwind.config.js`):
```javascript
darkMode: ['selector', '[class~="dark"]']
```

**Theme Context** (`ThemeContext.jsx`):
- `useTheme()` hook provides `theme`, `isDark`, `setTheme`, `toggleTheme`
- Theme persisted in localStorage as `renfield_theme`
- Values: `'light'`, `'dark'`, `'system'`

### Internationalization (i18n)

All frontend text must be internationalized using react-i18next. **Never hardcode user-facing strings.**

**Pattern**: Use `useTranslation` hook:
```jsx
import { useTranslation } from 'react-i18next';

function MyComponent() {
  const { t } = useTranslation();

  return (
    <div>
      <h1>{t('myFeature.title')}</h1>
      <button>{t('common.save')}</button>
    </div>
  );
}
```

**With variables (interpolation):**
```jsx
// In de.json: "deleteConfirm": "Möchtest du \"{{name}}\" wirklich löschen?"
t('users.deleteConfirm', { name: user.username })
```

**Localized date/time formatting:**
```jsx
const { i18n } = useTranslation();
new Date().toLocaleString(i18n.language);
// DE: "24.01.2026, 14:30:45"
// EN: "1/24/2026, 2:30:45 PM"
```

**Adding translations:**
1. Add key to `src/frontend/src/i18n/locales/de.json`
2. Add key to `src/frontend/src/i18n/locales/en.json`
3. Use `t('namespace.key')` in component

**Translation structure:**
```json
{
  "common": { "save": "Speichern", "cancel": "Abbrechen" },
  "nav": { "chat": "Chat", "settings": "Einstellungen" },
  "myFeature": { "title": "Mein Feature", "description": "..." }
}
```

**Documentation:** See `docs/MULTILANGUAGE.md` for complete i18n guide.

### Debugging Intent Recognition

Use the built-in debug endpoint:

```bash
curl -X POST "http://localhost:8000/debug/intent?message=Schalte das Licht ein"
```

This returns the extracted intent JSON without executing actions.

### Refreshing Home Assistant Keywords

After adding new devices in Home Assistant:

```bash
curl -X POST "http://localhost:8000/admin/refresh-keywords"
```

This reloads the dynamic keyword cache used for intent recognition.

## Testing

Tests are organized by component in the `tests/` directory at project root. The backend test suite covers **450+ tests** across all API routes and services.

```
tests/
├── conftest.py              # Shared fixtures for all tests
├── backend/                 # Backend-specific tests (450+ tests)
│   ├── conftest.py          # Backend fixtures (DB, async client, mocks)
│   │
│   │   # API Route Tests
│   ├── test_chat.py         # Chat API, conversations, search, stats
│   ├── test_voice.py        # STT, TTS, voice-chat endpoints
│   ├── test_speakers.py     # Speaker CRUD, enrollment, identification
│   ├── test_users.py        # User CRUD, password reset, speaker linking
│   ├── test_homeassistant.py # HA states, device control, services
│   ├── test_camera.py       # Frigate events, snapshots, permissions
│   ├── test_tasks.py        # Task CRUD, status updates, filtering
│   ├── test_settings.py     # Wakeword configuration, service status
│   ├── test_api_rooms.py    # Room management API endpoints
│   │
│   │   # Service Tests
│   ├── test_services.py     # OllamaService, RAGService, SpeakerService,
│   │                        # ActionExecutor, AudioPreprocessor,
│   │                        # DeviceManager, RoomService
│   ├── test_room_service.py # RoomService detailed tests
│   ├── test_action_executor.py # Intent execution tests
│   │
│   │   # Auth & Permissions
│   ├── test_auth.py         # JWT tokens, password hashing, RBAC,
│   │                        # permission hierarchy, role management
│   │
│   │   # Infrastructure
│   ├── test_models.py       # Database model tests
│   ├── test_websocket.py    # WebSocket protocol, rate limiting
│   ├── test_integrations.py # HA, Frigate, n8n client tests
│   └── test_utils.py        # Utility function tests
│
├── frontend/                # Frontend-specific tests
│   ├── conftest.py
│   ├── test_api_contracts.py # API contract validation (Python)
│   └── react/               # React component tests (Vitest) - separate from production
│       ├── package.json     # Isolated test dependencies (security)
│       ├── vitest.config.js # Vitest configuration
│       ├── setup.js         # Test setup with MSW
│       ├── test-utils.jsx   # Render helpers, mock providers
│       ├── config.js        # Configurable API base URL
│       ├── mocks/           # MSW handlers
│       ├── context/         # Context tests (AuthContext)
│       └── pages/           # Page component tests
│
├── satellite/               # Satellite-specific tests
│   ├── conftest.py
│   └── test_satellite.py    # Satellite functionality tests
│
├── integration/             # Cross-component E2E tests
│   ├── conftest.py
│   ├── test_e2e_scenarios.py
│   └── test_component_communication.py
│
└── manual/                  # Manual test scripts
    ├── test_media_player.py      # Media player intent testing
    └── test_ollama_connection.sh # Ollama connection verification
```

### Backend Test Coverage

| Test File | Coverage |
|-----------|----------|
| `test_chat.py` | Chat API, conversation CRUD, history, search, stats, cleanup |
| `test_voice.py` | STT endpoint, TTS endpoint, TTS cache, voice-chat flow |
| `test_speakers.py` | Speaker CRUD, enrollment, identification, verification, merge |
| `test_users.py` | User CRUD, role assignment, password reset, speaker linking |
| `test_homeassistant.py` | States, turn_on/off/toggle, service calls, permissions |
| `test_camera.py` | Events, cameras list, snapshots, latest by label |
| `test_tasks.py` | Task CRUD, status updates, filtering, queries |
| `test_settings.py` | Wakeword settings, service status, server fallback |
| `test_services.py` | OllamaService, RAGService, SpeakerService, DeviceManager |
| `test_auth.py` | JWT, passwords, permissions, roles, RBAC hierarchy |
| `test_websocket.py` | Protocol parsing, device registration, rate limiting |

### Running Tests

**Recommended: Use Makefile commands** (runs tests in Docker with correct environment):

```bash
make test             # Run all tests
make test-backend     # Run backend tests only
make test-frontend    # Run frontend API contract tests
make test-unit        # Run only unit tests
make test-coverage    # Run with coverage report
```

**React Component Tests (Vitest):**

React component tests use Vitest with React Testing Library and MSW for API mocking.
Tests have their own `package.json` in `tests/frontend/react/` to ensure complete separation
from production dependencies (security best practice).

```bash
# From tests/frontend/react directory
cd tests/frontend/react
npm install           # Install test dependencies (first time only)
npm test              # Run all React tests
npm test -- --run     # Run once without watch mode

# Or using make from project root
make test-frontend-react

# With custom API base URL (for different environments)
VITE_API_URL=http://localhost:3000 npm test -- --run
```

The tests are located in `tests/frontend/react/` and can be configured via:
- `VITE_API_URL` environment variable - Sets the API base URL for mock handlers (default: `http://localhost:8000`)

**Manual Docker execution:**

```bash
# Run all tests in Docker
docker compose exec -T -e PYTHONPATH=/app backend pytest /tests/ -v

# Run only backend tests
docker compose exec -T -e PYTHONPATH=/app backend pytest /tests/backend/ -v

# Run by marker
docker compose exec -T -e PYTHONPATH=/app backend pytest /tests/ -m unit -v
```

**Local execution** (requires local Python environment with dependencies):

```bash
# Run all tests from project root
pytest tests/ -v

# Run only backend tests
pytest tests/backend/ -v

# Run with coverage
pytest tests/backend/ --cov=src/backend --cov-report=html

# Run specific test file
pytest tests/backend/test_room_service.py -v
```

### Test Markers

- `@pytest.mark.unit` - Fast, isolated unit tests
- `@pytest.mark.database` - Tests requiring database fixtures
- `@pytest.mark.integration` - Integration tests with external services
- `@pytest.mark.e2e` - Full end-to-end scenarios
- `@pytest.mark.backend` - Backend-specific tests
- `@pytest.mark.frontend` - Frontend-specific tests
- `@pytest.mark.satellite` - Satellite-specific tests

### Manual Tests

Manual test scripts for interactive testing against running services:

```bash
# Test Ollama connection and model availability
./tests/manual/test_ollama_connection.sh

# Test media player intent extraction (requires running backend)
python tests/manual/test_media_player.py
```

## CI/CD Pipeline

The project uses GitHub Actions for continuous integration and deployment.

### Workflows

| Workflow | Trigger | Description |
|----------|---------|-------------|
| `ci.yml` | Push to main/develop, PRs | Full CI pipeline: lint, test, build |
| `pr-check.yml` | Pull requests | Quick checks for PRs |
| `release.yml` | Tag push (v*.*.*) | Build and push Docker images to GHCR |

### CI Pipeline

The CI pipeline runs on every push and PR:

1. **Backend Tests** - Python tests with PostgreSQL and Redis
2. **Frontend Tests** - JavaScript linting and build
3. **Integration Tests** - Cross-component tests
4. **Docker Build** - Verify Docker images build correctly
5. **Security Scan** - Check for vulnerabilities

### Creating a Release

```bash
# Create and push a version tag
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin v1.0.0

# Or use make
make release
```

This triggers the release workflow which:
- Builds multi-platform Docker images (amd64, arm64)
- Pushes images to GitHub Container Registry
- Creates a GitHub Release with changelog

### Docker Images

Released images are available at:
- `ghcr.io/<owner>/renfield/backend:latest`
- `ghcr.io/<owner>/renfield/frontend:latest`
- `ghcr.io/<owner>/renfield/backend:v1.0.0-gpu` (GPU version)

## Deployment Notes

- The system is designed to run fully offline once all models are downloaded
- First startup takes 5-10 minutes to download Ollama and Whisper models
- Whisper models are cached in Docker volume `whisper_models`
- Ollama models are cached in Docker volume `ollama_data`
- For production with GPU, use `docker-compose.prod.yml`
- For development on Mac, use `docker-compose.dev.yml`

### GPU Support

For NVIDIA GPU acceleration (faster Whisper transcription):

1. Install NVIDIA Container Toolkit on the host
2. Use `docker compose -f docker-compose.prod.yml up -d`
3. The backend will automatically use GPU for Whisper

## Common Issues

### Intent Recognition Problems

1. Check if Home Assistant keywords are loaded:
   ```bash
   curl http://localhost:8000/admin/refresh-keywords
   ```

2. Test intent extraction directly:
   ```bash
   curl -X POST "http://localhost:8000/debug/intent?message=YOUR_MESSAGE"
   ```

3. Verify Ollama model is loaded:
   ```bash
   docker exec -it renfield-ollama ollama list
   ```

### WebSocket Connection Failures

- Check CORS settings in `src/backend/main.py`
- Verify frontend `VITE_WS_URL` matches backend WebSocket endpoint
- Check backend logs: `docker compose logs -f backend`

### Voice Input Not Working

- Whisper model loads lazily on first use (check logs)
- Ensure audio file format is supported (WAV, MP3, OGG)
- Check backend logs for transcription errors

### Home Assistant Integration

- Verify token is valid (test in HA Developer Tools → Services)
- Check network connectivity between containers
- Ensure HA URL is accessible from Docker network
- Use `http://homeassistant.local:8123` or IP address, not `localhost`

### Satellite Issues

- **Satellite not finding backend**: Check Zeroconf advertisement with `docker compose logs backend | grep zeroconf`
- **Wrong microphone**: Ensure `.asoundrc` is configured for ReSpeaker (see satellite README)
- **Garbled transcription**: PyAudio must be installed (not soundcard) for ALSA support
- **GPIO errors**: Add user to gpio group with `sudo usermod -aG gpio $USER`

## Project Structure

```
renfield/
├── src/                       # Source code
│   ├── backend/               # Python FastAPI backend
│   ├── frontend/              # React frontend
│   └── satellite/             # Raspberry Pi satellite code
├── tests/                     # Test suite
│   ├── backend/               # Backend unit tests (pytest)
│   ├── frontend/              # Frontend tests
│   │   ├── test_api_contracts.py  # API contract tests (pytest)
│   │   └── react/             # React component tests (vitest)
│   ├── satellite/             # Satellite tests
│   ├── integration/           # Cross-component E2E tests
│   └── manual/                # Manual test scripts
├── bin/                       # Shell scripts
│   ├── start.sh               # Start all services
│   ├── update.sh              # Update system with backup
│   ├── quick-update.sh        # Quick backend restart
│   ├── debug.sh               # Debug info and logs
│   └── deploy.sh              # Deploy to GitHub
├── .github/workflows/         # CI/CD pipelines
│   ├── ci.yml                 # Main CI pipeline
│   ├── pr-check.yml           # PR validation
│   └── release.yml            # Release and Docker push
├── config/                    # Configuration files
│   └── nginx.conf             # Nginx config for production
├── docs/                      # Additional documentation
├── Makefile                   # Task orchestration
├── docker-compose.yml         # Standard Docker setup
├── docker-compose.dev.yml     # Development setup (Mac)
├── docker-compose.prod.yml    # Production setup (GPU)
└── pytest.ini                 # Test configuration
```

## Project Documentation

Additional documentation files in the repository:

- `README.md` - Main user documentation
- `PROJECT_OVERVIEW.md` - High-level architecture
- `QUICKSTART.md` - Quick setup guide
- `INSTALLATION.md` - Detailed installation guide
- `FEATURES.md` - Feature documentation
- `EXTERNAL_OLLAMA.md` - External Ollama instance setup
- `SPEAKER_RECOGNITION.md` - Speaker recognition system documentation
- `OUTPUT_ROUTING.md` - Audio output device routing system documentation
- `docs/MULTILANGUAGE.md` - Multi-language support (i18n) documentation
- `src/satellite/README.md` - Satellite setup guide
- `src/backend/integrations/plugins/README.md` - Plugin development guide
- `docs/ENVIRONMENT_VARIABLES.md` - Environment variable reference
- `docs/SECURITY.md` - Security headers, OWASP testing, vulnerability management
