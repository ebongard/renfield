import React from 'react';
import { useTranslation } from 'react-i18next';

/**
 * Audio waveform visualizer during recording.
 * Shows audio level bars and silence countdown.
 *
 * @param {Object} props - Component props
 * @param {number} props.audioLevel - Current audio level (0-100)
 * @param {number} props.silenceTimeRemaining - Time until auto-stop (ms)
 */
export default function AudioVisualizer({ audioLevel = 0, silenceTimeRemaining = 0 }) {
  const { t } = useTranslation();

  return (
    <div className="mb-4 p-4 bg-linear-to-br from-gray-100/80 to-gray-200/80 dark:from-gray-800/80 dark:to-gray-900/80 rounded-xl border border-gray-300/50 dark:border-gray-700/50 backdrop-blur-xs">
      {/* Header with status and countdown */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center space-x-2">
          <div className="w-2.5 h-2.5 bg-red-500 rounded-full animate-pulse"></div>
          <span className="text-sm font-medium text-gray-900 dark:text-white">
            {audioLevel > 10 ? t('voice.speechDetected') : silenceTimeRemaining > 0 ? t('voice.silenceDetected') : t('voice.listening')}
          </span>
        </div>

        {/* Countdown Timer */}
        {silenceTimeRemaining > 0 && (
          <div className="flex items-center space-x-2 px-3 py-1 bg-yellow-100 dark:bg-yellow-500/20 rounded-full border border-yellow-300 dark:border-yellow-500/30">
            <div className="w-1.5 h-1.5 bg-yellow-500 dark:bg-yellow-400 rounded-full animate-pulse"></div>
            <span className="text-xs font-mono text-yellow-700 dark:text-yellow-300">
              {t('voice.autoStopIn', { seconds: (silenceTimeRemaining / 1000).toFixed(1) })}
            </span>
          </div>
        )}
      </div>

      {/* Waveform visualization */}
      <div className="flex items-center justify-center space-x-1.5 h-16 mb-3">
        {[0, 1, 2, 3, 4, 5, 6, 7, 8].map((i) => {
          // Calculate height based on audioLevel with variation for wave effect
          const variation = Math.sin((Date.now() / 100) + i) * 0.3 + 0.7;
          const baseHeight = Math.max(10, audioLevel) * variation;
          const height = Math.min(100, baseHeight);

          // Color based on level
          const colorClass = audioLevel > 50 ? 'bg-green-500' :
            audioLevel > 10 ? 'bg-primary-500' :
              'bg-gray-400 dark:bg-gray-600';

          return (
            <div
              key={i}
              className={`w-2 rounded-full transition-all duration-150 ease-out ${colorClass}`}
              style={{
                height: `${height}%`,
                opacity: audioLevel > 5 ? 1 : 0.3
              }}
            />
          );
        })}
      </div>

      {/* Info Text */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-500 dark:text-gray-400">
          {t('voice.level')}: {audioLevel} / 10
        </span>
        <span className="text-gray-400 dark:text-gray-500">
          {t('voice.clickToStop')}
        </span>
      </div>
    </div>
  );
}
