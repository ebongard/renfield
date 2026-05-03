/**
 * Shared admin auth mock for page tests.
 *
 * Provides a fully-typed `AuthContextValue` so tests can mock `useAuth`
 * without resorting to `as any` or partial casts.
 */
import type { AuthContextValue, AuthUser } from '../../../src/frontend/src/context/AuthContext';
import type { User } from '../../../src/frontend/src/types/api';

const baseUser: User = {
  id: 1,
  username: 'admin',
  is_active: true,
  role_id: 1,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

export const adminUser: AuthUser = {
  ...baseUser,
  permissions: ['admin'],
};

export const adminAuthMock: AuthContextValue = {
  user: adminUser,
  loading: false,
  authEnabled: true,
  allowRegistration: false,
  isAuthenticated: true,
  features: {},
  isFeatureEnabled: () => true,
  login: async () => ({
    access_token: 'mock',
    refresh_token: 'mock',
    token_type: 'bearer',
    user: baseUser,
  }),
  logout: () => {},
  register: async () => baseUser,
  changePassword: async () => ({ message: 'ok' }),
  fetchUser: async () => null,
  hasPermission: () => true,
  hasAnyPermission: () => true,
  isAdmin: () => true,
  getAccessToken: () => 'mock-token',
};

export const unauthenticatedAuthMock: AuthContextValue = {
  user: null,
  loading: false,
  authEnabled: true,
  allowRegistration: false,
  isAuthenticated: false,
  features: {},
  isFeatureEnabled: () => false,
  login: async () => ({
    access_token: 'mock',
    refresh_token: 'mock',
    token_type: 'bearer',
    user: baseUser,
  }),
  logout: () => {},
  register: async () => baseUser,
  changePassword: async () => ({ message: 'ok' }),
  fetchUser: async () => null,
  hasPermission: () => false,
  hasAnyPermission: () => false,
  isAdmin: () => false,
  getAccessToken: () => null,
};
