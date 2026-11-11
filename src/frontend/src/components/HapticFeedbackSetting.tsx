/**
 * Per-device "haptic feedback" preference (#84).
 *
 * Unlike the rest of the account page this is NOT stored on the account: a
 * vibration motor belongs to the device, so the preference lives in this
 * browser's localStorage (same as theme and wake-word settings) and defaults
 * to off.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Vibrate } from 'lucide-react';

import {
  haptic,
  isHapticsSupported,
  loadHapticsEnabled,
  prefersReducedMotion,
  saveHapticsEnabled,
} from '../utils/haptics';

export default function HapticFeedbackSetting() {
  const { t } = useTranslation();
  const supported = isHapticsSupported();
  const reducedMotion = prefersReducedMotion();
  const [enabled, setEnabled] = useState<boolean>(() => supported && loadHapticsEnabled());
  const [saveFailed, setSaveFailed] = useState(false);

  const handleToggle = () => {
    const next = !enabled;
    const saved = saveHapticsEnabled(next);
    // The switch shows what `haptic()` will actually do: it reads the storage,
    // so a write that did not go through leaves the feature off.
    setSaveFailed(!saved);
    setEnabled(saved ? next : loadHapticsEnabled());
    // Confirms switching ON by feel. No condition needed: `haptic()` reads the
    // preference just written, so switching off (or a failed write) is silent.
    haptic('tap');
  };

  return (
    <div className="card mt-4">
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <h2 className="flex items-center gap-2 text-lg font-medium text-gray-900 dark:text-white">
            <Vibrate className="w-5 h-5" aria-hidden="true" />
            {t('account.hapticsTitle')}
          </h2>
          <p id="haptics-explain" className="mt-2 text-sm text-gray-600 dark:text-gray-400">
            {t('account.hapticsExplain')}
          </p>
          <p className="mt-3 text-sm font-medium text-gray-500 dark:text-gray-400" aria-live="polite">
            {enabled ? t('account.hapticsStateOn') : t('account.hapticsStateOff')}
          </p>
          {!supported && (
            <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">
              {t('account.hapticsUnsupported')}
            </p>
          )}
          {supported && reducedMotion && (
            <p className="mt-2 text-xs text-gray-600 dark:text-gray-300">
              {t('account.hapticsReducedMotion')}
            </p>
          )}
          {saveFailed && (
            <p className="mt-2 text-xs text-gray-600 dark:text-gray-300" role="status">
              {t('account.hapticsNotSaved')}
            </p>
          )}
        </div>

        <button
          type="button"
          role="switch"
          aria-checked={enabled}
          aria-label={t('account.hapticsTitle')}
          aria-describedby="haptics-explain"
          onClick={handleToggle}
          disabled={!supported}
          title={!supported ? t('account.hapticsUnsupported') : undefined}
          className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <span
            className={`relative inline-flex h-7 w-12 items-center rounded-full transition-colors motion-reduce:transition-none ${
              enabled ? 'bg-accent-600 dark:bg-accent-500' : 'bg-gray-300 dark:bg-gray-600'
            }`}
          >
            <span
              className={`inline-block h-5 w-5 rounded-full bg-white shadow transition-transform motion-reduce:transition-none ${
                enabled ? 'translate-x-6' : 'translate-x-1'
              }`}
            />
          </span>
        </button>
      </div>
    </div>
  );
}
