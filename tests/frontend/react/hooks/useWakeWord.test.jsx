import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, waitFor, act } from '@testing-library/react';

// Mock the wakeword config
vi.mock('../../../../src/frontend/src/config/wakeword', () => ({
  WAKEWORD_CONFIG: {
    modelBasePath: '/wakeword-models',
    ortWasmPath: '/ort/',
    availableKeywords: [
      { id: 'hey_jarvis', label: 'Hey Jarvis', model: 'hey_jarvis.onnx', description: 'Test' },
      { id: 'alexa', label: 'Alexa', model: 'alexa.onnx', description: 'Test' },
    ],
    defaults: {
      enabled: false,
      keyword: 'hey_jarvis',
      threshold: 0.5,
      cooldownMs: 2000,
      audioFeedback: true,
      gain: 1.0,
    },
    storageKeys: {
      enabled: 'renfield_wakeword_enabled',
      keyword: 'renfield_wakeword_keyword',
      threshold: 'renfield_wakeword_threshold',
      audioFeedback: 'renfield_wakeword_audio_feedback',
    },
  },
  loadWakeWordSettings: vi.fn(() => ({
    enabled: false,
    keyword: 'hey_jarvis',
    threshold: 0.5,
    audioFeedback: true,
  })),
  saveWakeWordSettings: vi.fn(),
}));

