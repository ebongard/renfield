/**
 * useUserEvents — the app-wide per-user live-event socket (/ws/user). Covers the
 * enable gate, socket construction, the debounced query invalidation on a
 * `documents_changed` event (a burst → ONE refetch), event-type filtering, and
 * cleanup on unmount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactElement, ReactNode } from 'react';

import { useUserEvents } from '../../../../src/frontend/src/hooks/useUserEvents';

// The socket is built AFTER an awaited token fetch — mock it to resolve to null
// (auth-off: open without a token), so construction happens on the next microtask.
vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  fetchWsToken: vi.fn().mockResolvedValue(null),
}));

async function flushConnect(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

type WsListener<E = unknown> = ((event: E) => void) | null;

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;

  url: string;
  readyState = 0;
  sent: string[] = [];
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
    const ev = typeof CloseEvent !== 'undefined' ? new CloseEvent('close') : (new Event('close') as unknown as CloseEvent);
    this.onclose?.(ev);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.readyState = 3;
  }
}

function latest(): MockWebSocket {
  return MockWebSocket.instances[MockWebSocket.instances.length - 1];
}

function makeClient(): QueryClient {
  return new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
}

function wrapper(client: QueryClient): (p: { children: ReactNode }) => ReactElement {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
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

describe('useUserEvents', () => {
  it('does NOT open a socket when disabled', async () => {
    renderHook(() => useUserEvents({ enabled: false }), { wrapper: wrapper(makeClient()) });
    await flushConnect();
    expect(MockWebSocket.instances).toHaveLength(0);
  });

  it('opens /ws/user when enabled', async () => {
    renderHook(() => useUserEvents({ enabled: true }), { wrapper: wrapper(makeClient()) });
    await flushConnect();
    expect(MockWebSocket.instances).toHaveLength(1);
    expect(latest().url).toContain('/ws/user');
  });

  it('invalidates the knowledge queries (debounced) on documents_changed', async () => {
    vi.useFakeTimers();
    const client = makeClient();
    const spy = vi.spyOn(client, 'invalidateQueries').mockResolvedValue(undefined);
    renderHook(() => useUserEvents({ enabled: true }), { wrapper: wrapper(client) });
    await flushConnect();
    const ws = latest();
    ws.fireOpen();

    ws.fireMessage({ type: 'documents_changed', reason: 'ingested' });
    expect(spy).not.toHaveBeenCalled(); // debounced — not yet
    vi.advanceTimersByTime(1000);
    expect(spy).toHaveBeenCalledWith({ queryKey: ['knowledge'] });
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('collapses a burst of events into ONE invalidation', async () => {
    vi.useFakeTimers();
    const client = makeClient();
    const spy = vi.spyOn(client, 'invalidateQueries').mockResolvedValue(undefined);
    renderHook(() => useUserEvents({ enabled: true }), { wrapper: wrapper(client) });
    await flushConnect();
    const ws = latest();
    ws.fireOpen();

    for (let i = 0; i < 25; i++) ws.fireMessage({ type: 'documents_changed', reason: 'ingested' });
    vi.advanceTimersByTime(1000);
    expect(spy).toHaveBeenCalledTimes(1); // 25 events → 1 refetch
  });

  it('ignores unknown event types', async () => {
    vi.useFakeTimers();
    const client = makeClient();
    const spy = vi.spyOn(client, 'invalidateQueries').mockResolvedValue(undefined);
    renderHook(() => useUserEvents({ enabled: true }), { wrapper: wrapper(client) });
    await flushConnect();
    const ws = latest();
    ws.fireOpen();

    ws.fireMessage({ type: 'something_else' });
    ws.fireMessage('not-an-object');
    vi.advanceTimersByTime(1000);
    expect(spy).not.toHaveBeenCalled();
  });

  it('sends a heartbeat after open', async () => {
    vi.useFakeTimers();
    renderHook(() => useUserEvents({ enabled: true }), { wrapper: wrapper(makeClient()) });
    await flushConnect();
    const ws = latest();
    ws.fireOpen();
    vi.advanceTimersByTime(25_000);
    expect(ws.sent).toContain('ping');
  });

  it('closes the socket on unmount', async () => {
    const { unmount } = renderHook(() => useUserEvents({ enabled: true }), {
      wrapper: wrapper(makeClient()),
    });
    await flushConnect();
    const ws = latest();
    ws.fireOpen();
    unmount();
    expect(ws.readyState).toBe(3); // CLOSED
  });

  it('reconnects after an unexpected close', async () => {
    vi.useFakeTimers();
    renderHook(() => useUserEvents({ enabled: true }), { wrapper: wrapper(makeClient()) });
    await flushConnect();
    expect(MockWebSocket.instances).toHaveLength(1);
    const ws = latest();
    ws.fireOpen();
    ws.fireClose(); // unexpected drop → schedule reconnect (jittered ≤ 1000ms first attempt)
    await vi.advanceTimersByTimeAsync(1000);
    await flushConnect();
    expect(MockWebSocket.instances.length).toBeGreaterThanOrEqual(2);
  });
});
