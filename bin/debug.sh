#!/bin/bash

# Renfield Debug Script
# Shows the status and logs of every container

# Change to project root directory
cd "$(dirname "$0")/.."

echo "🔍 Renfield Debug Info"
echo "======================="
echo ""

# Container Status
echo "📊 Container Status:"
docker-compose ps
echo ""

# Frontend Logs
echo "📱 Frontend logs (last 50 lines):"
echo "-----------------------------------"
docker-compose logs --tail=50 frontend
echo ""

# Backend Logs
echo "🔧 Backend logs (last 30 lines):"
echo "-----------------------------------"
docker-compose logs --tail=30 backend
echo ""

# Ollama Logs
echo "🤖 Ollama logs (last 20 lines):"
echo "-----------------------------------"
docker-compose logs --tail=20 ollama
echo ""

# Check the network
echo "🌐 Network ports:"
echo "-----------------------------------"
docker-compose ps | grep -E "PORTS|frontend|backend"
echo ""

# In Frontend Container reinschauen
echo "📂 Frontend container contents:"
echo "-----------------------------------"
docker-compose exec -T frontend ls -la /app 2>/dev/null || echo "Frontend container not reachable"
echo ""

echo "✅ Debug info complete"
echo ""
echo "💡 Tips:"
echo "  - Rebuild the frontend: docker-compose build frontend"
echo "  - Restart the frontend: docker-compose restart frontend"
echo "  - Restart everything: docker-compose restart"
