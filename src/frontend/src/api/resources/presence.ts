import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type DeviceTypeKind = 'phone' | 'watch' | 'tracker';
export type DetectionMethod = 'ble' | 'classic_bt';

export interface Occupant {
  user_id: number;
  user_name?: string;
  last_seen: number;
  confidence: number;
}

export interface PresenceRoom {
  room_id: number;
  room_name?: string;
  occupants: Occupant[];
}

export interface PresenceUser {
  id: number;
  username: string;
}

export interface BleDevice {
  id: number;
  user_id: number;
  mac_address: string;
  device_name: string;
  device_type: DeviceTypeKind;
  detection_method: DetectionMethod;
  is_enabled?: boolean;
}

export interface NewDevicePayload {
  user_id: number;
  mac_address: string;
  device_name: string;
  device_type: DeviceTypeKind;
  detection_method: DetectionMethod;
}

async function fetchRooms(): Promise<PresenceRoom[]> {
  const response = await apiClient.get<PresenceRoom[]>('/api/presence/rooms');
  return response.data ?? [];
}

async function fetchDevices(): Promise<BleDevice[]> {
  try {
    const response = await apiClient.get<BleDevice[]>('/api/presence/devices');
    return response.data ?? [];
  } catch {
    // May fail if non-admin
    return [];
  }
}

async function fetchPresenceUsers(): Promise<PresenceUser[]> {
  try {
    const response = await apiClient.get<PresenceUser[] | { users?: PresenceUser[] }>('/api/users');
    const data = response.data;
    return Array.isArray(data) ? data : (data?.users ?? []);
  } catch {
    return [];
  }
}

async function fetchPresenceStatus(): Promise<{ enabled: boolean }> {
  try {
    const response = await apiClient.get<{ enabled?: boolean }>('/api/presence/status');
    return { enabled: response.data?.enabled ?? false };
  } catch {
    return { enabled: false };
  }
}

async function createDeviceRequest(input: NewDevicePayload): Promise<void> {
  await apiClient.post('/api/presence/devices', input);
}

async function patchDeviceRequest(args: { id: number; patch: Partial<BleDevice> }): Promise<void> {
  await apiClient.patch(`/api/presence/devices/${args.id}`, args.patch);
}

async function deleteDeviceRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/presence/devices/${id}`);
}

export function usePresenceRoomsQuery(autoRefresh: boolean) {
  return useApiQuery(
    {
      queryKey: keys.presence.current(),
      queryFn: fetchRooms,
      staleTime: STALE.LIVE,
      refetchInterval: autoRefresh ? 5000 : false,
    },
    'presence.loadError',
  );
}

export function usePresenceDevicesQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.presence.all, 'devices'] as const,
      queryFn: fetchDevices,
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );
}

export function usePresenceUsersQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.presence.all, 'users'] as const,
      queryFn: fetchPresenceUsers,
      staleTime: STALE.CONFIG,
    },
    'common.error',
  );
}

export function usePresenceStatusQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.presence.all, 'status'] as const,
      queryFn: fetchPresenceStatus,
      staleTime: STALE.CONFIG,
    },
    'common.error',
  );
}

export function useCreatePresenceDevice() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: [...keys.presence.all, 'devices'] });
      },
    },
    'common.error',
  );
}

export function usePatchPresenceDevice() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: patchDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: [...keys.presence.all, 'devices'] });
      },
    },
    'common.error',
  );
}

export function useDeletePresenceDevice() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: [...keys.presence.all, 'devices'] });
      },
    },
    'common.error',
  );
}
