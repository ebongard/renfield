// usePresenceReadings — the PUSH data source for the "Readings" tab on
// /admin/presence.
//
// Opens the ADMIN-gated `/ws/presence-debug` socket (backend
// ha_glue/api/websocket/presence_debug_handler.py). The server sends one full
// `snapshot` on connect and a fresh one after satellite BLE reports (at most
// one per second), so this hook holds no timer that fetches data: the only
// timer here is the reconnect backoff. Every (re)connect re-hydrates from a
// fresh snapshot.
//
// The socket lives exactly as long as the component using the hook is mounted;
// the backend does no diagnostic work while no viewer is connected.
import { useEffect, useRef, useState } from 'react';

import { debug } from '../utils/debug';
import { getWebSocketUrl } from '../utils/env';
import { fetchWsToken } from '../utils/wsToken';

// ---- wire shapes (ha_glue/services/presence_debug.py) ----------------------

export interface PresenceReadingSettings {
  /** Readings weaker than this are ignored by the room decision. */
  rssi_floor_dbm: number;
  /** dB a challenger's filtered value must beat the current room by. */
  switch_enter_margin_db: number;
  /** Seconds a reading stays in the window / until a person counts as absent. */
  stale_timeout_s: number;
  /** False = legacy scan-count hysteresis: no filtered values, no margin. */
  rssi_filter_enabled: boolean;
  /** Seconds a voice/auth sighting pins the room against BLE (0 = off). */
  voice_hold_s: number;
  /** dB above the floor a room needs to take a user out of a voice/auth room
   *  the phone was never heard in (0 = off). */
  voice_release_margin_db: number;
}

export interface PresenceReadingSatellite {
  satellite_id: string;
  room_id: number | null;
  room_name: string | null;
}

export interface PresenceSighting {
  satellite_id: string;
  room_id: number | null;
  room_name: string | null;
  rssi: number;
  /** Epoch seconds. */
  timestamp: number;
  /** Seconds before the snapshot's `now` (server clock on both ends). */
  age_s: number;
}

export interface PresenceFilteredRoom {
  room_id: number;
  room_name: string | null;
  rssi: number;
}

export interface PresenceReadingDevice {
  /** Redacted `mac:<hash>` / `irk:<hash>` — never the raw address or label. */
  key: string;
  user_id: number | null;
  user_name: string | null;
  current_room_id: number | null;
  current_room_name: string | null;
  current_confidence: number | null;
  /** Seconds BLE still cannot move the user; null when no hold is running. */
  voice_hold_remaining_s: number | null;
  /** The room rests on a voice/auth sighting BLE has not replaced yet. */
  voice_pinned: boolean;
  /** The room is a held guess: nothing has placed the user there for a while. */
  location_uncertain: boolean;
  filtered_rooms: PresenceFilteredRoom[];
  sightings: PresenceSighting[];
}

export interface PresenceReadingsSnapshot {
  /** Epoch seconds of the snapshot (server clock). */
  now: number;
  enabled: boolean;
  settings: PresenceReadingSettings;
  satellites: PresenceReadingSatellite[];
  devices: PresenceReadingDevice[];
}

interface SnapshotMessage extends PresenceReadingsSnapshot {
  type: 'snapshot';
}

function isSnapshot(msg: unknown): msg is SnapshotMessage {
  if (typeof msg !== 'object' || msg === null) return false;
  const candidate = msg as Partial<SnapshotMessage>;
  return (
    candidate.type === 'snapshot' &&
    typeof candidate.now === 'number' &&
    typeof candidate.settings === 'object' &&
    candidate.settings !== null &&
    Array.isArray(candidate.devices) &&
    Array.isArray(candidate.satellites)
  );
}

/** Wire → model, field by field. The voice-hold fields default to "none" so a
 *  tab loaded during a deploy window reads an older backend without breaking. */
function toSnapshot(msg: SnapshotMessage): PresenceReadingsSnapshot {
  return {
    now: msg.now,
    enabled: msg.enabled,
    settings: {
      ...msg.settings,
      voice_hold_s: msg.settings.voice_hold_s ?? 0,
      voice_release_margin_db: msg.settings.voice_release_margin_db ?? 0,
    },
    satellites: msg.satellites,
    devices: msg.devices.map((device) => ({
      ...device,
      voice_hold_remaining_s: device.voice_hold_remaining_s ?? null,
      voice_pinned: device.voice_pinned ?? false,
      location_uncertain: device.location_uncertain ?? false,
    })),
  };
}