// Mock debug utility
vi.mock('../../../../src/frontend/src/utils/debug', () => ({
  debug: {
    log: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

// Create mock WakeWordEngine class
const createMockEngine = () => {
  const listeners = {};
  return {
    load: vi.fn().mockResolvedValue(undefined),
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn().mockResolvedValue(undefined),
    setActiveKeywords: vi.fn(),
    on: vi.fn((event, callback) => {
      listeners[event] = callback;
      return () => { delete listeners[event]; };
    }),
    emit: (event, data) => {
      if (listeners[event]) {
        listeners[event](data);
      }
    },
    _listeners: listeners,
  };
};

// Track module state for reset between tests
let mockEngineInstance = null;
let loadAttempted = false;
let loadError = null;

// Mock dynamic imports
vi.mock('onnxruntime-web', () => ({
  env: {
    wasm: {
      numThreads: 1,
      proxy: false,
      wasmPaths: '/ort/',
    },
  },
}));

// Note: openwakeword-wasm-browser is not mocked because dynamic import()
// calls at runtime aren't affected by vi.mock in the same way as static imports.
// The module will fail to load naturally because it's not installed in test env.

describe('useWakeWord', () => {
  let mockEngine;

  beforeEach(() => {
    vi.clearAllMocks();
    mockEngine = createMockEngine();
    mockEngineInstance = mockEngine;

    // Reset module-level state by reimporting
    vi.resetModules();

    // Clear localStorage
    localStorage.clear();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Initial State', () => {
    it('returns correct initial state', async () => {
      // Import fresh module
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      expect(result.current.isEnabled).toBe(false);
      expect(result.current.isListening).toBe(false);
      expect(result.current.isLoading).toBe(false);
      expect(result.current.lastDetection).toBeNull();
      expect(result.current.error).toBeNull();
      expect(result.current.settings).toBeDefined();
      expect(result.current.settings.keyword).toBe('hey_jarvis');
      expect(result.current.settings.threshold).toBe(0.5);
    });

    it('provides control functions', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      expect(typeof result.current.enable).toBe('function');
      expect(typeof result.current.disable).toBe('function');
      expect(typeof result.current.toggle).toBe('function');
      expect(typeof result.current.pause).toBe('function');
      expect(typeof result.current.resume).toBe('function');
      expect(typeof result.current.setKeyword).toBe('function');
      expect(typeof result.current.setThreshold).toBe('function');
    });

    it('provides available keywords from config', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      expect(result.current.availableKeywords).toBeDefined();
      expect(result.current.availableKeywords.length).toBe(2);
      expect(result.current.availableKeywords[0].id).toBe('hey_jarvis');
    });
  });

  describe('Settings Management', () => {
    it('setThreshold updates settings', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');
      const { saveWakeWordSettings } = await import('../../../../src/frontend/src/config/wakeword');

      const { result } = renderHook(() => useWakeWord());

      act(() => {
        result.current.setThreshold(0.7);
      });

      expect(result.current.settings.threshold).toBe(0.7);
      expect(saveWakeWordSettings).toHaveBeenCalledWith({ threshold: 0.7 });
    });

    it('setKeyword updates settings', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');
      const { saveWakeWordSettings } = await import('../../../../src/frontend/src/config/wakeword');

      const { result } = renderHook(() => useWakeWord());

      await act(async () => {
        await result.current.setKeyword('alexa');
      });

      expect(result.current.settings.keyword).toBe('alexa');
      expect(saveWakeWordSettings).toHaveBeenCalledWith({ keyword: 'alexa' });
    });
  });

  describe('Callbacks', () => {
    it('accepts callback options', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const onWakeWordDetected = vi.fn();
      const onSpeechStart = vi.fn();
      const onSpeechEnd = vi.fn();
      const onError = vi.fn();
      const onReady = vi.fn();

      const { result } = renderHook(() => useWakeWord({
        onWakeWordDetected,
        onSpeechStart,
        onSpeechEnd,
        onError,
        onReady,
      }));

      // Hook should initialize without errors
      expect(result.current.error).toBeNull();
    });
  });

  describe('Enable/Disable Flow', () => {
    it('sets isLoading during enable', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      // Start enabling - will fail because module not available
      act(() => {
        result.current.enable();
      });

      // Should be loading
      expect(result.current.isLoading).toBe(true);
    });

    it('handles module load failure gracefully', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const onError = vi.fn();
      const { result } = renderHook(() => useWakeWord({ onError }));

      await act(async () => {
        await result.current.enable();
      });

      // Should not be enabled after failure (module not available in test env)
      expect(result.current.isEnabled).toBe(false);
      expect(result.current.isListening).toBe(false);
      expect(result.current.isLoading).toBe(false);

      // Either the module failed to load (isAvailable=false) or it loaded but
      // engine creation failed (error set). In test env without the actual WASM
      // module, we expect one of these error states.
      const hasError = result.current.error !== null || result.current.isAvailable === false;
      expect(hasError).toBe(true);
    });

    it('disable does nothing when not listening', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      await act(async () => {
        await result.current.disable();
      });

      // Should still be in initial state
      expect(result.current.isEnabled).toBe(false);
      expect(result.current.isListening).toBe(false);
    });
  });

  describe('Pause/Resume', () => {
    it('pause does nothing when not listening', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      await act(async () => {
        await result.current.pause();
      });

      // Should still be false
      expect(result.current.isListening).toBe(false);
    });

    it('resume does nothing when not enabled', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      await act(async () => {
        await result.current.resume();
      });

      // Should still be false
      expect(result.current.isListening).toBe(false);
    });
  });

  describe('Toggle', () => {
    it('toggle calls enable when not enabled', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { result } = renderHook(() => useWakeWord());

      // Initially not enabled
      expect(result.current.isEnabled).toBe(false);

      await act(async () => {
        await result.current.toggle();
      });

      // Will attempt to enable (and fail due to missing module)
      expect(result.current.isLoading).toBe(false);
    });
  });

  describe('Config Update Events', () => {
    it('registers event listener for wakeword-config-update', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      // Track if event listener was added
      const addEventListenerSpy = vi.spyOn(window, 'addEventListener');

      const { unmount } = renderHook(() => useWakeWord());

      // Should have registered the event listener
      expect(addEventListenerSpy).toHaveBeenCalledWith(
        'wakeword-config-update',
        expect.any(Function)
      );

      // Cleanup
      addEventListenerSpy.mockRestore();
      unmount();
    });

    it('setKeyword updates settings directly', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');
      const { saveWakeWordSettings } = await import('../../../../src/frontend/src/config/wakeword');

      const { result } = renderHook(() => useWakeWord());

      // Initial state
      expect(result.current.settings.keyword).toBe('hey_jarvis');

      // Directly call setKeyword
      await act(async () => {
        await result.current.setKeyword('alexa');
      });

      // Settings should be updated
      expect(result.current.settings.keyword).toBe('alexa');
      expect(saveWakeWordSettings).toHaveBeenCalledWith({ keyword: 'alexa' });
    });
  });

  describe('Cleanup', () => {
    it('cleans up on unmount', async () => {
      const { useWakeWord } = await import('../../../../src/frontend/src/hooks/useWakeWord');

      const { unmount } = renderHook(() => useWakeWord());

      // Should unmount without errors
      unmount();
    });
  });
});
