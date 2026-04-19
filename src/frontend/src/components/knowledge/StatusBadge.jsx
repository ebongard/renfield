/**
 * StatusBadge — status pill for a Document row with stage + queue-position
 * sub-labels (#388).
 *
 * Accessibility (C1 subset):
 *   - Wrapping element has role="status" aria-live="polite" so screen readers
 *     announce status transitions.
 *   - Icon is aria-hidden; the full status string is carried on aria-label.
 *
 * Full progressbar semantics (for the pages={current,total} case) land in
 * C2 together with focus management on the 409 dialog.
 */
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

export default function StatusBadge({ doc, filename }) {
  const { t } = useTranslation();
  const meta = STATUS_META[doc.status] || STATUS_META.pending;
  const label = t(meta.labelKey);
  const sub = subLabel(t, doc);
  const { Icon } = meta;

  return (
    <div
      role="status"
      aria-live="polite"
      aria-label={`${label}: ${filename || doc.filename}`}
      className="inline-flex items-start gap-2"
    >
      <Icon
        aria-hidden="true"
        className={`w-5 h-5 shrink-0 ${meta.iconClass} ${meta.spin ? 'animate-spin' : ''}`}
      />
      <div className="flex flex-col">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">{label}</span>
        {sub && (
          <span className="text-xs text-gray-500 dark:text-gray-400 leading-tight">{sub}</span>
        )}
      </div>
    </div>
  );
}
