/**
 * Jeder Refus-Code des Abgleichers hat in JEDER Sprache einen Satz.
 *
 * Warum als eigener Test und nicht als Zusicherung in der Komponente: der
 * Fehlermodus, gegen den PR #1334 gebaut ist, ist "das Backend kennt einen
 * Grund, den die Oberflaeche nicht aussprechen kann". Ein Komponententest
 * prueft den Weg fuer die Codes, die jemand aufgeschrieben hat. Dieser hier
 * LIEST die Codeliste aus dem Dienst und kann deshalb auch den fangen, den
 * noch niemand aufgeschrieben hat: ein neuer `res.refusal_code = "..."` ohne
 * Schluessel faerbt diese Datei rot, ohne dass jemand daran denken muss.
 *
 * Zweitens die Sprachparitaet. Der adversariale Durchgang hat sie von Hand
 * nachgerechnet und in Ordnung befunden — aber KEIN Tor bewachte sie, und die
 * Komponententests laufen ausschliesslich auf Deutsch (`test-utils.tsx` setzt
 * `changeLanguage('de')`). Ein fehlender Schluessel in `it.json` waere gruen
 * durchgegangen und im Betrieb als roher Schluesselname erschienen.
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

/** Die Codes, die `resolve_cluster` tatsaechlich setzen kann. */
function backendRefusalCodes(): string[] {
  const src = readFileSync(SERVICE, 'utf8');
  const found = [...src.matchAll(/refusal_code = "([a-z_]+)"/g)].map((m) => m[1]);
  return [...new Set(found)].sort();
}

/** Der i18n-Schluessel, den `refusalMessage()` aus einem Code baut. */
function keyFor(code: string): string[] {
  // Der Paar-Refus hat eine eigene, reichere Darstellung (Namen + Gesamtzahl)
  // und deshalb andere Schluessel — die deckt der Komponententest ab.
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

describe('Refus-Codes und Sprachdateien', () => {
  it('kennt ueberhaupt Codes (sonst prueft der Rest nichts)', () => {
    // Ohne diese Zusicherung wuerde eine kaputte Regex die ganze Datei
    // stillschweigend zu einer leeren Schleife machen — gruen und wertlos.
    const codes = backendRefusalCodes();
    expect(codes.length).toBeGreaterThanOrEqual(7);
  });

  it.each(Object.keys(LOCALES))(
    'hat in %s fuer jeden Backend-Code einen nicht-leeren Satz',
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

  it('haelt die drei Sprachen im Cluster-Zweig deckungsgleich', () => {
    const keys = (loc: string): string[] => Object.keys(
      (lookup(LOCALES[loc], 'circles.mergeProposals.cluster') ?? {}) as object,
    ).sort();
    expect(keys('en')).toEqual(keys('de'));
    expect(keys('it')).toEqual(keys('de'));
  });

  it('hat einen allgemeinen Rueckfall fuer einen UNBEKANNTEN Code', () => {
    // Der Rueckfall ist der einzige Grund, warum ein Backend, das dem Bundle
    // vorauseilt (zwischengespeicherter Service Worker), keine leere Zeile
    // zeigt. Er darf in keiner Sprache fehlen.
    for (const loc of Object.keys(LOCALES)) {
      const v = lookup(LOCALES[loc], 'circles.mergeProposals.cluster.refused_generic');
      expect(typeof v === 'string' && v.trim() !== '').toBe(true);
    }
  });
});
