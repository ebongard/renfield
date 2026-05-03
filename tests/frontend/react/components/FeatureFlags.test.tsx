import type { ReactNode } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import Layout from '../../../../src/frontend/src/components/Layout';
import { renderWithRouter } from '../test-utils';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

type AuthValue = ReturnType<typeof useAuth>;

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn(),
}));

// Mock DeviceStatus to avoid unrelated side effects
vi.mock('../../../../src/frontend/src/components/DeviceStatus', () => ({
  default: () => <div data-testid="device-status">Device Status</div>,
}));

// Mock NotificationToast
vi.mock('../../../../src/frontend/src/components/NotificationToast', () => ({
  default: () => null,
}));

// Mock ThemeContext
vi.mock('../../../../src/frontend/src/context/ThemeContext', () => ({
  useTheme: () => ({ theme: 'light', isDark: false, setTheme: vi.fn(), toggleTheme: vi.fn() }),
  ThemeProvider: ({ children }: { children: ReactNode }) => children,
}));

const mockedUseAuth = vi.mocked(useAuth);

// Base auth mock with all features enabled (community edition)
const communityAuth: AuthValue = {
  user: {
    id: 1,
    username: 'admin',
    is_active: true,
    role_id: 1,
    role: {
      id: 1,
      name: 'Admin',
      permissions: ['admin'],
      created_at: '2024-01-01T00:00:00Z',
      updated_at: '2024-01-01T00:00:00Z',
    },
    permissions: ['admin'],
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
  },
  isAuthenticated: true,
  authEnabled: true,
  allowRegistration: false,
  loading: false,
  login: vi.fn(),
  logout: vi.fn(),
  register: vi.fn(),
  changePassword: vi.fn(),
  fetchUser: vi.fn(),
  hasPermission: () => true,
  hasAnyPermission: () => true,
  isAdmin: () => true,
  getAccessToken: () => 'mock-token',
  features: { smart_home: true, cameras: true, satellites: true },
  isFeatureEnabled: () => true,
};

// Pro edition - all home features disabled
const proAuth: AuthValue = {
  ...communityAuth,
  features: { smart_home: false, cameras: false, satellites: false },
  isFeatureEnabled: () => false,
};

describe('Layout feature flags', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows camera nav item when cameras feature is enabled', () => {
    mockedUseAuth.mockReturnValue(communityAuth);
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.getByText('Kameras')).toBeInTheDocument();
  });

  it('hides camera nav item when cameras feature is disabled', () => {
    mockedUseAuth.mockReturnValue({
      ...communityAuth,
      features: { smart_home: true, cameras: false, satellites: true },
      isFeatureEnabled: (f: string) => f !== 'cameras',
    });
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Kameras')).not.toBeInTheDocument();
    // Chat should still be visible
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('hides smart home nav item when smart_home feature is disabled', () => {
    mockedUseAuth.mockReturnValue({
      ...communityAuth,
      features: { smart_home: false, cameras: true, satellites: true },
      isFeatureEnabled: (f: string) => f !== 'smart_home',
    });
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Smart Home')).not.toBeInTheDocument();
  });

  it('hides satellites nav item when satellites feature is disabled', () => {
    mockedUseAuth.mockReturnValue({
      ...communityAuth,
      features: { smart_home: true, cameras: true, satellites: false },
      isFeatureEnabled: (f: string) => f !== 'satellites',
    });
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Satellites')).not.toBeInTheDocument();
  });

  it('hides all home features in pro edition', () => {
    mockedUseAuth.mockReturnValue(proAuth);
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Kameras')).not.toBeInTheDocument();
    expect(screen.queryByText('Smart Home')).not.toBeInTheDocument();
    expect(screen.queryByText('Satellites')).not.toBeInTheDocument();
    // Core features still visible
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('shows all nav items in community edition', () => {
    mockedUseAuth.mockReturnValue(communityAuth);
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Kameras')).toBeInTheDocument();
  });
});
