import { useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ExternalLink, FileText } from 'lucide-react';
import TierBadge from '../TierBadge';
import type { MessageSource } from '../../types/chat';

/**
 * Provenance "source chips" under an assistant turn.
 *
 * Shows which documents the answer drew on, each chip OPENING its document:
 *  - a knowledge-base document (filename + access-tier) links into the document
 *    view (`/knowledge?doc={id}`, which loads the document by id); when the
 *    server also sent a Paperless link, a second segment opens the filed
 *    original in a new tab;
 *  - a Paperless document (`kind: 'paperless'`, found by the agent's Paperless
 *    search) opens in Paperless in a new tab -- or is a plain, non-interactive
 *    chip when the server sent no link (no browser-reachable Paperless address
 *    is configured).
 *
 * 🛑 Every link target is built by the SERVER (`paperless_url`) or from a
 * numeric id here. Nothing the model wrote becomes an `href`, and a
 * `paperless_url` that is not plain http(s) is treated as absent.
 *
 * Design rules (per /plan-design-review):
 *  - Empty/undefined → render NOTHING (not an empty container).
 *  - Cap at MAX_VISIBLE; overflow collapses behind a "+N more" toggle.
 *  - Tier is never color-only — TierBadge pairs the color with a symbol+label.
 *  - Chips are real links: keyboard-focusable, visible focus ring. A chip that
 *    opens nothing is NOT focusable (no tabbing through dead controls).
 */

const MAX_VISIBLE = 6;

const CHIP =
  'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-200 dark:hover:bg-gray-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500';

// The elements this feature adds follow DESIGN.md §Accessibility (turquoise
// focus ring); the chip above keeps the ring it shipped with.
const EXTERNAL_CHIP =
  'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-200 dark:hover:bg-gray-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500';

const PLAIN_CHIP =
  'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 dark:bg-gray-600 dark:text-gray-200';

const EXTERNAL_SEGMENT =
  'inline-flex items-center px-1.5 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 hover:bg-gray-300 dark:bg-gray-600 dark:text-gray-200 dark:hover:bg-gray-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent-500';

/** The server-built Paperless link, or null when it is absent or not http(s). */
function externalUrl(source: MessageSource): string | null {
  const url = source.paperless_url;
  return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : null;
}

function sourceKey(source: MessageSource): string {
  return source.kind === 'paperless'
    ? `paperless-${source.paperless_document_id}`
    : `kb-${source.document_id}`;
}

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
        const url = externalUrl(src);

        if (src.kind === 'paperless') {
          const label =
            src.title || t('chat.sources.paperlessUntitled', { id: src.paperless_document_id });
          if (!url) {
            return (
              <span key={sourceKey(src)} title={t('chat.sources.noLink')} className={PLAIN_CHIP}>
                <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
                <span className="truncate max-w-[160px]">{label}</span>
                <span className="sr-only">{t('chat.sources.noLink')}</span>
              </span>
            );
          }
          return (
            <a
              key={sourceKey(src)}
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              title={label}
              aria-label={t('chat.sources.openInPaperless', { title: label })}
              className={EXTERNAL_CHIP}
            >
              <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
              <span className="truncate max-w-[160px]">{label}</span>
              <ExternalLink className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
            </a>
          );
        }

        const label = src.title || src.filename || String(src.document_id);
        const chip = (
          <Link
            key={sourceKey(src)}
            to={`/knowledge?doc=${encodeURIComponent(String(src.document_id))}`}
            title={label}
            className={CHIP}
          >
            <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
            <span className="truncate max-w-[160px]">{label}</span>
            {typeof src.tier === 'number' && src.tier >= 0 && src.tier <= 4 && (
              <TierBadge tier={src.tier} className="text-[10px]" />
            )}
          </Link>
        );
        if (!url) return chip;
        return (
          <span key={sourceKey(src)} className="inline-flex items-center gap-0.5">
            {chip}
            <a
              href={url}
              target="_blank"
              rel="noopener noreferrer"
              title={t('chat.sources.openInPaperless', { title: label })}
              aria-label={t('chat.sources.openInPaperless', { title: label })}
              className={EXTERNAL_SEGMENT}
            >
              <ExternalLink className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
            </a>
          </span>
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
