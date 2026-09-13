/**
 * Per-integration ingest credentials admin API (#1218 Phase 2).
 *
 * Mints / rotates / revokes the Bearer token each pushing MCP uses. The
 * plaintext is returned exactly once, by mint and rotate — the list route never
 * returns one, and only a bcrypt hash is stored server-side.
 */
import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type IngestRoute = 'folder_ingest' | 'email_ingest';

export interface IngestCredential {
  client_id: string;
  label: string;
  route: IngestRoute;
  /** Where this client's documents land. null => the instance default. */
  owner: string | null;
  tier: number | null;
  kb_name: string | null;
  created_at: string | null;
  rotated_at: string | null;
  last_authenticated_at: string | null;
  revoked_at: string | null;
  is_enabled: boolean;
}

/**
 * The legacy shared token. `source: 'env'` means the backend re-seeds it from
 * its environment on every boot, so rotating it here would silently revert at
 * the next restart — the UI renders those read-only.
 */
export interface LegacyToken {
  route: IngestRoute;
  configured: boolean;
  source: 'env' | 'db';
}

export interface IngestCredentialList {
  credentials: IngestCredential[];
  legacy: LegacyToken[];
  enabled: boolean;
}

export interface MintPayload {
  client_id: string;
  label: string;
  route: IngestRoute;
  /** Optional sphere routing, set by the ADMIN here — never by the client. */
  kb_name?: string | null;
  tier?: number | null;
}

/** Carries the plaintext. Shown once, never re-fetchable. */
export interface MintResult {
  client_id: string;
  token: string;
}

async function fetchCredentials(): Promise<IngestCredentialList> {
  const res = await apiClient.get<IngestCredentialList>('/api/ingest-credentials');
  return res.data ?? { credentials: [], legacy: [], enabled: false };
}

async function mintRequest(input: MintPayload): Promise<MintResult> {
  const res = await apiClient.post<MintResult>('/api/ingest-credentials', input);
  return res.data;
}

async function rotateRequest(clientId: string): Promise<MintResult> {
  const res = await apiClient.post<MintResult>(
    `/api/ingest-credentials/${encodeURIComponent(clientId)}/rotate`,
  );
  return res.data;
}

async function revokeRequest(clientId: string): Promise<void> {
  await apiClient.delete(`/api/ingest-credentials/${encodeURIComponent(clientId)}`);
}

export function useIngestCredentialsQuery() {
  return useApiQuery(
    {
      queryKey: keys.integrations.ingestCredentials(),
      queryFn: fetchCredentials,
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );
}

export function useMintIngestCredential() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: mintRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.integrations.all });
      },
    },
    'common.error',
  );
}

export function useRotateIngestCredential() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: rotateRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.integrations.all });
      },
    },
    'common.error',
  );
}

export function useRevokeIngestCredential() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: revokeRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.integrations.all });
      },
    },
    'common.error',
  );
}
