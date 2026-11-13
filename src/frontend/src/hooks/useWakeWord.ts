import { useState, useEffect, useRef, useCallback } from 'react';
import {
  WAKEWORD_CONFIG,
  loadWakeWordSettings,
  saveWakeWordSettings,
  parseKeywords,
  sameKeywordSet,
  type WakeWordSettings,
  type KeywordConfig,
} from '../config/wakeword';
import {
  loadWakeWordEngine,
  getWakeWordEngineClass,
  getWakeWordLoadError,
  wasWakeWordLoadAttempted,
  type WakeWordEngine,
} from './wakewordEngineLoader';
import { debug } from '../utils/debug';
import { haptic } from '../utils/haptics';

// Detection result
interface WakeWordDetection {
  keyword: string;
  score: number;
  timestamp: number;
}

// Hook options
interface UseWakeWordOptions {
  onWakeWordDetected?: (keyword: string, score: number) => void;
  onSpeechStart?: () => void;
  onSpeechEnd?: () => void;
  onError?: (error: Error) => void;
  onReady?: () => void;
}

// Hook return type
export interface UseWakeWordResult {
  isEnabled: boolean;
  isListening: boolean;
  isLoading: boolean;
  isReady: boolean;
  isAvailable: boolean;
  lastDetection: WakeWordDetection | null;
  error: Error | null;
  settings: WakeWordSettings;
  enable: () => Promise<void>;
  disable: () => Promise<void>;
  toggle: () => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;
  setKeyword: (keyword: string) => Promise<void>;
  toggleKeyword: (id: string) => Promise<void>;
  setThreshold: (threshold: number) => void;
  availableKeywords: KeywordConfig[];
}

/**
 * React hook for wake word detection using OpenWakeWord WASM.
 *
 * **Multi-keyword:** `settings.keyword` is a comma-separated active SET (the same
 * form the server settings page uses). The engine loads one model per id, so a
 * German+English household detects every pushed wake word — satellites already
 * do this; the browser now matches them. The engine's `start()` opens a fresh
 * mic + AudioContext and `stop()` closes them, so changing the set means
 * recreating the engine (it can only load models chosen at construction — we
 * never rely on `setActiveKeywords`).
 *
 * **Concurrency model.** Turn-ON transitions (enable/resume/keyword-rebuild) are
 * serialized through a single "arm" promise chain and reconcile the engine to
 * the desired state (`desiredEnabledRef`/`desiredListeningRef`/`keywordsRef`).
 * Turn-OFF transitions (disable/pause) and the error handler are PRE-EMPTIVE:
 * they run immediately and bump `genRef`, which an in-flight arm re-checks after
 * every await and aborts on — so a hung `start()` can never block mic-off, an
 * engine error can never leave a green-but-dead UI, and an unmount mid-build
 * never leaks the mic.
 */
