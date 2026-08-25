/**
 * useVoiceStream — barge-in / cancellation plumbing (plan §5.2, task T3).
 *
 * Covers the shared cancellation mechanism: the generation token that
 * gates post-interrupt TTS, the pending-request lifecycle across every
 * terminal frame (tts_done / cancelled / error), the WS-close and 60s
 * watchdog cleanup paths, cancelAllPlayback's fan-out, and the rid gate
 * that drops stray frames.
 *
 * Fork A's acoustic listener + AudioContext playback path are covered
 * by the T4 test pass (voiceAudioUtils.test.ts + the fork cases) — these
 * tests deliberately need only a mock WebSocket.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useVoiceStream } from '../../../../src/frontend/src/pages/ChatPage/hooks/useVoiceStream';

// The hook now fetches its own short-lived voice faucet token; mock it to null
// (anonymous handshake) so these WS-plumbing tests stay HTTP-free + deterministic.
vi.mock('../../../../src/frontend/src/utils/wsToken', () => ({
  fetchVoiceToken: () => Promise.resolve(null),
  fetchWsToken: () => Promise.resolve(null),
}));

type WsListener = (event: unknown) => void;

// Mock WebSocket with addEventListener support — useVoiceStream's
// session_ready handshake registers via addEventListener, not just the
// on* properties, so the mock must drive both.
class MockVoiceWebSocket {
  static instances: MockVoiceWebSocket[] = [];
  static OPEN = 1;
  static CONNECTING = 0;
  static CLOSING = 2;
  static CLOSED = 3;

  url: string;
  binaryType = 'blob';
  readyState: number = MockVoiceWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: WsListener | null = null;
  onclose: WsListener | null = null;
  onmessage: WsListener | null = null;
  onerror: WsListener | null = null;
  private listeners: Record<string, WsListener[]> = {};

  constructor(url: string) {
    this.url = url;
    MockVoiceWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(data);
  }

  close(): void {
    this.fireClose();
  }

  addEventListener(type: string, cb: WsListener): void {
    (this.listeners[type] ||= []).push(cb);
  }

  removeEventListener(type: string, cb: WsListener): void {
    this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== cb);
  }

  private emit(type: string, ev: unknown): void {
    [...(this.listeners[type] || [])].forEach((f) => f(ev));
  }

  // --- test drivers --------------------------------------------------
  fireOpen(): void {
    this.readyState = MockVoiceWebSocket.OPEN;
    this.onopen?.(new Event('open'));
    this.emit('open', new Event('open'));
  }

  fireClose(): void {
    this.readyState = MockVoiceWebSocket.CLOSED;
    const ev = { type: 'close' };
    this.onclose?.(ev);
    this.emit('close', ev);
  }

  emitMessage(data: string | ArrayBuffer): void {
    const ev = { data };
    this.onmessage?.(ev);
    this.emit('message', ev);
  }

  /** Convenience for server→client JSON frames. */
  emitJson(payload: object): void {
    this.emitMessage(JSON.stringify(payload));
  }
}

function lastWs(): MockVoiceWebSocket {
  const ws = MockVoiceWebSocket.instances.at(-1);
  if (!ws) throw new Error('no MockVoiceWebSocket created');
  return ws;
}

function sentOfType(ws: MockVoiceWebSocket, type: string): Array<Record<string, unknown>> {
  return ws.sent
    .map((s) => JSON.parse(s) as Record<string, unknown>)
    .filter((m) => m.type === type);
}

// Build a 24-byte-RFWA-headed binary frame for a given request id.
function uuidToBytes(uuid: string): Uint8Array {
  const hex = uuid.replace(/-/g, '');
  const out = new Uint8Array(16);
  for (let i = 0; i < 16; i += 1) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}
