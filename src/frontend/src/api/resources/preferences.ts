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
