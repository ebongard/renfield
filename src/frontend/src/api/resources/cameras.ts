import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export interface CameraEvent {
  label: string;
  camera: string;
  start_time: number;
  score?: number;
}

async function fetchCameras(): Promise<string[]> {
  const response = await apiClient.get<{ cameras: string[] }>('/api/camera/cameras');
  return response.data.cameras ?? [];
}

async function fetchCameraEvents(label: string | null): Promise<CameraEvent[]> {
  const params = label ? { label } : {};
  const response = await apiClient.get<{ events: CameraEvent[] }>('/api/camera/events', { params });
  return response.data.events ?? [];
}

export function useCamerasQuery() {
  return useApiQuery(
    {
      queryKey: keys.cameras.list(),
      queryFn: fetchCameras,
      staleTime: STALE.DEFAULT,
    },
    'cameras.loadingCameras',
  );
}

export function useCameraEventsQuery(label: string | null) {
  return useApiQuery(
    {
      queryKey: [...keys.cameras.list(), 'events', { label }] as const,
      queryFn: () => fetchCameraEvents(label),
      staleTime: STALE.DEFAULT,
    },
    'cameras.loadingEvents',
  );
}
