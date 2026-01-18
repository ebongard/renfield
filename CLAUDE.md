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
./start.sh

# Update system
./update.sh

# Debug mode with detailed logging
./debug.sh
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
cd backend

# Install dependencies
pip install -r requirements.txt

# Run dev server with hot reload
uvicorn main:app --reload --host 0.0.0.0 --port 8000

# Run single test
pytest tests/test_intent.py -v

# Run all tests
pytest

# View API docs
open http://localhost:8000/docs
```

### Frontend Development
```bash
cd frontend

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

**Dokumentation:** Siehe [EXTERNAL_OLLAMA.md](EXTERNAL_OLLAMA.md) für vollständige Anleitung zur Nutzung externer Ollama-Instanzen.

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

The core of Renfield is the intent recognition system in `backend/services/ollama_service.py`:

1. **extract_intent()**: Uses Ollama LLM to parse natural language into structured intents
2. **Dynamic Keyword Matching**: Fetches device names from Home Assistant to improve accuracy
3. **Intent Types**:
   - `homeassistant.*` - Smart home control (turn_on, turn_off, get_state, etc.)
   - `n8n.*` - Workflow triggers
   - `camera.*` - Camera/Frigate actions
   - `general.conversation` - Normal chat (no action needed)

**Key Implementation Detail**: The system pre-loads Home Assistant entity names and friendly names as "keywords" to determine if a user query is smart-home related. See `HomeAssistantClient.get_keywords()` for the dynamic keyword extraction logic.

### Conversation Persistence

Renfield implements full conversation persistence with PostgreSQL:

**Features:**
- Automatic message storage for all user/assistant interactions
- Context loading (last N messages) for conversation continuity
- Full-text search across all conversations
- Conversation statistics and analytics
- Automatic cleanup of old conversations

**Key Methods** (in `OllamaService`):
- `load_conversation_context(session_id, db, max_messages=20)` - Loads previous messages
- `save_message(session_id, role, content, db, metadata=None)` - Stores single message
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

**Documentation:** See `backend/CONVERSATION_API.md` for detailed API documentation and usage examples.

### Backend Structure

```
backend/
├── main.py                    # FastAPI app, WebSocket endpoints, lifecycle management
├── Dockerfile                 # CPU-only image
├── Dockerfile.gpu             # NVIDIA CUDA image for GPU acceleration
├── api/routes/                # REST API endpoints
│   ├── chat.py               # Chat history, non-streaming chat
│   ├── voice.py              # STT, TTS, voice-chat endpoint
│   ├── homeassistant.py      # HA state queries, control endpoints
│   ├── camera.py             # Frigate events, snapshots
│   └── tasks.py              # Task queue management
├── services/                  # Business logic layer
│   ├── ollama_service.py     # LLM interaction, intent extraction
│   ├── whisper_service.py    # Speech-to-text (with GPU support)
│   ├── piper_service.py      # Text-to-speech
│   ├── satellite_manager.py  # Satellite session management
│   ├── action_executor.py    # Routes intents to appropriate integrations
│   ├── task_queue.py         # Redis-based task queue
│   └── database.py           # SQLAlchemy setup, init_db()
├── integrations/              # External service clients
│   ├── homeassistant.py      # Home Assistant REST API client
│   ├── frigate.py            # Frigate API client
│   ├── n8n.py                # n8n webhook trigger client
│   └── plugins/              # YAML-based plugin system
├── models/                    # SQLAlchemy ORM models
│   └── database.py
└── utils/
    └── config.py             # Pydantic settings (loads from .env)
```

### Satellite Structure

```
renfield-satellite/
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
frontend/src/
├── main.jsx                  # React entry point
├── App.jsx                   # Router setup, main layout
├── pages/                    # Route components
│   ├── HomePage.jsx          # Dashboard/landing
│   ├── ChatPage.jsx          # Chat interface with WebSocket, voice controls
│   ├── HomeAssistantPage.jsx # Device browser and controls
│   ├── CameraPage.jsx        # Frigate events viewer
│   └── TasksPage.jsx         # Task queue viewer
├── components/
│   └── Layout.jsx            # Navigation, responsive layout
└── utils/
    └── axios.js              # Axios instance with base URL config
```

### WebSocket Protocol

#### Frontend Chat (`/ws`)

**Client → Server:**
```json
{
  "type": "text",
  "content": "Schalte das Licht im Wohnzimmer ein"
}
```

**Server → Client (streaming):**
```json
{"type": "action", "intent": {...}, "result": {...}}  // Action executed
{"type": "stream", "content": "Ich habe..."}          // Response chunks
{"type": "stream", "content": " das Licht..."}
{"type": "done"}                                       // End of stream
```

#### Satellite (`/ws/satellite`)

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

### Key Configuration

All configuration is in `.env` and loaded via `backend/utils/config.py` using Pydantic Settings:

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
- `ADVERTISE_HOST` - Hostname for Zeroconf service advertisement

## Common Development Patterns

### Adding a New Integration

1. Create client in `backend/integrations/your_service.py`:
   ```python
   class YourServiceClient:
       async def do_something(self):
           # Implementation
   ```

2. Add intent handling in `backend/services/action_executor.py`:
   ```python
   elif intent.startswith("yourservice."):
       return await self._execute_yourservice(intent, parameters)
   ```

3. Update intent recognition prompt in `backend/services/ollama_service.py` to include new intent types

4. Create API route in `backend/api/routes/yourservice.py`

5. Register route in `backend/main.py`:
   ```python
   app.include_router(yourservice.router, prefix="/api/yourservice", tags=["YourService"])
   ```

### Adding a New Frontend Page

1. Create page component in `frontend/src/pages/YourPage.jsx`

2. Add route in `frontend/src/App.jsx`:
   ```jsx
   <Route path="/your-page" element={<YourPage />} />
   ```

3. Add navigation link in `frontend/src/components/Layout.jsx`

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

Backend tests use pytest with async support:

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=. --cov-report=html

# Run specific test file
pytest tests/test_intent.py -v

# Run specific test
pytest tests/test_intent.py::test_extract_intent_turn_on -v
```

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

- Check CORS settings in `backend/main.py`
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

## Project Documentation

Additional documentation files in the repository:

- `README.md` - Main user documentation
- `PROJECT_OVERVIEW.md` - High-level architecture
- `QUICKSTART.md` - Quick setup guide
- `INSTALLATION.md` - Detailed installation guide
- `FEATURES.md` - Feature documentation
- `EXTERNAL_OLLAMA.md` - External Ollama instance setup
- `renfield-satellite/README.md` - Satellite setup guide
- `backend/integrations/plugins/README.md` - Plugin development guide
- `docs/ENVIRONMENT_VARIABLES.md` - Environment variable reference
