/**
 * Environment-variable resolution with explicit warnings when fallbacks kick in.
 *
 * Vite injects build-time env vars via `import.meta.env`. When `VITE_API_URL` or
 * `VITE_WS_URL` is unset, every consumer used to silently fall back to
 * `http://localhost:8000` — fine for `npm run dev` but a silent footgun in any
 * other deployment (containerized prod, mobile build, served-from-nginx).
 *
 * These helpers centralize the fallback and warn — at error level in PROD
 * builds, at warn level in DEV — so a misconfigured deploy is visible in the
 * browser console instead of looking like a backend that's just unreachable.
 *
 * Each helper warns at most once per page load.
 */

const FALLBACK_API_URL = 'http://localhost:8000';
// VITE_WS_URL convention (per .env.example + docker-compose.*.yml): the env
// value includes the `/ws` path suffix. The Dockerfile appends `/ws` to
// EXTERNAL_WS_URL automatically. Consumers that need a different endpoint
// (e.g. /ws/device) strip the trailing `/ws` and append their own path.
const FALLBACK_WS_URL = 'ws://localhost:8000/ws';

const _warned = new Set<string>();

function warnFallback(varName: string, fallbackValue: string): void {
  if (_warned.has(varName)) return;
  _warned.add(varName);
  const message =
    `[env] ${varName} is not set; falling back to ${fallbackValue}. ` +
    `This is expected for \`npm run dev\` but indicates a misconfigured build for any other deployment.`;
  if (import.meta.env.PROD) {
    console.error(message);
  } else {
    console.warn(message);
  }
}

/**
 * REST API base URL for the Renfield backend.
 *
 * - DEV (`npm run dev`): falls back to `http://localhost:8000` with a console warning.
 * - PROD (built bundle) without `VITE_API_URL`: returns `""` so axios uses
 *   relative URLs against the same origin. Standard same-origin reverse-proxy
 *   deployment (Traefik routes `/api/*` to backend, `/` to frontend) just works
 *   without any build-arg. Set `VITE_API_URL` only if the API is hosted on a
 *   different origin.
 */
export function getApiBaseUrl(): string {
  const value = import.meta.env.VITE_API_URL as string | undefined;
  if (value && value.length > 0) return value;
  if (import.meta.env.PROD) return '';
  warnFallback('VITE_API_URL', FALLBACK_API_URL);
  return FALLBACK_API_URL;
}

/**
 * WebSocket URL for the Renfield backend (includes `/ws` path per convention).
 * Falls back to `ws://localhost:8000/ws` when `VITE_WS_URL` is unset, with a
 * console warning.
 *
 * Consumers that need a different endpoint (e.g. `/ws/device`) should strip
 * the trailing `/ws` from the return value and append their own path —
 * `useDeviceConnection.getWsUrl()` is the canonical example.
 */
export function getWebSocketUrl(): string {
  const value = import.meta.env.VITE_WS_URL as string | undefined;
  if (value && value.length > 0) return value;
  warnFallback('VITE_WS_URL', FALLBACK_WS_URL);
  return FALLBACK_WS_URL;
}
