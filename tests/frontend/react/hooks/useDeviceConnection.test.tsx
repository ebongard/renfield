/**
 * useDeviceConnection — the /ws/device socket's handshake credential.
 *
 * Regression for the auth-on cutover: this was the ONE `/ws/*` socket the M2
 * pass missed. It opened the handshake with no `?token=`, so under
 * `AUTH_ENABLED=true` the backend closed it with 403 ("Authentication
 * required") and the browser surfaced a bare "WebSocket connection error".
 * The token must be the short-lived WS-scoped one from the faucet, never the
 * long-lived localStorage JWT (which is what putting it in the URL would leak).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import type { RenderHookResult } from '@testing-library/react';

import { useDeviceConnection } from '../../../../src/frontend/src/hooks/useDeviceConnection';
import { fetchWsToken } from '../../../../src/frontend/src/utils/wsToken';

vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  fetchWsToken: vi.fn(),
}));

const mockedFetchWsToken = vi.mocked(fetchWsToken);

async function flushConnect(): Promise<void> {
  for (let i = 0; i < 5; i += 1) await Promise.resolve();
}

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;

  url: string;
  readyState = 0;
  onopen: ((e: Event) => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: ((e: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  send(): void {}
  close(): void {
    this.readyState = 3;
  }
}

function latest(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

type Hook = RenderHookResult<ReturnType<typeof useDeviceConnection>, unknown>;

/** The hook keeps its socket + in-flight attempt in MODULE-level singletons (they
 *  survive React StrictMode remounts), so every test must hand them back or the
 *  next one is refused by the "already connecting" guard. */
async function withConnection(
  body: (hook: Hook) => Promise<void> | void,
  connects = 1,
): Promise<void> {
  const hook = renderHook(() => useDeviceConnection());
  try {
    await act(async () => {
      for (let i = 0; i < connects; i += 1) {
        hook.result.current.connect({ room: 'Wohnzimmer' }).catch(() => {});
      }
      await flushConnect();
    });
    await body(hook);
  } finally {
    act(() => { hook.result.current.disconnect(); });
    hook.unmount();
  }
}

beforeEach(() => {
  MockWebSocket.instances = [];
  vi.stubGlobal('WebSocket', MockWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('useDeviceConnection handshake credential', () => {
  it('carries the short-lived WS token on the /ws/device URL', async () => {
    mockedFetchWsToken.mockResolvedValue('ws-scoped-token');
    await withConnection(() => {
      expect(MockWebSocket.instances).toHaveLength(1);
      expect(latest().url).toContain('/ws/device');
      expect(latest().url).toContain('token=ws-scoped-token');
    });
  });

  it('opens without a token when the faucet returns none (auth-off)', async () => {
    mockedFetchWsToken.mockResolvedValue(null);
    await withConnection(() => {
      expect(latest().url).toContain('/ws/device');
      expect(latest().url).not.toContain('token=');
    });
  });

  it('does NOT open a socket when disconnect() lands during the token fetch', async () => {
    // disconnect() has nothing to close yet — the attempt is suspended in its
    // faucet round-trip. Without the epoch guard it resumes into a live socket,
    // re-registers the device and writes back the config resetSetup() just
    // cleared.
    let releaseToken: (t: string | null) => void = () => {};
    mockedFetchWsToken.mockReturnValue(
      new Promise<string | null>((resolve) => { releaseToken = resolve; }),
    );
    const hook = renderHook(() => useDeviceConnection());
    try {
      await act(async () => {
        hook.result.current.connect({ room: 'Wohnzimmer' }).catch(() => {});
        await Promise.resolve();
        hook.result.current.disconnect();
        releaseToken('ws-scoped-token');
        await flushConnect();
      });
      expect(MockWebSocket.instances).toHaveLength(0);
    } finally {
      act(() => { hook.result.current.disconnect(); });
      hook.unmount();
    }
  });

  it('does not answer a connect() for a DIFFERENT room with the running one', async () => {
    // Coalescing here would resolve the caller successfully while the device
    // registered in the old room — and saveConfig would persist the old one.
    let releaseToken: (t: string | null) => void = () => {};
    mockedFetchWsToken.mockReturnValue(
      new Promise<string | null>((resolve) => { releaseToken = resolve; }),
    );
    const hook = renderHook(() => useDeviceConnection());
    try {
      await act(async () => {
        hook.result.current.connect({ room: 'Wohnzimmer' }).catch(() => {});
        await Promise.resolve();
        mockedFetchWsToken.mockResolvedValue('ws-scoped-token');
        hook.result.current.connect({ room: 'Kueche' }).catch(() => {});
        releaseToken('ws-scoped-token');
        await flushConnect();
      });
      // Exactly one socket, and it belongs to the LATER (superseding) attempt:
      // the first is aborted by the epoch before it can construct one.
      expect(MockWebSocket.instances).toHaveLength(1);
    } finally {
      act(() => { hook.result.current.disconnect(); });
      hook.unmount();
    }
  });

  it('gives up when the token faucet hangs, instead of staying "connecting"', async () => {
    vi.useFakeTimers();
    mockedFetchWsToken.mockReturnValue(new Promise<string | null>(() => {}));
    const hook = renderHook(() => useDeviceConnection());
    try {
      let rejected: unknown = null;
      await act(async () => {
        hook.result.current.connect({ room: 'Wohnzimmer' }).catch((e) => { rejected = e; });
        await Promise.resolve();
        await vi.advanceTimersByTimeAsync(6000);
      });
      expect(MockWebSocket.instances).toHaveLength(0);
      expect((rejected as Error)?.message).toBe('Connection timeout');
    } finally {
      act(() => { hook.result.current.disconnect(); });
      hook.unmount();
      vi.useRealTimers();
    }
  });

  it('opens exactly ONE socket when connect() is called twice in a row', async () => {
    // The token fetch put an await before `new WebSocket`, so the "already
    // connecting" guard only holds if the attempt is published synchronously.
    mockedFetchWsToken.mockResolvedValue('ws-scoped-token');
    await withConnection(() => {
      expect(MockWebSocket.instances).toHaveLength(1);
    }, 2);
  });
});
