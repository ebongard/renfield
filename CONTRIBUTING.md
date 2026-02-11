# Contributing to Renfield

Thank you for your interest in contributing to Renfield! This guide will help you get started.

## Getting Started

### Prerequisites

- Docker & Docker Compose
- Python 3.11+ (for backend development)
- Node.js 20+ (for frontend development)
- At least 16 GB RAM (32 GB recommended)

### Development Setup

1. **Clone the repository**
   ```bash
   git clone https://github.com/ebongard/renfield.git
   cd renfield
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your settings (Ollama URL, model names, etc.)
   ```

3. **Start the stack**
   ```bash
   # Mac development
   docker compose -f docker-compose.dev.yml up -d

   # Standard
   docker compose up -d
   ```

4. **Download an LLM model**
   ```bash
   docker exec -it renfield-ollama ollama pull qwen3:8b
   ```

5. **Access the app**
   - Frontend: http://localhost:3000
   - API docs: http://localhost:8000/docs

## Code Style

### Backend (Python)

- **Linter/Formatter:** [ruff](https://docs.astral.sh/ruff/) — configured in `pyproject.toml`
- Run `make lint-backend` to check, `make format-backend` to auto-fix
- Follow existing patterns in the codebase

### Frontend (React/TypeScript)

- **Linter:** ESLint — configured in `eslint.config.js`
- **Styling:** Tailwind CSS with `dark:` variants for dark mode
- **i18n:** All user-facing strings via `react-i18next` (add to both `de.json` and `en.json`)

## Testing

Every code change should include matching tests.

```bash
make test                    # all tests
make test-backend            # backend tests (pytest)
make test-frontend-react     # frontend tests (Vitest + RTL)
make test-coverage           # with coverage report
```

### Test markers

| Marker | Description |
|--------|-------------|
| `@pytest.mark.unit` | Unit tests (fast, no external deps) |
| `@pytest.mark.database` | Tests that need PostgreSQL |
| `@pytest.mark.integration` | Integration tests |
| `@pytest.mark.e2e` | End-to-end tests |

### Where to put tests

| Change | Test file |
|--------|-----------|
| New API endpoint | `tests/backend/test_<route>.py` |
| New service | `tests/backend/test_services.py` or `tests/backend/test_<service>.py` |
| Database model | `tests/backend/test_models.py` |
| React component | `tests/frontend/react/` |

## Pull Request Process

1. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/your-feature
   ```

2. **Make your changes** — follow the code style and include tests.

3. **Run linting and tests locally:**
   ```bash
   make lint && make test
   ```

4. **Create a pull request** with a clear description:
   - What does this change?
   - Why is it needed?
   - How can it be tested?

5. **PR review** — maintainers will review your code. Please be patient and responsive to feedback.

### Commit Message Format

```
type(scope): short description (#issue)

Longer description if needed.
```

**Types:** `feat`, `fix`, `docs`, `refactor`, `test`, `chore`

**Examples:**
- `feat(agent): add tool timeout configuration (#42)`
- `fix(satellite): handle reconnection after network loss (#15)`
- `docs(readme): add satellite hardware guide`

## Issue Labels

| Label | Description |
|-------|-------------|
| `good-first-issue` | Great for newcomers |
| `help-wanted` | Looking for contributors |
| `bug` | Something isn't working |
| `enhancement` | New feature or improvement |
| `documentation` | Documentation improvements |
| `backend` | Python/FastAPI changes |
| `frontend` | React/TypeScript changes |
| `satellite` | Raspberry Pi satellite code |
| `mcp` | MCP server integrations |
| `agent-loop` | ReAct agent system |

## Adding a New Integration

All external integrations run via MCP servers. To add one:

1. Deploy an MCP server for the service (HTTP/SSE or stdio transport)
2. Add the server to `config/mcp_servers.yaml`
3. Tools are auto-discovered as `mcp.<server>.<tool>` intents — no code changes needed

See [CLAUDE.md](CLAUDE.md) for the full YAML schema and examples.

## Project Structure

```
renfield/
├── src/
│   ├── backend/          # Python FastAPI backend
│   │   ├── api/          # Routes and WebSocket handlers
│   │   ├── services/     # Business logic
│   │   ├── models/       # SQLAlchemy models
│   │   └── utils/        # Config, helpers, hooks
│   ├── frontend/         # React 18 + TypeScript + Vite
│   │   └── src/
│   │       ├── components/
│   │       ├── pages/
│   │       └── i18n/     # Translations (de.json, en.json)
│   └── satellite/        # Raspberry Pi satellite code
├── config/               # MCP server config (YAML)
├── tests/                # All tests
├── docs/                 # Documentation
└── docker-compose*.yml   # Docker Compose variants
```

## Questions?

- Open a [GitHub Discussion](https://github.com/ebongard/renfield/discussions) for questions
- Check existing [Issues](https://github.com/ebongard/renfield/issues) before creating new ones
- Read the [documentation](docs/) for detailed guides

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
