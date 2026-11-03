/**
 * Name-only signals for KG merge proposals — the view's copy of
 * `src/backend/services/kg_name_signals.py`. Keep the two in step; the tests
 * on both sides share the same cases.
 */

const DIGIT = /\p{Nd}/u;

function tokens(name: string | null | undefined): string[] {
  const s = (name ?? '').trim().toLowerCase();
  return s ? s.split(/\s+/u) : [];
}

function multisetMinus(a: string[], b: string[]): string[] {
  const left = new Map<string, number>();
  for (const t of b) left.set(t, (left.get(t) ?? 0) + 1);
  const out: string[] = [];
  for (const t of a) {
    const n = left.get(t) ?? 0;
    if (n > 0) left.set(t, n - 1);
    else out.push(t);
  }
  return out;
}

/**
 * True when two names differ, and every token they do NOT share contains a
 * digit ("Product A - 1.2.4" vs "Product A - 1.2.3", "Konto 01" vs "Konto 02",
 * "Bericht 2025" vs "Bericht"). Whitespace tokens, case-insensitive, compared
 * as a multiset: an added or dropped number counts, a reordering does not.
 * A different number names a different thing (#1329).
 */
export function namesDifferOnlyInDigitTokens(
  a: string | null | undefined,
  b: string | null | undefined,
): boolean {
  const ta = tokens(a);
  const tb = tokens(b);
  const diff = [...multisetMinus(ta, tb), ...multisetMinus(tb, ta)];
  return diff.length > 0 && diff.every((t) => DIGIT.test(t));
}
