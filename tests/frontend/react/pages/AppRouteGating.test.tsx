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

// Routes that legitimately render without a gate of their own: the public auth
// pages and the `/*` layout shell. Everything else must carry one — EXCEPT the
// children of `/wissen`, whose parent element already IS a ProtectedRoute;
// those are excluded by SOURCE POSITION below, not by name, so a future
// top-level route that happens to be called `graph` is still checked.
const PUBLIC_PATHS = new Set(['/login', '/register', '/auth/callback', '/*']);

/** The `/wissen` block, whose parent gate covers every child inside it. */
function wissenBlock(src: string): [number, number] {
  const from = src.indexOf('<Route path="/wissen"');
  if (from < 0) return [-1, -1];
  // The block ends at the closing tag of that Route element.
  const to = src.indexOf('</Route>', from);
  return [from, to < 0 ? src.length : to];
}

describe('App route gating', () => {
  it('renders no page component outside ProtectedRoute/AdminRoute', () => {
    const [wFrom, wTo] = wissenBlock(APP);
    const offenders: string[] = [];

    // Every <Route …>, whether it carries a path or is an index route, and
    // whatever shape its element takes — the first component named inside
    // `element={…}` is the one that renders.
    const re = /<Route\s+(index|path="([^"]+)")[^>]*?element=\{\s*<([A-Za-z]+)/gs;
    let m: RegExpExecArray | null;
    while ((m = re.exec(APP)) !== null) {
      if (m.index >= wFrom && m.index <= wTo) continue; // gated by /wissen
      const path = m[2] ?? '(index)';
      const element = m[3];
      if (element === 'ProtectedRoute' || element === 'AdminRoute') continue;
      // Pure redirects render no data; their target carries the gate.
      if (element === 'Navigate' || element === 'RedirectPreserving') continue;
      if (PUBLIC_PATHS.has(path)) continue;
      offenders.push(`${path} → <${element}>`);
    }

    expect(offenders).toEqual([]);
  });
});
