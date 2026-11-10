/**
 * Inline citation markup: `<cite entity="ID" type="ticket">Label</cite>`.
 *
 * A `check_output` handler (Reva's Wissensbasis) wraps entity names in it.
 * Card text renders it since the CitationChip work; the chat bubble, the
 * read-aloud path and the card renderer all parse it HERE, so the three
 * cannot drift apart. Nothing in this file produces HTML: callers turn a
 * segment into a React element, so every value crosses React's escape
 * boundary.
 */

/**
 * Server-side validator mirror — keep in sync with Reva's
 * _ALLOWED_ENTITY_RE in src/reva/wissensbasis/citation_chip.py.
 * Accepts alphanumeric + dash + underscore + slash (the slash supports
 * Digital.ai Release path-style IDs like Applications/Folder.../Release...).
 * Anything else (script tags, javascript:, smuggled HTML) and any ".."
 * substring (path traversal) renders as a missing chip.
 */
const CITE_ENTITY_RE = /^[A-Za-z0-9_\-/]{1,256}$/;

export const isValidEntity = (v: string): boolean =>
  !!v && !v.includes('..') && CITE_ENTITY_RE.test(v);

const CITE_TAG_SOURCE = String.raw`<cite\s+entity="([^"]+)"(?:\s+type="([^"]+)")?\s*>([^<]*)<\/cite>`;

/**
 * A fresh regex per call: a shared `g` regex carries `lastIndex` between
 * callers. Without `g` it is for `.match()`, with `g` for `matchAll`.
 */
export const citeTagRe = (flags = 'i'): RegExp => new RegExp(CITE_TAG_SOURCE, flags);

export interface CiteSegment {
  entity: string;
  type?: string;
  label: string;
  /** False for an id the validator rejects — render it as a missing chip. */
  valid: boolean;
}

/** Split text into plain strings and citation segments, in order. */
export function splitCites(text: string): Array<string | CiteSegment> {
  const out: Array<string | CiteSegment> = [];
  let last = 0;
  for (const m of text.matchAll(citeTagRe('gi'))) {
    const index = m.index ?? 0;
    if (index > last) out.push(text.slice(last, index));
    const [, entity, type, label] = m;
    out.push({ entity, type, label, valid: isValidEntity(entity) });
    last = index + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

/**
 * The text a person should HEAR: every citation reduced to its label.
 *
 * Also drops a lone opening or closing tag. Read-aloud splits the answer at
 * sentence ends, and a label may contain one, so a sentence can end inside
 * a tag; neither half may be read out as markup.
 */
export function stripCites(text: string): string {
  return text
    .replace(citeTagRe('gi'), (_m, _entity, _type, label: string) => label)
    .replace(/<cite\b[^>]*>?/gi, '')
    .replace(/<\/cite>/gi, '');
}
