import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type MemoryCategory = 'preference' | 'fact' | 'instruction' | 'context';

export interface Memory {
  id: string | number;
  content: string;
  category: MemoryCategory;
  importance: number;
  access_count: number;
  created_at: string;
}

export interface MemoryListResponse {
  memories: Memory[];
  total: number;
}

export interface MemoryInput {
  content: string;
  category: MemoryCategory;
  importance: number;
}

async function fetchMemories(category: MemoryCategory | null): Promise<MemoryListResponse> {
  const params = new URLSearchParams();
  if (category) params.set('category', category);
  params.set('limit', '100');
  const response = await apiClient.get<MemoryListResponse>(`/api/memory?${params}`);
  return response.data;
}

async function createMemoryRequest(input: MemoryInput): Promise<Memory> {
  const response = await apiClient.post<Memory>('/api/memory', input);
  return response.data;
}

async function updateMemoryRequest(args: { id: string | number; input: MemoryInput }): Promise<Memory> {
  const response = await apiClient.patch<Memory>(`/api/memory/${args.id}`, args.input);
  return response.data;
}

async function deleteMemoryRequest(id: string | number): Promise<void> {
  await apiClient.delete(`/api/memory/${id}`);
}

export function useMemoriesQuery(category: MemoryCategory | null) {
  return useApiQuery(
    {
      queryKey: keys.memories.list(category),
      queryFn: () => fetchMemories(category),
      staleTime: STALE.DEFAULT,
    },
    'memory.couldNotLoad',
  );
}

export function useCreateMemory() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createMemoryRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.memories.all });
      },
    },
    'common.error',
  );
}

export function useUpdateMemory() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateMemoryRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.memories.all });
      },
    },
    'common.error',
  );
}

export function useDeleteMemory() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteMemoryRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.memories.all });
      },
    },
    'common.error',
  );
}
