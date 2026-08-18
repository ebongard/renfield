// PDF-split review proposals (docs/design/pdf-split.md, PR2).
// Owner review queue for uncertain multi-document PDF splits: list/detail
// queries + approve (optionally with edited page ranges) / reject mutations.
// Approve/reject only persist state — the split executes in the worker.
import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export interface PdfSplitPiece {
  start_page: number;
  end_page: number;
  title: string;
  doc_type: string;
  confidence: number;
}

export interface PdfSplitPageSignal {
  page: number;
  snippet: string;
  quality_ok: boolean;
  via_vlm: boolean;
}

export interface PdfSplitProposal {
  id: number;
  document_id: number;
  document_filename: string;
  status: 'pending' | 'approved' | 'rejected';
  page_count: number;
  overall_confidence: number;
  created_at: string;
  documents: PdfSplitPiece[];
}

export interface PdfSplitProposalDetail extends PdfSplitProposal {
  page_signals: PdfSplitPageSignal[];
}

async function fetchProposals(): Promise<PdfSplitProposal[]> {
  const response = await apiClient.get<{ proposals: PdfSplitProposal[]; total: number }>(
    '/api/pdf-split/proposals',
  );
  return response.data.proposals;
}

async function fetchProposalDetail(id: number): Promise<PdfSplitProposalDetail> {
  const response = await apiClient.get<PdfSplitProposalDetail>(
    `/api/pdf-split/proposals/${id}`,
  );
  return response.data;
}

async function approveRequest(params: {
  id: number;
  documents?: PdfSplitPiece[];
}): Promise<PdfSplitProposal> {
  const body = params.documents ? { documents: params.documents } : {};
  const response = await apiClient.post<PdfSplitProposal>(
    `/api/pdf-split/proposals/${params.id}/approve`,
    body,
  );
  return response.data;
}

async function rejectRequest(id: number): Promise<PdfSplitProposal> {
  const response = await apiClient.post<PdfSplitProposal>(
    `/api/pdf-split/proposals/${id}/reject`,
  );
  return response.data;
}

/** Authenticated page-thumbnail fetch (an <img src> cannot carry the JWT
 *  header) — callers turn the blob into an object URL and revoke it on
 *  unmount. */
export async function fetchProposalPageBlob(
  proposalId: number,
  page: number,
): Promise<Blob> {
  const response = await apiClient.get(
    `/api/pdf-split/proposals/${proposalId}/pages/${page}`,
    { responseType: 'blob' },
  );
  return response.data as Blob;
}

export function usePdfSplitProposalsQuery(enabled = true) {
  return useApiQuery(
    {
      queryKey: keys.pdfSplit.proposals(),
      queryFn: fetchProposals,
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'pdfSplit.couldNotLoad',
  );
}

export function usePdfSplitProposalDetailQuery(id: number | null) {
  return useApiQuery(
    {
      queryKey: keys.pdfSplit.proposal(id ?? -1),
      queryFn: () => fetchProposalDetail(id as number),
      staleTime: STALE.DEFAULT,
      enabled: id !== null,
    },
    'pdfSplit.couldNotLoad',
  );
}

function invalidate(queryClient: ReturnType<typeof useQueryClient>) {
  void queryClient.invalidateQueries({ queryKey: keys.pdfSplit.all });
}

export function useApprovePdfSplitProposal() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: approveRequest, onSuccess: () => invalidate(queryClient) },
    'common.error',
  );
}

export function useRejectPdfSplitProposal() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: rejectRequest, onSuccess: () => invalidate(queryClient) },
    'common.error',
  );
}
