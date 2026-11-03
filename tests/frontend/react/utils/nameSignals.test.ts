/**
 * The view's copy of the backend's digit-token rule (#1329). Same cases as
 * tests/backend/test_kg_reconciler_low_signal.py.
 */
import { describe, it, expect } from 'vitest';
import { namesDifferOnlyInDigitTokens } from '../../../../src/frontend/src/utils/nameSignals';

describe('namesDifferOnlyInDigitTokens', () => {
  it.each([
    ['Product A - 1.2.4', 'Product A - 1.2.3'], // version = one whitespace token
    ['Konto 01', 'Konto 02'],                   // ordinal
    ['Bericht 2025', 'Bericht 2024'],           // year
    ['Bericht 2025', 'Bericht'],                // added / dropped number
    ['Raum 1', 'Raum 12'],
    ['Produkt-1.2.3', 'Produkt-1.2.4'],         // digit glued to the word
    ['Release v2', 'release V3'],               // case-insensitive
  ])('%s / %s differ only in a number', (a, b) => {
    expect(namesDifferOnlyInDigitTokens(a, b)).toBe(true);
  });

  it.each([
    ['Product A - 1.2.3', 'Product A - 1.2.3'],   // equal
    ['Product A - 1.2.3', 'product a -  1.2.3'],  // equal after normalisation
    ['1.2.3 Product', 'Product 1.2.3'],           // reordering only
    ['Acme GmbH', 'Acme GmbG'],                   // differing token has no digit
    ['Raum²', 'Raum³'],                           // superscripts are not decimal digits (parity with isdecimal)
    ['Product A 1.2.3', 'Product B 1.2.4'],       // a non-digit token differs too
  ])('%s / %s is not an identifier difference', (a, b) => {
    expect(namesDifferOnlyInDigitTokens(a, b)).toBe(false);
  });

  it('empty on both sides is no difference', () => {
    expect(namesDifferOnlyInDigitTokens(null, '')).toBe(false);
  });
});
