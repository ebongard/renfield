/**
 * useCapabilities Hook
 *
 * Provides capability-based feature detection for UI toggles.
 * Uses device context to determine what features should be available.
 */

import React, { useMemo, useCallback } from 'react';
import { useDevice } from '../context/DeviceContext';

/**
 * Hook for checking device capabilities
 */
export function useCapabilities() {
  const device = useDevice();

  const capabilities = device.capabilities || {};

  // Check if device has a specific capability
  const hasCapability = useCallback((capabilityName) => {
    return Boolean(capabilities[capabilityName]);
  }, [capabilities]);

  // Memoized capability checks
  const checks = useMemo(() => ({
    // Audio capabilities
    hasMicrophone: Boolean(capabilities.has_microphone),
    hasSpeaker: Boolean(capabilities.has_speaker),
    hasWakeWord: Boolean(capabilities.has_wakeword),
    wakeWordMethod: capabilities.wakeword_method || null,

    // Display capabilities
    hasDisplay: Boolean(capabilities.has_display),
    displaySize: capabilities.display_size || 'medium',
    supportsNotifications: Boolean(capabilities.supports_notifications),

    // Hardware capabilities (satellites)
    hasLeds: Boolean(capabilities.has_leds),
    ledCount: capabilities.led_count || 0,
    hasButton: Boolean(capabilities.has_button),

    // Device info
    isConnected: device.isConnected,
    isSetupComplete: device.isSetupComplete,
    deviceType: device.deviceType,
    roomName: device.roomName,
    deviceState: device.deviceState,
  }), [capabilities, device.isConnected, device.isSetupComplete, device.deviceType, device.roomName, device.deviceState]);

  // Feature flags based on capabilities
  const features = useMemo(() => ({
    // Voice input is available if microphone is enabled
    voiceInputEnabled: checks.hasMicrophone,

    // TTS playback is available if speaker is enabled
    ttsPlaybackEnabled: checks.hasSpeaker,

    // Wake word is available if has_wakeword and microphone
    wakeWordEnabled: checks.hasWakeWord && checks.hasMicrophone,

    // Show streaming text response (for display devices)
    showStreamingResponse: checks.hasDisplay,

    // Show audio visualizer during recording
    showAudioVisualizer: checks.hasMicrophone,

    // Enable push notifications
    notificationsEnabled: checks.supportsNotifications,

    // Show room context in UI
    showRoomContext: checks.isConnected && checks.roomName,

    // Show device setup prompt
    showSetupPrompt: !checks.isSetupComplete,
  }), [checks]);

  return {
    // Raw capabilities
    capabilities,
    hasCapability,

    // Computed checks
    ...checks,

    // Feature flags
    features,

    // Device context passthrough
    device,
  };
}

/**
 * HOC to wrap component with capability checks
 */
export function withCapabilities(Component) {
  return function WrappedComponent(props) {
    const capabilities = useCapabilities();
    return <Component {...props} capabilities={capabilities} />;
  };
}

/**
 * Component that only renders if capability is present
 */
export function IfCapability({ name, children, fallback = null }) {
  const { hasCapability } = useCapabilities();

  if (hasCapability(name)) {
    return children;
  }

  return fallback;
}

/**
 * Component that only renders if feature is enabled
 */
export function IfFeature({ name, children, fallback = null }) {
  const { features } = useCapabilities();

  if (features[name]) {
    return children;
  }

  return fallback;
}

export default useCapabilities;
