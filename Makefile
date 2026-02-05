# Renfield - Build & Task Orchestration
# =====================================
#
# Usage:
#   make help          - Show all available commands
#   make dev           - Start development environment
#   make test          - Run all tests
#   make build         - Build all components
#
# Prerequisites:
#   - Docker & Docker Compose
#   - Python 3.11+ (for local development)
#   - Node.js 18+ (for local development)

.PHONY: help dev prod stop clean build test lint format \
        backend-dev backend-test backend-lint backend-build \
        frontend-dev frontend-test frontend-lint frontend-build \
        test-frontend-react \
        docker-build docker-up docker-down docker-logs \
        db-migrate db-upgrade db-downgrade \
        ollama-pull ollama-test \
        ci install

# Default target
.DEFAULT_GOAL := help

# Colors for output
BLUE := \033[0;34m
GREEN := \033[0;32m
YELLOW := \033[1;33m
RED := \033[0;31m
NC := \033[0m

# Project paths
PROJECT_ROOT := $(shell pwd)
SRC_DIR := $(PROJECT_ROOT)/src
BACKEND_DIR := $(SRC_DIR)/backend
FRONTEND_DIR := $(SRC_DIR)/frontend
SATELLITE_DIR := $(SRC_DIR)/satellite
TESTS_DIR := $(PROJECT_ROOT)/tests

# Docker compose files
DC := docker compose
DC_DEV := docker compose -f docker-compose.dev.yml
DC_PROD := docker compose -f docker-compose.prod.yml

# ============================================================================
# Help
# ============================================================================

help: ## Show this help message
	@echo ""
	@echo "$(BLUE)Renfield - Build & Task Orchestration$(NC)"
	@echo "======================================="
	@echo ""
	@echo "$(GREEN)Quick Start:$(NC)"
	@echo "  make dev         Start development environment"
	@echo "  make test        Run all tests"
	@echo "  make build       Build all components"
	@echo ""
	@echo "$(GREEN)Available Commands:$(NC)"
	@awk 'BEGIN {FS = ":.*##"; printf ""} /^[a-zA-Z_-]+:.*?##/ { printf "  $(BLUE)%-18s$(NC) %s\n", $$1, $$2 }' $(MAKEFILE_LIST)
	@echo ""

# ============================================================================
# Development Environment
# ============================================================================

dev: docker-up ## Start development environment
	@echo "$(GREEN)✓ Development environment started$(NC)"
	@echo ""
	@echo "  Frontend: http://localhost:3000"
	@echo "  Backend:  http://localhost:8000"
	@echo "  API Docs: http://localhost:8000/docs"
	@echo ""

prod: ## Start production environment
	@echo "$(BLUE)Starting production environment...$(NC)"
	$(DC_PROD) up -d
	@echo "$(GREEN)✓ Production environment started$(NC)"

stop: docker-down ## Stop all containers

restart: stop dev ## Restart development environment

status: ## Show container status
	@$(DC) ps

logs: ## Show container logs (tail)
	@$(DC) logs -f --tail=100

logs-backend: ## Show backend logs
	@$(DC) logs -f backend

logs-frontend: ## Show frontend logs
	@$(DC) logs -f frontend

# ============================================================================
# Build Commands
# ============================================================================

build: backend-build frontend-build ## Build all components
	@echo "$(GREEN)✓ All components built$(NC)"

docker-build: ## Build all Docker images
	@echo "$(BLUE)Building Docker images...$(NC)"
	$(DC) build
	@echo "$(GREEN)✓ Docker images built$(NC)"

docker-build-no-cache: ## Build Docker images without cache
	@echo "$(BLUE)Building Docker images (no cache)...$(NC)"
	$(DC) build --no-cache
	@echo "$(GREEN)✓ Docker images built$(NC)"

backend-build: ## Build backend (check syntax in Docker)
	@echo "$(BLUE)Checking backend...$(NC)"
	@$(DC) exec -T backend python -m py_compile main.py 2>/dev/null || \
		(cd $(BACKEND_DIR) && python -m py_compile main.py)
	@echo "$(GREEN)✓ Backend syntax OK$(NC)"

frontend-build: ## Build frontend for production (in Docker)
	@echo "$(BLUE)Building frontend...$(NC)"
	@$(DC) exec -T frontend npm run build 2>/dev/null || \
		(cd $(FRONTEND_DIR) && npm run build)
	@echo "$(GREEN)✓ Frontend built$(NC)"

# ============================================================================
# Test Commands (run in Docker by default)
# ============================================================================

test: test-backend test-frontend test-integration ## Run all tests (in Docker)
	@echo "$(GREEN)✓ All tests passed$(NC)"

test-backend: ## Run backend tests (in Docker)
	@echo "$(BLUE)Running backend tests...$(NC)"
	@$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/backend/ -v --tb=short || \
		(echo "$(YELLOW)Container not running. Starting...$(NC)" && \
		$(DC) up -d backend && sleep 5 && \
		$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/backend/ -v --tb=short)
	@echo "$(GREEN)✓ Backend tests passed$(NC)"

test-frontend: ## Run frontend API contract tests (in Docker)
	@echo "$(BLUE)Running frontend API contract tests...$(NC)"
	@$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/frontend/ -v --tb=short || \
		(echo "$(YELLOW)Container not running. Starting...$(NC)" && \
		$(DC) up -d backend && sleep 5 && \
		$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/frontend/ -v --tb=short)
	@echo "$(GREEN)✓ Frontend API contract tests passed$(NC)"

test-frontend-react: ## Run React component tests (Vitest)
	@echo "$(BLUE)Running React component tests...$(NC)"
	@cd $(PROJECT_ROOT)/tests/frontend/react && npm test -- --run
	@echo "$(GREEN)✓ React component tests passed$(NC)"

test-satellite: ## Run satellite tests (in Docker)
	@echo "$(BLUE)Running satellite tests...$(NC)"
	@$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/satellite/ -v --tb=short
	@echo "$(GREEN)✓ Satellite tests passed$(NC)"

test-integration: ## Run integration tests (in Docker)
	@echo "$(BLUE)Running integration tests...$(NC)"
	@$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/integration/ -v --tb=short
	@echo "$(GREEN)✓ Integration tests passed$(NC)"

test-unit: ## Run only unit tests (in Docker)
	@echo "$(BLUE)Running unit tests...$(NC)"
	@$(DC) exec -T -e PYTHONPATH=/app backend pytest /tests/ -m unit -v --tb=short
	@echo "$(GREEN)✓ Unit tests passed$(NC)"

test-coverage: ## Run tests with coverage (in Docker)
	@echo "$(BLUE)Running tests with coverage...$(NC)"
	@$(DC) exec -T -e PYTHONPATH=/app backend pytest --cov=/app --cov-report=html --cov-report=term-missing --cov-fail-under=50 /tests/backend/
	@echo "$(GREEN)✓ Coverage report generated$(NC)"

test-local: ## Run tests locally (requires local Python env)
	@echo "$(BLUE)Running tests locally...$(NC)"
	@cd $(PROJECT_ROOT) && pytest tests/ -v --tb=short
	@echo "$(GREEN)✓ Local tests passed$(NC)"

test-manual-ollama: ## Run manual Ollama connection test
	@./tests/manual/test_ollama_connection.sh

# ============================================================================
# Lint & Format Commands
# ============================================================================

lint: lint-backend lint-frontend ## Lint all code (in Docker)
	@echo "$(GREEN)✓ All linting passed$(NC)"

lint-backend: ## Lint backend Python code (in Docker)
	@echo "$(BLUE)Linting backend...$(NC)"
	@$(DC) exec -T backend ruff check /app --config /app/pyproject.toml
	@echo "$(GREEN)✓ Backend linted$(NC)"

lint-frontend: ## Lint frontend JavaScript code (in Docker)
	@echo "$(BLUE)Linting frontend...$(NC)"
	@$(DC) exec -T frontend npm run lint 2>/dev/null || \
		(cd $(FRONTEND_DIR) && npm run lint)
	@echo "$(GREEN)✓ Frontend linted$(NC)"

format: format-backend format-frontend ## Format all code (in Docker)
	@echo "$(GREEN)✓ All code formatted$(NC)"

format-backend: ## Format backend Python code (in Docker)
	@echo "$(BLUE)Formatting backend...$(NC)"
	@$(DC) exec -T backend ruff format /app --config /app/pyproject.toml
	@$(DC) exec -T backend ruff check --fix /app --config /app/pyproject.toml
	@echo "$(GREEN)✓ Backend formatted$(NC)"

format-frontend: ## Format frontend JavaScript code (in Docker)
	@echo "$(BLUE)Formatting frontend...$(NC)"
	@$(DC) exec -T frontend npm run format 2>/dev/null || \
		$(DC) exec -T frontend npm run lint -- --fix 2>/dev/null || true
	@echo "$(GREEN)✓ Frontend formatted$(NC)"

# ============================================================================
# Docker Commands
# ============================================================================

docker-up: ## Start Docker containers
	@echo "$(BLUE)Starting Docker containers...$(NC)"
	$(DC) up -d
	@echo "$(GREEN)✓ Containers started$(NC)"

docker-up-dev: ## Start Docker containers (dev mode)
	@echo "$(BLUE)Starting Docker containers (dev)...$(NC)"
	$(DC_DEV) up -d
	@echo "$(GREEN)✓ Containers started$(NC)"

docker-down: ## Stop Docker containers
	@echo "$(BLUE)Stopping Docker containers...$(NC)"
	$(DC) down
	@echo "$(GREEN)✓ Containers stopped$(NC)"

docker-restart: ## Restart Docker containers
	@echo "$(BLUE)Restarting Docker containers...$(NC)"
	$(DC) restart
	@echo "$(GREEN)✓ Containers restarted$(NC)"

docker-restart-backend: ## Restart only backend container
	@echo "$(BLUE)Restarting backend...$(NC)"
	$(DC) restart backend
	@echo "$(GREEN)✓ Backend restarted$(NC)"

docker-clean: ## Remove all containers and volumes
	@echo "$(YELLOW)Warning: This will delete all data!$(NC)"
	@read -p "Are you sure? (y/n) " -n 1 -r; echo; \
	if [ "$$REPLY" = "y" ]; then \
		$(DC) down -v --remove-orphans; \
		echo "$(GREEN)✓ Cleaned$(NC)"; \
	else \
		echo "Aborted"; \
	fi

docker-shell-backend: ## Open shell in backend container
	@$(DC) exec backend /bin/bash

docker-shell-frontend: ## Open shell in frontend container
	@$(DC) exec frontend /bin/sh

# ============================================================================
# Database Commands
# ============================================================================

db-migrate: ## Create a new database migration
	@read -p "Migration description: " desc; \
	$(DC) exec backend alembic revision --autogenerate -m "$$desc"

db-upgrade: ## Apply database migrations
	@echo "$(BLUE)Applying database migrations...$(NC)"
	@$(DC) exec backend alembic upgrade head
	@echo "$(GREEN)✓ Migrations applied$(NC)"

db-downgrade: ## Rollback last database migration
	@echo "$(BLUE)Rolling back last migration...$(NC)"
	@$(DC) exec backend alembic downgrade -1
	@echo "$(GREEN)✓ Migration rolled back$(NC)"

db-reset: ## Reset database (WARNING: deletes all data)
	@echo "$(RED)Warning: This will delete all database data!$(NC)"
	@read -p "Are you sure? (y/n) " -n 1 -r; echo; \
	if [ "$$REPLY" = "y" ]; then \
		$(DC) exec backend alembic downgrade base; \
		$(DC) exec backend alembic upgrade head; \
		echo "$(GREEN)✓ Database reset$(NC)"; \
	else \
		echo "Aborted"; \
	fi

# ============================================================================
# Ollama Commands
# ============================================================================

ollama-pull: ## Pull/update Ollama model
	@echo "$(BLUE)Pulling Ollama model...$(NC)"
	@source .env 2>/dev/null; \
	MODEL=$${OLLAMA_MODEL:-llama3.2:3b}; \
	$(DC) exec ollama ollama pull $$MODEL || \
	docker exec renfield-ollama ollama pull $$MODEL
	@echo "$(GREEN)✓ Model pulled$(NC)"

ollama-list: ## List installed Ollama models
	@$(DC) exec ollama ollama list 2>/dev/null || \
	docker exec renfield-ollama ollama list

ollama-test: ## Test Ollama connection
	@./tests/manual/test_ollama_connection.sh

# ============================================================================
# Install & Setup Commands
# ============================================================================

install: install-backend install-frontend ## Install all dependencies
	@echo "$(GREEN)✓ All dependencies installed$(NC)"

install-backend: ## Install backend dependencies
	@echo "$(BLUE)Installing backend dependencies...$(NC)"
	@cd $(BACKEND_DIR) && pip install -r requirements.txt
	@echo "$(GREEN)✓ Backend dependencies installed$(NC)"

install-frontend: ## Install frontend dependencies
	@echo "$(BLUE)Installing frontend dependencies...$(NC)"
	@cd $(FRONTEND_DIR) && npm install
	@echo "$(GREEN)✓ Frontend dependencies installed$(NC)"

setup: ## Initial project setup
	@echo "$(BLUE)Setting up project...$(NC)"
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "$(YELLOW)Created .env from .env.example - please configure it$(NC)"; \
	fi
	@$(MAKE) docker-build
	@$(MAKE) docker-up
	@sleep 10
	@$(MAKE) db-upgrade
	@$(MAKE) ollama-pull
	@echo "$(GREEN)✓ Project setup complete$(NC)"

# ============================================================================
# CI/CD Commands
# ============================================================================

ci: lint test ## Run CI pipeline (lint + test)
	@echo "$(GREEN)✓ CI pipeline passed$(NC)"

ci-full: lint test-coverage docker-build ## Run full CI pipeline
	@echo "$(GREEN)✓ Full CI pipeline passed$(NC)"

release: ## Create a release (tag + push)
	@read -p "Version (e.g., v1.0.0): " version; \
	git tag -a $$version -m "Release $$version"; \
	git push origin $$version; \
	echo "$(GREEN)✓ Released $$version$(NC)"

# ============================================================================
# Utility Commands
# ============================================================================

clean: ## Clean temporary files
	@echo "$(BLUE)Cleaning temporary files...$(NC)"
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name "node_modules" -prune -o -type d -name ".cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@rm -rf htmlcov .coverage 2>/dev/null || true
	@echo "$(GREEN)✓ Cleaned$(NC)"

version: ## Show version information
	@echo "$(BLUE)Renfield Version Info$(NC)"
	@echo "====================="
	@echo "Git commit: $$(git rev-parse --short HEAD 2>/dev/null || echo 'N/A')"
	@echo "Git branch: $$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'N/A')"
	@echo "Docker:     $$(docker --version 2>/dev/null || echo 'N/A')"
	@echo "Python:     $$(python --version 2>/dev/null || echo 'N/A')"
	@echo "Node:       $$(node --version 2>/dev/null || echo 'N/A')"

check-env: ## Check if .env is configured
	@if [ ! -f .env ]; then \
		echo "$(RED)✗ .env file not found$(NC)"; \
		exit 1; \
	fi
	@echo "$(GREEN)✓ .env file exists$(NC)"
	@echo ""
	@echo "Configuration:"
	@grep -E "^(OLLAMA_URL|HOME_ASSISTANT_URL|DATABASE_URL)=" .env | sed 's/=.*/=***/'
