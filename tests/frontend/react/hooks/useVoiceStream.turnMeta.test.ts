/**
 * useVoiceStream — content-free turn-end metadata for the turn trace (#1613).
 *
 * Every `final_transcript` becomes a FinalTranscript whose `voiceTurn` says why
 * the turn ended (the server's reason, `client_flush` refined by why the client
 * stopped), how long the recording was, and how soon after the previous
 * answer's playback it started (0 = while the answer still played, null = no
 * previous answer). Harness copied from `useVoiceStream.bargein.test.ts`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import {
  gapSinceAnswerS,
  useVoiceStream,
  voiceTurnEndReason,
  type FinalTranscript,
} from '../../../../src/frontend/src/pages/ChatPage/hooks/useVoiceStream';

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

async function startRecording(result: { current: VoiceHook }): Promise<void> {
  await act(async () => {
    await result.current.startRecording();
    await flushMicrotasks();
  });
}

function emitFinal(ws: MockVoiceWebSocket, extra: Record<string, unknown> = {}): void {
  act(() => {
    ws.emitJson({ type: 'final_transcript', text: 'hallo', language: 'de', audio_duration_s: 2.5, ...extra });
  });
}

describe('voiceTurnEndReason', () => {
  it('keeps a reason only the server knows', () => {
    expect(voiceTurnEndReason('server_vad', 'button')).toBe('server_vad');
    expect(voiceTurnEndReason('max_utterance', null)).toBe('max_utterance');
  });

  it('refines client_flush by why the client stopped', () => {
    expect(voiceTurnEndReason('client_flush', 'silence')).toBe('silence');
    expect(voiceTurnEndReason('client_flush', 'button')).toBe('button');
    expect(voiceTurnEndReason('client_flush', null)).toBe('client_flush');
  });

  it('an older server without the field leaves the client cause', () => {
    expect(voiceTurnEndReason(undefined, 'silence')).toBe('silence');
    expect(voiceTurnEndReason(undefined, null)).toBe('unknown');
    expect(voiceTurnEndReason('bogus', null)).toBe('unknown');
  });
});

describe('gapSinceAnswerS', () => {
  it('measures from the end of the previous answer', () => {
    expect(gapSinceAnswerS(10_000, 6_000, false)).toBe(4);
  });
  it('is 0 while the answer still plays (a barge-in)', () => {
    expect(gapSinceAnswerS(10_000, 6_000, true)).toBe(0);
    expect(gapSinceAnswerS(10_000, null, true)).toBe(0);
  });
  it('is null without a previous answer', () => {
    expect(gapSinceAnswerS(10_000, null, false)).toBeNull();
  });
});

describe('useVoiceStream — voiceTurn on the final transcript', () => {
  it('a manual stop after a first recording: button, the duration, no previous answer', async () => {
    const finals: FinalTranscript[] = [];
    const { result } = renderHook(() => useVoiceStream({ onFinal: (r) => finals.push(r) }));
    const pending = result.current.startRecording();
    await act(async () => { await Promise.resolve(); });
    const ws = lastWs();
    await act(async () => {
      ws.fireOpen();
      ws.emitJson({ type: 'session_ready' });
      await pending;
      await flushMicrotasks();
    });
    act(() => { result.current.stopRecording(); });
    expect(sentOfType(ws, 'stt_flush')).toHaveLength(1);
    emitFinal(ws, { end_reason: 'client_flush' });

    expect(finals).toHaveLength(1);
    expect(finals[0].voiceTurn).toEqual({
      end_reason: 'button', audio_duration_s: 2.5, gap_since_answer_s: null,
      turn_key: expect.any(String),
    });
  });

  it('one page keeps one turn key; another page gets another', async () => {
    const finals: FinalTranscript[] = [];
    const { result } = renderHook(() => useVoiceStream({ onFinal: (r) => finals.push(r) }));
    const { ws } = await speakAndConnect(result);
    await startRecording(result);
    emitFinal(ws, { end_reason: 'server_vad' });
    await startRecording(result);
    emitFinal(ws, { end_reason: 'server_vad' });
    const other: FinalTranscript[] = [];
    const second = renderHook(() => useVoiceStream({ onFinal: (r) => other.push(r) }));
    const { ws: ws2 } = await speakAndConnect(second.result);
    await startRecording(second.result);
    emitFinal(ws2, { end_reason: 'server_vad' });

    expect(finals).toHaveLength(2);
    expect(finals[0].voiceTurn.turn_key).toBe(finals[1].voiceTurn.turn_key);
    expect(finals[0].voiceTurn.turn_key.length).toBeGreaterThan(8);
    expect(other[0].voiceTurn.turn_key).not.toBe(finals[0].voiceTurn.turn_key);
  });

  it('a server-VAD end keeps server_vad', async () => {
    const finals: FinalTranscript[] = [];
    const { result } = renderHook(() => useVoiceStream({ onFinal: (r) => finals.push(r) }));
    const { ws } = await speakAndConnect(result);
    act(() => { ws.emitJson({ type: 'tts_done', request_id: sentOfType(ws, 'tts_request')[0].request_id }); });
    await startRecording(result);
    emitFinal(ws, { end_reason: 'server_vad' });
    expect(finals[0].voiceTurn.end_reason).toBe('server_vad');
  });

  it('measures the gap from the end of the previous answer\'s playback', async () => {
    const finals: FinalTranscript[] = [];
    const { result } = renderHook(() => useVoiceStream({ onFinal: (r) => finals.push(r) }));
    const { ws, rid } = await speakAndConnect(result);
    await act(async () => {
      ws.emitMessage(makeRfwaFrame(rid)); // plays and ends at once (mock)
      await flushMicrotasks();
      ws.emitJson({ type: 'tts_done', request_id: rid });
    });
    await act(async () => { vi.advanceTimersByTime(4000); });
    await startRecording(result);
    emitFinal(ws, { end_reason: 'client_flush' });
    expect(finals[0].voiceTurn.gap_since_answer_s).toBeCloseTo(4, 1);
  });

  it('a recording that starts while the answer is still in flight has gap 0', async () => {
    const finals: FinalTranscript[] = [];
    const { result } = renderHook(() => useVoiceStream({ onFinal: (r) => finals.push(r) }));
    const { ws } = await speakAndConnect(result); // tts_request pending
    await startRecording(result);
    emitFinal(ws, { end_reason: 'client_flush' });
    expect(finals[0].voiceTurn.gap_since_answer_s).toBe(0);
  });

  it('a tail final of the same utterance does not reuse the gap', async () => {
    const finals: FinalTranscript[] = [];
    const { result } = renderHook(() => useVoiceStream({ onFinal: (r) => finals.push(r) }));
    const { ws } = await speakAndConnect(result); // tts_request pending -> gap 0
    await startRecording(result);
    emitFinal(ws, { end_reason: 'server_vad' });
    emitFinal(ws, { end_reason: 'client_flush' }); // the stt_flush tail, no new mic start
    expect(finals).toHaveLength(2);
    expect(finals[0].voiceTurn.gap_since_answer_s).toBe(0);
    expect(finals[1].voiceTurn.gap_since_answer_s).toBeNull();
  });
});
