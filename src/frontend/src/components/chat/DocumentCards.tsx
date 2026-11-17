import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router';
import { useTranslation } from 'react-i18next';
import { ExternalLink, FileText } from 'lucide-react';
import TierBadge from '../TierBadge';
import { formatDate } from '../../utils/datetime';
import { focusRevealedItem } from './revealFocus';
import type { MessageSource } from '../../types/chat';

/**
 * Document cards under an assistant turn: one card per document the answer
 * drew on, with what one needs to recognise it (title, access tier, date,
 * document type, correspondent, pages) and the buttons that open it.
 *
 * Rendered instead of the chip row (`SourceChips`) where the backend flag
 * `CHAT_DOCUMENT_LINKS_ENABLED` is on; the flag reaches the page as
 * `chat_document_links_enabled` in `/api/config/features`.
 *
 * 🛑 A card is built from the turn's `sources` -- a list the SERVER assembled
 * from tool results and its own document lookup -- and from nothing the model
 * wrote. Every link target is built by the server (`paperless_url`) or here
 * from a numeric id; a `paperless_url` that is not plain http(s) is treated as
 * absent. All text goes through React's escaping.
 *
 *  - Knowledge-base document: tier stripe + badge, "Öffnen" into the document
 *    view (`/knowledge?doc={id}`); when it is also filed in Paperless, a second
 *    button opens the original there. One document, one card.
 *  - Paperless document (`kind: 'paperless'`): no tier, the neutral row, one
 *    button -- or none when the server sent no link. A card without a button
 *    is plain content: nothing in it takes the focus.
 *
 * Design: DESIGN.md list-row pattern (`.atom-row` + `tier-ring-{tier}`),
 * `TierBadge` (colour is never the only signal), `.btn-secondary` with the
 * focus ring that class defines. The buttons are 44 px high through
 * `min-h-11` -- a real height, no invisible hit area. The class alone does not
 * reach DESIGN.md's 44 px touch target: measured 38 px with `text-sm`
 * (42 px at its default text size).
 */

const MAX_VISIBLE = 3;

// Complete class names: Tailwind only emits what it can read in the source.
const TIER_RING: Record<number, string> = {
  0: 'tier-ring-0',
  1: 'tier-ring-1',
  2: 'tier-ring-2',
  3: 'tier-ring-3',
  4: 'tier-ring-4',
};

// `.atom-row` is a horizontal flex row; a card stacks its three lines. The
// children take the full width themselves (`w-full`), `min-w-0` lets a long
// title wrap instead of widening the bubble.
const CARD = 'atom-row flex-col gap-2 p-3 min-w-0';

const BUTTON = 'btn-secondary min-h-11 max-w-full gap-1.5 px-3 text-sm';

const ISO_DAY = /^\d{4}-\d{2}-\d{2}$/;

/** The server-built Paperless link, or null when it is absent or not http(s). */
function externalUrl(source: MessageSource): string | null {
  const url = source.paperless_url;
  return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : null;
}

/** The in-app target of a knowledge-base document, or null when its id is not
 *  a positive integer. */
function documentViewPath(source: MessageSource): string | null {
  if (source.kind === 'paperless') return null;
  const raw = source.document_id;
  const id = typeof raw === 'number' ? raw : /^[1-9]\d*$/.test(raw) ? Number(raw) : NaN;
  return Number.isSafeInteger(id) && id > 0 ? `/knowledge?doc=${id}` : null;
}

function sourceKey(source: MessageSource): string {
  return source.kind === 'paperless'
    ? `paperless-${source.paperless_document_id}`
    : `kb-${source.document_id}`;
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : null;
}

function validTier(tier: unknown): number | null {
  return typeof tier === 'number' && Number.isInteger(tier) && tier >= 0 && tier <= 4 ? tier : null;
}

