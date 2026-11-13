/**
 * usePresenceReadings — the push data source of the "Readings" tab on
 * /admin/presence: snapshot → state, full replace on every further snapshot,
 * reconnect with backoff and re-hydration, and no socket left behind on unmount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { usePresenceReadings } from '../../../../src/frontend/src/hooks/usePresenceReadings';
import { requestWsToken } from '../../../../src/frontend/src/utils/wsToken';

// The hook awaits a short-lived WS-scoped token before constructing the socket.
vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  requestWsToken: vi.fn(),
}));

const tokenRequest = vi.mocked(requestWsToken);
const TOKEN_OK = { ok: true, token: 'test-ws-token' } as const;
const TOKEN_FAILED = { ok: false } as const;

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
  /** Default 1006 = abnormal closure, what a browser reports for a dropped
   *  connection or a refused upgrade. */
  fireClose(code = 1006): void {
    this.onclose?.({ code } as CloseEvent);
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
      voice_hold_s: 60,
      voice_release_margin_db: 10,
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
        voice_hold_remaining_s: 41.5,
        voice_pinned: true,
        location_uncertain: false,
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
  tokenRequest.mockReset();
  tokenRequest.mockResolvedValue(TOKEN_OK);
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

  it('carries the voice-hold state of a device and its settings', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    expect(result.current.snapshot?.settings.voice_hold_s).toBe(60);
    expect(result.current.snapshot?.settings.voice_release_margin_db).toBe(10);
    expect(result.current.snapshot?.devices[0]).toMatchObject({
      voice_hold_remaining_s: 41.5, voice_pinned: true, location_uncertain: false,
    });
  });

  it('reads a backend that does not send the voice-hold fields yet', async () => {
    const old = snapshot(-55);
    const { voice_hold_s: _h, voice_release_margin_db: _r, ...oldSettings } = old.settings;
    const {
      voice_hold_remaining_s: _a, voice_pinned: _b, location_uncertain: _c, ...oldDevice
    } = old.devices[0];
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage({ ...old, settings: oldSettings, devices: [oldDevice] });
    });
    expect(result.current.snapshot?.settings).toMatchObject({
      voice_hold_s: 0, voice_release_margin_db: 0,
    });
    expect(result.current.snapshot?.devices[0]).toMatchObject({
      voice_hold_remaining_s: null, voice_pinned: false, location_uncertain: false,
    });
  });

  it('shows access denied on the confirmed refusal, drops the readings and stops retrying', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    act(() => latest().fireClose(4403));
    expect(result.current.status).toBe('denied');
    // Readings received under a role that is gone do not stay on screen.
    expect(result.current.snapshot).toBeNull();
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000);
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(result.current.status).toBe('denied');
  });

  it('shows access denied when the very first handshake is refused for good', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireClose(4403);
    });
    expect(result.current.status).toBe('denied');
  });

  it.each([4001, 4002, 4003, 4004])(
    'treats close code %i ("could not verify") as temporary: reconnecting, with backoff',
    async (code) => {
      vi.useFakeTimers();
      const { result } = renderHook(() => usePresenceReadings());
      await flushConnect();
      act(() => {
        latest().fireOpen();
        latest().fireMessage(snapshot(-55));
      });
      act(() => latest().fireClose(code));
      expect(result.current.status).toBe('reconnecting');
      expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-55);

      await act(async () => {
        vi.advanceTimersByTime(1000);
        await flushConnect();
      });
      expect(MockWebSocket.instances).toHaveLength(2);
      act(() => {
        latest().fireOpen();
        latest().fireMessage(snapshot(-49));
      });
      expect(result.current.status).toBe('live');
      expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-49);
    },
  );

  it('backs off further while the server keeps answering "could not verify"', async () => {
    // The server ACCEPTS before it refuses, so an opened socket must not
    // reset the backoff — or a refusing server would be hit every second.
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireClose(4001);
    });
    await act(async () => {
      vi.advanceTimersByTime(1000);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    act(() => {
      latest().fireOpen();
      latest().fireClose(4001);
    });
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
    // An accepted handshake alone is not "live".
    act(() => latest().fireOpen());
    expect(result.current.status).toBe('reconnecting');
  });

  it('does not open a socket when the token request fails, and goes live once it succeeds', async () => {
    vi.useFakeTimers();
    tokenRequest.mockResolvedValueOnce(TOKEN_FAILED);
    const { result } = renderHook(() => usePresenceReadings());
    await act(async () => {
      await flushConnect();
    });
    // No token-less socket (an auth-on server would refuse it): retry instead.
    expect(MockWebSocket.instances).toHaveLength(0);
    expect(result.current.status).toBe('reconnecting');

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await flushConnect();
    });
    expect(tokenRequest).toHaveBeenCalledTimes(2);
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latest().url).toContain('?token=test-ws-token');
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    expect(result.current.status).toBe('live');
  });

  it('a failed token request at the 15-minute renewal ends in reconnecting, not denied', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });

    tokenRequest.mockResolvedValueOnce(TOKEN_FAILED);
    await act(async () => {
      latest().fireClose(1000);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(result.current.status).toBe('reconnecting');
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-55);

    await act(async () => {
      vi.advanceTimersByTime(1000);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(2);
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-51));
    });
    expect(result.current.status).toBe('live');
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-51);
  });

  it('connects without a token when the server says none is needed', async () => {
    // Auth off: the faucet answers successfully with token null. (With auth on
    // it hands out a token in cookie mode too, so that path is the first test.)
    tokenRequest.mockResolvedValue({ ok: true, token: null });
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latest().url).toMatch(/\/ws\/presence-debug$/);
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });
    expect(result.current.status).toBe('live');
  });

  it('reconnects at once and silently after a planned close (session renewal)', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
    });

    await act(async () => {
      latest().fireClose(1000);
      await flushConnect();
    });
    // New socket without any timer having run; no "reconnecting" in between.
    expect(MockWebSocket.instances).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(0);
    expect(result.current.status).toBe('live');
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-55);

    act(() => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-47));
    });
    expect(result.current.status).toBe('live');
    expect(result.current.snapshot?.devices[0].sightings[0].rssi).toBe(-47);
  });

  it('does not grow the backoff from planned closes', async () => {
    vi.useFakeTimers();
    renderHook(() => usePresenceReadings());
    await flushConnect();
    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        latest().fireOpen();
        latest().fireMessage(snapshot(-55));
        latest().fireClose(1000);
        await flushConnect();
      });
    }
    expect(MockWebSocket.instances).toHaveLength(5);
    // A real drop afterwards still starts at the first backoff step (1 s).
    act(() => {
      latest().fireOpen();
      latest().fireClose();
    });
    await act(async () => {
      vi.advanceTimersByTime(1000);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(6);
  });

  it('falls back to the backoff when the renewal itself fails', async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    await act(async () => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
      latest().fireClose(1000);
      await flushConnect();
    });
    act(() => latest().fireClose());
    expect(result.current.status).toBe('reconnecting');
    expect(vi.getTimerCount()).toBe(1);
  });

  it('a renewal that is refused ends in access denied', async () => {
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    await act(async () => {
      latest().fireOpen();
      latest().fireMessage(snapshot(-55));
      latest().fireClose(1000);
      await flushConnect();
    });
    act(() => {
      latest().fireOpen();
      latest().fireClose(4403);
    });
    expect(result.current.status).toBe('denied');
    expect(result.current.snapshot).toBeNull();
  });

  it('treats a normal close from a socket that never delivered as a drop', async () => {
    // Otherwise a server closing normally right after the handshake would
    // drive an unthrottled reconnect loop.
    vi.useFakeTimers();
    const { result } = renderHook(() => usePresenceReadings());
    await flushConnect();
    await act(async () => {
      latest().fireOpen();
      latest().fireClose(1000);
      await flushConnect();
    });
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(result.current.status).toBe('reconnecting');
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
