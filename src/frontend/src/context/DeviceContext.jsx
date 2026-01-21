/**
 * DeviceContext
 *
 * Provides device connection state and methods to all components.
 * Manages the WebSocket connection to /ws/device endpoint.
 */

import React, { createContext, useContext, useState, useCallback, useEffect } from 'react';
import {
  useDeviceConnection,
  DEVICE_TYPES,
  CONNECTION_STATES,
  DEVICE_STATES,
} from '../hooks/useDeviceConnection';

// Create context
const DeviceContext = createContext(null);

/**
 * DeviceProvider component
 */
export function DeviceProvider({ children }) {
  // Setup state
  const [isSetupComplete, setIsSetupComplete] = useState(false);
  const [showSetupModal, setShowSetupModal] = useState(false);

  // Message callbacks - will be populated by consumers
  const [messageHandlers, setMessageHandlers] = useState({
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
  }, []);

  // Handle setup completion
  const handleSetupComplete = useCallback((config) => {
    setIsSetupComplete(true);
    setShowSetupModal(false);
  }, []);

  // Register message handlers
  const registerHandlers = useCallback((handlers) => {
    setMessageHandlers(prev => ({
      ...prev,
      ...handlers,
    }));
  }, []);

  // Connect with existing config
  const autoConnect = useCallback(() => {
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
  const value = {
    // Connection state
    ...deviceConnection,

    // Setup state
    isSetupComplete,
    showSetupModal,
    setShowSetupModal,

    // Actions
    handleSetupComplete,
    registerHandlers,
    autoConnect,
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
export function useDevice() {
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