function makeRfwaFrame(requestId: string, sequence = 0): ArrayBuffer {
  const buf = new Uint8Array(24 + 8); // 8-byte dummy WAV body
  buf.set([0x52, 0x46, 0x57, 0x41], 0); // "RFWA"
  buf.set(uuidToBytes(requestId), 4);
  new DataView(buf.buffer).setUint32(20, sequence, false);
  return buf.buffer;
}

// Minimal AudioContext mock for the playback-path tests. drainQueue
// touches: state, resume, decodeAudioData, createBufferSource,
// destination. A real source fires onended when playback ends.
class MockAudioBufferSource {
  buffer: unknown = null;
  onended: (() => void) | null = null;
  connect = vi.fn();
  start = vi.fn(() => {
    // drainQueue assigns onended before calling start(), so resolving
    // its await synchronously here mirrors a finished playback.
    this.onended?.();
  });
  stop = vi.fn(() => { this.onended?.(); });
}

class MockAudioContext {
  static instances: MockAudioContext[] = [];
  // When false, decodeAudioData returns a promise the test resolves by
  // hand — lets a barge-in land while a decode is mid-flight.
  static autoDecode = true;
  // Frequency-domain frame served by every analyser this context makes.
  // Default [] → all zero → RMS 0 (silence). Tests set it loud to
  // simulate the user speaking over the assistant.
  static frequencyFrame: number[] = [];

  state = 'running';
  destination = {};
  pendingDecodes: Array<(buf: unknown) => void> = [];
  createdSources: MockAudioBufferSource[] = [];

  constructor() {
    MockAudioContext.instances.push(this);
  }

  decodeAudioData(_buf: ArrayBuffer): Promise<unknown> {
    if (MockAudioContext.autoDecode) return Promise.resolve({ duration: 1 });
    return new Promise((resolve) => { this.pendingDecodes.push(resolve); });
  }

  createBufferSource(): MockAudioBufferSource {
    const source = new MockAudioBufferSource();
    this.createdSources.push(source);
    return source;
  }

  createAnalyser() {
    return {
      fftSize: 0,
      smoothingTimeConstant: 0,
      frequencyBinCount: 16,
      getByteFrequencyData: (arr: Uint8Array): void => {
        for (let i = 0; i < arr.length; i += 1) {
          arr[i] = MockAudioContext.frequencyFrame[i] ?? 0;
        }
      },
      connect: () => {},
    };
  }

  createMediaStreamSource() {
    return { connect: () => {} };
  }

  resume(): Promise<void> {
    return Promise.resolve();
  }

  close(): Promise<void> {
    this.state = 'closed';
    return Promise.resolve();
  }
}

function lastCtx(): MockAudioContext {
  const ctx = MockAudioContext.instances.at(-1);
  if (!ctx) throw new Error('no MockAudioContext created');
  return ctx;
}

// Playback uses resolved promises, not timers — a few microtask hops
// drain the whole drainQueue coroutine.
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

// --- MediaRecorder + getUserMedia mocks (recording + barge-in paths) -
class MockMediaRecorder {
  static instances: MockMediaRecorder[] = [];
  static isTypeSupported = (): boolean => true;

  state: 'inactive' | 'recording' | 'paused' = 'inactive';
  stream: MediaStream;
  mimeType: string;
  ondataavailable: ((e: unknown) => void) | null = null;
  onstop: (() => void) | null = null;
  start = vi.fn((): void => { this.state = 'recording'; });
  stop = vi.fn((): void => {
    this.state = 'inactive';
    this.onstop?.();
  });

  constructor(stream: MediaStream, opts?: { mimeType?: string }) {
    this.stream = stream;
    this.mimeType = opts?.mimeType ?? '';
    MockMediaRecorder.instances.push(this);
  }
}

function makeFakeStream(): MediaStream {
  const track = { stop: vi.fn(), kind: 'audio' as const };
  return {
    getTracks: () => [track],
    getAudioTracks: () => [track],
  } as unknown as MediaStream;
}

