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
      month,
      year,
      force,
    }: {
      id: number;
      category: string;
      type: string;
      description: string;
      month: number;
      year: number;
      force?: boolean;
    }) => {
      const r = await apiClient.post<{
        success: boolean;
        message: string;
        already_in_simba?: boolean;
        existing?: string | null;
      }>(`/api/simba-ingest/${id}/confirm`, { category, type, description, month, year, force });
      return r.data;
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PROPOSALS_KEY }),
  });
}

export interface SendToSimbaResult {
  success: boolean;
  message: string;
  proposal_id: number | null;
  suggested_category: string | null;
  suggested_type: string | null;
  suggested_description: string;
}

/** Create (or reuse) a pending Simba proposal for an EXISTING knowledge-base
 * document — the first step of the doc-page "send to Simba" overlay, which then
 * confirms the upload in place. Idempotent on the pending state; returns the
 * suggested category/type/Bezeichnung so the overlay prefills without a second
 * fetch. Complements the folder-ingest flow, which only fires on new documents. */
export function useSendDocumentToSimba() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: async (documentId: number): Promise<SendToSimbaResult> => {
      const r = await apiClient.post<SendToSimbaResult>(
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
