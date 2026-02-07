# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Renfield is a fully offline-capable, self-hosted **digital assistant** — a personal AI hub that consolidates knowledge, information retrieval, and multi-channel queries into one interface. It serves multiple users in parallel, primarily within the household. Core capabilities include a queryable knowledge base (RAG), bundled tool access (web search, weather, news, etc.), and smart home control as a complementary feature. It informs, assists, and entertains.

**LLM:** Local LLMs via Ollama (configurable per role). See `docs/LLM_MODEL_GUIDE.md` for model recommendations. The system supports structured JSON output, function calling, and chain-of-thought reasoning — enabling multi-step agent workflows.

**Tech Stack:**
- Backend: Python 3.11 + FastAPI + SQLAlchemy
- Frontend: React 18 + TypeScript + Vite + Tailwind CSS + PWA
- Infrastructure: Docker Compose, PostgreSQL 16, Redis 7, Ollama
- LLM: Local models via Ollama (multi-model: chat, intent, RAG, agent, embeddings)
- Integrations: Home Assistant, Frigate (camera NVR), n8n (workflows), SearXNG (web search), Paperless, Email (IMAP/SMTP)
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

1. **Neue API-Endpoints**: Tests in `tests/backend/test_<route>.py` — HTTP status codes, schemas, error handling, edge cases
2. **Neue Services**: Tests in `tests/backend/test_services.py` — unit tests with mocks, `@pytest.mark.unit`
3. **Datenbank-Änderungen**: Tests in `tests/backend/test_models.py` — model creation, constraints, `@pytest.mark.database`
4. **Frontend-Komponenten**: Tests in `tests/frontend/react/` — RTL rendering, user interactions, MSW API mocks

Follow existing test patterns in the respective test files. See `tests/` directory structure for conventions.

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
./bin/start.sh              # Start entire stack
./bin/update.sh             # Update system
./bin/debug.sh              # Debug mode
./bin/quick-update.sh       # Quick backend restart
./bin/deploy-satellite.sh [hostname] [user]  # Deploy satellite
```

### Docker Compose Variants
```bash
docker compose -f docker-compose.dev.yml up -d   # Development (Mac, no GPU)
docker compose -f docker-compose.prod.yml up -d   # Production (NVIDIA GPU, SSL)
docker compose up -d                               # Standard (CPU only)
```

### Database
```bash
docker exec -it renfield-backend alembic revision --autogenerate -m "description"
docker exec -it renfield-backend alembic upgrade head
docker exec -it renfield-backend alembic downgrade -1
```

### Linting & Formatting
```bash
make lint                    # Lint all code (ruff + eslint)
make lint-backend            # Lint backend with ruff
make format-backend          # Format + auto-fix with ruff
```

**Configuration:** `pyproject.toml` — contains ruff, pytest, and coverage config. No separate `pytest.ini`, `.flake8`, etc.

### Testing
```bash
make test                    # Run all tests
make test-backend            # Backend tests only
make test-frontend-react     # React component tests (Vitest)
make test-coverage           # Tests with coverage report (fail-under=50%)
```

### Monitoring
```bash
# Enable Prometheus metrics endpoint (opt-in)
METRICS_ENABLED=true
curl http://localhost:8000/metrics  # Prometheus exposition format
```

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
Integration → MCPManager (HA, n8n, weather, search, etc.) / RAGService (knowledge)
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

### Agent Loop (ReAct — Multi-Step Tool Chaining)

For complex queries requiring multiple steps or conditional logic, Renfield uses a ReAct (Reason + Act) Agent Loop:

```
User → ComplexityDetector → simple? → Single-Intent (as before)
                          → complex? → Agent Loop:
                                        ├─ LLM: Plan → Tool Call 1
                                        ├─ User sees: "🔍 Hole Wetterdaten..."
                                        ├─ Tool Result → back to LLM
                                        ├─ LLM: Reasoning → Tool Call 2
                                        ├─ User sees: "🔍 Suche Hotels..."
                                        └─ LLM: Final Answer → Stream
