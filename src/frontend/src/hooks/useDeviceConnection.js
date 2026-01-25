/**
 * useDeviceConnection Hook
 *
 * Manages WebSocket connection to the unified /ws/device endpoint.
 * Handles device registration, session management, and state synchronization.
 *
 * Uses module-level singleton pattern to survive React StrictMode remounts.
 */

import { useState, useEffect, useRef, useCallback } from 'react';

// Module-level storage for WebSocket connection (survives React remounts)
let _activeWebSocket = null;
let _activeConnectionPromise = null;
let _connectionResolvers = null; // { resolve, reject, timeout, ws }
let _connectionId = 0; // Incremented for each new connection attempt

// Device types matching backend
export const DEVICE_TYPES = {
  SATELLITE: 'satellite',
  WEB_PANEL: 'web_panel',
  WEB_TABLET: 'web_tablet',
  WEB_BROWSER: 'web_browser',
  WEB_KIOSK: 'web_kiosk',
};

// Device type labels for UI
export const DEVICE_TYPE_LABELS = {
  [DEVICE_TYPES.SATELLITE]: 'Satellite (Hardware)',
  [DEVICE_TYPES.WEB_PANEL]: 'Web Panel (Stationary)',
  [DEVICE_TYPES.WEB_TABLET]: 'Web Tablet (Mobile)',
  [DEVICE_TYPES.WEB_BROWSER]: 'Web Browser',
  [DEVICE_TYPES.WEB_KIOSK]: 'Kiosk Terminal',
};

// Connection states
export const CONNECTION_STATES = {
  DISCONNECTED: 'disconnected',
  CONNECTING: 'connecting',
  CONNECTED: 'connected',
  REGISTERED: 'registered',
  ERROR: 'error',
};

// Device states (from backend)
export const DEVICE_STATES = {
  IDLE: 'idle',
  LISTENING: 'listening',
  PROCESSING: 'processing',
  SPEAKING: 'speaking',
  ERROR: 'error',
};

