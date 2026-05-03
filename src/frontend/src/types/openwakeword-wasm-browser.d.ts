/**
 * Type declarations for the `openwakeword-wasm-browser` package
 * (https://github.com/dnavarrom/openwakeword_wasm).
 *
 * The package ships ESM JavaScript only; this declaration file mirrors
 * the constructor surface and runtime API used by `src/hooks/useWakeWord.ts`.
 * Keep in sync with `node_modules/openwakeword-wasm-browser/src/WakeWordEngine.js`.
 */

declare module 'openwakeword-wasm-browser' {
  export interface WakeWordDetectEvent {
    keyword: string;
    score: number;
    /** Performance-clock timestamp emitted by the engine, when available. */
    at?: number;
  }

  export interface WakeWordEngineOptions {
    /** Wake-word IDs the engine should listen for (e.g. `'hey_jarvis'`). */
    keywords?: string[];
    /**
     * Map of wake-word ID -> ONNX model filename, relative to
     * `baseAssetUrl`. Defaults to `MODEL_FILE_MAP`.
     */
    modelFiles?: Record<string, string>;
    /** Base URL where ONNX assets are served from. */
    baseAssetUrl?: string;
    /** Override path for the onnxruntime-web WASM artifacts. */
    ortWasmPath?: string;
    frameSize?: number;
    sampleRate?: number;
    vadHangoverFrames?: number;
    /** Score threshold above which a detection is emitted. */
    detectionThreshold?: number;
    /** Minimum gap between consecutive detections, in milliseconds. */
    cooldownMs?: number;
    /** ONNX runtime execution providers (e.g. `['wasm']`). */
    executionProviders?: string[];
    embeddingWindowSize?: number;
    debug?: boolean;
  }

  export interface WakeWordStartOptions {
    /** Optional `getUserMedia` device ID. */
    deviceId?: string;
    /** Linear gain applied to the microphone signal. Defaults to 1.0. */
    gain?: number;
  }

  /** Built-in wake-word ID -> model filename mapping shipped with the package. */
  export const MODEL_FILE_MAP: Record<string, string>;

  export class WakeWordEngine {
    constructor(options?: WakeWordEngineOptions);

    load(): Promise<void>;
    start(options?: WakeWordStartOptions): Promise<void>;
    stop(): Promise<void>;
    setActiveKeywords(keywords: string[]): void;

    /** Subscribe to engine events. Returns an unsubscribe function. */
    on(event: 'ready', handler: () => void): () => void;
    on(event: 'detect', handler: (data: WakeWordDetectEvent) => void): () => void;
    on(event: 'speech-start', handler: () => void): () => void;
    on(event: 'speech-end', handler: () => void): () => void;
    on(event: 'error', handler: (error: Error) => void): () => void;

    off(event: string, handler: (...args: unknown[]) => void): void;
  }

  export default WakeWordEngine;
}
