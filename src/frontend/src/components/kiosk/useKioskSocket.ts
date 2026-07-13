// useKioskSocket — the PUSH data source for the /kiosk wall display.
//
// Opens the ADMIN-gated `/ws/kiosk` hub (backend api/websocket/kiosk_handler.py),
// hydrates from the one `snapshot` message it sends on connect, then folds each
// delta event into a single reducer-held `KioskLiveModel`. This replaces the
// kiosk's former react-query POLLING chain (useCommandCenterModel +
// useSatellites/Weather/NowPlaying queries) with a single event-driven socket —
// no browser timers hit our own REST API.
//
// LIVENESS IS BACKEND-AUTHORITATIVE (phase 2). The hub pushes a delta at every
// real mutation: `satellite_state` (voice state), `satellite_online`/
// `satellite_offline` (roster liveness — register / unregister / heartbeat
// timeout), `presence_changed`, `now_playing_changed`, `tool_health_changed`,
// `weather_updated`, plus `turn_activity` (the active-subsystem pulse). So the
// model NEVER decays frozen snapshot values against the wall clock: a satellite
// present in `satellites[]` IS online (the backend removed it via
// `satellite_offline` the moment it dropped), a peer's reachability is whatever
// the last snapshot said, and a reconnect re-anchors everything from a fresh
// snapshot. Unknown event `type`s are still tolerated so a later delta can ship
// on the backend without breaking an already-deployed kiosk tab.
import { useEffect, useReducer, useRef, useState } from 'react';

import { debug } from '../../utils/debug';
import { getWebSocketUrl } from '../../utils/env';
import type { NodeHealth, SatelliteState } from './types';
import type {
  AgentRoleInfo,
  KioskNowPlaying,
  KioskWeather,
  RoleActivityEntry,
} from '../../api/resources/kiosk';

// ---- snapshot section shapes (as the backend hub emits them) --------------

export interface KioskSatellite {
  satellite_id: string;
  room: string;
  room_id: number | null;
  state: SatelliteState;
  has_active_session?: boolean;
}

export interface KioskPresenceRoom {
  room_id: number;
  room_name: string | null;
  /** Occupant COUNT (content-free — no user ids). */
  occupants: number;
}

export interface KioskPresence {
  rooms: KioskPresenceRoom[];
  people_present: number;
  occupied_rooms: number;
}

export interface KioskMcpServer {
  name: string;
  connected: boolean;
  last_error?: string | null;
  tool_count: number;
  // Backend-folded connectivity+functionality health. Present since the
  // degraded-health change; when absent the model falls back to deriving health
  // from connectivity + tool-call success rate.
  health?: NodeHealth;
  // Stable machine code for WHY a node is degraded ('plugin_failed' | 'no_tools')
  // — the frontend localizes it (never a raw backend string, per the i18n rule).
  impaired_code?: string | null;
}

export interface KioskMcp {
  enabled: boolean;
  total_tools: number;
  servers: KioskMcpServer[];
}

export interface KioskToolHealth {
  tool_name: string;
  total: number;
  success_rate: number;
  degraded: boolean;
}

/** Health verdict for an internal-only subsystem (knowledge / presence / media)
 *  — the three synthetic pseudo-nodes. `health` mirrors the MCP-node axis;
 *  `impaired_code` is a machine reason the frontend localizes. */
export interface KioskInternalHealth {
  id: string;
  health: NodeHealth;
  impaired_code?: string | null;
}

/** Folded id → verdict map (built from the snapshot's `internal_health` array
 *  and each `internal_health_changed` delta). */
export type InternalHealthMap = Record<
  string,
  { health: NodeHealth; impaired_code?: string | null }
>;

/** Fold the wire array into the id-keyed map the model reads. */
function foldInternalHealth(list: KioskInternalHealth[] | undefined): InternalHealthMap {
  const map: InternalHealthMap = {};
  for (const entry of list ?? []) {
    if (entry && typeof entry.id === 'string' && entry.id) {
      map[entry.id] = { health: entry.health, impaired_code: entry.impaired_code ?? null };
    }
  }
  return map;
}

export interface KioskPeer {
  id: string;
  name: string;
  last_seen_at: string | null;
  reachable: boolean;
}

