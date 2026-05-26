import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiMutation, useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export type TrajectoryOutcome = 'success' | 'tool_fail' | 'abort' | 'user_corrected';

export interface TrajectorySummary {
  id: number;
  user_id: number | null;
  conversation_id: number | null;
  outcome: TrajectoryOutcome;
  tool_count: number;
  distinct_tool_count: number;
  token_count: number | null;
  extracted_skill_id: number | null;
  used_skill_ids: number[];
  flagged_for_retention: boolean;
  created_at: string;
}

export interface TrajectoryDetail extends TrajectorySummary {
  raw_payload: Record<string, unknown>;
  redacted_payload: Record<string, unknown> | null;
}

export interface TrajectoryStats {
  total: number;
  by_outcome: Record<string, number>;
  last_7d: number;
  flagged_total: number;
  capture_enabled: boolean;
  retention_days: number;
}

export interface TrajectoryListFilters {
  user_id?: number;
  outcome?: TrajectoryOutcome;
  flagged_only?: boolean;
  since_days?: number;
  limit?: number;
  offset?: number;
}

async function fetchTrajectories(filters: TrajectoryListFilters): Promise<TrajectorySummary[]> {
  const response = await apiClient.get<TrajectorySummary[]>('/api/trajectories', {
    params: filters,
  });
  return response.data ?? [];
}

async function fetchTrajectory(id: number): Promise<TrajectoryDetail> {
  const response = await apiClient.get<TrajectoryDetail>(`/api/trajectories/${id}`);
  return response.data;
}

async function fetchTrajectoryStats(): Promise<TrajectoryStats> {
  const response = await apiClient.get<TrajectoryStats>('/api/trajectories/stats');
  return response.data;
}

async function flagTrajectoryRequest(input: { id: number; flagged: boolean }): Promise<TrajectorySummary> {
  const response = await apiClient.post<TrajectorySummary>(
    `/api/trajectories/${input.id}/flag`,
    { flagged: input.flagged },
  );
  return response.data;
}

export function useTrajectoriesQuery(filters: TrajectoryListFilters = {}) {
  return useApiQuery(
    {
      queryKey: keys.trajectories.list(filters),
      queryFn: () => fetchTrajectories(filters),
      staleTime: STALE.DEFAULT,
    },
    'selfLearning.trajectories.loadFailed',
  );
}

export function useTrajectoryQuery(id: number | null) {
  return useApiQuery(
    {
      queryKey: keys.trajectories.detail(id ?? -1),
      queryFn: () => fetchTrajectory(id as number),
      staleTime: STALE.DEFAULT,
      enabled: id != null,
    },
    'selfLearning.trajectories.loadFailed',
  );
}

export function useTrajectoryStatsQuery() {
  return useApiQuery(
    {
      queryKey: keys.trajectories.stats(),
      queryFn: fetchTrajectoryStats,
      staleTime: STALE.DEFAULT,
    },
    'selfLearning.trajectories.loadFailed',
  );
}

export function useFlagTrajectory() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: flagTrajectoryRequest,
      onSuccess: () => qc.invalidateQueries({ queryKey: keys.trajectories.all }),
    },
    'selfLearning.trajectories.flagFailed',
  );
}

/**
 * Build the JSONL export URL with the current filters. Used by the
 * AdminTrajectoriesPage "Download JSONL" button so it can issue a
 * browser-native download instead of streaming into memory.
 */
export function buildTrajectoryExportUrl(filters: {
  outcome?: TrajectoryOutcome;
  since_days?: number;
  flagged_only?: boolean;
  require_redacted?: boolean;
}): string {
  const params = new URLSearchParams();
  if (filters.outcome) params.set('outcome', filters.outcome);
  if (filters.since_days != null) params.set('since_days', String(filters.since_days));
  if (filters.flagged_only) params.set('flagged_only', 'true');
  if (filters.require_redacted === false) params.set('require_redacted', 'false');
  const qs = params.toString();
  return `/api/trajectories/export.jsonl${qs ? `?${qs}` : ''}`;
}
