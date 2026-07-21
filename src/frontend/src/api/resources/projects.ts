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

/** One merged project-timeline event (Phase 4A). Mirrors TimelineEvent in
 *  api/routes/projects.py — a document/meeting/decision/chat, newest-first. */
export interface TimelineEvent {
  kind: 'document' | 'meeting' | 'decision' | 'chat' | 'note';
  id: string;
  ts: string;
  title: string;
  subtitle: string | null;
  document_id: number | null;
  meeting_id: number | null;
  conversation_session_id: string | null;
}

async function fetchProjects(): Promise<Project[]> {
  const response = await apiClient.get<Project[]>('/api/projects');
  return Array.isArray(response.data) ? response.data : [];
}

async function fetchProject(id: number): Promise<Project> {
  const response = await apiClient.get<Project>(`/api/projects/${id}`);
  return response.data;
}

async function fetchTimeline(id: number): Promise<TimelineEvent[]> {
  const response = await apiClient.get<TimelineEvent[]>(`/api/projects/${id}/timeline`);
  return Array.isArray(response.data) ? response.data : [];
}

async function createProjectRequest(input: ProjectInput): Promise<Project> {
  const response = await apiClient.post<Project>('/api/projects', input);
  return response.data;
}

async function deleteProjectRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/projects/${id}`);
}

export function useProjectsQuery(enabled = true) {
  return useApiQuery(
    {
      queryKey: keys.projects.list(),
      queryFn: fetchProjects,
      staleTime: STALE.DEFAULT,
      // `/api/projects` 404s entirely when projects_enabled is off; callers on
      // projects-optional surfaces (e.g. MeetingsPage) gate this so a disabled
      // instance doesn't 404-retry on every mount.
      enabled,
    },
    'projects.failedToLoad',
  );
}

export function useProjectQuery(id: number | null) {
  return useApiQuery(
    {
      queryKey: keys.projects.detail(id ?? 0),
      queryFn: () => fetchProject(id as number),
      staleTime: STALE.DEFAULT,
      enabled: id != null,
    },
    'projects.failedToLoad',
  );
}

export function useProjectTimeline(id: number | null) {
  return useApiQuery(
    {
      queryKey: keys.projects.timeline(id ?? 0),
      queryFn: () => fetchTimeline(id as number),
      staleTime: STALE.DEFAULT,
      enabled: id != null,
    },
    'projects.failedToLoadTimeline',
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
