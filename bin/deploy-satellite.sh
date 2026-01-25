#!/bin/bash
# Deploy satellite code to Raspberry Pi

# Change to project root directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

SATELLITE_HOST="${1:-satellite-wohnzimmer.local}"
SATELLITE_USER="${2:-evdb}"
SATELLITE_PATH="/opt/renfield-satellite"

echo "🛰️ Deploying satellite code to $SATELLITE_HOST..."

# Sync satellite source code
rsync -avz --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.git' \
  --exclude 'venv' \
  --exclude '.pytest_cache' \
  "$PROJECT_ROOT/src/satellite/renfield_satellite/" \
  "$SATELLITE_USER@$SATELLITE_HOST:$SATELLITE_PATH/renfield_satellite/"

if [ $? -eq 0 ]; then
  echo "✅ Files synced successfully"
  
  # Restart the satellite service
  echo "🔄 Restarting satellite service..."
  ssh "$SATELLITE_USER@$SATELLITE_HOST" "sudo systemctl restart renfield-satellite"
  
  if [ $? -eq 0 ]; then
    echo "✅ Satellite service restarted"
    echo "📊 Checking service status..."
    ssh "$SATELLITE_USER@$SATELLITE_HOST" "sudo systemctl status renfield-satellite --no-pager | head -20"
  else
    echo "❌ Failed to restart service"
    exit 1
  fi
else
  echo "❌ Failed to sync files"
  exit 1
fi
