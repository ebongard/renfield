#!/bin/bash

# Renfield GitHub Deploy Script
# Commits and pushes every file to the GitHub repository

set -e

# Change to project root directory
cd "$(dirname "$0")/.."

echo "🚀 Renfield GitHub Deployment"
echo "=============================="
echo ""

# Farben
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Check that we are inside a git repository
if [ ! -d .git ]; then
    echo -e "${YELLOW}Initialisiere Git Repository...${NC}"
    git init
    git remote add origin https://github.com/ebongard/renfield.git
fi

# Show the status
echo -e "${BLUE}Git Status:${NC}"
git status
echo ""

# Add every file
echo -e "${YELLOW}Füge Dateien hinzu...${NC}"
git add .

# Show what was added
echo ""
echo -e "${BLUE}Dateien die committed werden:${NC}"
git diff --cached --name-only
echo ""

# Create the commit
read -p "Commit Message (Enter für default): " commit_msg
if [ -z "$commit_msg" ]; then
    commit_msg="Initial commit: Complete Renfield AI Assistant system

- Backend: FastAPI with Ollama, Whisper, Piper
- Frontend: React PWA with voice interface
- Integrations: Home Assistant, Frigate, n8n
- Docker Compose setup
- Complete documentation"
fi

echo ""
echo -e "${YELLOW}Erstelle Commit...${NC}"
git commit -m "$commit_msg"

# Set the branch
echo ""
echo -e "${YELLOW}Setze Main Branch...${NC}"
git branch -M main

# Push
echo ""
echo -e "${YELLOW}Pushe zu GitHub...${NC}"
echo "Repository: https://github.com/ebongard/renfield"
echo ""

# Check whether the remote already exists
if git remote | grep -q origin; then
    echo "Remote 'origin' existiert bereits"
else
    git remote add origin https://github.com/ebongard/renfield.git
fi

# Carry out the push
git push -u origin main

echo ""
echo -e "${GREEN}================================${NC}"
echo -e "${GREEN}✓ Deployment erfolgreich!${NC}"
echo -e "${GREEN}================================${NC}"
echo ""
echo "🌐 Repository: https://github.com/ebongard/renfield"
echo "📝 Commit: $(git log -1 --oneline)"
echo ""
