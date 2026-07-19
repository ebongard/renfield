/**
 * useVoiceStream — Phase B streaming voice hook.
 *
 * Replaces the request-response pair `useAudioRecording` (mic → REST /api/voice/stt)
 * + `speakText` (REST /api/voice/tts) with a single bidirectional WebSocket
 * to the voice-server (k8s-gpu-3) at `/ws/voice`.
 *
 * Capture path:
 *   MediaRecorder (audio/webm;codecs=opus) → ws.send(binary chunk)
 *   → server VAD finalizes → server emits `final_transcript` with
 *     speaker_embedding[192]
 *
 * Playback path:
 *   ws.onmessage(binary) → strip 24-byte RFWA header → decode WAV →
 *     bounded playback queue (decoded buffers played sequentially)
 *
 * Cancellation / barge-in plumbing (plan §5.2):
 *   A monotonic generation token + a pending-request map let
 *   `cancelAllPlayback()` stop in-flight TTS cleanly — see the refs
 *   block below. The acoustic barge-in listener that drives it lives
 *   in Fork A (T4); this hook ships the shared mechanism.
 *
 * Gated by `VITE_FEATURE_VOICE_STREAM=true`; consumers pick this hook
 * vs `useAudioRecording` based on the flag. The hook does NOT do its
 * own feature-flag check — caller responsibility.
 *
 * See docs/VOICE_PIPELINE_DESIGN.md § "WebSocket protocol" for the
 * full message table.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getWebSocketUrl } from '../../../utils/env';
import { debug } from '../../../utils/debug';
import { computeRms, detectBargeIn, VOICE_MIC_CONSTRAINTS } from './voiceAudioUtils';

const RFWA_MAGIC = new Uint8Array([0x52, 0x46, 0x57, 0x41]); // "RFWA"
const HEADER_LEN = 24; // 4 magic + 16 uuid + 4 sequence

const VOICE_CODEC = 'audio/webm;codecs=opus';
const CHUNK_INTERVAL_MS = 100;

// A TTS request whose terminal frame (tts_done / cancelled / error)
// never arrives — server crash, dropped frame, WS death — would pin
// `playbackActive` true forever and (in Fork A) hold the barge-in mic
// open. This watchdog force-drops such a rid as a last-resort backstop.
const PENDING_TTS_WATCHDOG_MS = 60000;
// Grace window for the `playbackActive` false-edge. TTS is sentence-
// streamed (one request per sentence), so the raw signal flickers in
// the gaps between sentences; holding it true through the gap makes it
// one clean edge per reply instead of one per sentence.
const PLAYBACK_IDLE_GRACE_MS = 600;

// --- Fork A barge-in listener (plan §6.2) ----------------------------
// RMS threshold (0..255 scale) above which the mic counts as "voiced"
// while TTS plays. The Phase 0 AEC spike measured TTS-only RMS p95 ≈ 8
// vs human-speech median ≈ 54 on laptop speakers (6.8× margin); 20 sits
// well clear of the TTS floor (geo-mean of the two was ≈ 20.7).
const BARGE_IN_RMS_THRESHOLD = 20;
// Voiced energy must hold this long continuously before it counts as a
// barge-in — rejects transient clicks, coughs, and key taps.
const BARGE_IN_SUSTAIN_MS = 150;
// Suppress detection for this long after the listener opens, so browser
// AEC has settled and playback has actually begun.
const BARGE_IN_WARMUP_MS = 250;

// Timing diagnostics for voice pipeline. Toggle in browser console:
//   localStorage.setItem('renfield_voice_timing', '1') → enable
//   localStorage.removeItem('renfield_voice_timing') → disable
// Outputs structured timestamps so the perceived-latency breakdown
// is reconstructable from the console.
function vlog(stage: string, extra?: Record<string, unknown>): void {
  try {
    if (typeof localStorage !== 'undefined' && localStorage.getItem('renfield_voice_timing')) {
      const ms = performance.now().toFixed(1);
      // eslint-disable-next-line no-console
      console.log(`🎤 [+${ms}ms] ${stage}`, extra ?? '');
    }
  } catch { /* ignore */ }
}

// Voice-activity detection — mirrors useAudioRecording's defaults so the
// streaming path's end-of-utterance UX matches what users learned with
// the legacy hook. Server-side VAD also runs as a safety net (C.2).
const VAD = {
  // RMS below this counts as silence. Set above the measured ambient noise
  // floor so steady background noise doesn't read as speech (which kept the
  // recording open forever and fed Whisper garbage). Measured in a real noisy
  // room WITH VOICE_MIC_CONSTRAINTS: suppressed floor median 12, p95 15, max 19
  // — so 22 clears the floor with margin while staying well under speech
  // (median ~40-54 on this scale). Sits just above BARGE_IN_RMS_THRESHOLD (20),
  // the codebase's other "voiced" gate. Raising it further risks clipping soft
  // speakers; a never-voiced session (soft/distant speaker, or a wedged mic) is
  // finalized by the server VAD instead. NOTE: there is deliberately NO
  // client-side max-duration cap — long-form capture (e.g. the diary use case)
  // must be able to run well past any fixed limit.
  SILENCE_THRESHOLD: 22,
  SILENCE_DURATION_MS: 1500,  // total silence before auto-stop
  MIN_RECORDING_MS: 800,      // ignore silence in the first ~800 ms
  FFT_SIZE: 512,
  SMOOTHING: 0.3,
};

