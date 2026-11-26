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
  // Guard against a non-array body (e.g. a stale cached HTML/SPA fallback) so
  // consumers never crash on `.flatMap`/`.map`.
  return Array.isArray(response.data) ? response.data : [];
}

async function fetchDevices(): Promise<BleDevice[]> {
  try {
    const response = await apiClient.get<BleDevice[]>('/api/presence/devices');
    return Array.isArray(response.data) ? response.data : [];
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

interface AnalyticsArgs {
  days: number;
  userId: string;
}

async function fetchHeatmap({ days, userId }: AnalyticsArgs): Promise<unknown[]> {
  const params: Record<string, unknown> = { days };
  if (userId) params.user_id = userId;
  const response = await apiClient.get<unknown[]>('/api/presence/analytics/heatmap', { params });
  return Array.isArray(response.data) ? response.data : [];
}

async function fetchPredictions({ days, userId }: AnalyticsArgs): Promise<unknown[]> {
  if (!userId) return [];
  const response = await apiClient.get<unknown[]>('/api/presence/analytics/predictions', {
    params: { user_id: userId, days },
  });
  return Array.isArray(response.data) ? response.data : [];
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

export function usePresenceHeatmapQuery<T = unknown>(args: { days: number; userId: string }) {
  return useApiQuery(
    {
      queryKey: keys.presence.analytics(`heatmap:${args.days}:${args.userId}`),
      queryFn: () => fetchHeatmap(args) as Promise<T[]>,
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );
}

export function usePresencePredictionsQuery<T = unknown>(args: { days: number; userId: string }) {
  return useApiQuery(
    {
      queryKey: keys.presence.analytics(`predictions:${args.days}:${args.userId}`),
      queryFn: () => fetchPredictions(args) as Promise<T[]>,
      staleTime: STALE.DEFAULT,
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

// --- Per-person BLE IRK store (resolve rotating phone addresses) ---

export interface BleIrk {
  id: number;
  user_id: number;
  label: string;
  is_enabled: boolean;
  created_at?: string | null;
}

export interface IrkCapturePayload {
  satellite_id: string;
  user_id: number;
  label: string;
  window_seconds?: number;
}

async function fetchIrks(): Promise<BleIrk[]> {
  try {
    const response = await apiClient.get<BleIrk[]>('/api/presence/irks');
    return Array.isArray(response.data) ? response.data : [];
  } catch {
    return [];
  }
}

async function captureIrkRequest(input: IrkCapturePayload): Promise<BleIrk> {
  // Long-poll: the backend holds the request open for the pairing window.
  const response = await apiClient.post<BleIrk>('/api/presence/irks/capture', input, {
    timeout: ((input.window_seconds ?? 60) + 30) * 1000,
  });
  return response.data;
}

async function deleteIrkRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/presence/irks/${id}`);
}

export function usePresenceIrksQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.presence.all, 'irks'] as const,
      queryFn: fetchIrks,
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );
}

export function useCaptureIrk() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: captureIrkRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: [...keys.presence.all, 'irks'] });
      },
    },
    'common.error',
  );
}

export function useDeletePresenceIrk() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteIrkRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: [...keys.presence.all, 'irks'] });
      },
    },
    'common.error',
  );
}

// --- Placement v2 calibration (admin; ha_glue/api/routes/presence.py) ---

export interface CalibrationReceiver {
  satellite_id: string;
  offset_db: number;
  in_room_dbm: number;
  cutoff_dbm: number;
}

export interface CalibrationZone {
  name: string;
  room_ids: number[];
}

/** What a satellite (>= 1.4.23) last said about its BLE scanner. */
export interface CalibrationScanner {
  satellite_id: string;
  mode: 'continuous' | 'periodic';
  running: boolean;
  /** The cutoff it filters at — the one in effect (it wins over the stored one). */
  cutoff_dbm: number | null;
  /** Seconds since it last heard an advertisement of any device. */
  last_advert_age_s: number | null;
  /** Heard something within the window that counts as a sign of life. */
  hearing: boolean;
}

export interface Calibration {
  source: 'database' | 'empty';
  settings_ignored: boolean;
  engine_active: boolean;
  receivers: CalibrationReceiver[];
  zones: CalibrationZone[];
  min_samples: number;
  in_room_percentile: number;
  /** Absent from an older backend. */
  scanners?: CalibrationScanner[];
}

export interface CalibrationProposal {
  satellite_id: string;
  samples: number;
  in_room_dbm: number;
  min_dbm: number;
  median_dbm: number;
  max_dbm: number;
  /** null = no cutoff known (older satellite, uncalibrated): the admin enters it. */
  cutoff_dbm: number | null;
  offset_db: number;
  current: CalibrationReceiver | null;
  /** Where cutoff_dbm comes from. */
  cutoff_source?: 'satellite' | 'stored' | null;
}

/** Stable refusal codes of the calibration routes (`detail.code`). */
export type CalibrationErrorCode =
  | 'invalid_receiver'
  | 'invalid_zones'
  | 'invalid_satellite'
  | 'unknown_rooms'
  | 'not_calibrated'
  | 'not_started'
  | 'too_old'
  | 'device_not_heard'
  | 'too_few_readings'
  | 'bound_not_above_cutoff';

export interface CalibrationErrorDetail {
  code: CalibrationErrorCode;
  samples?: number;
  needed?: number;
  window_s?: number;
  bound_dbm?: number;
  cutoff_dbm?: number;
  room_ids?: number[];
}

/** The refusal detail of a calibration request, or null for any other failure. */
export function calibrationErrorDetail(error: unknown): CalibrationErrorDetail | null {
  if (typeof error !== 'object' || error === null || !('response' in error)) return null;
  const response = (error as { response?: { data?: { detail?: unknown } } }).response;
  const detail = response?.data?.detail;
  if (typeof detail === 'object' && detail !== null && 'code' in detail
      && typeof (detail as { code: unknown }).code === 'string') {
    return detail as CalibrationErrorDetail;
  }
  return null;
}

const calibrationKey = [...keys.presence.all, 'calibration'] as const;

async function fetchCalibration(): Promise<Calibration> {
  const response = await apiClient.get<Calibration>('/api/presence/calibration');
  return response.data;
}

export function usePresenceCalibrationQuery() {
  return useApiQuery(
    { queryKey: calibrationKey, queryFn: fetchCalibration, staleTime: STALE.CONFIG },
    'common.error',
  );
}

export async function saveCalibrationReceiver(receiver: CalibrationReceiver): Promise<Calibration> {
  const { satellite_id: satelliteId, ...values } = receiver;
  const response = await apiClient.put<Calibration>(
    `/api/presence/calibration/receivers/${encodeURIComponent(satelliteId)}`,
    values,
  );
  return response.data;
}

export async function deleteCalibrationReceiver(satelliteId: string): Promise<Calibration> {
  const response = await apiClient.delete<Calibration>(
    `/api/presence/calibration/receivers/${encodeURIComponent(satelliteId)}`,
  );
  return response.data;
}

export async function saveCalibrationZones(zones: CalibrationZone[]): Promise<Calibration> {
  const response = await apiClient.put<Calibration>('/api/presence/calibration/zones', { zones });
  return response.data;
}

export async function startCalibrationMeasurement(): Promise<number> {
  const response = await apiClient.post<{ since: number }>('/api/presence/calibration/measurement');
  return response.data.since;
}

export async function requestCalibrationProposal(args: {
  deviceKey: string;
  satelliteId: string;
  since: number;
}): Promise<CalibrationProposal> {
  const response = await apiClient.post<CalibrationProposal>('/api/presence/calibration/proposal', {
    device_key: args.deviceKey,
    satellite_id: args.satelliteId,
    since: args.since,
  });
  return response.data;
}

/** Writes return the whole calibration; put it into the cache. */
export function useCalibrationCache() {
  const queryClient = useQueryClient();
  return (calibration: Calibration) => queryClient.setQueryData(calibrationKey, calibration);
}
