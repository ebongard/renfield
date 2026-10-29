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
  /** 'asked' = diese Instanz hat gefragt, 'answered' = sie hat geantwortet. */
  direction?: 'asked' | 'answered';
  /**
   * Auf welcher Identität die Antwort beruhte.
   *
   * 🛑 `undefined`/`null` heisst NICHT 'peer_tier', sondern „nicht
   * festgehalten": bei Frager-Zeilen weiss diese Instanz es nicht (die
   * Gegenseite entscheidet), und Zeilen von vor dem 2026-10-29 kennen die
   * Angabe gar nicht. Wer das zusammenwirft, behauptet die engere Stufe fuer
   * Zuege, die mit voller Reichweite liefen.
   *
   * 🛑 `(string & {})` ist Absicht, keine Nachlaessigkeit: die Spalte ist
   * serverseitig ein freier Text, kein Aufzaehlungstyp. Ohne diesen Zusatz
   * hielte TypeScript eine Fallunterscheidung ueber die beiden bekannten
   * Werte fuer vollstaendig — und ein kuenftiger dritter Wert liefe
   * stillschweigend in den `peer_tier`-Zweig, also in die ENGERE Behauptung.
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
 * Eine Personenverknuepfung, wie sie die EIGENE Person sieht.
 *
 * 🛑 Ohne `querier_ref`. Der ist das opake Band zwischen zwei Instanzen und
 * kein Anzeigewert — wer ihn hat, kann sich bei der Gegenstelle als diese
 * Person ausgeben. Das Backend laesst ihn darum gar nicht erst heraus.
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
