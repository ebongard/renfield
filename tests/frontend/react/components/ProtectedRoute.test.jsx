import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithProviders } from '../test-utils.jsx';
import ProtectedRoute, { AdminRoute } from '../../../../src/frontend/src/components/ProtectedRoute';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

// Mock Navigate component
vi.mock('react-router', async () => {
  const actual = await vi.importActual('react-router');
  return {
    ...actual,
    Navigate: ({ to }) => <div data-testid="navigate" data-to={to}>Redirecting to {to}</div>,
    useLocation: () => ({ pathname: '/protected', state: null })
  };
});

// Test child component
const TestChild = () => <div data-testid="protected-content">Protected Content</div>;

describe('ProtectedRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Loading State', () => {
    it('shows loading spinner while checking auth', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: false,
        authEnabled: true,
        loading: true,
        hasPermission: () => false,
        hasAnyPermission: () => false
      });

      renderWithProviders(
        <ProtectedRoute>
          <TestChild />
        </ProtectedRoute>
      );

      // Should not show content while loading
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });
  });

  describe('Auth Disabled', () => {
    it('allows access when auth is disabled', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: false,
        authEnabled: false,
        loading: false,
        hasPermission: () => true,
        hasAnyPermission: () => true
      });

      renderWithProviders(
        <ProtectedRoute>
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });
  });

  describe('Unauthenticated User', () => {
    it('redirects to login when not authenticated', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: false,
        authEnabled: true,
        loading: false,
        hasPermission: () => false,
        hasAnyPermission: () => false
      });

      renderWithProviders(
        <ProtectedRoute>
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByTestId('navigate')).toHaveAttribute('data-to', '/login');
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });
  });

  describe('Authenticated User', () => {
    it('allows access when authenticated without permission requirement', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: true,
        authEnabled: true,
        loading: false,
        hasPermission: () => true,
        hasAnyPermission: () => true
      });

      renderWithProviders(
        <ProtectedRoute>
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });
  });

  describe('Permission Checks', () => {
    it('allows access when user has required permission', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: true,
        authEnabled: true,
        loading: false,
        hasPermission: (perm) => perm === 'admin',
        hasAnyPermission: () => true
      });

      renderWithProviders(
        <ProtectedRoute permission="admin">
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('shows access denied when user lacks permission', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: true,
        authEnabled: true,
        loading: false,
        hasPermission: () => false,
        hasAnyPermission: () => false
      });

      renderWithProviders(
        <ProtectedRoute permission="admin">
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByText('Access Denied')).toBeInTheDocument();
      expect(screen.getByText(/you don't have permission/i)).toBeInTheDocument();
      expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument();
    });

    it('allows access when user has any of multiple permissions (requireAny)', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: true,
        authEnabled: true,
        loading: false,
        hasPermission: (perm) => perm === 'plugins.use',
        hasAnyPermission: (perms) => perms.includes('plugins.use')
      });

      renderWithProviders(
        <ProtectedRoute permission={['admin', 'plugins.use']} requireAny={true}>
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });

    it('denies access when user lacks all permissions (requireAny=false)', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: true,
        authEnabled: true,
        loading: false,
        hasPermission: (perm) => perm === 'plugins.use',
        hasAnyPermission: () => true
      });

      renderWithProviders(
        <ProtectedRoute permission={['admin', 'plugins.use']} requireAny={false}>
          <TestChild />
        </ProtectedRoute>
      );

      // Should be denied because user doesn't have 'admin'
      expect(screen.getByText('Access Denied')).toBeInTheDocument();
    });

    it('allows access when user has all required permissions', () => {
      vi.mocked(useAuth).mockReturnValue({
        isAuthenticated: true,
        authEnabled: true,
        loading: false,
        hasPermission: () => true,
        hasAnyPermission: () => true
      });

      renderWithProviders(
        <ProtectedRoute permission={['admin', 'plugins.manage']} requireAny={false}>
          <TestChild />
        </ProtectedRoute>
      );

      expect(screen.getByTestId('protected-content')).toBeInTheDocument();
    });
  });
});

describe('AdminRoute', () => {
  it('allows access for admin users', () => {
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: true,
      authEnabled: true,
      loading: false,
      hasPermission: (perm) => perm === 'admin',
      hasAnyPermission: () => true
    });

    renderWithProviders(
      <AdminRoute>
        <TestChild />
      </AdminRoute>
    );

    expect(screen.getByTestId('protected-content')).toBeInTheDocument();
  });

  it('denies access for non-admin users', () => {
    vi.mocked(useAuth).mockReturnValue({
      isAuthenticated: true,
      authEnabled: true,
      loading: false,
      hasPermission: () => false,
      hasAnyPermission: () => false
    });

    renderWithProviders(
      <AdminRoute>
        <TestChild />
      </AdminRoute>
    );

    expect(screen.getByText('Access Denied')).toBeInTheDocument();
  });
});
