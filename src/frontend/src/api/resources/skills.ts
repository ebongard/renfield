import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiMutation, useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export type SkillStatus = 'draft' | 'approved' | 'rejected' | 'archived';
export type SkillSource = 'auto_extracted' | 'seed' | 'user_created';

export interface Skill {
  id: number;
  title: string;
  body_md: string;
  trigger_examples: string[];
  tool_sequence: string[];
  source: SkillSource;
  status: SkillStatus;
  version: number;
  success_count: number;
  failure_count: number;
  last_used_at: string | null;
  pinned: boolean;
  circle_tier: number;
  atom_id: string | null;
  merged_into_id: number | null;
  user_id: number | null;
  created_at: string;
  updated_at: string;
  is_owner: boolean;
}

export interface SkillListFilters {
  status?: SkillStatus;
  source?: SkillSource;
  admin_view?: boolean;
  include_seeds?: boolean;
  limit?: number;
  offset?: number;
}

export interface SkillUpdateInput {
  id: number;
  title?: string;
  body_md?: string;
  trigger_examples?: string[];
  tool_sequence?: string[];
  pinned?: boolean;
  status?: SkillStatus;
}

export interface SkillCreateInput {
  title: string;
  body_md: string;
  trigger_examples: string[];
  tool_sequence?: string[];
  circle_tier?: number;
}

async function fetchSkills(filters: SkillListFilters): Promise<Skill[]> {
  const response = await apiClient.get<Skill[]>('/api/skills', { params: filters });
  return response.data ?? [];
}

async function fetchSkill(id: number): Promise<Skill> {
  const response = await apiClient.get<Skill>(`/api/skills/${id}`);
  return response.data;
}

async function fetchDraftCount(): Promise<number> {
  const response = await apiClient.get<{ count: number }>('/api/skills/draft-count');
  return response.data?.count ?? 0;
}

async function approveSkillRequest(id: number): Promise<Skill> {
  const response = await apiClient.post<Skill>(`/api/skills/${id}/approve`);
  return response.data;
}

async function rejectSkillRequest(id: number): Promise<Skill> {
  const response = await apiClient.post<Skill>(`/api/skills/${id}/reject`);
  return response.data;
}

async function updateSkillRequest(input: SkillUpdateInput): Promise<Skill> {
  const { id, ...patch } = input;
  const response = await apiClient.patch<Skill>(`/api/skills/${id}`, patch);
  return response.data;
}

async function createSkillRequest(input: SkillCreateInput): Promise<Skill> {
  const response = await apiClient.post<Skill>('/api/skills', input);
  return response.data;
}

async function deleteSkillRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/skills/${id}`);
}

async function pinSkillRequest(id: number): Promise<Skill> {
  const response = await apiClient.post<Skill>(`/api/skills/${id}/pin`);
  return response.data;
}

async function unpinSkillRequest(id: number): Promise<Skill> {
  const response = await apiClient.post<Skill>(`/api/skills/${id}/unpin`);
  return response.data;
}

export function useSkillsQuery(filters: SkillListFilters = {}) {
  return useApiQuery(
    {
      queryKey: keys.skills.list(filters),
      queryFn: () => fetchSkills(filters),
      staleTime: STALE.DEFAULT,
    },
    'selfLearning.skills.loadFailed',
  );
}

export function useSkillQuery(id: number | null) {
  return useApiQuery(
    {
      queryKey: keys.skills.detail(id ?? -1),
      queryFn: () => fetchSkill(id as number),
      staleTime: STALE.DEFAULT,
      enabled: id != null,
    },
    'selfLearning.skills.loadFailed',
  );
}

export function useDraftCountQuery() {
  return useApiQuery(
    {
      queryKey: keys.skills.draftCount(),
      queryFn: fetchDraftCount,
      // The badge updates frequently from the agent's background extractor;
      // 15 s STALE keeps the dot fresh without thundering the endpoint.
      staleTime: 15_000,
      refetchInterval: 60_000,
    },
    'selfLearning.skills.loadFailed',
  );
}

function invalidateAllSkills(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: keys.skills.all });
}

export function useApproveSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: approveSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.approveFailed',
  );
}

export function useRejectSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: rejectSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.rejectFailed',
  );
}

export function useUpdateSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.updateFailed',
  );
}

export function useCreateSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.createFailed',
  );
}

export function useDeleteSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.deleteFailed',
  );
}

export function usePinSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: pinSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.updateFailed',
  );
}

export function useUnpinSkill() {
  const qc = useQueryClient();
  return useApiMutation(
    {
      mutationFn: unpinSkillRequest,
      onSuccess: () => invalidateAllSkills(qc),
    },
    'selfLearning.skills.updateFailed',
  );
}
