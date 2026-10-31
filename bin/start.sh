#!/bin/bash

# Renfield Startup Script
# Starts every service and checks the configuration

set -e

# Change to project root directory
cd "$(dirname "$0")/.."

echo "🚀 Starting Renfield..."
echo ""

# Colors for the output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check whether Docker is running
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}❌ Docker is not running. Please start Docker.${NC}"
    exit 1
fi

# Check whether .env exists
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env file not found. Creating it from .env.example...${NC}"
    cp .env.example .env
    echo -e "${YELLOW}⚠️  Please adjust the .env file and run the script again.${NC}"
    exit 1
fi

# Load .env
source .env

# Check the important variables
if [ "$POSTGRES_PASSWORD" = "changeme" ]; then
    echo -e "${YELLOW}⚠️  Please change POSTGRES_PASSWORD in .env!${NC}"
fi

# Start the containers
echo "📦 Starting Docker containers..."
docker-compose up -d

echo ""
echo "⏳ Waiting for services..."
sleep 10

# Health Check
echo ""
echo "🔍 Checking services..."

check_service() {
    local name=$1
    local url=$2
    if curl -s -f -o /dev/null "$url"; then
        echo -e "${GREEN}✓${NC} $name is ready"
    else
        echo -e "${RED}✗${NC} $name is not reachable"
    fi
}

check_service "Backend" "http://localhost:8000/health"
check_service "Frontend" "http://localhost:3000"

# Check whether the Ollama model is loaded
echo ""
echo "🤖 Checking Ollama model..."
if docker exec renfield-ollama ollama list | grep -q "$OLLAMA_MODEL"; then
    echo -e "${GREEN}✓${NC} Ollama model '$OLLAMA_MODEL' is present"
else
    echo -e "${YELLOW}⚠️  Ollama model '$OLLAMA_MODEL' not found. Downloading...${NC}"
    docker exec renfield-ollama ollama pull "$OLLAMA_MODEL"
fi

# Summary
echo ""
echo "================================================"
echo -e "${GREEN}✓ Renfield is ready!${NC}"
echo "================================================"
echo ""
echo "📱 Web-Interface: http://localhost:3000"
echo "🔧 API Docs:      http://localhost:8000/docs"
echo ""
echo "📊 Container Status:"
docker-compose ps
echo ""
echo "📝 Show logs:        docker-compose logs -f"
echo "🛑 Stop:             docker-compose down"
echo ""
