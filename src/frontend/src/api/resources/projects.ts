import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

/** A business-instance project (Phase 1). Mirrors ProjectResponse in
 *  api/routes/projects.py. Each project owns exactly one KnowledgeBase. */
export interface Project {
  id: number;
  name: string;
  description: string | null;
  owner_id: number | null;
  knowledge_base_id: number | null;
  circle_tier: number;
  status: string;
  created_at: string;
  document_count: number;
}

export interface ProjectInput {
  name: string;
  description?: string | null;
  circle_tier?: number;
}

async function fetchProjects(): Promise<Project[]> {
  const response = await apiClient.get<Project[]>('/api/projects');
  return Array.isArray(response.data) ? response.data : [];
}

async function createProjectRequest(input: ProjectInput): Promise<Project> {
  const response = await apiClient.post<Project>('/api/projects', input);
  return response.data;
}

async function deleteProjectRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/projects/${id}`);
}

export function useProjectsQuery() {
  return useApiQuery(
    {
      queryKey: keys.projects.list(),
      queryFn: fetchProjects,
      staleTime: STALE.DEFAULT,
    },
    'projects.failedToLoad',
  );
}

export function useCreateProject() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createProjectRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.projects.all });
      },
    },
    'projects.failedToSave',
  );
}

export function useDeleteProject() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteProjectRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.projects.all });
      },
    },
    'projects.failedToDelete',
  );
}
