# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Renfield is a fully offline-capable, self-hosted AI assistant for smart home control, camera monitoring, and workflow automation. It uses local LLMs (Ollama), speech-to-text (Whisper), text-to-speech (Piper), and integrates with Home Assistant, Frigate, and n8n.

**Tech Stack:**
- Backend: Python 3.11 + FastAPI + SQLAlchemy
- Frontend: React 18 + Vite + Tailwind CSS + PWA
- Infrastructure: Docker Compose, PostgreSQL 16, Redis 7, Ollama
- Integrations: Home Assistant, Frigate (camera NVR), n8n (workflows)
- Satellites: Raspberry Pi Zero 2 W + ReSpeaker 2-Mics Pi HAT + OpenWakeWord

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
make test-frontend    # Run frontend tests only
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

### Backend Structure

```
src/backend/
├── main.py                    # FastAPI app, WebSocket endpoints, lifecycle management
├── Dockerfile                 # CPU-only image
├── Dockerfile.gpu             # NVIDIA CUDA image for GPU acceleration
├── api/routes/                # REST API endpoints
│   ├── chat.py               # Chat history, non-streaming chat
│   ├── voice.py              # STT, TTS, voice-chat endpoint
│   ├── speakers.py           # Speaker recognition management
│   ├── rooms.py              # Room and device management, HA sync
│   ├── homeassistant.py      # HA state queries, control endpoints
│   ├── camera.py             # Frigate events, snapshots
│   └── tasks.py              # Task queue management
├── services/                  # Business logic layer
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
│   └── database.py           # Room, RoomDevice, and other models
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
│   └── network/
│       └── websocket_client.py
└── config/
    └── satellite.yaml        # Example configuration
```

### Frontend Structure

```
src/frontend/
├── src/
│   ├── main.jsx              # React entry point
│   ├── App.jsx               # Router setup, main layout
│   ├── pages/                # Route components
│   │   ├── HomePage.jsx      # Dashboard/landing
│   │   ├── ChatPage.jsx      # Chat interface with WebSocket, voice controls
│   │   ├── SpeakersPage.jsx  # Speaker management and enrollment
│   │   ├── RoomsPage.jsx     # Room management with device list, HA sync
│   │   ├── HomeAssistantPage.jsx # Device browser and controls
│   │   ├── CameraPage.jsx    # Frigate events viewer
│   │   └── TasksPage.jsx     # Task queue viewer
│   ├── components/
│   │   ├── Layout.jsx        # Navigation, responsive layout
│   │   ├── DeviceSetup.jsx   # Device registration modal
│   │   └── DeviceStatus.jsx  # Device/room status indicator for navbar
│   ├── context/
│   │   └── DeviceContext.jsx # App-wide device connection state
│   ├── hooks/
│   │   ├── useDeviceConnection.js  # WebSocket connection to /ws/device
│   │   └── useCapabilities.jsx     # Capability-based feature toggles
│   └── utils/
│       └── axios.js          # Axios instance with base URL config
├── Dockerfile
├── package.json
└── vite.config.js
```

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

Tests are organized by component in the `tests/` directory at project root:

```
tests/
├── conftest.py              # Shared fixtures for all tests
├── backend/                 # Backend-specific tests
│   ├── conftest.py          # Backend fixtures (DB, mocks)
│   ├── test_models.py       # Database model tests
│   ├── test_room_service.py # RoomService tests
│   ├── test_action_executor.py
│   ├── test_api_rooms.py    # API endpoint tests
│   ├── test_integrations.py # HA, Frigate, n8n client tests
│   ├── test_websocket.py    # WebSocket protocol tests
│   └── test_utils.py        # Utility function tests
├── frontend/                # Frontend-specific tests
│   ├── conftest.py
│   └── test_api_contracts.py # API contract validation
├── satellite/               # Satellite-specific tests
│   ├── conftest.py
│   └── test_satellite.py    # Satellite functionality tests
├── integration/             # Cross-component E2E tests
│   ├── conftest.py
│   ├── test_e2e_scenarios.py
│   └── test_component_communication.py
└── manual/                  # Manual test scripts
    ├── test_media_player.py      # Media player intent testing
    └── test_ollama_connection.sh # Ollama connection verification
```

### Running Tests

**Recommended: Use Makefile commands** (runs tests in Docker with correct environment):

```bash
make test             # Run all tests
make test-backend     # Run backend tests only
make test-frontend    # Run frontend API contract tests
make test-unit        # Run only unit tests
make test-coverage    # Run with coverage report
```

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
│   ├── backend/               # Backend unit tests
│   ├── frontend/              # Frontend API contract tests
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
- `src/satellite/README.md` - Satellite setup guide
- `src/backend/integrations/plugins/README.md` - Plugin development guide
- `docs/ENVIRONMENT_VARIABLES.md` - Environment variable reference
