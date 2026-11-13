/**
 * usePresenceReadings — the push data source of the "Readings" tab on
 * /admin/presence: snapshot → state, full replace on every further snapshot,
 * reconnect with backoff and re-hydration, and no socket left behind on unmount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePresenceReadings } from '../../../../src/frontend/src/hooks/usePresenceReadings';

// The hook awaits a short-lived WS-scoped token before constructing the socket.
vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  fetchWsToken: vi.fn().mockResolvedValue('test-ws-token'),
}));

async function flushConnect(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

type WsListener<E = unknown> = ((event: E) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  url: string;
  closeCalls = 0;
  onopen: WsListener<Event> = null;
  onclose: WsListener<CloseEvent> = null;
  onmessage: WsListener<MessageEvent> = null;
  onerror: WsListener<Event> = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  fireOpen(): void {
    this.onopen?.(new Event('open'));
  }
  fireMessage(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
  fireRaw(data: string): void {
    this.onmessage?.({ data } as MessageEvent);
  }
  fireClose(): void {
    this.onclose?.(new Event('close') as unknown as CloseEvent);
  }
  close(): void {
    this.closeCalls += 1;
  }
}

function latest(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

function snapshot(rssi: number) {
  return {
    type: 'snapshot',
    now: 1_800_000_000,
    enabled: true,
    settings: {
      rssi_floor_dbm: -80,
      switch_enter_margin_db: 8,
      stale_timeout_s: 120,
      rssi_filter_enabled: true,
    },
    satellites: [{ satellite_id: 'sat-kitchen', room_id: 10, room_name: 'Kitchen' }],
    devices: [
      {
        key: 'mac:0a1b2c3d',
        user_id: 1,
        user_name: 'alice',
        current_room_id: 10,
        current_room_name: 'Kitchen',
        current_confidence: 0.7,
        filtered_by_room: { Kitchen: rssi },
        filtered_rooms: [{ room_id: 10, room_name: 'Kitchen', rssi }],
        sightings: [
          {
            satellite_id: 'sat-kitchen', room_id: 10, room_name: 'Kitchen',
            rssi, timestamp: 1_799_999_998, age_s: 2,
          },
        ],
      },
    ],
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

describe('usePresenceReadings', () => {
  it('opens /ws/presence-debug with the scoped token and starts connecting', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    expect(latest().url).toContain('/ws/presence-debug?token=test-ws-token');
    expect(result.current.status).toBe('connecting');
    expect(result.current.snapshot).toBeNull();
  });

  it('turns a snapshot into state and replaces it with the next one', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    expect(result.current.status).toBe('live');
    expect(result.current.snapshot?.now).toBe(1_800_000_000);
    expect(result.current.snapshot?.settings.switch_enter_margin_db).toBe(8);
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-55);
    // The wire-only `type` field does not leak into the model.
    expect(result.current.snapshot).not.toHaveProperty('type');

    act(() => latest().fireMessage(snapshot(-62)));
    expect(result.current.snapshot?.devices).toHaveLength(1);
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-62);
  });

  it('ignores malformed frames and unknown message types', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
      latest().fireRaw('not json');
      latest().fireMessage({ type: 'something_new', devices: [] });
      latest().fireMessage({ type: 'snapshot' }); // no body
    });
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-55);
  });

  it('reconnects with backoff, keeps the last snapshot and re-hydrates', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    expect(MockWebSocket.instances).toHaveLength(1);

    act(() => latest().fireClose());
    expect(result.current.status).toBe('reconnecting');
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-55);

    // First retry after 1 s — not earlier.
    await act(async () => {
      vi.advanceTimersByTime(999);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    await act(async () => {
      vi.advanceTimersByTime(1);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(2);

    // That attempt fails too: the next one waits 2 s.
    act(() => latest().fireClose());
    await act(async () => {
      vi.advanceTimersByTime(1999);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    await act(async () => {
      vi.advanceTimersByTime(1);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(3);

    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-48));
    });
    expect(result.current.status).toBe('live');
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-48);
  });

  it('reports reconnecting when the very first attempt fails', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => latest().fireClose());
    expect(result.current.status).toBe('reconnecting');
    expect(result.current.snapshot).toBeNull();
  });

  it('closes the socket on unmount and does not reconnect afterwards', async () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => latest().fireOpen());
    const ws = latest();
    unmount();
    expect(ws.closeCalls).toBe(1);
    ws.fireClose();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(MockWebSocket.instances).toHaveLength(1);
  });

  it('receives data without any timer of its own', async () => {
    vi.useFakeTimers();
    renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    // A live socket schedules nothing: no fetch loop, no refresh tick.
    expect(vi.getTimerCount()).toBe(0);
  });
});
