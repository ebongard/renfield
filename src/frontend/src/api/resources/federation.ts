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
  /** Time of the peer's last authenticated REQUEST to us -- not a connectivity state. */
  last_seen_at?: string | null;
  /**
   * The four fields below exist only while the instance shows every account
   * all of its active pairings (`FEDERATION_PEERS_VISIBLE_TO_ALL`). Absent,
   * the list holds the caller's own pairings only -- so "absent" reads as
   * "mine, and I may revoke it", never as `false`.
   */
  owned_by_me?: boolean;
  /** Display name of the local account that owns the pairing. */
  paired_by?: string;
  /** The CALLER asks this peer as a linked person. */
  linked?: boolean;
  can_revoke?: boolean;
  /**
   * The INSTANCE part of the peer's label: the name the far instance sent in
   * a verified attachment, else the host of its stored endpoint. Absent when
   * there is neither -- the row then shows `remote_display_name` alone.
   * Display text, never a key.
   */
  instance_name?: string | null;
  /**
   * The three fields below exist only while the instance runs the signed
   * reachability check (`FEDERATION_REACHABILITY_ENABLED`). Absent = no
   * statement about reachability at all, which is not the same as "unknown".
   */
  reachability?: PeerReachability | null;
  /** When THIS instance last sent the peer a real query. */
  last_asked_at?: string | null;
  /** A real request went either way within the last five minutes. */
  recently_active?: boolean | null;
}

/** The last signed round trip to a paired instance, as the server folds it. */
export interface PeerReachability {
  /**
   * 🛑 `(string & {})` as on `identity_basis` below: the server sends free
   * text. A state this build does not know must fall into the UNKNOWN branch,
   * not silently into "unreachable".
   */
  state: 'reachable' | 'unreachable' | 'unknown' | (string & {});
  /**
   * Machine code the UI localises: `unreachable`, `pin_mismatch`, `refused`,
   * `unsupported`, `invalid_response`, `no_endpoint`, `not_checked`; `null`
   * when reachable.
   */
  reason: string | null;
  /** Naive-UTC time of that round trip; `null` = never checked or expired. */
  checked_at: string | null;
}

export type AuditStatus = 'success' | 'failed' | 'in_progress' | string;

export interface AuditEntry {
  id: string;
  peer_pubkey: string;
  peer_display_name: string;
  /**
   * The instance part of the peer's label AS IT STOOD when the row was
   * written -- a snapshot like `peer_display_name`, never today's name.
   * `null`/absent on rows from before the instance name existed and wherever
   * no label existed at the time: the view shows the person alone then.
   */
  peer_instance_name?: string | null;
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
   * 🛑 `undefined`/`null` does NOT mean 'peer_tier' but "not recorded": on
   * asker rows this instance does not know (the far side decides), and rows
   * from before 2026-10-29 do not know the field at all. Conflating the two
   * claims the narrower tier for queries that ran with full reach.
   *
   * 🛑 `(string & {})` is deliberate, not sloppiness: server-side the column is
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

async function checkPeerRequest(peerId: string): Promise<PeerReachability> {
  const response = await apiClient.post<PeerReachability>(`/api/federation/peers/${peerId}/check`);
  return response.data;
}

/**
 * "Check now": one signed round trip to a paired instance, on demand.
 *
 * The result is written straight into the cached peer list -- the server
 * stored the same reading, so the row is current without a second request,
 * and `last_seen_at` & co. stay exactly what the list last said.
 */
export function useCheckFederationPeer() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: checkPeerRequest,
      onSuccess: (reachability, peerId) => {
        queryClient.setQueryData<FederationPeer[]>(keys.federation.peers(), (peers) =>
          peers?.map((peer) =>
            String(peer.id) === String(peerId) ? { ...peer, reachability } : peer,
          ),
        );
      },
    },
    'circles.peerReachCheckFailed',
  );
}

/**
 * A person link as the person themselves sees it.
 *
 * 🛑 Without `querier_ref`. That is the opaque band between two instances and
 * not a display value -- whoever holds it can present themselves to the far
 * side as this person. The backend does not emit it in the first place.
 */
export interface IdentityLink {
  id: number;
  peer_id: number;
  peer_display_name: string;
  /** The instance part of the peer's label; absent when there is none. */
  peer_instance_name?: string | null;
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
