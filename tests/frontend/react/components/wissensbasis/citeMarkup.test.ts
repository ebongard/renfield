/**
 * The one parser for `<cite>` markup — shared by card text, the chat bubble
 * and read-aloud, so the three cannot drift apart.
 */
import { describe, it, expect } from 'vitest';

import {
  isValidEntity,
  splitCites,
  stripCites,
} from '../../../../../src/frontend/src/components/wissensbasis/citeMarkup';

describe('splitCites', () => {
  it('splits text and citations in order', () => {
    expect(
      splitCites('Offen: <cite entity="T-1" type="ticket">Login kaputt</cite> und <cite entity="T-2">Export</cite>.'),
    ).toEqual([
      'Offen: ',
      { entity: 'T-1', type: 'ticket', label: 'Login kaputt', valid: true },
      ' und ',
      { entity: 'T-2', type: undefined, label: 'Export', valid: true },
      '.',
    ]);
  });

  it('marks an id the validator rejects as invalid, keeping the label', () => {
    const [seg] = splitCites('<cite entity="../etc">Datei</cite>');
    expect(seg).toEqual({ entity: '../etc', type: undefined, label: 'Datei', valid: false });
  });

  it('leaves text without markup alone', () => {
    expect(splitCites('nur Text')).toEqual(['nur Text']);
  });

  it('is stable across calls (no shared lastIndex)', () => {
    const text = 'a <cite entity="X">x</cite> b';
    expect(splitCites(text)).toEqual(splitCites(text));
  });
});

describe('stripCites', () => {
  it('reduces every citation to its label', () => {
    expect(stripCites('Ticket <cite entity="T-1" type="ticket">Login kaputt</cite> ist offen.')).toBe(
      'Ticket Login kaputt ist offen.',
    );
  });

  it('drops a half tag left by a sentence split inside a label', () => {
    expect(stripCites('Siehe <cite entity="T-1" type="ticket">Nr. 1')).toBe('Siehe Nr. 1');
    expect(stripCites('ist erledigt</cite> heute.')).toBe('ist erledigt heute.');
  });
});

describe('isValidEntity', () => {
  it('accepts ids and path ids, rejects traversal and markup', () => {
    expect(isValidEntity('abc-123')).toBe(true);
    expect(isValidEntity('Applications/Folder1/Release2')).toBe(true);
    expect(isValidEntity('a/../b')).toBe(false);
    expect(isValidEntity('<script>')).toBe(false);
    expect(isValidEntity('')).toBe(false);
  });
});
