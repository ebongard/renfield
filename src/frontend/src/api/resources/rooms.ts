import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type DeviceTypeKey = 'satellite' | 'web_panel' | 'web_tablet' | 'web_browser' | 'web_kiosk';

export interface RoomDevice {
  device_id: string;
  device_name?: string | null;
  device_type: DeviceTypeKey;
  is_online: boolean;
}

export interface Room {
  id: number;
  name: string;
  alias: string;
  icon?: string | null;
  source?: 'homeassistant' | 'satellite' | 'renfield' | string;
  ha_area_id?: string | null;
  owner_id?: number | null;
  owner_name?: string | null;
  device_count?: number;
  online_count?: number;
  devices?: RoomDevice[];
}

export interface HAArea {
  area_id: string;
  name: string;
  is_linked?: boolean;
  linked_room_name?: string;
}

export type ConflictResolution = 'skip' | 'link' | 'overwrite';

async function fetchRooms(): Promise<Room[]> {
  const response = await apiClient.get<Room[]>('/api/rooms');
  return response.data ?? [];
}

async function fetchHAAreas(): Promise<HAArea[]> {
  const response = await apiClient.get<HAArea[]>('/api/rooms/ha/areas');
  return response.data ?? [];
}

export interface CreateRoomInput {
  name: string;
  icon?: string | null;
}

interface UpdateRoomInput {
  id: number;
  patch: { name: string; icon?: string | null };
}

interface PatchOwnerInput {
  id: number;
  ownerId: number | null;
}

async function createRoomRequest(input: CreateRoomInput): Promise<Room> {
  const response = await apiClient.post<Room>('/api/rooms', input);
  return response.data;
}

async function updateRoomRequest({ id, patch }: UpdateRoomInput): Promise<Room> {
  const response = await apiClient.patch<Room>(`/api/rooms/${id}`, patch);
  return response.data;
}

async function patchRoomOwnerRequest({ id, ownerId }: PatchOwnerInput): Promise<void> {
  await apiClient.patch(`/api/rooms/${id}/owner`, null, { params: { owner_id: ownerId } });
}

async function deleteRoomRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/rooms/${id}`);
}

async function linkRoomRequest(args: { roomId: number; areaId: string }): Promise<void> {
  await apiClient.post(`/api/rooms/${args.roomId}/link/${args.areaId}`);
}

async function unlinkRoomRequest(roomId: number): Promise<void> {
  await apiClient.delete(`/api/rooms/${roomId}/link`);
}

async function importHARequest(conflictResolution: ConflictResolution): Promise<{ imported: number; linked: number; skipped: number }> {
  const response = await apiClient.post<{ imported: number; linked: number; skipped: number }>(
    '/api/rooms/ha/import',
    { conflict_resolution: conflictResolution },
  );
  return response.data;
}

async function exportHARequest(): Promise<{ exported: number; linked: number }> {
  const response = await apiClient.post<{ exported: number; linked: number }>('/api/rooms/ha/export');
  return response.data;
}

async function syncHARequest(conflictResolution: ConflictResolution): Promise<{
  import_results: { imported: number; linked: number };
  export_results: { exported: number; linked: number };
}> {
  const response = await apiClient.post<{
    import_results: { imported: number; linked: number };
    export_results: { exported: number; linked: number };
  }>(`/api/rooms/ha/sync?conflict_resolution=${conflictResolution}`);
  return response.data;
}

async function deleteDeviceRequest(deviceId: string): Promise<void> {
  await apiClient.delete(`/api/rooms/devices/${deviceId}`);
}

export function useRoomsQuery() {
  return useApiQuery(
    {
      queryKey: keys.rooms.list(),
      queryFn: fetchRooms,
      staleTime: STALE.DEFAULT,
    },
    'rooms.couldNotLoad',
  );
}

export function useHAAreasQuery(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.rooms.all, 'ha-areas'] as const,
      queryFn: fetchHAAreas,
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'rooms.couldNotLoadAreas',
  );
}

export function useCreateRoom() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createRoomRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'common.error',
  );
}

export function useUpdateRoom() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateRoomRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'common.error',
  );
}

export function usePatchRoomOwner() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: patchRoomOwnerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'common.error',
  );
}

export function useDeleteRoom() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteRoomRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'common.error',
  );
}

export function useLinkRoom() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: linkRoomRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'rooms.linkFailed',
  );
}

export function useUnlinkRoom() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: unlinkRoomRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'rooms.unlinkFailed',
  );
}

export function useHAImport() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: importHARequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'rooms.importFailed',
  );
}

export function useHAExport() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: exportHARequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'rooms.exportFailed',
  );
}

export function useHASync() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: syncHARequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'rooms.syncFailed',
  );
}

export function useDeleteRoomDevice() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.all });
      },
    },
    'common.error',
  );
}
