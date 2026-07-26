import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type AuditMode = 'new_only' | 'full';
export type FixMode = 'review' | 'auto_threshold' | 'auto_all';

export interface AuditStatus {
  running: boolean;
  progress?: number;
  total?: number;
}

/** Canonical field names for the review overlay — must match the backend
 *  PaperlessAuditService.EDITABLE_FIELDS. */
export type EditableField =
  | 'title'
  | 'correspondent'
  | 'document_type'
  | 'tags'
  | 'date'
  | 'storage_path'
  | 'custom_fields';

/** Manual edits the user made to the suggested values, keyed by canonical field.
 *  Scalar fields carry a string, tags a string[], custom_fields an object. */
export type ReviewOverrides = Partial<{
  title: string;
  correspondent: string;
  document_type: string;
  date: string;
  storage_path: string;
  tags: string[];
  custom_fields: Record<string, unknown>;
}>;

export interface AuditResult {
  id: number;
  paperless_doc_id: number;
  current_title?: string | null;
  suggested_title?: string | null;
  current_correspondent?: string | null;
  suggested_correspondent?: string | null;
  current_document_type?: string | null;
  suggested_document_type?: string | null;
  current_date?: string | null;
  suggested_date?: string | null;
  current_storage_path?: string | null;
  suggested_storage_path?: string | null;
  current_tags?: string[] | null;
  current_custom_fields?: Record<string, unknown> | null;
  suggested_custom_fields?: Record<string, unknown> | null;
  detected_language?: string | null;
  suggested_tags?: string[];
  missing_fields?: string[];
  confidence?: number | null;
  ocr_quality?: number;
  ocr_issues?: string;
  content_completeness?: number;
  completeness_issues?: string;
  status?: string;
  // Review overlay (PR: selective + manual edits). Both null = no review →
  // apply uses all suggested_* changes.
  user_overrides?: ReviewOverrides | null;
  field_selection?: EditableField[] | null;
  // Low-quality OCR signal (resolved from the renfield Document; null/false
  // for Paperless-only docs that were never ingested into the KB).
  low_quality_ocr?: boolean;
  chunks_dropped?: number | null;
  chunks_total?: number | null;
  quality_ignored?: boolean;
  renfield_document_id?: number | null;
}

export interface AuditStats {
  total_audited?: number;
  changes_needed?: number;
  applied?: number;
  skipped?: number;
  pending?: number;
  failed?: number;
  missing_metadata_count?: number;
  duplicate_groups?: number;
  avg_confidence?: number;
  ocr_quality_distribution?: Record<string, number>;
  ocr_distribution?: Record<string, number>;
  language_distribution?: Record<string, number>;
  completeness_distribution?: Record<string, number>;
}

export interface DuplicateDoc {
  id: number;
  paperless_doc_id: number;
  current_title?: string | null;
  current_correspondent?: string | null;
  duplicate_score?: number | null;
}

export interface DuplicateGroup {
  group_id: string;
  documents: DuplicateDoc[];
}

export interface CorrespondentVariant {
  name: string;
  similarity: number;
}

export interface CorrespondentCluster {
  canonical: string;
  variants: CorrespondentVariant[];
}

interface ResultsListResponse {
  results: AuditResult[];
  total: number;
}

async function fetchStatus(): Promise<AuditStatus> {
  const response = await apiClient.get<AuditStatus>('/api/admin/paperless-audit/status');
  return response.data;
}

async function fetchResults(params: Record<string, unknown>): Promise<ResultsListResponse> {
  const response = await apiClient.get<{ results?: AuditResult[]; total?: number } | AuditResult[]>(
    '/api/admin/paperless-audit/results',
    { params },
  );
  const data = response.data;
  if (Array.isArray(data)) {
    return { results: data, total: data.length };
  }
  const results = data.results ?? [];
  return { results, total: data.total ?? results.length };
}

async function fetchStats(): Promise<AuditStats> {
  const response = await apiClient.get<AuditStats>('/api/admin/paperless-audit/stats');
  return response.data;
}

