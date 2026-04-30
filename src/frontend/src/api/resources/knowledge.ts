import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';
import type { DocPages } from '../../components/knowledge/StatusBadge';

export type DocStatus = 'pending' | 'processing' | 'completed' | 'failed';
export type StatusFilter = DocStatus | 'all';

export interface KnowledgeBaseRow {
  id: number;
  name: string;
  description?: string | null;
  document_count?: number;
}

export interface KnowledgeStats {
  document_count: number;
  completed_documents: number;
  chunk_count: number;
  knowledge_base_count: number;
}

export interface SearchChunk {
  content: string;
  page_number?: number | null;
  section_title?: string | null;
}

export interface SearchResultDocument {
  id: number;
  filename: string;
}

export interface SearchResultChunk {
  document: SearchResultDocument;
  chunk: SearchChunk;
  similarity: number;
}

export interface DocumentRow {
  id: number;
  filename: string;
  status: DocStatus;
  stage?: string | null;
  pages?: DocPages | null;
  queue_position?: number | null;
  file_type?: string;
  knowledge_base_id?: number;
  error_message?: string;
  size_bytes?: number;
  file_size?: number;
  page_count?: number;
  chunk_count?: number;
  title?: string;
  created_at?: string;
}

interface DocsFilter {
  knowledgeBaseId: number | null;
  statusFilter: StatusFilter;
}

async function fetchDocuments({ knowledgeBaseId, statusFilter }: DocsFilter): Promise<DocumentRow[]> {
  const params: Record<string, unknown> = {};
  if (knowledgeBaseId) params.knowledge_base_id = knowledgeBaseId;
  if (statusFilter !== 'all') params.status = statusFilter;
  const response = await apiClient.get<DocumentRow[]>('/api/knowledge/documents', { params });
  return response.data ?? [];
}

async function fetchKnowledgeBases(): Promise<KnowledgeBaseRow[]> {
  const response = await apiClient.get<KnowledgeBaseRow[]>('/api/knowledge/bases');
  return response.data ?? [];
}

async function fetchStats(): Promise<KnowledgeStats> {
  const response = await apiClient.get<KnowledgeStats>('/api/knowledge/stats');
  return response.data;
}

interface SearchInput {
  query: string;
  knowledgeBaseId: number | null;
  topK?: number;
}

async function searchKnowledgeRequest(input: SearchInput): Promise<SearchResultChunk[]> {
  const response = await apiClient.post<{ results: SearchResultChunk[] }>('/api/knowledge/search', {
    query: input.query,
    top_k: input.topK ?? 5,
    knowledge_base_id: input.knowledgeBaseId,
  });
  return response.data.results ?? [];
}

interface CreateKbInput {
  name: string;
  description: string | null;
}

async function createKnowledgeBaseRequest(input: CreateKbInput): Promise<KnowledgeBaseRow> {
  const response = await apiClient.post<KnowledgeBaseRow>('/api/knowledge/bases', input);
  return response.data;
}

async function deleteKnowledgeBaseRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/knowledge/bases/${id}`);
}

async function deleteDocumentRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/knowledge/documents/${id}`);
}

async function reindexDocumentRequest(id: number): Promise<void> {
  await apiClient.post(`/api/knowledge/documents/${id}/reindex`);
}

interface MoveDocumentsInput {
  documentIds: number[];
  targetKbId: number;
}

async function moveDocumentsRequest({ documentIds, targetKbId }: MoveDocumentsInput): Promise<{ moved_count: number }> {
  const response = await apiClient.post<{ moved_count: number }>('/api/knowledge/documents/move', {
    document_ids: documentIds,
    target_knowledge_base_id: targetKbId,
  });
  return response.data;
}

export function useKnowledgeDocumentsQuery(filter: DocsFilter) {
  return useApiQuery(
    {
      queryKey: [...keys.knowledge.list(), { filter }] as const,
      queryFn: () => fetchDocuments(filter),
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );
}

export function useKnowledgeBasesQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.knowledge.all, 'bases'] as const,
      queryFn: fetchKnowledgeBases,
      staleTime: STALE.CONFIG,
    },
    'common.error',
  );
}

export function useKnowledgeStatsQuery() {
  return useApiQuery(
    {
      queryKey: [...keys.knowledge.all, 'stats'] as const,
      queryFn: fetchStats,
      staleTime: STALE.DEFAULT,
    },
    'common.error',
  );
}

export function useSearchKnowledge() {
  return useApiMutation(
    {
      mutationFn: searchKnowledgeRequest,
    },
    'common.error',
  );
}

export function useCreateKnowledgeBase() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createKnowledgeBaseRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
      },
    },
    'common.error',
  );
}

export function useDeleteKnowledgeBase() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteKnowledgeBaseRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
      },
    },
    'common.error',
  );
}

export function useDeleteKnowledgeDocument() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteDocumentRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
      },
    },
    'knowledge.deleteFailed',
  );
}

export function useReindexKnowledgeDocument() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: reindexDocumentRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
      },
    },
    'knowledge.reindexFailed',
  );
}

export function useMoveKnowledgeDocuments() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: moveDocumentsRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.knowledge.all });
      },
    },
    'common.error',
  );
}
