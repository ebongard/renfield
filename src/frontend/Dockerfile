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

# Set environment variables for build
ENV VITE_API_URL=${VITE_API_URL}
ENV VITE_WS_URL=${VITE_WS_URL}

# App Code kopieren
COPY . .

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