type TtsOutcome = 'done' | 'cancelled' | 'error';

interface FinalTranscript {
  text: string;
  language: string;
  speakerEmbedding: number[] | null;
  audioDurationS: number;
}

interface UseVoiceStreamOptions {
  token: string | null;
  onPartial?: (text: string, confidence: number) => void;
  onFinal?: (result: FinalTranscript) => void;
  onError?: (code: string, message: string, requestId?: string) => void;
  // Fires once per TTS request when it reaches a terminal frame:
  // 'done' (finished), 'cancelled' (client barge-in), or 'error'.
  // Consumers that count in-flight TTS MUST drain on every outcome —
  // a missed 'cancelled' leaves the counter off-by-N (plan finding 8).
  onTtsSettled?: (requestId: string, outcome: TtsOutcome) => void;
  onRecordingStart?: () => void | Promise<void>;
  onRecordingStop?: () => void;
}

function buildVoiceWsUrl(token: string | null): string {
  // VITE_WS_URL convention includes a trailing /ws — strip it and
  // append /ws/voice. Mirror useDeviceConnection's pattern.
  // Token is omitted entirely when null so voice-server's
  // auth_required=False path works for AUTH_ENABLED=false deployments.
  // VITE_VOICE_CLIENT_ID (when set) is sent as ?client= so a shared
  // multi-tenant voice-server (AUTH_MODE=registry) routes verification to
  // this product; omitted → legacy single-tenant voice-servers unaffected.
  const base = getWebSocketUrl().replace(/\/ws$/, '');
  const clientId = import.meta.env.VITE_VOICE_CLIENT_ID as string | undefined;
  const params = new URLSearchParams();
  if (token) params.set('token', token);
  if (clientId) params.set('client', clientId);
  const qs = params.toString();
  return qs ? `${base}/ws/voice?${qs}` : `${base}/ws/voice`;
}

function bytesToUuid(bytes: Uint8Array): string {
  const hex: string[] = [];
  for (let i = 0; i < 16; i += 1) hex.push(bytes[i].toString(16).padStart(2, '0'));
  return (
    `${hex.slice(0, 4).join('')}-${hex.slice(4, 6).join('')}-`
    + `${hex.slice(6, 8).join('')}-${hex.slice(8, 10).join('')}-${hex.slice(10, 16).join('')}`
  );
}

function decodeRfwaHeader(
  buf: ArrayBuffer,
): { requestId: string; sequence: number; wavBody: ArrayBuffer } | null {
  if (buf.byteLength <= HEADER_LEN) return null;
  const view = new Uint8Array(buf, 0, 4);
  for (let i = 0; i < 4; i += 1) {
    if (view[i] !== RFWA_MAGIC[i]) return null;
  }
  // Bytes 4..19 are the request_id (Python uuid.UUID.bytes — standard
  // RFC 4122 order, so this reconstructs the crypto.randomUUID() string
  // the client sent in the matching tts_request).
  const requestId = bytesToUuid(new Uint8Array(buf, 4, 16));
  const dv = new DataView(buf, 20, 4);
  const sequence = dv.getUint32(0, false); // big-endian per protocol
  return { requestId, sequence, wavBody: buf.slice(HEADER_LEN) };
}

function generateRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Minimal v4-shaped fallback for ancient runtimes that don't ship crypto.randomUUID.
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

