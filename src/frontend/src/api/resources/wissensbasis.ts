/**
 * Wissensbasis API resource — composed memory UX surface (A-LANDING).
 *
 * Backend lives in Reva (`src/reva/wissensbasis/routes.py`). All endpoints
 * gate on `REVA_WISSENSBASIS_ENABLED`; queries enabled=false until the
 * settings hook reports the feature is on.
 *
 * - GET /api/wissensbasis/focus  → 1-hop + 2-hop neighborhood for an entity
 * - GET /api/wissensbasis/trace  → last reasoning trace for a session
 * - GET /api/wissensbasis/me/mix → A2/A4 layout split for the user's role
 */

import { useFeatureFlags } from './brain';
import apiClient from '../../utils/axios';
import { useApiQuery } from '../hooks';
import { STALE } from '../keys';

export type EntityType =
  | 'release'
  | 'ticket'
  | 'person'
  | 'document'
  | 'incident'
  | 'concept'
  | 'unknown';

export interface TraceEntity {
  entity_id: string;
  display_name: string;
  entity_type: string;
}

export interface TraceEdge {
  from_entity: string;
  to_entity: string;
  relation: string;
  weight: number;
}

export interface ReasoningTrace {
  entities: TraceEntity[];
  edges: TraceEdge[];
}

export interface TracePeek {
  session_id: string;
  trace: ReasoningTrace;
  is_empty: boolean;
}

export interface FocusEntity {
  entity_id: string;
  display_name: string;
  entity_type: string;
  importance: number;
  /** Circle tier (0 self … 4 public) — drives the tier-token node colour in
   *  the 3D scene. Optional so older API responses still deserialize. */
  circle_tier?: number;
}

export interface FocusEdge {
  from_entity: string;
  to_entity: string;
  relation: string;
}

/**
 * A value observed for an entity at a specific time. Sourced from the
 * sprint-2 wb_field_provenance substrate. Backend pins the JSON value
 * the source returned at ``fetched_at`` so audit replay reconstructs
 * "what did we know about X at time Y" even after the upstream value
 * changes or the upstream record is deleted.
 *
 * source_type is the coarse DB CHECK enum (release / jira / confluence /
 * itsm / memory / derived). The fine-grained source (release_phase,
 * jira_issue, …) is recoverable from the row's field_path.
 */
export interface ObservedField {
  field_path: string;
  value: unknown;
  fetched_at: string; // ISO 8601 UTC
  source_type: string;
}

export interface FocusNeighborhood {
  focus: FocusEntity;
  hop1: FocusEntity[];
  hop2: FocusEntity[];
  edges: FocusEdge[];
  overflow_hop1: number;
  overflow_hop2: number;
  // Sprint 2 additions — default to empty / null so older API responses
  // (which omit them) still deserialize cleanly.
  observed_fields?: ObservedField[];
  source_priority?: 1 | 2 | 3 | null;
}

export interface RoleMix {
  a2: number;
  a4: number;
  source: 'role' | 'user_override' | 'default';
  role: string | null;
}

export interface SearchHit {
  entity_id: string;
  display_name: string;
  entity_type: string;
  mention_count: number;
}

export interface SearchResults {
  items: SearchHit[];
  total: number;
}

// Query key factories. Keep keys local to this resource — `keys.ts` is
// shared and would couple the platform to a Reva-only feature flag.
const wbKeys = {
  all: ['wissensbasis'] as const,
  trace: (sessionId: string) => ['wissensbasis', 'trace', sessionId] as const,
  focus: (entityId: string, hops: number, maxPerHop: number | null) =>
    ['wissensbasis', 'focus', entityId, { hops, maxPerHop }] as const,
  mix: (role: string | null) => ['wissensbasis', 'mix', role] as const,
  search: (q: string) => ['wissensbasis', 'search', q] as const,
};

async function fetchTrace(sessionId: string): Promise<TracePeek> {
  const { data } = await apiClient.get<TracePeek>('/api/wissensbasis/trace', {
    params: { session_id: sessionId },
  });
  return data;
}

