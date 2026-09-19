import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type OutputType = 'audio' | 'visual';

/**
 * Named TTS sound profiles the backend knows. `null` on a device = off.
 * An unknown name is rejected by the backend with HTTP 422, so this list is
 * the single frontend source for the picker.
 */
export const TTS_EQ_PROFILES = ['hifi_speech'] as const;
export type TtsEqProfile = (typeof TTS_EQ_PROFILES)[number];

export function isTtsEqProfile(value: string): value is TtsEqProfile {
  return (TTS_EQ_PROFILES as readonly string[]).includes(value);
}

export interface OutputDevice {
  id: number;
  output_type: OutputType;
  is_enabled: boolean;
  allow_interruption: boolean;
  tts_volume: number | null;
  // Named sound profile for spoken answers; null = off. Typed as string because
  // the server may know a profile this build does not (rendered verbatim then).
  tts_eq_profile: string | null;
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

/** Create body for POST /api/rooms/{id}/output-devices. */
export interface AddOutputPayload {
  output_type: OutputType;
  allow_interruption: boolean;
  tts_volume: number;
  tts_eq_profile: TtsEqProfile | null;
  priority: number;
  output_provider?: string;
  output_target_id?: string;
  renfield_device_id?: string;
  ha_entity_id?: string;
  dlna_renderer_name?: string;
}

interface AddOutputArgs {
  roomId: number;
  payload: AddOutputPayload;
}

async function addOutputDeviceRequest({ roomId, payload }: AddOutputArgs): Promise<void> {
  await apiClient.post(`/api/rooms/${roomId}/output-devices`, payload);
}

/** Update body for PATCH /api/rooms/output-devices/{id}. */
export type UpdateOutputPayload = Partial<
  Pick<OutputDevice, 'is_enabled' | 'allow_interruption' | 'tts_volume' | 'priority'>
> & { tts_eq_profile?: TtsEqProfile | null };

interface UpdateOutputArgs {
  deviceId: number;
  updates: UpdateOutputPayload;
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
