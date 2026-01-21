#!/bin/bash
# Test-Script für Ollama-Verbindung
# Testet ob die konfigurierte Ollama-Instanz erreichbar ist
#
# Run from project root:
#     ./tests/manual/test_ollama_connection.sh

set -e

# Change to project root directory
cd "$(dirname "$0")/../.."

# Farben für Output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "🔍 Teste Ollama-Verbindung..."
echo ""

# Lade .env Datei
if [ ! -f .env ]; then
    echo -e "${RED}❌ .env Datei nicht gefunden!${NC}"
    echo "Kopiere .env.example zu .env und konfiguriere OLLAMA_URL"
    exit 1
fi

# Extrahiere OLLAMA_URL aus .env
OLLAMA_URL=$(grep "^OLLAMA_URL=" .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")
OLLAMA_MODEL=$(grep "^OLLAMA_MODEL=" .env | cut -d '=' -f2- | tr -d '"' | tr -d "'")

# Fallback zu Default-Werten
OLLAMA_URL=${OLLAMA_URL:-http://ollama:11434}
OLLAMA_MODEL=${OLLAMA_MODEL:-llama3.2:3b}

echo "📋 Konfiguration:"
echo "   URL: $OLLAMA_URL"
echo "   Model: $OLLAMA_MODEL"
echo ""

# Test 1: Basis-Erreichbarkeit
echo "🔌 Test 1: Basis-Erreichbarkeit..."
if curl -s --connect-timeout 5 "$OLLAMA_URL" > /dev/null 2>&1; then
    echo -e "${GREEN}✅ Ollama ist erreichbar${NC}"
else
    echo -e "${RED}❌ Ollama nicht erreichbar unter $OLLAMA_URL${NC}"
    echo ""
    echo "Mögliche Ursachen:"
    echo "  - Ollama läuft nicht"
    echo "  - Falsche URL in .env"
    echo "  - Firewall blockiert Port 11434"
    echo "  - Hostname nicht auflösbar"
    echo ""
    echo "Troubleshooting:"
    echo "  ping ${OLLAMA_URL#http://}"
    echo "  telnet ${OLLAMA_URL#http://} | cut -d: -f1"
    exit 1
fi

# Test 2: API-Verfügbarkeit
echo "🔌 Test 2: API-Verfügbarkeit..."
API_RESPONSE=$(curl -s --connect-timeout 5 "$OLLAMA_URL/api/tags" 2>&1)
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ API ist verfügbar${NC}"
else
    echo -e "${RED}❌ API antwortet nicht${NC}"
    echo "Response: $API_RESPONSE"
    exit 1
fi

# Test 3: Modell verfügbar
echo "🔌 Test 3: Modell-Verfügbarkeit..."
if echo "$API_RESPONSE" | grep -q "\"$OLLAMA_MODEL\""; then
    echo -e "${GREEN}✅ Modell '$OLLAMA_MODEL' ist installiert${NC}"
else
    echo -e "${YELLOW}⚠️  Modell '$OLLAMA_MODEL' nicht gefunden${NC}"
    echo ""
    echo "Installierte Modelle:"
    echo "$API_RESPONSE" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | sed 's/^/  - /'
    echo ""
    echo "Installiere das Modell mit:"
    if [[ "$OLLAMA_URL" == *"ollama:11434"* ]]; then
        echo "  docker exec -it renfield-ollama ollama pull $OLLAMA_MODEL"
    else
        echo "  ssh ${OLLAMA_URL#http://} \"ollama pull $OLLAMA_MODEL\""
        echo "  # oder direkt auf dem Ollama-Server:"
        echo "  ollama pull $OLLAMA_MODEL"
    fi
    echo ""
    read -p "Trotzdem fortfahren? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Test 4: Chat-Request
echo "🔌 Test 4: Test-Chat..."
CHAT_TEST=$(curl -s --connect-timeout 10 "$OLLAMA_URL/api/chat" -d '{
  "model": "'"$OLLAMA_MODEL"'",
  "messages": [{"role": "user", "content": "Hi"}],
  "stream": false
}' 2>&1)

if [ $? -eq 0 ] && echo "$CHAT_TEST" | grep -q "message"; then
    echo -e "${GREEN}✅ Chat funktioniert${NC}"
    RESPONSE=$(echo "$CHAT_TEST" | grep -o '"content":"[^"]*"' | head -1 | cut -d'"' -f4)
    echo "   Response: ${RESPONSE:0:50}..."
else
    echo -e "${RED}❌ Chat fehlgeschlagen${NC}"
    echo "Response: $CHAT_TEST"
    exit 1
fi

# Zusammenfassung
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}✅ Alle Tests erfolgreich!${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "Du kannst Renfield jetzt starten:"
if [[ "$OLLAMA_URL" == *"ollama:11434"* ]]; then
    echo "  ./bin/start.sh"
    echo "  # oder: docker compose --profile ollama up -d"
else
    echo "  ./bin/start.sh"
    echo "  # oder: docker compose up -d"
fi
echo ""
