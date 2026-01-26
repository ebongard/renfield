/**
 * useDeviceConnection Hook
 *
 * Manages WebSocket connection to the unified /ws/device endpoint.
 * Handles device registration, session management, and state synchronization.
 *
 * Uses module-level singleton pattern to survive React StrictMode remounts.
 */

import { useState, useEffect, useRef, useCallback } from 'react';
import { debug } from '../utils/debug';
import type {
  DeviceType,
  DeviceState,
  ConnectionState,
  DeviceCapabilities,
  DeviceConfig,
  DeviceConnectionOptions,
  WebSocketMessage,
  TranscriptionMessage,
  ActionMessage,
  TtsAudioMessage,
  ResponseTextMessage,
  StreamMessage,
  SessionEndMessage,
  ErrorMessage,
} from '../types/device';

// Re-export constants for backward compatibility
export {
  DEVICE_TYPES,
  CONNECTION_STATES,
  DEVICE_STATES,
} from '../types/device';

// Module-level storage for WebSocket connection (survives React remounts)
let _activeWebSocket: WebSocket | null = null;
let _activeConnectionPromise: Promise<{ deviceId: string; roomId: number }> | null = null;
let _connectionResolvers: {
  resolve: (value: { deviceId: string; roomId: number }) => void;
  reject: (reason: Error) => void;
  timeout: ReturnType<typeof setTimeout>;
  ws: WebSocket;
  connectionId: number;
} | null = null;
let _connectionId = 0; // Incremented for each new connection attempt

// Default capabilities for web devices
const getDefaultCapabilities = (deviceType: DeviceType): DeviceCapabilities => {
  switch (deviceType) {
    case 'web_panel':
      return {
        has_microphone: true,
        has_speaker: true,
        has_wakeword: true,
        wakeword_method: 'browser_wasm',
        has_display: true,
        display_size: 'large',
        supports_notifications: true,
      };
    case 'web_tablet':
      return {
        has_microphone: true,
        has_speaker: true,
        has_wakeword: true,
        wakeword_method: 'browser_wasm',
        has_display: true,
        display_size: 'medium',
        supports_notifications: true,
      };
    case 'web_browser':
      return {
        has_microphone: false, // Will be updated after permission check
        has_speaker: false,
        has_wakeword: false,
        has_display: true,
        display_size: 'large',
        supports_notifications: true,
      };
    case 'web_kiosk':
      return {
        has_microphone: true,
        has_speaker: true,
        has_wakeword: false,
        has_display: true,
        display_size: 'large',
        supports_notifications: false,
      };
    default:
      return {
        has_microphone: false,
        has_speaker: false,
        has_wakeword: false,
        has_display: true,
      };
  }
};

// Generate device ID
const generateDeviceId = (): string => {
  // Try to get from localStorage first (persistent ID)
  const storedId = localStorage.getItem('renfield_device_id');
  if (storedId) {
    return storedId;
  }

  // Generate new ID
  const newId = `web-${Date.now()}-${Math.random().toString(36).substr(2, 8)}`;
  localStorage.setItem('renfield_device_id', newId);
  return newId;
};

// Get stored device config
const getStoredConfig = (): DeviceConfig | null => {
  try {
    const stored = localStorage.getItem('renfield_device_config');
    return stored ? JSON.parse(stored) : null;
  } catch {
    return null;
  }
};

// Save device config
const saveConfig = (config: DeviceConfig): void => {
  localStorage.setItem('renfield_device_config', JSON.stringify(config));
};

// Device type labels for UI
export const DEVICE_TYPE_LABELS: Record<DeviceType, string> = {
  satellite: 'Satellite (Hardware)',
  web_panel: 'Web Panel (Stationary)',
  web_tablet: 'Web Tablet (Mobile)',
  web_browser: 'Web Browser',
  web_kiosk: 'Kiosk Terminal',
};

/**
 * Hook for managing device connection
 */
