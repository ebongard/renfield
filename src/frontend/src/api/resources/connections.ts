/**
 * Connections — per-user tool credentials (per-user data scoping).
 *
 * Talks to Reva's /api/connections. Secrets are write-only: the list never
 * returns a token, only a `connected` boolean per provider.
 */
import { useQueryClient } from '@tanstack/react-query';
import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export interface ConnectionProvider {
  provider_key: string;
  display_name?: string;
  descriptor?: string;
  credential_type?: string;
  read_only?: boolean;
  mint_url?: string;
  help?: string;
  connected: boolean;
}

async function fetchConnections(): Promise<ConnectionProvider[]> {
  const response = await apiClient.get<ConnectionProvider[]>('/api/connections');
  return response.data ?? [];
}

async function connectRequest(input: { providerKey: string; secret: string }): Promise<void> {
  await apiClient.put(`/api/connections/${encodeURIComponent(input.providerKey)}`, {
    secret: input.secret,
  });
}

async function disconnectRequest(providerKey: string): Promise<void> {
  await apiClient.delete(`/api/connections/${encodeURIComponent(providerKey)}`);
}

export function useConnections() {
  return useApiQuery<ConnectionProvider[]>(
    {
      queryKey: keys.connections.list(),
      queryFn: fetchConnections,
      staleTime: STALE.DEFAULT,
    },
    'connections.errors.load',
  );
}

export function useConnect() {
  const queryClient = useQueryClient();
  return useApiMutation<void, { providerKey: string; secret: string }>(
    {
      mutationFn: connectRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.connections.all }),
    },
    'connections.errors.connect',
  );
}

export function useDisconnect() {
  const queryClient = useQueryClient();
  return useApiMutation<void, string>(
    {
      mutationFn: disconnectRequest,
      onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.connections.all }),
    },
    'connections.errors.disconnect',
  );
}
