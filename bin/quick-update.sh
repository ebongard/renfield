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
echo -e "${YELLOW}Restarting backend...${NC}"
docker-compose restart backend

# Wait a moment
sleep 2

# Check whether the backend is running
if docker-compose ps | grep -q "renfield-backend.*Up"; then
    echo ""
    echo -e "${GREEN}✅ Backend restarted successfully!${NC}"
    echo ""
    echo -e "${BLUE}📊 Check the logs:${NC}"
    echo "  docker-compose logs -f backend"
    echo ""
    echo -e "${BLUE}🧪 Now test in the chat:${NC}"
    echo '  "Ist das Licht im Wohnzimmer an?"'
    echo ""
    echo -e "${BLUE}📋 Expected logs:${NC}"
    echo "  📨 WebSocket message..."
    echo "  🔍 Extracting intent..."
    echo "  🎯 Intent recognised: homeassistant.get_state"
    echo "  ⚡ Executing action..."
    echo "  ✅ Action: True"
    echo ""
else
    echo ""
    echo -e "${YELLOW}⚠️  Backend is still starting or an error occurred${NC}"
    echo ""
    echo "Check the logs:"
    docker-compose logs --tail=20 backend
fi
