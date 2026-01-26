import React, { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader, Ear, EarOff, Settings } from 'lucide-react';

/**
 * Chat header component with wake word controls and connection status.
 *
 * @param {Object} props - Component props
 * @param {boolean} props.wsConnected - WebSocket connection status
 * @param {Object} props.wakeWord - Wake word state and methods from useWakeWord hook
 * @param {boolean} props.recording - Whether currently recording audio
 */
export default function ChatHeader({
  wsConnected = false,
  wakeWord = {},
  recording = false,
}) {
  const { t } = useTranslation();
  const [showWakeWordSettings, setShowWakeWordSettings] = useState(false);

  const {
    isEnabled: wakeWordEnabled,
    isListening: wakeWordListening,
    isLoading: wakeWordLoading,
    error: wakeWordError,
    settings: wakeWordSettings = {},
    toggle: toggleWakeWord,
    setKeyword: setWakeWordKeyword,
    setThreshold: setWakeWordThreshold,
    availableKeywords = [],
    lastDetection,
    status: wakeWordStatus,
  } = wakeWord;

  return (
    <div className="card mb-4 mx-4 mt-4 md:mx-0 md:mt-0">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-white">{t('chat.title')}</h1>
          <p className="text-gray-500 dark:text-gray-400">{t('chat.subtitle')}</p>
        </div>
        <div className="flex items-center space-x-4">
          {/* Wake Word Controls */}
          <div className="flex items-center space-x-2">
            <button
              onClick={toggleWakeWord}
              disabled={wakeWordLoading || recording}
              className={`p-2 rounded-lg transition-all ${
                wakeWordEnabled
                  ? 'bg-green-600 hover:bg-green-700 text-white'
                  : wakeWordError
                    ? 'bg-red-100 hover:bg-red-200 text-red-600 dark:bg-red-900/50 dark:hover:bg-red-800/50 dark:text-red-300'
                    : 'bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300'
              } ${wakeWordLoading ? 'opacity-50 cursor-wait' : ''}`}
              title={wakeWordError
                ? `${t('wakeword.notAvailable')}: ${wakeWordError.message}`
                : wakeWordEnabled
                  ? t('wakeword.listening', { keyword: availableKeywords.find(k => k.id === wakeWordSettings.keyword)?.label || 'Hey Jarvis' })
                  : t('wakeword.enable')
              }
            >
              {wakeWordLoading ? (
                <Loader className="w-4 h-4 animate-spin" />
              ) : wakeWordEnabled ? (
                <Ear className="w-4 h-4" />
              ) : (
                <EarOff className="w-4 h-4" />
              )}
            </button>

            {/* Wake Word Settings Button */}
            {wakeWordEnabled && (
              <button
                onClick={() => setShowWakeWordSettings(!showWakeWordSettings)}
                className="p-2 rounded-lg bg-gray-200 hover:bg-gray-300 text-gray-600 dark:bg-gray-700 dark:hover:bg-gray-600 dark:text-gray-300"
                title={t('wakeword.settings')}
              >
                <Settings className="w-4 h-4" />
              </button>
            )}
          </div>

          {/* Connection Status */}
          <div className="flex items-center space-x-2">
            <div className={`w-3 h-3 rounded-full ${wsConnected ? 'bg-green-500' : 'bg-red-500'}`} />
            <span className="text-sm text-gray-500 dark:text-gray-400">
              {wsConnected ? t('common.connected') : t('common.disconnected')}
            </span>
          </div>
        </div>
      </div>

      {/* Wake Word Error Message */}
      {wakeWordError && !wakeWordEnabled && (
        <div className="mt-3 flex items-center px-3 py-2 bg-red-100 dark:bg-red-900/30 rounded-lg border border-red-300 dark:border-red-700/50">
          <div className="flex items-center space-x-2">
            <div className="w-2 h-2 rounded-full bg-red-500" />
            <span className="text-sm text-red-700 dark:text-red-300">
              {wakeWordError.name === 'BrowserNotSupportedError'
                ? t('wakeword.browserNotSupported')
                : <>{t('wakeword.notAvailable')}. Run: <code className="bg-red-200 dark:bg-red-900/50 px-1 rounded-sm">docker compose up -d --build</code></>
              }
            </span>
          </div>
        </div>
      )}

      {/* Wake Word Listening Indicator */}
      {wakeWordEnabled && !recording && (
        <div className="mt-3 flex items-center justify-between px-3 py-2 bg-green-100 dark:bg-green-900/30 rounded-lg border border-green-300 dark:border-green-700/50">
          <div className="flex items-center space-x-2">
            <div className={`w-2 h-2 rounded-full ${wakeWordListening ? 'bg-green-500 animate-pulse' : 'bg-yellow-500'}`} />
            <span className="text-sm text-green-700 dark:text-green-300">
              {wakeWordListening
                ? t('wakeword.listening', { keyword: availableKeywords.find(k => k.id === wakeWordSettings.keyword)?.label || 'Hey Jarvis' })
                : wakeWordStatus === 'activated'
                  ? t('wakeword.detected')
                  : t('wakeword.paused')
              }
            </span>
          </div>
          {lastDetection && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {t('wakeword.lastDetection')}: {lastDetection.keyword} ({(lastDetection.score * 100).toFixed(0)}%)
            </span>
          )}
        </div>
      )}

      {/* Wake Word Settings Dropdown */}
      {showWakeWordSettings && (
        <div className="mt-3 p-4 bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700">
          <h3 className="text-sm font-medium text-gray-900 dark:text-white mb-3">{t('wakeword.settings')}</h3>

          <div className="space-y-3">
            {/* Keyword Selection */}
            <div>
              <label className="text-xs text-gray-500 dark:text-gray-400 mb-1 block">{t('wakeword.keyword')}</label>
              <select
                value={wakeWordSettings.keyword || ''}
                onChange={(e) => setWakeWordKeyword?.(e.target.value)}
                className="w-full bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-white text-sm rounded-lg px-3 py-2 border border-gray-300 dark:border-gray-600 focus:border-primary-500 focus:outline-hidden"
              >
                {availableKeywords.map(kw => (
                  <option key={kw.id} value={kw.id}>{kw.label}</option>
                ))}
              </select>
            </div>

            {/* Threshold Slider */}
            <div>
              <label className="text-xs text-gray-500 dark:text-gray-400 mb-1 block">
                {t('wakeword.sensitivity')}: {((wakeWordSettings.threshold || 0.5) * 100).toFixed(0)}%
              </label>
              <input
                type="range"
                min="0.3"
                max="0.8"
                step="0.05"
                value={wakeWordSettings.threshold || 0.5}
                onChange={(e) => setWakeWordThreshold?.(parseFloat(e.target.value))}
                className="w-full accent-primary-600"
              />
              <div className="flex justify-between text-xs text-gray-500">
                <span>{t('wakeword.moreSensitive')}</span>
                <span>{t('wakeword.lessFalsePositives')}</span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
