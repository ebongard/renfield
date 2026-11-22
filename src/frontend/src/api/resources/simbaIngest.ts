import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import type { AxiosError } from 'axios';

import { keys } from '../keys';
import apiClient from '../../utils/axios';

/** A file the Simba portal can never accept — data; the UI renders the text. */
export interface SimbaUploadBlocked {
  code: 'too_large';
  size_mb: number;
  max_mb: number;
  /** The upload context it was judged against — the portal's limit is per
   *  Kategorie + Typ, so the finding holds for this selection only. */
  category: string;
  type: string;
}

/** The finding inside a 422 from confirm / from-document (#1609), or null for
 *  any other error. The body is the same data the list carries. */
export function uploadBlockedOf(err: unknown): SimbaUploadBlocked | null {
  const detail = (err as AxiosError<{ detail?: unknown }>)?.response?.data?.detail;
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null;
  const d = detail as Partial<Record<keyof SimbaUploadBlocked, unknown>>;
  if (
    d.code !== 'too_large' ||
    typeof d.size_mb !== 'number' ||
    typeof d.max_mb !== 'number' ||
    typeof d.category !== 'string' ||
    typeof d.type !== 'string'
  ) {
    return null;
  }
  return { code: 'too_large', size_mb: d.size_mb, max_mb: d.max_mb, category: d.category, type: d.type };
}

export interface SimbaProposal {
  id: number;
  document_id: number;
  filename: string;
  suggested_category: string | null;
  suggested_type: string | null;
  suggested_description: string;
  /** Booking period (Zeitraum) from the document's date; null → form uses now. */
  suggested_month?: number | null;
  suggested_year?: number | null;
  /** Why it can never land in Simba (over the portal's file size limit, #1609);
   * the form shows it instead of the confirm. null = it fits, or unknown. */
  upload_blocked?: SimbaUploadBlocked | null;
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
    onSuccess: (data) => {
      void queryClient.invalidateQueries({ queryKey: PROPOSALS_KEY });
      // A landed upload flips the document's `in_simba` state → refresh the
      // knowledge documents list so the row's Simba status icon updates without
      // a manual reload. Both the doc-page send overlay and the /brain/review
      // section confirm through here. Gated on an actual upload — an
      // already-in-Simba (no force) or rejected confirm returns success:false
      // and changes no document state, so it needs no documents refetch.
      if (data?.success) {
        void queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
      }
    },
    // A refused confirm (e.g. the file is over the portal's limit, #1609) is
    // news about the queue too: the list re-reads its findings.
    onError: () => {
      void queryClient.invalidateQueries({ queryKey: PROPOSALS_KEY });
    },
  });
}

export interface SendToSimbaResult {
  success: boolean;
  message: string;
  proposal_id: number | null;
  suggested_category: string | null;
  suggested_type: string | null;
  suggested_description: string;
  suggested_month?: number | null;
  suggested_year?: number | null;
  upload_blocked?: SimbaUploadBlocked | null;
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
