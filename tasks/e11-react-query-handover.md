# E11 React Query — handover for the next session

**Status:** Foundation + 5 reference pages landed in **#504** on `e11-react-query` (2026-04-30). This doc is everything a fresh session needs to finish the remaining 19 pages + 7 components + 25 spec migrations + `useChatSessions` rewrite without re-reading the original plan.

**Authoritative plan (don't re-derive decisions, use these as locked):** `~/.claude/plans/pure-questing-blanket.md` — 6 decisions (D1–D6) + 5 outside-voice fixes recorded inline.

---

## What is already locked in

These are committed and tested in #504. Don't re-design them — just reuse.

| Concern | Decision | Where it lives |
|---|---|---|
| Mutation retry | `mutations.retry: 0` (Renfield endpoints not idempotent) | `src/frontend/src/api/queryClient.ts` |
| Query retry | `retry` callback bails on 4xx, caps at 1 retry on 5xx/network | `src/frontend/src/api/queryClient.ts` |
| Focus refetch | Disabled globally (admin UX) | same |
| Provider order | `ErrorBoundary → ThemeProvider → AuthProvider → QueryClientProvider → DeviceProvider` — AuthProvider FIRST so `AuthContext.tsx:226-263` interceptors install before any RQ fetcher | `src/frontend/src/App.tsx` |
| Stale taxonomy | `STALE.LIVE = 5_000` / `STALE.DEFAULT = 30_000` / `STALE.CONFIG = 5*60_000` | `src/frontend/src/api/keys.ts` |
| Key shape | `.all`, `.list(filters?)`, `.detail(id)`, `.nested(parentId, sub?)` | `src/frontend/src/api/keys.ts` (entries already stubbed for every resource) |
| Error wrapper | `useApiQuery(opts, fallbackI18nKey)` and `useApiMutation(opts, fallbackI18nKey)` integrate `extractApiError` + `extractFieldErrors` + i18n | `src/frontend/src/api/hooks.ts` |
| Test wrapper | `renderWithProviders(ui, { initialEntries, queryClient } = {})` | `tests/frontend/react/test-utils.jsx` |
| Cross-tree React | `vitest.config.js` aliases `@tanstack/react-query` to test tree to dedupe | `tests/frontend/react/vitest.config.js` |

## Pages already migrated (use as templates)

| Page | Shape | Reference for |
|---|---|---|
| `MemoryPage.tsx` | CRUD with filter, modal form, optimistic-free invalidation | The simplest CRUD page |
| `RolesPage.tsx` | CRUD with feature-toggle-aware permission categories | Pages that drop manual `Authorization: Bearer …` headers (the AuthContext interceptor handles it now) |
| `IntentsPage.tsx` | Multi-resource read-only with conditional second query (`enabled` flag for the prompt modal) | Pages with on-demand secondary fetches |
| `SettingsPage.tsx` | GET/PUT + RQ-driven polling via `refetchInterval: (query) => data.all_synced ? false : 2000` capped by a setTimeout that flips `enabled` back off | Pages with bounded polling |
| `MaintenancePage.tsx` | Mutation-only (4 admin actions, no list query); per-mutation result state stays local | Pages that are pure side-effect dashboards |

Resource files for each: `src/frontend/src/api/resources/{memories,roles,intents,settings,maintenance}.ts`.

## The canonical migration recipe

For every remaining page, follow this exactly:

1. **Read the page** to enumerate the API calls. Note: any GET → `useApiQuery`, any POST/PATCH/DELETE → `useApiMutation`.
2. **Create or extend** `src/frontend/src/api/resources/<resource>.ts`:
   - Plain typed fetcher functions (`async function fetchFoos()`, `createFooRequest(input)`, etc.)
   - One `useFooQuery(params)` per query, calling `useApiQuery({ queryKey, queryFn, staleTime: STALE.DEFAULT|LIVE|CONFIG }, fallbackI18nKey)`
   - One `useCreateFoo() / useUpdateFoo() / useDeleteFoo()` per mutation, each calling `useApiMutation({ mutationFn, onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.foo.all }) }, fallbackI18nKey)`
   - The `keys.foo` entry probably already exists in `src/api/keys.ts` — check first, extend if a new `.list(filter)` shape is needed.
3. **Rewrite the page**:
   - Replace `useState<T[]>([]) + useState(loading) + useState(error)` triplet → `const fooQuery = useFoosQuery(params); const foos = fooQuery.data ?? []`
   - Drop `useCallback(loadFoos)` + `useEffect(loadFoos)` entirely
   - Delete manual `Authorization: Bearer …` header construction — the interceptor handles it
   - Mutations: `await apiClient.post(...) ; loadFoos()` → `await createFoo.mutateAsync(input)` (invalidation auto-refetches)
   - Keep the local `success` Alert state for mutation success messages
   - For `error` rendering: `const error = fooQuery.errorMessage ?? mutationError`
4. **Subtle correctness rule** (already cost a green test once — don't re-learn):

   ```ts
   // ❌ WRONG — reads previous render's value
   try { await createFoo.mutateAsync(input); } 
   catch { setMutationError(createFoo.errorMessage ?? t('common.error')); }
   
   // ✅ RIGHT — formats the just-thrown error directly
   try { await createFoo.mutateAsync(input); } 
   catch (err) { setMutationError(extractApiError(err, t('common.error'))); }
   ```

   This is because `mutationHook.errorMessage` is derived from React state that has not yet propagated synchronously inside the catch block. SettingsPage's `'Invalid keyword'` test caught this; all 5 migrated pages are fixed. New pages must follow the same pattern.
5. **Run the page's existing test** (if it has one): `cd tests/frontend/react && npx vitest run pages/<Page>.test.jsx`. Existing tests should pass unchanged because MSW intercepts at the network layer.

## Migration order (recommended)

Tackle in this order. Smaller first to keep momentum, biggest last when the pattern is muscle memory:

### Mid-tier (12 pages)
1. `TasksPage` — small CRUD
2. `BrainPage` / `BrainReviewPage` / `CirclesPeersPage` / `CirclesSettingsPage` — small CRUD-ish, all touch the `circles` keys family already in keys.ts
3. `IntegrationsPage` — list MCP servers + restart mutations (16 specs to keep green)
4. `FederationAuditPage` — read-only feed (1 spec)
5. `RoutingDashboardPage` — live data (use `STALE.LIVE`)
6. `PresencePage` — analytics + raw events (use `STALE.LIVE`)
7. `SatellitesPage` — live health (use `STALE.LIVE`)
8. `CameraPage` / `HomeAssistantPage` — feature-flag-gated, similar to IntentsPage shape

### Large (6 pages)
9. `UsersPage` (751 LOC, 14 specs) — CRUD with `extractFieldErrors` for 422 form errors → use `useApiMutation().fieldErrors`
10. `RoomsPage` (849, 11 specs) — has `outputs(roomId)` nested key already factory'd
11. `KnowledgePage` (952) — careful: interacts with `useDocumentUpload` (NOT migrated, keeps `onUploadProgress`) and `useDocumentPolling` (NOT migrated, custom backoff). Migrate ONLY the KB list/CRUD; leave upload + polling hooks alone.
12. `SpeakersPage` (1003, 12 specs)
13. `KnowledgeGraphPage` (1094) — entities + relations queries already in keys.ts
14. `PaperlessAuditPage` (1450) — biggest; `STALE.LIVE` for status/results/duplicate-groups

### Components (7)
15. `RoomOutputSettings.tsx` — migrate alongside RoomsPage (uses `keys.rooms.outputs(roomId)`)
16. `DeviceSetup.tsx` — uses `keys.rooms.list()`
17. `LanguageSwitcher.tsx` — uses `keys.preferences.language()`
18. `presence/AnalyticsTab.tsx` — uses `keys.presence.analytics(range)`
19. `knowledge-graph/GraphView.tsx` — uses `keys.knowledgeGraph.{entities, relations}`
20. `PairResponderModal.tsx` / `PairInitiatorModal.tsx` — federation pair endpoints (mutation-only, similar to MaintenancePage shape)

### Special
21. **`useChatSessions` move:** delete `src/frontend/src/hooks/useChatSessions.ts`, recreate at `src/frontend/src/api/resources/chatSessions.ts` on top of `useApiQuery`/`useApiMutation`. **Critical:** preserve external shape `{ conversations, loading, error, refreshConversations, deleteConversation, loadConversationHistory, addConversation, updateConversationPreview }` — alias `loading` to RQ's `isLoading` so `ChatContext.tsx` (1133 lines, NOT migrated this PR) doesn't need to change. There's an existing `useChatSessions.test.jsx` that must stay green.

### Tests (25 existing specs)
22. The 5 migrated pages' specs already pass under the new `renderWithProviders`. The remaining 25 specs work today (they don't touch RQ), but as you migrate each page convert its spec to `renderWithProviders` if it's not already using it. Most already are.

### NOT in scope (do NOT migrate)
- `useDocumentPolling.ts` — custom backoff ladder + Page Visibility + localStorage
- `useDocumentUpload.ts` — `onUploadProgress` callback
- `useChatWebSocket.ts` + ChatContext WebSocket plumbing
- `AuthContext` core — separate audit

## Per-page checklist (copy into PR description)

```
- [ ] TasksPage
- [ ] BrainPage
- [ ] BrainReviewPage
- [ ] CirclesPeersPage
- [ ] CirclesSettingsPage
- [ ] IntegrationsPage
- [ ] FederationAuditPage
- [ ] RoutingDashboardPage
- [ ] PresencePage
- [ ] SatellitesPage
- [ ] CameraPage
- [ ] HomeAssistantPage
- [ ] UsersPage
- [ ] RoomsPage + RoomOutputSettings + DeviceSetup
- [ ] KnowledgePage
- [ ] SpeakersPage
- [ ] KnowledgeGraphPage + knowledge-graph/GraphView
- [ ] PaperlessAuditPage
- [ ] LanguageSwitcher
- [ ] presence/AnalyticsTab
- [ ] PairResponderModal + PairInitiatorModal
- [ ] useChatSessions move to src/api/resources/chatSessions.ts
- [ ] All 25 existing specs green under renderWithProviders
```

## Verification before opening the next PR

```bash
# 1. TypeScript — no NEW errors (some pre-existing TS errors in useDeviceConnection / useWakeWord / platform.ts are unrelated)
cd src/frontend && npx tsc --noEmit | grep -v useDeviceConnection | grep -v useWakeWord | grep -v platform.ts

# 2. Vitest — must end up at 382/382 (currently 358/382 with 24 pre-existing failures untouched)
cd tests/frontend/react && npx vitest run

# 3. Lint
cd src/frontend && npm run lint

# 4. Manual smoke (./bin/start.sh, then in browser)
#    /memory /admin/users /knowledge /chat — verify React Query Devtools shows correct queryKeys
#    /admin/satellites /admin/presence /admin/routing — verify 5s freshness (live data)
#    /settings/circles — verify NO refetch on revisit within 5 min (config staleTime)
#    Forms with 422 errors — verify per-field messages render via fieldErrors
```

## Open question for next session

The eng-review plan recommends **one PR for the entire migration** (D1). #504 split it because of session-budget realism, not a strategic decision. The next session should pick: another big PR finishing everything, or batch by mid-tier / large / components. D1's reasoning was "split-mid-stream is how chronic refactors are born" — so prefer one finishing PR if budget allows.

## Pointers

- Plan file: `~/.claude/plans/pure-questing-blanket.md`
- PR with foundation: https://github.com/ebongard/renfield/pull/504
- Branch to rebase off (or continue on): `e11-react-query`
- Test plan artifact: `~/.gstack/projects/ebongard-renfield/evdb-main-eng-review-test-plan-20260430-105719.md`
- Audit entry (now marked `[~]` partial): `tasks/audit-findings-plan.md` §E11
