/**
 * DeviceSetup Component
 *
 * Allows users to configure their device for room-based voice interaction.
 * Handles device type selection, room selection, and capability configuration.
 */

import { useState, useEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import type { LucideIcon } from 'lucide-react';
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
import { extractApiError } from '../utils/axios';
import { DEVICE_TYPES, DEVICE_TYPE_LABELS } from '../hooks/useDeviceConnection';
import type { DeviceConfig, DeviceType } from '../types/device';
import type { Room } from '../types/api';
import { useDevice } from '../context/DeviceContext';

const DEVICE_TYPE_ICONS: Partial<Record<DeviceType, LucideIcon>> = {
  [DEVICE_TYPES.WEB_PANEL]: Monitor,
  [DEVICE_TYPES.WEB_TABLET]: Tablet,
  [DEVICE_TYPES.WEB_BROWSER]: Smartphone,
  [DEVICE_TYPES.WEB_KIOSK]: Tv,
};

type MicPermission = PermissionState | 'unknown';

interface DeviceSetupProps {
  onSetupComplete?: (config: DeviceConfig, device: ReturnType<typeof useDevice>) => void;
  onCancel?: () => void;
  existingConfig?: DeviceConfig | null;
}

export default function DeviceSetup({
  onSetupComplete,
  onCancel,
  existingConfig = null,
}: DeviceSetupProps) {
  const { t } = useTranslation();

  const DEVICE_TYPE_DESCRIPTIONS: Partial<Record<DeviceType, string>> = {
    [DEVICE_TYPES.WEB_PANEL]: t('device.descriptionPanel'),
    [DEVICE_TYPES.WEB_TABLET]: t('device.descriptionTablet'),
    [DEVICE_TYPES.WEB_BROWSER]: t('device.descriptionBrowser'),
    [DEVICE_TYPES.WEB_KIOSK]: t('device.descriptionKiosk'),
  };

  const [deviceType, setDeviceType] = useState<DeviceType>(existingConfig?.type ?? DEVICE_TYPES.WEB_BROWSER);
  const [selectedRoom, setSelectedRoom] = useState<string>(existingConfig?.room ?? '');
  const [deviceName, setDeviceName] = useState<string>(existingConfig?.name ?? '');
  const [isStationary, setIsStationary] = useState<boolean>(existingConfig?.isStationary ?? true);

  const [hasMicrophone, setHasMicrophone] = useState(true);
  const [hasSpeaker, setHasSpeaker] = useState(true);
  const [hasWakeWord, setHasWakeWord] = useState(false);

  const [rooms, setRooms] = useState<Room[]>([]);
  const [loadingRooms, setLoadingRooms] = useState(true);
  const [newRoomName, setNewRoomName] = useState('');
  const [showNewRoomInput, setShowNewRoomInput] = useState(false);

  const [micPermission, setMicPermission] = useState<MicPermission>('unknown');
  const [, setCheckingPermissions] = useState(false);

  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  const loadRooms = async () => {
    setLoadingRooms(true);
    try {
      const response = await apiClient.get<Room[]>('/api/rooms');
      const data = response.data ?? [];
      setRooms(data);

      if (!selectedRoom && data.length > 0) {
        setSelectedRoom(data[0].name);
      }
    } catch (err) {
      console.error('Failed to load rooms:', err);
      setError(t('device.couldNotLoadRooms'));
    } finally {
      setLoadingRooms(false);
    }
  };

  const checkMicrophonePermission = async () => {
    setCheckingPermissions(true);
    try {
      if (navigator.permissions && navigator.permissions.query) {
        const result = await navigator.permissions.query({ name: 'microphone' as PermissionName });
        setMicPermission(result.state);

        result.onchange = () => {
          setMicPermission(result.state);
        };
      } else {
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          stream.getTracks().forEach((track) => track.stop());
          setMicPermission('granted');
        } catch {
          setMicPermission('denied');
        }
      }
    } catch {
      setMicPermission('prompt');
    } finally {
      setCheckingPermissions(false);
    }
  };

  const requestMicrophonePermission = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      stream.getTracks().forEach((track) => track.stop());
      setMicPermission('granted');
      setHasMicrophone(true);
    } catch {
      setMicPermission('denied');
      setHasMicrophone(false);
    }
  };

  const createRoom = async () => {
    if (!newRoomName.trim()) return;

    try {
      const response = await apiClient.post<Room>('/api/rooms', {
        name: newRoomName.trim(),
      });
      setRooms([...rooms, response.data]);
      setSelectedRoom(response.data.name);
      setNewRoomName('');
      setShowNewRoomInput(false);
    } catch (err) {
      console.error('Failed to create room:', err);
      setError(extractApiError(err, t('device.couldNotCreateRoom')));
    }
  };

  const handleComplete = useCallback(async () => {
    if (!selectedRoom) {
      setError(t('device.pleaseSelectRoom'));
      return;
    }

    setIsConnecting(true);
    setError(null);

    const config: DeviceConfig = {
      room: selectedRoom,
      type: deviceType,
      name: deviceName || null,
      isStationary,
      customCapabilities: {
        has_microphone: hasMicrophone,
        has_speaker: hasSpeaker,
        has_wakeword: hasWakeWord,
        ...(hasWakeWord ? { wakeword_method: 'browser_wasm' as const } : {}),
      },
    };

    try {
      await device.connect(config);

      setIsConnecting(false);
      onSetupComplete?.(config, device);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Connection failed';
      setError(message);
      setIsConnecting(false);
    }
  }, [device, selectedRoom, deviceType, deviceName, isStationary, hasMicrophone, hasSpeaker, hasWakeWord, onSetupComplete, t]);

  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
      {/* Header */}
      <div className="px-6 py-4 border-b border-gray-200 dark:border-gray-700">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2 bg-primary-500/20 rounded-lg">
              <Settings className="w-5 h-5 text-primary-400" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">{t('device.setup')}</h2>
              <p className="text-sm text-gray-500 dark:text-gray-400">{t('device.configureVoice')}</p>
            </div>
          </div>
          {onCancel && (
            <button
              onClick={onCancel}
              className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
            >
              <X className="w-5 h-5 text-gray-500 dark:text-gray-400" />
            </button>
          )}
        </div>
      </div>

      {/* Content */}
      <div className="p-6 space-y-6">
        {/* Error message */}
        {error && (
          <div className="p-4 bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 rounded-lg text-red-700 dark:text-red-300 text-sm">
            {error}
          </div>
        )}

        {/* Room Selection */}
        <div>
          <label htmlFor="room-select" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            <MapPin className="w-4 h-4 inline mr-2" aria-hidden="true" />
            {t('device.room')}
          </label>
          <div className="flex space-x-2">
            <select
              id="room-select"
              value={selectedRoom}
              onChange={(e) => setSelectedRoom(e.target.value)}
              disabled={loadingRooms}
              className="flex-1 bg-gray-100 dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg px-4 py-2 text-gray-900 dark:text-white focus:border-primary-500 focus:outline-hidden disabled:opacity-50"
              aria-describedby={loadingRooms ? 'room-loading' : undefined}
            >
              <option value="">{t('device.selectRoom')}</option>
              {rooms.map(room => (
                <option key={room.id} value={room.name}>
                  {room.name}
                </option>
              ))}
            </select>
            {loadingRooms && <span id="room-loading" className="sr-only">{t('rooms.loadingRooms')}</span>}
            <button
              onClick={() => setShowNewRoomInput(!showNewRoomInput)}
              className="px-4 py-2 bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 transition-colors"
              aria-label={t('device.addNewRoom')}
              aria-expanded={showNewRoomInput}
            >
              +
            </button>
            <button
              onClick={loadRooms}
              disabled={loadingRooms}
              className="px-3 py-2 bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 border border-gray-300 dark:border-gray-600 rounded-lg text-gray-700 dark:text-gray-300 transition-colors disabled:opacity-50"
              aria-label={t('device.refreshRooms')}
            >
              <RefreshCw className={`w-4 h-4 ${loadingRooms ? 'animate-spin' : ''}`} aria-hidden="true" />
            </button>
          </div>

          {/* New room input */}
          {showNewRoomInput && (
            <div className="mt-2 flex space-x-2">
              <label htmlFor="new-room-name" className="sr-only">{t('device.newRoomName')}</label>
              <input
                id="new-room-name"
                type="text"
                value={newRoomName}
                onChange={(e) => setNewRoomName(e.target.value)}
                placeholder={t('device.newRoomPlaceholder')}
                className="flex-1 bg-gray-100 dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg px-4 py-2 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:border-primary-500 focus:outline-hidden"
                onKeyDown={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    createRoom();
                  }
                }}
              />
              <button
                onClick={createRoom}
                disabled={!newRoomName.trim()}
                className="px-4 py-2 bg-primary-600 hover:bg-primary-500 rounded-lg text-white transition-colors disabled:opacity-50"
                aria-label={t('device.createRoom')}
              >
                <Check className="w-4 h-4" aria-hidden="true" />
              </button>
            </div>
          )}
        </div>

        {/* Device Type Selection */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
            {t('device.deviceType')}
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
                        : 'border-gray-300 dark:border-gray-600 bg-gray-100/50 dark:bg-gray-700/50 hover:border-gray-400 dark:hover:border-gray-500'
                    }`}
                  >
                    <div className="flex items-start space-x-3">
                      <Icon className={`w-5 h-5 ${isSelected ? 'text-primary-400' : 'text-gray-500 dark:text-gray-400'}`} />
                      <div>
                        <div className={`font-medium ${isSelected ? 'text-gray-900 dark:text-white' : 'text-gray-700 dark:text-gray-300'}`}>
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
          <label htmlFor="device-name" className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-2">
            {t('device.deviceNameOptional')}
          </label>
          <input
            id="device-name"
            type="text"
            value={deviceName}
            onChange={(e) => setDeviceName(e.target.value)}
            placeholder={t('device.deviceNamePlaceholder')}
            className="w-full bg-gray-100 dark:bg-gray-700 border border-gray-300 dark:border-gray-600 rounded-lg px-4 py-2 text-gray-900 dark:text-white placeholder-gray-500 dark:placeholder-gray-400 focus:border-primary-500 focus:outline-hidden"
          />
        </div>

        {/* Capabilities */}
        <div>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-3">
            {t('device.capabilities')}
          </label>
          <div className="space-y-3">
            {/* Microphone */}
            <div className="flex items-center justify-between p-3 bg-gray-100/50 dark:bg-gray-700/50 rounded-lg">
              <div className="flex items-center space-x-3">
                <Mic className={`w-4 h-4 ${hasMicrophone ? 'text-green-500 dark:text-green-400' : 'text-gray-500'}`} />
                <div>
                  <div className="text-sm text-gray-900 dark:text-white">{t('device.microphone')}</div>
                  <div className="text-xs text-gray-500">
                    {micPermission === 'granted' ? t('device.permissionGranted') :
                     micPermission === 'denied' ? t('device.permissionDenied') :
                     t('device.permissionRequired')}
                  </div>
                </div>
              </div>
              <div className="flex items-center space-x-2">
                {micPermission !== 'granted' && (
                  <button
                    onClick={requestMicrophonePermission}
                    className="text-xs px-2 py-1 bg-primary-600 hover:bg-primary-500 rounded-sm text-white"
                  >
                    {t('device.allow')}
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
                  <div className="w-9 h-5 bg-gray-300 dark:bg-gray-600 peer-focus:outline-hidden rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600 peer-disabled:opacity-50"></div>
                </label>
              </div>
            </div>

            {/* Speaker */}
            <div className="flex items-center justify-between p-3 bg-gray-100/50 dark:bg-gray-700/50 rounded-lg">
              <div className="flex items-center space-x-3">
                <Speaker className={`w-4 h-4 ${hasSpeaker ? 'text-green-500 dark:text-green-400' : 'text-gray-500'}`} />
                <div>
                  <div className="text-sm text-gray-900 dark:text-white">{t('device.speakerTts')}</div>
                  <div className="text-xs text-gray-500">{t('device.playVoiceResponses')}</div>
                </div>
              </div>
              <label className="relative inline-flex items-center cursor-pointer">
                <input
                  type="checkbox"
                  checked={hasSpeaker}
                  onChange={(e) => setHasSpeaker(e.target.checked)}
                  className="sr-only peer"
                />
                <div className="w-9 h-5 bg-gray-300 dark:bg-gray-600 peer-focus:outline-hidden rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600"></div>
              </label>
            </div>

            {/* Wake Word (only for panel/tablet types) */}
            {(deviceType === DEVICE_TYPES.WEB_PANEL || deviceType === DEVICE_TYPES.WEB_TABLET) && (
              <div className="flex items-center justify-between p-3 bg-gray-100/50 dark:bg-gray-700/50 rounded-lg">
                <div className="flex items-center space-x-3">
                  <div className={`w-4 h-4 rounded-full ${hasWakeWord ? 'bg-green-500 dark:bg-green-400' : 'bg-gray-400 dark:bg-gray-500'}`} />
                  <div>
                    <div className="text-sm text-gray-900 dark:text-white">{t('device.wakeword')}</div>
                    <div className="text-xs text-gray-500">{t('device.handsfreActivation')}</div>
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
                  <div className="w-9 h-5 bg-gray-300 dark:bg-gray-600 peer-focus:outline-hidden rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600 peer-disabled:opacity-50"></div>
                </label>
              </div>
            )}
          </div>
        </div>

        {/* Stationary toggle (for browser type) */}
        {deviceType === DEVICE_TYPES.WEB_BROWSER && (
          <div className="flex items-center justify-between p-3 bg-gray-100/50 dark:bg-gray-700/50 rounded-lg">
            <div>
              <div className="text-sm text-gray-900 dark:text-white">{t('device.stationaryDevice')}</div>
              <div className="text-xs text-gray-500">
                {t('device.staysInOneRoom')}
              </div>
            </div>
            <label className="relative inline-flex items-center cursor-pointer">
              <input
                type="checkbox"
                checked={isStationary}
                onChange={(e) => setIsStationary(e.target.checked)}
                className="sr-only peer"
              />
              <div className="w-9 h-5 bg-gray-300 dark:bg-gray-600 peer-focus:outline-hidden rounded-full peer peer-checked:after:translate-x-full peer-checked:after:border-white after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-4 after:w-4 after:transition-all peer-checked:bg-primary-600"></div>
            </label>
          </div>
        )}
      </div>

      {/* Footer */}
      <div className="px-6 py-4 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
        <div className="flex justify-end space-x-3">
          {onCancel && (
            <button
              onClick={onCancel}
              className="btn btn-secondary"
            >
              {t('common.cancel')}
            </button>
          )}
          <button
            onClick={handleComplete}
            disabled={isConnecting || !selectedRoom}
            className="btn btn-primary flex items-center space-x-2 disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {isConnecting ? (
              <>
                <Loader className="w-4 h-4 animate-spin" />
                <span>{t('device.connecting')}</span>
              </>
            ) : (
              <>
                <Check className="w-4 h-4" />
                <span>{t('device.connect')}</span>
              </>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
