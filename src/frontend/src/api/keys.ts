/**
 * Per-resource staleTime taxonomy. Resources reference these instead of inlining
 * magic numbers in 20 places.
 */
export const STALE = {
  LIVE: 5_000,
  DEFAULT: 30_000,
  CONFIG: 5 * 60_000,
} as const;

/**
 * Query key factories. Convention:
 *   .all                        invalidates everything for the resource
 *   .list(filters?)             one filtered list view (filters object always last segment)
 *   .detail(id)                 one record by id
 *   .nested(parentId, sub?)     child collections under a parent
 *
 * `queryClient.invalidateQueries({ queryKey: keys.foo.all })` invalidates every
 * key that starts with `['foo', ...]` (RQ uses prefix-match on tuples).
 */
export const keys = {
  memories: {
    all: ['memories'] as const,
    list: (category: string | null) => ['memories', 'list', { category }] as const,
  },
  users: {
    all: ['users'] as const,
    list: () => ['users', 'list'] as const,
    detail: (id: number) => ['users', 'detail', id] as const,
  },
  roles: {
    all: ['roles'] as const,
    list: () => ['roles', 'list'] as const,
    detail: (id: number) => ['roles', 'detail', id] as const,
  },
  rooms: {
    all: ['rooms'] as const,
    list: () => ['rooms', 'list'] as const,
    detail: (id: number) => ['rooms', 'detail', id] as const,
    outputs: (roomId: number) => ['rooms', roomId, 'outputs'] as const,
    availableOutputs: () => ['rooms', 'available-outputs'] as const,
  },
  speakers: {
    all: ['speakers'] as const,
    list: () => ['speakers', 'list'] as const,
    detail: (id: number) => ['speakers', 'detail', id] as const,
    embeddings: (id: number) => ['speakers', id, 'embeddings'] as const,
  },
  intents: {
    all: ['intents'] as const,
    list: () => ['intents', 'list'] as const,
  },
  integrations: {
    all: ['integrations'] as const,
    list: () => ['integrations', 'list'] as const,
  },
  knowledge: {
    all: ['knowledge'] as const,
    list: () => ['knowledge', 'list'] as const,
    detail: (id: number) => ['knowledge', 'detail', id] as const,
    documents: (kbId: number) => ['knowledge', kbId, 'documents'] as const,
    permissions: (kbId: number) => ['knowledge', kbId, 'permissions'] as const,
  },
  settings: {
    all: ['settings'] as const,
    list: () => ['settings', 'list'] as const,
  },
  satellites: {
    all: ['satellites'] as const,
    list: () => ['satellites', 'list'] as const,
    detail: (id: number) => ['satellites', 'detail', id] as const,
  },
  presence: {
    all: ['presence'] as const,
    current: () => ['presence', 'current'] as const,
    analytics: (range: string) => ['presence', 'analytics', { range }] as const,
    raw: (limit: number) => ['presence', 'raw', { limit }] as const,
  },
  paperlessAudit: {
    all: ['paperlessAudit'] as const,
    status: () => ['paperlessAudit', 'status'] as const,
    results: () => ['paperlessAudit', 'results'] as const,
    stats: () => ['paperlessAudit', 'stats'] as const,
    duplicateGroups: () => ['paperlessAudit', 'duplicate-groups'] as const,
  },
  federation: {
    all: ['federation'] as const,
    audit: () => ['federation', 'audit'] as const,
    peers: () => ['federation', 'peers'] as const,
  },
  routing: {
    all: ['routing'] as const,
    current: () => ['routing', 'current'] as const,
    history: () => ['routing', 'history'] as const,
    stats: () => ['routing', 'stats'] as const,
  },
  knowledgeGraph: {
    all: ['knowledgeGraph'] as const,
    entities: (filters?: Record<string, unknown>) => ['knowledgeGraph', 'entities', filters ?? {}] as const,
    relations: (filters?: Record<string, unknown>) => ['knowledgeGraph', 'relations', filters ?? {}] as const,
    circleTiers: () => ['knowledgeGraph', 'circle-tiers'] as const,
  },
  circles: {
    all: ['circles'] as const,
    settings: () => ['circles', 'settings'] as const,
    members: () => ['circles', 'members'] as const,
    review: () => ['circles', 'review'] as const,
    peers: () => ['circles', 'peers'] as const,
  },
  tasks: {
    all: ['tasks'] as const,
    list: (filter?: string) => ['tasks', 'list', { filter }] as const,
  },
  cameras: {
    all: ['cameras'] as const,
    list: () => ['cameras', 'list'] as const,
    snapshot: (id: string) => ['cameras', id, 'snapshot'] as const,
  },
  homeAssistant: {
    all: ['homeAssistant'] as const,
    entities: () => ['homeAssistant', 'entities'] as const,
    states: () => ['homeAssistant', 'states'] as const,
  },
  brain: {
    all: ['brain'] as const,
    search: (query: string, filters?: Record<string, unknown>) =>
      ['brain', 'search', { query, ...(filters ?? {}) }] as const,
    review: () => ['brain', 'review'] as const,
  },
  maintenance: {
    all: ['maintenance'] as const,
    status: () => ['maintenance', 'status'] as const,
  },
  chatSessions: {
    all: ['chatSessions'] as const,
    list: () => ['chatSessions', 'list'] as const,
    history: (id: string) => ['chatSessions', 'history', id] as const,
  },
  preferences: {
    all: ['preferences'] as const,
    language: () => ['preferences', 'language'] as const,
  },
  auth: {
    all: ['auth'] as const,
    me: () => ['auth', 'me'] as const,
  },
} as const;
