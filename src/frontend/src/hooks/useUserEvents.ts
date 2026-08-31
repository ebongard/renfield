import { useEffect } from 'react';
import { useQueryClient } from '@tanstack/react-query';

import { keys } from '../api/keys';
import { getWebSocketUrl } from '../utils/env';
import { fetchWsToken } from '../utils/wsToken';

/**
 * App-wide per-user live-event socket (`/ws/user`).
 *
 * The server pushes CONTENT-FREE events (`{type, reason}`, no document identity)
 * when the user's corpus changes server-side (folder/email ingest completion,
 * Paperless/Simba filing) — so open KB surfaces refetch WITHOUT polling and
 * WITHOUT a manual reload. The socket is inbound-idle; we send a periodic
 * heartbeat only to keep the connection past reverse-proxy idle timeouts.
 *
 * Design: docs/design/user-events-ws.md. Mounted once in AppRoutes, gated so it
 * connects only when the feature is on AND (auth is off OR the user is logged
 * in) — never on the login/unauth surface (which would just reconnect-loop).
 */

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;
const HEARTBEAT_MS = 25_000; // < the typical 60s ingress idle timeout
const INVALIDATE_DEBOUNCE_MS = 1000; // collapse a burst of events into one refetch

interface UseUserEventsOptions {
  /** Connect only when true (feature on AND auth-off-or-logged-in). */
  enabled: boolean;
}

export function useUserEvents({ enabled }: UseUserEventsOptions): void {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled) return;

    let ws: WebSocket | null = null;
    let attempt = 0;
    let intentionalClose = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
    let debounceTimer: ReturnType<typeof setTimeout> | null = null;

    const clearHeartbeat = () => {
      if (heartbeatTimer) {
        clearInterval(heartbeatTimer);
        heartbeatTimer = null;
      }
    };

    const invalidateDocumentsSoon = () => {
      // Client-side debounce (defense in depth with the server-side coalescer):
      // a folder-ingest backlog of N completions → ONE refetch, not N.
      if (debounceTimer) return;
      debounceTimer = setTimeout(() => {
        debounceTimer = null;
        // Narrow to the documents LIST key (design §4/T7 blast-radius decision):
        // the page query is [...keys.knowledge.list(), {filter}], so this prefix
        // matches it without churning detail/bases/stats.
        void queryClient.invalidateQueries({ queryKey: keys.knowledge.list() });
      }, INVALIDATE_DEBOUNCE_MS);
    };

    const handleEvent = (payload: unknown) => {
      if (!payload || typeof payload !== 'object') return;
      const type = (payload as { type?: string }).type;
      if (type === 'documents_changed') {
        invalidateDocumentsSoon();
      }
      // Future event types (obligations_changed, notes_changed, …) add a case
      // here that invalidates their own query key — no new socket needed.
    };

    const scheduleReconnect = () => {
      const base = Math.min(MAX_BACKOFF_MS, INITIAL_BACKOFF_MS * 2 ** attempt);
      // Jitter so a fleet-wide backend rollout doesn't produce a synchronized
      // reconnect storm (tolerant of the transient WS 404s during a deploy).
      const delay = base * (0.5 + Math.random() * 0.5);
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        void connect();
      }, delay);
    };

    const connect = async () => {
      if (intentionalClose) return;
      try {
        let url = getWebSocketUrl().replace(/\/ws$/, '') + '/ws/user';
        // Short-lived WS-scoped token (null when auth is off → open without it;
        // the backend registers the socket under the household broadcast bucket).
        const token = await fetchWsToken();
        if (intentionalClose) return;
        if (token) url += `?token=${token}`;

        ws = new WebSocket(url);
        ws.onopen = () => {
          attempt = 0;
          clearHeartbeat();
          heartbeatTimer = setInterval(() => {
            try {
              ws?.send('ping');
            } catch {
              /* a failed send surfaces via onclose */
            }
          }, HEARTBEAT_MS);
        };
        ws.onmessage = (event) => {
          try {
            handleEvent(JSON.parse(event.data));
          } catch {
            /* ignore malformed frames */
          }
        };
        ws.onclose = () => {
          clearHeartbeat();
          if (!intentionalClose) scheduleReconnect();
        };
        ws.onerror = () => {
          try {
            ws?.close();
          } catch {
            /* onclose handles reconnect */
          }
        };
      } catch {
        if (!intentionalClose) scheduleReconnect();
      }
    };

    void connect();

    return () => {
      intentionalClose = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (debounceTimer) clearTimeout(debounceTimer);
      clearHeartbeat();
      try {
        ws?.close();
      } catch {
        /* ignore */
      }
      ws = null;
    };
  }, [enabled, queryClient]);
}

export default useUserEvents;
