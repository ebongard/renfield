import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { formatTime } from '../utils/datetime';
import { RefreshCw, AlertCircle } from 'lucide-react';
import {
  useRoutingTracesQuery,
  useRoutingStatsQuery,
  type Layer,
} from '../api/resources/routing';

const LAYER_COLORS: Record<Layer, string> = {
  role_hint: 'bg-teal-100 text-teal-800 dark:bg-teal-900/30 dark:text-teal-300',
  entity_id: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  continuity: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  semantic: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  llm: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
  fallback: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  unknown: 'bg-gray-100 text-gray-500 dark:bg-gray-800 dark:text-gray-400',
};

function layerClass(layer: string): string {
  return LAYER_COLORS[layer as Layer] ?? LAYER_COLORS.unknown;
}

export default function RoutingDashboardPage() {
  const { t } = useTranslation();
  const [domainFilter, setDomainFilter] = useState('');

  const tracesQuery = useRoutingTracesQuery(domainFilter);
  const statsQuery = useRoutingStatsQuery();

  const traces = tracesQuery.data?.traces ?? [];
  const stats = statsQuery.data ?? null;
  const loading = tracesQuery.isFetching || statsQuery.isFetching;
  const failed = tracesQuery.isError || statsQuery.isError;
  const meta = tracesQuery.data ?? statsQuery.data;

  const refresh = () => {
    tracesQuery.refetch();
    statsQuery.refetch();
  };

  return (
    <div className="max-w-6xl mx-auto p-4 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            {t('routing.title')}
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            {t('routing.subtitle')}
            {meta && ` ${t('routing.retention', { days: meta.retention_days })}`}
          </p>
        </div>
        <button
          onClick={refresh}
          disabled={loading}
          className="btn-secondary flex items-center gap-2"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          {t('common.refresh')}
        </button>
      </div>

      {failed && (
        <div
          role="alert"
          className="flex items-center gap-2 rounded-md bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-800 p-3 text-sm text-red-700 dark:text-red-300"
        >
          <AlertCircle className="w-4 h-4 flex-shrink-0" aria-hidden="true" />
          {t('routing.loadError')}
        </div>
      )}

      {meta && !meta.enabled && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-md bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 p-3 text-sm text-amber-800 dark:text-amber-200"
        >
          <AlertCircle className="w-4 h-4 flex-shrink-0 mt-0.5" aria-hidden="true" />
          {t('routing.disabled')}
        </div>
      )}

      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {Object.entries(stats.by_domain).map(([domain, count]) => (
            <div
              key={domain}
              onClick={() => setDomainFilter(domainFilter === domain ? '' : domain)}
              className={`card cursor-pointer transition-all ${
                domainFilter === domain ? 'ring-2 ring-blue-500' : ''
              }`}
            >
              <div className="text-2xl font-bold text-gray-900 dark:text-gray-100">{count}</div>
              <div className="text-sm text-gray-500 dark:text-gray-400 capitalize">{domain}</div>
            </div>
          ))}
        </div>
      )}

      {stats && Object.keys(stats.by_layer).length > 0 && (
        <div className="card">
          <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">{t('routing.classificationLayers')}</h2>
          <div className="flex flex-wrap gap-2">
            {Object.entries(stats.by_layer).map(([layer, count]) => (
              <span
                key={layer}
                className={`px-3 py-1 rounded-full text-xs font-medium ${layerClass(layer)}`}
              >
                {layer}: {count}
              </span>
            ))}
          </div>
        </div>
      )}

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
              <th className="pb-2 pr-4">{t('routing.col.time')}</th>
              <th className="pb-2 pr-4">{t('routing.col.message')}</th>
              <th className="pb-2 pr-4">{t('routing.col.domain')}</th>
              <th className="pb-2 pr-4">{t('routing.col.layer')}</th>
              <th className="pb-2 pr-4">{t('routing.col.confidence')}</th>
              <th className="pb-2">{t('routing.col.entities')}</th>
            </tr>
          </thead>
          <tbody>
            {traces.map((trace) => (
              <tr
                key={trace.id}
                className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50"
              >
                <td className="py-2 pr-4 text-xs text-gray-400 whitespace-nowrap">
                  {formatTime(trace.created_at)}
                </td>
                <td
                  className="py-2 pr-4 max-w-xs truncate text-gray-700 dark:text-gray-300"
                  title={trace.message}
                >
                  {trace.message}
                </td>
                <td className="py-2 pr-4">
                  <span className="px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 capitalize">
                    {trace.domain}
                    {trace.sub_intent && `/${trace.sub_intent}`}
                  </span>
                </td>
                <td className="py-2 pr-4">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${layerClass(trace.layer)}`}>
                    {trace.layer}
                  </span>
                </td>
                <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">
                  {trace.confidence != null ? trace.confidence.toFixed(2) : '-'}
                </td>
                <td className="py-2 text-xs text-gray-500 dark:text-gray-400">
                  {trace.entity_matches.map(e => e.id).join(', ') || '-'}
                </td>
              </tr>
            ))}
            {traces.length === 0 && (
              <tr>
                <td colSpan={6} className="py-8 text-center text-gray-400">
                  {loading ? t('common.loading') : t('routing.empty')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
