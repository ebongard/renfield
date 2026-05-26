import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiMutation, useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export type CuratorRunStatus = 'running' | 'success' | 'partial' | 'failed';
export type CuratorRunType = 'scheduled' | 'manual';

export interface CuratorRun {
  id: number;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  run_type: CuratorRunType;
  triggered_by_user_id: number | null;
  status: CuratorRunStatus;
  skills_examined: number;
  duplicate_pairs_found: number;
  duplicate_pairs_merged: number;
  stale_skills_archived: number;
  error_message: string | null;
}

async function fetchCuratorRuns(): Promise<CuratorRun[]> {
  const response = await apiClient.get<CuratorRun[]>('/api/skills/curator/runs', {
    params: { limit: 50 },
  });
  return response.data ?? [];
}

async function runCuratorRequest(): Promise<CuratorRun> {
  const response = await apiClient.post<CuratorRun>('/api/skills/curator/run', {});
  return response.data;
}

export function useCuratorRunsQuery() {
  return useApiQuery(
    {
      queryKey: keys.curator.runs(),
      queryFn: fetchCuratorRuns,
      staleTime: STALE.DEFAULT,
    },
    'selfLearning.curator.loadFailed',
  );
}

export function useRunCurator() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: runCuratorRequest,
      onSuccess: () => {
        qc.invalidateQueries({ queryKey: keys.curator.all });
        qc.invalidateQueries({ queryKey: keys.skills.all });
      },
    },
    'selfLearning.curator.runFailed',
  );
}