export function useDeviceConnection({
  autoConnect = false,
  onMessage = () => {},
  onStateChange = () => {},
  onTranscription = () => {},
  onAction = () => {},
  onTtsAudio = () => {},
  onResponseText = () => {},
  onStream = () => {},
  onSessionEnd = () => {},
  onError = () => {},
}: DeviceConnectionOptions = {}) {
  // State
  const [connectionState, setConnectionState] = useState<ConnectionState>('disconnected');
  const [deviceState, setDeviceState] = useState<DeviceState>('idle');
  const [deviceId, setDeviceId] = useState<string | null>(null);
  const [roomId, setRoomId] = useState<number | null>(null);
  const [roomName, setRoomName] = useState<string | null>(null);
  const [deviceType, setDeviceType] = useState<DeviceType>('web_browser');
  const [deviceName, setDeviceName] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<DeviceCapabilities>({} as DeviceCapabilities);
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [error, setError] = useState<Error | null>(null);

  // Refs
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Get WebSocket URL
  const getWsUrl = useCallback((): string => {
    const baseUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000';
    // Replace /ws with /ws/device for the device endpoint
    return baseUrl.replace(/\/ws$/, '') + '/ws/device';
  }, []);

  // Stop heartbeat
  const stopHeartbeat = useCallback((): void => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
      heartbeatIntervalRef.current = null;
    }
  }, []);

  // Start heartbeat
  const startHeartbeat = useCallback((): void => {
    stopHeartbeat();
    heartbeatIntervalRef.current = setInterval(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'heartbeat',
          status: deviceState,
        }));
      }
    }, 30000); // Every 30 seconds
  }, [deviceState, stopHeartbeat]);

  // Connect to WebSocket - returns a Promise that resolves when registered
  const connect = useCallback((config: Partial<DeviceConfig> = {}): Promise<{ deviceId: string; roomId: number }> => {
    const {
      room = null,
      type = 'web_browser' as DeviceType,
      name = null,
      isStationary = true,
      customCapabilities = {},
    } = config;

    // If there's already an active connection attempt, return its promise
    if (_activeConnectionPromise && _activeWebSocket && _activeWebSocket.readyState <= WebSocket.OPEN) {
      debug.log('🔄 Reusing existing connection attempt');
      return _activeConnectionPromise;
    }

    // Clean up existing connection
    if (wsRef.current && wsRef.current !== _activeWebSocket) {
      wsRef.current.close();
    }
    if (_activeWebSocket) {
      _activeWebSocket.close();
      _activeWebSocket = null;
    }

    // Clear any existing promise resolvers (don't reject - just clear)
    if (_connectionResolvers) {
      clearTimeout(_connectionResolvers.timeout);
      // Don't reject here - the old connection is being replaced intentionally
      _connectionResolvers = null;
    }

    setConnectionState('connecting');
    setError(null);

    const wsUrl = getWsUrl();
    debug.log('🔌 Connecting to device WebSocket:', wsUrl);

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    _activeWebSocket = ws; // Store in module-level variable

    // Increment connection ID to track which connection this is
    const thisConnectionId = ++_connectionId;

    // Create and return a Promise for the connection
    const connectionPromise = new Promise<{ deviceId: string; roomId: number }>((resolve, reject) => {
      // Timeout after 10 seconds
      const timeout = setTimeout(() => {
        // Only timeout if this is still the active connection
        if (_connectionResolvers && _connectionResolvers.connectionId === thisConnectionId) {
          _connectionResolvers.reject(new Error('Connection timeout'));
          _connectionResolvers = null;
          _activeConnectionPromise = null;
        }
      }, 10000);

      // Store resolvers in module-level variable (survives remounts)
      // Include the WebSocket and connectionId so handlers can verify they match
      _connectionResolvers = { resolve, reject, timeout, ws, connectionId: thisConnectionId };
    });

    _activeConnectionPromise = connectionPromise;

    // Clear module-level state when promise settles
    connectionPromise.finally(() => {
      _activeConnectionPromise = null;
    });

    ws.onopen = () => {
      debug.log('✅ Device WebSocket connected');
      setConnectionState('connected');

      // Register device
      const id = generateDeviceId();
      const defaultCaps = getDefaultCapabilities(type);
      const mergedCaps = { ...defaultCaps, ...customCapabilities };

      setDeviceId(id);
      setDeviceType(type);
      setDeviceName(name);
      setCapabilities(mergedCaps);

      const registerMsg = {
        type: 'register',
        device_id: id,
        device_type: type,
        room: room || 'Unknown Room',
        device_name: name,
        is_stationary: isStationary,
        capabilities: mergedCaps,
      };

      debug.log('📝 Registering device:', registerMsg);
      ws.send(JSON.stringify(registerMsg));

      // Save config for reconnection
      saveConfig({ room, type, name, isStationary, customCapabilities } as DeviceConfig);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WebSocketMessage;
        debug.log('📩 Device message:', data.type, data);

        // Handle message types
        switch (data.type) {
          case 'register_ack':
            if (data.success) {
              setConnectionState('registered');
              setRoomId(data.room_id || null);
              if (data.capabilities) {
                setCapabilities(data.capabilities);
              }
              debug.log('✅ Device registered:', data.device_id, 'in room', data.room_id);

              // Start heartbeat
              startHeartbeat();

              // Resolve the connection promise (only if resolvers belong to this WebSocket)
              if (_connectionResolvers && _connectionResolvers.ws === ws) {
                clearTimeout(_connectionResolvers.timeout);
                _connectionResolvers.resolve({ deviceId: data.device_id!, roomId: data.room_id! });
                _connectionResolvers = null;
              }
            } else {
              const err = new Error('Device registration failed');
              setError(err);
              setConnectionState('error');

              // Reject the connection promise (only if resolvers belong to this WebSocket)
              if (_connectionResolvers && _connectionResolvers.ws === ws) {
                clearTimeout(_connectionResolvers.timeout);
                _connectionResolvers.reject(err);
                _connectionResolvers = null;
              }
            }
            break;

          case 'state':
            setDeviceState(data.state);
            onStateChange(data.state);
            break;

          case 'transcription':
            onTranscription(data as TranscriptionMessage);
            break;

          case 'action':
            onAction(data as ActionMessage);
            break;

          case 'tts_audio':
            onTtsAudio(data as TtsAudioMessage);
            break;

          case 'response_text':
            onResponseText(data as ResponseTextMessage);
            break;

          case 'stream':
            onStream(data as StreamMessage);
            break;

          case 'session_end':
            setCurrentSessionId(null);
            setDeviceState('idle');
            onSessionEnd(data as SessionEndMessage);
            break;

          case 'heartbeat_ack':
            // Heartbeat acknowledged
            break;

          case 'config_update':
            // Server pushed new wake word configuration
            debug.log('🔄 Config update received:', data.config);
            // Dispatch custom event for useWakeWord hook to handle
            window.dispatchEvent(new CustomEvent('wakeword-config-update', {
              detail: data.config
            }));
            break;

          case 'error':
            setError(new Error(data.message));
            onError(data as ErrorMessage);
            break;

          default:
            onMessage(data);
        }
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    ws.onclose = (event) => {
      debug.log('👋 Device WebSocket closed:', event.code, event.reason);

      // Only update state if this is the active WebSocket
      if (_activeWebSocket === ws || wsRef.current === ws) {
        setConnectionState('disconnected');
        setCurrentSessionId(null);
        stopHeartbeat();
      }

      // Clear module-level WebSocket reference only if it's this WebSocket
      if (_activeWebSocket === ws) {
        _activeWebSocket = null;
      }

      // Only reject if this is the WebSocket that the resolvers belong to
      // This prevents old connections from rejecting new connection promises
      if (_connectionResolvers && _connectionResolvers.ws === ws) {
        clearTimeout(_connectionResolvers.timeout);
        _connectionResolvers.reject(new Error(`WebSocket closed: ${event.code} ${event.reason || ''}`));
        _connectionResolvers = null;
      }

      // Auto-reconnect after delay (only for unexpected closures and only if this is still the active connection)
      if (!event.wasClean && event.code !== 1000 && _activeWebSocket === null) {
        reconnectTimeoutRef.current = setTimeout(() => {
          debug.log('🔄 Attempting to reconnect...');
          const storedConfig = getStoredConfig();
          if (storedConfig) {
            connect(storedConfig);
          }
        }, 5000);
      }
    };

    ws.onerror = () => {
      console.error('❌ Device WebSocket error');

      // Only update state if this is the active WebSocket
      if (_activeWebSocket === ws || wsRef.current === ws) {
        const err = new Error('WebSocket connection error');
        setError(err);
        setConnectionState('error');
        onError({ type: 'error', message: 'WebSocket connection error' });

        // Only reject if this is the WebSocket that the resolvers belong to
        if (_connectionResolvers && _connectionResolvers.ws === ws) {
          clearTimeout(_connectionResolvers.timeout);
          _connectionResolvers.reject(err);
          _connectionResolvers = null;
        }
      }
    };

    return connectionPromise;
  }, [getWsUrl, onMessage, onStateChange, onTranscription, onAction, onTtsAudio, onResponseText, onStream, onSessionEnd, onError, startHeartbeat, stopHeartbeat]);

  // Disconnect
  const disconnect = useCallback(() => {
    if (reconnectTimeoutRef.current) {
      clearTimeout(reconnectTimeoutRef.current);
    }
    stopHeartbeat();

    // Clean up any pending connection promise (module-level)
    if (_connectionResolvers) {
      clearTimeout(_connectionResolvers.timeout);
      _connectionResolvers = null;
    }
    _activeConnectionPromise = null;

    // Close WebSocket
    if (wsRef.current) {
      wsRef.current.close(1000, 'User disconnect');
      wsRef.current = null;
    }
    if (_activeWebSocket) {
      _activeWebSocket.close(1000, 'User disconnect');
      _activeWebSocket = null;
    }

    setConnectionState('disconnected');
    setCurrentSessionId(null);
  }, [stopHeartbeat]);

  // Send text message
  const sendText = useCallback((content: string) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'text',
        content,
        session_id: currentSessionId,
      }));
    }
  }, [currentSessionId]);

  // Start voice session
  const startSession = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'start_session',
      }));
    }
  }, []);

  // Send wake word detected
  const sendWakeWordDetected = useCallback((keyword: string, confidence: number) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'wakeword_detected',
        keyword,
        confidence,
      }));
    }
  }, []);

  // Send audio chunk
  const sendAudioChunk = useCallback((chunkBase64: string, sequence: number) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && currentSessionId) {
      wsRef.current.send(JSON.stringify({
        type: 'audio',
        chunk: chunkBase64,
        sequence,
        session_id: currentSessionId,
      }));
    }
  }, [currentSessionId]);

  // End audio stream
  const sendAudioEnd = useCallback((reason = 'silence') => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && currentSessionId) {
      wsRef.current.send(JSON.stringify({
        type: 'audio_end',
        session_id: currentSessionId,
        reason,
      }));
    }
  }, [currentSessionId]);

  // Auto-connect on mount if configured
  useEffect(() => {
    if (autoConnect) {
      const storedConfig = getStoredConfig();
      if (storedConfig) {
        connect(storedConfig);
      }
    }
    // NOTE: Intentionally omitting 'connect' from deps to prevent reconnection loops.
    // connect() is a useCallback that may change when its deps change, but we only
    // want this effect to run on mount or when autoConnect changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoConnect]);

  // Cleanup on unmount only
  // NOTE: We do NOT close the WebSocket here because it may be reused
  // after a React StrictMode remount. The module-level WebSocket survives
  // component remounts intentionally.
  useEffect(() => {
    // On mount, sync the ref with module-level WebSocket if it exists
    if (_activeWebSocket && !wsRef.current) {
      wsRef.current = _activeWebSocket;
      // Also restore connection state if registered
      if (_activeWebSocket.readyState === WebSocket.OPEN) {
        setConnectionState('connected');
      }
    }

    return () => {
      // Only clear local refs, don't close the WebSocket
      // The WebSocket will be closed by disconnect() or when user navigates away
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
      }
      if (heartbeatIntervalRef.current) {
        clearInterval(heartbeatIntervalRef.current);
      }
    };
  }, []); // Empty deps = only on mount/unmount

  // Update room name from stored config or roomId
  useEffect(() => {
    const storedConfig = getStoredConfig();
    if (storedConfig?.room) {
      setRoomName(storedConfig.room);
    }
  }, [roomId]);

  return {
    // Connection state
    connectionState,
    isConnected: connectionState === 'registered',
    isConnecting: connectionState === 'connecting' || connectionState === 'connected',

    // Device info
    deviceId,
    deviceType,
    deviceName,
    roomId,
    roomName,
    capabilities,

    // Session state
    deviceState,
    currentSessionId,
    error,

    // Actions
    connect,
    disconnect,
    sendText,
    startSession,
    sendWakeWordDetected,
    sendAudioChunk,
    sendAudioEnd,

    // Utilities
    getStoredConfig,
    clearStoredConfig: () => {
      localStorage.removeItem('renfield_device_config');
      localStorage.removeItem('renfield_device_id');
    },
  };
}

export default useDeviceConnection;
