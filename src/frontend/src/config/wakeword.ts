/**
 * Wake Word Detection Configuration
 *
 * Configuration for the OpenWakeWord WASM browser-based wake word detection.
 * Models are loaded from /public/wakeword-models/
 */

// Keyword configuration type
export interface KeywordConfig {
  id: string;
  label: string;
  model: string;
  description: string;
}

// Wake word settings type
export interface WakeWordSettings {
  enabled: boolean;
  keyword: string;
  threshold: number;
  audioFeedback: boolean;
}

// Storage keys type
interface StorageKeys {
  enabled: string;
  keyword: string;
  threshold: string;
  audioFeedback: string;
}

// Wake word defaults type
interface WakeWordDefaults {
  enabled: boolean;
  keyword: string;
  threshold: number;
  cooldownMs: number;
  audioFeedback: boolean;
  gain: number;
}

// Main config type
export interface WakeWordConfigType {
  modelBasePath: string;
  ortWasmPath: string;
  availableKeywords: KeywordConfig[];
  defaults: WakeWordDefaults;
  storageKeys: StorageKeys;
  vadHangoverFrames: number;
  activationDelayMs: number;
}

export const WAKEWORD_CONFIG: WakeWordConfigType = {
  // Path to ONNX model files (relative to public folder)
  modelBasePath: '/wakeword-models',

  // Path to ONNX Runtime WASM files (relative to public folder)
  ortWasmPath: '/ort/',

  // Available wake words with their model files
  // Add custom trained models here (e.g., hey_renfield.onnx)
  availableKeywords: [
    {
      id: 'hey_renfield',
      label: 'Hey Renfield',
      model: 'hey_renfield.onnx',
      description: 'Custom trained wake word'
    },
    {
      id: 'hey_jarvis',
      label: 'Hey Jarvis',
      model: 'hey_jarvis_v0.1.onnx',
      description: 'Pre-trained wake word'
    },
    {
      id: 'alexa',
      label: 'Alexa',
      model: 'alexa_v0.1.onnx',
      description: 'Pre-trained wake word'
    },
    {
      id: 'hey_mycroft',
      label: 'Hey Mycroft',
      model: 'hey_mycroft_v0.1.onnx',
      description: 'Pre-trained wake word'
    },
  ],

  // Default settings
  defaults: {
    enabled: false,           // Disabled by default (opt-in for privacy)
    keyword: 'hey_renfield',  // Default wake word
    threshold: 0.5,           // Detection sensitivity (0.0 - 1.0)
    cooldownMs: 2000,         // Minimum ms between detections
    audioFeedback: true,      // Play sound on detection
    gain: 1.0,                // Microphone gain
  },

  // LocalStorage keys for persisting settings
  storageKeys: {
    enabled: 'renfield_wakeword_enabled',
    keyword: 'renfield_wakeword_keyword',
    threshold: 'renfield_wakeword_threshold',
    audioFeedback: 'renfield_wakeword_audio_feedback',
  },

  // Performance tuning
  // VAD hangover frames - keeps speech detection open long enough for wake word
  vadHangoverFrames: 12,

  // Delay after wake word before starting recording (let wake word audio finish)
  activationDelayMs: 300,
};

/**
 * Get the model file path for a keyword
 */
export function getModelPath(keywordId: string): string | null {
  const keyword = WAKEWORD_CONFIG.availableKeywords.find(k => k.id === keywordId);
  if (!keyword) return null;
  return `${WAKEWORD_CONFIG.modelBasePath}/${keyword.model}`;
}

/**
 * Get keyword configuration by ID
 */
export function getKeywordConfig(keywordId: string): KeywordConfig | null {
  return WAKEWORD_CONFIG.availableKeywords.find(k => k.id === keywordId) || null;
}

/**
 * Load saved wake word settings from localStorage
 */
export function loadWakeWordSettings(): WakeWordSettings {
  const { defaults, storageKeys } = WAKEWORD_CONFIG;

  try {
    return {
      enabled: localStorage.getItem(storageKeys.enabled) === 'true',
      keyword: localStorage.getItem(storageKeys.keyword) || defaults.keyword,
      threshold: parseFloat(localStorage.getItem(storageKeys.threshold) || '') || defaults.threshold,
      audioFeedback: localStorage.getItem(storageKeys.audioFeedback) !== 'false',
    };
  } catch {
    // localStorage not available (e.g., private browsing)
    return {
      enabled: defaults.enabled,
      keyword: defaults.keyword,
      threshold: defaults.threshold,
      audioFeedback: defaults.audioFeedback,
    };
  }
}

/**
 * Save wake word settings to localStorage
 */
export function saveWakeWordSettings(settings: Partial<WakeWordSettings>): void {
  const { storageKeys } = WAKEWORD_CONFIG;

  try {
    if (settings.enabled !== undefined) {
      localStorage.setItem(storageKeys.enabled, String(settings.enabled));
    }
    if (settings.keyword !== undefined) {
      localStorage.setItem(storageKeys.keyword, settings.keyword);
    }
    if (settings.threshold !== undefined) {
      localStorage.setItem(storageKeys.threshold, String(settings.threshold));
    }
    if (settings.audioFeedback !== undefined) {
      localStorage.setItem(storageKeys.audioFeedback, String(settings.audioFeedback));
    }
  } catch {
    // localStorage not available
    console.warn('Could not save wake word settings to localStorage');
  }
}

export default WAKEWORD_CONFIG;
