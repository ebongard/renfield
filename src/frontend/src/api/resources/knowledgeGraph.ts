import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiQuery, useApiMutation } from '../hooks';
import { keys, STALE } from '../keys';

export type EntityType = 'person' | 'place' | 'organization' | 'thing' | 'event' | 'concept';

export interface KgEntity {
  id: number;
  name: string;
  entity_type: EntityType;
  description?: string | null;
  circle_tier?: number;
  mention_count?: number;
  last_seen_at?: string;
}

export interface KgEntityRef {
  id: number;
  name: string;
  entity_type?: EntityType;
}

export interface KgRelation {
  id: number;
  predicate: string;
  confidence?: number;
  subject?: KgEntityRef;
  object?: KgEntityRef;
}

export interface CircleTierInfo {
  tier: number;
  name: string;
  label: string;
  description?: string;
}

export interface KgStats {
  entity_count?: number;
  relation_count?: number;
  entity_types?: Record<string, number>;
  top_entities?: KgEntity[];
}

interface EntitiesFilters {
  page: number;
  size: number;
  type?: string;
  search?: string;
  circleTier?: string;
}

interface RelationsFilters {
  page: number;
  size: number;
  entityId?: string;
}

async function fetchEntities(filters: EntitiesFilters): Promise<{ entities: KgEntity[]; total: number }> {
  const params = new URLSearchParams();
  params.set('page', String(filters.page));
  params.set('size', String(filters.size));
  if (filters.type) params.set('type', filters.type);
  if (filters.search) params.set('search', filters.search);
  if (filters.circleTier && filters.circleTier !== 'all') params.set('circle_tier', filters.circleTier);
  const response = await apiClient.get<{ entities: KgEntity[]; total: number }>(
    `/api/knowledge-graph/entities?${params}`,
  );
  return { entities: response.data.entities ?? [], total: response.data.total ?? 0 };
}

async function fetchRelations(filters: RelationsFilters): Promise<{ relations: KgRelation[]; total: number }> {
  const params = new URLSearchParams();
  params.set('page', String(filters.page));
  params.set('size', String(filters.size));
  if (filters.entityId) params.set('entity_id', filters.entityId);
  const response = await apiClient.get<{ relations: KgRelation[]; total: number }>(
    `/api/knowledge-graph/relations?${params}`,
  );
  return { relations: response.data.relations ?? [], total: response.data.total ?? 0 };
}

async function fetchStats(): Promise<KgStats> {
  const response = await apiClient.get<KgStats>('/api/knowledge-graph/stats');
  return response.data;
}

async function fetchCircleTiers(lang: string): Promise<CircleTierInfo[]> {
  const response = await apiClient.get<{ tiers: CircleTierInfo[] }>('/api/knowledge-graph/circle-tiers', {
    params: { lang },
  });
  return response.data.tiers ?? [];
}

async function searchEntitiesRequest(query: string): Promise<KgEntity[]> {
  if (!query || query.length < 1) return [];
  const response = await apiClient.get<{ entities: KgEntity[] }>('/api/knowledge-graph/entities', {
    params: { search: query, size: 10 },
  });
  return response.data.entities ?? [];
}

interface UpdateEntityInput {
  id: number;
  patch: { name: string; entity_type: EntityType; description: string | null };
}

async function updateEntityRequest({ id, patch }: UpdateEntityInput): Promise<void> {
  await apiClient.put(`/api/knowledge-graph/entities/${id}`, patch);
}

async function deleteEntityRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/knowledge-graph/entities/${id}`);
}

async function deleteRelationRequest(id: number): Promise<void> {
  await apiClient.delete(`/api/knowledge-graph/relations/${id}`);
}

interface MergeEntitiesInput {
  source_id: number;
  target_id: number;
}

async function mergeEntitiesRequest(input: MergeEntitiesInput): Promise<void> {
  await apiClient.post('/api/knowledge-graph/entities/merge', input);
}

interface UpdateTierInput {
  id: number;
  circleTier: number;
}

async function updateEntityTierRequest({ id, circleTier }: UpdateTierInput): Promise<void> {
  await apiClient.patch(`/api/knowledge-graph/entities/${id}/circle-tier`, { circle_tier: circleTier });
}

interface CreateRelationInput {
  subject_id: number;
  predicate: string;
  object_id: number;
  confidence: number;
}

async function createRelationRequest(input: CreateRelationInput): Promise<void> {
  await apiClient.post('/api/knowledge-graph/relations', input);
}

interface UpdateRelationInput {
  id: number;
  patch: { predicate: string; confidence: number; subject_id: number; object_id: number };
}

async function updateRelationRequest({ id, patch }: UpdateRelationInput): Promise<void> {
  await apiClient.put(`/api/knowledge-graph/relations/${id}`, patch);
}

export function useKgEntitiesQuery(filters: EntitiesFilters, enabled = true) {
  return useApiQuery(
    {
      queryKey: keys.knowledgeGraph.entities(filters as unknown as Record<string, unknown>),
      queryFn: () => fetchEntities(filters),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'knowledgeGraph.couldNotLoad',
  );
}

export function useKgRelationsQuery(filters: RelationsFilters, enabled = true) {
  return useApiQuery(
    {
      queryKey: keys.knowledgeGraph.relations(filters as unknown as Record<string, unknown>),
      queryFn: () => fetchRelations(filters),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'knowledgeGraph.couldNotLoad',
  );
}

export function useKgStatsQuery(enabled = true) {
  return useApiQuery(
    {
      queryKey: [...keys.knowledgeGraph.all, 'stats'] as const,
      queryFn: fetchStats,
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'knowledgeGraph.couldNotLoad',
  );
}

export function useKgCircleTiersQuery(lang: string) {
  return useApiQuery(
    {
      queryKey: [...keys.knowledgeGraph.circleTiers(), lang] as const,
      queryFn: () => fetchCircleTiers(lang),
      staleTime: STALE.CONFIG,
    },
    'common.error',
  );
}

export function useKgEntitySearch() {
  return useApiMutation(
    {
      mutationFn: searchEntitiesRequest,
    },
    'common.error',
  );
}

function invalidateKg(queryClient: ReturnType<typeof useQueryClient>) {
  queryClient.invalidateQueries({ queryKey: keys.knowledgeGraph.all });
}

export function useUpdateKgEntity() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: updateEntityRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}

export function useDeleteKgEntity() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: deleteEntityRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}

export function useDeleteKgRelation() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: deleteRelationRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}

export function useMergeKgEntities() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: mergeEntitiesRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}

export function useUpdateKgEntityTier() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: updateEntityTierRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}

export function useCreateKgRelation() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: createRelationRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}

export function useUpdateKgRelation() {
  const queryClient = useQueryClient();
  return useApiMutation(
    { mutationFn: updateRelationRequest, onSuccess: () => invalidateKg(queryClient) },
    'common.error',
  );
}
