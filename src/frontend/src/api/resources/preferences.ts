import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

async function fetchLanguage(): Promise<{ language: string | null }> {
  try {
    const response = await apiClient.get<{ language?: string }>('/api/preferences/language');
    return { language: response.data.language ?? null };
  } catch {
    return { language: null };
  }
}

async function setLanguageRequest(code: string): Promise<void> {
  await apiClient.put('/api/preferences/language', { language: code });
}

export function useLanguagePreferenceQuery(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.preferences.language(),
      queryFn: fetchLanguage,
      staleTime: STALE.CONFIG,
      enabled,
    },
    'common.error',
  );
}

export function useSetLanguagePreference() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: setLanguageRequest,
      onSuccess: (_, code) => {
        queryClient.setQueryData(keys.preferences.language(), { language: code });
      },
    },
    'common.error',
  );
}

// --- Own number for calls (#1683) ---------------------------------------------------------

export interface CallNumberStatus {
  number: string | null;
  verified_at: string | null;
  pending: string | null;
}

async function fetchCallNumber(): Promise<CallNumberStatus> {
  const response = await apiClient.get<CallNumberStatus>('/api/preferences/call-number');
  return response.data;
}

export function useCallNumberQuery(enabled: boolean) {
  return useApiQuery(
    { queryKey: keys.preferences.callNumber(), queryFn: fetchCallNumber, enabled },
    'common.error',
  );
}

function useCallNumberMutation<T>(fn: (arg: T) => Promise<CallNumberStatus>) {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: fn,
      onSuccess: (data) => queryClient.setQueryData(keys.preferences.callNumber(), data),
    },
    'common.error',
  );
}

export function useStartCallNumber() {
  return useCallNumberMutation(async (number: string) =>
    (await apiClient.put<CallNumberStatus>('/api/preferences/call-number', { number })).data,
  );
}

export function useVerifyCallNumber() {
  return useCallNumberMutation(async (code: string) =>
    (await apiClient.post<CallNumberStatus>('/api/preferences/call-number/verify', { code })).data,
  );
}

export function useDeleteCallNumber() {
  return useCallNumberMutation(async (_: void) =>
    (await apiClient.delete<CallNumberStatus>('/api/preferences/call-number')).data,
  );
}