// getUserMedia mock. `getUserMediaShouldReject` exercises the listener's
// silent-degrade path (finding 3B); `getUserMediaDeferred` holds the
// promise open so a test can resolve it after the reply has already
// ended (the open()-vs-cleanup race).
let getUserMediaShouldReject = false;
let getUserMediaDeferred = false;
let getUserMediaResolvers: Array<() => void> = [];
let lastDeferredStream: MediaStream | null = null;
const getUserMediaMock = vi.fn(async (): Promise<MediaStream> => {
  if (getUserMediaShouldReject) throw new DOMException('denied', 'NotAllowedError');
  if (getUserMediaDeferred) {
    return new Promise<MediaStream>((resolve) => {
      getUserMediaResolvers.push(() => {
        lastDeferredStream = makeFakeStream();
        resolve(lastDeferredStream);
      });
    });
  }
  return makeFakeStream();
});

type VoiceHook = ReturnType<typeof useVoiceStream>;

// Render the hook, then run speakText() to first-connect: drive the
// socket open + session_ready so the handshake resolves and the
// tts_request goes out. Returns the resolved request id.
async function speakAndConnect(
  result: { current: VoiceHook },
  text = 'hallo welt',
): Promise<{ ws: MockVoiceWebSocket; rid: string }> {
  let ridP!: Promise<string | null>;
  act(() => {
    ridP = result.current.speakText(text);
  });
  // The hook now fetches a short-lived voice token async before creating the
  // socket — flush that microtask so the WebSocket exists before we grab it.
  await act(async () => { await Promise.resolve(); });
  const ws = lastWs();
  await act(async () => {
    ws.fireOpen();
    ws.emitJson({ type: 'session_ready' });
    await ridP;
  });
  const rid = await ridP;
  if (typeof rid !== 'string') {
    throw new Error('speakAndConnect: speakText did not resolve to a request id');
  }
  return { ws, rid };
}

// speakText() on an already-connected hook (ensureSocket fast-path).
async function speakAgain(
  result: { current: VoiceHook },
  text: string,
): Promise<string | null> {
  let ridP!: Promise<string | null>;
  await act(async () => {
    ridP = result.current.speakText(text);
    await ridP;
  });
  return ridP;
}

