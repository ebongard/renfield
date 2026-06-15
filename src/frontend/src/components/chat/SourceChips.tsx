import { useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { FileText } from 'lucide-react';
import TierBadge from '../TierBadge';
import type { MessageSource } from '../../types/chat';

/**
 * Provenance "source chips" under a knowledge-backed assistant turn.
 *
 * Shows which KB documents the answer drew on (filename + access-tier), each a
 * link into the document view (`/knowledge?doc={id}`). The sources are already
 * circle-filtered at retrieval time, so this is pure display.
 *
 * Design rules (per /plan-design-review):
 *  - Empty/undefined → render NOTHING (not an empty container).
 *  - Cap at MAX_VISIBLE; overflow collapses behind a "+N more" toggle.
 *  - Tier is never color-only — TierBadge pairs the color with a symbol+label.
 *  - Chips are real links: keyboard-focusable, visible focus ring.
 */

const MAX_VISIBLE = 6;

export default function SourceChips({ sources }: { sources?: MessageSource[] }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);

  if (!sources || sources.length === 0) return null;

  const visible = expanded ? sources : sources.slice(0, MAX_VISIBLE);
  const hidden = sources.length - visible.length;

  return (
    <div className="mt-2 flex flex-wrap items-center gap-1.5" aria-label={t('chat.sources.label')}>
      <span className="text-xs text-gray-500 dark:text-gray-400 mr-0.5">
        {t('chat.sources.label')}:
      </span>
      {visible.map((src) => {
        const label = src.title || src.filename || String(src.document_id);
        return (
          <Link
            key={src.document_id}
            to={`/knowledge?doc=${encodeURIComponent(String(src.document_id))}`}
            title={label}
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-200 dark:hover:bg-gray-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500"
          >
            <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
            <span className="truncate max-w-[160px]">{label}</span>
            {typeof src.tier === 'number' && src.tier >= 0 && src.tier <= 4 && (
              <TierBadge tier={src.tier} className="text-[10px]" />
            )}
          </Link>
        );
      })}
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => setExpanded(true)}
          className="text-xs text-primary-600 dark:text-primary-400 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 rounded"
        >
          {t('chat.sources.more', { count: hidden })}
        </button>
      )}
    </div>
  );
}
