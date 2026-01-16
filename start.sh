#!/bin/bash

# Renfield Startup Script
# Startet alle Services und prüft die Konfiguration

set -e

echo "🚀 Renfield wird gestartet..."
echo ""

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Prüfe ob Docker läuft
if ! docker info > /dev/null 2>&1; then
    echo -e "${RED}❌ Docker läuft nicht. Bitte Docker starten.${NC}"
    exit 1
fi

# Prüfe ob .env existiert
if [ ! -f .env ]; then
    echo -e "${YELLOW}⚠️  .env Datei nicht gefunden. Erstelle aus .env.example...${NC}"
    cp .env.example .env
    echo -e "${YELLOW}⚠️  Bitte .env Datei anpassen und Script erneut ausführen.${NC}"
    exit 1
fi

# Lade .env
source .env

# Prüfe wichtige Variablen
if [ "$POSTGRES_PASSWORD" = "changeme" ]; then
    echo -e "${YELLOW}⚠️  Bitte POSTGRES_PASSWORD in .env ändern!${NC}"
fi

# Container starten
echo "📦 Starte Docker Container..."
docker-compose up -d

echo ""
echo "⏳ Warte auf Services..."
sleep 10

# Health Check
echo ""
echo "🔍 Prüfe Services..."

check_service() {
    local name=$1
    local url=$2
    if curl -s -f -o /dev/null "$url"; then
        echo -e "${GREEN}✓${NC} $name ist bereit"
    else
        echo -e "${RED}✗${NC} $name ist nicht erreichbar"
    fi
}

check_service "Backend" "http://localhost:8000/health"
check_service "Frontend" "http://localhost:3000"

# Prüfe ob Ollama Modell geladen ist
echo ""
echo "🤖 Prüfe Ollama Modell..."
if docker exec renfield-ollama ollama list | grep -q "$OLLAMA_MODEL"; then
    echo -e "${GREEN}✓${NC} Ollama Modell '$OLLAMA_MODEL' ist vorhanden"
else
    echo -e "${YELLOW}⚠️  Ollama Modell '$OLLAMA_MODEL' nicht gefunden. Lade herunter...${NC}"
    docker exec renfield-ollama ollama pull "$OLLAMA_MODEL"
fi

# Zusammenfassung
echo ""
echo "================================================"
echo -e "${GREEN}✓ Renfield ist bereit!${NC}"
echo "================================================"
echo ""
echo "📱 Web-Interface: http://localhost:3000"
echo "🔧 API Docs:      http://localhost:8000/docs"
echo ""
echo "📊 Container Status:"
docker-compose ps
echo ""
echo "📝 Logs anzeigen:    docker-compose logs -f"
echo "🛑 Stoppen:          docker-compose down"
echo ""
