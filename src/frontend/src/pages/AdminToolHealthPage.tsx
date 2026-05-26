import { useTranslation } from 'react-i18next';

import AdminListPageShell from '../components/AdminListPageShell';
import { useToolStatsQuery } from '../api/resources/toolHealth';

function formatTimestamp(value: string | null): string {
  if (!value) return '—';
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function HealthBar({ rate }: { rate: number }) {
  const pct = Math.max(0, Math.min(1, rate));
  const tone = pct >= 0.9
    ? 'bg-emerald-500'
    : pct >= 0.6
      ? 'bg-amber-500'
      : 'bg-rose-500';
  return (
    <div
      className="w-24 h-1.5 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden"
      role="progressbar"
      aria-valuenow={Math.round(pct * 100)}
      aria-valuemin={0}
      aria-valuemax={100}
    >
      <div className={`h-full ${tone}`} style={{ width: `${pct * 100}%` }} />
    </div>
  );
}

/**
 * Admin Tool-Health dashboard. Reads the Phase-3 outcome counters and
 * surfaces per-(user, tool) success rates so the household admin can spot
 * a tool that's silently broken for a specific user (permissions drift,
 * stale credentials) before it shows up as a sustained run of failed
 * agent turns.
 */
export default function AdminToolHealthPage() {
  const { t } = useTranslation();
  const statsQuery = useToolStatsQuery(null);
  const rows = statsQuery.data ?? [];

  return (
    <AdminListPageShell
      title={t('selfLearning.toolHealth.title')}
      description={t('selfLearning.toolHealth.description')}
      isLoading={statsQuery.isLoading}
      errorMessage={statsQuery.errorMessage}
      itemCount={rows.length}
      emptyState={t('selfLearning.toolHealth.empty')}
      testId="admin-tool-health-page"
    >
      <div className="overflow-x-auto rounded-lg border border-gray-200 dark:border-gray-700">
        <table className="min-w-full text-sm" data-testid="tool-health-table">
          <thead className="bg-gray-50 dark:bg-gray-800 text-gray-700 dark:text-gray-300">
            <tr>
              <th className="px-3 py-2 text-left">{t('selfLearning.toolHealth.cols.tool')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.toolHealth.cols.user')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.toolHealth.cols.success')}</th>
              <th className="px-3 py-2 text-right">{t('selfLearning.toolHealth.cols.failure')}</th>
              <th className="px-3 py-2">{t('selfLearning.toolHealth.cols.rate')}</th>
              <th className="px-3 py-2 text-left">{t('selfLearning.toolHealth.cols.lastUsed')}</th>
              <th className="px-3 py-2 text-left">{t('selfLearning.toolHealth.cols.lastFailure')}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                key={`${row.user_id ?? 'global'}-${row.tool_name}`}
                className="border-t border-gray-200 dark:border-gray-700"
                data-testid={`tool-row-${row.tool_name}`}
              >
                <td className="px-3 py-2 font-mono text-xs text-gray-800 dark:text-gray-200">
                  {row.tool_name}
                </td>
                <td className="px-3 py-2 text-right text-gray-700 dark:text-gray-300">
                  {row.user_id ?? '—'}
                </td>
                <td className="px-3 py-2 text-right text-emerald-700 dark:text-emerald-300 font-medium">
                  {row.success_count}
                </td>
                <td className="px-3 py-2 text-right text-rose-700 dark:text-rose-300 font-medium">
                  {row.failure_count}
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center gap-2">
                    <HealthBar rate={row.success_rate} />
                    <span className="text-xs text-gray-600 dark:text-gray-400">
                      {Math.round(row.success_rate * 100)}%
                    </span>
                  </div>
                </td>
                <td className="px-3 py-2 text-xs text-gray-600 dark:text-gray-400">
                  {formatTimestamp(row.last_used_at)}
                </td>
                <td className="px-3 py-2 text-xs text-gray-600 dark:text-gray-400 max-w-xs truncate">
                  {row.last_failure_summary ?? '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </AdminListPageShell>
  );
}