export function useWakeWord({
  onWakeWordDetected,
  onSpeechStart,
  onSpeechEnd,
  onError,
  onReady,
}: UseWakeWordOptions = {}): UseWakeWordResult {
  // State
  const [isEnabled, setIsEnabled] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [lastDetection, setLastDetection] = useState<WakeWordDetection | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [settings, setSettings] = useState<WakeWordSettings>(() => loadWakeWordSettings());
  const [isAvailable, setIsAvailable] = useState(
    !wasWakeWordLoadAttempted() || getWakeWordEngineClass() !== null,
  );

  // Engine + subscriptions
  const engineRef = useRef<WakeWordEngine | null>(null);
  const unsubscribersRef = useRef<Array<() => void>>([]);
  const builtKeywordsRef = useRef<string[]>([]); // the set the live engine was built with
  const builtThresholdRef = useRef<number>(settings.threshold); // its detection threshold
  const engineStartedRef = useRef(false); // whether the live engine is capturing (started)
  const callbacksRef = useRef({ onWakeWordDetected, onSpeechStart, onSpeechEnd, onError, onReady });

  // Actual-state mirrors (kept in sync synchronously with the setState calls).
  const isEnabledRef = useRef(false);
  const isListeningRef = useRef(false);
  // Desired state — what the user/serve wants. The arm loop reconciles to these.
  const desiredEnabledRef = useRef(false);
  const desiredListeningRef = useRef(false);
  // Desired keyword set + threshold, read at engine-build time.
  const keywordsRef = useRef<string[]>(parseKeywords(settings.keyword));
  const thresholdRef = useRef<number>(settings.threshold);

  // Generation counter — bumped by every pre-emptive turn-OFF (disable/pause),
  // by an engine error, by a keyword change, and by unmount. An in-flight arm
  // captures the generation and aborts if it changed across an await.
  const genRef = useRef(0);
  const mountedRef = useRef(true);
  // Serializes turn-ON (arm) transitions so two builds never race engineRef.
  const armChainRef = useRef<Promise<unknown>>(Promise.resolve());

  // Keep callbacks ref updated
  useEffect(() => {
    callbacksRef.current = { onWakeWordDetected, onSpeechStart, onSpeechEnd, onError, onReady };
  }, [onWakeWordDetected, onSpeechStart, onSpeechEnd, onError, onReady]);

  // Build a fresh engine from the CURRENT keyword set + threshold (from refs).
  const initEngine = useCallback(async (): Promise<WakeWordEngine> => {
    const EngineClass = getWakeWordEngineClass();
    if (!EngineClass) {
      throw new Error('Wake word detection not available. Please rebuild the application.');
    }

    // Model file map (includes custom keywords like hey_renfield).
    const modelFiles: Record<string, string> = {};
    for (const kw of WAKEWORD_CONFIG.availableKeywords) {
      modelFiles[kw.id] = kw.model;
    }

    // Load EVERY active keyword's model. Fall back to the default single keyword
    // if the set somehow resolved to nothing.
    const keywords = keywordsRef.current.length
      ? [...keywordsRef.current]
      : [WAKEWORD_CONFIG.defaults.keyword];

    return new EngineClass({
      baseAssetUrl: WAKEWORD_CONFIG.modelBasePath,
      keywords,
      modelFiles,
      detectionThreshold: thresholdRef.current,
      cooldownMs: WAKEWORD_CONFIG.defaults.cooldownMs,
    });
  }, []);

  // Fully tear down the engine: unsubscribe, stop (closes mic + AudioContext),
  // drop the ref. Low-level; callers own the state/gen bookkeeping.
  const teardownEngine = useCallback(async () => {
    unsubscribersRef.current.forEach((unsub) => unsub?.());
    unsubscribersRef.current = [];
    const engine = engineRef.current;
    engineRef.current = null;
    builtKeywordsRef.current = [];
    engineStartedRef.current = false;
    setIsReady(false);
    if (engine) {
      try {
        await engine.stop();
      } catch (err) {
        console.error('Failed to stop wake word engine:', err);
      }
    }
  }, []);

  // Map raw engine/browser errors to friendly, actionable messages. Used by
  // every build/start failure path (enable, resume, keyword-rebuild).
  const reportEnableError = useCallback((err: unknown) => {
    console.error('Failed to start wake word:', err);
    const raw = err instanceof Error ? err : new Error(String(err));

    let out = raw;
    if (raw.message && raw.message.includes('sample-rate')) {
      // Firefox: AudioContext sample rate mismatch
      out = new Error(
        'Wake word detection is not supported in Firefox due to AudioContext sample rate limitations. ' +
          'Please use Chrome or Edge for wake word detection, or use the manual recording button.',
      );
      out.name = 'BrowserNotSupportedError';
    } else if (
      raw.message &&
      (raw.message.includes('SharedArrayBuffer') ||
        raw.message.includes('cross-origin isolated') ||
        raw.message.includes('CompileError') ||
        raw.message.includes('WebAssembly'))
    ) {
      // Safari/WebKit: WASM or SharedArrayBuffer issues
      out = new Error(
        'Wake word detection requires WebAssembly threading which may not be available in this browser. ' +
          'Please use Chrome or Edge, or use the manual recording button.',
      );
      out.name = 'BrowserNotSupportedError';
    }

    setError(out);
    callbacksRef.current.onError?.(out);
  }, []);

  // Wire a fresh engine's events to hook state/callbacks.
  const subscribe = useCallback(
    (engine: WakeWordEngine) => {
      const unsubReady = engine.on('ready', () => {
        setIsReady(true);
        callbacksRef.current.onReady?.();
      });
      const unsubDetect = engine.on('detect', ({ keyword, score, at }) => {
        // Ignore detections while not actively listening — a paused engine keeps
        // its subscription (resume fast-path) and could emit a buffered detect
        // mid-recording, spuriously re-triggering the wake word.
        if (!isListeningRef.current) return;
        const detection: WakeWordDetection = { keyword, score, timestamp: at || Date.now() };
        setLastDetection(detection);
        callbacksRef.current.onWakeWordDetected?.(keyword, score);
      });
      const unsubSpeechStart = engine.on('speech-start', () => {
        callbacksRef.current.onSpeechStart?.();
      });
      const unsubSpeechEnd = engine.on('speech-end', () => {
        callbacksRef.current.onSpeechEnd?.();
      });
      const unsubError = engine.on('error', (err: Error) => {
        setError(err);
        callbacksRef.current.onError?.(err);
        // The engine died — reflect it honestly (status dot goes yellow) and
        // DROP it so a recovery resume() rebuilds a fresh one. Bump the
        // generation so any in-flight arm can't re-mark this dead engine as
        // listening (the green-but-dead race). isEnabled stays true so the
        // user's intent is preserved and recovery can fire.
        isListeningRef.current = false;
        setIsListening(false);
        genRef.current++;
        void teardownEngine();
      });

      unsubscribersRef.current = [
        unsubReady,
        unsubDetect,
        unsubSpeechStart,
        unsubSpeechEnd,
        unsubError,
      ];
    },
    [teardownEngine],
  );

  // Reconcile the engine to the desired state. Serialized via `arm()`. Re-checks
  // the generation / mounted / desired flags after every await and bails (tearing
  // down any half-built engine) if a pre-emptive op superseded it.
  const armInner = useCallback(async () => {
    if (!mountedRef.current || !desiredEnabledRef.current) return;
    const myGen = genRef.current;

    // Ensure the WASM module is available.
    const loaded = await loadWakeWordEngine();
    if (!loaded) {
      setIsAvailable(false);
      // If we were listening (a rebuild path), clear it so the UI isn't left
      // green-but-dead and recovery can fire.
      isListeningRef.current = false;
      setIsListening(false);
      const err =
        getWakeWordLoadError() ||
        new Error(
          'Wake word detection not available. Please run: npm install && docker compose up -d --build',
        );
      setError(err);
      callbacksRef.current.onError?.(err);
      return;
    }
    setIsAvailable(true);
    if (myGen !== genRef.current || !mountedRef.current || !desiredEnabledRef.current) return;

    // Rebuild if the live engine drifted from the desired keyword set OR threshold
    // (both are only read at construction).
    if (
      engineRef.current &&
      (!sameKeywordSet(builtKeywordsRef.current.join(','), keywordsRef.current.join(',')) ||
        builtThresholdRef.current !== thresholdRef.current)
    ) {
      await teardownEngine();
      if (myGen !== genRef.current || !mountedRef.current || !desiredEnabledRef.current) return;
    }

    // Build (load + subscribe) if there's no engine.
    if (!engineRef.current) {
      let engine: WakeWordEngine;
      try {
        engine = await initEngine();
        await engine.load();
      } catch (err) {
        // Build failed — if we were listening (rebuild path), clear it so the UI
        // isn't stranded green with no engine.
        isListeningRef.current = false;
        setIsListening(false);
        reportEnableError(err);
        return;
      }
      if (myGen !== genRef.current || !mountedRef.current || !desiredEnabledRef.current) {
        // Superseded during the build — discard the engine we just made.
        try {
          await engine.stop();
        } catch {
          /* discarded */
        }
        return;
      }
      subscribe(engine);
      engineRef.current = engine;
      builtKeywordsRef.current = [...keywordsRef.current];
      builtThresholdRef.current = thresholdRef.current;
    }

    // Start capturing if we should be listening. Guard on engineStartedRef so a
    // second arm queued behind the first can't double-start the same engine.
    if (desiredListeningRef.current) {
      if (!engineStartedRef.current) {
        try {
          await engineRef.current.start({ gain: WAKEWORD_CONFIG.defaults.gain });
        } catch (err) {
          await teardownEngine();
          isListeningRef.current = false;
          setIsListening(false);
          reportEnableError(err);
          return;
        }
        if (myGen !== genRef.current || !mountedRef.current || !desiredListeningRef.current) {
          // disable / pause / unmount / keyword-change landed during start — undo.
          await teardownEngine();
          isListeningRef.current = false;
          setIsListening(false);
          return;
        }
        engineStartedRef.current = true;
      }
      isEnabledRef.current = true;
      isListeningRef.current = true;
      setIsEnabled(true);
      setIsListening(true);
      saveWakeWordSettings({ enabled: true });
    }
  }, [initEngine, subscribe, teardownEngine, reportEnableError]);

  // Queue an arm (turn-ON reconcile) after any in-flight one. Errors in one op
  // don't poison the chain.
  const arm = useCallback((): Promise<void> => {
    const next = armChainRef.current.then(armInner, armInner);
    armChainRef.current = next.catch(() => undefined);
    return next;
  }, [armInner]);

  // Enable wake word listening.
  const enable = useCallback((): Promise<void> => {
    if (isEnabledRef.current && isListeningRef.current) return Promise.resolve();
    // Only flip the spinner once we know there's real work (avoids a flicker when
    // called redundantly while already listening).
    setIsLoading(true);
    setError(null);
    desiredEnabledRef.current = true;
    desiredListeningRef.current = true;
    return arm().finally(() => setIsLoading(false));
  }, [arm]);

  // Disable wake word listening. PRE-EMPTIVE: runs immediately (not queued behind
  // a possibly-hung arm) and stops the engine directly, so the mic can ALWAYS be
  // turned off. Works from any state (listening, paused, post-error).
  const disable = useCallback(async () => {
    if (!desiredEnabledRef.current && !engineRef.current) return;
    desiredEnabledRef.current = false;
    desiredListeningRef.current = false;
    genRef.current++; // abort any in-flight arm
    isEnabledRef.current = false;
    isListeningRef.current = false;
    setIsEnabled(false);
    setIsListening(false);
    setIsLoading(false);
    saveWakeWordSettings({ enabled: false });
    await teardownEngine();
  }, [teardownEngine]);

  // Toggle wake word
  const toggle = useCallback(async () => {
    haptic('tap');
    if (isEnabled) {
      await disable();
    } else {
      await enable();
    }
  }, [isEnabled, enable, disable]);

  // Pause listening temporarily (e.g., while recording). PRE-EMPTIVE: stops the
  // engine immediately so recording never overlaps a live wake-word mic.
  const pause = useCallback(async () => {
    debug.log('⏸️ pause() called - isListening:', isListeningRef.current, 'hasEngine:', !!engineRef.current);
    // Nothing to pause only if we're neither listening NOR arming toward it. If
    // an enable()/resume() arm is in flight (desiredListening=true, isListening
    // still false), we must still cancel it so it can't open the mic on top of
    // the recording that's starting.
    if (!isListeningRef.current && !desiredListeningRef.current) {
      debug.log('⚠️ pause() skipped: not listening and not arming');
      return;
    }
    // Record the pause intent + abort any in-flight arm FIRST — even if the
    // engine is transiently null mid-rebuild — so nothing re-starts the mic.
    desiredListeningRef.current = false;
    genRef.current++;
    isListeningRef.current = false;
    setIsListening(false);
    engineStartedRef.current = false;
    // Drop the engine entirely rather than keeping it for a resume fast-path. A
    // retained instance let pause()'s async stop() race a concurrent resume()'s
    // start() on the SAME engine (stop landing after start → green-but-dead; or a
    // failed stop → double-start mic leak), and let its still-registered detect
    // handler fire mid-recording. resume() rebuilds a fresh engine — start()
    // always reopens the mic + AudioContext anyway, so this costs only a model
    // reload, and pause()'s stop() now runs on an instance nobody else touches.
    await teardownEngine();
    debug.log('✅ Wake word paused (isEnabled stays true)');
  }, [teardownEngine]);

  // Resume listening after pause. arm() rebuilds if the engine was dropped
  // (keyword change while paused, or an error), else just re-starts.
  const resume = useCallback((): Promise<void> => {
    if (isListeningRef.current) return Promise.resolve();
    if (!desiredEnabledRef.current) return Promise.resolve();
    desiredListeningRef.current = true;
    return arm();
  }, [arm]);

  // Update the active keyword set (single id or comma-separated). Persists it
  // (survives reload) and, if the set changed while listening, rebuilds the
  // engine so every keyword is loaded.
  const setKeyword = useCallback(
    async (keyword: string) => {
      const ids = parseKeywords(keyword);
      // Ignore a selection that resolves to nothing we ship a model for.
      if (ids.length === 0) return;
      const normalized = ids.join(',');
      if (sameKeywordSet(normalized, keywordsRef.current.join(','))) return;

      keywordsRef.current = ids; // read by the next engine build
      setSettings((prev) => ({ ...prev, keyword: normalized }));
      saveWakeWordSettings({ keyword: normalized });
      genRef.current++; // supersede any in-flight arm building the old set

      // Reconcile now if we should be listening; otherwise the next resume()/
      // enable() picks up the new set (arm's drift check rebuilds).
      if (desiredListeningRef.current) {
        await arm();
      }
    },
    [arm],
  );

  // Toggle one keyword in/out of the active set. Reads the CURRENT set from the
  // ref (not a render closure) so rapid successive toggles don't lose updates.
  const toggleKeyword = useCallback(
    async (id: string) => {
      const current = keywordsRef.current;
      const next = current.includes(id) ? current.filter((k) => k !== id) : [...current, id];
      if (next.length === 0) return; // keep at least one active
      await setKeyword(next.join(','));
    },
    [setKeyword],
  );

  // Update threshold. The engine only reads detectionThreshold at construction,
  // so when we're listening this rebuilds to apply the new sensitivity. Rapid
  // slider drags bump the generation each tick, so intermediate rebuilds abort
  // and only the last value is applied.
  const setThreshold = useCallback(
    (threshold: number) => {
      if (threshold === thresholdRef.current) return;
      thresholdRef.current = threshold;
      setSettings((prev) => ({ ...prev, threshold }));
      saveWakeWordSettings({ threshold });
      genRef.current++;
      if (desiredListeningRef.current) {
        void arm();
      }
    },
    [arm],
  );

  // Cleanup on unmount — pre-empt any in-flight arm and stop the engine so the
  // mic/AudioContext never leak after navigation.
  useEffect(() => {
    return () => {
      mountedRef.current = false;
      genRef.current++;
      unsubscribersRef.current.forEach((unsub) => unsub?.());
      unsubscribersRef.current = [];
      const engine = engineRef.current;
      engineRef.current = null;
      if (engine) {
        engine.stop().catch(() => {});
      }
    };
  }, []);

  // Auto-enable if was previously enabled
  useEffect(() => {
    const savedSettings = loadWakeWordSettings();
    if (savedSettings.enabled && !isEnabled && !isLoading) {
      // Delay auto-enable to allow page to fully load
      const timer = setTimeout(() => {
        enable();
      }, 1000);
      return () => clearTimeout(timer);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Only run once on mount

  // Listen for config updates from server (via WebSocket). Loads the FULL
  // server-pushed wake_words set, not just the first (multi-language household).
  useEffect(() => {
    const handleConfigUpdate = (event: CustomEvent<{ wake_words?: string[]; threshold?: number }>) => {
      const config = event.detail;
      debug.log('🔄 Wake word config update from server:', config);

      if (config.wake_words && config.wake_words.length > 0) {
        const ids = parseKeywords(config.wake_words.join(','));
        if (ids.length > 0 && !sameKeywordSet(ids.join(','), keywordsRef.current.join(','))) {
          debug.log(
            `🎤 Updating wake words: [${keywordsRef.current.join(', ')}] -> [${ids.join(', ')}]`,
          );
          void setKeyword(ids.join(','));
        }
      }

      if (config.threshold !== undefined && config.threshold !== thresholdRef.current) {
        debug.log(`🎚️ Updating threshold: ${thresholdRef.current} -> ${config.threshold}`);
        setThreshold(config.threshold);
      }
    };

    window.addEventListener('wakeword-config-update', handleConfigUpdate as EventListener);
    return () => window.removeEventListener('wakeword-config-update', handleConfigUpdate as EventListener);
  }, [setKeyword, setThreshold]);

  return {
    // State
    isEnabled,
    isListening,
    isLoading,
    isReady,
    isAvailable,
    lastDetection,
    error,
    settings,

    // Controls
    enable,
    disable,
    toggle,
    pause,
    resume,
    setKeyword,
    toggleKeyword,
    setThreshold,

    // Config access
    availableKeywords: WAKEWORD_CONFIG.availableKeywords,
  };
}

export default useWakeWord;
