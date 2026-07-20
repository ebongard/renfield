import { describe, it, expect } from 'vitest';
import {
  LENSES, ATOM_LENS_SEGMENT, isLensVisible, lensForSegment,
  type LensVisibilityAuth,
} from '../../../../../src/frontend/src/pages/wissen/lenses';

describe('Notizen lens registration (4B.3)', () => {
  it('registers a Notizen lens gated on the notes feature', () => {
    const lens = LENSES.find((l) => l.key === 'notizen');
    expect(lens).toBeDefined();
    expect(lens!.segment).toBe('notes');
    expect(lens!.feature).toBe('notes');
    expect(lens!.atomTypes).toContain('note');
    expect(lensForSegment('notes')?.key).toBe('notizen');
  });

  it('routes the `note` atom type to the notes lens segment', () => {
    expect(ATOM_LENS_SEGMENT.note).toBe('notes');
  });

  it('is hidden when the notes feature is off, visible when on', () => {
    const lens = LENSES.find((l) => l.key === 'notizen')!;
    const off: LensVisibilityAuth = { authEnabled: true, isFeatureEnabled: () => false, hasAnyPermission: () => true };
    const on: LensVisibilityAuth = { authEnabled: true, isFeatureEnabled: (f) => f === 'notes', hasAnyPermission: () => true };
    expect(isLensVisible(lens, off)).toBe(false);
    expect(isLensVisible(lens, on)).toBe(true);
  });
});
