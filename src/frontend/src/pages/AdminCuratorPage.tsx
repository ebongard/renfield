import { useTranslation } from 'react-i18next';
import { Play, AlertTriangle, CheckCircle2, Loader2 } from 'lucide-react';

import AdminListPageShell from '../components/AdminListPageShell';
import { useCuratorRunsQuery, useRunCurator, type CuratorRun } from '../api/resources/curator';

function formatTimestamp(value: string | null): string {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function StatusIcon({ status }: { status: CuratorRun['status'] }) {
  switch (status) {
    case 'success':
      return <CheckCircle2 className="w-4 h-4 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />;
    case 'partial':
      return <AlertTriangle className="w-4 h-4 text-amber-600 dark:text-amber-400" aria-hidden="true" />;
    case 'failed':
      return <AlertTriangle className="w-4 h-4 text-rose-600 dark:text-rose-400" aria-hidden="true" />;
    case 'running':
    default:
      return <Loader2 className="w-4 h-4 text-gray-400 animate-spin" aria-hidden="true" />;
  }
}

/**
 * Admin Curator runbook. Shows the history of SkillCuratorRun audit rows
 * with their counters (duplicates found / merged / stale archived) and
 * exposes a "Run Now" button that triggers a manual curator pass. The
 * row history is the single source of truth for the curator's actual
 * impact on the corpus — operators no longer have to grep log files to
 * find out whether last night's scheduled run actually merged anything.
 */
export default function AdminCuratorPage() {
  const { t } = useTranslation();
  const runsQuery = useCuratorRunsQuery();
  const runNow = useRunCurator();

  const runs = runsQuery.data ?? [];

  const toolbar = (
    <button
      type="button"
      className="btn-primary inline-flex items-center gap-2 px-4 py-2"
      onClick={() => runNow.mutate(undefined as unknown as void)}
      disabled={runNow.isPending}
      data-testid="run-curator-button"
    >
      <Play className="w-4 h-4" aria-hidden="true" />
      {runNow.isPending
        ? t('selfLearning.curator.running')
        : t('selfLearning.curator.runNow')}
    </button>
  );

  return (
    <AdminListPageShell
      title={t('selfLearning.curator.title')}
      description={t('selfLearning.curator.description')}
      toolbar={toolbar}
      isLoading={runsQuery.isLoading}
      errorMessage={runsQuery.errorMessage || runNow.errorMessage}
      itemCount={runs.length}
      emptyState={t('selfLearning.curator.empty')}
      testId="admin-curator-page"
    >
      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table className="min-w-full text-sm" data-testid="curator-runs-table">
          <thead className="bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300">
            <tr>
              <th className="px-3 py-2 text-left">{t('selfLearning.curator.cols.status')}</th>
              <th className="px-3 py-2 text-left">{t('selfLearning.curator.cols.startedAt')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.curator.cols.duration')}</th>
              <th className="px-3 py-2 text-left">{t('selfLearning.curator.cols.type')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.curator.cols.examined')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.curator.cols.dupFound')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.curator.cols.dupMerged')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.curator.cols.staleArchived')}</th>
              <th className="px-3 py-2 text-left">{t('selfLearning.curator.cols.error')}</th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr
                key={run.id}
                className="border-t border-gray-200 dark:border-gray-700"
                data-testid={`curator-run-${run.id}`}
              >
                <td className="px-3 py-2">
                  <span className="inline-flex items-center gap-1.5">
                    <StatusIcon status={run.status} />
                    <span className="text-xs">{t(`selfLearning.curator.status.${run.status}`)}</span>
                  </span>
                </td>
                <td className="px-3 py-2 text-xs text-gray-600 dark:text-gray-400">
                  {formatTimestamp(run.started_at)}
                </td>
                <td className="px-3 py-2 text-right text-xs text-gray-600 dark:text-gray-400">
                  {run.duration_seconds != null ? `${run.duration_seconds.toFixed(2)}s` : '—'}
                </td>
                <td className="px-3 py-2 text-xs text-gray-700 dark:text-gray-300">
                  {t(`selfLearning.curator.runType.${run.run_type}`)}
                </td>
                <td className="px-3 py-2 text-right">{run.skills_examined}</td>
                <td className="px-3 py-2 text-right">{run.duplicate_pairs_found}</td>
                <td className="px-3 py-2 text-right text-emerald-700 dark:text-emerald-300 font-medium">
                  {run.duplicate_pairs_merged}
                </td>
                <td className="px-3 py-2 text-right text-amber-700 dark:text-amber-300 font-medium">
                  {run.stale_skills_archived}
                </td>
                <td className="px-3 py-2 text-xs text-rose-700 dark:text-rose-300 max-w-xs truncate">
                  {run.error_message ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </AdminListPageShell>
  );
}
