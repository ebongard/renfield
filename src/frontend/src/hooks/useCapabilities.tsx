/**
 * useCapabilities Hook
 *
 * Provides capability-based feature detection for UI toggles.
 * Uses device context to determine what features should be available.
 */

import { useMemo, useCallback, ComponentType, ReactNode } from 'react';
import { useDevice } from '../context/DeviceContext';
import type { DeviceCapabilities, DeviceState, DeviceType } from '../types/device';

// Extended capabilities including satellite hardware
interface ExtendedCapabilities extends DeviceCapabilities {
  has_leds?: boolean;
  led_count?: number;
  has_button?: boolean;
}

// Capability check results
interface CapabilityChecks {
  hasMicrophone: boolean;
  hasSpeaker: boolean;
  hasWakeWord: boolean;
  wakeWordMethod: string | null;
  hasDisplay: boolean;
  displaySize: string;
  supportsNotifications: boolean;
  hasLeds: boolean;
  ledCount: number;
  hasButton: boolean;
  isConnected: boolean;
  isSetupComplete: boolean;
  deviceType: DeviceType;
  roomName: string | null;
  deviceState: DeviceState;
}

// Feature flags
interface FeatureFlags {
  voiceInputEnabled: boolean;
  ttsPlaybackEnabled: boolean;
  wakeWordEnabled: boolean;
  showStreamingResponse: boolean;
  showAudioVisualizer: boolean;
  notificationsEnabled: boolean;
  showRoomContext: boolean;
  showSetupPrompt: boolean;
}

// Hook return type
interface UseCapabilitiesResult {
  capabilities: ExtendedCapabilities;
  hasCapability: (capabilityName: keyof ExtendedCapabilities) => boolean;
  hasMicrophone: boolean;
  hasSpeaker: boolean;
  hasWakeWord: boolean;
  wakeWordMethod: string | null;
  hasDisplay: boolean;
  displaySize: string;
  supportsNotifications: boolean;
  hasLeds: boolean;
  ledCount: number;
  hasButton: boolean;
  isConnected: boolean;
  isSetupComplete: boolean;
  deviceType: DeviceType;
  roomName: string | null;
  deviceState: DeviceState;
  features: FeatureFlags;
  device: ReturnType<typeof useDevice>;
}

/**
 * Hook for checking device capabilities
 */
export function useCapabilities(): UseCapabilitiesResult {
  const device = useDevice();

  // Memoize capabilities to prevent unnecessary re-renders
  const capabilities = useMemo(
    () => (device.capabilities || {}) as ExtendedCapabilities,
    [device.capabilities]
  );

  // Check if device has a specific capability
  const hasCapability = useCallback((capabilityName: keyof ExtendedCapabilities): boolean => {
    return Boolean(capabilities[capabilityName]);
  }, [capabilities]);

  // Memoized capability checks
  const checks: CapabilityChecks = useMemo(() => ({
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
  const features: FeatureFlags = useMemo(() => ({
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
    showRoomContext: checks.isConnected && Boolean(checks.roomName),

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

// Props for wrapped component
interface WithCapabilitiesProps {
  capabilities: UseCapabilitiesResult;
}

/**
 * HOC to wrap component with capability checks
 */
export function withCapabilities<P extends object>(
  Component: ComponentType<P & WithCapabilitiesProps>
): ComponentType<Omit<P, keyof WithCapabilitiesProps>> {
  return function WrappedComponent(props: Omit<P, keyof WithCapabilitiesProps>) {
    const capabilities = useCapabilities();
    return <Component {...(props as P)} capabilities={capabilities} />;
  };
}

// Props for IfCapability component
interface IfCapabilityProps {
  name: keyof ExtendedCapabilities;
  children: ReactNode;
  fallback?: ReactNode;
}

/**
 * Component that only renders if capability is present
 */
export function IfCapability({ name, children, fallback = null }: IfCapabilityProps): ReactNode {
  const { hasCapability } = useCapabilities();

  if (hasCapability(name)) {
    return children;
  }

  return fallback;
}

// Props for IfFeature component
interface IfFeatureProps {
  name: keyof FeatureFlags;
  children: ReactNode;
  fallback?: ReactNode;
}

/**
 * Component that only renders if feature is enabled
 */
export function IfFeature({ name, children, fallback = null }: IfFeatureProps): ReactNode {
  const { features } = useCapabilities();

  if (features[name]) {
    return children;
  }

  return fallback;
}

export default useCapabilities;