beforeEach(() => {
  MockVoiceWebSocket.instances = [];
  MockAudioContext.instances = [];
  MockAudioContext.autoDecode = true;
  MockAudioContext.frequencyFrame = [];
  MockMediaRecorder.instances = [];
  getUserMediaShouldReject = false;
  getUserMediaDeferred = false;
  getUserMediaResolvers = [];
  lastDeferredStream = null;
  getUserMediaMock.mockClear();
  vi.stubGlobal('WebSocket', MockVoiceWebSocket);
  vi.stubGlobal('AudioContext', MockAudioContext);
  vi.stubGlobal('MediaRecorder', MockMediaRecorder);
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: getUserMediaMock },
    configurable: true,
  });
  // Fake rAF + performance too: the barge-in listener's
  // requestAnimationFrame loop and its performance.now() warmup/sustain
  // clock must both advance under vi.advanceTimersByTime.
  vi.useFakeTimers({
    toFake: [
      'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date',
      'performance', 'requestAnimationFrame', 'cancelAnimationFrame',
    ],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('useVoiceStream — barge-in plumbing', () => {
  it('speakText dispatches a tts_request and resolves to its request id', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result);

    expect(typeof rid).toBe('string');
    const ttsRequests = sentOfType(ws, 'tts_request');
    expect(ttsRequests).toHaveLength(1);
    expect(ttsRequests[0].request_id).toBe(rid);
    expect(result.current.playbackActive).toBe(true);
  });

  it('speakText is gated (returns null) when cancelAllPlayback fires during the handshake', async () => {
    // Headline 1A: TTS the agent produced before the user interrupted
    // must not reach the speaker.
    const { result } = renderHook(() => useVoiceStream({}));
    let ridP!: Promise<string | null>;
    act(() => {
      ridP = result.current.speakText('soll nicht gesprochen werden');
    });
    // Flush the async voice-token fetch so the socket is created (still pre-ready).
    await act(async () => { await Promise.resolve(); });
    const ws = lastWs();
    // Barge-in lands while the socket handshake is still in flight.
    act(() => {
      result.current.cancelAllPlayback();
    });
    await act(async () => {
      ws.fireOpen();
      ws.emitJson({ type: 'session_ready' });
      await ridP;
    });

    expect(await ridP).toBeNull();
    expect(sentOfType(ws, 'tts_request')).toHaveLength(0);
  });

  it('tts_done drains the request and ends playback after the grace window', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result);
    expect(result.current.playbackActive).toBe(true);

    act(() => {
      ws.emitJson({ type: 'tts_done', request_id: rid });
    });
    // Held true through the inter-sentence grace window...
    expect(result.current.playbackActive).toBe(true);
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(result.current.playbackActive).toBe(false);
  });

  it('a cancelled frame drains the request without surfacing an error', async () => {
    const onError = vi.fn();
    const onTtsSettled = vi.fn();
    const { result } = renderHook(() =>
      useVoiceStream({ onError, onTtsSettled }),
    );
    const { ws, rid } = await speakAndConnect(result);

    act(() => {
      ws.emitJson({ type: 'cancelled', request_id: rid });
    });

    expect(onTtsSettled).toHaveBeenCalledWith(rid, 'cancelled');
    expect(onError).not.toHaveBeenCalled(); // cancel is not a failure
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(result.current.playbackActive).toBe(false);
  });

  it('an error frame with a request_id drains the request and surfaces onError', async () => {
    const onError = vi.fn();
    const onTtsSettled = vi.fn();
    const { result } = renderHook(() =>
      useVoiceStream({ onError, onTtsSettled }),
    );
    const { ws, rid } = await speakAndConnect(result);

    act(() => {
      ws.emitJson({ type: 'error', code: 'tts_failed', message: 'boom', request_id: rid });
    });

    expect(onTtsSettled).toHaveBeenCalledWith(rid, 'error');
    expect(onError).toHaveBeenCalledWith('tts_failed', 'boom', rid);
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(result.current.playbackActive).toBe(false);
  });

  it('WS close clears pending TTS and ends playback immediately', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws } = await speakAndConnect(result);
    expect(result.current.playbackActive).toBe(true);

    act(() => {
      ws.fireClose();
    });
    // No grace window on a socket death — the terminal frame is never coming.
    expect(result.current.playbackActive).toBe(false);
  });

  it('the 60s watchdog force-drops a request whose terminal frame never arrives', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    await speakAndConnect(result);
    expect(result.current.playbackActive).toBe(true);

    // No tts_done / cancelled / error ever arrives.
    act(() => {
      vi.advanceTimersByTime(60000); // watchdog fires
    });
    act(() => {
      vi.advanceTimersByTime(600); // playback-idle grace
    });
    expect(result.current.playbackActive).toBe(false);
  });

  it('cancelAllPlayback sends one cancel frame per in-flight request', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result, 'satz eins');
    const rid2 = await speakAgain(result, 'satz zwei');
    const rid3 = await speakAgain(result, 'satz drei');

    act(() => {
      result.current.cancelAllPlayback();
    });

    const cancels = sentOfType(ws, 'cancel');
    expect(cancels).toHaveLength(3);
    expect(cancels.map((c) => c.request_id).sort()).toEqual([rid, rid2, rid3].sort());
    act(() => {
      vi.advanceTimersByTime(600);
    });
    expect(result.current.playbackActive).toBe(false);
  });

  it('cancelAllPlayback after the socket closed does not throw', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws } = await speakAndConnect(result);
    act(() => {
      ws.fireClose();
    });
    expect(() => {
      act(() => {
        result.current.cancelAllPlayback();
      });
    }).not.toThrow();
  });

  it('a binary frame for an unknown request id is dropped (rid gate)', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result);

    // Settle the real request so playback is idle.
    act(() => {
      ws.emitJson({ type: 'tts_done', request_id: rid });
      vi.advanceTimersByTime(600);
    });
    expect(result.current.playbackActive).toBe(false);

    // A stray frame for a request that is no longer pending must be
    // dropped — if it were enqueued it would reach drainQueue, build an
    // AudioContext, and flip playbackActive back to true.
    act(() => {
      ws.emitMessage(makeRfwaFrame('00000000-0000-4000-8000-000000000000'));
    });
    expect(result.current.playbackActive).toBe(false);
    // The barge-in listener may have built an AudioContext, but a dropped
    // frame must never reach drainQueue — so no buffer source is created.
    const sourcesPlayed = MockAudioContext.instances
      .reduce((n, c) => n + c.createdSources.length, 0);
    expect(sourcesPlayed).toBe(0);
  });

  it('a binary frame for an in-flight request passes the rid gate and reaches playback', async () => {
    // Positive counterpart to the drop test. Without this, an inverted
    // gate condition (drop the pending ones) would silently discard ALL
    // TTS audio and the suite would still pass.
    MockAudioContext.autoDecode = true;
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result);

    await act(async () => {
      ws.emitMessage(makeRfwaFrame(rid));
      await flushMicrotasks();
    });

    const ctx = lastCtx();
    expect(ctx.createdSources.length).toBeGreaterThan(0); // frame was played
    expect(ctx.createdSources[0].start).toHaveBeenCalled();
  });

  it('drainQueue discards a chunk that finishes decoding after a barge-in', async () => {
    // The core 1C race: a frame is mid-decode when the user interrupts.
    // The generation re-check after decodeAudioData must drop the buffer
    // instead of starting a source.
    MockAudioContext.autoDecode = false;
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result);

    await act(async () => {
      ws.emitMessage(makeRfwaFrame(rid));
      await flushMicrotasks();
    });
    const ctx = lastCtx();
    expect(ctx.pendingDecodes).toHaveLength(1); // drainQueue parked on decode

    // Barge-in lands while the decode is still in flight.
    act(() => {
      result.current.cancelAllPlayback();
    });

    // The decode resolves now — but the generation moved, so drainQueue
    // must break before creating a source.
    await act(async () => {
      ctx.pendingDecodes[0]({ duration: 1 });
      await flushMicrotasks();
    });
    expect(ctx.createdSources).toHaveLength(0);
  });

  it('the barge-in listener does not fire while the mic is silent', async () => {
    MockAudioContext.frequencyFrame = []; // silence
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws } = await speakAndConnect(result);
    await act(async () => { await flushMicrotasks(); }); // listener open() settles
    await act(async () => {
      vi.advanceTimersByTime(2000);
      await flushMicrotasks();
    });
    expect(sentOfType(ws, 'cancel')).toHaveLength(0);
    expect(MockMediaRecorder.instances).toHaveLength(0);
  });

  it('voiced energy within the warmup window does not fire a barge-in', async () => {
    MockAudioContext.frequencyFrame = new Array(16).fill(200); // loud
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws } = await speakAndConnect(result);
    await act(async () => { await flushMicrotasks(); });
    // Only 200ms elapse — short of the 250ms warmup, so detection is
    // suppressed even though the mic is loud.
    await act(async () => {
      vi.advanceTimersByTime(200);
      await flushMicrotasks();
    });
    expect(sentOfType(ws, 'cancel')).toHaveLength(0);
    expect(MockMediaRecorder.instances).toHaveLength(0);
  });

  it('sustained voiced energy fires a barge-in: cancels TTS and promotes to recording', async () => {
    MockAudioContext.frequencyFrame = new Array(16).fill(200); // user speaking
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws } = await speakAndConnect(result);
    await act(async () => { await flushMicrotasks(); });

    // Past warmup (250ms) + sustain (150ms) — the rAF loop detects it.
    await act(async () => {
      vi.advanceTimersByTime(600);
      await flushMicrotasks(); // let the promotion's async IIFE settle
    });

    expect(sentOfType(ws, 'cancel').length).toBeGreaterThan(0); // TTS cancelled
    expect(MockMediaRecorder.instances.length).toBeGreaterThan(0); // promoted
    expect(MockMediaRecorder.instances[0].start).toHaveBeenCalled();
    // The promotion reuses the listener's stream — NO second getUserMedia
    // (plan §6.3). The one call is the listener opening.
    expect(getUserMediaMock).toHaveBeenCalledTimes(1);
  });

  it('a denied mic disables barge-in for the reply without surfacing an error', async () => {
    getUserMediaShouldReject = true;
    MockAudioContext.frequencyFrame = new Array(16).fill(200);
    const onError = vi.fn();
    const { result } = renderHook(() => useVoiceStream({ onError }));
    const { ws } = await speakAndConnect(result);
    await act(async () => {
      vi.advanceTimersByTime(600);
      await flushMicrotasks();
    });
    // Listener never opened → no barge-in, and the degradation is silent.
    expect(sentOfType(ws, 'cancel')).toHaveLength(0);
    expect(MockMediaRecorder.instances).toHaveLength(0);
    expect(onError).not.toHaveBeenCalled();
  });

  it('a listener mic that resolves after the reply ended is stopped, not leaked', async () => {
    // open()-vs-cleanup race: getUserMedia is still in flight when the
    // reply finishes. The cancelled-flag guard must stop the late stream.
    getUserMediaDeferred = true;
    const { result } = renderHook(() => useVoiceStream({}));
    const { ws, rid } = await speakAndConnect(result);
    await act(async () => { await flushMicrotasks(); }); // listener parked on getUserMedia
    expect(getUserMediaResolvers).toHaveLength(1);

    // The reply ends before the mic resolves → playbackActive falls →
    // the listener effect cleanup runs (cancelled = true).
    act(() => { ws.emitJson({ type: 'tts_done', request_id: rid }); });
    act(() => { vi.advanceTimersByTime(600); }); // playback-idle grace

    // The mic resolves only now — open() must see cancelled and stop it.
    await act(async () => {
      getUserMediaResolvers[0]();
      await flushMicrotasks();
    });
    expect(lastDeferredStream).not.toBeNull();
    const track = lastDeferredStream!.getTracks()[0];
    expect(track.stop).toHaveBeenCalled(); // late stream stopped — no leak
  });
});

