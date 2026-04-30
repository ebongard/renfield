import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export type Layer = 'entity_id' | 'continuity' | 'semantic' | 'mlp' | 'llm';

export interface EntityMatch {
  id: string;
}

export interface RoutingTrace {
  id: string | number;
  created_at?: string;
  message: string;
  domain: string;
  layer?: Layer;
  confidence?: number | null;
  entity_matches?: EntityMatch[];
  user_feedback?: 1 | -1 | null;
}

export interface RoutingStats {
  by_domain?: Record<string, number>;
  by_layer?: Record<string, number>;
}

async function fetchTraces(domain: string): Promise<RoutingTrace[]> {
  const params = domain ? { domain } : {};
  const response = await apiClient.get<{ traces: RoutingTrace[] }>(
    '/api/admin/routing-traces',
    { params },
  );
  return response.data.traces ?? [];
}

async function fetchStats(): Promise<RoutingStats> {
  const response = await apiClient.get<RoutingStats>('/api/admin/routing-stats');
  return response.data ?? {};
}

export function useRoutingTracesQuery(domain: string) {
  return useApiQuery(
    {
      queryKey: [...keys.routing.history(), { domain }] as const,
      queryFn: () => fetchTraces(domain),
      staleTime: STALE.LIVE,
    },
    'common.error',
  );
}

export function useRoutingStatsQuery() {
  return useApiQuery(
    {
      queryKey: keys.routing.stats(),
      queryFn: fetchStats,
      staleTime: STALE.LIVE,
    },
    'common.error',
  );
}
