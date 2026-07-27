/**
 * useKioskSocket — the PUSH data source for /kiosk. Covers the reducer folding
 * (snapshot hydration, each delta, unknown-event tolerance) and the reconnect
 * lifecycle (a dropped socket reconnects with backoff and re-hydrates from the
 * fresh snapshot the hub sends on every connect).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useKioskSocket } from '../../../../src/frontend/src/components/kiosk/useKioskSocket';

type WsListener<E = unknown> = ((event: E) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;

  url: string;
  readyState = 0;
  onopen: WsListener<Event> = null;
  onclose: WsListener<CloseEvent> = null;
  onmessage: WsListener<MessageEvent> = null;
  onerror: WsListener<Event> = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  fireOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }
  fireMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
  fireClose(): void {
    this.readyState = 3;
    const ev =
      typeof CloseEvent !== 'undefined'
        ? new CloseEvent('close')
        : (new Event('close') as unknown as CloseEvent);
    this.onclose?.(ev);
  }
  // The hook's cleanup calls close(); real close() completes async, so it does
  // NOT synchronously fire onclose here.
  close(): void {
    this.readyState = 3;
  }
}

function latest(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

function baseSnapshot() {
  return {
    type: 'snapshot',
    at: '2026-07-04T21:00:00Z',
    satellites: [
      { satellite_id: 'sat-wz', room: 'Wohnzimmer', room_id: 1, state: 'idle' },
      { satellite_id: 'sat-ez', room: 'Esszimmer', room_id: 2, state: 'idle' },
    ],
    presence: {
      rooms: [{ room_id: 1, room_name: 'Wohnzimmer', occupants: 2 }],
      people_present: 2,
      occupied_rooms: 1,
    },
    mcp: {
      enabled: true,
      total_tools: 12,
      servers: [{ name: 'homeassistant', connected: true, transport: 'stdio', tool_count: 10 }],
    },
    tool_health: [{ tool_name: 'mcp.homeassistant.turn_on', total: 10, success_rate: 1, degraded: false }],
    internal_health: [
      { id: 'presence', health: 'degraded', impaired_code: 'presence_satellite_unauthenticated' },
      { id: 'knowledge', health: 'healthy', impaired_code: null },
    ],
    roles: [
      { name: 'general', description: { de: 'Allgemein', en: 'General' }, mcp_servers: null, internal_tools: null, has_agent_loop: true },
    ],
    activity: [{ role: 'general', at: '2026-07-04T20:59:00Z', ok: true }],
    peers: [{ id: 'p1', name: 'Peer', last_seen_at: '2026-07-04T20:58:00Z', reachable: true }],
    weather: { location: 'Musterstadt', temp: 20, unit: '°C', code: 0, condition: 'Klar', high: 22, low: 12 },
    now_playing: [{ room: 'Wohnzimmer', kind: 'radio', title: 'Radio', subtitle: null, track: null, total: null }],
  };
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('useKioskSocket', () => {
  it('opens /ws/kiosk and starts unhydrated (boot skeleton)', () => {
    const { result } = renderHook(() => useKioskSocket());
    expect(latest().url).toContain('/ws/kiosk');
    expect(result.current.bootLoading).toBe(true);
    expect(result.current.backendUnreachable).toBe(false);
    expect(result.current.live.hydrated).toBe(false);
  });

  it('hydrates the full model from the snapshot message', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });

    expect(result.current.bootLoading).toBe(false);
    expect(result.current.backendUnreachable).toBe(false);
    const m = result.current.live;
    expect(m.hydrated).toBe(true);
    // The roster is connected-only: every satellite it carries is online.
    expect(m.satellites).toHaveLength(2);
    expect(m.satellites.map((s) => s.satellite_id)).toEqual(['sat-wz', 'sat-ez']);
    expect(m.presence.people_present).toBe(2);
    expect(m.mcp.servers[0].name).toBe('homeassistant');
    expect(m.toolHealth).toHaveLength(1);
    expect(m.roles[0].name).toBe('general');
    expect(m.activity[0].role).toBe('general');
    expect(m.peers[0].name).toBe('Peer');
    expect(m.weather?.location).toBe('Musterstadt');
    expect(m.nowPlaying[0].title).toBe('Radio');
    // internal-subsystem health folds into an id-keyed map
    expect(m.internalHealth.presence).toEqual({
      health: 'degraded',
      impaired_code: 'presence_satellite_unauthenticated',
    });
    expect(m.internalHealth.knowledge.health).toBe('healthy');
  });

  it('folds an internal_health_changed delta (full replace) onto the map', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    expect(result.current.live.internalHealth.presence.health).toBe('degraded');
    act(() => {
      latest().fireMessage({
        type: 'internal_health_changed',
        subsystems: [
          { id: 'presence', health: 'healthy', impaired_code: null },
          { id: 'media', health: 'down', impaired_code: 'media_disabled' },
        ],
      });
    });
    const ih = result.current.live.internalHealth;
    // full replace: presence flips healthy, media appears, knowledge is gone
    expect(ih.presence.health).toBe('healthy');
    expect(ih.media).toEqual({ health: 'down', impaired_code: 'media_disabled' });
    expect(ih.knowledge).toBeUndefined();
  });

  it('folds a peer_status_changed delta (full replace) onto the peer set', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    expect(result.current.live.peers.map((p) => p.id)).toEqual(['p1']);
    act(() => {
      latest().fireMessage({
        type: 'peer_status_changed',
        peers: [{ id: 'p1', name: 'Peer', last_seen_at: '2026-07-04T20:58:00Z', reachable: false }],
      });
    });
    // full replace: p1 flips unreachable live (no wait for reconnect snapshot)
    expect(result.current.live.peers).toEqual([
      { id: 'p1', name: 'Peer', last_seen_at: '2026-07-04T20:58:00Z', reachable: false },
    ]);
  });

  it('folds a satellite_state delta onto the matching satellite', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    act(() => {
      latest().fireMessage({ type: 'satellite_state', satellite_id: 'sat-wz', room: 'Wohnzimmer', room_id: 1, state: 'listening' });
    });
    const wz = result.current.live.satellites.find((s) => s.satellite_id === 'sat-wz');
    expect(wz?.state).toBe('listening');
    // untouched satellite keeps its state
    expect(result.current.live.satellites.find((s) => s.satellite_id === 'sat-ez')?.state).toBe('idle');
  });

  it('ignores a satellite_state for an id not in the roster (offline can\'t be resurrected)', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    // Drop sat-ez, then a stale/reordered state frame arrives for it: roster
    // membership is owned by online/offline, so the state event must NOT re-add
    // it (that would resurrect the offline satellite and re-pin the core).
    act(() => {
      latest().fireMessage({ type: 'satellite_offline', satellite_id: 'sat-ez', room: 'Esszimmer', room_id: 2, online: false });
      latest().fireMessage({ type: 'satellite_state', satellite_id: 'sat-ez', room: 'Esszimmer', room_id: 2, state: 'speaking' });
    });
    expect(result.current.live.satellites.map((s) => s.satellite_id)).toEqual(['sat-wz']);
    // an unknown id (never in the roster) is likewise ignored, not added
    act(() => {
      latest().fireMessage({ type: 'satellite_state', satellite_id: 'sat-new', room: 'Küche', room_id: 9, state: 'speaking' });
    });
    expect(result.current.live.satellites.find((s) => s.satellite_id === 'sat-new')).toBeUndefined();
  });

  it('reinstates a satellite on satellite_online and drops it on satellite_offline', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });

    // offline drops the satellite out of the roster (stops it pinning the core)
    act(() => {
      latest().fireMessage({ type: 'satellite_offline', satellite_id: 'sat-ez', room: 'Esszimmer', room_id: 2, online: false });
    });
    expect(result.current.live.satellites.map((s) => s.satellite_id)).toEqual(['sat-wz']);

    // online reinstates it (defaulting to idle until a state delta arrives)
    act(() => {
      latest().fireMessage({ type: 'satellite_online', satellite_id: 'sat-ez', room: 'Esszimmer', room_id: 2, online: true });
    });
    const ez = result.current.live.satellites.find((s) => s.satellite_id === 'sat-ez');
    expect(ez?.state).toBe('idle');

    // a redundant online for an already-present satellite doesn't duplicate it
    act(() => {
      latest().fireMessage({ type: 'satellite_online', satellite_id: 'sat-wz', room: 'Wohnzimmer', room_id: 1, online: true });
    });
    expect(result.current.live.satellites.filter((s) => s.satellite_id === 'sat-wz')).toHaveLength(1);
  });

  it('folds presence / now_playing / weather / tool_health deltas', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });

    act(() => {
      latest().fireMessage({
        type: 'presence_changed',
        rooms: [{ room_id: 2, room_name: 'Esszimmer', occupants: 1 }],
        people_present: 1,
        occupied_rooms: 1,
      });
      latest().fireMessage({ type: 'now_playing_changed', sessions: [] });
      latest().fireMessage({ type: 'weather_updated', weather: null });
      latest().fireMessage({ type: 'tool_health_changed', server: 'homeassistant', connected: false });
    });

    expect(result.current.live.presence.people_present).toBe(1);
    expect(result.current.live.presence.rooms[0].room_name).toBe('Esszimmer');
    expect(result.current.live.nowPlaying).toHaveLength(0);
    expect(result.current.live.weather).toBeNull();
    expect(result.current.live.mcp.servers.find((s) => s.name === 'homeassistant')?.connected).toBe(false);

    // reconnect clears the stale error so the server isn't stuck degraded
    act(() => {
      latest().fireMessage({ type: 'tool_health_changed', server: 'homeassistant', connected: true });
    });
    const ha = result.current.live.mcp.servers.find((s) => s.name === 'homeassistant');
    expect(ha?.connected).toBe(true);
    expect(ha?.last_error ?? null).toBeNull();
  });

  it('folds the connectivity+functionality health field (degraded while connected)', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      // Snapshot hydrates a server that is connected but backend-marked degraded
      // (e.g. its backing plugin failed to load), so it is NOT green-healthy.
      const snap = {
        ...baseSnapshot(),
        mcp: {
          enabled: true,
          total_tools: 2,
          servers: [
            { name: 'twin', connected: true, transport: 'streamable_http', tool_count: 2, health: 'degraded', impaired_code: 'plugin_failed' },
          ],
        },
      };
      latest().fireMessage(snap);
    });
    const twin0 = result.current.live.mcp.servers.find((s) => s.name === 'twin');
    expect(twin0?.connected).toBe(true);
    expect(twin0?.health).toBe('degraded');
    expect(twin0?.impaired_code).toBe('plugin_failed');

    // A live delta can flip it back to healthy without a reconnect; the stale
    // reason code must clear so it can't linger on a now-healthy node.
    act(() => {
      latest().fireMessage({ type: 'tool_health_changed', server: 'twin', connected: true, health: 'healthy' });
    });
    const twin1 = result.current.live.mcp.servers.find((s) => s.name === 'twin');
    expect(twin1?.health).toBe('healthy');
    expect(twin1?.impaired_code ?? null).toBeNull();
  });

  it('folds a turn_activity delta into the trail and the subsystem pulses', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    act(() => {
      latest().fireMessage({ type: 'turn_activity', role: 'smart_home', subsystems: ['homeassistant', 'weather'], ok: true, at: '2026-07-04T21:01:00Z' });
    });
    // newest activity prepended
    expect(result.current.live.activity[0].role).toBe('smart_home');
    // each named subsystem stamped with the event time
    const pulses = result.current.live.subsystemPulses;
    expect(pulses.homeassistant).toBe(Date.parse('2026-07-04T21:01:00Z'));
    expect(pulses.weather).toBe(Date.parse('2026-07-04T21:01:00Z'));
  });

  it('ignores an unknown event type without crashing or mutating state', () => {
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    const before = result.current.live;
    act(() => {
      latest().fireMessage({ type: 'something_from_phase_9' });
      latest().fireMessage({ type: 'another_future_delta', payload: { x: 1 } });
    });
    // reference unchanged → no re-render churn, and definitely no throw
    expect(result.current.live).toBe(before);
  });

  it('reconnects with backoff and re-hydrates from a fresh snapshot', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    expect(MockWebSocket.instances).toHaveLength(1);

    // socket drops → no new socket yet (waits for the backoff), board held live
    act(() => {
      latest().fireClose();
    });
    expect(result.current.backendUnreachable).toBe(false);
    expect(MockWebSocket.instances).toHaveLength(1);

    // first backoff (1s) elapses → a fresh socket is opened
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    // the hub re-sends a snapshot on connect → the missed-event gap self-heals
    act(() => {
      latest().fireOpen();
      latest().fireMessage({ ...baseSnapshot(), satellites: [{ satellite_id: 'sat-wz', room: 'Wohnzimmer', room_id: 1, state: 'speaking' }] });
    });
    expect(result.current.backendUnreachable).toBe(false);
    expect(result.current.live.satellites).toHaveLength(1);
    expect(result.current.live.satellites[0].state).toBe('speaking');
  });

  it('surfaces unreachable at once on a first-connect failure (no empty ready board)', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useKioskSocket());
    expect(result.current.bootLoading).toBe(true);
    // the very first socket closes before any snapshot (e.g. auth rejected)
    act(() => {
      latest().fireClose();
    });
    // boot skeleton clears AND we go straight to unreachable — there is no
    // last-good board, so we must NOT show a calm idle wall for the grace window.
    expect(result.current.bootLoading).toBe(false);
    expect(result.current.backendUnreachable).toBe(true);
  });

  it('keeps the board live through a brief blip, flips unreachable only when sustained', () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useKioskSocket());
    act(() => {
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    expect(result.current.backendUnreachable).toBe(false);

    // socket drops — still NOT unreachable (last-good board held through a blip)
    act(() => {
      latest().fireClose();
    });
    expect(result.current.backendUnreachable).toBe(false);

    // it reopens quickly (< 8s) → the blip never escalates
    act(() => {
      vi.advanceTimersByTime(1000);
      latest().fireOpen();
      latest().fireMessage(baseSnapshot());
    });
    expect(result.current.backendUnreachable).toBe(false);

    // now a sustained drop: no reopen past the 8s window → board reads stale
    act(() => {
      latest().fireClose();
    });
    act(() => {
      vi.advanceTimersByTime(8000);
    });
    expect(result.current.backendUnreachable).toBe(true);
  });
});