/** The full folded model the kiosk derives its view from. */
export interface KioskLiveModel {
  /** False until the first `snapshot` arrives (first-paint skeleton gate). */
  hydrated: boolean;
  /** ISO timestamp of the most recent snapshot. */
  at: string | null;
  satellites: KioskSatellite[];
  presence: KioskPresence;
  mcp: KioskMcp;
  toolHealth: KioskToolHealth[];
  roles: AgentRoleInfo[];
  activity: RoleActivityEntry[];
  peers: KioskPeer[];
  weather: KioskWeather | null;
  nowPlaying: KioskNowPlaying[];
  /** id → health verdict for the internal-only subsystem pseudo-nodes
   *  (knowledge / presence / media). Empty until the first snapshot; an id
   *  missing here renders 'unknown' (gray). */
  internalHealth: InternalHealthMap;
  /** True while ≥1 web-chat turn is being processed → the core shows
   *  "processing" even with no satellite active (typed commands have no room). */
  chatActive: boolean;
  /** subsystem id → epoch-ms of its most recent `turn_activity` mention. Drives
   *  the active-subsystem pulse; the view fades it on a render tick. Preserved
   *  across snapshots (it is delta-sourced state with its own decay). */
  subsystemPulses: Record<string, number>;
}

// ---- wire message shapes ---------------------------------------------------

interface SnapshotMessage {
  type: 'snapshot';
  at?: string;
  satellites?: Array<{
    satellite_id: string;
    room: string;
    room_id?: number | null;
    state: SatelliteState;
    has_active_session?: boolean;
  }>;
  presence?: {
    rooms?: KioskPresenceRoom[];
    people_present?: number;
    occupied_rooms?: number;
  };
  mcp?: KioskMcp;
  tool_health?: KioskToolHealth[];
  internal_health?: KioskInternalHealth[];
  roles?: AgentRoleInfo[];
  activity?: RoleActivityEntry[];
  peers?: KioskPeer[];
  weather?: KioskWeather | null;
  now_playing?: KioskNowPlaying[];
  chat_active?: boolean;
}

interface ChatActivityDelta {
  type: 'chat_activity';
  active: boolean;
}

interface SatelliteStateDelta {
  type: 'satellite_state';
  satellite_id: string;
  room: string;
  room_id?: number | null;
  state: SatelliteState;
}

/** Roster liveness — a satellite registered (online) or dropped (offline). */
interface SatelliteLivenessDelta {
  type: 'satellite_online' | 'satellite_offline';
  satellite_id: string;
  room?: string | null;
  room_id?: number | null;
  online: boolean;
}

interface PresenceChangedDelta {
  type: 'presence_changed';
  rooms?: KioskPresenceRoom[];
  people_present?: number;
  occupied_rooms?: number;
}

interface NowPlayingChangedDelta {
  type: 'now_playing_changed';
  sessions?: KioskNowPlaying[];
}

interface ToolHealthChangedDelta {
  type: 'tool_health_changed';
  server: string;
  connected: boolean;
  health?: NodeHealth;
  impaired_code?: string | null;
}

interface WeatherUpdatedDelta {
  type: 'weather_updated';
  weather?: KioskWeather | null;
}

interface InternalHealthChangedDelta {
  type: 'internal_health_changed';
  subsystems?: KioskInternalHealth[];
}

interface PeerStatusChangedDelta {
  type: 'peer_status_changed';
  peers?: KioskPeer[];
}

interface TurnActivityDelta {
  type: 'turn_activity';
  role: string;
  subsystems?: string[];
  ok?: boolean | null;
  at?: string;
}

/** Any parsed inbound frame. Unknown `type`s are tolerated (see reducer). */
type KioskMessage =
  | SnapshotMessage
  | SatelliteStateDelta
  | SatelliteLivenessDelta
  | PresenceChangedDelta
  | NowPlayingChangedDelta
  | ToolHealthChangedDelta
  | WeatherUpdatedDelta
  | InternalHealthChangedDelta
  | PeerStatusChangedDelta
  | TurnActivityDelta
  | ChatActivityDelta
  | { type: string; [key: string]: unknown };

// ---- reducer ---------------------------------------------------------------

const EMPTY_MODEL: KioskLiveModel = {
  hydrated: false,
  at: null,
  satellites: [],
  presence: { rooms: [], people_present: 0, occupied_rooms: 0 },
  mcp: { enabled: false, total_tools: 0, servers: [] },
  toolHealth: [],
  roles: [],
  activity: [],
  peers: [],
  weather: null,
  nowPlaying: [],
  internalHealth: {},
  chatActive: false,
  subsystemPulses: {},
};

