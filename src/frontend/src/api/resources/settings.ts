import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export interface KeywordOption {
  id: string;
  label: string;
  description?: string;
}

export interface WakewordSettingsData {
  keyword: string;
  threshold: number;
  cooldown_ms: number;
  available_keywords?: KeywordOption[];
  subscriber_count?: number;
}

export interface SyncDevice {
  device_id: string;
  device_type?: 'satellite' | 'web' | string;
  synced: boolean;
  active_keywords?: string[];
  error?: string;
}

export interface SyncStatusData {
  devices: SyncDevice[];
  all_synced: boolean;
  failed_count: number;
}

export interface WakewordInput {
  keyword: string;
  threshold: number;
  cooldown_ms: number;
}

async function fetchWakewordSettings(): Promise<WakewordSettingsData> {
  const response = await apiClient.get<WakewordSettingsData>('/api/settings/wakeword');
  return response.data;
}

async function fetchWakewordSyncStatus(): Promise<SyncStatusData> {
  const response = await apiClient.get<SyncStatusData>('/api/settings/wakeword/sync-status');
  return response.data;
}

async function saveWakewordSettings(input: WakewordInput): Promise<WakewordSettingsData> {
  const response = await apiClient.put<WakewordSettingsData>('/api/settings/wakeword', input);
  return response.data;
}

export function useWakewordSettingsQuery() {
  return useApiQuery(
    {
      queryKey: ['settings', 'wakeword'] as const,
      queryFn: fetchWakewordSettings,
      staleTime: STALE.CONFIG,
    },
    'settings.failedToLoad',
  );
}

/**
 * Polling query for wake-word sync status. Polls every 2 s while `enabled` is
 * true, stopping once all devices report `all_synced` or the caller flips
 * `enabled` back to false (e.g. after timeout).
 */
export function useWakewordSyncStatusQuery(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: ['settings', 'wakeword', 'sync-status'] as const,
      queryFn: fetchWakewordSyncStatus,
      enabled,
      staleTime: 0,
      refetchInterval: (query) => {
        const data = query.state.data;
        if (data?.all_synced) return false;
        return 2_000;
      },
    },
    'settings.failedToLoad',
  );
}

export function useSaveWakewordSettings() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: saveWakewordSettings,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.settings.all });
      },
    },
    'settings.failedToSave',
  );
}
