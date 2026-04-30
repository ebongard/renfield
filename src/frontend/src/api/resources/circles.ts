import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';
import type { CircleTier } from '../../components/TierBadge';

export interface CircleSettings {
  default_capture_policy?: { tier?: number; [key: string]: unknown };
  [key: string]: unknown;
}

export interface CircleMember {
  member_user_id: number;
  member_username?: string;
  dimensions?: { tier?: number; [key: string]: unknown };
}

async function fetchCircleSettings(): Promise<CircleSettings> {
  const response = await apiClient.get<CircleSettings>('/api/circles/me/settings');
  return response.data;
}

async function fetchCircleMembers(): Promise<CircleMember[]> {
  const response = await apiClient.get<CircleMember[]>('/api/circles/me/members');
  return response.data ?? [];
}

async function patchCircleSettingsRequest(input: Partial<CircleSettings>): Promise<CircleSettings> {
  const response = await apiClient.patch<CircleSettings>('/api/circles/me/settings', input);
  return response.data;
}

interface AddMemberInput {
  member_user_id: number;
  dimension: 'tier';
  value: CircleTier;
}

async function addMemberRequest(input: AddMemberInput): Promise<void> {
  await apiClient.post('/api/circles/me/members', input);
}

interface UpdateMemberInput {
  memberUserId: number;
  dimension: 'tier';
  value: CircleTier;
}

async function updateMemberRequest({ memberUserId, dimension, value }: UpdateMemberInput): Promise<void> {
  await apiClient.patch(`/api/circles/me/members/${memberUserId}`, { dimension, value });
}

async function deleteMemberRequest(memberUserId: number): Promise<void> {
  await apiClient.delete(`/api/circles/me/members/${memberUserId}`);
}

export function useCircleSettingsQuery() {
  return useApiQuery(
    {
      queryKey: keys.circles.settings(),
      queryFn: fetchCircleSettings,
      staleTime: STALE.CONFIG,
    },
    'circles.couldNotLoad',
  );
}

export function useCircleMembersQuery() {
  return useApiQuery(
    {
      queryKey: keys.circles.members(),
      queryFn: fetchCircleMembers,
      staleTime: STALE.DEFAULT,
    },
    'circles.couldNotLoad',
  );
}

export function usePatchCircleSettings() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: patchCircleSettingsRequest,
      onSuccess: (data) => {
        queryClient.setQueryData(keys.circles.settings(), data);
      },
    },
    'circles.couldNotSave',
  );
}

export function useAddCircleMember() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: addMemberRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.circles.members() });
      },
    },
    'circles.couldNotSave',
  );
}

export function useUpdateCircleMember() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateMemberRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.circles.members() });
      },
    },
    'circles.couldNotSave',
  );
}

export function useDeleteCircleMember() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteMemberRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.circles.members() });
      },
    },
    'circles.couldNotSave',
  );
}