/** Recent role activations kept for the pulse trail — bounded so a long-lived
 *  tab's activity list can't grow without limit off `turn_activity` deltas. */
const ACTIVITY_CAP = 30;

/** Naive-UTC-safe parse: the backend emits ISO strings that may lack a zone
 *  suffix; anchor them to UTC so Date.parse doesn't read them as local time. */
function parseAtMs(iso: string | undefined): number {
  if (!iso) return Date.now();
  const ms = Date.parse(iso.endsWith('Z') ? iso : `${iso}Z`);
  return Number.isFinite(ms) ? ms : Date.now();
}

function hydrate(prev: KioskLiveModel, msg: SnapshotMessage): KioskLiveModel {
  return {
    hydrated: true,
    at: msg.at ?? null,
    satellites: (msg.satellites ?? []).map((s) => ({
      satellite_id: s.satellite_id,
      room: s.room,
      room_id: s.room_id ?? null,
      state: s.state,
      has_active_session: s.has_active_session,
    })),
    presence: {
      rooms: msg.presence?.rooms ?? [],
      people_present: msg.presence?.people_present ?? 0,
      occupied_rooms: msg.presence?.occupied_rooms ?? 0,
    },
    mcp: msg.mcp ?? { enabled: false, total_tools: 0, servers: [] },
    toolHealth: msg.tool_health ?? [],
    roles: msg.roles ?? [],
    activity: msg.activity ?? [],
    peers: msg.peers ?? [],
    weather: msg.weather ?? null,
    nowPlaying: msg.now_playing ?? [],
    internalHealth: foldInternalHealth(msg.internal_health),
    chatActive: msg.chat_active ?? false,
    // Preserve the pulse map: it is delta-sourced and self-decays in the view,
    // so a reconnect snapshot must not wipe an in-flight subsystem pulse.
    subsystemPulses: prev.subsystemPulses,
  };
}

