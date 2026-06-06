import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';
import type { CircleTier } from '../../components/TierBadge';

export type AtomType =
  | 'kb_document'
  | 'kg_node'
  | 'kg_edge'
  | 'conversation_memory'
  | 'document_fact';

export interface AtomMatch {
  atom: {
    atom_id: string;
    atom_type: AtomType;
    tier?: CircleTier | number;
    /**
     * Per-type source fields the detail drawer reads (already on the wire via
     * AtomResponse.payload): kb_document → {document_id, document_title,
     * document_filename}; document_fact → {fact_id, document_id, kind, value,
     * obligation_date, amount_value, amount_currency, legal_gate, source};
     * kg_node → {entity_id, name, entity_type}; kg_edge → {relation_id,
     * subject_name, predicate, object_name}; conversation_memory → {memory_id,
     * content, category}.
     */
    payload?: Record<string, unknown>;
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

/** A single Schicht A fact — mirrors backend `DocumentFactResponse`. */
export type FactCategory = 'identifier' | 'obligation' | 'universal';
export type FactSource = 'deterministic' | 'llm' | null;

export interface DocumentFact {
  id: number;
  document_id: number;
  atom_id: string | null;
  category: FactCategory | string;
  kind: string;
  value: string;
  normalized_value: string | null;
  excerpt: string | null;
  obligation_date: string | null;
  amount_value: number | null;
  amount_currency: string | null;
  legal_gate: boolean;
  payment_method: string | null;
  confidence: number | null;
  source: FactSource;
  circle_tier: number;
  /**
   * The asker's per-user Bestätigt state from the server ledger (only present on
   * the obligations() query; absent/false on the facts-for-document query).
   */
  confirmed?: boolean;
  /** This fact's circle_tier was set independently of the parent document. */
  tier_overridden?: boolean;
}

export interface ObligationsFilter {
  dueBefore?: string | null;
  limit?: number;
  offset?: number;
}

export interface CalendarOption {
  name: string;
  label: string;
}

/** The user's obligation-calendar sync preference + the calendars they can pick. */
export interface ObligationCalendarPref {
  calendar_name: string | null;
  available: CalendarOption[];
}

/** Frontend-visible backend feature flags (allowlist — see api/routes/config.py). */
export interface FeatureFlags {
  schicht_a_extraction_enabled: boolean;
  /** Gates the unified /wissen workspace nav + routing (D10). */
  wissen_workspace_enabled: boolean;
}

async function fetchAtomSearch(query: string): Promise<AtomMatch[]> {
  const response = await apiClient.get<AtomMatch[]>('/api/atoms', {
    params: { q: query, top_k: 20 },
  });
  return response.data ?? [];
}

async function fetchAtomById(atomId: string): Promise<AtomMatch> {
  // GET /api/atoms/{id} → AtomResponse (atom + payload). 404 for not-found /
  // not-authorized / synthetic non-table ids (kg_node:*, kg_edge:*).
  const response = await apiClient.get<AtomMatch['atom']>(`/api/atoms/${encodeURIComponent(atomId)}`);
  return { atom: response.data, snippet: '', score: 0, rank: 0 };
}

async function fetchAtomsForReview(days: number): Promise<ReviewAtom[]> {
  const response = await apiClient.get<ReviewAtom[]>('/api/circles/me/atoms-for-review', {
    params: { days, limit: 50 },
  });
  return response.data ?? [];
}

async function fetchDocumentFacts(documentId: number): Promise<DocumentFact[]> {
  const response = await apiClient.get<DocumentFact[]>(
    `/api/atoms/documents/${documentId}/facts`,
  );
  return response.data ?? [];
}

/**
 * Build the iCalendar (.ics) export URL for obligations with the current
 * horizon. Used by the agenda's "Export" button for a browser-native download
 * (mirrors the trajectory JSONL export); the backend circle-filters + caps it.
 */
export function buildObligationsIcsUrl(filter: { dueBefore?: string | null } = {}): string {
  const params = new URLSearchParams();
  if (filter.dueBefore) params.set('due_before', filter.dueBefore);
  const qs = params.toString();
  return `/api/atoms/obligations/export.ics${qs ? `?${qs}` : ''}`;
}

async function fetchObligations(filter: ObligationsFilter): Promise<DocumentFact[]> {
  const params: Record<string, unknown> = {
    limit: filter.limit ?? 200,
    offset: filter.offset ?? 0,
  };
  if (filter.dueBefore) params.due_before = filter.dueBefore;
  const response = await apiClient.get<DocumentFact[]>('/api/atoms/obligations', { params });
  return response.data ?? [];
}

async function fetchFeatureFlags(): Promise<FeatureFlags> {
  const response = await apiClient.get<FeatureFlags>('/api/config/features');
  return response.data;
}

interface PatchAtomTierArgs {
  atomId: string;
  policy: Record<string, unknown>;
}

async function patchAtomTierRequest({ atomId, policy }: PatchAtomTierArgs): Promise<void> {
  await apiClient.patch(`/api/atoms/${atomId}/tier`, { policy });
}

async function confirmObligationRequest(factId: number): Promise<void> {
  await apiClient.post(`/api/atoms/obligations/${factId}/confirm`);
}

async function reopenObligationRequest(factId: number): Promise<void> {
  await apiClient.delete(`/api/atoms/obligations/${factId}/confirm`);
}

async function resetFactTierRequest(factId: number): Promise<void> {
  await apiClient.post(`/api/atoms/documents/facts/${factId}/reset-tier`);
}

async function fetchObligationCalendarPref(): Promise<ObligationCalendarPref> {
  const response = await apiClient.get<ObligationCalendarPref>('/api/atoms/obligations/calendar-pref');
  return response.data;
}

async function setObligationCalendarPrefRequest(calendarName: string | null): Promise<ObligationCalendarPref> {
  const response = await apiClient.put<ObligationCalendarPref>(
    '/api/atoms/obligations/calendar-pref',
    { calendar_name: calendarName },
  );
  return response.data;
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

/**
 * A single atom by id — backs the detail drawer's cold-load (a `?detail=`
 * deep-link opened without a clicked seed). 404s (incl. synthetic kg ids)
 * surface as an error and the drawer degrades to closed.
 */
export function useAtomByIdQuery(atomId: string | null) {
  return useApiQuery(
    {
      queryKey: keys.brain.atom(atomId ?? ''),
      queryFn: () => fetchAtomById(atomId as string),
      staleTime: STALE.DEFAULT,
      enabled: !!atomId,
      retry: false,
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

/**
 * All Schicht A facts for one document. Lazy — pass `enabled: false` until the
 * panel is opened so the list view never fans out a fetch per document.
 */
export function useFactsForDocumentQuery(documentId: number, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.brain.facts(documentId),
      queryFn: () => fetchDocumentFacts(documentId),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'knowledge.facts.loadError',
  );
}

/**
 * Obligation agenda rows, soonest-first. `offset` pages the stable
 * (obligation_date, id) order for "Mehr laden".
 */
export function useObligationsQuery(filter: ObligationsFilter = {}) {
  return useApiQuery(
    {
      queryKey: keys.brain.obligations({
        dueBefore: filter.dueBefore ?? null,
        limit: filter.limit ?? 200,
        offset: filter.offset ?? 0,
      }),
      queryFn: () => fetchObligations(filter),
      staleTime: STALE.DEFAULT,
    },
    'obligations.loadError',
  );
}

/** Frontend-visible backend feature flags (config-stable). */
export function useFeatureFlags() {
  return useApiQuery(
    {
      queryKey: keys.config.features(),
      queryFn: fetchFeatureFlags,
      staleTime: STALE.CONFIG,
    },
    'common.error',
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

/**
 * Mark / unmark an obligation handled for the current user (the agenda's
 * Bestätigen / Wieder öffnen) — the server home for the former localStorage
 * state. Invalidates obligations so the `confirmed` flag reflects the ledger
 * (the agenda layers an optimistic override on top for the 5s undo window).
 */
export function useConfirmObligation() {
  const queryClient = useQueryClient();
  return useApiMutation<void, number>(
    {
      mutationFn: confirmObligationRequest,
      onSuccess: () => {
        // Scope to the obligations agenda (prefix match) — no need to churn
        // /brain search, review, or per-document facts queries.
        queryClient.invalidateQueries({ queryKey: ['brain', 'obligations'] });
      },
    },
    'obligations.confirmError',
  );
}

export function useReopenObligation() {
  const queryClient = useQueryClient();
  return useApiMutation<void, number>(
    {
      mutationFn: reopenObligationRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: ['brain', 'obligations'] });
      },
    },
    'obligations.confirmError',
  );
}

/**
 * Reset a fact's per-fact tier override back to its parent document's tier.
 * Invalidates brain queries so the facts panel / drawer reflect the restored
 * tier + cleared override badge.
 */
/** The user's obligation→calendar sync preference + writable-calendar options. */
export function useObligationCalendarPref() {
  return useApiQuery(
    {
      queryKey: keys.brain.calendarPref(),
      queryFn: fetchObligationCalendarPref,
      staleTime: STALE.DEFAULT,
    },
    'obligations.calendarError',
  );
}

export function useSetObligationCalendarPref() {
  const queryClient = useQueryClient();
  return useApiMutation<ObligationCalendarPref, string | null>(
    {
      mutationFn: setObligationCalendarPrefRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.brain.calendarPref() });
      },
    },
    'obligations.calendarError',
  );
}

export function useResetFactTier() {
  const queryClient = useQueryClient();
  return useApiMutation<void, number>(
    {
      mutationFn: resetFactTierRequest,
      onSuccess: () => {
        queryClient.invalidateQueries({ queryKey: keys.brain.all });
      },
    },
    'circles.couldNotSave',
  );
}
