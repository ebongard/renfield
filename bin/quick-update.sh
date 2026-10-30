#!/bin/bash

# Renfield Quick Update Script
# Updates only the backend container

# Change to project root directory
cd "$(dirname "$0")/.."

echo "🔄 Renfield Quick Update"
echo "======================="
echo ""

# Farben
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Restart the backend
echo -e "${YELLOW}Starte Backend neu...${NC}"
docker-compose restart backend

# Wait a moment
sleep 2

# Check whether the backend is running
if docker-compose ps | grep -q "renfield-backend.*Up"; then
    echo ""
    echo -e "${GREEN}✅ Backend erfolgreich neu gestartet!${NC}"
    echo ""
    echo -e "${BLUE}📊 Logs prüfen:${NC}"
    echo "  docker-compose logs -f backend"
    echo ""
    echo -e "${BLUE}🧪 Teste jetzt im Chat:${NC}"
    echo '  "Ist das Licht im Wohnzimmer an?"'
    echo ""
    echo -e "${BLUE}📋 Erwartete Logs:${NC}"
    echo "  📨 WebSocket Nachricht..."
    echo "  🔍 Extrahiere Intent..."
    echo "  🎯 Intent erkannt: homeassistant.get_state"
    echo "  ⚡ Führe Aktion aus..."
    echo "  ✅ Aktion: True"
    echo ""
else
    echo ""
    echo -e "${YELLOW}⚠️  Backend startet noch oder Fehler aufgetreten${NC}"
    echo ""
    echo "Prüfe Logs:"
    docker-compose logs --tail=20 backend
fi
