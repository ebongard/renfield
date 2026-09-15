/**
 * useVoiceStream — `?client=` selection for the shared registry voice-server.
 *
 * Browser voice on an auth-on instance (xidra) must name its registry row, or
 * the voice-server rejects the handshake. The id is RUNTIME config from
 * /api/config/features (one shared frontend image, many instances), with the
 * build-time VITE_VOICE_CLIENT_ID as fallback and no parameter at all when
 * neither is set (household: byte-identical URL).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import {
  buildVoiceWsUrl,
  useVoiceStream,
} from '../../../../src/frontend/src/pages/ChatPage/hooks/useVoiceStream';

vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  fetchVoiceToken: () => Promise.resolve('tok-123'),
  fetchWsToken: () => Promise.resolve(null),
}));

function params(url: string): URLSearchParams {
  return new URL(url).searchParams;
}

describe('buildVoiceWsUrl', () => {
  it('targets /ws/voice', () => {
    expect(new URL(buildVoiceWsUrl(null, null, undefined)).pathname).toMatch(/\/ws\/voice$/);
  });

  it('omits every parameter when there is no token and no id (household)', () => {
    const url = buildVoiceWsUrl(null, null, undefined);
    expect(url).not.toContain('?');
  });

  it('omits client when both ids are empty strings', () => {
    const url = buildVoiceWsUrl('tok', '', '');
    expect(params(url).has('client')).toBe(false);
    expect(params(url).get('token')).toBe('tok');
  });

  it('prefers the runtime id over the build-time id', () => {
    expect(params(buildVoiceWsUrl('tok', 'xidra', 'reva')).get('client')).toBe('xidra');
  });

  it('falls back to the build-time id when no runtime id exists', () => {
    expect(params(buildVoiceWsUrl('tok', null, 'reva')).get('client')).toBe('reva');
    expect(params(buildVoiceWsUrl('tok', undefined, 'reva')).get('client')).toBe('reva');
  });

  it('URL-encodes the token and the id', () => {
    const url = buildVoiceWsUrl('a+b/c=&d', 'x y', undefined);
    expect(url).toContain('token=a%2Bb%2Fc%3D%26d');
    expect(params(url).get('token')).toBe('a+b/c=&d');
    expect(params(url).get('client')).toBe('x y');
  });
});

class MockWs {
  static instances: MockWs[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;
  url: string;
  binaryType = 'blob';
  readyState = MockWs.CONNECTING;
  onopen: ((e: unknown) => void) | null = null;
  onclose: ((e: unknown) => void) | null = null;
  onmessage: ((e: unknown) => void) | null = null;
  onerror: ((e: unknown) => void) | null = null;
  constructor(url: string) {
    this.url = url;
    MockWs.instances.push(this);
  }
  send(): void {}
  close(): void {
    this.readyState = MockWs.CLOSED;
  }
  addEventListener(): void {}
  removeEventListener(): void {}
}

async function flush(): Promise<void> {
  for (let i = 0; i < 5; i += 1) {
    await Promise.resolve();
  }
}

describe('useVoiceStream connect-time client id', () => {
  beforeEach(() => {
    MockWs.instances = [];
    vi.stubGlobal('WebSocket', MockWs);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('waits for the features before opening the socket, then sends ?client=', async () => {
    let release: (id: string | null) => void = () => {};
    const pending = new Promise<string | null>((resolve) => { release = resolve; });
    const getClientId = vi.fn(() => pending);

    const { result } = renderHook(() => useVoiceStream({ getClientId }));

    act(() => {
      void result.current.speakText('hallo');
    });
    await act(flush);

    expect(getClientId).toHaveBeenCalledTimes(1);
    expect(MockWs.instances).toHaveLength(0);

    await act(async () => {
      release('xidra');
      await flush();
    });

    expect(MockWs.instances).toHaveLength(1);
    const url = MockWs.instances[0].url;
    expect(params(url).get('client')).toBe('xidra');
    expect(params(url).get('token')).toBe('tok-123');
  });

  it('a failing features lookup still connects (no runtime id)', async () => {
    const getClientId = vi.fn(() => Promise.reject(new Error('features down')));
    const { result } = renderHook(() => useVoiceStream({ getClientId }));

    act(() => {
      void result.current.speakText('hallo');
    });
    await act(flush);

    expect(MockWs.instances).toHaveLength(1);
    // VITE_VOICE_CLIENT_ID is unset in the test build → no client parameter.
    expect(params(MockWs.instances[0].url).has('client')).toBe(false);
  });

  it('without a resolver the URL carries no client (household path)', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    act(() => {
      void result.current.speakText('hallo');
    });
    await act(flush);

    expect(MockWs.instances).toHaveLength(1);
    expect(params(MockWs.instances[0].url).has('client')).toBe(false);
  });
});
