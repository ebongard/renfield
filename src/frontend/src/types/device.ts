/**
 * Device-related type definitions
 */

// Device types matching backend
export const DEVICE_TYPES = {
  SATELLITE: 'satellite',
  WEB_PANEL: 'web_panel',
  WEB_TABLET: 'web_tablet',
  WEB_BROWSER: 'web_browser',
  WEB_KIOSK: 'web_kiosk',
} as const;

export type DeviceType = (typeof DEVICE_TYPES)[keyof typeof DEVICE_TYPES];

// Connection states
export const CONNECTION_STATES = {
  DISCONNECTED: 'disconnected',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  REGISTERED: 'registered',
  ERROR: 'error',
} as const;

export type ConnectionState = (typeof CONNECTION_STATES)[keyof typeof CONNECTION_STATES];

// Device states (from backend)
export const DEVICE_STATES = {
  IDLE: 'idle',
  LISTENING: 'listening',
  PROCESSING: 'processing',
  SPEAKING: 'speaking',
  ERROR: 'error',
} as const;

export type DeviceState = (typeof DEVICE_STATES)[keyof typeof DEVICE_STATES];

// Device capabilities
export interface DeviceCapabilities {
  has_microphone: boolean;
  has_speaker: boolean;
  has_wakeword: boolean;
  wakeword_method?: 'browser_wasm' | 'native';
  has_display: boolean;
  display_size?: 'small' | 'medium' | 'large';
  supports_notifications?: boolean;
}

// Device config stored in localStorage
export interface DeviceConfig {
  room: string | null;
  type: DeviceType;
  name: string | null;
  isStationary: boolean;
  customCapabilities?: Partial<DeviceCapabilities>;
}

// Device connection options
export interface DeviceConnectionOptions {
  autoConnect?: boolean;
  onMessage?: (data: WebSocketMessage) => void;
  onStateChange?: (state: DeviceState) => void;
  onTranscription?: (data: TranscriptionMessage) => void;
  onAction?: (data: ActionMessage) => void;
  onTtsAudio?: (data: TtsAudioMessage) => void;
  onResponseText?: (data: ResponseTextMessage) => void;
  onStream?: (data: StreamMessage) => void;
  onSessionEnd?: (data: SessionEndMessage) => void;
  onError?: (data: ErrorMessage) => void;
}

// Device connection result
export interface DeviceConnectionResult {
  // Connection state
  connectionState: ConnectionState;
  isConnected: boolean;
  isConnecting: boolean;

  // Device info
  deviceId: string | null;
  deviceType: DeviceType;
  deviceName: string | null;
  roomId: number | null;
  roomName: string | null;
  capabilities: DeviceCapabilities;

  // Session state
  deviceState: DeviceState;
  currentSessionId: string | null;
  error: Error | null;

  // Actions
  connect: (config?: Partial<DeviceConfig>) => Promise<{ deviceId: string; roomId: number }>;
  disconnect: () => void;
  sendText: (content: string) => void;
  startSession: () => void;
  sendWakeWordDetected: (keyword: string, confidence: number) => void;
  sendAudioChunk: (chunkBase64: string, sequence: number) => void;
  sendAudioEnd: (reason?: string) => void;

  // Utilities
  getStoredConfig: () => DeviceConfig | null;
  clearStoredConfig: () => void;
}

// WebSocket message types
export interface BaseWebSocketMessage {
  type: string;
}

export interface RegisterMessage extends BaseWebSocketMessage {
  type: 'register';
  device_id: string;
  device_type: DeviceType;
  room: string;
  device_name: string | null;
  is_stationary: boolean;
  capabilities: DeviceCapabilities;
}

export interface RegisterAckMessage extends BaseWebSocketMessage {
  type: 'register_ack';
  success: boolean;
  device_id?: string;
  room_id?: number;
  capabilities?: DeviceCapabilities;
}

export interface StateMessage extends BaseWebSocketMessage {
  type: 'state';
  state: DeviceState;
}

export interface TranscriptionMessage extends BaseWebSocketMessage {
  type: 'transcription';
  text: string;
  session_id?: string;
}

export interface ActionMessage extends BaseWebSocketMessage {
  type: 'action';
  intent: Record<string, unknown>;
  result: Record<string, unknown>;
}

export interface TtsAudioMessage extends BaseWebSocketMessage {
  type: 'tts_audio';
  audio: string;
  is_final: boolean;
  session_id?: string;
}

export interface ResponseTextMessage extends BaseWebSocketMessage {
  type: 'response_text';
  text: string;
  session_id?: string;
}

export interface StreamMessage extends BaseWebSocketMessage {
  type: 'stream';
  content: string;
}

export interface SessionEndMessage extends BaseWebSocketMessage {
  type: 'session_end';
  session_id: string;
  reason: string;
}

export interface ErrorMessage extends BaseWebSocketMessage {
  type: 'error';
  message: string;
}

export interface HeartbeatMessage extends BaseWebSocketMessage {
  type: 'heartbeat';
  status: DeviceState;
}

export interface HeartbeatAckMessage extends BaseWebSocketMessage {
  type: 'heartbeat_ack';
}

export interface ConfigUpdateMessage extends BaseWebSocketMessage {
  type: 'config_update';
  config: {
    wake_words?: string[];
    threshold?: number;
  };
}

export type WebSocketMessage =
  | RegisterMessage
  | RegisterAckMessage
  | StateMessage
  | TranscriptionMessage
  | ActionMessage
  | TtsAudioMessage
  | ResponseTextMessage
  | StreamMessage
  | SessionEndMessage
  | ErrorMessage
  | HeartbeatMessage
  | HeartbeatAckMessage
  | ConfigUpdateMessage;
