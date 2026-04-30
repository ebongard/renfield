import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';
import type { CircleTier } from '../../components/TierBadge';

export type AtomType = 'kb_document' | 'kg_node' | 'kg_edge' | 'conversation_memory';

export interface AtomMatch {
  atom: {
    atom_id: string;
    atom_type: AtomType;
    tier?: CircleTier | number;
  };
  score: number;
  snippet: string;
  rank: number;
}

export interface ReviewAtom {
  atom_id: string;
  atom_type: AtomType;
  tier?: CircleTier | number;
  policy?: { tier?: CircleTier | number; [key: string]: unknown };
  title?: string;
  preview?: string;
  created_at?: string;
}

async function fetchAtomSearch(query: string): Promise<AtomMatch[]> {
  const response = await apiClient.get<AtomMatch[]>('/api/atoms', {
    params: { q: query, top_k: 20 },
  });
  return response.data ?? [];
}

async function fetchAtomsForReview(days: number): Promise<ReviewAtom[]> {
  const response = await apiClient.get<ReviewAtom[]>('/api/circles/me/atoms-for-review', {
    params: { days, limit: 50 },
  });
  return response.data ?? [];
}

interface PatchAtomTierArgs {
  atomId: string;
  policy: Record<string, unknown>;
}

async function patchAtomTierRequest({ atomId, policy }: PatchAtomTierArgs): Promise<void> {
  await apiClient.patch(`/api/atoms/${atomId}/tier`, { policy });
}

export function useAtomSearchQuery(query: string) {
  return useApiQuery(
    {
      queryKey: keys.brain.search(query),
      queryFn: () => fetchAtomSearch(query),
      staleTime: STALE.DEFAULT,
      enabled: query.trim().length > 0,
    },
    'circles.couldNotLoad',
  );
}

export function useAtomsForReviewQuery(days: number) {
  return useApiQuery(
    {
      queryKey: [...keys.brain.review(), { days }] as const,
      queryFn: () => fetchAtomsForReview(days),
      staleTime: STALE.DEFAULT,
    },
    'circles.couldNotLoad',
  );
}

export function usePatchAtomTier() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: patchAtomTierRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.brain.all });
      },
    },
    'circles.couldNotSave',
  );
}
