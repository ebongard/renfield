import { useState, useEffect, useMemo, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { Link, useLocation } from 'react-router';
import { CalendarClock, Download } from 'lucide-react';

import PageHeader from '../components/PageHeader';
import LensFrame from '../components/wissen/LensFrame';
import Alert from '../components/Alert';
import ObligationRow from '../components/ObligationRow';
import BestaetigenButton from '../components/obligations/BestaetigenButton';
import BestaetigtToast from '../components/obligations/BestaetigtToast';
import { useObligationsQuery, buildObligationsIcsUrl, type DocumentFact } from '../api/resources/brain';
import { useBestaetigt } from '../hooks/useBestaetigt';
import { urgencyGroup, URGENCY_ORDER, type UrgencyGroup } from '../utils/frist';

// Endpoint ceiling (D9). >200 obligations in a window is the rare case the
// "Mehr laden" offset hatch exists for; the agenda is a short scroll otherwise.
const PAGE_SIZE = 200;
const DAY_OPTIONS = [3, 7, 14, 30, 90];
const DEFAULT_RANGE = 30;
const HIGHLIGHT_MS = 1000;

function isoInDays(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

export default function ObligationsPage() {
  const { t } = useTranslation();
  const location = useLocation();
  const { isConfirmed, confirm, undo, reopen, pending, error: confirmError } = useBestaetigt();

  const [rangeDays, setRangeDays] = useState(DEFAULT_RANGE);
  const [offset, setOffset] = useState(0);
  // Accumulated across offset pages ("Mehr laden"); reset when the range changes.
  const [accumulated, setAccumulated] = useState<DocumentFact[]>([]);
  const [hasMore, setHasMore] = useState(false);

  const dueBefore = useMemo(() => isoInDays(rangeDays), [rangeDays]);
  const query = useObligationsQuery({ dueBefore, limit: PAGE_SIZE, offset });

  // Reset the accumulator when the time range changes.
  useEffect(() => {
    setOffset(0);
    setAccumulated([]);
    setHasMore(false);
  }, [dueBefore]);

  // Merge each fetched page into the accumulator (dedup by id — the
  // (obligation_date, id) order is stable so pages don't overlap, but a
  // refetch of page 0 could).
  useEffect(() => {
    if (!query.data) return;
    setAccumulated((prev) => {
      const byId = new Map(prev.map((f) => [f.id, f]));
      for (const f of query.data!) byId.set(f.id, f);
      return [...byId.values()];
    });
    setHasMore(query.data.length === PAGE_SIZE);
  }, [query.data]);

  // Recomputed each render (not frozen at mount) so a tab left open across
  // local midnight re-buckets obligations into the correct urgency group
  // instead of grouping against yesterday. Cost is trivial at agenda scale.
  const now = new Date();

  const groups = useMemo(() => {
    const out: Record<UrgencyGroup, DocumentFact[]> = { overdue: [], thisWeek: [], later: [] };
    for (const f of accumulated) {
      if (!f.obligation_date) continue;
      out[urgencyGroup(f.obligation_date, now)].push(f);
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accumulated, now.toDateString()]);

  // Inbound #frist-{id}: scroll to the row + brief highlight (T6).
  const [highlightId, setHighlightId] = useState<number | null>(null);
  const highlightHandled = useRef<string | null>(null);
  useEffect(() => {
    const hash = location.hash;
    if (!hash.startsWith('#frist-') || highlightHandled.current === hash) return;
    const id = Number(hash.slice('#frist-'.length));
    if (!Number.isInteger(id)) return;
    if (!accumulated.some((f) => f.id === id)) return; // wait until the row exists
    highlightHandled.current = hash;
    setHighlightId(id);
    requestAnimationFrame(() => {
      document.getElementById(`frist-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    });
    const timer = setTimeout(() => setHighlightId(null), HIGHLIGHT_MS);
    return () => clearTimeout(timer);
  }, [location.hash, accumulated]);

  const isEmpty = !query.isLoading && accumulated.length === 0;

  return (
    <LensFrame standaloneClassName="max-w-5xl mx-auto p-6 space-y-6">
      <PageHeader icon={CalendarClock} title={t('obligations.title')} subtitle={t('obligations.subtitle')} />

      {query.errorMessage && <Alert variant="error">{query.errorMessage}</Alert>}
      {confirmError && <Alert variant="error">{confirmError}</Alert>}

      <div className="flex items-center gap-3 flex-wrap">
        <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
          {t('obligations.timeRangeLabel')}:
        </span>
        <div className="flex gap-1 overflow-x-auto" role="group" aria-label={t('obligations.timeRangeLabel')}>
          {DAY_OPTIONS.map((d) => (
            <button
              key={d}
              type="button"
              onClick={() => setRangeDays(d)}
              className={`px-3 py-1 rounded-full text-xs font-medium transition-colors whitespace-nowrap ${
                rangeDays === d
                  ? 'bg-primary-600 text-white'
                  : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
              aria-pressed={rangeDays === d}
            >
              {d}d
            </button>
          ))}
        </div>
        <a
          href={buildObligationsIcsUrl({ dueBefore })}
          className="btn-secondary inline-flex items-center gap-1 px-3 py-1.5 text-sm ml-auto"
          data-testid="export-ics-link"
        >
          <Download className="w-4 h-4" aria-hidden="true" />
          {t('obligations.exportIcs')}
        </a>
      </div>

      {query.isLoading && accumulated.length === 0 ? (
        <div className="text-center py-12 text-gray-500 dark:text-gray-400">{t('common.loading')}</div>
      ) : isEmpty ? (
        <div className="empty-state">
          <p className="text-3xl font-display text-gray-900 dark:text-white mb-2">
            {t('obligations.emptyTitle')}
          </p>
          <p className="text-base text-gray-600 dark:text-gray-300 max-w-md">
            {t('obligations.emptyBody')}
          </p>
        </div>
      ) : (
        <div className="space-y-8">
          {URGENCY_ORDER.map((g) => {
            const items = groups[g];
            if (items.length === 0) return null; // skip empty groups — no "0" headers
            return (
              <section key={g}>
                <div className="flex items-baseline justify-between border-b border-gray-200 dark:border-gray-700 pb-1 mb-3">
                  <h2 className="text-2xl font-display font-medium text-gray-900 dark:text-white">
                    {t(`obligations.group.${g}`)}
                  </h2>
                  <span className="text-xs tabular-nums text-gray-500 dark:text-gray-400">
                    {items.length}
                  </span>
                </div>
                <ul className="space-y-3 animate-stagger">
                  {items.map((f) => {
                    const confirmed = isConfirmed(f.id, f.confirmed);
                    return (
                      <li
                        key={f.id}
                        id={`frist-${f.id}`}
                        className={`atom-row tier-ring-${f.circle_tier} animate-fade-slide-in flex-col sm:flex-row ${
                          confirmed ? 'atom-row--bestaetigt' : ''
                        } ${highlightId === f.id ? 'animate-gentle-pulse' : ''}`}
                      >
                        <div className="flex-1 min-w-0 space-y-1">
                          <ObligationRow fact={f} now={now} confirmed={confirmed} />
                          <Link
                            to={`/knowledge?doc=${f.document_id}#fakten`}
                            className="inline-block text-xs text-gray-500 dark:text-gray-400 hover:underline"
                          >
                            {t('obligations.openSource')}
                          </Link>
                        </div>
                        <div className="sm:ml-4 sm:flex-shrink-0 self-end sm:self-center">
                          <BestaetigenButton
                            confirmed={confirmed}
                            onConfirm={() => confirm(f.id)}
                            onReopen={() => reopen(f.id)}
                          />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              </section>
            );
          })}

          {hasMore && (
            <div className="text-center pt-2">
              <button
                type="button"
                className="btn-secondary"
                onClick={() => setOffset((o) => o + PAGE_SIZE)}
                disabled={query.isFetching}
              >
                {t('obligations.loadMore')}
              </button>
            </div>
          )}
        </div>
      )}

      {pending !== null && <BestaetigtToast onUndo={() => undo(pending)} />}
    </LensFrame>
  );
}
