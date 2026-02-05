/**
 * DeviceContext
 *
 * Provides device connection state and methods to all components.
 * Manages the WebSocket connection to /ws/device endpoint.
 */

import { createContext, useContext, useState, useCallback, useEffect, ReactNode } from 'react';
import {
  useDeviceConnection,
  DEVICE_TYPES,
  CONNECTION_STATES,
  DEVICE_STATES,
} from '../hooks/useDeviceConnection';
import type {
  DeviceType,
  DeviceState,
  ConnectionState,
  DeviceCapabilities,
  DeviceConfig,
  WebSocketMessage,
  TranscriptionMessage,
  ActionMessage,
  TtsAudioMessage,
  ResponseTextMessage,
  StreamMessage,
  SessionEndMessage,
  ErrorMessage,
} from '../types/device';

// Message handlers type
interface MessageHandlers {
  onMessage: (data: WebSocketMessage) => void;
  onStateChange: (state: DeviceState) => void;
  onTranscription: (data: TranscriptionMessage) => void;
  onAction: (data: ActionMessage) => void;
  onTtsAudio: (data: TtsAudioMessage) => void;
  onResponseText: (data: ResponseTextMessage) => void;
  onStream: (data: StreamMessage) => void;
  onSessionEnd: (data: SessionEndMessage) => void;
  onError: (data: ErrorMessage) => void;
}

// Device context value type
interface DeviceContextValue {
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
  sendNotificationAck: (notificationId: number, action?: 'acknowledged' | 'dismissed') => void;

  // Utilities
  getStoredConfig: () => DeviceConfig | null;
  clearStoredConfig: () => void;

  // Setup state
  isSetupComplete: boolean;
  showSetupModal: boolean;
  setShowSetupModal: (show: boolean) => void;

  // Setup actions
  handleSetupComplete: (config: DeviceConfig) => void;
  registerHandlers: (handlers: Partial<MessageHandlers>) => void;
  autoConnect: () => void;
  resetSetup: () => void;
}

// Create context
const DeviceContext = createContext<DeviceContextValue | null>(null);

interface DeviceProviderProps {
  children: ReactNode;
}

/**
 * DeviceProvider component
 */
export function DeviceProvider({ children }: DeviceProviderProps) {
  // Setup state
  const [isSetupComplete, setIsSetupComplete] = useState(false);
  const [showSetupModal, setShowSetupModal] = useState(false);

  // Message callbacks - will be populated by consumers
  const [messageHandlers, setMessageHandlers] = useState<MessageHandlers>({
    onMessage: () => {},
    onStateChange: () => {},
    onTranscription: () => {},
    onAction: () => {},
    onTtsAudio: () => {},
    onResponseText: () => {},
    onStream: () => {},
    onSessionEnd: () => {},
    onError: () => {},
  });

  // Device connection
  const deviceConnection = useDeviceConnection({
    autoConnect: false,
    onMessage: (data) => messageHandlers.onMessage(data),
    onStateChange: (state) => messageHandlers.onStateChange(state),
    onTranscription: (data) => messageHandlers.onTranscription(data),
    onAction: (data) => messageHandlers.onAction(data),
    onTtsAudio: (data) => messageHandlers.onTtsAudio(data),
    onResponseText: (data) => messageHandlers.onResponseText(data),
    onStream: (data) => messageHandlers.onStream(data),
    onSessionEnd: (data) => messageHandlers.onSessionEnd(data),
    onError: (data) => messageHandlers.onError(data),
  });

  // Check for existing config on mount
  useEffect(() => {
    const storedConfig = deviceConnection.getStoredConfig();
    if (storedConfig && storedConfig.room) {
      setIsSetupComplete(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Handle setup completion
  const handleSetupComplete = useCallback((_config: DeviceConfig) => {
    setIsSetupComplete(true);
    setShowSetupModal(false);
  }, []);

  // Register message handlers
  const registerHandlers = useCallback((handlers: Partial<MessageHandlers>) => {
    setMessageHandlers(prev => ({
      ...prev,
      ...handlers,
    }));
  }, []);

  // Connect with existing config
  const autoConnectFn = useCallback(() => {
    const storedConfig = deviceConnection.getStoredConfig();
    if (storedConfig) {
      deviceConnection.connect(storedConfig);
    }
  }, [deviceConnection]);

  // Reset setup
  const resetSetup = useCallback(() => {
    deviceConnection.disconnect();
    deviceConnection.clearStoredConfig();
    setIsSetupComplete(false);
  }, [deviceConnection]);

  // Context value
  const value: DeviceContextValue = {
    // Connection state
    ...deviceConnection,

    // Setup state
    isSetupComplete,
    showSetupModal,
    setShowSetupModal,

    // Actions
    handleSetupComplete,
    registerHandlers,
    autoConnect: autoConnectFn,
    resetSetup,
  };

  return (
    <DeviceContext.Provider value={value}>
      {children}
    </DeviceContext.Provider>
  );
}

/**
 * Hook to use device context
 */
export function useDevice(): DeviceContextValue {
  const context = useContext(DeviceContext);
  if (!context) {
    throw new Error('useDevice must be used within a DeviceProvider');
  }
  return context;
}

// Re-export constants
export {
  DEVICE_TYPES,
  CONNECTION_STATES,
  DEVICE_STATES,
};

export default DeviceContext;
