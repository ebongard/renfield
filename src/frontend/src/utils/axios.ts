import axios, { AxiosInstance, InternalAxiosRequestConfig, AxiosResponse, AxiosError } from 'axios';

import { getApiBaseUrl } from './env';

// Axios Instance mit Base URL
const apiClient: AxiosInstance = axios.create({
  baseURL: getApiBaseUrl(),
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  }
});

// Request Interceptor
apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // Hier könnte Auth-Token hinzugefügt werden
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
