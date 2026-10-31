#!/usr/bin/env bash
# generate-secrets.sh -- generates secure secret files for Docker Compose secrets
#
# Usage: ./bin/generate-secrets.sh
#
# Creates the secrets/ directory with these files:
#   postgres_password, secret_key, default_admin_password (auto-generated)
#   home_assistant_token, openweather_api_key, newsapi_key, jellyfin_api_key (interactive)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SECRETS_DIR="$PROJECT_DIR/secrets"

echo "=== Renfield Secrets Generator ==="
echo ""

# Create the secrets directory
mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# Helper: generate a secret (skips when one is already there)
generate_secret() {
    local name="$1"
    local length="$2"
    local file="$SECRETS_DIR/$name"

    if [ -f "$file" ]; then
        echo "  [SKIP] $name (already exists)"
        return
    fi

    openssl rand -base64 "$length" | tr -d '\n' > "$file"
    chmod 600 "$file"
    echo "  [OK]   $name (generated)"
}

# Helper: generate a URL-safe token
generate_token() {
    local name="$1"
    local length="$2"
    local file="$SECRETS_DIR/$name"

    if [ -f "$file" ]; then
        echo "  [SKIP] $name (already exists)"
        return
    fi

    python3 -c "import secrets; print(secrets.token_urlsafe($length), end='')" > "$file"
    chmod 600 "$file"
    echo "  [OK]   $name (generated)"
}

# Helper: interactive input.
# Empty input -> an empty placeholder file is created. That keeps
# `docker compose -f docker-compose.prod*.yml up` able to start even when an
# optional integration secret (Jellyfin, Presence, ...) is not configured.
# Pydantic's `SecretStr | None` reads an empty file as falsy and the feature
# disables itself.
prompt_secret() {
    local name="$1"
    local description="$2"
    local file="$SECRETS_DIR/$name"

    if [ -f "$file" ]; then
        echo "  [SKIP] $name (already exists)"
        return
    fi

    echo ""
    echo "  $description"
    read -rp "  $name: " value

    if [ -z "$value" ]; then
        : > "$file"
        chmod 600 "$file"
        echo "  [OK]   $name (empty — placeholder, feature stays disabled)"
        return
    fi

    printf '%s' "$value" > "$file"
    chmod 600 "$file"
    echo "  [OK]   $name (saved)"
}

echo "1. Auto-generated secrets:"
echo ""
generate_secret "postgres_password" 32
generate_token "secret_key" 64
generate_secret "default_admin_password" 24

echo ""
echo "2. External API keys (interactive input):"
echo "   (press Enter to skip)"

prompt_secret "home_assistant_token" "Home Assistant Long-Lived Access Token:"
prompt_secret "openweather_api_key" "OpenWeatherMap API Key (https://openweathermap.org/api):"
prompt_secret "newsapi_key" "NewsAPI Key (https://newsapi.org/):"
prompt_secret "jellyfin_api_key" "Jellyfin API Key:"
prompt_secret "jellyfin_token" "Jellyfin Access Token (optional, empty = skip):"
prompt_secret "jellyfin_base_url" "Jellyfin Base URL (e.g. http://jellyfin.local:8096):"
prompt_secret "jellyfin_user_id" "Jellyfin User ID (GUID):"
prompt_secret "n8n_api_key" "n8n API Key (Settings → API → Create API Key):"
prompt_secret "paperless_api_token" "Paperless-NGX API Token:"
prompt_secret "mail_primary_password" "Mail primary account IMAP/SMTP password:"

echo ""
echo "3. Auto-generated shared secrets (webhooks, etc.):"
generate_token "presence_webhook_secret" 32

echo ""
echo "=== Done ==="
echo ""
echo "Secrets saved in: $SECRETS_DIR/"
echo ""
echo "Next steps:"
echo "  1. Remove secrets from .env (POSTGRES_PASSWORD, HOME_ASSISTANT_TOKEN, SECRET_KEY, etc.)"
echo "  2. Start the stack: docker compose -f docker-compose.prod.yml up -d"
echo "  3. Health Check: curl -sk https://localhost/health"
echo ""
echo "Note: secrets can be regenerated at any time (existing ones are skipped)."
echo "         To overwrite: rm secrets/<name> && ./bin/generate-secrets.sh"
