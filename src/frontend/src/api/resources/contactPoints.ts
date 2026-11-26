/**
 * Contact points (#1240) -- phone / mobile / fax as attributes of a KG entity.
 * The routes exist only while `contact_points_enabled` is on; callers gate on
 * that flag. Reads are circle-filtered server-side; writes need the entity's
 * owner (or an admin).
 */
import { useQueryClient } from '@tanstack/react-query';

import apiClient from '../../utils/axios';
import { useApiMutation, useApiQuery } from '../hooks';
import { keys, STALE } from '../keys';

export type ContactPointKind = 'phone' | 'mobile' | 'fax';
export type ContactPointLabel = 'private' | 'work';

export interface ContactPoint {
  id: number;
  entity_id: number;
  kind: ContactPointKind;
  label: ContactPointLabel | null;
  value: string;
  normalized: string;
  source_type: string;
  circle_tier: number;
  created_at: string;
}

/** Error codes the backend returns as `detail` (translated under contactPoints.errors). */
export const CONTACT_POINT_ERROR_CODES: ReadonlySet<string> = new Set([
  'duplicate', 'invalid_number', 'invalid_kind', 'invalid_label',
]);

async function fetchContactPoints(entityId: number): Promise<ContactPoint[]> {
  const response = await apiClient.get<{ contact_points: ContactPoint[] }>(
    `/api/knowledge-graph/entities/${entityId}/contact-points`,
  );
  return response.data.contact_points ?? [];
}

export interface CreateContactPointInput {
  entityId: number;
  kind: ContactPointKind;
  value: string;
  label: ContactPointLabel | null;
}

async function createContactPoint({ entityId, ...body }: CreateContactPointInput): Promise<ContactPoint> {
  const response = await apiClient.post<ContactPoint>(
    `/api/knowledge-graph/entities/${entityId}/contact-points`, body,
  );
  return response.data;
}

async function deleteContactPoint(id: number): Promise<void> {
  await apiClient.delete(`/api/knowledge-graph/contact-points/${id}`);
}

export function useContactPoints(entityId: number, enabled: boolean) {
  return useApiQuery(
    {
      queryKey: keys.knowledgeGraph.contactPoints(entityId),
      queryFn: () => fetchContactPoints(entityId),
      staleTime: STALE.DEFAULT,
      enabled,
    },
    'contactPoints.couldNotLoad',
  );
}

export function useCreateContactPoint() {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: createContactPoint,
      onSuccess: (_data, vars) => queryClient.invalidateQueries({
        queryKey: keys.knowledgeGraph.contactPoints(vars.entityId),
      }),
    },
    'common.error',
  );
}

export function useDeleteContactPoint(entityId: number) {
  const queryClient = useQueryClient();
  return useApiMutation(
    {
      mutationFn: deleteContactPoint,
      onSuccess: () => queryClient.invalidateQueries({
        queryKey: keys.knowledgeGraph.contactPoints(entityId),
      }),
    },
    'common.error',
  );
}
