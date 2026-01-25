import { useState, useEffect, useRef, useCallback } from 'react';
import {
  WAKEWORD_CONFIG,
  loadWakeWordSettings,
  saveWakeWordSettings,
} from '../config/wakeword';
import { debug } from '../utils/debug';

// Lazy-loaded wake word engine class
let WakeWordEngineClass = null;
let loadAttempted = false;
let loadError = null;

/**
 * Lazy load the wake word engine module
 * @returns {Promise<boolean>} - True if loaded successfully
 */
async function loadWakeWordEngine() {
  if (loadAttempted) {
    return WakeWordEngineClass !== null;
  }

  loadAttempted = true;

  try {
    // Configure ONNX Runtime before importing the engine
    const ort = await import('onnxruntime-web');

    // Disable multi-threading to avoid SharedArrayBuffer requirement
    ort.env.wasm.numThreads = 1;

    // Disable proxy mode - it causes dynamic import issues with Vite
    ort.env.wasm.proxy = false;

    // Set explicit WASM file paths to avoid Vite module interception
    ort.env.wasm.wasmPaths = '/ort/';

    debug.log('✅ ONNX Runtime (WASM) configured with paths:', ort.env.wasm.wasmPaths);

    const module = await import('openwakeword-wasm-browser');
    WakeWordEngineClass = module.default || module.WakeWordEngine;
    debug.log('✅ Wake word engine loaded successfully');
    return true;
  } catch (e) {
    loadError = e;
    console.warn('⚠️ openwakeword-wasm-browser not available:', e.message);
    console.warn('💡 Run: npm install && docker compose up -d --build');
    return false;
  }
}

/**
 * React hook for wake word detection using OpenWakeWord WASM
 *
 * @param {object} options - Hook options
 * @param {function} options.onWakeWordDetected - Callback when wake word is detected
 * @param {function} options.onSpeechStart - Callback when speech starts
 * @param {function} options.onSpeechEnd - Callback when speech ends
 * @param {function} options.onError - Callback for errors
 * @param {function} options.onReady - Callback when engine is ready
 * @returns {object} - Hook state and controls
 */
