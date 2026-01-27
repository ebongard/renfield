#!/usr/bin/env bash
# generate-secrets.sh — Generiert sichere Secret-Dateien für Docker Compose Secrets
#
# Nutzung: ./bin/generate-secrets.sh
#
# Erstellt das secrets/ Verzeichnis mit folgenden Dateien:
#   postgres_password, secret_key, default_admin_password (auto-generiert)
#   home_assistant_token, openweather_api_key, newsapi_key, jellyfin_api_key (interaktiv)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SECRETS_DIR="$PROJECT_DIR/secrets"

echo "=== Renfield Secrets Generator ==="
echo ""

# Secrets-Verzeichnis erstellen
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# Hilfsfunktion: Secret generieren (überspringt wenn bereits vorhanden)
generate_secret() {
    local name="$1"
    local length="$2"
    local file="$SECRETS_DIR/$name"

    if [ -f "$file" ]; then
        echo "  [SKIP] $name (existiert bereits)"
        return
    fi

    openssl rand -base64 "$length" | tr -d '\n' > "$file"
    chmod 600 "$file"
    echo "  [OK]   $name (generiert)"
}

# Hilfsfunktion: URL-safe Token generieren
generate_token() {
    local name="$1"
    local length="$2"
    local file="$SECRETS_DIR/$name"

    if [ -f "$file" ]; then
        echo "  [SKIP] $name (existiert bereits)"
        return
    fi

    python3 -c "import secrets; print(secrets.token_urlsafe($length), end='')" > "$file"
    chmod 600 "$file"
    echo "  [OK]   $name (generiert)"
}

# Hilfsfunktion: Interaktive Eingabe
prompt_secret() {
    local name="$1"
    local description="$2"
    local file="$SECRETS_DIR/$name"

    if [ -f "$file" ]; then
        echo "  [SKIP] $name (existiert bereits)"
        return
    fi

    echo ""
    echo "  $description"
    read -rp "  $name: " value

    if [ -z "$value" ]; then
        echo "  [SKIP] $name (leer, übersprungen)"
        return
    fi

    printf '%s' "$value" > "$file"
    chmod 600 "$file"
    echo "  [OK]   $name (gespeichert)"
}

echo "1. Auto-generierte Secrets:"
echo ""
generate_secret "postgres_password" 32
generate_token "secret_key" 64
generate_secret "default_admin_password" 24

echo ""
echo "2. Externe API-Keys (interaktive Eingabe):"
echo "   (Enter drücken um zu überspringen)"

prompt_secret "home_assistant_token" "Home Assistant Long-Lived Access Token:"
prompt_secret "openweather_api_key" "OpenWeatherMap API Key (https://openweathermap.org/api):"
prompt_secret "newsapi_key" "NewsAPI Key (https://newsapi.org/):"
prompt_secret "jellyfin_api_key" "Jellyfin API Key:"

echo ""
echo "=== Fertig ==="
echo ""
echo "Secrets gespeichert in: $SECRETS_DIR/"
echo ""
echo "Nächste Schritte:"
echo "  1. Secrets aus .env entfernen (POSTGRES_PASSWORD, HOME_ASSISTANT_TOKEN, SECRET_KEY, etc.)"
echo "  2. Stack starten: docker compose -f docker-compose.prod.yml up -d"
echo "  3. Health Check: curl -sk https://localhost/health"
echo ""
echo "Hinweis: Secrets können jederzeit neu generiert werden (vorhandene werden übersprungen)."
echo "         Zum Überschreiben: rm secrets/<name> && ./bin/generate-secrets.sh"
