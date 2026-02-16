import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen } from '@testing-library/react';
import Layout from '../../../../src/frontend/src/components/Layout';
import { renderWithRouter } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

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
  ThemeProvider: ({ children }) => children,
}));

// Base auth mock with all features enabled (community edition)
const communityAuth = {
  user: { username: 'admin', role: 'Admin', permissions: ['admin'] },
  isAuthenticated: true,
  authEnabled: true,
  allowRegistration: false,
  loading: false,
  logout: vi.fn(),
  hasPermission: () => true,
  hasAnyPermission: () => true,
  isAdmin: () => true,
  getAccessToken: () => 'mock-token',
  features: { smart_home: true, cameras: true, satellites: true },
  isFeatureEnabled: () => true,
};

// Pro edition - all home features disabled
const proAuth = {
  ...communityAuth,
  features: { smart_home: false, cameras: false, satellites: false },
  isFeatureEnabled: () => false,
};

describe('Layout feature flags', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows camera nav item when cameras feature is enabled', () => {
    useAuth.mockReturnValue(communityAuth);
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.getByText('Kameras')).toBeInTheDocument();
  });

  it('hides camera nav item when cameras feature is disabled', () => {
    useAuth.mockReturnValue({
      ...communityAuth,
      features: { smart_home: true, cameras: false, satellites: true },
      isFeatureEnabled: (f) => f !== 'cameras',
    });
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Kameras')).not.toBeInTheDocument();
    // Chat should still be visible
    expect(screen.getByText('Chat')).toBeInTheDocument();
  });

  it('hides smart home nav item when smart_home feature is disabled', () => {
    useAuth.mockReturnValue({
      ...communityAuth,
      features: { smart_home: false, cameras: true, satellites: true },
      isFeatureEnabled: (f) => f !== 'smart_home',
    });
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Smart Home')).not.toBeInTheDocument();
  });

  it('hides satellites nav item when satellites feature is disabled', () => {
    useAuth.mockReturnValue({
      ...communityAuth,
      features: { smart_home: true, cameras: true, satellites: false },
      isFeatureEnabled: (f) => f !== 'satellites',
    });
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.queryByText('Satellites')).not.toBeInTheDocument();
  });

  it('hides all home features in pro edition', () => {
    useAuth.mockReturnValue(proAuth);
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
    useAuth.mockReturnValue(communityAuth);
    renderWithRouter(
      <Layout><div>content</div></Layout>,
    );
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Kameras')).toBeInTheDocument();
  });
});
