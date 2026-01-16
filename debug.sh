#!/bin/bash

# Renfield Debug Script
# Zeigt Status und Logs aller Container

echo "🔍 Renfield Debug Info"
echo "======================="
echo ""

# Container Status
echo "📊 Container Status:"
docker-compose ps
echo ""

# Frontend Logs
echo "📱 Frontend Logs (letzte 50 Zeilen):"
echo "-----------------------------------"
docker-compose logs --tail=50 frontend
echo ""

# Backend Logs
echo "🔧 Backend Logs (letzte 30 Zeilen):"
echo "-----------------------------------"
docker-compose logs --tail=30 backend
echo ""

# Ollama Logs
echo "🤖 Ollama Logs (letzte 20 Zeilen):"
echo "-----------------------------------"
docker-compose logs --tail=20 ollama
echo ""

# Netzwerk prüfen
echo "🌐 Netzwerk-Ports:"
echo "-----------------------------------"
docker-compose ps | grep -E "PORTS|frontend|backend"
echo ""

# In Frontend Container reinschauen
echo "📂 Frontend Container Inhalt:"
echo "-----------------------------------"
docker-compose exec -T frontend ls -la /app 2>/dev/null || echo "Frontend Container nicht erreichbar"
echo ""

echo "✅ Debug-Info komplett"
echo ""
echo "💡 Tipps:"
echo "  - Frontend neu bauen: docker-compose build frontend"
echo "  - Frontend neu starten: docker-compose restart frontend"
echo "  - Alle neu starten: docker-compose restart"
