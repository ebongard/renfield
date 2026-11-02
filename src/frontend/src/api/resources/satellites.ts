import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type SatState = 'idle' | 'listening' | 'processing' | 'speaking' | 'error';

export interface WakeWordEvent {
  keyword: string;
  confidence: number;
  timestamp: number;
}

export interface SatelliteMetrics {
  audio_rms?: number;
  audio_db?: number;
  is_speech?: boolean;
  cpu_percent?: number;
  memory_percent?: number;
  temperature?: number;
  session_count_1h?: number;
  error_count_1h?: number;
  last_wakeword?: WakeWordEvent;
}

export interface SatelliteCapabilities {
  local_wakeword?: boolean;
  speaker?: boolean;
  led_count?: number;
  button?: boolean;
  /** "apa102" | "gpio_rgb" | "xvf3800" | "none" — runtime LED/array hint
   *  (true Ansible hat_type is not transmitted by the device). */
  led_type?: string | null;
  /** Physical mic count from the hardware (NOT post-DSP capture channels —
   *  XVF3800 / AC108 4-mic arrays deliver 1 channel after beamforming). */
  mic_count?: number;
  has_camera?: boolean;
  has_display?: boolean;
  has_enviro?: boolean;
}

export interface SatelliteSession {
  duration_seconds: number;
  audio_chunks_count: number;
  transcription?: string;
}

export type UpdateStatus = 'in_progress' | 'failed' | 'success' | string;

export interface SatelliteData {
  satellite_id: string;
  room: string;
  state: SatState;
  version?: string;
  has_active_session?: boolean;
  uptime_seconds: number;
  heartbeat_ago_seconds: number;
  metrics?: SatelliteMetrics;
  current_session?: SatelliteSession;
  capabilities?: SatelliteCapabilities;
  update_available?: boolean;
  update_status?: UpdateStatus;
  update_stage?: string;
  update_progress?: number;
  update_error?: string;
  /** The backend could not build this entry from its roster and serves
   *  placeholder values (durations are -1). The cause is in the backend log. */
  corrupt?: boolean;
}

interface SatellitesResponse {
  satellites: SatelliteData[];
  latest_version?: string;
}

async function fetchSatellites(): Promise<SatellitesResponse> {
  const response = await apiClient.get<SatellitesResponse>('/api/satellites');
  return {
    satellites: response.data.satellites ?? [],
    latest_version: response.data.latest_version ?? '',
  };
}

async function triggerUpdateRequest(satelliteId: string): Promise<void> {
  await apiClient.post(`/api/satellites/${satelliteId}/update`);
}

export function useSatellitesQuery(autoRefresh: boolean) {
  return useApiQuery(
    {
      queryKey: keys.satellites.list(),
      queryFn: fetchSatellites,
      staleTime: STALE.LIVE,
      refetchInterval: autoRefresh ? 2000 : false,
    },
    'satellites.loadError',
  );
}

export function useTriggerSatelliteUpdate() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: triggerUpdateRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.satellites.all });
      },
    },
    'satellites.updateError',
  );
}
