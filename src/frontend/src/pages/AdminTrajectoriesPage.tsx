import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Download, Flag, Search } from 'lucide-react';

import AdminListPageShell from '../components/AdminListPageShell';
import StepTimeline from '../components/StepTimeline';
import {
  buildTrajectoryExportUrl,
  useFlagTrajectory,
  useTrajectoriesQuery,
  useTrajectoryQuery,
  useTrajectoryStatsQuery,
  type TrajectoryOutcome,
} from '../api/resources/trajectories';

const OUTCOME_FILTERS: (TrajectoryOutcome | 'all')[] = ['all', 'success', 'tool_fail', 'abort', 'user_corrected'];

function formatTimestamp(value: string | null): string {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

/**
 * Admin Trajectories inspector. Lists captured agent turns, supports
 * outcome / date filtering, lets the admin flag a row for retention, and
 * surfaces the full step trace in a side panel. The "Download JSONL"
 * button mirrors the current filter set to the export endpoint.
 */
export default function AdminTrajectoriesPage() {
  const { t } = useTranslation();
  const [outcomeFilter, setOutcomeFilter] = useState<TrajectoryOutcome | 'all'>('all');
  const [sinceDays, setSinceDays] = useState<number>(7);
  const [flaggedOnly, setFlaggedOnly] = useState(false);
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const filters = useMemo(() => ({
    outcome: outcomeFilter === 'all' ? undefined : outcomeFilter,
    since_days: sinceDays,
    flagged_only: flaggedOnly,
    limit: 100,
  }), [outcomeFilter, sinceDays, flaggedOnly]);

  const listQuery = useTrajectoriesQuery(filters);
  const statsQuery = useTrajectoryStatsQuery();
  const detailQuery = useTrajectoryQuery(selectedId);
  const flag = useFlagTrajectory();

  const rows = listQuery.data ?? [];
  const stats = statsQuery.data;
  const exportUrl = buildTrajectoryExportUrl({
    outcome: outcomeFilter === 'all' ? undefined : outcomeFilter,
    since_days: sinceDays,
    flagged_only: flaggedOnly,
  });

  const toolbar = (
    <>
      <span className="text-sm text-gray-600 dark:text-gray-400">
        {t('selfLearning.trajectories.outcome')}:
      </span>
      {OUTCOME_FILTERS.map((o) => (
        <button
          key={o}
          type="button"
          className={`px-2.5 py-1 rounded text-xs font-medium ${
            outcomeFilter === o
              ? 'bg-primary-600 text-white'
              : 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300'
          }`}
          onClick={() => setOutcomeFilter(o)}
          aria-pressed={outcomeFilter === o}
        >
          {o === 'all'
            ? t('selfLearning.trajectories.outcomeAll')
            : t(`selfLearning.trajectories.outcomes.${o}`)}
        </button>
      ))}
      <label className="ml-2 inline-flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
        {t('selfLearning.trajectories.sinceDays')}:
        <input
          type="number"
          className="input w-16 text-xs py-1"
          value={sinceDays}
          min={1}
          max={3650}
          onChange={(e) => setSinceDays(Math.max(1, Number(e.target.value) || 1))}
        />
      </label>
      <label className="inline-flex items-center gap-1 text-xs text-gray-600 dark:text-gray-400">
        <input
          type="checkbox"
          checked={flaggedOnly}
          onChange={(e) => setFlaggedOnly(e.target.checked)}
        />
        {t('selfLearning.trajectories.flaggedOnly')}
      </label>
      <a
        href={exportUrl}
        className="btn-secondary inline-flex items-center gap-1 px-3 py-1.5 text-sm ml-auto"
        data-testid="export-jsonl-link"
      >
        <Download className="w-4 h-4" aria-hidden="true" />
        {t('selfLearning.trajectories.exportJsonl')}
      </a>
    </>
  );

  return (
    <AdminListPageShell
      title={t('selfLearning.trajectories.title')}
      description={t('selfLearning.trajectories.description')}
      toolbar={toolbar}
      isLoading={listQuery.isLoading}
      errorMessage={listQuery.errorMessage}
      itemCount={rows.length}
      emptyState={t('selfLearning.trajectories.empty')}
      testId="admin-trajectories-page"
    >
      {stats && (
        <dl className="grid grid-cols-2 md:grid-cols-4 gap-3" data-testid="stats-panel">
          <div className="card p-3">
            <dt className="text-xs text-gray-500 dark:text-gray-400">
              {t('selfLearning.trajectories.statsTotal')}
            </dt>
            <dd className="text-xl font-semibold text-gray-900 dark:text-white">{stats.total}</dd>
          </div>
          <div className="card p-3">
            <dt className="text-xs text-gray-500 dark:text-gray-400">
              {t('selfLearning.trajectories.stats7d')}
            </dt>
            <dd className="text-xl font-semibold text-gray-900 dark:text-white">{stats.last_7d}</dd>
          </div>
          <div className="card p-3">
            <dt className="text-xs text-gray-500 dark:text-gray-400">
              {t('selfLearning.trajectories.statsFlagged')}
            </dt>
            <dd className="text-xl font-semibold text-gray-900 dark:text-white">{stats.flagged_total}</dd>
          </div>
          <div className="card p-3">
            <dt className="text-xs text-gray-500 dark:text-gray-400">
              {t('selfLearning.trajectories.statsRetention')}
            </dt>
            <dd className="text-xl font-semibold text-gray-900 dark:text-white">
              {stats.retention_days}d
            </dd>
          </div>
        </dl>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="space-y-2" data-testid="trajectories-list">
          {rows.map((row) => (
            <article
              key={row.id}
              className={`card p-3 cursor-pointer hover:border-primary-400 ${
                selectedId === row.id ? 'ring-2 ring-primary-500' : ''
              }`}
              onClick={() => setSelectedId(row.id)}
              role="button"
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === 'Enter') setSelectedId(row.id);
              }}
              data-testid={`trajectory-row-${row.id}`}
            >
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-mono text-gray-500 dark:text-gray-400">
                  #{row.id}
                </span>
                <span className={`text-xs font-medium ${
                  row.outcome === 'success'
                    ? 'text-emerald-700 dark:text-emerald-300'
                    : row.outcome === 'tool_fail'
                      ? 'text-amber-700 dark:text-amber-300'
                      : 'text-rose-700 dark:text-rose-300'
                }`}>
                  {t(`selfLearning.trajectories.outcomes.${row.outcome}`)}
                </span>
                <button
                  type="button"
                  className={`p-1 rounded ${row.flagged_for_retention ? 'text-accent-600' : 'text-gray-400'}`}
                  onClick={(e) => {
                    e.stopPropagation();
                    flag.mutate({ id: row.id, flagged: !row.flagged_for_retention });
                  }}
                  aria-label={t('selfLearning.trajectories.flagToggle')}
                  data-testid={`flag-button-${row.id}`}
                >
                  <Flag className="w-3.5 h-3.5" aria-hidden="true" />
                </button>
              </div>
              <div className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {formatTimestamp(row.created_at)} · {row.tool_count} tools
              </div>
            </article>
          ))}
        </div>

        <aside className="card p-4 max-h-[70vh] overflow-y-auto" data-testid="trajectory-detail">
          {selectedId == null ? (
            <p className="text-sm text-gray-500 dark:text-gray-400 inline-flex items-center gap-2">
              <Search className="w-4 h-4" aria-hidden="true" />
              {t('selfLearning.trajectories.selectPrompt')}
            </p>
          ) : detailQuery.isLoading ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">...</p>
          ) : detailQuery.data ? (
            <StepTimeline payload={detailQuery.data.redacted_payload ?? detailQuery.data.raw_payload} />
          ) : (
            <p className="text-sm text-rose-600 dark:text-rose-300">
              {detailQuery.errorMessage}
            </p>
          )}
        </aside>
      </div>
    </AdminListPageShell>
  );
}