```

**Key Components:**
- `services/complexity_detector.py` — Regex-based detection (zero-cost, no LLM call)
- `services/agent_tools.py` — Wraps MCP + Plugin tools as descriptions for the LLM prompt
- `services/agent_service.py` — Core loop: LLM → Tool → LLM → ... → Answer (AsyncGenerator)

**Configuration** (all opt-in, disabled by default):
```bash
AGENT_ENABLED=false          # Enable agent loop
AGENT_MAX_STEPS=8            # Max reasoning steps
AGENT_STEP_TIMEOUT=30.0      # Per-step LLM timeout (seconds)
AGENT_TOTAL_TIMEOUT=120.0    # Total timeout
AGENT_MODEL=                 # Optional: separate model for agent
```

**WebSocket Message Types** (Server → Client):
- `agent_thinking` — Agent is analyzing the query
- `agent_tool_call` — Agent is calling a tool (with tool name, parameters, reason)
- `agent_tool_result` — Tool result (success/failure, data)
- `stream` — Final answer (same as single-intent path)
- `done` with `agent_steps` count

### LLM Client Factory

All services obtain their `ollama.AsyncClient` through a central factory in `utils/llm_client.py` instead of instantiating clients directly. The factory provides URL-based caching (same URL → same client instance) and a `LLMClient` Protocol that `ollama.AsyncClient` satisfies via structural typing.

```python
from utils.llm_client import get_default_client, get_agent_client, create_llm_client

