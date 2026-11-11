/**
 * The French bundle is a complete, well-formed translation of the English one.
 *
 * `fr.json` is data, so its proof is mechanical: the key set, the interpolation
 * placeholders and the embedded tags are compared key by key against `en.json`.
 * A translation that drops `{{name}}` renders a sentence with a hole in it, and
 * nothing else in the suite would notice -- the component tests run in German.
 *
 * Plural keys are the one place where the key sets legitimately differ: French
 * has three CLDR categories (`one`, `many`, `other`) where English has two, so
 * every English `_other` key needs a French `_many` sibling. Without it a count
 * of one million falls through to the German fallback bundle.
 *
 * Catch-up rule for keys added to `en.json` later: a key that French does not
 * have yet falls back to German at runtime (`fallbackLng`), exactly as the
 * Italian bundle behaves today. That gap is therefore REPORTED here as a
 * warning and only fails the run under `RENFIELD_I18N_STRICT=1`, which is the
 * mode to run before a French release. Everything else in this file -- stale
 * keys, placeholders, empty and untranslated values -- always fails.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';

import en from '../../../../src/frontend/src/i18n/locales/en.json';
import fr from '../../../../src/frontend/src/i18n/locales/fr.json';
import enPro from '../../../../src/frontend/src/i18n/locales/en.pro.json';
import frPro from '../../../../src/frontend/src/i18n/locales/fr.pro.json';
import i18n from '../../../../src/frontend/src/i18n';

const STRICT = process.env.RENFIELD_I18N_STRICT === '1';

type Flat = Record<string, string>;

/** Flatten a nested bundle into `a.b.c -> value`; fails on a non-string leaf. */
function flatten(node: unknown, prefix = '', out: Flat = {}): Flat {
  if (typeof node === 'string') {
    out[prefix] = node;
    return out;
  }
  if (node === null || typeof node !== 'object' || Array.isArray(node)) {
    throw new Error(`Unexpected leaf at "${prefix}": ${JSON.stringify(node)}`);
  }
  for (const [key, value] of Object.entries(node)) {
    flatten(value, prefix ? `${prefix}.${key}` : key, out);
  }
  return out;
}

const PLURAL_SUFFIX = /_(one|many|other)$/;
const PLACEHOLDER = /\{\{\s*([^}]+?)\s*\}\}/g;
const TAG = /<\/?[A-Za-z][^>]*>/g;

function sortedMatches(value: string, pattern: RegExp, group = 0): string[] {
  return [...value.matchAll(pattern)].map((m) => m[group]).sort();
}

/**
 * The English key a French key is checked against. `x_many` has no English
 * counterpart of its own and is measured against `x_other`.
 */
function referenceKey(frKey: string): string {
  return frKey.endsWith('_many') ? `${frKey.slice(0, -'_many'.length)}_other` : frKey;
}

/**
 * The placeholders of a value. `count` is ignored on plural forms: English
 * spells "1 LED" out in its `_one` form, but French `_one` also covers zero,
 * so the French form has to interpolate the number.
 */
function placeholders(key: string, value: string): string[] {
  const names = sortedMatches(value, PLACEHOLDER, 1);
  return PLURAL_SUFFIX.test(key) ? names.filter((name) => name !== 'count') : names;
}

/**
 * Values that are legitimately the same in both languages: shared vocabulary
 * (French "Documents", "Navigation"), product and protocol names, and names a
 * language is listed under in its own language. A French value equal to the
 * English one that is NOT in this list is an untranslated string.
 */
