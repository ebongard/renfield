# ============================================================================
# Renfield Frontend - Multi-Stage Docker Build
# ============================================================================
# Stages:
#   1. base        - Install dependencies (shared)
#   2. development - Vite dev server with hot reload
#   3. build       - Compile production assets
#   4. production  - Nginx serving static files
#
# Usage:
#   Development: docker build --target development -t renfield-frontend:dev .
#   Production:  docker build --target production -t renfield-frontend:prod .
# ============================================================================

# ============================================================================
# Stage 1: Base - Install dependencies
# ============================================================================
FROM node:20-alpine AS base

WORKDIR /app

# Package files kopieren
COPY package*.json ./

# Dependencies installieren
RUN npm ci

# ============================================================================
# Stage 2: Development - Vite dev server with hot reload
# ============================================================================
FROM base AS development

# App Code kopieren
COPY . .

# Port exposieren
EXPOSE 3000

# Development Server starten
CMD ["npm", "run", "dev", "--", "--host"]

# ============================================================================
# Stage 3: Build - Compile production assets
# ============================================================================
FROM base AS build

# Build arguments for environment configuration
ARG VITE_API_URL=
ARG VITE_WS_URL=/ws
# White-label branding — defaults preserve the Renfield identity.
# Plugins (e.g. Reva) override these at build time:
#   docker build --build-arg VITE_APP_NAME=Reva \
#                --build-arg VITE_APP_LOGO_URL=/reva-logo.png ...
# The logo file must already be present under src/frontend/public/.
ARG VITE_APP_NAME=Renfield
ARG VITE_APP_LOGO_URL=
# JSON array of starter prompts shown on an empty chat page. Plugins
# override per deployment, e.g.
#   --build-arg VITE_CHAT_STARTERS='["Status REL-100","Open Jira tickets"]'
# Defaults to i18n examples (weather/light/music) when unset.
ARG VITE_CHAT_STARTERS=
# Phase B streaming voice path. Set to "true" to activate the
# /ws/voice WebSocket UI (live partial transcripts, sub-second TTS
# first-byte). Default unset = legacy REST path (request-response).
ARG VITE_FEATURE_VOICE_STREAM=
# Edition selector for translation overlays. "community" (default) keeps
# the household-flavored Circles vocabulary (tier 2 = "Household",
# settings = "Circles & Members"). "pro" loads the de.pro.json /
# en.pro.json overlays which substitute enterprise-friendly vocabulary
# (tier 2 = "Team", settings = "Visibility & Members") for white-label
# multi-tenant deploys (e.g. Reva for X-IDRA Systems). See
# src/i18n/index.ts deepMerge for resolution.
ARG VITE_APP_EDITION=community
# Tenant name displayed on the login page hero ("Sign in to {tenant}").
# Pro edition only — empty in community deploys. White-label deploys set
# this to the customer organization name (e.g. "X-IDRA Systems"). See
# pages/LoginPage.tsx for the render logic.
ARG VITE_APP_TENANT_NAME=

# Set environment variables for build
ENV VITE_API_URL=${VITE_API_URL}
ENV VITE_WS_URL=${VITE_WS_URL}
ENV VITE_APP_NAME=${VITE_APP_NAME}
ENV VITE_APP_LOGO_URL=${VITE_APP_LOGO_URL}
ENV VITE_CHAT_STARTERS=${VITE_CHAT_STARTERS}
ENV VITE_FEATURE_VOICE_STREAM=${VITE_FEATURE_VOICE_STREAM}
ENV VITE_APP_EDITION=${VITE_APP_EDITION}
ENV VITE_APP_TENANT_NAME=${VITE_APP_TENANT_NAME}

# App Code kopieren
COPY . .

# Copy ONNX Runtime WASM files to public directory
# (postinstall ran before COPY, so public/ort/ was missed)
RUN mkdir -p public/ort && \
    cp node_modules/onnxruntime-web/dist/ort-wasm*.wasm \
       node_modules/onnxruntime-web/dist/ort-wasm*.mjs \
       public/ort/ 2>/dev/null || true

# Production build erstellen
RUN npm run build

# ============================================================================
# Stage 4: Production - Nginx serving static files
# ============================================================================
FROM nginx:1.28-alpine AS production

# Nginx configuration for SPA routing
RUN rm /etc/nginx/conf.d/default.conf

# Copy custom nginx config
COPY nginx.conf /etc/nginx/conf.d/default.conf

# Copy built assets from build stage
COPY --from=build /app/dist /usr/share/nginx/html

# Expose port
EXPOSE 80

# Health check using nginx pid (no curl/wget needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD test -f /var/run/nginx.pid && kill -0 $(cat /var/run/nginx.pid)

# Run nginx
CMD ["nginx", "-g", "daemon off;"]