function DocumentCard({ source }: { source: MessageSource }) {
  const { t, i18n } = useTranslation();

  const isPaperless = source.kind === 'paperless';
  const title = isPaperless
    ? source.title || t('chat.sources.paperlessUntitled', { id: source.paperless_document_id })
    : source.title || source.filename || t('chat.sources.untitled', { id: source.document_id });
  const tier = isPaperless ? null : validTier(source.tier);

  const day = isPaperless ? source.created : source.document_date;
  const pages = source.page_count;
  const meta = [
    typeof day === 'string' && ISO_DAY.test(day)
      ? formatDate(day, i18n.language, { day: '2-digit', month: '2-digit', year: 'numeric' })
      : null,
    isPaperless ? text(source.document_type) : null,
    isPaperless ? text(source.correspondent) : null,
    typeof pages === 'number' && Number.isInteger(pages) && pages > 0
      ? t('chat.sources.pages', { count: pages })
      : null,
  ].filter((part): part is string => Boolean(part));

  const viewPath = documentViewPath(source);
  const paperlessUrl = externalUrl(source);

  return (
    <li className={tier === null ? CARD : `${CARD} ${TIER_RING[tier]}`}>
      {/* In a narrow bubble the badge moves under the title (`flex-wrap` +
          the title's 10rem basis) instead of squeezing it into a column that
          breaks every word. */}
      <div className="flex w-full min-w-0 flex-wrap items-start gap-x-2 gap-y-1">
        <span className="flex min-w-0 flex-1 basis-40 items-start gap-2">
          <FileText
            className="mt-1 h-4 w-4 flex-shrink-0 text-gray-500 dark:text-gray-400"
            aria-hidden="true"
          />
          <span className="min-w-0 flex-1 break-words font-medium text-gray-900 dark:text-gray-100">
            {title}
          </span>
        </span>
        {tier !== null && <TierBadge tier={tier} className="flex-shrink-0" />}
      </div>

      {/* Each part in its own <bdi>: a right-to-left name from a third system
          cannot reorder the separators or its neighbours. */}
      {meta.length > 0 && (
        <p data-card-meta className="w-full break-words text-xs text-gray-600 dark:text-gray-300">
          {meta.map((part, i) => (
            <span key={i}>
              {i > 0 && ' · '}
              <bdi>{part}</bdi>
            </span>
          ))}
        </p>
      )}

      {(viewPath || paperlessUrl) && (
        <div className="flex w-full min-w-0 flex-wrap gap-2">
          {viewPath && (
            <Link
              to={viewPath}
              className={BUTTON}
              aria-label={t('chat.sources.openDocument', { title })}
            >
              {t('chat.sources.open')}
            </Link>
          )}
          {paperlessUrl && (
            <a
              href={paperlessUrl}
              target="_blank"
              rel="noopener noreferrer"
              className={BUTTON}
              aria-label={t('chat.sources.openInPaperless', { title })}
            >
              <span className="min-w-0">{t('chat.sources.inPaperless')}</span>
              <ExternalLink className="h-4 w-4 flex-shrink-0" aria-hidden="true" />
            </a>
          )}
        </div>
      )}

      {!viewPath && !paperlessUrl && isPaperless && (
        <p className="w-full text-xs text-gray-600 dark:text-gray-300">
          {t('chat.sources.noLink')}
        </p>
      )}
    </li>
  );
}

export default function DocumentCards({ sources }: { sources?: MessageSource[] }) {
  const { t } = useTranslation();
  const [expanded, setExpanded] = useState(false);
  const listRef = useRef<HTMLUListElement>(null);
  const focusOnReveal = useRef(false);

  // "+N" removes itself: hand the focus to the first card it revealed.
  useEffect(() => {
    if (!expanded || !focusOnReveal.current) return;
    focusOnReveal.current = false;
    focusRevealedItem(listRef.current?.children[MAX_VISIBLE]);
  }, [expanded]);

  if (!sources || sources.length === 0) return null;

  const visible = expanded ? sources : sources.slice(0, MAX_VISIBLE);
  const hidden = sources.length - visible.length;

  return (
    <div className="mt-2 flex min-w-0 flex-col items-start gap-2">
      <ul ref={listRef} className="flex w-full min-w-0 flex-col gap-2" aria-label={t('chat.sources.label')}>
        {visible.map((source) => (
          <DocumentCard key={sourceKey(source)} source={source} />
        ))}
      </ul>
      {hidden > 0 && (
        <button
          type="button"
          onClick={() => {
            focusOnReveal.current = true;
            setExpanded(true);
          }}
          className="btn-ghost min-h-11 px-3 text-sm"
        >
          {t('chat.sources.more', { count: hidden })}
        </button>
      )}
    </div>
  );
}
