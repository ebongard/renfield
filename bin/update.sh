#!/bin/bash

# Renfield Update Script
# Updates the system safely

set -e

# Change to project root directory
cd "$(dirname "$0")/.."

echo "🔄 Renfield Update"
echo ""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Create a backup
echo "💾 Erstelle Backup..."
timestamp=$(date +%Y%m%d_%H%M%S)
backup_dir="backups/backup_$timestamp"
mkdir -p "$backup_dir"

# Database backup
echo "  → Datenbank..."
docker exec renfield-postgres pg_dump -U renfield renfield > "$backup_dir/database.sql"

# .env Backup
echo "  → Konfiguration..."
cp .env "$backup_dir/.env"

echo -e "${GREEN}✓${NC} Backup erstellt in $backup_dir"
echo ""

# Git pull (if this is a git repository)
if [ -d .git ]; then
    echo "📥 Lade Updates..."
    git pull
    echo -e "${GREEN}✓${NC} Code aktualisiert"
    echo ""
fi

# Rebuild the containers
echo "🔨 Baue Container neu..."
docker-compose build

# Restart the containers
echo "🔄 Starte Container neu..."
docker-compose down
docker-compose up -d

echo ""
echo "⏳ Warte auf Services..."
sleep 15

# Health Check
if curl -s -f -o /dev/null "http://localhost:8000/health"; then
    echo -e "${GREEN}✓${NC} Backend ist online"
else
    echo -e "${RED}✗${NC} Backend ist nicht erreichbar"
    echo "Rollback mit: ./bin/rollback.sh $backup_dir"
    exit 1
fi

if curl -s -f -o /dev/null "http://localhost:3000"; then
    echo -e "${GREEN}✓${NC} Frontend ist online"
else
    echo -e "${RED}✗${NC} Frontend ist nicht erreichbar"
fi

echo ""
echo "================================================"
echo -e "${GREEN}✓ Update erfolgreich!${NC}"
echo "================================================"
echo ""
echo "Backup gespeichert in: $backup_dir"
echo ""