client = get_default_client()                          # settings.ollama_url
client, url = get_agent_client(role_url, fallback_url) # role → fallback → default
client = create_llm_client("http://custom:11434")      # arbitrary URL
```

**Key files:** `utils/llm_client.py` (Protocol + Factory), `tests/backend/test_llm_client.py`

**Consumers:** `OllamaService`, `AgentService`, `AgentRouter`, `RAGService`, `IntentFeedbackService`

### Intent Recognition System

The core of Renfield is the intent recognition system in `src/backend/services/ollama_service.py`:

1. **extract_intent()**: Uses Ollama LLM to parse natural language into structured intents (returns top intent)
2. **extract_ranked_intents()**: Returns ranked list of 1-3 intents sorted by confidence (for fallback chain)
3. **Dynamic Keyword Matching**: Fetches device names from Home Assistant to improve accuracy
4. **Intent Types**:
   - `mcp.*` - All external integrations via MCP servers (Home Assistant, n8n, weather, search, news, etc.)
   - `knowledge.*` - Knowledge base / RAG queries (only for user's own documents)
   - `general.conversation` - Normal chat, general knowledge, smalltalk (no action needed)

**Ranked Intents & Fallback Chain:** The LLM returns up to 3 weighted intents. The chat handler tries them in order — if one fails (e.g., RAG returns 0 results), it falls through to the next. If all fail and Agent Loop is enabled, it kicks in as final fallback.

**MCP Tool Prompt Filtering**: With 100+ MCP tools across 8 servers, the intent prompt uses `prompt_tools` (from `mcp_servers.yaml`) to show only the most relevant tools per server. This reduces the prompt to ~20 tools while keeping all tools available for execution. See `IntentRegistry.build_intent_prompt()`.

### Intent Feedback Learning (Semantic Correction)

Renfield learns from user corrections using a 3-scope feedback system with pgvector semantic matching. Scopes: `intent` (wrong classification), `agent_tool` (wrong tool choice), `complexity` (wrong simple/complex). Corrections are stored with 768-dim embeddings and injected as few-shot examples on future similar queries (cosine similarity threshold: 0.75).

**Key files:** `services/intent_feedback_service.py`, `api/routes/feedback.py`, `models/database.py` (IntentCorrection model), `components/IntentCorrectionButton.jsx`

### Conversation Persistence

Full conversation persistence with PostgreSQL across Chat, WebSocket, and Satellite channels. Supports follow-up questions ("Mach es aus" after "Schalte das Licht an"), full-text search, and automatic cleanup.

**Documentation:** See `src/backend/CONVERSATION_API.md` for API endpoints and usage.

### Speaker Recognition

Automatic speaker identification using SpeechBrain ECAPA-TDNN. 192-dim voice embeddings stored in PostgreSQL, cosine similarity matching (threshold: 0.25). Auto-discovery of unknown speakers, continuous learning. **Documentation:** See `SPEAKER_RECOGNITION.md`.

### Device Management

Multiple device types (satellite, web_panel, web_tablet, web_browser, web_kiosk) connect via `/ws/device`. IP-based room detection for stationary devices provides automatic room context:
```python
room_context = await room_service.get_room_context_by_ip(ip_address)
# Returns: {"room_name": "Kitchen", "room_id": 1, "device_id": "...", "auto_detected": True}
```

### Audio Output Routing

Intelligent TTS routing to best available output device per room (priority-ordered, availability-checked). Supports Renfield devices and HA Media Players. `done` message includes `tts_handled` flag. **Documentation:** See `OUTPUT_ROUTING.md`.

### Authentication & Authorization (RPBAC)

Optional (`AUTH_ENABLED=true`). JWT-based auth with role-permission system.

**Permission Hierarchy:**
```
kb.all > kb.shared > kb.own > kb.none
ha.full > ha.control > ha.read > ha.none
cam.full > cam.view > cam.none
```

**Default Roles:** Admin (full access), Familie (ha.full, kb.shared, cam.view), Gast (ha.read, kb.none, cam.none)

**Key files:** `models/permissions.py`, `services/auth_service.py`, `api/routes/auth.py`, `api/routes/roles.py`

**Documentation:** See `ACCESS_CONTROL.md`.

### Frontend Connection Architecture

The frontend uses **two independent WebSocket connections**:

| Connection | Endpoint | Purpose |
|------------|----------|---------|
| **Chat WS** | `/ws` | Send/receive chat messages, conversation persistence via `session_id` |
| **Device WS** | `/ws/device` | Device registration, room assignment, capabilities |

These are completely independent — chat works without device registration, but room context requires it.

### WebSocket Protocol — Chat (`/ws`)

**Client → Server:**
```json
{
  "type": "text",
  "content": "Schalte das Licht im Wohnzimmer ein",
  "session_id": "session-1234567890-abc123def",
  "use_rag": false,
  "knowledge_base_id": null
}
```

**Server → Client (streaming):**
```json
{"type": "action", "intent": {...}, "result": {...}}
{"type": "stream", "content": "Ich habe..."}
{"type": "done", "tts_handled": false}
```

When `session_id` is provided, history is loaded from DB (up to 10 messages) and each exchange is saved.

For Device (`/ws/device`) and Satellite (`/ws/satellite`) protocol details, see the source code in `src/backend/main.py`.

### Key Configuration

All configuration via `.env`, loaded by `src/backend/utils/config.py` (Pydantic Settings). For the full list, see `docs/ENVIRONMENT_VARIABLES.md`.

**Key non-obvious settings:**
- `ADVERTISE_HOST` / `ADVERTISE_PORT` — Hostname for Zeroconf and TTS URL (required for HA media player output)
- `RAG_HYBRID_ENABLED` — Enable Hybrid Search: Dense + BM25 via RRF (default: `true`)
- `RAG_CONTEXT_WINDOW` — Adjacent chunks per direction for context expansion (default: `1`)
- `MCP_ENABLED` — Master switch for MCP server integration (default: `false`)
- Per-server toggles: `WEATHER_ENABLED`, `SEARCH_ENABLED`, `NEWS_ENABLED`, `JELLYFIN_ENABLED`, `N8N_MCP_ENABLED`, `HA_MCP_ENABLED`, `PAPERLESS_ENABLED`, `EMAIL_MCP_ENABLED`
- `MEMORY_CONTRADICTION_RESOLUTION` — LLM-based contradiction detection for memories (default: `false`, opt-in)
- `METRICS_ENABLED` — Prometheus `/metrics` endpoint (default: `false`, opt-in)

## Common Development Patterns

### Adding a New Integration

All external integrations run via MCP servers. To add a new one:

1. Deploy an MCP server for the service (HTTP/SSE or stdio transport)

2. Add the server to `config/mcp_servers.yaml`:
   ```yaml
   servers:
     - name: your_service
       url: "${YOUR_SERVICE_MCP_URL:-http://localhost:9090/mcp}"
       transport: streamable_http
       enabled: "${YOUR_SERVICE_ENABLED:-true}"
       refresh_interval: 300
       example_intent: mcp.your_service.main_tool
       prompt_tools:
         - main_tool
         - secondary_tool
       examples:
         de: ["Beispiel-Anfrage auf Deutsch"]
         en: ["Example query in English"]
   ```

   **YAML fields:**
   | Field | Required | Description |
   |-------|----------|-------------|
   | `name` | Yes | Server identifier, used in `mcp.<name>.<tool>` namespace |
   | `transport` | Yes | `streamable_http`, `sse`, or `stdio` |
   | `enabled` | Yes | Env-var toggle (e.g. `"${MY_ENABLED:-false}"`) |
   | `prompt_tools` | No | Tool base names to include in LLM intent prompt. Omit = show all. All tools remain executable. |
   | `example_intent` | No | Override intent name in prompt examples. Defaults to first tool. |
   | `examples` | No | Bilingual example queries (`de`/`en`) for LLM prompt |

3. Tools are auto-discovered as `mcp.your_service.<tool_name>` intents. `ActionExecutor` routes `mcp.*` intents to `MCPManager.execute_tool()` automatically — no code changes needed.

### Adding a New Frontend Page

1. Create page component in `src/frontend/src/pages/YourPage.jsx`
2. Add route in `src/frontend/src/App.jsx`
3. Add navigation link in `src/frontend/src/components/Layout.jsx`

### Dark Mode Styling

All components must support light and dark mode using Tailwind `dark:` variants:
```jsx
className="bg-gray-50 dark:bg-gray-900"       // Page backgrounds
className="bg-white dark:bg-gray-800"         // Cards, modals
className="text-gray-900 dark:text-white"     // Primary text
className="text-gray-600 dark:text-gray-300"  // Secondary text
className="border-gray-200 dark:border-gray-700"  // Borders
```

**Component classes** (in `src/frontend/src/index.css`): `.card`, `.input`, `.btn-primary`, `.btn-secondary`

**Theme Context** (`ThemeContext.jsx`): `useTheme()` → `theme`, `isDark`, `setTheme`, `toggleTheme`. Persisted in localStorage as `renfield_theme`. Values: `'light'`, `'dark'`, `'system'`.

### Internationalization (i18n)

All frontend text must use react-i18next. **Never hardcode user-facing strings.**

```jsx
import { useTranslation } from 'react-i18next';
const { t } = useTranslation();
// Usage: {t('myFeature.title')}, t('users.deleteConfirm', { name })
```

Add translations to both `src/frontend/src/i18n/locales/de.json` and `en.json`. See `docs/MULTILANGUAGE.md`.

### Debugging Intent Recognition

```bash
curl -X POST "http://localhost:8000/debug/intent?message=Schalte das Licht ein"
```

### Refreshing Home Assistant Keywords

```bash
curl -X POST "http://localhost:8000/admin/refresh-keywords"
```

### Re-Embedding All Vectors

After changing the embedding model (`OLLAMA_EMBED_MODEL`), all existing vectors must be recalculated:

```bash
curl -X POST "http://localhost:8000/admin/reembed"
```

This re-embeds RAG chunks, conversation memories, intent corrections, and notification suppressions in the background.

## Testing

Tests are in `tests/` at project root. Backend: 1,300+ tests across all API routes and services.

```bash
make test                # All tests
make test-backend        # Backend only
make test-frontend-react # React component tests (Vitest)
make test-coverage       # With coverage report
```

**Test markers:** `@pytest.mark.unit`, `@pytest.mark.database`, `@pytest.mark.integration`, `@pytest.mark.e2e`, `@pytest.mark.backend`, `@pytest.mark.frontend`, `@pytest.mark.satellite`

**React tests** use Vitest + RTL + MSW in `tests/frontend/react/` (separate `package.json` for security isolation).

## CI/CD Pipeline

| Workflow | Trigger | Description |
|----------|---------|-------------|
| `ci.yml` | Push to main/develop, PRs | Full CI: ruff lint, test (with coverage threshold), build |
| `pr-check.yml` | Pull requests | Quick PR checks (ruff lint, eslint) |
| `release.yml` | Tag push (v*.*.*) | Build + push Docker images to GHCR |

```bash
make release    # Create and push version tag
```

## Deployment

- Fully offline once models are downloaded. First startup: 5-10 min for model download.
- GPU: Install NVIDIA Container Toolkit, use `docker-compose.prod.yml`
- `Dockerfile.gpu` includes Node.js 20 for MCP stdio servers (`npx`)

### Production Secrets

Production uses Docker Compose file-based secrets (`/run/secrets/`) instead of `.env` for sensitive values. Secret files are in `/opt/renfield/secrets/` on the production server.

**Key rule:** Sensitive values (passwords, tokens, API keys) must NEVER appear in `.env` on production.

**How secrets reach MCP servers:** Pydantic `secrets_dir="/run/secrets"` loads into Settings. `mcp_client.py` additionally injects `/run/secrets/*` into `os.environ` for YAML `${VAR}` substitution and stdio subprocesses.

```bash
# Deploy workflow
rsync -av --exclude='.env' --exclude='secrets/' ./ renfield.local:/opt/renfield/
ssh renfield.local 'cd /opt/renfield && docker compose -f docker-compose.prod.yml up -d --build'
```

**Documentation:** See `docs/SECRETS_MANAGEMENT.md`.

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
- **ReSpeaker not detected**: Check for GPIO4 conflict with `w1-gpio` overlay (disable it in `/boot/firmware/config.txt`)
- **Wrong microphone**: Ensure `.asoundrc` is configured for ReSpeaker — copy from `src/satellite/config/asoundrc`
- **Garbled transcription**: PyAudio must be installed (not soundcard) for ALSA support
- **GPIO errors**: Add user to gpio group with `sudo usermod -aG gpio $USER`
- **lgpio build fails**: Install `swig` and `liblgpio-dev` system packages
- **openwakeword on Python 3.13+**: Install with `--no-deps` (tflite-runtime has no Python 3.13 wheels)
