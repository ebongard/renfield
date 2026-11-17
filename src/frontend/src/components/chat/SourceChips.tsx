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
 *  - DESIGN.md §Accessibility: every interactive element of the row has a
 *    44x44 px touch target and the one turquoise focus ring (see below).
 */

const MAX_VISIBLE = 6;

// Touch target (DESIGN.md: >= 44x44 px on every interactive element). The chip
// keeps its size; an invisible ::after, centred on it, is at least 44 px in
// both directions and at least as large as the chip. A click on the
// pseudo-element is a click on the chip.
//
// `after:-z-10` + `isolate` on the row: the hit areas lie BEHIND the visible
// chips of the row, so where a hit area reaches over a neighbour (the 24 px
// wide Paperless segment, a short "+N") the neighbour's visible body wins and
// only empty space is claimed. `isolate` keeps the negative z-index inside
// the row -- without it the area would fall behind the message bubble.
// For the Paperless segment the 44x44 px is therefore a BOUNDING BOX: its
// knowledge-base chip covers part of it, the solid target measured in a
// browser is about 34x44 px.
const HIT_AREA =
  'relative after:absolute after:-z-10 after:left-1/2 after:top-1/2 after:h-full after:w-full after:min-h-[44px] after:min-w-[44px] after:-translate-x-1/2 after:-translate-y-1/2';

// Focus ring (DESIGN.md: turquoise accent-500, :focus-visible, 2px at 2px
// offset) -- as an OUTLINE, like the button classes in index.css, so it needs
// no offset colour that would have to match the bubble in both themes.
// 🛑 No `outline-none` beside it: in Tailwind v4 that zeroes the outline style
// this ring is drawn with. The browser shows an outline on :focus-visible only.
const FOCUS_RING =
  'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent-500';

const CHIP_LOOK =
  'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 dark:bg-gray-600 dark:text-gray-200';

const CHIP_HOVER = 'hover:bg-gray-300 dark:hover:bg-gray-500';

const CHIP = `${CHIP_LOOK} ${CHIP_HOVER} ${HIT_AREA} ${FOCUS_RING}`;

// A chip that opens nothing: no hover, no hit area, no ring.
const PLAIN_CHIP = CHIP_LOOK;

const EXTERNAL_SEGMENT =
  `inline-flex items-center px-1.5 py-0.5 rounded-full text-xs bg-gray-200 text-gray-700 dark:bg-gray-600 dark:text-gray-200 ${CHIP_HOVER} ${HIT_AREA} ${FOCUS_RING}`;

// min-h-5: a bare text button is 16 px high, its hit area would reach 14 px
// beyond it -- 2 px more than the row provides when "+N" stands alone on the
// last line. At 20 px the reach is the 12 px the row has.
const MORE_BUTTON =
  `min-h-5 text-xs text-primary-600 dark:text-primary-400 hover:underline rounded ${HIT_AREA} ${FOCUS_RING}`;

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
    // gap-y-6 (24 px) between wrapped lines: a line is at least 20 px high
    // (measured: a chip with tier badge 23.3 px; the Paperless segment and a
    // bare "+N" 16 px, the latter raised to 20 px by min-h-5), so the lines
    // sit at least 44 px apart and their hit areas do not overlap. py-1 on
    // top of mt-2 (and of the next block's own margin) gives 12 px beyond the
    // first and last line -- the reach of the hit area of a 20 px element.
    <div
      className="isolate mt-2 py-1 flex flex-wrap items-center gap-x-1.5 gap-y-6"
      aria-label={t('chat.sources.label')}
    >
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
              className={CHIP}
            >
              <FileText className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
              <span className="truncate max-w-[160px]">{label}</span>
              <ExternalLink className="w-3 h-3 flex-shrink-0" aria-hidden="true" />
            </a>
          );
        }

        const label =
          src.title || src.filename || t('chat.sources.untitled', { id: src.document_id });
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
          className={MORE_BUTTON}
        >
          {t('chat.sources.more', { count: hidden })}
        </button>
      )}
    </div>
  );
}
