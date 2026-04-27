import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';
import { useChatWebSocket } from '../../../../src/frontend/src/pages/ChatPage/hooks/useChatWebSocket';

// Mock WebSocket whose readyState starts as CONNECTING and becomes OPEN
// only when fireOpen() is called externally. Lets us model the
// page-load race: the hook constructs the socket but onopen hasn't fired
// yet when sendMessage / whenReady are called.
class ControllableWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0; // CONNECTING
    this.OPEN = 1;
    this.CONNECTING = 0;
    this.CLOSED = 3;
    this.sent = [];
    this.onopen = null;
    this.onclose = null;
    this.onmessage = null;
    this.onerror = null;
    ControllableWebSocket.instances.push(this);
  }
  fireOpen() {
    this.readyState = 1;
    this.onopen?.({});
  }
  fireClose() {
    this.readyState = 3;
    this.onclose?.({});
  }
  send(data) {
    this.sent.push(data);
  }
  close() {
    this.fireClose();
  }
}
ControllableWebSocket.instances = [];

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
