/**
 * DeviceStatus Component
 *
 * Shows current device/room status and provides quick access to device setup.
 * Displayed in the app header/navbar.
 */

import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import {
  MapPin,
  Wifi,
  WifiOff,
  Settings,
  Monitor,
  Tablet,
  Smartphone,
  Tv,
  Mic,
  MicOff,
  Volume2,
  VolumeX,
  Loader,
} from 'lucide-react';
import { useDevice, DEVICE_TYPES, CONNECTION_STATES, DEVICE_STATES } from '../context/DeviceContext';
import { useCapabilities } from '../hooks/useCapabilities';
import DeviceSetup from './DeviceSetup';

// Device type icons
const DEVICE_ICONS = {
  [DEVICE_TYPES.SATELLITE]: Wifi,
  [DEVICE_TYPES.WEB_PANEL]: Monitor,
  [DEVICE_TYPES.WEB_TABLET]: Tablet,
  [DEVICE_TYPES.WEB_BROWSER]: Smartphone,
  [DEVICE_TYPES.WEB_KIOSK]: Tv,
};

// State colors
const STATE_COLORS = {
  [DEVICE_STATES.IDLE]: 'bg-gray-500',
  [DEVICE_STATES.LISTENING]: 'bg-green-500 animate-pulse',
  [DEVICE_STATES.PROCESSING]: 'bg-yellow-500 animate-pulse',
  [DEVICE_STATES.SPEAKING]: 'bg-blue-500 animate-pulse',
  [DEVICE_STATES.ERROR]: 'bg-red-500',
};