const SAME_IN_BOTH_LANGUAGES = new Set<string>([
  'Action', 'Actions', 'Administration', 'Agent', 'Alias', 'Chat', 'Concept',
  'Confirmation', 'Conversation', 'Conversations', 'Cron', 'Date', 'Description',
  'Deutsch', 'Direct', 'Document', 'Documents', 'Embeddings', 'English', 'Focus',
  'Home Assistant', 'Importance', 'Information', 'Invitation', 'Maintenance',
  'Mentions', 'Message', 'Microphone', 'Mode', 'Navigation', 'Note', 'Notes',
  'Notifications', 'OK', 'Page', 'Pages', 'Plugins', 'Public', 'Relation',
  'Relations', 'Renfield', 'Route', 'Routine', 'Satellite', 'Satellites',
  'Section', 'Silence', 'Smartphone', 'Sources', 'Table', 'Total', 'Type',
  'Types', 'VAD', 'Wissensbasis',
  'Page {{page}}', 'Pages {{from}}–{{to}}', 'Position {{position}}',
  'Relations ({{count}})', 'mcp.ha.turn_on\nmcp.ha.get_state',
  '{{count}} document', '{{count}} documents', '{{count}} mention',
  '{{count}} mentions', '{{count}} message', '{{count}} messages',
  '{{count}} relation', '{{count}} relations', '{{n}} min', '{{name}} via SSO',
]);

/** True when a value carries no words at all (only placeholders, digits, signs). */
function hasNoWords(value: string): boolean {
  return !/\p{L}/u.test(value.replace(PLACEHOLDER, ''));
}

const EN = flatten(en);
const FR = flatten(fr);

describe('French locale bundle', () => {
  it('is not trivially small (otherwise the rest checks nothing)', () => {
    expect(Object.keys(EN).length).toBeGreaterThan(2000);
    expect(Object.keys(FR).length).toBeGreaterThan(2000);
  });

  it('has no key the English bundle lacks, apart from the French _many forms', () => {
    const stale = Object.keys(FR).filter((key) => !(referenceKey(key) in EN)).sort();
    expect(stale).toEqual([]);
  });

  it('has a _many form for every English _other form', () => {
    const missing = Object.keys(EN)
      .filter((key) => key.endsWith('_other') && key in FR)
      .map((key) => `${key.slice(0, -'_other'.length)}_many`)
      .filter((key) => !(key in FR));
    expect(missing).toEqual([]);
  });

  it('covers every English key (reported; enforced under RENFIELD_I18N_STRICT=1)', () => {
    const missing = Object.keys(EN).filter((key) => !(key in FR)).sort();
    if (missing.length > 0 && !STRICT) {
      console.warn(
        `fr.json is behind en.json by ${missing.length} key(s); they fall back to German:\n  `
        + missing.join('\n  '),
      );
      return;
    }
    expect(missing).toEqual([]);
  });

  it('keeps the placeholders of every key', () => {
    const drifted: string[] = [];
    for (const [key, value] of Object.entries(FR)) {
      const reference = EN[referenceKey(key)];
      if (reference === undefined) continue; // reported by the stale-key test
      const want = placeholders(key, reference);
      const got = placeholders(key, value);
      if (want.join('|') !== got.join('|')) {
        drifted.push(`${key}: en {${want.join(', ')}} vs fr {${got.join(', ')}}`);
      }
    }
    expect(drifted).toEqual([]);
  });

  it('keeps embedded tags and line breaks of every key', () => {
    const drifted: string[] = [];
    for (const [key, value] of Object.entries(FR)) {
      const reference = EN[referenceKey(key)];
      if (reference === undefined) continue;
      const sameTags = sortedMatches(reference, TAG).join('|') === sortedMatches(value, TAG).join('|');
      const sameBreaks = reference.split('\n').length === value.split('\n').length;
      if (!sameTags || !sameBreaks) drifted.push(key);
    }
    expect(drifted).toEqual([]);
  });

  it('has no empty value', () => {
    const empty = Object.entries(FR).filter(([, value]) => value.trim() === '').map(([key]) => key);
    expect(empty).toEqual([]);
  });

  it('leaves no value untranslated beyond the shared-vocabulary allowlist', () => {
    const untranslated = Object.entries(FR)
      .filter(([key, value]) => EN[key] === value)
      .filter(([, value]) => !hasNoWords(value) && !SAME_IN_BOTH_LANGUAGES.has(value))
      .map(([key, value]) => `${key}: ${JSON.stringify(value)}`);
    expect(untranslated).toEqual([]);
  });

  it('lists nothing in the allowlist that is no longer needed', () => {
    // A stale entry would silently excuse a future untranslated string.
    const used = new Set(
      Object.entries(FR).filter(([key, value]) => EN[key] === value).map(([, value]) => value),
    );
    const unused = [...SAME_IN_BOTH_LANGUAGES].filter((value) => !used.has(value));
    expect(unused).toEqual([]);
  });

  it('puts a no-break space before double punctuation, as French typography asks', () => {
    const plainSpace = Object.entries(FR)
      .filter(([, value]) => /\S [?!:;»]/.test(value) || /« /.test(value))
      .map(([key]) => key);
    expect(plainSpace).toEqual([]);
  });
});