async function fetchDuplicateGroups(): Promise<DuplicateGroup[]> {
  const response = await apiClient.get<DuplicateGroup[]>('/api/admin/paperless-audit/duplicate-groups');
  return response.data ?? [];
}

async function fetchCorrespondentClusters(threshold: number): Promise<CorrespondentCluster[]> {
  const response = await apiClient.get<{ clusters?: CorrespondentCluster[] }>(
    '/api/admin/paperless-audit/correspondent-normalization',
    { params: { threshold } },
  );
  return response.data.clusters ?? [];
}

interface StartAuditInput {
  mode: AuditMode;
  fix_mode: FixMode;
  confidence_threshold: number;
}

async function startAuditRequest(input: StartAuditInput): Promise<void> {
  await apiClient.post('/api/admin/paperless-audit/start', input);
}

async function stopAuditRequest(): Promise<void> {
  await apiClient.post('/api/admin/paperless-audit/stop', {});
}

async function applyResultsRequest(ids: number[]): Promise<void> {
  await apiClient.post('/api/admin/paperless-audit/apply', { result_ids: ids });
}

async function skipResultsRequest(ids: number[]): Promise<void> {
  await apiClient.post('/api/admin/paperless-audit/skip', { result_ids: ids });
}

/** Selectable Paperless taxonomy for the review lookup fields. */
export interface TaxonomyResponse {
  correspondents: string[];
  document_types: string[];
  tags: string[];
  storage_paths: string[];
  /** Which fields can create a typed-new value on apply (mirrors the backend
   *  auto-create gating); a false field is restricted to existing values. */
  allow_create: {
    correspondent: boolean;
    document_type: boolean;
    tags: boolean;
    storage_path: boolean;
  };
}

async function fetchTaxonomy(): Promise<TaxonomyResponse> {
  const { data } = await apiClient.get<TaxonomyResponse>('/api/admin/paperless-audit/taxonomy');
  return data;
}

export interface UpdateReviewInput {
  id: number;
  // Each field REPLACES the stored overlay; pass null to leave it untouched.
  overrides?: ReviewOverrides | null;
  field_selection?: EditableField[] | null;
}

async function updateReviewRequest(input: UpdateReviewInput): Promise<AuditResult> {
  const { id, overrides, field_selection } = input;
  const { data } = await apiClient.patch<AuditResult>(
    `/api/admin/paperless-audit/results/${id}`,
    { overrides, field_selection },
  );
  return data;
}

async function reOcrRequest(ids: number[]): Promise<void> {
  // Re-OCR is synchronous server-side and now runs the local OCR stack PLUS a VLM
  // fallback (~40s+ per document for a rotated/poor scan). The shared apiClient's
  // 30s default would abort it client-side — the request completes on the server
  // (Paperless gets the cleaned text) but the UI shows "Fehler beim Laden der
  // Audit-Daten". timeout:0 lets the client wait for the real response.
  await apiClient.post(
    '/api/admin/paperless-audit/re-ocr',
    { result_ids: ids },
    { timeout: 0 },
  );
}

async function markQualityIgnoredRequest(input: { result_ids: number[]; ignored: boolean }): Promise<void> {
  await apiClient.post('/api/admin/paperless-audit/quality-ignore', input);
}

async function detectDuplicatesRequest(): Promise<void> {
  await apiClient.post('/api/admin/paperless-audit/detect-duplicates', {});
}

export interface ReviewFilter {
  page: number;
  perPage: number;
  sortBy: string | null;
  sortOrder: 'asc' | 'desc';
  search: string;
}

export interface OcrFilter {
  page: number;
  perPage: number;
}

export interface CompletenessFilter {
  page: number;
  perPage: number;
}

export interface LowQualityFilter {
  page: number;
  perPage: number;
}

