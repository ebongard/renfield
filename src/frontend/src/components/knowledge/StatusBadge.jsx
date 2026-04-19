/**
 * StatusBadge — status pill for a Document row with stage + queue-position
 * sub-labels (#388).
 *
 * Accessibility (C2):
 *   - `processing` with known total pages renders a real `<progress>` with
 *     aria-valuenow / aria-valuemax / aria-valuetext so screen readers
 *     announce "Seite 47 von 120".
 *   - `processing` without a page count carries `aria-busy="true"` instead.
 *   - Stage changes announce through a dedicated polite-live sub-region
 *     rate-limited to one announcement per 10 s so long OCR jobs don't
 *     spam the screen-reader buffer.
 *   - Icon-only badges carry `aria-label="{statusLabel}: {filename}"`.
 *   - Contrast: pending text bumped from `gray-500` → `gray-700`
 *     (`gray-300` in dark) to clear WCAG AA against the card background.
 */
import { useEffect, useRef, useState } from 'react';
import { CheckCircle, Loader2, Clock, AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';

const STATUS_META = {
  completed: { Icon: CheckCircle, labelKey: 'knowledge.statusCompleted', iconClass: 'text-green-500', spin: false },
  processing: { Icon: Loader2, labelKey: 'knowledge.statusProcessing', iconClass: 'text-primary-500', spin: true },
  pending: { Icon: Clock, labelKey: 'knowledge.statusQueued', iconClass: 'text-gray-500', spin: false },
  failed: { Icon: AlertCircle, labelKey: 'knowledge.statusFailed', iconClass: 'text-red-500', spin: false },
};

const STAGE_KEY = {
  parsing: 'knowledge.stageParsing',
  chunking: 'knowledge.stageChunking',
  embedding: 'knowledge.stageEmbedding',
};

// Minimum gap between live-region announcements for stage/page updates on
// the same document. Prevents SR buffer spam on long OCR jobs.
const LIVE_REGION_MIN_GAP_MS = 10_000;

function subLabel(t, doc) {
  if (doc.status === 'pending' && doc.queue_position != null) {
    return t('knowledge.statusQueuePosition', { position: doc.queue_position });
  }
  if (doc.status === 'processing') {
    if (doc.stage === 'ocr') {
      if (doc.pages && doc.pages.total > 0) {
        return t('knowledge.stageOcr', { current: doc.pages.current, total: doc.pages.total });
      }
      return t('knowledge.stageOcrNoPages');
    }
    const key = STAGE_KEY[doc.stage];
    if (key) return t(key);
  }
  return null;
}

function hasKnownPageProgress(doc) {
  return (
    doc.status === 'processing' &&
    doc.pages &&
    typeof doc.pages.current === 'number' &&
    typeof doc.pages.total === 'number' &&
    doc.pages.total > 0
  );
}

export default function StatusBadge({ doc, filename }) {
  const { t } = useTranslation();
  const meta = STATUS_META[doc.status] || STATUS_META.pending;
  const label = t(meta.labelKey);
  const sub = subLabel(t, doc);
  const { Icon } = meta;
  const progress = hasKnownPageProgress(doc);

  // Rate-limit the live-region announcement so a long OCR job doesn't hit
  // the screen reader with 120 updates in a minute.
  const lastAnnouncedAtRef = useRef(0);
  const [announcement, setAnnouncement] = useState('');
  useEffect(() => {
    const now = Date.now();
    if (!sub) return;
    if (now - lastAnnouncedAtRef.current < LIVE_REGION_MIN_GAP_MS) return;
    lastAnnouncedAtRef.current = now;
    setAnnouncement(`${label}: ${sub}`);
  }, [label, sub]);

  // Status transitions are always announced (terminal or major change). The
  // stage/page sub-label uses a separate polite region above.
  const statusAnnouncement = `${label}: ${filename || doc.filename}`;

  // The outer element swaps semantics based on whether we know pages. We
  // keep the visible markup identical across branches — only the ARIA
  // wiring differs.
  const outerProps = progress
    ? {
        role: 'progressbar',
        'aria-valuenow': doc.pages.current,
        'aria-valuemin': 0,
        'aria-valuemax': doc.pages.total,
        'aria-valuetext': sub || undefined,
        'aria-label': statusAnnouncement,
      }
    : {
        role: 'status',
        'aria-live': 'polite',
        'aria-busy': doc.status === 'processing' ? 'true' : undefined,
        'aria-label': statusAnnouncement,
      };

  return (
    <div {...outerProps} className="inline-flex items-start gap-2">
      <Icon
        aria-hidden="true"
        className={`w-5 h-5 shrink-0 ${meta.iconClass} ${meta.spin ? 'animate-spin' : ''}`}
      />
      <div className="flex flex-col">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{label}</span>
        {sub && (
          <span className="text-xs text-gray-700 dark:text-gray-300 leading-tight">{sub}</span>
        )}
      </div>
      {/* Separate polite-live sub-region for rate-limited stage announcements.
          Visually hidden but read by screen readers. Kept out of the
          progressbar element so SR doesn't get duplicate readouts. */}
      <span className="sr-only" aria-live="polite">{announcement}</span>
    </div>
  );
}