export function useVoiceStream({
  token,
  onPartial,
  onFinal,
  onError,
  onTtsSettled,
  onRecordingStart,
  onRecordingStop,
}: UseVoiceStreamOptions) {
  const wsRef = useRef<WebSocket | null>(null);
  const pendingSocketRef = useRef<Promise<WebSocket> | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const vadFrameRef = useRef<number | null>(null);
  const sessionReadyRef = useRef(false);
  const unmountedRef = useRef(false);

  // Bounded playback queue: decoded WAV buffers played sequentially via
  // a single drain coroutine. Replaces the unbounded `.then` chain that
  // would otherwise grow microtask wrappers across long sessions.
  const chunkQueueRef = useRef<ArrayBuffer[]>([]);
  const drainingRef = useRef(false);

  // --- Barge-in / cancellation plumbing (plan §5.2) ----------------
  // Monotonic token. cancelAllPlayback() bumps it; speakText and
  // drainQueue capture it across awaits and bail if it moved, so TTS
  // the agent produced before the user interrupted never reaches the
  // speaker. A monotonic counter is self-correcting — each new request
  // captures the current value — so it never needs resetting.
  const bargeInGenerationRef = useRef(0);
  // request_id → its watchdog timer. A Map (not a Set) so a rid that
  // settles normally clears its own timer; `.size` / `.has` still give
  // the set-membership the rest of the code needs.
  const pendingTtsRef = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map());
  // The BufferSource currently playing — hoisted out of drainQueue so
  // cancelAllPlayback can hard-stop it mid-sentence.
  const currentSourceRef = useRef<AudioBufferSourceNode | null>(null);
  // Debounce timer for the playbackActive false-edge (hysteresis).
  const playbackIdleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- Fork A barge-in listener (plan §6.2/6.3) --------------------
  // A dedicated analyser-only mic stream, opened for the duration of a
  // TTS reply. On detection the SAME stream is promoted to the recorder
  // (§6.3), so the user's interrupting utterance is captured, not lost.
  const bargeInStreamRef = useRef<MediaStream | null>(null);
  const bargeInAnalyserRef = useRef<AnalyserNode | null>(null);
  const bargeInFrameRef = useRef<number | null>(null);
  const bargeInVoicedSinceRef = useRef<number | null>(null);
  const bargeInStartedAtRef = useRef<number>(0);
  // Latched once a barge-in fires so the rAF loop cannot double-trigger
  // between detection and teardown.
  const bargeInFiredRef = useRef(false);
  // Indirection so the listener effect never lists the handler as a
  // dependency — the listener must open exactly once per reply.
  const bargeInHandlerRef = useRef<() => void>(() => {});

  const [recording, setRecording] = useState(false);
  const [partialText, setPartialText] = useState<string>('');
  const [connected, setConnected] = useState(false);
  // Surfaced to consumers so they can render the same listening
  // indicator as the legacy hook (RMS bar + countdown).
  const [audioLevel, setAudioLevel] = useState(0);
  const [silenceTimeRemaining, setSilenceTimeRemaining] = useState(0);
  // True while a TTS reply is pending or playing, held through the gaps
  // between sentence-streamed chunks. Fork A's barge-in listener gates
  // on this; ChatContext re-exports it for a future manual-stop button.
  const [playbackActive, setPlaybackActive] = useState(false);

  // 'closed' is a valid AudioContextState at runtime but the TS DOM
  // lib version this project ships with omits it from the union.
  // Compare via string coercion to avoid `as any` and preserve the
  // recreate-after-close safety check.
  const ensureAudioContext = useCallback((): AudioContext => {
    if (!audioContextRef.current || (audioContextRef.current.state as string) === 'closed') {
      const Ctor = window.AudioContext || (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      audioContextRef.current = new Ctor();
    }
    return audioContextRef.current;
  }, []);

  // --- playbackActive derivation (hysteresis) ----------------------
  const isPlaybackIdle = useCallback((): boolean => (
    pendingTtsRef.current.size === 0
    && chunkQueueRef.current.length === 0
    && !drainingRef.current
  ), []);

  const markPlaybackActive = useCallback((): void => {
    if (playbackIdleTimerRef.current !== null) {
      clearTimeout(playbackIdleTimerRef.current);
      playbackIdleTimerRef.current = null;
    }
    setPlaybackActive(true);
  }, []);

  // Schedule the false-edge behind a grace window so the signal does
  // not flap in the gap between two sentence-streamed TTS requests.
  const maybeEndPlayback = useCallback((): void => {
    if (!isPlaybackIdle()) return;
    if (playbackIdleTimerRef.current !== null) return; // grace already pending
    playbackIdleTimerRef.current = setTimeout(() => {
      playbackIdleTimerRef.current = null;
      if (isPlaybackIdle()) setPlaybackActive(false);
    }, PLAYBACK_IDLE_GRACE_MS);
  }, [isPlaybackIdle]);

  // --- pending-TTS bookkeeping -------------------------------------
  const removePendingTts = useCallback((requestId: string): void => {
    const timer = pendingTtsRef.current.get(requestId);
    if (timer === undefined) return;
    clearTimeout(timer);
    pendingTtsRef.current.delete(requestId);
    maybeEndPlayback();
  }, [maybeEndPlayback]);

  const clearAllPendingTts = useCallback((): void => {
    pendingTtsRef.current.forEach((timer) => clearTimeout(timer));
    pendingTtsRef.current.clear();
  }, []);

  const drainQueue = useCallback(async () => {
    if (drainingRef.current) return;
    drainingRef.current = true;
    try {
      while (chunkQueueRef.current.length > 0) {
        if (unmountedRef.current) return;
        const wavBuf = chunkQueueRef.current.shift();
        if (!wavBuf) continue;
        // Capture the barge-in generation for this chunk. If the user
        // interrupts while decodeAudioData is awaiting below, the token
        // moves and we drop the decoded buffer rather than play audio
        // the user already cancelled.
        const gen = bargeInGenerationRef.current;
        try {
          const ctx = ensureAudioContext();
          if ((ctx.state as string) === 'closed') return;
          if (ctx.state === 'suspended') {
            try { await ctx.resume(); } catch { /* user-gesture not granted yet */ }
          }
          const audioBuffer = await ctx.decodeAudioData(wavBuf);
          if (unmountedRef.current || (ctx.state as string) === 'closed') return;
          if (gen !== bargeInGenerationRef.current) break; // barge-in during decode
          const source = ctx.createBufferSource();
          source.buffer = audioBuffer;
          source.connect(ctx.destination);
          currentSourceRef.current = source;
          await new Promise<void>((resolve) => {
            source.onended = () => {
              if (currentSourceRef.current === source) currentSourceRef.current = null;
              resolve();
            };
            source.start();
          });
        } catch (err) {
          if (!unmountedRef.current) {
            debug.log('voice: WAV chunk decode/playback failed', err);
          }
        }
      }
    } finally {
      drainingRef.current = false;
      // No source is ever playing at a drainQueue exit — every exit is
      // past an onended or before a start. Null the ref so a stale
      // handle can't linger between a barge-in and the next reply.
      currentSourceRef.current = null;
      maybeEndPlayback();
    }
  }, [ensureAudioContext, maybeEndPlayback]);

  const enqueuePlayback = useCallback((requestId: string, wavBuf: ArrayBuffer) => {
    if (unmountedRef.current) return;
    // Rid gate: only play a frame whose request is still in flight. A
    // binary frame already on the wire for a request the user just
    // cancelled (cancelAllPlayback dropped it from pendingTtsRef) is
    // discarded here instead of playing one stray sentence.
    if (!pendingTtsRef.current.has(requestId)) {
      debug.log('voice: dropping frame for settled/cancelled request', requestId);
      return;
    }
    chunkQueueRef.current.push(wavBuf);
    markPlaybackActive();
    void drainQueue();
  }, [drainQueue, markPlaybackActive]);

  const handleTextMessage = useCallback((raw: string) => {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      debug.log('voice: bad json from server', raw);
      return;
    }
    const t = msg.type as string | undefined;
    switch (t) {
      case 'session_ready':
        sessionReadyRef.current = true;
        break;
      case 'partial_transcript': {
        const text = (msg.text as string) ?? '';
        const confidence = (msg.confidence as number) ?? 0;
        vlog('partial_transcript', { text: text.slice(0, 40), confidence: confidence.toFixed(2) });
        setPartialText(text);
        onPartial?.(text, confidence);
        break;
      }
      case 'final_transcript': {
        vlog('final_transcript', { text: ((msg.text as string) || '').slice(0, 60), conf: (msg.speaker_confidence as number)?.toFixed(2) });
        // Server-side VAD beat browser-side VAD (or the user clicked
        // stop simultaneously) — stop the recorder so we don't keep
        // streaming chunks against a now-flushed decoder. recorder.onstop
        // sends stt_flush, which is a no-op on the server side after
        // the auto-finalize already ran (server is idempotent on
        // double-flush). C.4 from the design.
        const rec = recorderRef.current;
        if (rec && rec.state !== 'inactive') {
          try { rec.stop(); } catch { /* ignore */ }
        }
        const result: FinalTranscript = {
          text: (msg.text as string) ?? '',
          language: (msg.language as string) ?? 'de',
          speakerEmbedding: (msg.speaker_embedding as number[] | undefined) ?? null,
          audioDurationS: (msg.audio_duration_s as number) ?? 0,
        };
        setPartialText('');
        onFinal?.(result);
        break;
      }
      case 'tts_done': {
        const rid = (msg.request_id as string) ?? '';
        vlog('tts_done', { rid });
        removePendingTts(rid);
        onTtsSettled?.(rid, 'done');
        break;
      }
      case 'cancelled': {
        // Client barge-in honoured by the server (plan §5.1). A clean,
        // expected stop — NOT an error; never surfaces to onError.
        const rid = (msg.request_id as string) ?? '';
        vlog('tts_cancelled', { rid });
        removePendingTts(rid);
        onTtsSettled?.(rid, 'cancelled');
        break;
      }
      case 'error': {
        // `error` is also a terminal frame for a TTS request — drain
        // the pending entry so a failed rid can't pin playbackActive.
        const rid = msg.request_id as string | undefined;
        if (rid) {
          removePendingTts(rid);
          onTtsSettled?.(rid, 'error');
        }
        onError?.(
          (msg.code as string) ?? 'unknown',
          (msg.message as string) ?? '',
          rid,
        );
        break;
      }
      case 'pong':
        break;
      default:
        debug.log('voice: unknown message type', t);
    }
  }, [onPartial, onFinal, onError, onTtsSettled, removePendingTts]);

  const ensureSocket = useCallback((): Promise<WebSocket> => {
    // Fast-path: already open and session_ready.
    const existing = wsRef.current;
    if (existing && existing.readyState === WebSocket.OPEN && sessionReadyRef.current) {
      return Promise.resolve(existing);
    }
    // Concurrent callers: share the in-flight handshake promise so we
    // never create a second WebSocket while one is CONNECTING.
    if (pendingSocketRef.current) return pendingSocketRef.current;

    // token=null is allowed (no-auth deployments). buildVoiceWsUrl
    // omits the query param; voice-server's auth.authenticate treats
    // empty token as anonymous when auth_required=False.
    const url = buildVoiceWsUrl(token);
    const ws = new WebSocket(url);
    ws.binaryType = 'arraybuffer';
    wsRef.current = ws;
    sessionReadyRef.current = false;

    ws.onopen = () => {
      setConnected(true);
      ws.send(JSON.stringify({ type: 'session_start', codec: VOICE_CODEC }));
    };
    ws.onclose = () => {
      setConnected(false);
      sessionReadyRef.current = false;
      if (wsRef.current === ws) wsRef.current = null;
      // R3: bidirectional cleanup — stop recorder if WS dropped mid-session.
      const rec = recorderRef.current;
      if (rec && rec.state !== 'inactive') {
        try { rec.stop(); } catch { /* ignore */ }
      }
      setRecording(false);
      // No terminal frame will arrive for in-flight TTS once the socket
      // is gone — drop the pending map (and its watchdog timers) and the
      // undelivered queue so a leaked rid can't pin playbackActive true.
      clearAllPendingTts();
      chunkQueueRef.current = [];
      if (playbackIdleTimerRef.current !== null) {
        clearTimeout(playbackIdleTimerRef.current);
        playbackIdleTimerRef.current = null;
      }
      setPlaybackActive(false);
    };
    ws.onerror = (ev) => {
      debug.log('voice: ws error', ev);
    };
    ws.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        handleTextMessage(ev.data);
        return;
      }
      if (ev.data instanceof ArrayBuffer) {
        const decoded = decodeRfwaHeader(ev.data);
        if (decoded) {
          vlog('binary_frame', { size: decoded.wavBody.byteLength, seq: decoded.sequence });
          // decoded.wavBody is a standalone WAV with its own header.
          enqueuePlayback(decoded.requestId, decoded.wavBody);
        } else {
          debug.log('voice: binary frame missing RFWA header, dropping');
        }
      }
    };

    const handshake = new Promise<WebSocket>((resolve, reject) => {
      const cleanup = () => {
        ws.removeEventListener('message', onReady);
        ws.removeEventListener('close', onCloseEarly);
        ws.removeEventListener('error', onErrorEarly);
      };
      const onReady = (ev: MessageEvent) => {
        if (typeof ev.data !== 'string') return;
        try {
          const m = JSON.parse(ev.data as string) as { type?: string };
          if (m.type !== 'session_ready') return;
        } catch {
          return;
        }
        cleanup();
        resolve(ws);
      };
      const onCloseEarly = () => {
        cleanup();
        reject(new Error('voice-stream: ws closed before session_ready'));
      };
      const onErrorEarly = () => {
        cleanup();
        reject(new Error('voice-stream: ws error before session_ready'));
      };
      ws.addEventListener('message', onReady);
      ws.addEventListener('close', onCloseEarly, { once: true });
      ws.addEventListener('error', onErrorEarly, { once: true });
    });

    pendingSocketRef.current = handshake;
    handshake.finally(() => {
      if (pendingSocketRef.current === handshake) pendingSocketRef.current = null;
    });
    return handshake;
  }, [token, handleTextMessage, enqueuePlayback, clearAllPendingTts]);

  // Build the MediaRecorder + VAD loop on an already-open mic stream
  // and start streaming. Shared by startRecording (which gets a fresh
  // getUserMedia stream) and the barge-in promotion (§6.3, which reuses
  // the already-open listener stream — no second getUserMedia).
  const beginRecordingWithStream = useCallback(async (
    stream: MediaStream,
    ws: WebSocket,
  ): Promise<void> => {
    streamRef.current = stream;
    // Surface whether the mic constraints actually applied — some Android/WebView
    // builds silently ignore `autoGainControl:false`, which lets AGC push the
    // noise floor back to speech level and revives the noise-as-speech bug on
    // exactly those devices. Logged so it's diagnosable instead of invisible.
    try {
      const s = stream.getAudioTracks()[0]?.getSettings?.();
      if (s) debug.log('voice mic settings:', { autoGainControl: s.autoGainControl, noiseSuppression: s.noiseSuppression, echoCancellation: s.echoCancellation });
    } catch { /* getSettings unsupported — ignore */ }
    const recorder = new MediaRecorder(stream, { mimeType: VOICE_CODEC });
    recorderRef.current = recorder;

    // Voice-activity detector — auto-stop after 1.5 s of silence so
    // users don't have to find the mic button. Mirrors the legacy
    // useAudioRecording's RMS check. Server-side VAD (C.2) is the
    // safety net for browsers without AnalyserNode access.
    try {
      const ctx = ensureAudioContext();
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = VAD.FFT_SIZE;
      analyser.smoothingTimeConstant = VAD.SMOOTHING;
      source.connect(analyser);
      analyserRef.current = analyser;

      const dataArray = new Uint8Array(analyser.frequencyBinCount);
      const recordingStart = Date.now();
      let lastSoundAt = Date.now();
      let speechSeen = false;
      // Throttle React state updates from the 60 Hz rAF loop to ~15 Hz
      // (every ~66 ms). Updating state every frame caused so much
      // context-consumer re-rendering that the partial-transcript
      // bubble's setState calls were getting visually lost in the
      // churn. RMS at 15 Hz is still smooth for the visualizer.
      let lastStateAt = 0;

      const checkSilence = () => {
        const a = analyserRef.current;
        const rec = recorderRef.current;
        if (!a || !rec || rec.state === 'inactive') {
          vadFrameRef.current = null;
          return;
        }
        const rms = computeRms(a, dataArray);
        const now = Date.now();
        const recordingTime = now - recordingStart;

        // Compute the silence-auto-stop decision every frame (cheap,
        // VAD timing precision matters), but throttle React state
        // updates to ~15 Hz so we don't churn ChatContext consumers
        // (which would also drown out partial_transcript updates).
        const shouldUpdateState = now - lastStateAt > 66;

        if (rms > VAD.SILENCE_THRESHOLD) {
          lastSoundAt = now;
          speechSeen = true;
          if (shouldUpdateState) {
            setAudioLevel(Math.round(rms));
            setSilenceTimeRemaining(0);
            lastStateAt = now;
          }
        } else if (
          speechSeen
          && recordingTime > VAD.MIN_RECORDING_MS
          && now - lastSoundAt >= VAD.SILENCE_DURATION_MS
        ) {
          // Silence-auto-stop. recorder.onstop fires next, which
          // sends stt_flush and the server takes it from there.
          try { rec.stop(); } catch { /* ignore */ }
          vadFrameRef.current = null;
          return;
        } else if (shouldUpdateState) {
          setAudioLevel(Math.round(rms));
          if (speechSeen && recordingTime > VAD.MIN_RECORDING_MS) {
            const remaining = Math.max(0, VAD.SILENCE_DURATION_MS - (now - lastSoundAt));
            setSilenceTimeRemaining(remaining);
          }
          lastStateAt = now;
        }
        vadFrameRef.current = requestAnimationFrame(checkSilence);
      };
      vadFrameRef.current = requestAnimationFrame(checkSilence);
    } catch (e) {
      debug.log('voice: VAD setup failed; recording without silence-auto-stop', e);
    }

    recorder.ondataavailable = (e: BlobEvent) => {
      if (!e.data || e.data.size === 0) return;
      if (ws.readyState !== WebSocket.OPEN) return;
      void e.data.arrayBuffer().then((buf) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(buf);
      });
    };
    recorder.onstop = () => {
      if (vadFrameRef.current !== null) {
        cancelAnimationFrame(vadFrameRef.current);
        vadFrameRef.current = null;
      }
      analyserRef.current = null;
      if (ws.readyState === WebSocket.OPEN) {
        vlog('stt_flush_sent');
        ws.send(JSON.stringify({ type: 'stt_flush' }));
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      recorderRef.current = null;
      setRecording(false);
      setAudioLevel(0);
      setSilenceTimeRemaining(0);
      try { onRecordingStop?.(); } catch (e) { debug.log('voice: onRecordingStop threw', e); }
    };

    try {
      const maybe = onRecordingStart?.();
      if (maybe instanceof Promise) await maybe;
    } catch (e) {
      debug.log('voice: onRecordingStart threw', e);
    }

    recorder.start(CHUNK_INTERVAL_MS);
    setRecording(true);
  }, [ensureAudioContext, onRecordingStart, onRecordingStop]);

  const startRecording = useCallback(async (): Promise<void> => {
    if (recording) return;

    if (!MediaRecorder.isTypeSupported(VOICE_CODEC)) {
      onError?.('codec_unsupported', `browser does not support codec ${VOICE_CODEC}`);
      return;
    }

    let ws: WebSocket;
    try {
      ws = await ensureSocket();
    } catch (e) {
      onError?.('ws_open_failed', e instanceof Error ? e.message : String(e));
      return;
    }

    // Each utterance gets its own session_start so the server spawns a
    // fresh decoder (the first works off the WS-open decoder; later ones
    // would hit "decoder is None" after finalize). Idempotent server-
    // side; message order guarantees the decoder is up before the first
    // chunk arrives ~100 ms later.
    if (ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'session_start', codec: VOICE_CODEC }));
    }

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia(VOICE_MIC_CONSTRAINTS);
    } catch (e) {
      onError?.('mic_denied', e instanceof Error ? e.message : String(e));
      return;
    }

    await beginRecordingWithStream(stream, ws);
  }, [recording, ensureSocket, beginRecordingWithStream, onError]);

  const stopRecording = useCallback((): void => {
    const rec = recorderRef.current;
    if (rec && rec.state !== 'inactive') {
      rec.stop();
    } else {
      setRecording(false);
    }
  }, []);

  const speakText = useCallback(async (text: string, language?: string): Promise<string | null> => {
    vlog('speakText:start', { len: text.length, preview: text.slice(0, 40) });
    // Capture the barge-in generation before any await. If the user
    // interrupts during the socket handshake below, the token moves and
    // we drop this request rather than speak over their new turn.
    const gen = bargeInGenerationRef.current;
    let ws: WebSocket;
    try {
      ws = await ensureSocket();
    } catch (e) {
      debug.log('voice: speakText ensureSocket failed', e);
      return null;
    }
    if (gen !== bargeInGenerationRef.current) {
      vlog('speakText:gated', { reason: 'barge-in during handshake' });
      return null;
    }
    const requestId = generateRequestId();
    // Watchdog backstop: if no terminal frame ever arrives, force-drop
    // the rid so it can't pin playbackActive (and Fork A's mic) open.
    const watchdog = setTimeout(() => {
      if (pendingTtsRef.current.has(requestId)) {
        debug.log('voice: tts watchdog force-dropped', requestId);
        removePendingTts(requestId);
      }
    }, PENDING_TTS_WATCHDOG_MS);
    pendingTtsRef.current.set(requestId, watchdog);
    markPlaybackActive();
    ws.send(JSON.stringify({ type: 'tts_request', request_id: requestId, text, language }));
    vlog('tts_request_sent', { rid: requestId, len: text.length });
    return requestId;
  }, [ensureSocket, markPlaybackActive, removePendingTts]);

  // Send a cancel frame for one request. Private — cancelAllPlayback is
  // the public operation; no caller wants per-request cancellation.
  const cancelTts = useCallback((requestId: string): void => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'cancel', request_id: requestId }));
    }
  }, []);

  // Stop all TTS — the barge-in entry point. Drives the server-side
  // cancel, the local queue flush, and the generation bump that gates
  // any post-interrupt TTS the agent is still producing.
  const cancelAllPlayback = useCallback((): void => {
    // Bump the generation so TTS the agent generates for the
    // interrupted turn no-ops in speakText / drainQueue.
    bargeInGenerationRef.current += 1;
    // Cancel every in-flight request server-side and drop it locally.
    // Optimistic removal — a binary frame still on the wire for one of
    // these rids is then dropped by enqueuePlayback's rid gate.
    pendingTtsRef.current.forEach((timer, rid) => {
      clearTimeout(timer);
      cancelTts(rid);
    });
    pendingTtsRef.current.clear();
    // Flush undelivered audio and hard-stop what is playing now.
    chunkQueueRef.current = [];
    if (currentSourceRef.current) {
      try { currentSourceRef.current.stop(); } catch { /* already stopped */ }
      currentSourceRef.current = null;
    }
    // Fork A wires synchronous barge-in-listener teardown in here (T4).
    maybeEndPlayback();
  }, [cancelTts, maybeEndPlayback]);

  // --- Fork A: barge-in detected → promote listener to recorder (§6.3)
  const handleBargeInDetected = useCallback((): void => {
    if (bargeInFiredRef.current) return;
    bargeInFiredRef.current = true;
    vlog('barge_in_detected');
    // Stop the listening loop now; KEEP the stream — it is about to
    // become the recording stream.
    if (bargeInFrameRef.current !== null) {
      cancelAnimationFrame(bargeInFrameRef.current);
      bargeInFrameRef.current = null;
    }
    bargeInAnalyserRef.current = null;
    const stream = bargeInStreamRef.current;
    // Null the ref before the playbackActive effect's teardown can run,
    // so teardown sees no listener stream to stop — ownership has moved.
    bargeInStreamRef.current = null;

    // Stop the assistant.
    cancelAllPlayback();

    if (!stream || recording) {
      // No stream to promote, or a recording is somehow already live —
      // the cancel above is all that was needed.
      stream?.getTracks().forEach((tr) => tr.stop());
      return;
    }

    // Promote the listener's stream straight into the recorder so the
    // user's interrupting utterance becomes the next query — captured,
    // not discarded (plan finding 6). No second getUserMedia.
    void (async () => {
      let ws: WebSocket;
      try {
        ws = await ensureSocket();
      } catch (e) {
        onError?.('ws_open_failed', e instanceof Error ? e.message : String(e));
        stream.getTracks().forEach((tr) => tr.stop());
        return;
      }
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'session_start', codec: VOICE_CODEC }));
      }
      await beginRecordingWithStream(stream, ws);
    })();
  }, [cancelAllPlayback, ensureSocket, beginRecordingWithStream, onError, recording]);

  // Keep the ref pointed at the latest handler so the listener effect
  // below never depends on its identity — the listener opens exactly
  // once per reply, not on every handler re-creation.
  bargeInHandlerRef.current = handleBargeInDetected;

  // The barge-in listener (plan §6.2): a dedicated analyser-only mic
  // stream, open for the duration of a TTS reply, watching for the user
  // to talk over the assistant. Gated on the hysteresis-debounced
  // `playbackActive` so it is one open per reply, not one per sentence.
  // Phase 0 measured browser AEC keeps the assistant's own TTS well
  // below BARGE_IN_RMS_THRESHOLD on laptop speakers.
  useEffect(() => {
    // Also gate on !recording: the listener and an active recording are
    // mutually exclusive. A manual mic-button press during TTS would
    // otherwise leave a second mic stream open alongside the recorder.
    if (!playbackActive || recording) return undefined;
    let cancelled = false;

    const open = async (): Promise<void> => {
      let stream: MediaStream;
      try {
        stream = await navigator.mediaDevices.getUserMedia(VOICE_MIC_CONSTRAINTS);
      } catch (e) {
        // Mic permission revoked between turns — degrade silently to
        // no-barge-in for this reply (finding 3B). Mic button + wake
        // word still work; the reply just plays uninterruptibly.
        debug.log('voice: barge-in listener mic unavailable — barge-in off this reply', e);
        return;
      }
      if (cancelled || unmountedRef.current) {
        stream.getTracks().forEach((tr) => tr.stop());
        return;
      }
      bargeInStreamRef.current = stream;
      bargeInFiredRef.current = false;
      bargeInVoicedSinceRef.current = null;
      bargeInStartedAtRef.current = performance.now();
      try {
        const ctx = ensureAudioContext();
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = VAD.FFT_SIZE;
        analyser.smoothingTimeConstant = VAD.SMOOTHING;
        source.connect(analyser);
        bargeInAnalyserRef.current = analyser;
        const scratch = new Uint8Array(analyser.frequencyBinCount);

        const tick = (): void => {
          const a = bargeInAnalyserRef.current;
          if (!a || bargeInFiredRef.current) {
            bargeInFrameRef.current = null;
            return;
          }
          const now = performance.now();
          if (now - bargeInStartedAtRef.current >= BARGE_IN_WARMUP_MS) {
            const rms = computeRms(a, scratch);
            const r = detectBargeIn(
              rms, bargeInVoicedSinceRef.current, now,
              BARGE_IN_RMS_THRESHOLD, BARGE_IN_SUSTAIN_MS,
            );
            bargeInVoicedSinceRef.current = r.voicedSince;
            if (r.bargeIn) {
              bargeInFrameRef.current = null;
              bargeInHandlerRef.current();
              return;
            }
          }
          bargeInFrameRef.current = requestAnimationFrame(tick);
        };
        bargeInFrameRef.current = requestAnimationFrame(tick);
      } catch (e) {
        debug.log('voice: barge-in analyser setup failed', e);
        stream.getTracks().forEach((tr) => tr.stop());
        bargeInStreamRef.current = null;
      }
    };
    void open();

    return () => {
      cancelled = true;
      if (bargeInFrameRef.current !== null) {
        cancelAnimationFrame(bargeInFrameRef.current);
        bargeInFrameRef.current = null;
      }
      bargeInAnalyserRef.current = null;
      // Stop the stream ONLY if it is still the listener's. If a
      // barge-in fired, the handler nulled this ref and handed the
      // stream to the recorder — leave that one running.
      const s = bargeInStreamRef.current;
      if (s) {
        s.getTracks().forEach((tr) => tr.stop());
        bargeInStreamRef.current = null;
      }
    };
  }, [playbackActive, recording, ensureAudioContext]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      unmountedRef.current = true;
      if (vadFrameRef.current !== null) {
        cancelAnimationFrame(vadFrameRef.current);
        vadFrameRef.current = null;
      }
      analyserRef.current = null;
      const rec = recorderRef.current;
      if (rec && rec.state !== 'inactive') {
        try { rec.stop(); } catch { /* ignore */ }
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
      const ws = wsRef.current;
      if (ws && ws.readyState !== WebSocket.CLOSED) {
        try { ws.close(); } catch { /* ignore */ }
      }
      // Tear down the barge-in listener if one is still open.
      if (bargeInFrameRef.current !== null) {
        cancelAnimationFrame(bargeInFrameRef.current);
        bargeInFrameRef.current = null;
      }
      bargeInAnalyserRef.current = null;
      bargeInStreamRef.current?.getTracks().forEach((tr) => tr.stop());
      bargeInStreamRef.current = null;
      // Clear any queued chunks so the drain loop exits promptly.
      chunkQueueRef.current = [];
      // Drop pending-TTS watchdog timers + the playback-idle timer so
      // they can't fire into a destroyed component.
      pendingTtsRef.current.forEach((timer) => clearTimeout(timer));
      pendingTtsRef.current.clear();
      if (playbackIdleTimerRef.current !== null) {
        clearTimeout(playbackIdleTimerRef.current);
        playbackIdleTimerRef.current = null;
      }
      const ctx = audioContextRef.current;
      if (ctx && (ctx.state as string) !== 'closed') {
        try { void ctx.close(); } catch { /* ignore */ }
      }
    };
  }, []);

  return {
    startRecording,
    stopRecording,
    speakText,
    cancelAllPlayback,
    recording,
    partialText,
    connected,
    audioLevel,
    silenceTimeRemaining,
    playbackActive,
  };
}

export type { FinalTranscript, TtsOutcome };