describe('French pro-edition overlay', () => {
  const EN_PRO = flatten(enPro);
  const FR_PRO = flatten(frPro);

  it('overrides exactly the keys the English overlay overrides', () => {
    expect(Object.keys(FR_PRO).sort()).toEqual(Object.keys(EN_PRO).sort());
  });

  it('translates every override and keeps its placeholders', () => {
    // The overlay may introduce keys the base bundle does not have
    // (`auth.tenantPrefix`), so it is measured against the English overlay only.
    const problems: string[] = [];
    for (const [key, value] of Object.entries(FR_PRO)) {
      if (key === '_comment') continue;
      const reference = EN_PRO[key];
      if (reference === undefined) continue; // reported by the key-set test
      if (placeholders(key, reference).join('|') !== placeholders(key, value).join('|')) {
        problems.push(`${key}: placeholders differ`);
      }
      if (value.trim() === '') problems.push(`${key}: empty`);
      if (value === reference && !SAME_IN_BOTH_LANGUAGES.has(value)) {
        problems.push(`${key}: untranslated`);
      }
    }
    expect(problems).toEqual([]);
  });
});

describe('French at runtime', () => {
  it('is a registered, supported language', () => {
    expect(i18n.options.supportedLngs).toContain('fr');
    expect(i18n.hasResourceBundle('fr', 'translation')).toBe(true);
  });

  it('resolves keys in French, not through the German fallback', () => {
    const t = i18n.getFixedT('fr');
    expect(t('common.save')).toBe('Enregistrer');
    expect(t('chat.subtitle')).toBe('Discutez avec Renfield');
  });

  it.each([
    [0, '0 outil'],
    [1, '1 outil'],
    [2, '2 outils'],
    [1000000, "1000000 d'outils"],
  ])('picks the French plural form for a count of %i', (count, expected) => {
    expect(i18n.getFixedT('fr')('kiosk.toolHint', { count })).toBe(expected);
  });

  it('falls back to the unsuffixed key where English keeps its singular there', () => {
    const t = i18n.getFixedT('fr');
    expect(t('chat.messageCount', { count: 1 })).toBe('1 message');
    expect(t('chat.messageCount', { count: 3 })).toBe('3 messages');
    expect(t('projects.documentCount', { count: 1 })).toBe('1 document');
    expect(t('projects.documentCount', { count: 3 })).toBe('3 documents');
  });
});

describe('French in the pro edition', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it('lays the overlay over the base bundle', async () => {
    vi.stubEnv('VITE_APP_EDITION', 'pro');
    vi.resetModules();
    const { default: proI18n } = await import('../../../../src/frontend/src/i18n');
    const t = proI18n.getFixedT('fr');

    expect(t('circles.tier.2')).toBe('Équipe');          // overridden by fr.pro.json
    expect(t('nav.circles')).toBe('Visibilité');
    expect(t('common.save')).toBe('Enregistrer');        // falls through to fr.json
    expect(t('circles.tier.4')).toBe('Public');          // sibling of an overridden key survives
  });
});