// Default capabilities for web devices
const getDefaultCapabilities = (deviceType) => {
  switch (deviceType) {
    case DEVICE_TYPES.WEB_PANEL:
      return {
        has_microphone: true,
        has_speaker: true,
        has_wakeword: true,
        wakeword_method: 'browser_wasm',
        has_display: true,
        display_size: 'large',
        supports_notifications: true,
      };
    case DEVICE_TYPES.WEB_TABLET:
      return {
        has_microphone: true,
        has_speaker: true,
        has_wakeword: true,
        wakeword_method: 'browser_wasm',
        has_display: true,
        display_size: 'medium',
        supports_notifications: true,
      };
    case DEVICE_TYPES.WEB_BROWSER:
      return {
        has_microphone: false, // Will be updated after permission check
        has_speaker: false,
        has_wakeword: false,
        has_display: true,
        display_size: 'large',
        supports_notifications: true,
      };
    case DEVICE_TYPES.WEB_KIOSK:
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
const generateDeviceId = () => {
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
const getStoredConfig = () => {
  try {
    const stored = localStorage.getItem('renfield_device_config');
    return stored ? JSON.parse(stored) : null;
  } catch (e) {
    return null;
  }
};

// Save device config
const saveConfig = (config) => {
  localStorage.setItem('renfield_device_config', JSON.stringify(config));
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
} = {}) {
  // State
  const [connectionState, setConnectionState] = useState(CONNECTION_STATES.DISCONNECTED);
  const [deviceState, setDeviceState] = useState(DEVICE_STATES.IDLE);
  const [deviceId, setDeviceId] = useState(null);
  const [roomId, setRoomId] = useState(null);
  const [roomName, setRoomName] = useState(null);
  const [deviceType, setDeviceType] = useState(DEVICE_TYPES.WEB_BROWSER);
  const [deviceName, setDeviceName] = useState(null);
  const [capabilities, setCapabilities] = useState({});
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [error, setError] = useState(null);

  // Refs
  const wsRef = useRef(null);
  const reconnectTimeoutRef = useRef(null);
  const heartbeatIntervalRef = useRef(null);
  // Note: Promise resolution is handled via module-level _connectionResolvers

  // Get WebSocket URL
  const getWsUrl = useCallback(() => {
    const baseUrl = import.meta.env.VITE_WS_URL || 'ws://localhost:8000';
    // Replace /ws with /ws/device for the device endpoint
    return baseUrl.replace(/\/ws$/, '') + '/ws/device';
  }, []);

  // Connect to WebSocket - returns a Promise that resolves when registered
  const connect = useCallback((config = {}) => {
    const {
      room = null,
      type = DEVICE_TYPES.WEB_BROWSER,
      name = null,
      isStationary = true,
      customCapabilities = {},
    } = config;

    // If there's already an active connection attempt, return its promise
    if (_activeConnectionPromise && _activeWebSocket && _activeWebSocket.readyState <= WebSocket.OPEN) {
      console.log('🔄 Reusing existing connection attempt');
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

    setConnectionState(CONNECTION_STATES.CONNECTING);
    setError(null);

    const wsUrl = getWsUrl();
    console.log('🔌 Connecting to device WebSocket:', wsUrl);

    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    _activeWebSocket = ws; // Store in module-level variable

    // Increment connection ID to track which connection this is
    const thisConnectionId = ++_connectionId;

    // Create and return a Promise for the connection
    const connectionPromise = new Promise((resolve, reject) => {
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
      console.log('✅ Device WebSocket connected');
      setConnectionState(CONNECTION_STATES.CONNECTED);

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

      console.log('📝 Registering device:', registerMsg);
      ws.send(JSON.stringify(registerMsg));

      // Save config for reconnection
      saveConfig({ room, type, name, isStationary, customCapabilities });
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        console.log('📩 Device message:', data.type, data);

        // Handle message types
        switch (data.type) {
          case 'register_ack':
            if (data.success) {
              setConnectionState(CONNECTION_STATES.REGISTERED);
              setRoomId(data.room_id);
              if (data.capabilities) {
                setCapabilities(data.capabilities);
              }
              console.log('✅ Device registered:', data.device_id, 'in room', data.room_id);

              // Start heartbeat
              startHeartbeat();

              // Resolve the connection promise (only if resolvers belong to this WebSocket)
              if (_connectionResolvers && _connectionResolvers.ws === ws) {
                clearTimeout(_connectionResolvers.timeout);
                _connectionResolvers.resolve({ deviceId: data.device_id, roomId: data.room_id });
                _connectionResolvers = null;
              }
            } else {
              const err = new Error('Device registration failed');
              setError(err);
              setConnectionState(CONNECTION_STATES.ERROR);

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

          case 'session_started':
            setCurrentSessionId(data.session_id);
            break;

          case 'transcription':
            onTranscription(data);
            break;

          case 'action':
            onAction(data);
            break;

          case 'tts_audio':
            onTtsAudio(data);
            break;

          case 'response_text':
            onResponseText(data);
            break;

          case 'stream':
            onStream(data);
            break;

          case 'session_end':
            setCurrentSessionId(null);
            setDeviceState(DEVICE_STATES.IDLE);
            onSessionEnd(data);
            break;

          case 'heartbeat_ack':
            // Heartbeat acknowledged
            break;

          case 'config_update':
            // Server pushed new wake word configuration
            console.log('🔄 Config update received:', data.config);
            // Dispatch custom event for useWakeWord hook to handle
            window.dispatchEvent(new CustomEvent('wakeword-config-update', {
              detail: data.config
            }));
            break;

          case 'error':
            setError(new Error(data.message));
            onError(data);
            break;

          default:
            onMessage(data);
        }
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
      }
    };

    ws.onclose = (event) => {
      console.log('👋 Device WebSocket closed:', event.code, event.reason);

      // Only update state if this is the active WebSocket
      if (_activeWebSocket === ws || wsRef.current === ws) {
        setConnectionState(CONNECTION_STATES.DISCONNECTED);
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
          console.log('🔄 Attempting to reconnect...');
          const storedConfig = getStoredConfig();
          if (storedConfig) {
            connect(storedConfig);
          }
        }, 5000);
      }
    };

    ws.onerror = (error) => {
      console.error('❌ Device WebSocket error:', error);

      // Only update state if this is the active WebSocket
      if (_activeWebSocket === ws || wsRef.current === ws) {
        const err = new Error('WebSocket connection error');
        setError(err);
        setConnectionState(CONNECTION_STATES.ERROR);
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
  }, [getWsUrl, onMessage, onStateChange, onTranscription, onAction, onTtsAudio, onResponseText, onStream, onSessionEnd, onError]);

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

    setConnectionState(CONNECTION_STATES.DISCONNECTED);
    setCurrentSessionId(null);
  }, []);

  // Start heartbeat
  const startHeartbeat = useCallback(() => {
    stopHeartbeat();
    heartbeatIntervalRef.current = setInterval(() => {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'heartbeat',
          status: deviceState,
        }));
      }
    }, 30000); // Every 30 seconds
  }, [deviceState]);

  // Stop heartbeat
  const stopHeartbeat = useCallback(() => {
    if (heartbeatIntervalRef.current) {
      clearInterval(heartbeatIntervalRef.current);
      heartbeatIntervalRef.current = null;
    }
  }, []);

  // Send text message
  const sendText = useCallback((content) => {
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
  const sendWakeWordDetected = useCallback((keyword, confidence) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'wakeword_detected',
        keyword,
        confidence,
      }));
    }
  }, []);

  // Send audio chunk
  const sendAudioChunk = useCallback((chunkBase64, sequence) => {
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoConnect]); // Only run on mount and when autoConnect changes

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
        setConnectionState(CONNECTION_STATES.CONNECTED);
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
    isConnected: connectionState === CONNECTION_STATES.REGISTERED,
    isConnecting: connectionState === CONNECTION_STATES.CONNECTING || connectionState === CONNECTION_STATES.CONNECTED,

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