export default function DeviceStatus({ compact = false }) {
  const { t } = useTranslation();
  const device = useDevice();
  const { features, hasMicrophone, hasSpeaker } = useCapabilities();
  const [showSetup, setShowSetup] = useState(false);

  const DeviceIcon = DEVICE_ICONS[device.deviceType] || Smartphone;
  const stateColor = STATE_COLORS[device.deviceState] || 'bg-gray-500';

  // Handle setup completion
  const handleSetupComplete = (config, connection) => {
    device.handleSetupComplete(config);
    setShowSetup(false);
  };

  // Compact version for navbar
  if (compact) {
    return (
      <>
        <button
          onClick={() => setShowSetup(true)}
          className={`flex items-center space-x-2 px-3 py-1.5 rounded-lg transition-colors ${
            device.isConnected
              ? 'bg-green-100 dark:bg-green-900/30 hover:bg-green-200 dark:hover:bg-green-900/50 border border-green-300 dark:border-green-700/50'
              : device.isSetupComplete
                ? 'bg-yellow-100 dark:bg-yellow-900/30 hover:bg-yellow-200 dark:hover:bg-yellow-900/50 border border-yellow-300 dark:border-yellow-700/50'
                : 'bg-gray-100 dark:bg-gray-700 hover:bg-gray-200 dark:hover:bg-gray-600 border border-gray-300 dark:border-gray-600'
          }`}
        >
          {device.isConnected ? (
            <>
              <div className={`w-2 h-2 rounded-full ${stateColor}`} />
              <MapPin className="w-3.5 h-3.5 text-green-600 dark:text-green-400" />
              <span className="text-sm text-green-700 dark:text-green-300">{device.roomName || t('common.connected')}</span>
            </>
          ) : device.isConnecting ? (
            <>
              <Loader className="w-3.5 h-3.5 text-yellow-600 dark:text-yellow-400 animate-spin" />
              <span className="text-sm text-yellow-700 dark:text-yellow-300">{t('device.connecting')}</span>
            </>
          ) : device.isSetupComplete ? (
            <>
              <WifiOff className="w-3.5 h-3.5 text-yellow-600 dark:text-yellow-400" />
              <span className="text-sm text-yellow-700 dark:text-yellow-300">{t('common.offline')}</span>
            </>
          ) : (
            <>
              <Settings className="w-3.5 h-3.5 text-gray-500 dark:text-gray-400" />
              <span className="text-sm text-gray-700 dark:text-gray-300">{t('device.setup')}</span>
            </>
          )}
        </button>

        {/* Setup Modal */}
        {showSetup && (
          <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
            <div className="w-full max-w-lg">
              <DeviceSetup
                onSetupComplete={handleSetupComplete}
                onCancel={() => setShowSetup(false)}
                existingConfig={device.getStoredConfig()}
              />
            </div>
          </div>
        )}
      </>
    );
  }

  // Full version for settings/status page
  return (
    <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
        <div className="flex items-center space-x-3">
          <div className={`p-2 rounded-lg ${device.isConnected ? 'bg-green-100 dark:bg-green-500/20' : 'bg-gray-100 dark:bg-gray-700'}`}>
            <DeviceIcon className={`w-5 h-5 ${device.isConnected ? 'text-green-600 dark:text-green-400' : 'text-gray-500 dark:text-gray-400'}`} />
          </div>
          <div>
            <h3 className="font-medium text-gray-900 dark:text-white">
              {device.deviceName || t('device.thisDevice')}
            </h3>
            <p className="text-sm text-gray-500 dark:text-gray-400">
              {device.isConnected ? t('common.connected') : device.isConnecting ? t('device.connecting') : t('device.notConnected')}
            </p>
          </div>
        </div>

        <button
          onClick={() => setShowSetup(true)}
          className="p-2 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors"
        >
          <Settings className="w-5 h-5 text-gray-500 dark:text-gray-400" />
        </button>
      </div>

      {/* Status */}
      <div className="p-4 space-y-3">
        {/* Connection Status */}
        <div className="flex items-center justify-between">
          <span className="text-sm text-gray-500 dark:text-gray-400">{t('common.status')}</span>
          <div className="flex items-center space-x-2">
            <div className={`w-2 h-2 rounded-full ${stateColor}`} />
            <span className="text-sm text-gray-900 dark:text-white capitalize">{device.deviceState}</span>
          </div>
        </div>

        {/* Room */}
        {device.roomName && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-500 dark:text-gray-400">{t('device.room')}</span>
            <div className="flex items-center space-x-2">
              <MapPin className="w-4 h-4 text-gray-400 dark:text-gray-500" />
              <span className="text-sm text-gray-900 dark:text-white">{device.roomName}</span>
            </div>
          </div>
        )}

        {/* Capabilities */}
        <div className="flex items-center justify-between">
          <span className="text-sm text-gray-500 dark:text-gray-400">{t('device.capabilities')}</span>
          <div className="flex items-center space-x-2">
            {hasMicrophone ? (
              <Mic className="w-4 h-4 text-green-500 dark:text-green-400" />
            ) : (
              <MicOff className="w-4 h-4 text-gray-400 dark:text-gray-500" />
            )}
            {hasSpeaker ? (
              <Volume2 className="w-4 h-4 text-green-500 dark:text-green-400" />
            ) : (
              <VolumeX className="w-4 h-4 text-gray-400 dark:text-gray-500" />
            )}
          </div>
        </div>

        {/* Device ID */}
        {device.deviceId && (
          <div className="flex items-center justify-between">
            <span className="text-sm text-gray-500 dark:text-gray-400">{t('device.deviceId')}</span>
            <span className="text-xs text-gray-400 dark:text-gray-500 font-mono">{device.deviceId}</span>
          </div>
        )}
      </div>

      {/* Actions */}
      <div className="px-4 py-3 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50">
        {device.isConnected ? (
          <button
            onClick={() => device.disconnect()}
            className="w-full px-4 py-2 bg-red-100 dark:bg-red-600/20 hover:bg-red-200 dark:hover:bg-red-600/30 border border-red-300 dark:border-red-600/50 rounded-lg text-red-600 dark:text-red-400 text-sm transition-colors"
          >
            {t('device.disconnect')}
          </button>
        ) : device.isSetupComplete ? (
          <button
            onClick={() => device.autoConnect()}
            className="w-full btn btn-primary text-sm"
          >
            {t('device.connect')}
          </button>
        ) : (
          <button
            onClick={() => setShowSetup(true)}
            className="w-full btn btn-primary text-sm"
          >
            {t('device.setup')}
          </button>
        )}
      </div>

      {/* Setup Modal */}
      {showSetup && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 p-4">
          <div className="w-full max-w-lg">
            <DeviceSetup
              onSetupComplete={handleSetupComplete}
              onCancel={() => setShowSetup(false)}
              existingConfig={device.getStoredConfig()}
            />
          </div>
        </div>
      )}
    </div>
  );
}
