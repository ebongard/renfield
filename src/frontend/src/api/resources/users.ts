import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type PersonalityStyle = 'freundlich' | 'direkt' | 'formell' | 'casual';

export interface AdminUser {
  id: number;
  username: string;
  first_name?: string | null;
  last_name?: string | null;
  email?: string | null;
  role_id: number;
  role_name?: string;
  is_active: boolean;
  personality_style?: PersonalityStyle;
  personality_prompt?: string | null;
  speaker_id?: number | null;
  last_login?: string | null;
}

export interface RoleSummary {
  id: number;
  name: string;
  description?: string;
}

export interface SpeakerSummary {
  id: number;
  name: string;
  embedding_count: number;
}

export interface CreateUserInput {
  username: string;
  first_name?: string | null;
  last_name?: string | null;
  email?: string | null;
  password: string;
  role_id: number;
  is_active: boolean;
  personality_style: PersonalityStyle;
  personality_prompt?: string | null;
}

export interface UpdateUserInput {
  id: number;
  patch: {
    username: string;
    first_name?: string | null;
    last_name?: string | null;
    email?: string | null;
    role_id: number;
    is_active: boolean;
    personality_style: PersonalityStyle;
    personality_prompt?: string | null;
  };
}

async function fetchUsers(): Promise<AdminUser[]> {
  const response = await apiClient.get<AdminUser[] | { users?: AdminUser[] }>('/api/users');
  const data = response.data;
  return Array.isArray(data) ? data : (data.users ?? []);
}

async function fetchRoles(): Promise<RoleSummary[]> {
  const response = await apiClient.get<RoleSummary[] | { roles?: RoleSummary[] }>('/api/roles');
  const data = response.data;
  return Array.isArray(data) ? data : (data.roles ?? []);
}

async function fetchSpeakers(): Promise<SpeakerSummary[]> {
  try {
    const response = await apiClient.get<SpeakerSummary[]>('/api/speakers');
    return response.data ?? [];
  } catch {
    return [];
  }
}

async function createUserRequest(input: CreateUserInput): Promise<AdminUser> {
  const response = await apiClient.post<AdminUser>('/api/users', input);
  return response.data;
}

async function updateUserRequest(input: UpdateUserInput): Promise<AdminUser> {
  const response = await apiClient.patch<AdminUser>(`/api/users/${input.id}`, input.patch);
  return response.data;
}

async function resetPasswordRequest(args: { id: number; password: string }): Promise<void> {
  await apiClient.post(`/api/users/${args.id}/reset-password`, { new_password: args.password });
}

async function deleteUserRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/users/${id}`);
}

async function linkSpeakerRequest(args: { userId: number; speakerId: number }): Promise<void> {
  await apiClient.post(`/api/users/${args.userId}/link-speaker`, { speaker_id: args.speakerId });
}

async function unlinkSpeakerRequest(userId: number): Promise<void> {
  await apiClient.delete(`/api/users/${userId}/unlink-speaker`);
}

export function useUsersQuery() {
  return useApiQuery(
    {
      queryKey: keys.users.list(),
      queryFn: fetchUsers,
      staleTime: STALE.DEFAULT,
    },
    'users.failedToLoad',
  );
}

export function useRolesListQuery() {
  return useApiQuery(
    {
      queryKey: keys.roles.list(),
      queryFn: fetchRoles,
      staleTime: STALE.CONFIG,
    },
    'users.failedToLoad',
  );
}

export function useSpeakersListQuery() {
  return useApiQuery(
    {
      queryKey: keys.speakers.list(),
      queryFn: fetchSpeakers,
      staleTime: STALE.DEFAULT,
    },
    'users.failedToLoad',
  );
}

export function useCreateUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToSave',
  );
}

export function useUpdateUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToSave',
  );
}

export function useResetUserPassword() {
  return useApiMutation(
    {
      mutationFn: resetPasswordRequest,
    },
    'users.failedToSave',
  );
}

export function useDeleteUser() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteUserRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToDelete',
  );
}

export function useLinkSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: linkSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToLink',
  );
}

export function useUnlinkSpeaker() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: unlinkSpeakerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.users.all });
      },
    },
    'users.failedToUnlink',
  );
}
