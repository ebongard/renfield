/**
 * Every refusal code of the reconciler has a sentence in EVERY language.
 *
 * Why as its own test and not as an assertion in the component: the failure
 * mode PR #1334 was built against is "the backend knows a reason the interface
 * cannot say out loud". A component test checks the path for the codes somebody
 * wrote down. This one READS the code list out of the service and can therefore
 * also catch the one nobody has written down yet: a new
 * `res.refusal_code = "..."` without a key turns this file red, with nobody
 * having to remember it.
 *
 * Second, language parity. The adversarial pass recomputed it by hand and found
 * it in order -- but NO gate guarded it, and the component tests run
 * exclusively in German (`test-utils.tsx` sets `changeLanguage('de')`). A
 * missing key in `it.json` would have passed green and shown up in operation as
 * a raw key name.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import de from '../../../../src/frontend/src/i18n/locales/de.json';
import en from '../../../../src/frontend/src/i18n/locales/en.json';
import it_ from '../../../../src/frontend/src/i18n/locales/it.json';

const LOCALES: Record<string, unknown> = { de, en, it: it_ };

const SERVICE = resolve(
  __dirname, '../../../../src/backend/services/kg_reconciler_service.py',
);

/** The codes `resolve_cluster` can actually set. */
function backendRefusalCodes(): string[] {
  const src = readFileSync(SERVICE, 'utf8');
  const found = [...src.matchAll(/refusal_code = "([a-z_]+)"/g)].map((m) => m[1]);
  return [...new Set(found)].sort();
}

/** The i18n key `refusalMessage()` builds from a code. */
function keyFor(code: string): string[] {
  // The pair refusal has its own, richer presentation (names + total) and
  // therefore different keys -- the component test covers those.
  return code === 'cluster_has_undecidable_pair'
    ? ['circles.mergeProposals.cluster.undecidable_one',
       'circles.mergeProposals.cluster.undecidable_other']
    : [`circles.mergeProposals.cluster.refused_${code}`];
}

function lookup(bundle: unknown, path: string): unknown {
  return path.split('.').reduce<unknown>(
    (node, part) => (node && typeof node === 'object'
      ? (node as Record<string, unknown>)[part]
      : undefined),
    bundle,
  );
}

describe('refusal codes and locale files', () => {
  it('knows any codes at all (otherwise the rest checks nothing)', () => {
    // Without this assertion a broken regex would silently turn the whole file
    // into an empty loop -- green and worthless.
    const codes = backendRefusalCodes();
    expect(codes.length).toBeGreaterThanOrEqual(7);
  });

  it.each(Object.keys(LOCALES))(
    'has a non-empty sentence in %s for every backend code',
    (loc) => {
      const missing: string[] = [];
      for (const code of backendRefusalCodes()) {
        for (const key of keyFor(code)) {
          const v = lookup(LOCALES[loc], key);
          if (typeof v !== 'string' || v.trim() === '') missing.push(`${code} -> ${key}`);
        }
      }
      expect(missing).toEqual([]);
    },
  );

  it('keeps the three languages congruent in the cluster branch', () => {
    const keys = (loc: string): string[] => Object.keys(
      (lookup(LOCALES[loc], 'circles.mergeProposals.cluster') ?? {}) as object,
    ).sort();
    expect(keys('en')).toEqual(keys('de'));
    expect(keys('it')).toEqual(keys('de'));
  });

  it('has a general fallback for an UNKNOWN code', () => {
    // The fallback is the only reason a backend running ahead of the bundle (a
    // cached service worker) does not show an empty line. It must not be
    // missing in any language.
    for (const loc of Object.keys(LOCALES)) {
      const v = lookup(LOCALES[loc], 'circles.mergeProposals.cluster.refused_generic');
      expect(typeof v === 'string' && v.trim() !== '').toBe(true);
    }
  });
});
