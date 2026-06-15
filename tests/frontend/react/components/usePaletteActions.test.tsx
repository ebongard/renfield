/**
 * usePaletteActions — palette display-gate (permission + feature-flag filter).
 */
import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { I18nextProvider } from 'react-i18next';
import type { ReactNode } from 'react';
import i18n from '../../../../src/frontend/src/i18n';

// usePaletteActions calls useTranslation(); without the provider t() returns the
// raw key and the label-based query filter can't match. Wrap renderHook with i18n.
i18n.changeLanguage('de');
const wrapper = ({ children }: { children: ReactNode }) => (
  <I18nextProvider i18n={i18n}>{children}</I18nextProvider>
);

const authState = {
  hasPermission: vi.fn<(p: string) => boolean>(),
  hasAnyPermission: vi.fn<(ps: string[]) => boolean>(),
};
const featureState = { data: { wissen_workspace_enabled: false } as Record<string, boolean> };

vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: () => authState,
}));
vi.mock('../../../../src/frontend/src/api/resources/brain', () => ({
  useFeatureFlags: () => featureState,
}));

import { usePaletteActions } from '../../../../src/frontend/src/components/chat/palette/usePaletteActions';

describe('usePaletteActions', () => {
  it('admin (all permissions) sees the full catalog incl. gated actions', () => {
    authState.hasPermission.mockReturnValue(true);
    authState.hasAnyPermission.mockReturnValue(true);
    const { result } = renderHook(() => usePaletteActions(''), { wrapper });
    const ids = result.current.visible.map((a) => a.id);
    expect(ids).toContain('tool.bt_scan');        // needs ha.control
    expect(ids).toContain('nav.graph');           // needs kg.view
    expect(ids).toContain('nav.brain');           // ungated
  });

  it('hides permission-gated actions the user lacks', () => {
    // No HA / KG / camera / rooms perms; only ungated actions survive.
    authState.hasPermission.mockReturnValue(false);
    authState.hasAnyPermission.mockReturnValue(false);
    const { result } = renderHook(() => usePaletteActions(''), { wrapper });
    const ids = result.current.visible.map((a) => a.id);
    expect(ids).not.toContain('tool.bt_scan');    // ha.control hidden
    expect(ids).not.toContain('nav.graph');       // kg.view hidden
    expect(ids).toContain('nav.brain');           // ungated still shown
    expect(ids).toContain('tool.presence');       // ungated
  });

  it('query filters by localised label', () => {
    authState.hasPermission.mockReturnValue(true);
    authState.hasAnyPermission.mockReturnValue(true);
    const { result } = renderHook(() => usePaletteActions('bluetooth'), { wrapper });
    const ids = result.current.filtered.map((a) => a.id);
    expect(ids).toEqual(['tool.bt_scan']);
  });

  it('resolves wissen-workspace nav paths when the flag is on', () => {
    authState.hasPermission.mockReturnValue(true);
    authState.hasAnyPermission.mockReturnValue(true);
    featureState.data.wissen_workspace_enabled = true;
    const { result } = renderHook(() => usePaletteActions(''), { wrapper });
    const brain = result.current.visible.find((a) => a.id === 'nav.brain');
    expect(brain?.to).toBe('/wissen');
    featureState.data.wissen_workspace_enabled = false; // reset
  });
});
