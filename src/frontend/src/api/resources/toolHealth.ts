import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export interface ToolOutcomeStat {
  user_id: number | null;
  tool_name: string;
  success_count: number;
  failure_count: number;
  success_rate: number;
  last_used_at: string | null;
  last_failure_at: string | null;
  last_failure_summary: string | null;
}

export interface ToolWarning {
  tool_name: string;
  success_count: number;
  failure_count: number;
  total: number;
  success_rate: number;
  last_failure_at: string | null;
  last_failure_summary: string | null;
}

async function fetchToolStats(userId?: number | null): Promise<ToolOutcomeStat[]> {
  const response = await apiClient.get<ToolOutcomeStat[]>('/api/tool-health', {
    params: userId != null ? { user_id: userId } : undefined,
  });
  return response.data ?? [];
}

async function fetchToolWarnings(userId: number): Promise<ToolWarning[]> {
  const response = await apiClient.get<ToolWarning[]>(`/api/tool-health/warnings/${userId}`);
  return response.data ?? [];
}

export function useToolStatsQuery(userId?: number | null) {
  return useApiQuery(
    {
      queryKey: keys.toolHealth.list(userId ?? null),
      queryFn: () => fetchToolStats(userId),
      staleTime: STALE.DEFAULT,
    },
    'selfLearning.toolHealth.loadFailed',
  );
}

export function useToolWarningsQuery(userId: number | null) {
  return useApiQuery(
    {
      queryKey: keys.toolHealth.warnings(userId ?? -1),
      queryFn: () => fetchToolWarnings(userId as number),
      staleTime: STALE.DEFAULT,
      enabled: userId != null,
    },
    'selfLearning.toolHealth.loadFailed',
  );
}
