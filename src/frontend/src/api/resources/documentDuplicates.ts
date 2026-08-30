import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

// KB near-duplicate DOCUMENT review (#1170, Phase 2). Mirrors the KG merge-proposal
// resource: propose-only detection, owner resolves each pair on /brain/review by
// approving (survivor + per-pair supersede-vs-delete) or rejecting.

export type DuplicateResolution = 'supersede' | 'delete';

export interface DuplicateDocBrief {
  id: number;
  name: string;
  paperless_document_id: number | null;
  created_at: string | null;
}

export interface DocumentDuplicateProposal {
  id: number;
  signal: string;
  shared_key: string | null;
  similarity: number;
  suggested_survivor_id: number | null;
  document_a: DuplicateDocBrief;
  document_b: DuplicateDocBrief;
}

async function fetchDocumentDuplicates(): Promise<DocumentDuplicateProposal[]> {
  const response = await apiClient.get<{ proposals: DocumentDuplicateProposal[] }>(
    '/api/document-duplicates',
  );
  return response.data.proposals;
}

async function approveDocumentDuplicateRequest(input: {
  id: number;
  resolution: DuplicateResolution;
  survivorId: number;
}): Promise<void> {
  await apiClient.post(`/api/document-duplicates/${input.id}/approve`, {
    resolution: input.resolution,
    survivor_id: input.survivorId,
  });
}

async function rejectDocumentDuplicateRequest(id: number): Promise<void> {
  await apiClient.post(`/api/document-duplicates/${id}/reject`, {});
}

function invalidate(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: keys.documentDuplicates.all });
}

export function useDocumentDuplicatesQuery(enabled = true) {
  return useApiQuery(
    {
      queryKey: keys.documentDuplicates.proposals(),
      queryFn: fetchDocumentDuplicates,
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'common.error',
  );
}

export function useApproveDocumentDuplicate() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: approveDocumentDuplicateRequest, onSuccess: () => invalidate(queryClient) },
    'common.error',
  );
}

export function useRejectDocumentDuplicate() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: rejectDocumentDuplicateRequest, onSuccess: () => invalidate(queryClient) },
    'common.error',
  );
}
