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
  /** 'asked' = this instance asked, 'answered' = this instance answered. */
  direction?: 'asked' | 'answered';
  /**
   * Which identity the answer rested on.
   *
   * `undefined`/`null` does NOT mean 'peer_tier' but "not recorded": on
   * asker rows this instance does not know (the far side decides), and rows
   * from before 2026-10-29 do not know the field at all. Conflating the two
   * claims the narrower tier for queries that ran with full reach.
   *
   * `(string & {})` is deliberate, not sloppiness: server-side the column is
   * free text, not an enum. Without it TypeScript would consider a switch over
   * the two known values exhaustive -- and a future third value would run
   * silently into the `peer_tier` branch, i.e. into the NARROWER claim.
   */
  identity_basis?: 'mapped' | 'peer_tier' | (string & {}) | null;
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

/**
 * A person link as the person themselves sees it.
 *
 * Without `querier_ref`. That is the opaque band between two instances and
 * not a display value -- whoever holds it can present themselves to the far
 * side as this person. The backend does not emit it in the first place.
 */
export interface IdentityLink {
  id: number;
  peer_id: number;
  peer_display_name: string;
  created_at: string;
}

async function fetchMyIdentityLinks(): Promise<IdentityLink[]> {
  const response = await apiClient.get<IdentityLink[]>('/api/federation/links/mine');
  return response.data ?? [];
}

async function deleteIdentityLinkRequest(linkId: number): Promise<void> {
  await apiClient.delete(`/api/federation/links/${linkId}`);
}

export function useMyIdentityLinksQuery() {
  return useApiQuery(
    {
      queryKey: keys.federation.links(),
      queryFn: fetchMyIdentityLinks,
      staleTime: STALE.DEFAULT,
    },
    'circles.linksCouldNotLoad',
  );
}

export function useDeleteIdentityLink() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteIdentityLinkRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.federation.links() });
      },
    },
    'circles.linkRevokeFailed',
  );
}
