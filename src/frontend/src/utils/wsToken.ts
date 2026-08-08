import apiClient from './axios';

/**
 * Fetch a SHORT-LIVED, WS-scoped access token for a WebSocket handshake
 * (security audit M2).
 *
 * The browser passes the WS auth token as `?token=` on the WebSocket URL, where
 * it lands in reverse-proxy access logs / history / Referer. Handing over the
 * full 24h API access JWT there was the vulnerability. Instead we fetch a
 * dedicated `scope:"ws"` token from `/api/ws/token` that lives ~90s and is
 * rejected by the REST API — so even if harvested from a log it is useless.
 *
 * Returns `null` when WS auth is disabled (the household instance — the endpoint
 * returns `{token: null}`) or on any error; the caller then opens the socket
 * WITHOUT a token (the backend skips auth when `WS_AUTH_ENABLED` is off, or
 * rejects and the caller retries on its normal reconnect path). We deliberately
 * do NOT fall back to the long-lived localStorage access token — that would
 * re-introduce the full-JWT-in-URL exposure this change closes.
 */
export async function fetchWsToken(): Promise<string | null> {
  try {
    const res = await apiClient.post('/api/ws/token');
    return res.data?.token ?? null;
  } catch {
    return null;
  }
}
