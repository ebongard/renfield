/**
 * Contact points (#1240) -- phone / mobile / fax as attributes of a KG entity.
 * The routes exist only while `contact_points_enabled` is on; callers gate on
 * that flag. Reads are circle-filtered server-side; writes need the entity's
 * owner (or an admin) -- the list says so in `editable`.
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

/**
 * Error codes the backend returns as a string `detail` (translated under
 * contactPoints.errors). A bad kind/label never gets here: the request schema
 * rejects it first, with a validation list as `detail`.
 */
export const CONTACT_POINT_ERROR_CODES: ReadonlySet<string> = new Set([
  'duplicate', 'invalid_number', 'not_owner',
]);

export interface ContactPointList {
  points: ContactPoint[];
  /** May the caller add / remove values (owner of the entity, or admin)? */
  editable: boolean;
}

async function fetchContactPoints(entityId: number): Promise<ContactPointList> {
  const response = await apiClient.get<{ contact_points: ContactPoint[]; editable?: boolean }>(
    `/api/knowledge-graph/entities/${entityId}/contact-points`,
  );
  return { points: response.data.contact_points ?? [], editable: response.data.editable === true };
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
