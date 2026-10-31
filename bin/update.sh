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
echo "💾 Creating backup..."
timestamp=$(date +%Y%m%d_%H%M%S)
backup_dir="backups/backup_$timestamp"
mkdir -p "$backup_dir"

# Database backup
echo "  → Database..."
docker exec renfield-postgres pg_dump -U renfield renfield > "$backup_dir/database.sql"

# .env Backup
echo "  → Configuration..."
cp .env "$backup_dir/.env"

echo -e "${GREEN}✓${NC} Backup created in $backup_dir"
echo ""

# Git pull (if this is a git repository)
if [ -d .git ]; then
    echo "📥 Fetching updates..."
    git pull
    echo -e "${GREEN}✓${NC} Code updated"
    echo ""
fi

# Rebuild the containers
echo "🔨 Rebuilding containers..."
docker-compose build

# Restart the containers
echo "🔄 Restarting containers..."
docker-compose down
docker-compose up -d

echo ""
echo "⏳ Waiting for services..."
sleep 15

# Health Check
if curl -s -f -o /dev/null "http://localhost:8000/health"; then
    echo -e "${GREEN}✓${NC} Backend is online"
else
    echo -e "${RED}✗${NC} Backend is not reachable"
    echo "Roll back with: ./bin/rollback.sh $backup_dir"
    exit 1
fi

if curl -s -f -o /dev/null "http://localhost:3000"; then
    echo -e "${GREEN}✓${NC} Frontend is online"
else
    echo -e "${RED}✗${NC} Frontend is not reachable"
fi

echo ""
echo "================================================"
echo -e "${GREEN}✓ Update successful!${NC}"
echo "================================================"
echo ""
echo "Backup saved in: $backup_dir"
echo ""