// ---- connection lifecycle --------------------------------------------------

/** `connecting` = first attempt, nothing received yet; `live` = socket open;
 *  `reconnecting` = the socket dropped (or never opened) and a retry is due —
 *  the last snapshot, if any, is kept but no longer current; `denied` = the
 *  server refused the socket (not logged in, or no admin role) — final, no
 *  retry, and no snapshot is kept. */
export type PresenceReadingsStatus = 'connecting' | 'live' | 'reconnecting' | 'denied';

export interface PresenceReadingsState {
  snapshot: PresenceReadingsSnapshot | null;
  status: PresenceReadingsStatus;
}

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;

/** The server's refusal codes (backend `WSAuthError`: unauthorized, token
 *  expired, token invalid, auth required). It accepts the handshake before
 *  closing with one of them, so the browser sees the code instead of a bare
 *  1006. */
const AUTH_CLOSE_CODES: ReadonlySet<number> = new Set([4001, 4002, 4003, 4004]);

/** Normal closure. The server ends every socket after a maximum lifetime so
 *  the next handshake re-checks the admin role; that close is planned and gets
 *  an immediate, silent reconnect. */
const PLANNED_CLOSE_CODE = 1000;

async function readingsWsUrl(): Promise<string> {
  // getWebSocketUrl() returns `.../ws`; strip it and append our own path.
  let url = getWebSocketUrl().replace(/\/ws$/, '') + '/ws/presence-debug';
  // Short-lived, WS-scoped token (never the long-lived access JWT in a URL).
  // null → open without one: auth off, or a cookie session the server reads
  // from the handshake itself.
  const token = await fetchWsToken();
  if (token) url += `?token=${token}`;
  return url;
}

export function usePresenceReadings(): PresenceReadingsState {
  const [snapshot, setSnapshot] = useState<PresenceReadingsSnapshot | null>(null);
  const [status, setStatus] = useState<PresenceReadingsStatus>('connecting');

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const closedRef = useRef(false);

  useEffect(() => {
    closedRef.current = false;

    const scheduleReconnect = () => {
      if (closedRef.current || reconnectTimerRef.current) return;
      setStatus('reconnecting');
      const delay = Math.min(MAX_BACKOFF_MS, INITIAL_BACKOFF_MS * 2 ** attemptRef.current);
      attemptRef.current += 1;
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null;
        void connect();
      }, delay);
    };

    const connect = async () => {
      const url = await readingsWsUrl();
      // The effect may have torn down during the async token fetch.
      if (closedRef.current) return;

      let ws: WebSocket;
      try {
        ws = new WebSocket(url);
      } catch (err) {
        debug.log('Presence readings WS construct failed:', err);
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;
      // Whether THIS socket delivered data — a planned close only earns the
      // immediate reconnect from a socket that worked, so a server that closes
      // normally right after the handshake cannot drive a tight loop.
      let hydrated = false;

      ws.onopen = () => {
        attemptRef.current = 0;
        setStatus('live');
      };

      ws.onmessage = (event: MessageEvent) => {
        let msg: unknown;
        try {
          msg = JSON.parse(event.data as string);
        } catch {
          return; // a malformed frame must not crash the tab
        }
        if (isSnapshot(msg)) {
          hydrated = true;
          setSnapshot(toSnapshot(msg));
        }
      };

      ws.onclose = (event: CloseEvent) => {
        if (closedRef.current || wsRef.current !== ws) return;
        if (AUTH_CLOSE_CODES.has(event.code)) {
          // Refused: retrying cannot help, and readings received under a role
          // that is gone must not stay on screen.
          debug.log('Presence readings WS refused:', event.code);
          closedRef.current = true;
          wsRef.current = null;
          setSnapshot(null);
          setStatus('denied');
          return;
        }
        if (event.code === PLANNED_CLOSE_CODE && hydrated) {
          // Session renewal: reconnect now, keep "live" and the last snapshot.
          // A failing renewal falls into the ordinary backoff via its own close.
          void connect();
          return;
        }
        debug.log('Presence readings WS closed — scheduling reconnect');
        scheduleReconnect();
      };
    };

    void connect();

    return () => {
      closedRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  return { snapshot, status };
}