function reduce(state: KioskLiveModel, msg: KioskMessage): KioskLiveModel {
  switch (msg.type) {
    case 'snapshot':
      return hydrate(state, msg as SnapshotMessage);

    case 'satellite_state': {
      const delta = msg as SatelliteStateDelta;
      // Roster membership is owned EXCLUSIVELY by satellite_online/offline (and
      // the snapshot); a state event only updates an already-known satellite.
      // Ignoring an unknown id here stops a stale/reordered state frame from
      // resurrecting a satellite that satellite_offline just dropped — the very
      // stale-core bug the offline drop exists to prevent. A satellite that
      // connected after hydrate arrives via its own satellite_online delta.
      let changed = false;
      const satellites = state.satellites.map((sat) => {
        if (sat.satellite_id !== delta.satellite_id) return sat;
        changed = true;
        return {
          ...sat,
          room: delta.room ?? sat.room,
          room_id: delta.room_id ?? sat.room_id,
          state: delta.state,
        };
      });
      return changed ? { ...state, satellites } : state;
    }

    case 'satellite_online': {
      const delta = msg as SatelliteLivenessDelta;
      const existing = state.satellites.find(
        (s) => s.satellite_id === delta.satellite_id,
      );
      if (existing) {
        // Already in the roster — just refresh its room binding (state comes
        // from `satellite_state`; don't clobber it with a liveness event).
        const satellites = state.satellites.map((sat) =>
          sat.satellite_id === delta.satellite_id
            ? { ...sat, room: delta.room ?? sat.room, room_id: delta.room_id ?? sat.room_id }
            : sat,
        );
        return { ...state, satellites };
      }
      // Reinstate a resumed satellite. The online delta carries no voice state;
      // default to idle — the next `satellite_state` delta (or snapshot)
      // corrects it. Tolerate a null room (DB sync runs after register).
      return {
        ...state,
        satellites: [
          ...state.satellites,
          {
            satellite_id: delta.satellite_id,
            room: delta.room ?? '',
            room_id: delta.room_id ?? null,
            state: 'idle',
          },
        ],
      };
    }

    case 'satellite_offline': {
      const delta = msg as SatelliteLivenessDelta;
      // Drop the crashed/disconnected satellite out of the roster so it stops
      // pinning the voice core and its room stops rendering as live.
      return {
        ...state,
        satellites: state.satellites.filter(
          (s) => s.satellite_id !== delta.satellite_id,
        ),
      };
    }

    case 'presence_changed': {
      const delta = msg as PresenceChangedDelta;
      return {
        ...state,
        presence: {
          rooms: delta.rooms ?? [],
          people_present: delta.people_present ?? 0,
          occupied_rooms: delta.occupied_rooms ?? 0,
        },
      };
    }

    case 'now_playing_changed': {
      const delta = msg as NowPlayingChangedDelta;
      return { ...state, nowPlaying: delta.sessions ?? [] };
    }

    case 'tool_health_changed': {
      const delta = msg as ToolHealthChangedDelta;
      let found = false;
      const servers = state.mcp.servers.map((server) => {
        if (server.name !== delta.server) return server;
        found = true;
        // A reconnect clears any stale error text so the server stops rendering
        // as degraded once it is healthy again.
        return {
          ...server,
          connected: delta.connected,
          last_error: delta.connected ? null : server.last_error,
          health: delta.health ?? server.health,
          // Carry the reason code with the health it belongs to; clear it once
          // the node is no longer degraded so a stale reason can't linger.
          impaired_code:
            delta.health === 'degraded'
              ? delta.impaired_code ?? server.impaired_code
              : null,
        };
      });
      if (!found) {
        servers.push({
          name: delta.server,
          connected: delta.connected,
          tool_count: 0,
          health: delta.health,
          impaired_code: delta.health === 'degraded' ? delta.impaired_code : null,
        });
      }
      return { ...state, mcp: { ...state.mcp, servers } };
    }

    case 'weather_updated': {
      const delta = msg as WeatherUpdatedDelta;
      return { ...state, weather: delta.weather ?? null };
    }

    case 'internal_health_changed': {
      // Full replace — the backend recomputes and pushes the whole set on any
      // change (diff-gated), so we never merge partial verdicts.
      const delta = msg as InternalHealthChangedDelta;
      return { ...state, internalHealth: foldInternalHealth(delta.subsystems) };
    }

    case 'peer_status_changed': {
      // Full replace — backend diff-pushes the whole peer set on any reachability
      // change, so peer nodes go green/red live instead of only on reconnect.
      const delta = msg as PeerStatusChangedDelta;
      return { ...state, peers: delta.peers ?? [] };
    }

    case 'chat_activity': {
      const delta = msg as ChatActivityDelta;
      return { ...state, chatActive: !!delta.active };
    }

    case 'turn_activity': {
      const delta = msg as TurnActivityDelta;
      const at = delta.at ?? new Date().toISOString();
      const atMs = parseAtMs(delta.at);
      const activity = [
        { role: delta.role, at, ok: delta.ok ?? null },
        ...state.activity,
      ].slice(0, ACTIVITY_CAP);
      const subsystemPulses = { ...state.subsystemPulses };
      for (const sub of delta.subsystems ?? []) {
        if (typeof sub === 'string' && sub) subsystemPulses[sub] = atMs;
      }
      return { ...state, activity, subsystemPulses };
    }

    default:
      // Unknown event type — tolerate gracefully so a later delta phase can
      // ship on the backend without breaking an already-deployed kiosk tab.
      return state;
  }
}

// ---- connection lifecycle --------------------------------------------------

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30_000;
/** A drop shorter than this keeps the last-good board (a WS blip mustn't flip
 *  the whole display to "unreachable"); a sustained outage flips after it. */
const SUSTAINED_DISCONNECT_MS = 8000;

export interface KioskSocketState {
  live: KioskLiveModel;
  /** True until the first snapshot lands OR the first connection attempt fails
   *  (so an auth/backend failure at boot doesn't hang on the skeleton forever). */
  bootLoading: boolean;
  /** True when the board can no longer be trusted as live: either a SUSTAINED
   *  mid-session disconnect (held false through a brief blip so the last-good
   *  board survives), or a boot that resolved WITHOUT ever hydrating (a failed
   *  first connect — there is no last-good board to hold, so surface it at once
   *  rather than showing an empty "idle/ready" wall for the grace window). */
  backendUnreachable: boolean;
}

