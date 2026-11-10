import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

/** Which router layer decided (`AgentRole.route_layer` in the backend). */
export type Layer =
  | 'role_hint'
  | 'entity_id'
  | 'continuity'
  | 'semantic'
  | 'llm'
  | 'fallback'
  | 'unknown';

export interface EntityMatch {
  id: string;
}

export interface RoutingTrace {
  id: number;
  created_at: string;
  user_id: number | null;
  message: string;
  domain: string;
  sub_intent: string | null;
  layer: Layer;
  /** Continuity score or semantic similarity; null for layers without one. */
  confidence: number | null;
  entity_matches: EntityMatch[];
}

export interface RoutingTracesResponse {
  traces: RoutingTrace[];
  enabled: boolean;
  retention_days: number;
}

export interface RoutingStats {
  total: number;
  by_domain: Record<string, number>;
  by_layer: Record<string, number>;
  enabled: boolean;
  retention_days: number;
}

async function fetchTraces(domain: string): Promise<RoutingTracesResponse> {
  const params = domain ? { domain } : {};
  const response = await apiClient.get<RoutingTracesResponse>(
    '/api/admin/routing-traces',
    { params },
  );
  return response.data;
}

async function fetchStats(): Promise<RoutingStats> {
  const response = await apiClient.get<RoutingStats>('/api/admin/routing-stats');
  return response.data;
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
