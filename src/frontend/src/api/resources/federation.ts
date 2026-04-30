import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';
import type { CircleTier } from '../../components/TierBadge';

export interface FederationPeer {
  id: string;
  remote_display_name: string;
  remote_pubkey: string;
  circle_tier: CircleTier | number;
  last_seen_at?: string | null;
}

export type AuditStatus = 'success' | 'failed' | 'in_progress' | string;

export interface AuditEntry {
  id: string;
  peer_pubkey: string;
  peer_display_name: string;
  query_text: string;
  answer_excerpt?: string;
  error_message?: string;
  initiated_at: string;
  finalized_at?: string;
  final_status: AuditStatus;
  verified_signature?: boolean;
}

async function fetchFederationPeers(): Promise<FederationPeer[]> {
  const response = await apiClient.get<{ peers: FederationPeer[] }>('/api/federation/peers');
  return response.data.peers ?? [];
}

async function fetchAuditEntries(args: { peerPubkey: string | null; limit: number }): Promise<AuditEntry[]> {
  const params = new URLSearchParams({ limit: String(args.limit) });
  if (args.peerPubkey) params.set('peer_pubkey', args.peerPubkey);
  const response = await apiClient.get<{ entries: AuditEntry[] }>(`/api/federation/audit?${params}`);
  return response.data.entries ?? [];
}

async function deletePeerRequest(peerId: string): Promise<void> {
  await apiClient.delete(`/api/federation/peers/${peerId}`);
}

export function useFederationPeersQuery() {
  return useApiQuery(
    {
      queryKey: keys.federation.peers(),
      queryFn: fetchFederationPeers,
      staleTime: STALE.DEFAULT,
    },
    'circles.peersCouldNotLoad',
  );
}

export function useFederationAuditQuery(peerPubkey: string | null, limit = 50) {
  return useApiQuery(
    {
      queryKey: [...keys.federation.audit(), { peerPubkey, limit }] as const,
      queryFn: () => fetchAuditEntries({ peerPubkey, limit }),
      staleTime: STALE.DEFAULT,
    },
    'federationAudit.loadFailed',
  );
}

export function useDeleteFederationPeer() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deletePeerRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.federation.peers() });
      },
    },
    'circles.peerRevokeFailed',
  );
}
