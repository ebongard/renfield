import type { CSSProperties } from 'react';
import { useTranslation } from 'react-i18next';

import TierBadge, { type CircleTier } from '../TierBadge';

export const TIER_UNDO_WINDOW_MS = 5000;

/**
 * Local undo toast after a document's visibility changed. Self-contained (like
 * BestaetigtToast) — not routed through the server-ack notification queue. The
 * countdown bar is visual only; the parent's timer owns dismissal.
 */
interface TierChangeToastProps {
  tier: CircleTier;
  onUndo: () => void;
  durationMs?: number;
}

export default function TierChangeToast({ tier, onUndo, durationMs = TIER_UNDO_WINDOW_MS }: TierChangeToastProps) {
  const { t } = useTranslation();

  return (
    <div
      className="toast bottom-4 left-1/2 -translate-x-1/2 sm:left-auto sm:right-4 sm:translate-x-0"
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-4">
        <span className="flex items-center gap-2 text-sm text-gray-800 dark:text-gray-100">
          {t('knowledge.visibility.changed')}
          <TierBadge tier={tier} />
        </span>
        <button
          type="button"
          onClick={onUndo}
          className="text-sm font-medium text-primary-600 dark:text-primary-400 hover:underline min-h-[44px] sm:min-h-0"
        >
          {t('knowledge.visibility.undo')}
        </button>
      </div>
      <div className="h-1 w-full bg-gray-200 dark:bg-gray-700 rounded-full overflow-hidden">
        <div
          className="h-full bg-primary-500 animate-toast-countdown"
          style={{ '--toast-duration': `${durationMs}ms` } as CSSProperties}
        />
      </div>
    </div>
  );
}
