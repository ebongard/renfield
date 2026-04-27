import { useState, useEffect, useCallback } from 'react';
import { RefreshCw, CheckCircle2, XCircle } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import apiClient from '../utils/axios';

type Layer = 'entity_id' | 'continuity' | 'semantic' | 'mlp' | 'llm';

const LAYER_COLORS: Record<Layer, string> = {
  entity_id: 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300',
  continuity: 'bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300',
  semantic: 'bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300',
  mlp: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300',
  llm: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
};

interface EntityMatch {
  id: string;
}

interface RoutingTrace {
  id: string | number;
  created_at?: string;
  message: string;
  domain: string;
  layer?: Layer;
  confidence?: number | null;
  entity_matches?: EntityMatch[];
  user_feedback?: 1 | -1 | null;
}

interface RoutingStats {
  by_domain?: Record<string, number>;
  by_layer?: Record<string, number>;
}

export default function RoutingDashboardPage() {
  const { getAccessToken } = useAuth();
  const [traces, setTraces] = useState<RoutingTrace[]>([]);
  const [stats, setStats] = useState<RoutingStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [domainFilter, setDomainFilter] = useState('');

  const fetchData = useCallback(async () => {
    setLoading(true);
    try {
      const token = getAccessToken();
      const headers = { Authorization: `Bearer ${token}` };
      const params = domainFilter ? { domain: domainFilter } : {};

      const [tracesRes, statsRes] = await Promise.all([
        apiClient.get<{ traces: RoutingTrace[] }>('/api/admin/routing-traces', { headers, params }),
        apiClient.get<RoutingStats>('/api/admin/routing-stats', { headers }),
      ]);

      setTraces(tracesRes.data.traces || []);
      setStats(statsRes.data || null);
    } catch (err) {
      console.error('Failed to fetch routing data:', err);
    } finally {
      setLoading(false);
    }
  }, [getAccessToken, domainFilter]);

  useEffect(() => { fetchData(); }, [fetchData]);

  return (
    <div className="max-w-6xl mx-auto p-4 space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
            Routing Dashboard
          </h1>
          <p className="text-sm text-gray-500 dark:text-gray-400">
            Recent routing decisions across all channels
          </p>
        </div>
        <button
          onClick={fetchData}
          disabled={loading}
          className="btn-secondary flex items-center gap-2"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Stats Summary */}
      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {Object.entries(stats.by_domain || {}).map(([domain, count]) => (
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

      {/* Layer Distribution */}
      {stats?.by_layer && (
        <div className="card">
          <h2 className="text-sm font-semibold text-gray-700 dark:text-gray-300 mb-2">Classification Layers</h2>
          <div className="flex flex-wrap gap-2">
            {Object.entries(stats.by_layer).map(([layer, count]) => (
              <span
                key={layer}
                className={`px-3 py-1 rounded-full text-xs font-medium ${LAYER_COLORS[layer as Layer] || LAYER_COLORS.llm}`}
              >
                {layer}: {count}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Traces Table */}
      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
              <th className="pb-2 pr-4">Time</th>
              <th className="pb-2 pr-4">Message</th>
              <th className="pb-2 pr-4">Domain</th>
              <th className="pb-2 pr-4">Layer</th>
              <th className="pb-2 pr-4">Confidence</th>
              <th className="pb-2 pr-4">Entities</th>
              <th className="pb-2">Feedback</th>
            </tr>
          </thead>
          <tbody>
            {traces.map((trace) => (
              <tr
                key={trace.id}
                className="border-b border-gray-100 dark:border-gray-800 hover:bg-gray-50 dark:hover:bg-gray-800/50"
              >
                <td className="py-2 pr-4 text-xs text-gray-400 whitespace-nowrap">
                  {trace.created_at ? new Date(trace.created_at).toLocaleTimeString() : '-'}
                </td>
                <td className="py-2 pr-4 max-w-xs truncate text-gray-700 dark:text-gray-300">
                  {trace.message}
                </td>
                <td className="py-2 pr-4">
                  <span className="px-2 py-0.5 rounded text-xs font-medium bg-blue-50 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300 capitalize">
                    {trace.domain}
                  </span>
                </td>
                <td className="py-2 pr-4">
                  <span className={`px-2 py-0.5 rounded text-xs font-medium ${(trace.layer && LAYER_COLORS[trace.layer]) || LAYER_COLORS.llm}`}>
                    {trace.layer || 'llm'}
                  </span>
                </td>
                <td className="py-2 pr-4 text-gray-600 dark:text-gray-400">
                  {trace.confidence != null ? trace.confidence.toFixed(2) : '-'}
                </td>
                <td className="py-2 pr-4 text-xs text-gray-500">
                  {trace.entity_matches?.map(e => e.id).join(', ') || '-'}
                </td>
                <td className="py-2">
                  {trace.user_feedback === 1 && <CheckCircle2 className="w-4 h-4 text-green-500" />}
                  {trace.user_feedback === -1 && <XCircle className="w-4 h-4 text-red-500" />}
                  {trace.user_feedback == null && <span className="text-gray-300">-</span>}
                </td>
              </tr>
            ))}
            {traces.length === 0 && (
              <tr>
                <td colSpan={7} className="py-8 text-center text-gray-400">
                  {loading ? 'Loading...' : 'No routing traces found'}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
