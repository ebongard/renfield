import apiClient from '../../utils/axios';
import { useApiMutation } from '../hooks';

export interface FtsResult {
  updated_count?: number;
  updated?: number;
  fts_config?: string;
}

export interface KwResult {
  keywords_count?: number;
  count?: number;
  sample?: string[] | string;
}

export interface EmbedResult {
  model?: string;
  counts?: Record<string, number>;
  errors?: string[];
}

export type IntentResult = Record<string, unknown>;

async function reindexFts(): Promise<FtsResult> {
  const response = await apiClient.post<FtsResult>('/api/knowledge/reindex-fts');
  return response.data;
}

async function refreshKeywords(): Promise<KwResult> {
  const response = await apiClient.post<KwResult>('/admin/refresh-keywords');
  return response.data;
}

async function reembedAll(): Promise<EmbedResult> {
  const response = await apiClient.post<EmbedResult>('/admin/reembed', null, {
    timeout: 1_800_000, // 30 min
  });
  return response.data;
}

async function testIntent(message: string): Promise<IntentResult> {
  const response = await apiClient.post<IntentResult>(
    `/debug/intent?message=${encodeURIComponent(message)}`,
  );
  return response.data;
}

export function useReindexFts() {
  return useApiMutation({ mutationFn: reindexFts }, 'maintenance.errors.reindexFailed');
}

export function useRefreshKeywords() {
  return useApiMutation({ mutationFn: refreshKeywords }, 'maintenance.errors.refreshKeywordsFailed');
}

export function useReembedAll() {
  return useApiMutation({ mutationFn: reembedAll }, 'maintenance.errors.reembedFailed');
}

export function useTestIntent() {
  return useApiMutation({ mutationFn: testIntent }, 'maintenance.errors.intentTestFailed');
}
