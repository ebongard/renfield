import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export interface Role {
  id: number;
  name: string;
  description?: string | null;
  permissions: string[];
  is_system?: boolean;
}

export interface RoleInput {
  name: string;
  description: string | null;
  permissions: string[];
}

async function fetchRoles(): Promise<Role[]> {
  const response = await apiClient.get<Role[]>('/api/roles');
  return Array.isArray(response.data) ? response.data : [];
}

async function fetchAllPermissions(): Promise<string[]> {
  const response = await apiClient.get<string[]>('/api/auth/permissions');
  return Array.isArray(response.data) ? response.data : [];
}

async function createRoleRequest(input: RoleInput): Promise<Role> {
  const response = await apiClient.post<Role>('/api/roles', input);
  return response.data;
}

async function updateRoleRequest(args: { id: number; input: RoleInput }): Promise<Role> {
  const response = await apiClient.patch<Role>(`/api/roles/${args.id}`, args.input);
  return response.data;
}

async function deleteRoleRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/roles/${id}`);
}

export function useRolesQuery() {
  return useApiQuery(
    {
      queryKey: keys.roles.list(),
      queryFn: fetchRoles,
      staleTime: STALE.CONFIG,
    },
    'roles.failedToLoad',
  );
}

export function useAllPermissionsQuery() {
  return useApiQuery(
    {
      queryKey: ['auth', 'permissions'] as const,
      queryFn: fetchAllPermissions,
      staleTime: STALE.CONFIG,
    },
    'roles.failedToLoad',
  );
}

export function useCreateRole() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createRoleRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.roles.all });
      },
    },
    'roles.failedToSave',
  );
}

export function useUpdateRole() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateRoleRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.roles.all });
      },
    },
    'roles.failedToSave',
  );
}

export function useDeleteRole() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteRoleRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.roles.all });
      },
    },
    'roles.failedToDelete',
  );
}