export function useWakeWord({
  onWakeWordDetected,
  onSpeechStart,
  onSpeechEnd,
  onError,
  onReady,
} = {}) {
  // State
  const [isEnabled, setIsEnabled] = useState(false);
  const [isListening, setIsListening] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isReady, setIsReady] = useState(false);
  const [lastDetection, setLastDetection] = useState(null);
  const [error, setError] = useState(null);
  const [settings, setSettings] = useState(() => loadWakeWordSettings());
  const [isAvailable, setIsAvailable] = useState(!loadAttempted || WakeWordEngineClass !== null);

  // Refs
  const engineRef = useRef(null);
  const unsubscribersRef = useRef([]);
  const callbacksRef = useRef({ onWakeWordDetected, onSpeechStart, onSpeechEnd, onError, onReady });
  const isEnabledRef = useRef(false); // Ref to avoid stale closure in resume()

  // Keep callbacks ref updated
  useEffect(() => {
    callbacksRef.current = { onWakeWordDetected, onSpeechStart, onSpeechEnd, onError, onReady };
  }, [onWakeWordDetected, onSpeechStart, onSpeechEnd, onError, onReady]);

  // Keep isEnabledRef in sync with state
  useEffect(() => {
    isEnabledRef.current = isEnabled;
    debug.log('🔄 isEnabledRef updated to:', isEnabled);
  }, [isEnabled]);

  // Initialize engine
  const initEngine = useCallback(async () => {
    if (!WakeWordEngineClass) {
      throw new Error('Wake word detection not available. Please rebuild the application.');
    }

    const engine = new WakeWordEngineClass({
      baseAssetUrl: WAKEWORD_CONFIG.modelBasePath,
      // Let onnxruntime-web load its WASM files from default location
      keywords: [settings.keyword],
      detectionThreshold: settings.threshold,
      cooldownMs: WAKEWORD_CONFIG.defaults.cooldownMs,
    });

    return engine;
  }, [settings.keyword, settings.threshold]);

  // Enable wake word listening
  const enable = useCallback(async () => {
    if (isListening || isLoading) return;

    setIsLoading(true);
    setError(null);

    try {
      // Lazy load the wake word engine module
      const loaded = await loadWakeWordEngine();
      if (!loaded) {
        setIsAvailable(false);
        const err = loadError || new Error('Wake word detection not available. Please run: npm install && docker compose up -d --build');
        setError(err);
        callbacksRef.current.onError?.(err);
        setIsLoading(false);
        return;
      }
      setIsAvailable(true);

      // Create engine if not exists
      if (!engineRef.current) {
        engineRef.current = await initEngine();
      }

      const engine = engineRef.current;

      // Load models
      await engine.load();

      // Subscribe to events
      const unsubReady = engine.on('ready', () => {
        setIsReady(true);
        callbacksRef.current.onReady?.();
      });

      const unsubDetect = engine.on('detect', ({ keyword, score, at }) => {
        const detection = { keyword, score, timestamp: at || Date.now() };
        setLastDetection(detection);
        callbacksRef.current.onWakeWordDetected?.(keyword, score);
      });

      const unsubSpeechStart = engine.on('speech-start', () => {
        callbacksRef.current.onSpeechStart?.();
      });

      const unsubSpeechEnd = engine.on('speech-end', () => {
        callbacksRef.current.onSpeechEnd?.();
      });

      const unsubError = engine.on('error', (err) => {
        setError(err);
        callbacksRef.current.onError?.(err);
      });

      unsubscribersRef.current = [
        unsubReady,
        unsubDetect,
        unsubSpeechStart,
        unsubSpeechEnd,
        unsubError,
      ];

      // Start listening
      await engine.start({
        gain: WAKEWORD_CONFIG.defaults.gain,
      });

      setIsEnabled(true);
      setIsListening(true);
      saveWakeWordSettings({ enabled: true });
    } catch (err) {
      console.error('Failed to enable wake word:', err);

      // Check for Firefox sample rate mismatch error
      if (err.message && err.message.includes('sample-rate')) {
        const firefoxError = new Error(
          'Wake word detection is not supported in Firefox due to AudioContext sample rate limitations. ' +
          'Please use Chrome, Edge, or Safari for wake word detection, or use the manual recording button.'
        );
        firefoxError.name = 'BrowserNotSupportedError';
        setError(firefoxError);
        callbacksRef.current.onError?.(firefoxError);
      } else {
        setError(err);
        callbacksRef.current.onError?.(err);
      }
    } finally {
      setIsLoading(false);
    }
  }, [isListening, isLoading, initEngine]);

  // Disable wake word listening
  const disable = useCallback(async () => {
    if (!isListening) return;

    try {
      // Unsubscribe from events
      unsubscribersRef.current.forEach(unsub => unsub?.());
      unsubscribersRef.current = [];

      // Stop engine
      if (engineRef.current) {
        await engineRef.current.stop();
      }

      setIsListening(false);
      setIsEnabled(false);
      setIsReady(false);
      saveWakeWordSettings({ enabled: false });
    } catch (err) {
      console.error('Failed to disable wake word:', err);
    }
  }, [isListening]);

  // Toggle wake word
  const toggle = useCallback(async () => {
    if (isEnabled) {
      await disable();
    } else {
      await enable();
    }
  }, [isEnabled, enable, disable]);

  // Pause listening temporarily (e.g., while recording)
  const pause = useCallback(async () => {
    debug.log('⏸️ pause() called - isListening:', isListening, 'hasEngine:', !!engineRef.current);

    if (!isListening || !engineRef.current) {
      debug.log('⚠️ pause() skipped: not listening or no engine');
      return;
    }

    try {
      await engineRef.current.stop();
      setIsListening(false);
      debug.log('✅ Wake word paused (isEnabled stays true)');
    } catch (err) {
      console.error('Failed to pause wake word:', err);
    }
  }, [isListening]);

  // Resume listening after pause
  const resume = useCallback(async () => {
    // Use refs to avoid stale closure issues
    const currentIsEnabled = isEnabledRef.current;

    debug.log('🔄 resume() called - checking conditions:', {
      isListening,
      isEnabled: currentIsEnabled,
      isEnabledRef: isEnabledRef.current,
      hasEngine: !!engineRef.current
    });

    if (isListening) {
      debug.log('⚠️ resume() skipped: already listening');
      return;
    }
    if (!currentIsEnabled) {
      debug.log('⚠️ resume() skipped: wake word not enabled (using ref)');
      return;
    }
    if (!engineRef.current) {
      debug.log('⚠️ resume() skipped: no engine');
      return;
    }

    try {
      debug.log('▶️ Starting wake word engine...');
      await engineRef.current.start({
        gain: WAKEWORD_CONFIG.defaults.gain,
      });
      setIsListening(true);
      debug.log('✅ Wake word engine resumed successfully');
    } catch (err) {
      console.error('Failed to resume wake word:', err);
      setError(err);
    }
  }, [isListening]); // Removed isEnabled from deps since we use ref

  // Update keyword
  const setKeyword = useCallback(async (keyword) => {
    const newSettings = { ...settings, keyword };
    setSettings(newSettings);
    saveWakeWordSettings({ keyword });

    // If currently listening, restart with new keyword
    if (isListening && engineRef.current) {
      try {
        engineRef.current.setActiveKeywords([keyword]);
      } catch {
        // If setActiveKeywords fails, restart engine
        await disable();
        await enable();
      }
    }
  }, [settings, isListening, disable, enable]);

  // Update threshold
  const setThreshold = useCallback((threshold) => {
    const newSettings = { ...settings, threshold };
    setSettings(newSettings);
    saveWakeWordSettings({ threshold });
    // Note: threshold changes may require engine restart
  }, [settings]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      unsubscribersRef.current.forEach(unsub => unsub?.());
      if (engineRef.current) {
        engineRef.current.stop().catch(() => {});
        engineRef.current = null;
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
  }, []); // Only run once on mount

  // Listen for config updates from server (via WebSocket)
  useEffect(() => {
    const handleConfigUpdate = (event) => {
      const config = event.detail;
      debug.log('🔄 Wake word config update from server:', config);

      // Update keyword if provided
      if (config.wake_words && config.wake_words[0]) {
        const newKeyword = config.wake_words[0];
        if (newKeyword !== settings.keyword) {
          debug.log(`🎤 Updating wake word: ${settings.keyword} -> ${newKeyword}`);
          setKeyword(newKeyword);
        }
      }

      // Update threshold if provided
      if (config.threshold !== undefined && config.threshold !== settings.threshold) {
        debug.log(`🎚️ Updating threshold: ${settings.threshold} -> ${config.threshold}`);
        setThreshold(config.threshold);
      }
    };

    window.addEventListener('wakeword-config-update', handleConfigUpdate);
    return () => window.removeEventListener('wakeword-config-update', handleConfigUpdate);
  }, [settings.keyword, settings.threshold, setKeyword, setThreshold]);

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
    setThreshold,

    // Config access
    availableKeywords: WAKEWORD_CONFIG.availableKeywords,
  };
}

export default useWakeWord;