describe('useVoiceStream — checkSilence regression (plan §8.3)', () => {
  // IRON RULE: the computeRms extraction (T4a) refactored checkSilence.
  // This proves end-of-utterance auto-stop still fires.
  it('still auto-stops the recorder after trailing silence', async () => {
    const { result } = renderHook(() => useVoiceStream({}));
    let startP!: Promise<void>;
    act(() => { startP = result.current.startRecording(); });
    // Flush the async voice-token fetch so the socket exists before we grab it.
    await act(async () => { await Promise.resolve(); });
    const ws = lastWs();
    await act(async () => {
      ws.fireOpen();
      ws.emitJson({ type: 'session_ready' });
      await startP;
      await flushMicrotasks();
    });
    const recorder = MockMediaRecorder.instances[0];
    expect(recorder).toBeDefined();
    expect(recorder.start).toHaveBeenCalled();

    // Speak for a bit — sets speechSeen and clears the min-recording gate.
    MockAudioContext.frequencyFrame = new Array(16).fill(200);
    act(() => { vi.advanceTimersByTime(900); });
    expect(recorder.stop).not.toHaveBeenCalled();

    // Fall silent past SILENCE_DURATION_MS (1500ms) — VAD must auto-stop.
    MockAudioContext.frequencyFrame = [];
    act(() => { vi.advanceTimersByTime(1700); });
    expect(recorder.stop).toHaveBeenCalled();
  });
});