async function fetchFocus(
  entityId: string,
  opts: { hops?: number; maxPerHop?: number | null } = {},
): Promise<FocusNeighborhood> {
  const { data } = await apiClient.get<FocusNeighborhood>('/api/wissensbasis/focus', {
    params: {
      entity_id: entityId,
      hops: opts.hops ?? 2,
      ...(opts.maxPerHop ? { max_per_hop: opts.maxPerHop } : {}),
    },
  });
  return data;
}

async function fetchMix(role: string | null): Promise<RoleMix> {
  const { data } = await apiClient.get<RoleMix>('/api/wissensbasis/me/mix', {
    params: role ? { role } : {},
  });
  return data;
}

export function useTraceQuery(sessionId: string | null, enabled = true) {
  return useApiQuery(
    {
      queryKey: wbKeys.trace(sessionId ?? ''),
      queryFn: () => fetchTrace(sessionId!),
      // Trace is rebuilt every agent turn; LIVE keeps the panel current
      // without spamming the backend during quiet periods.
      staleTime: STALE.LIVE,
      enabled: enabled && !!sessionId,
    },
    'wissensbasis.trace.couldNotLoad',
  );
}

export function useFocusQuery(
  entityId: string | null,
  opts: { hops?: number; maxPerHop?: number | null } = {},
  enabled = true,
) {
  return useApiQuery(
    {
      queryKey: wbKeys.focus(entityId ?? '', opts.hops ?? 2, opts.maxPerHop ?? null),
      queryFn: () => fetchFocus(entityId!, opts),
      // Focus neighborhood depends on relatively stable KG topology;
      // DEFAULT (30s) gives breathing room without going stale during a
      // single session.
      staleTime: STALE.DEFAULT,
      enabled: enabled && !!entityId,
    },
    'wissensbasis.focus.couldNotLoad',
  );
}

async function fetchSearch(q: string): Promise<SearchResults> {
  const { data } = await apiClient.get<SearchResults>('/api/wissensbasis/search', {
    params: { q },
  });
  return data;
}

/**
 * A4 direct-entry search. Live suggestions over KGEntity.name.
 *
 * Use case: user lands on /wissensbasis with a specific entity in
 * mind. Empty query short-circuits to no results — the direct-entry
 * premise is "I know what I'm looking for," not "show me everything."
 *
 * STALE.LIVE keeps the suggestion list fresh during typing without
 * spamming the backend across debounce ticks.
 */
export function useSearchQuery(q: string, enabled = true) {
  const trimmed = q.trim();
  return useApiQuery(
    {
      queryKey: wbKeys.search(trimmed),
      queryFn: () => fetchSearch(trimmed),
      staleTime: STALE.LIVE,
      enabled: enabled && trimmed.length > 0,
    },
    'wissensbasis.search.couldNotLoad',
  );
}

export function useRoleMixQuery(role: string | null, enabled = true) {
  return useApiQuery(
    {
      queryKey: wbKeys.mix(role),
      // CONFIG (5min) — role mix only changes when an operator edits
      // agent_roles.yaml + restarts the pod.
      queryFn: () => fetchMix(role),
      staleTime: STALE.CONFIG,
      enabled,
    },
    'wissensbasis.mix.couldNotLoad',
  );
}

/**
 * Check whether the richer Reva Wissensbasis surface (/trace + /me/mix) is
 * available on the backend.
 *
 * Reads the `wissensbasis_reva_available` flag from `/api/config/features`
 * (the backend reports whether the Reva-only /me/mix route is mounted). This
 * REPLACES the previous approach of PROBING /me/mix and treating a 404 as
 * "off": that probe 404s by design in standalone Renfield and spammed the
 * browser console with "Failed to load resource" + "API Error" lines. Reading
 * a 200 config flag keeps the console clean and makes availability
 * deterministic (no transient-true window that would briefly mount the
 * Reva-only side panel and fire its own /trace 404).
 *
 * Returns:
 *   - undefined while the feature flags are loading (don't flash the nav entry)
 *   - true  when the Reva Wissensbasis routes are mounted
 *   - false in standalone Renfield (routes absent)
 */
export function useWissensbasisAvailable(): boolean | undefined {
  const { data, isLoading } = useFeatureFlags();
  if (isLoading || !data) return undefined;
  return data.wissensbasis_reva_available;
}
