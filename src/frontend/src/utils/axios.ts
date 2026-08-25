import axios, { AxiosInstance, InternalAxiosRequestConfig, AxiosResponse, AxiosError } from 'axios';

import { getApiBaseUrl } from './env';
import { ACCESS_TOKEN_KEY } from './authTokens';

// JS-readable double-submit CSRF cookie name (must match backend csrf_cookie_name).
const CSRF_COOKIE_NAME = 'renfield_csrf';
const MUTATING_METHODS = new Set(['post', 'put', 'patch', 'delete']);

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
  return match ? decodeURIComponent(match[1]) : null;
}

// Axios Instance mit Base URL
const apiClient: AxiosInstance = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 30000,
  // Send the HttpOnly session cookie on every request (JWT cookie migration).
  // Harmless in the localStorage-Bearer era: no cookie is set until the backend
  // auth_cookie_enabled flag is on, so nothing is sent.
  withCredentials: true,
  headers: {
    'Content-Type': 'application/json',
  }
});

// Request Interceptor — attach the bearer token from localStorage on EVERY
// request. This is registered at module scope (when apiClient is created,
// before any React component mounts), so it CANNOT race the app's first
// authenticated queries. Previously the token was attached only by an
// interceptor AuthContext registered inside a useEffect; a child component's
// query effect (e.g. useFeatureFlags → /api/config/features) runs before the
// parent AuthContext's effect, so on an auth-on instance with a token already
// in localStorage that first request went out unauthenticated → 401 → and
// because react-query does not retry 401s, the feature flags cached as `false`
// (hiding e.g. the Meetings/Wissen nav). Reading from localStorage here removes
// that ordering dependency entirely.
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // Bearer from localStorage — KEPT during the cookie transition so the Reva
    // fragment→localStorage path and any pre-cutover client still authenticate.
    // When cookie mode is on the backend reads the cookie first; the header is
    // a harmless no-op (no token stored).
    const token = localStorage.getItem(ACCESS_TOKEN_KEY);
    if (token && !config.headers.Authorization) {
      config.headers.Authorization = `Bearer ${token}`;
    }
    // Double-submit CSRF: echo the JS-readable csrf cookie on mutating requests.
    // No-op when the cookie is absent (Bearer/household → CSRF middleware exempt).
    if (MUTATING_METHODS.has((config.method || 'get').toLowerCase())) {
      const csrf = readCookie(CSRF_COOKIE_NAME);
      if (csrf && !config.headers['X-CSRF-Token']) {
        config.headers['X-CSRF-Token'] = csrf;
      }
    }
    return config;
  },
  (error: AxiosError) => {
    return Promise.reject(error);
  }
);

// Response Interceptor
apiClient.interceptors.response.use(
  (response: AxiosResponse) => {
    return response;
  },
  (error: AxiosError) => {
    // Globale Error-Behandlung
    console.error('API Error:', error);
    return Promise.reject(error);
  }
);

/**
 * Extract per-field validation errors from a Pydantic 422 response.
 * Returns a map of field name → error message. Empty {} for non-field errors.
 */
export function extractFieldErrors(err: unknown): Record<string, string> {
  const resp = (err as AxiosError<{ detail?: unknown }>)?.response;
  const detail = resp?.data?.detail;

  // Pydantic 422: detail is an array of { loc, msg, type }
  if (resp?.status === 422 && Array.isArray(detail)) {
    const fields: Record<string, string> = {};
    for (const d of detail) {
      const loc = d.loc as string[] | undefined;
      const msg = d.msg as string | undefined;
      if (loc && msg) {
        // loc is e.g. ["body", "username"] — take the last element as field name
        const fieldName = loc[loc.length - 1];
        if (fieldName && fieldName !== 'body') {
          fields[fieldName] = msg;
        }
      }
    }
    if (Object.keys(fields).length > 0) return fields;
  }

  // Non-422 string detail with field name hints
  if (typeof detail === 'string') {
    const lower = detail.toLowerCase();
    if (lower.includes('username')) return { username: detail };
    if (lower.includes('email')) return { email: detail };
    if (lower.includes('password')) return { password: detail };
  }

  return {};
}

/**
 * Extract a displayable error message from an Axios error.
 * Handles both simple string details and Pydantic 422 validation arrays.
 */
export function extractApiError(err: unknown, fallback: string): string {
  const detail = (err as AxiosError<{ detail?: unknown }>)?.response?.data?.detail;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join(', ');
  }
  return fallback;
}

export default apiClient;
