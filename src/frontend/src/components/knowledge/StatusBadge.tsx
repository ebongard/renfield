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
import type { LucideIcon } from 'lucide-react';
import { AlertCircle, CheckCircle, Clock, Loader2 } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';

export type DocStatus = 'completed' | 'processing' | 'pending' | 'failed';

export interface DocPages {
  current: number;
  total: number;
}

export interface DocLike {
  status: DocStatus;
  filename?: string;
  queue_position?: number | null;
  stage?: 'parsing' | 'chunking' | 'embedding' | 'ocr' | string | null;
  pages?: DocPages | null;
}

interface StatusMeta {
  Icon: LucideIcon;
  labelKey: string;
  iconClass: string;
  spin: boolean;
}

const STATUS_META: Record<DocStatus, StatusMeta> = {
  completed: { Icon: CheckCircle, labelKey: 'knowledge.statusCompleted', iconClass: 'text-green-500', spin: false },
  processing: { Icon: Loader2, labelKey: 'knowledge.statusProcessing', iconClass: 'text-primary-500', spin: true },
  pending: { Icon: Clock, labelKey: 'knowledge.statusQueued', iconClass: 'text-gray-500', spin: false },
  failed: { Icon: AlertCircle, labelKey: 'knowledge.statusFailed', iconClass: 'text-red-500', spin: false },
};

const STAGE_KEY: Record<string, string> = {
  parsing: 'knowledge.stageParsing',
  chunking: 'knowledge.stageChunking',
  embedding: 'knowledge.stageEmbedding',
};

// Minimum gap between live-region announcements for stage/page updates on
// the same document. Prevents SR buffer spam on long OCR jobs.
const LIVE_REGION_MIN_GAP_MS = 10_000;

type TFn = (key: string, options?: Record<string, unknown>) => string;

function subLabel(t: TFn, doc: DocLike): string | null {
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
    const key = doc.stage ? STAGE_KEY[doc.stage] : undefined;
    if (key) return t(key);
  }
  return null;
}

function hasKnownPageProgress(doc: DocLike): doc is DocLike & { pages: DocPages } {
  return (
    doc.status === 'processing' &&
    doc.pages != null &&
    typeof doc.pages.current === 'number' &&
    typeof doc.pages.total === 'number' &&
    doc.pages.total > 0
  );
}

interface StatusBadgeProps {
  doc: DocLike;
  filename?: string;
}

export default function StatusBadge({ doc, filename }: StatusBadgeProps) {
  const { t } = useTranslation();
  const meta = STATUS_META[doc.status] ?? STATUS_META.pending;
  const label = t(meta.labelKey);
  const sub = subLabel(t as TFn, doc);
  const { Icon } = meta;
  const progress = hasKnownPageProgress(doc);

  // Rate-limit the live-region announcement so a long OCR job doesn't hit
  // the screen reader with 120 updates in a minute. Key the effect on
  // `sub` only — a language switch changes `label` but shouldn't reset
  // the rate-limit window (user already heard the equivalent phrase in
  // the old language).
  const lastAnnouncedAtRef = useRef<number>(0);
  const [announcement, setAnnouncement] = useState<string>('');
  useEffect(() => {
    if (!sub) return;
    const now = Date.now();
    if (now - lastAnnouncedAtRef.current < LIVE_REGION_MIN_GAP_MS) return;
    lastAnnouncedAtRef.current = now;
    setAnnouncement(`${label}: ${sub}`);
    // eslint-disable-next-line react-hooks/exhaustive-deps — see above
  }, [sub]);

  const statusAnnouncement = `${label}: ${filename || doc.filename || ''}`;

  // The outer element swaps semantics based on whether we know pages. We
  // keep the visible markup identical across branches — only the ARIA
  // wiring differs.
  type DivAttrs = React.HTMLAttributes<HTMLDivElement> & {
    'aria-valuenow'?: number;
    'aria-valuemin'?: number;
    'aria-valuemax'?: number;
    'aria-valuetext'?: string;
  };
  const outerProps: DivAttrs = progress
    ? {
        role: 'progressbar',
        'aria-valuenow': doc.pages.current,
        'aria-valuemin': 0,
        'aria-valuemax': doc.pages.total,
        'aria-valuetext': sub ?? undefined,
        'aria-label': statusAnnouncement,
      }
    : {
        role: 'status',
        'aria-live': 'polite',
        'aria-busy': doc.status === 'processing' ? true : undefined,
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
      {announcement && (
        <span className="sr-only" aria-live="polite">{announcement}</span>
      )}
    </div>
  );
}
