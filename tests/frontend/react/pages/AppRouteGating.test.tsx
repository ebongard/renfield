/**
 * Every route that renders data must sit behind ProtectedRoute.
 *
 * Four did not (`/tasks`, `/connections`, `/memory`, `/knowledge-graph`), which
 * means they skipped BOTH gates the wrapper owns: the login redirect and the
 * forced-password-rotation redirect. Observed live on 2026-09-24: a flagged
 * account on /tasks stayed there and rendered `password_change_required` as a
 * page error instead of being sent to the change-password screen.
 *
 * This is a source-shape guard, not a render test: the point is that no future
 * route is added to App.tsx without a gate.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const APP = readFileSync(
  resolve(__dirname, '../../../../src/frontend/src/App.tsx'),
  'utf8',
);

// Routes that legitimately render without a gate of their own:
//  - the public auth pages,
//  - the `/*` layout shell,
//  - CHILD routes of `/wissen`, whose parent element already IS a
//    ProtectedRoute (this regex cannot see the nesting).
// Pure redirects are skipped by element type below — their target carries the
// gate, so wrapping them would only add a second hop.
const UNGATED_BY_DESIGN = new Set([
  '/login', '/register', '/auth/callback', '/*',
  'graph', 'erinnerungen', 'fristen',
]);

describe('App route gating', () => {
  it('renders no page component outside ProtectedRoute/AdminRoute', () => {
    const offenders: string[] = [];
    const re = /<Route path="([^"]+)" element=\{\s*<([A-Za-z]+)/g;
    let m: RegExpExecArray | null;
    while ((m = re.exec(APP)) !== null) {
      const [, path, element] = m;
      if (element === 'ProtectedRoute' || element === 'AdminRoute') continue;
      if (element === 'Navigate' || element === 'RedirectPreserving') continue;
      if (UNGATED_BY_DESIGN.has(path)) continue;
      offenders.push(`${path} → <${element}>`);
    }
    expect(offenders).toEqual([]);
  });
});
