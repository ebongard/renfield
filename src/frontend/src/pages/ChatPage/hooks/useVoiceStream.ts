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

const RFWA_MAGIC = new Uint8Array([0x52, 0x46, 0x57, 0x41]); // "RFWA"
const HEADER_LEN = 24; // 4 magic + 16 uuid + 4 sequence

const VOICE_CODEC = 'audio/webm;codecs=opus';
const CHUNK_INTERVAL_MS = 100;

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
  onTtsDone?: (requestId: string) => void;
  onRecordingStart?: () => void | Promise<void>;
  onRecordingStop?: () => void;
}

function buildVoiceWsUrl(token: string): string {
  // VITE_WS_URL convention includes a trailing /ws — strip it and
  // append /ws/voice. Mirror useDeviceConnection's pattern.
  const base = getWebSocketUrl().replace(/\/ws$/, '');
  return `${base}/ws/voice?token=${encodeURIComponent(token)}`;
}

function decodeRfwaHeader(buf: ArrayBuffer): { sequence: number; wavBody: ArrayBuffer } | null {
  if (buf.byteLength <= HEADER_LEN) return null;
  const view = new Uint8Array(buf, 0, 4);
  for (let i = 0; i < 4; i += 1) {
    if (view[i] !== RFWA_MAGIC[i]) return null;
  }
  const dv = new DataView(buf, 20, 4);
  const sequence = dv.getUint32(0, false); // big-endian per protocol
  return { sequence, wavBody: buf.slice(HEADER_LEN) };
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
  onTtsDone,
  onRecordingStart,
  onRecordingStop,
}: UseVoiceStreamOptions) {
  const wsRef = useRef<WebSocket | null>(null);
  const pendingSocketRef = useRef<Promise<WebSocket> | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const sessionReadyRef = useRef(false);
  const unmountedRef = useRef(false);

  // Bounded playback queue: decoded WAV buffers played sequentially via
  // a single drain coroutine. Replaces the unbounded `.then` chain that
  // would otherwise grow microtask wrappers across long sessions.
  const chunkQueueRef = useRef<ArrayBuffer[]>([]);
  const drainingRef = useRef(false);

  const [recording, setRecording] = useState(false);
  const [partialText, setPartialText] = useState<string>('');
  const [connected, setConnected] = useState(false);

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

  const drainQueue = useCallback(async () => {
    if (drainingRef.current) return;
    drainingRef.current = true;
    try {
      while (chunkQueueRef.current.length > 0) {
        if (unmountedRef.current) return;
        const wavBuf = chunkQueueRef.current.shift();
        if (!wavBuf) continue;
        try {
          const ctx = ensureAudioContext();
          if ((ctx.state as string) === 'closed') return;
          if (ctx.state === 'suspended') {
            try { await ctx.resume(); } catch { /* user-gesture not granted yet */ }
          }
          const audioBuffer = await ctx.decodeAudioData(wavBuf);
          if (unmountedRef.current || (ctx.state as string) === 'closed') return;
          const source = ctx.createBufferSource();
          source.buffer = audioBuffer;
          source.connect(ctx.destination);
          await new Promise<void>((resolve) => {
            source.onended = () => resolve();
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
    }
  }, [ensureAudioContext]);

  const enqueuePlayback = useCallback((wavBuf: ArrayBuffer) => {
    if (unmountedRef.current) return;
    chunkQueueRef.current.push(wavBuf);
    void drainQueue();
  }, [drainQueue]);

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
        setPartialText(text);
        onPartial?.(text, confidence);
        break;
      }
      case 'final_transcript': {
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
      case 'tts_done':
        onTtsDone?.((msg.request_id as string) ?? '');
        break;
      case 'error':
        onError?.(
          (msg.code as string) ?? 'unknown',
          (msg.message as string) ?? '',
          msg.request_id as string | undefined,
        );
        break;
      case 'pong':
        break;
      default:
        debug.log('voice: unknown message type', t);
    }
  }, [onPartial, onFinal, onError, onTtsDone]);

  const ensureSocket = useCallback((): Promise<WebSocket> => {
    // Fast-path: already open and session_ready.
    const existing = wsRef.current;
    if (existing && existing.readyState === WebSocket.OPEN && sessionReadyRef.current) {
      return Promise.resolve(existing);
    }
    // Concurrent callers: share the in-flight handshake promise so we
    // never create a second WebSocket while one is CONNECTING.
    if (pendingSocketRef.current) return pendingSocketRef.current;

    if (!token) {
      return Promise.reject(new Error('voice-stream: no auth token'));
    }

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
          // decoded.wavBody is a standalone WAV with its own header.
          enqueuePlayback(decoded.wavBody);
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
  }, [token, handleTextMessage, enqueuePlayback]);

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

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      onError?.('mic_denied', e instanceof Error ? e.message : String(e));
      return;
    }

    streamRef.current = stream;
    const recorder = new MediaRecorder(stream, { mimeType: VOICE_CODEC });
    recorderRef.current = recorder;

    recorder.ondataavailable = (e: BlobEvent) => {
      if (!e.data || e.data.size === 0) return;
      if (ws.readyState !== WebSocket.OPEN) return;
      void e.data.arrayBuffer().then((buf) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(buf);
      });
    };
    recorder.onstop = () => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stt_flush' }));
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
      recorderRef.current = null;
      setRecording(false);
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
  }, [recording, ensureSocket, onError, onRecordingStart, onRecordingStop]);

  const stopRecording = useCallback((): void => {
    const rec = recorderRef.current;
    if (rec && rec.state !== 'inactive') {
      rec.stop();
    } else {
      setRecording(false);
    }
  }, []);

  const speakText = useCallback(async (text: string, language?: string): Promise<string> => {
    const ws = await ensureSocket();
    const requestId = generateRequestId();
    ws.send(JSON.stringify({ type: 'tts_request', request_id: requestId, text, language }));
    return requestId;
  }, [ensureSocket]);

  const cancelTts = useCallback((requestId: string): void => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'cancel', request_id: requestId }));
    }
  }, []);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      unmountedRef.current = true;
      const rec = recorderRef.current;
      if (rec && rec.state !== 'inactive') {
        try { rec.stop(); } catch { /* ignore */ }
      }
      streamRef.current?.getTracks().forEach((t) => t.stop());
      const ws = wsRef.current;
      if (ws && ws.readyState !== WebSocket.CLOSED) {
        try { ws.close(); } catch { /* ignore */ }
      }
      // Clear any queued chunks so the drain loop exits promptly.
      chunkQueueRef.current = [];
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
    cancelTts,
    recording,
    partialText,
    connected,
  };
}

export type { FinalTranscript };
