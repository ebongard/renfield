/**
 * DeviceSetup Component
 *
 * Allows users to configure their device for room-based voice interaction.
 * Handles device type selection, room selection, and capability configuration.
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Monitor,
  Tablet,
  Smartphone,
  Tv,
  Mic,
  Speaker,
  MapPin,
  Check,
  X,
  Loader,
  RefreshCw,
  Settings,
} from 'lucide-react';
import apiClient from '../utils/axios';
import {
  DEVICE_TYPES,
  DEVICE_TYPE_LABELS,
} from '../hooks/useDeviceConnection';
import { useDevice } from '../context/DeviceContext';

// Device type icons
const DEVICE_TYPE_ICONS = {
  [DEVICE_TYPES.WEB_PANEL]: Monitor,
  [DEVICE_TYPES.WEB_TABLET]: Tablet,
  [DEVICE_TYPES.WEB_BROWSER]: Smartphone,
  [DEVICE_TYPES.WEB_KIOSK]: Tv,
};

// Device type descriptions
const DEVICE_TYPE_DESCRIPTIONS = {
  [DEVICE_TYPES.WEB_PANEL]: 'Stationary display (wall-mounted iPad/tablet)',
  [DEVICE_TYPES.WEB_TABLET]: 'Mobile tablet that can move between rooms',
  [DEVICE_TYPES.WEB_BROWSER]: 'Desktop or laptop browser',
  [DEVICE_TYPES.WEB_KIOSK]: 'Touch-screen kiosk or terminal',
};

export default function DeviceSetup({
  onSetupComplete,
  onCancel,
  existingConfig = null,
}) {
  // Form state
  const [deviceType, setDeviceType] = useState(existingConfig?.type || DEVICE_TYPES.WEB_BROWSER);
  const [selectedRoom, setSelectedRoom] = useState(existingConfig?.room || '');
  const [deviceName, setDeviceName] = useState(existingConfig?.name || '');
  const [isStationary, setIsStationary] = useState(existingConfig?.isStationary ?? true);

  // Capability overrides
  const [hasMicrophone, setHasMicrophone] = useState(true);
  const [hasSpeaker, setHasSpeaker] = useState(true);
  const [hasWakeWord, setHasWakeWord] = useState(false);

  // Rooms list
  const [rooms, setRooms] = useState([]);
  const [loadingRooms, setLoadingRooms] = useState(true);
  const [newRoomName, setNewRoomName] = useState('');
  const [showNewRoomInput, setShowNewRoomInput] = useState(false);

  // Permission state
  const [micPermission, setMicPermission] = useState('unknown'); // unknown, granted, denied, prompt
  const [checkingPermissions, setCheckingPermissions] = useState(false);

  // Setup state
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState(null);

  // Use device context instead of creating own connection
  const device = useDevice();

  // Load rooms on mount
  useEffect(() => {
    loadRooms();
    checkMicrophonePermission();
  }, []);

  // Update capabilities based on device type
  useEffect(() => {
    if (deviceType === DEVICE_TYPES.WEB_TABLET) {
      setIsStationary(false);
    } else if (deviceType !== DEVICE_TYPES.WEB_BROWSER) {
      setIsStationary(true);
    }

    // Update default capabilities
    if (deviceType === DEVICE_TYPES.WEB_PANEL || deviceType === DEVICE_TYPES.WEB_TABLET) {
      setHasMicrophone(true);
      setHasSpeaker(true);
      setHasWakeWord(true);
    } else if (deviceType === DEVICE_TYPES.WEB_KIOSK) {
      setHasMicrophone(true);
      setHasSpeaker(true);
      setHasWakeWord(false);
    } else {
      setHasMicrophone(micPermission === 'granted');
      setHasSpeaker(true);
      setHasWakeWord(false);
    }
  }, [deviceType, micPermission]);

  // Load rooms from API
  const loadRooms = async () => {
    setLoadingRooms(true);
    try {
      const response = await apiClient.get('/api/rooms');
      setRooms(response.data || []);

      // Auto-select first room if none selected
      if (!selectedRoom && response.data.length > 0) {
        setSelectedRoom(response.data[0].name);
      }
    } catch (err) {
      console.error('Failed to load rooms:', err);
      setError('Could not load rooms');
    } finally {
      setLoadingRooms(false);
    }
  };

  // Check microphone permission
  const checkMicrophonePermission = async () => {
    setCheckingPermissions(true);
    try {
      // Check if permission API is available
      if (navigator.permissions && navigator.permissions.query) {
        const result = await navigator.permissions.query({ name: 'microphone' });
        setMicPermission(result.state);

        // Listen for changes
        result.onchange = () => {
          setMicPermission(result.state);
        };
      } else {
        // Fallback: try to access microphone
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          stream.getTracks().forEach(track => track.stop());
          setMicPermission('granted');
        } catch (e) {
          setMicPermission('denied');
        }
      }
    } catch (err) {
      setMicPermission('prompt');
    } finally {
      setCheckingPermissions(false);
    }
  };

  // Request microphone permission
  const requestMicrophonePermission = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach(track => track.stop());
      setMicPermission('granted');
      setHasMicrophone(true);
    } catch (err) {
      setMicPermission('denied');
      setHasMicrophone(false);
    }
  };

  // Create new room
  const createRoom = async () => {
    if (!newRoomName.trim()) return;

    try {
      const response = await apiClient.post('/api/rooms', {
        name: newRoomName.trim(),
      });
      setRooms([...rooms, response.data]);
      setSelectedRoom(response.data.name);
      setNewRoomName('');
      setShowNewRoomInput(false);
    } catch (err) {
      console.error('Failed to create room:', err);
      setError(err.response?.data?.detail || 'Could not create room');
    }
  };

  // Handle setup completion
  const handleComplete = useCallback(async () => {
    if (!selectedRoom) {
      setError('Please select a room');
      return;
    }

    setIsConnecting(true);
    setError(null);

    const config = {
      room: selectedRoom,
      type: deviceType,
      name: deviceName || null,
      isStationary,
      customCapabilities: {
        has_microphone: hasMicrophone,
        has_speaker: hasSpeaker,
        has_wakeword: hasWakeWord,
        wakeword_method: hasWakeWord ? 'browser_wasm' : null,
      },
    };

    try {
      // connect() now returns a Promise that resolves when registered
      await device.connect(config);

      setIsConnecting(false);
      onSetupComplete?.(config, device);
    } catch (err) {
      setError(err.message || 'Connection failed');
      setIsConnecting(false);
    }
  }, [device, selectedRoom, deviceType, deviceName, isStationary, hasMicrophone, hasSpeaker, hasWakeWord, onSetupComplete]);

  // Quick setup (skip configuration)
  const handleQuickSetup = () => {
    handleComplete();
  };

  return (
    <div className="bg-gray-800 rounded-xl border border-gray-700 overflow-hidden">
      {/* Header */}
      <div className="px-6 py-4 border-b border-gray-700">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-primary-500/20 rounded-lg">
              <Settings className="w-5 h-5 text-primary-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-white">Device Setup</h2>
              <p className="text-sm text-gray-400">Configure your device for voice interaction</p>
            </div>
          </div>
          {onCancel && (
            <button
              onClick={onCancel}
              className="p-2 hover:bg-gray-700 rounded-lg transition-colors"
            >
              <X className="w-5 h-5 text-gray-400" />
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="p-6 space-y-6">
        {/* Error message */}
        {error && (
          <div className="p-4 bg-red-500/20 border border-red-500/50 rounded-lg text-red-300 text-sm">
            {error}
          </div>
        )}

        {/* Room Selection */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-2">
            <MapPin className="w-4 h-4 inline mr-2" />
            Room
          </label>
          <div className="flex space-x-2">
            <select
              value={selectedRoom}
              onChange={(e) => setSelectedRoom(e.target.value)}
              disabled={loadingRooms}
              className="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white focus:border-primary-500 focus:outline-none disabled:opacity-50"
            >
              <option value="">Select a room...</option>
              {rooms.map(room => (
                <option key={room.id} value={room.name}>
                  {room.name}
                </option>
              ))}
            </select>
            <button
              onClick={() => setShowNewRoomInput(!showNewRoomInput)}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 border border-gray-600 rounded-lg text-gray-300 transition-colors"
            >
              +
            </button>
            <button
              onClick={loadRooms}
              disabled={loadingRooms}
              className="px-3 py-2 bg-gray-700 hover:bg-gray-600 border border-gray-600 rounded-lg text-gray-300 transition-colors disabled:opacity-50"
            >
              <RefreshCw className={`w-4 h-4 ${loadingRooms ? 'animate-spin' : ''}`} />
            </button>
          </div>

          {/* New room input */}
          {showNewRoomInput && (
            <div className="mt-2 flex space-x-2">
              <input
                type="text"
                value={newRoomName}
                onChange={(e) => setNewRoomName(e.target.value)}
                placeholder="New room name..."
                className="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white placeholder-gray-400 focus:border-primary-500 focus:outline-none"
                onKeyPress={(e) => e.key === 'Enter' && createRoom()}
              />
              <button
                onClick={createRoom}
                disabled={!newRoomName.trim()}
                className="px-4 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg text-white transition-colors disabled:opacity-50"
              >
                <Check className="w-4 h-4" />
              </button>
            </div>
          )}
        </div>

        {/* Device Type Selection */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-3">
            Device Type
          </label>
          <div className="grid grid-cols-2 gap-3">
            {Object.entries(DEVICE_TYPES)
              .filter(([key]) => key !== 'SATELLITE') // Don't show satellite option for web
              .map(([key, type]) => {
                const Icon = DEVICE_TYPE_ICONS[type] || Monitor;
                const isSelected = deviceType === type;

                return (
                  <button
                    key={type}
                    onClick={() => setDeviceType(type)}
                    className={`p-4 rounded-lg border text-left transition-all ${
                      isSelected
                        ? 'border-primary-500 bg-primary-500/20'
                        : 'border-gray-600 bg-gray-700/50 hover:border-gray-500'
                    }`}
                  >
                    <div className="flex items-start space-x-3">
                      <Icon className={`w-5 h-5 ${isSelected ? 'text-primary-400' : 'text-gray-400'}`} />
                      <div>
                        <div className={`font-medium ${isSelected ? 'text-white' : 'text-gray-300'}`}>
                          {DEVICE_TYPE_LABELS[type]}
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          {DEVICE_TYPE_DESCRIPTIONS[type]}
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
          </div>
        </div>

        {/* Device Name (optional) */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-2">
            Device Name (optional)
          </label>
          <input
            type="text"
            value={deviceName}
            onChange={(e) => setDeviceName(e.target.value)}
            placeholder="e.g., Living Room iPad"
            className="w-full bg-gray-700 border border-gray-600 rounded-lg px-4 py-2 text-white placeholder-gray-400 focus:border-primary-500 focus:outline-none"
          />
        </div>

        {/* Capabilities */}
        <div>
          <label className="block text-sm font-medium text-gray-300 mb-3">
            Capabilities
          </label>
          <div className="space-y-3">
            {/* Microphone */}
            <div className="flex items-center justify-between p-3 bg-gray-700/50 rounded-lg">
              <div className="flex items-center space-x-3">
                <Mic className={`w-4 h-4 ${hasMicrophone ? 'text-green-400' : 'text-gray-500'}`} />
                <div>
                  <div className="text-sm text-white">Microphone</div>
                  <div className="text-xs text-gray-500">
                    {micPermission === 'granted' ? 'Permission granted' :
                     micPermission === 'denied' ? 'Permission denied' :
                     'Permission required'}
                  </div>
                </div>
              </div>
              <div className="flex items-center space-x-2">
                {micPermission !== 'granted' && (
                  <button
                    onClick={requestMicrophonePermission}
                    className="text-xs px-2 py-1 bg-primary-600 hover:bg-primary-500 rounded text-white"
                  >
                    Allow
                  </button>
                )}
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hasMicrophone}
                    onChange={(e) => setHasMicrophone(e.target.checked)}
                    disabled={micPermission !== 'granted'}
                    className="sr-only peer"
                  />
                  <div className="w-9 h-5 bg-gray-600 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600 peer-disabled:opacity-50"></div>
                </label>
              </div>
            </div>

            {/* Speaker */}
            <div className="flex items-center justify-between p-3 bg-gray-700/50 rounded-lg">
              <div className="flex items-center space-x-3">
                <Speaker className={`w-4 h-4 ${hasSpeaker ? 'text-green-400' : 'text-gray-500'}`} />
                <div>
                  <div className="text-sm text-white">Speaker (TTS)</div>
                  <div className="text-xs text-gray-500">Play voice responses</div>
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={hasSpeaker}
                  onChange={(e) => setHasSpeaker(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-gray-600 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600"></div>
              </label>
            </div>

            {/* Wake Word (only for panel/tablet types) */}
            {(deviceType === DEVICE_TYPES.WEB_PANEL || deviceType === DEVICE_TYPES.WEB_TABLET) && (
              <div className="flex items-center justify-between p-3 bg-gray-700/50 rounded-lg">
                <div className="flex items-center space-x-3">
                  <div className={`w-4 h-4 rounded-full ${hasWakeWord ? 'bg-green-400' : 'bg-gray-500'}`} />
                  <div>
                    <div className="text-sm text-white">Wake Word</div>
                    <div className="text-xs text-gray-500">Hands-free activation</div>
                  </div>
                </div>
                <label className="relative inline-flex items-center cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hasWakeWord}
                    onChange={(e) => setHasWakeWord(e.target.checked)}
                    disabled={!hasMicrophone}
                    className="sr-only peer"
                  />
                  <div className="w-9 h-5 bg-gray-600 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600 peer-disabled:opacity-50"></div>
                </label>
              </div>
            )}
          </div>
        </div>

        {/* Stationary toggle (for browser type) */}
        {deviceType === DEVICE_TYPES.WEB_BROWSER && (
          <div className="flex items-center justify-between p-3 bg-gray-700/50 rounded-lg">
            <div>
              <div className="text-sm text-white">Stationary Device</div>
              <div className="text-xs text-gray-500">
                This device stays in one room
              </div>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input
                type="checkbox"
                checked={isStationary}
                onChange={(e) => setIsStationary(e.target.checked)}
                className="sr-only peer"
              />
              <div className="w-9 h-5 bg-gray-600 peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600"></div>
            </label>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-6 py-4 border-t border-gray-700 bg-gray-800/50">
        <div className="flex justify-end space-x-3">
          {onCancel && (
            <button
              onClick={onCancel}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-gray-300 transition-colors"
            >
              Cancel
            </button>
          )}
          <button
            onClick={handleComplete}
            disabled={isConnecting || !selectedRoom}
            className="px-6 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-2"
          >
            {isConnecting ? (
              <>
                <Loader className="w-4 h-4 animate-spin" />
                <span>Connecting...</span>
              </>
            ) : (
              <>
                <Check className="w-4 h-4" />
                <span>Connect</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
