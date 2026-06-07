import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type OutputType = 'audio' | 'visual';

export interface OutputDevice {
  id: number;
  output_type: OutputType;
  is_enabled: boolean;
  allow_interruption: boolean;
  tts_volume: number | null;
  priority: number;
  device_name?: string | null;
  dlna_renderer_name?: string | null;
  ha_entity_id?: string | null;
  renfield_device_id?: string | null;
  // Generic output-provider pair (output_providers_enabled). When set, the row
  // was created via the unified picker; legacy id columns may be null.
  output_provider?: string | null;
  output_target_id?: string | null;
}

/** One target in the unified available-outputs union (flag-on). */
export interface OutputTarget {
  provider: string;        // renfield | homeassistant | dlna | samsung | ...
  target_id: string;
  name: string;
  capabilities: string[];  // audio | video | power | transport | queue
  room_hint?: string | null;
  reachable: boolean;
}

export interface RenfieldOutputDevice {
  device_id: string;
  device_name?: string;
}

export interface HaOutputDevice {
  entity_id: string;
  friendly_name?: string;
}

export interface DlnaOutputDevice {
  name: string;
  friendly_name?: string;
}

export interface AvailableOutputs {
  renfield_devices: RenfieldOutputDevice[];
  ha_media_players: HaOutputDevice[];
  dlna_renderers: DlnaOutputDevice[];
  // Unified capability-tagged union — present only when output_providers_enabled.
  // null/undefined => legacy three-list shape.
  output_targets?: OutputTarget[] | null;
}

const EMPTY_AVAILABLE: AvailableOutputs = {
  renfield_devices: [],
  ha_media_players: [],
  dlna_renderers: [],
};

async function fetchOutputDevices(roomId: number): Promise<OutputDevice[]> {
  const response = await apiClient.get<OutputDevice[]>(`/api/rooms/${roomId}/output-devices`);
  return response.data ?? [];
}

async function fetchAvailableOutputs(roomId: number): Promise<AvailableOutputs> {
  const response = await apiClient.get<AvailableOutputs>(`/api/rooms/${roomId}/available-outputs`);
  return response.data ?? EMPTY_AVAILABLE;
}

interface AddOutputArgs {
  roomId: number;
  payload: Record<string, unknown>;
}

async function addOutputDeviceRequest({ roomId, payload }: AddOutputArgs): Promise<void> {
  await apiClient.post(`/api/rooms/${roomId}/output-devices`, payload);
}

interface UpdateOutputArgs {
  deviceId: number;
  updates: Partial<OutputDevice>;
}

async function updateOutputDeviceRequest({ deviceId, updates }: UpdateOutputArgs): Promise<void> {
  await apiClient.patch(`/api/rooms/output-devices/${deviceId}`, updates);
}

async function deleteOutputDeviceRequest(deviceId: number): Promise<void> {
  await apiClient.delete(`/api/rooms/output-devices/${deviceId}`);
}

interface ReorderArgs {
  roomId: number;
  outputType: OutputType;
  deviceIds: number[];
}

async function reorderOutputsRequest({ roomId, outputType, deviceIds }: ReorderArgs): Promise<void> {
  await apiClient.post(
    `/api/rooms/${roomId}/output-devices/reorder?output_type=${outputType}`,
    { device_ids: deviceIds },
  );
}

export function useOutputDevicesQuery(roomId: number, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.rooms.outputs(roomId),
      queryFn: () => fetchOutputDevices(roomId),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'common.error',
  );
}

export function useAvailableOutputsQuery(roomId: number, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.rooms.outputs(roomId), 'available'] as const,
      queryFn: () => fetchAvailableOutputs(roomId),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'common.error',
  );
}

export function useAddOutputDevice(roomId: number) {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: addOutputDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.outputs(roomId) });
      },
    },
    'common.error',
  );
}

export function useUpdateOutputDevice(roomId: number) {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: updateOutputDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.outputs(roomId) });
      },
    },
    'common.error',
  );
}

export function useDeleteOutputDevice(roomId: number) {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteOutputDeviceRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.outputs(roomId) });
      },
    },
    'common.error',
  );
}

export function useReorderOutputDevices(roomId: number) {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: reorderOutputsRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.rooms.outputs(roomId) });
      },
    },
    'common.error',
  );
}