function reviewParams(filter: ReviewFilter): Record<string, unknown> {
  const params: Record<string, unknown> = {
    status: 'pending',
    changes_needed: true,
    per_page: filter.perPage,
    page: filter.page + 1,
  };
  if (filter.sortBy) {
    params.sort_by = filter.sortBy;
    params.sort_order = filter.sortOrder;
  }
  if (filter.search.trim()) {
    params.search = filter.search.trim();
  }
  return params;
}

export function useAuditStatusQuery() {
  return useApiQuery(
    {
      queryKey: keys.paperlessAudit.status(),
      queryFn: fetchStatus,
      staleTime: STALE.LIVE,
      // refetchInterval reads from the query's own data — when an audit is
      // running, poll every 2s; otherwise stay idle. RQ re-evaluates this
      // callback after each fetch, so polling automatically stops when the
      // audit completes.
      refetchInterval: (query) => (query.state.data?.running ? 2000 : false),
    },
    'paperlessAudit.error',
  );
}

export function useReviewResultsQuery(filter: ReviewFilter, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.paperlessAudit.results(), 'review', filter] as const,
      queryFn: () => fetchResults(reviewParams(filter)),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

export function useOcrResultsQuery(filter: OcrFilter, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.paperlessAudit.results(), 'ocr', filter] as const,
      queryFn: () =>
        fetchResults({ ocr_quality_max: 2, per_page: filter.perPage, page: filter.page + 1 }),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

export function useLowQualityResultsQuery(filter: LowQualityFilter, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.paperlessAudit.results(), 'lowquality', filter] as const,
      queryFn: () =>
        fetchResults({ low_quality_only: true, per_page: filter.perPage, page: filter.page + 1 }),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

export function useCompletenessResultsQuery(filter: CompletenessFilter, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.paperlessAudit.results(), 'completeness', filter] as const,
      queryFn: () =>
        fetchResults({ completeness_max: 2, per_page: filter.perPage, page: filter.page + 1 }),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

export function useAuditStatsQuery(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.paperlessAudit.stats(),
      queryFn: fetchStats,
      // Stats are an aggregate that only changes after a mutation — the
      // mutations already invalidate keys.paperlessAudit.all on success,
      // so STALE.DEFAULT is correct here. STALE.LIVE would refetch every
      // 5s for no benefit.
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

export function useDuplicateGroupsQuery(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.paperlessAudit.duplicateGroups(),
      queryFn: fetchDuplicateGroups,
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

export function useCorrespondentClustersQuery(threshold: number, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: [...keys.paperlessAudit.all, 'correspondents', threshold] as const,
      queryFn: () => fetchCorrespondentClusters(threshold),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'paperlessAudit.error',
  );
}

function invalidateAudit(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: keys.paperlessAudit.all });
}

export function useStartAudit() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: startAuditRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}

export function useStopAudit() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: stopAuditRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}

export function useApplyResults() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: applyResultsRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}

export function useSkipResults() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: skipResultsRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}

/** Selectable Paperless taxonomy for the lookup fields — cached (changes rarely),
 *  only fetched while the review tab is open. */
export function useTaxonomyQuery(enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.paperlessAudit.taxonomy(),
      queryFn: fetchTaxonomy,
      staleTime: STALE.CONFIG,
      enabled,
    },
    'paperlessAudit.error',
  );
}

/** Persist a manual review overlay (edited values + per-field apply selection).
 *  Deliberately does NOT invalidate the audit query: this fires on blur/toggle
 *  while the user is editing, and a refetch would clobber in-progress local
 *  drafts on other rows. apply/skip invalidate when the batch settles. */
export function useUpdateReview() {
  return useApiMutation(
    {
      mutationFn: updateReviewRequest,
    },
    'paperlessAudit.error',
  );
}

export function useReOcr() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: reOcrRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}

export function useMarkQualityIgnored() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: markQualityIgnoredRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}

export function useDetectDuplicates() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: detectDuplicatesRequest,
      onSuccess: () => invalidateAudit(queryClient),
    },
    'paperlessAudit.error',
  );
}
