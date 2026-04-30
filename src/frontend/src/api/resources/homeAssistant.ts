import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export interface HaEntityAttributes {
  friendly_name?: string;
  brightness?: number;
  [key: string]: unknown;
}

export interface HaEntity {
  entity_id: string;
  state: string;
  attributes?: HaEntityAttributes;
}

async function fetchStates(): Promise<HaEntity[]> {
  const response = await apiClient.get<{ states: HaEntity[] }>('/api/homeassistant/states');
  return response.data.states ?? [];
}

async function toggleEntityRequest(entityId: string): Promise<void> {
  await apiClient.post(`/api/homeassistant/toggle/${entityId}`);
}

export function useHaStatesQuery() {
  return useApiQuery(
    {
      queryKey: keys.homeAssistant.states(),
      queryFn: fetchStates,
      staleTime: STALE.LIVE,
    },
    'homeassistant.loadingDevices',
  );
}

export function useToggleHaEntity() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: toggleEntityRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.homeAssistant.all });
      },
    },
    'common.error',
  );
}
