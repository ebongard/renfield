import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useChatWebSocket } from '../../../../src/frontend/src/pages/ChatPage/hooks/useChatWebSocket';

type WsListener<E = unknown> = ((event: E) => void) | null;

// Mock WebSocket whose readyState starts as CONNECTING and becomes OPEN
// only when fireOpen() is called externally. Lets us model the
// page-load race: the hook constructs the socket but onopen hasn't fired
// yet when sendMessage / whenReady are called.
class ControllableWebSocket {
  static instances: ControllableWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSED = 3;

  url: string;
  readyState: number = 0;
  OPEN = 1;
  CONNECTING = 0;
  CLOSED = 3;
  sent: string[] = [];
  onopen: WsListener<Event> = null;
  onclose: WsListener<CloseEvent> = null;
  onmessage: WsListener<MessageEvent> = null;
  onerror: WsListener<Event> = null;

  constructor(url: string) {
    this.url = url;
    ControllableWebSocket.instances.push(this);
  }
  fireOpen(): void {
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }
  fireClose(): void {
    this.readyState = 3;
    // jsdom may not provide CloseEvent — fall back to a plain Event cast.
    const closeEvent =
      typeof CloseEvent !== 'undefined'
        ? new CloseEvent('close')
        : (new Event('close') as unknown as CloseEvent);
    this.onclose?.(closeEvent);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {
    this.fireClose();
  }
}

beforeEach(() => {
  ControllableWebSocket.instances = [];
  ControllableWebSocket.OPEN = 1;
  ControllableWebSocket.CONNECTING = 0;
  ControllableWebSocket.CLOSED = 3;
  vi.stubGlobal('WebSocket', ControllableWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('useChatWebSocket', () => {
  describe('whenReady', () => {
    it('resolves immediately to true when socket is already OPEN', async () => {
      const { result } = renderHook(() => useChatWebSocket());
      const ws = ControllableWebSocket.instances[0];

      act(() => ws.fireOpen());

      const ok = await result.current.whenReady(1000);
      expect(ok).toBe(true);
    });

    it('resolves to true once onopen fires after the call', async () => {
      const { result } = renderHook(() => useChatWebSocket());
      const ws = ControllableWebSocket.instances[0];

      // Socket not yet OPEN — call whenReady, then fire onopen.
      const promise = result.current.whenReady(2000);

      // Yield to React, then open the socket.
      await act(async () => {
        ws.fireOpen();
      });

      await expect(promise).resolves.toBe(true);
    });

    it('resolves to false when the timeout elapses without onopen', async () => {
      vi.useFakeTimers();
      try {
        const { result } = renderHook(() => useChatWebSocket());

        const promise = result.current.whenReady(500);
        await act(async () => {
          vi.advanceTimersByTime(600);
        });
        await expect(promise).resolves.toBe(false);
      } finally {
        vi.useRealTimers();
      }
    });

    it('resolves to false when the abort signal fires', async () => {
      const { result } = renderHook(() => useChatWebSocket());

      const ac = new AbortController();
      const promise = result.current.whenReady(5000, ac.signal);
      ac.abort();

      await expect(promise).resolves.toBe(false);
    });

    it('resolves to false if the socket closes before opening', async () => {
      const { result } = renderHook(() => useChatWebSocket());
      const ws = ControllableWebSocket.instances[0];

      const promise = result.current.whenReady(5000);
      await act(async () => {
        ws.fireClose();
      });

      await expect(promise).resolves.toBe(false);
    });
  });

  describe('sendMessage', () => {
    it('returns true and transmits when socket is OPEN', () => {
      const { result } = renderHook(() => useChatWebSocket());
      const ws = ControllableWebSocket.instances[0];
      act(() => ws.fireOpen());

      const ok = result.current.sendMessage({ type: 'text', content: 'hi' });
      expect(ok).toBe(true);
      expect(ws.sent).toHaveLength(1);
      expect(JSON.parse(ws.sent[0])).toMatchObject({ type: 'text', content: 'hi' });
    });

    it('returns false and does not transmit when socket is not OPEN', () => {
      const { result } = renderHook(() => useChatWebSocket());
      const ws = ControllableWebSocket.instances[0];
      // Socket left in CONNECTING state.

      const ok = result.current.sendMessage({ type: 'text', content: 'hi' });
      expect(ok).toBe(false);
      expect(ws.sent).toHaveLength(0);
    });
  });
});
