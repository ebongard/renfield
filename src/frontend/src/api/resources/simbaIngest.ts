import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';

export interface SimbaProposal {
  id: number;
  document_id: number;
  filename: string;
  suggested_category: string | null;
  suggested_type: string | null;
  suggested_description: string;
}

const PROPOSALS_KEY = ['simbaIngest', 'proposals'] as const;

export function useSimbaProposalsQuery(enabled = true) {
  return useQuery({
    queryKey: PROPOSALS_KEY,
    queryFn: async (): Promise<SimbaProposal[]> => {
      const r = await apiClient.get<{ proposals: SimbaProposal[] }>('/api/simba-ingest');
      return r.data?.proposals ?? [];
    },
    enabled,
    staleTime: 30_000,
  });
}

export function useSimbaCategoriesQuery(enabled = true) {
  return useQuery({
    queryKey: ['simbaIngest', 'categories'],
    queryFn: async (): Promise<Record<string, string[]>> => {
      const r = await apiClient.get<{ categories: Record<string, string[]> }>(
        '/api/chat/upload/simba/categories',
      );
      return r.data?.categories ?? {};
    },
    enabled,
    staleTime: 5 * 60_000,
  });
}

export function useConfirmSimbaProposal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async ({
      id,
      category,
      type,
      description,
    }: {
      id: number;
      category: string;
      type: string;
      description: string;
    }) => {
      const r = await apiClient.post<{ success: boolean; message: string }>(
        `/api/simba-ingest/${id}/confirm`,
        { category, type, description },
      );
      return r.data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PROPOSALS_KEY }),
  });
}

/** Queue an EXISTING knowledge-base document for Simba review (the "send to
 * Simba" action). Complements the folder-ingest flow, which only fires on new
 * documents. Creates a pending proposal on /brain/review. */
export function useSendDocumentToSimba() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (documentId: number) => {
      const r = await apiClient.post<{ success: boolean; message: string; proposal_id: number | null }>(
        `/api/simba-ingest/from-document/${documentId}`,
      );
      return r.data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PROPOSALS_KEY }),
  });
}

export function useRejectSimbaProposal() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (id: number) => {
      await apiClient.post(`/api/simba-ingest/${id}/reject`);
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PROPOSALS_KEY }),
  });
}
