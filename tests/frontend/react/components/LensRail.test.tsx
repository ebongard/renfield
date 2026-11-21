/**
 * LensRail — per-lens visibility gating (plan-eng-review D2 / regression #4).
 *
 * The rail must hide a lens the user can't reach: Graph when the
 * `knowledge_graph` feature is off, Dokumente when the user lacks kb
 * permission. Otherwise users see tabs that 403 or load empty.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithRouter } from '../test-utils';
import LensRail from '../../../../src/frontend/src/components/wissen/LensRail';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

type AuthValue = ReturnType<typeof useAuth>;

vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn(),
}));
const mockUseAuth = vi.mocked(useAuth);

function buildAuth(overrides: Partial<AuthValue> = {}): AuthValue {
  return {
    user: null,
    loading: false,
    authEnabled: true,
    allowRegistration: false,
    ssoProviders: [],
    passwordForm: 'visible',
    isAuthenticated: true,
    features: {},
    isFeatureEnabled: () => true,
    login: vi.fn(),
    logout: vi.fn(),
    register: vi.fn(),
    changePassword: vi.fn(),
    fetchUser: vi.fn(),
    hasPermission: () => true,
    hasAnyPermission: () => true,
    isAdmin: () => true,
    getAccessToken: () => null,
    ...overrides,
  };
}

beforeEach(() => mockUseAuth.mockReset());

// Labels render in both the mobile <select> option and the desktop <nav> link,
// so count occurrences rather than expecting exactly one.
const count = (label: string) => screen.queryAllByText(label).length;

describe('LensRail', () => {
  it('hides the Graph lens when the knowledge_graph feature is off', () => {
    mockUseAuth.mockReturnValue(
      buildAuth({ isFeatureEnabled: (f: string) => f !== 'knowledge_graph' }),
    );
    renderWithRouter(<LensRail />, { route: '/wissen' });

    expect(count('Übersicht')).toBeGreaterThan(0);
    expect(count('Graph')).toBe(0);
  });

  it('hides the Dokumente lens when the user lacks kb permission', () => {
    mockUseAuth.mockReturnValue(
      buildAuth({ hasAnyPermission: (perms: string[]) => !perms.includes('kb.own') }),
    );
    renderWithRouter(<LensRail />, { route: '/wissen' });

    expect(count('Dokumente')).toBe(0);
    expect(count('Fristen')).toBeGreaterThan(0); // ungated lens still shows
  });

  it('shows every lens when fully permissioned with all features on', () => {
    mockUseAuth.mockReturnValue(buildAuth());
    renderWithRouter(<LensRail />, { route: '/wissen' });

    for (const label of ['Übersicht', 'Dokumente', 'Graph', 'Erinnerungen', 'Fristen', 'Prüfen']) {
      expect(count(label)).toBeGreaterThan(0);
    }
  });
});
