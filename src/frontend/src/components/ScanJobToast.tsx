import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ScanLine, X } from 'lucide-react';

import { SCAN_JOB_FINISHED_EVENT, type ScanJobFinishedDetail } from '../hooks/useUserEvents';
import { isTabConversation } from '../utils/tabConversations';

/**
 * Live notice that a background scan the user started has finished.
 *
 * The scan runs on the scanner host after `scan_document` returned; its outcome is
 * written server-side as a message into the requesting conversation and announced
 * over the content-free /ws/user socket. This toast only says THAT it finished and
 * points at the chat — the event carries no document identity, by design.
 *
 * Only in the tab the scan was requested from: the event names its conversation,
 * and a tab shows the notice only if it wrote into that conversation. In a
 * household without login every tab receives the event, and a notice in all of
 * them reads as a scan nobody there asked for; the other tabs still refresh the
 * conversation list quietly. A voice request has no tab — it is answered in its
 * room. An event without a conversation (older backend) shows, as before.
 *
 * Deliberately standalone, not NotificationToast: that one is the device-WS queue
 * with server acks, and it is presence-gated for personal notices, which would hide
 * this from a user who is not BLE-tracked.
 */
const AUTO_DISMISS_MS = 10_000;

type Outcome = 'done' | 'unrouted' | 'failed' | 'interrupted';

const OUTCOMES: readonly Outcome[] = ['done', 'unrouted', 'failed', 'interrupted'];

/** A reason this build does not know gets NO toast rather than a claim of
 * success — the chat still reloads and shows the real outcome. */
function toOutcome(reason: unknown): Outcome | null {
  return OUTCOMES.find((o) => o === reason) ?? null;
}

export default function ScanJobToast() {
  const { t } = useTranslation();
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent<ScanJobFinishedDetail>).detail;
      if (detail?.sessionId && !isTabConversation(detail.sessionId)) return;
      setOutcome(toOutcome(detail?.reason));
    };
    window.addEventListener(SCAN_JOB_FINISHED_EVENT, handler);
    return () => window.removeEventListener(SCAN_JOB_FINISHED_EVENT, handler);
  }, []);

  useEffect(() => {
    if (!outcome) return;
    const timer = setTimeout(() => setOutcome(null), AUTO_DISMISS_MS);
    return () => clearTimeout(timer);
  }, [outcome]);

  if (!outcome) return null;

  return (
    <div
      className="toast bottom-4 left-1/2 -translate-x-1/2 sm:left-auto sm:right-4 sm:translate-x-0"
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-4">
        <span className="flex items-center gap-2 text-sm text-gray-800 dark:text-gray-100">
          <ScanLine className="w-4 h-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
          {t(`notifications.scanJob.${outcome}`)}
        </span>
        <button
          type="button"
          onClick={() => setOutcome(null)}
          aria-label={t('notifications.dismiss')}
          className="text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-100 min-h-[44px] min-w-[44px] sm:min-h-0 sm:min-w-0 flex items-center justify-center"
        >
          <X className="w-4 h-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