function kioskWsUrl(): string {
  // getWebSocketUrl() returns `.../ws`; strip it and append our own path —
  // the canonical pattern (useDeviceConnection.getWsUrl for `/ws/device`).
  let url = getWebSocketUrl().replace(/\/ws$/, '') + '/ws/kiosk';
  // Same JWT-in-query auth the chat + KG-live sockets use; the hub verifies it
  // and requires Permission.ADMIN at connect.
  const token = localStorage.getItem('renfield_access_token');
  if (token) url += `?token=${token}`;
  return url;
}

/**
 * Subscribe to the live kiosk hub. Returns the folded model plus a coarse
 * connection status the kiosk surfaces (so a dropped socket shows a calm
 * "reconnecting" state instead of silently freezing on stale data).
 *
 * Reconnect uses exponential backoff (1s → 30s, reset on open). On every
 * (re)connect the hub re-sends a full `snapshot`, so a missed delta during a
 * blip self-heals without the client asking for anything.
 */
export function useKioskSocket(): KioskSocketState {
  const [live, dispatch] = useReducer(reduce, EMPTY_MODEL);
  // Resolves the first-paint skeleton on EITHER the first snapshot or the first
  // failed connect — so a boot that never authenticates doesn't hang forever.
  const [bootResolved, setBootResolved] = useState(false);
  // Only true after a disconnect has persisted past SUSTAINED_DISCONNECT_MS.
  const [sustainedDisconnect, setSustainedDisconnect] = useState(false);

  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sustainedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptRef = useRef(0);
  const intentionalCloseRef = useRef(false);

  useEffect(() => {
    intentionalCloseRef.current = false;

    const clearSustainedTimer = () => {
      if (sustainedTimerRef.current) {
        clearTimeout(sustainedTimerRef.current);
        sustainedTimerRef.current = null;
      }
    };

    const connect = () => {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }

      let ws: WebSocket;
      try {
        ws = new WebSocket(kioskWsUrl());
      } catch (err) {
        debug.log('Kiosk WS construct failed:', err);
        setBootResolved(true);
        armSustainedTimer();
        scheduleReconnect();
        return;
      }
      wsRef.current = ws;

      ws.onopen = () => {
        debug.log('Kiosk WS connected');
        attemptRef.current = 0;
        clearSustainedTimer();
        setSustainedDisconnect(false);
      };

      ws.onmessage = (event: MessageEvent) => {
        let msg: KioskMessage;
        try {
          msg = JSON.parse(event.data as string) as KioskMessage;
        } catch {
          return; // ignore malformed frames rather than crash the tab
        }
        // First real frame resolves the boot skeleton (belt-and-braces with the
        // hydrated flag; a snapshot is always the first frame the hub sends).
        setBootResolved(true);
        dispatch(msg);
      };

      ws.onerror = (err: Event) => {
        debug.log('Kiosk WS error:', err);
        // First failure resolves the boot skeleton so we don't hang on it; let
        // onclose drive the reconnect (browsers fire error → close).
        setBootResolved(true);
      };

      ws.onclose = () => {
        if (intentionalCloseRef.current) return;
        debug.log('Kiosk WS closed — scheduling reconnect');
        setBootResolved(true);
        armSustainedTimer();
        scheduleReconnect();
      };
    };

    // Arm the "stale board" flag on a fresh disconnect, but only if one isn't
    // already pending — so repeated reconnect failures don't keep pushing the
    // deadline out. A blip that reopens before it fires leaves the board live.
    const armSustainedTimer = () => {
      if (sustainedTimerRef.current) return;
      sustainedTimerRef.current = setTimeout(() => {
        sustainedTimerRef.current = null;
        setSustainedDisconnect(true);
      }, SUSTAINED_DISCONNECT_MS);
    };

    const scheduleReconnect = () => {
      const delay = Math.min(
        MAX_BACKOFF_MS,
        INITIAL_BACKOFF_MS * 2 ** attemptRef.current,
      );
      attemptRef.current += 1;
      reconnectTimerRef.current = setTimeout(connect, delay);
    };

    connect();

    return () => {
      intentionalCloseRef.current = true;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      clearSustainedTimer();
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, []);

  return {
    live,
    bootLoading: !live.hydrated && !bootResolved,
    // Sustained mid-session drop, OR a boot that resolved without ever
    // hydrating (failed first connect — no last-good board to hold, so don't
    // pass the 8s grace showing an empty "ready" wall).
    backendUnreachable: sustainedDisconnect || (bootResolved && !live.hydrated),
  };
}
