/**
 * Recording for the second login factor -- microphone in, blob out.
 *
 * WHY NOT `useAudioRecording`
 * ---------------------------
 * The chat hook (`pages/ChatPage/hooks/useAudioRecording.ts`) is built for
 * dictation and ends in a POST to `/api/voice/stt`. Two of its properties are
 * outright wrong here:
 *
 * 1. **It cuts on silence via VAD.** A login phrase with a short pause in it
 *    would be truncated -- and the shorter the recording, the worse the voice
 *    comparison separates.
 * 2. **It transcribes.** The second factor needs the RAW recording; a
 *    transcript does not help it, and the detour through speech recognition
 *    would be an extra service in the login path.
 *
 * Hence a small hook of its own: no VAD, no transcription, but a MINIMUM and a
 * maximum duration. The ~40 lines of `MediaRecorder` scaffolding resemble the
 * chat hook; that is scaffolding, not business logic. Whoever touches the chat
 * hook next can extract the shared core -- doing it beforehand would be a
 * rebuild of a path `ChatContext` uses, for a gain of forty lines.
 *
 * THE MINIMUM DURATION IS NOT A SECURITY CHECK
 * ---------------------------------------------
 * It is guidance: half a second of audio yields a poor comparison, and the
 * user should notice that before submitting rather than collect a rejection.
 * An attacker posts straight to the API anyway. The server-side duration bound
 * (H3 of the 2026-09-27 review) is a separate, open item and is NOT replaced
 * by this.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

/** Minimum duration from which submitting is unlocked. */
export const MIN_DURATION_MS = 1500;
/** Maximum duration -- the recording stops itself after this. */
export const MAX_DURATION_MS = 8000;

export type RecordingError =
  /** The user denied the microphone, or the browser blocks it. */
  | 'permission'
  /** Kein Mikrofon vorhanden. */
  | 'no-device'
  /** The browser cannot record (no MediaRecorder / no getUserMedia). */
  | 'unsupported'
  /** Alles andere. */
  | 'unknown';

interface State {
  recording: boolean;
  /** 0..1, for the level meter. */
  level: number;
  /** Bisherige Dauer in Millisekunden. */
  elapsedMs: number;
  /** Fertige Aufnahme, sobald gestoppt. */
  blob: Blob | null;
  error: RecordingError | null;
}

const INITIAL: State = {
  recording: false, level: 0, elapsedMs: 0, blob: null, error: null,
};

interface AudioContextCapableWindow {
  AudioContext?: typeof AudioContext;
  webkitAudioContext?: typeof AudioContext;
}

export function useVoiceFactorRecording() {
  const [state, setState] = useState<State>(INITIAL);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const frameRef = useRef<number | null>(null);
  const startedAtRef = useRef<number>(0);
  const stopTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Prevents a `setState` running after unmount (React 18 StrictMode mounts
  // twice, and the microphone must not stay open through that).
  const aliveRef = useRef(true);

  const teardown = useCallback(() => {
    if (frameRef.current !== null) {
      cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }
    if (stopTimerRef.current) {
      clearTimeout(stopTimerRef.current);
      stopTimerRef.current = null;
    }
    // The tracks MUST be stopped individually, or the browser's microphone
    // indicator stays on -- on a LOGIN page a quietly open microphone is the
    // last thing anyone wants.
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (audioCtxRef.current && audioCtxRef.current.state !== 'closed') {
      void audioCtxRef.current.close();
    }
    audioCtxRef.current = null;
    analyserRef.current = null;
    recorderRef.current = null;
  }, []);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
      teardown();
    };
  }, [teardown]);

  const stop = useCallback(() => {
    const rec = recorderRef.current;
    if (rec && rec.state !== 'inactive') {
      rec.stop();   // `onstop` sets the blob and cleans up
    }
  }, []);

  const start = useCallback(async () => {
    if (state.recording) return;

    const hasMedia = typeof navigator !== 'undefined'
      && !!navigator.mediaDevices?.getUserMedia
      && typeof MediaRecorder !== 'undefined';
    if (!hasMedia) {
      setState({ ...INITIAL, error: 'unsupported' });
      return;
    }

    setState({ ...INITIAL, recording: true });
    chunksRef.current = [];

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        // The same settings as the chat path: the model's reference profiles
        // come from material recorded this way.
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (err) {
      const name = (err as { name?: string })?.name ?? '';
      // These cases are DELIBERATELY distinguishable: they are about the
      // user's device, not a secret of the server's. Somebody who does not
      // know they denied the microphone cannot allow it.
      const error: RecordingError =
        name === 'NotAllowedError' || name === 'SecurityError' ? 'permission'
          : name === 'NotFoundError' || name === 'DevicesNotFoundError' ? 'no-device'
            : 'unknown';
      if (aliveRef.current) setState({ ...INITIAL, error });
      return;
    }

    if (!aliveRef.current) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }
    streamRef.current = stream;

    // Level meter, so the user sees something is arriving. Purely visual --
    // no decision hangs on it.
    try {
      const w = window as unknown as AudioContextCapableWindow;
      const Ctor = w.AudioContext ?? w.webkitAudioContext;
      if (Ctor) {
        const ctx = new Ctor();
        audioCtxRef.current = ctx;
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        ctx.createMediaStreamSource(stream).connect(analyser);
        analyserRef.current = analyser;
      }
    } catch {
      // Without the meter the recording is still valid -- do not abort.
      analyserRef.current = null;
    }

    startedAtRef.current = Date.now();
    const data = analyserRef.current
      ? new Uint8Array(analyserRef.current.frequencyBinCount)
      : null;

    const tick = () => {
      if (!aliveRef.current) return;
      let level = 0;
      if (analyserRef.current && data) {
        analyserRef.current.getByteFrequencyData(data);
        let sum = 0;
        for (const v of data) sum += v;
        level = Math.min(1, sum / data.length / 128);
      }
      setState((s) => (s.recording
        ? { ...s, level, elapsedMs: Date.now() - startedAtRef.current }
        : s));
      frameRef.current = requestAnimationFrame(tick);
    };
    frameRef.current = requestAnimationFrame(tick);

    const recorder = new MediaRecorder(stream);
    recorderRef.current = recorder;
    recorder.ondataavailable = (e: BlobEvent) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    recorder.onstop = () => {
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
      const elapsedMs = Date.now() - startedAtRef.current;
      teardown();
      if (aliveRef.current) {
        setState({ recording: false, level: 0, elapsedMs, blob, error: null });
      }
    };
    recorder.start();

    // Self-stop at the maximum duration. Without it the microphone runs until
    // den Knopf wiederfindet.
    stopTimerRef.current = setTimeout(() => {
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop();
    }, MAX_DURATION_MS);
  }, [state.recording, teardown]);

  const reset = useCallback(() => {
    teardown();
    setState(INITIAL);
  }, [teardown]);

  return {
    ...state,
    /** Long enough to be compared meaningfully. */
    longEnough: state.elapsedMs >= MIN_DURATION_MS,
    start,
    stop,
    reset,
  };
}
