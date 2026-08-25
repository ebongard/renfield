import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, renderHook, act } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import { AuthProvider, useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Shape of /api/auth/me used by these tests. Mirrors the JSON the real
// backend returns and what AuthContext drops verbatim into `user` state via
// `setUser(response.data)`. The real `User` type in `types/api.ts` carries
// extra server-only fields (is_active, role_id, timestamps) that the auth
// status hooks under test never read — keep this local to be precise about
// the test fixture.
interface AuthMeFixture {
  id: number;
  username: string;
  role: string; // tests pass a plain role label, not the full Role object
  permissions: string[];
}

interface AuthStatusFixture {
  auth_enabled: boolean;
  allow_registration: boolean;
}

// The token key used by AuthContext (private constant; mirrored here)
const ACCESS_TOKEN_KEY = 'renfield_access_token';

// Test component that uses the auth context
function TestComponent() {
  const {
    user,
    isAuthenticated,
    authEnabled,
    hasPermission,
    hasAnyPermission,
    isAdmin,
    loading,
  } = useAuth();

  if (loading) {
    return <div>Loading...</div>;
  }

  return (
    <div>
      <div data-testid="auth-enabled">{authEnabled ? 'true' : 'false'}</div>
      <div data-testid="is-authenticated">{isAuthenticated ? 'true' : 'false'}</div>
      <div data-testid="is-admin">{isAdmin() ? 'true' : 'false'}</div>
      <div data-testid="username">{user?.username || 'none'}</div>
      <div data-testid="has-plugins-manage">{hasPermission('plugins.manage') ? 'true' : 'false'}</div>
      <div data-testid="has-plugins-use">{hasPermission('plugins.use') ? 'true' : 'false'}</div>
      <div data-testid="has-plugins-none">{hasPermission('plugins.none') ? 'true' : 'false'}</div>
      <div data-testid="has-any-plugin-perm">
        {hasAnyPermission(['plugins.use', 'plugins.manage']) ? 'true' : 'false'}
      </div>
      <div data-testid="permissions">{JSON.stringify(user?.permissions || [])}</div>
    </div>
  );
}

describe('AuthContext', () => {
  beforeEach(() => {
    server.resetHandlers();
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  describe('Cookie mode (XSS hardening)', () => {
    it('login persists NEITHER token to localStorage when auth_cookie_enabled', async () => {
      server.use(
        http.get(`${BASE_URL}/api/auth/status`, () =>
          HttpResponse.json({
            auth_enabled: true,
            allow_registration: false,
            auth_cookie_enabled: true,
          })
        ),
        http.get(`${BASE_URL}/api/auth/me`, () =>
          HttpResponse.json({ id: 1, username: 'u', role: 'Admin', permissions: ['admin'] })
        ),
        http.post(`${BASE_URL}/api/auth/login`, () =>
          HttpResponse.json({
            access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 3600,
          })
        )
      );

      const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider });
      await waitFor(() => expect(result.current.loading).toBe(false));
      await act(async () => {
        await result.current.login('u', 'pw');
      });

      // The whole point of the migration: in cookie mode the session lives only
      // in the HttpOnly cookies — an XSS has nothing to read from localStorage.
      expect(localStorage.getItem('renfield_access_token')).toBeNull();
      expect(localStorage.getItem('renfield_refresh_token')).toBeNull();
      expect(result.current.isAuthenticated).toBe(true);
    });

    it('login DOES persist tokens to localStorage when cookie mode is off (legacy)', async () => {
      server.use(
        http.get(`${BASE_URL}/api/auth/status`, () =>
          HttpResponse.json({ auth_enabled: true, allow_registration: false })
        ),
        http.get(`${BASE_URL}/api/auth/me`, () =>
          HttpResponse.json({ id: 1, username: 'u', role: 'Admin', permissions: ['admin'] })
        ),
        http.post(`${BASE_URL}/api/auth/login`, () =>
          HttpResponse.json({
            access_token: 'a', refresh_token: 'r', token_type: 'bearer', expires_in: 3600,
          })
        )
      );

      const { result } = renderHook(() => useAuth(), { wrapper: AuthProvider });
      await waitFor(() => expect(result.current.loading).toBe(false));
      await act(async () => {
        await result.current.login('u', 'pw');
      });

      expect(localStorage.getItem('renfield_access_token')).toBe('a');
      expect(localStorage.getItem('renfield_refresh_token')).toBe('r');
    });
  });

  describe('Initialization', () => {
    it('checks auth status on mount', async () => {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('auth-enabled').textContent).toBe('true');
      });
    });

    it('handles auth disabled', async () => {
      server.use(
        http.get(`${BASE_URL}/api/auth/status`, () => {
          return HttpResponse.json<AuthStatusFixture>({
            auth_enabled: false,
            allow_registration: false,
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('auth-enabled').textContent).toBe('false');
      });
    });
  });

  describe('Plugin Permissions', () => {
    it('hasPermission returns true for matching permission', async () => {
      // Set up authenticated user with plugins.manage
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      server.use(
        http.get(`${BASE_URL}/api/auth/me`, () => {
          return HttpResponse.json<AuthMeFixture>({
            id: 1,
            username: 'admin',
            role: 'Admin',
            permissions: ['admin', 'plugins.manage', 'kb.all'],
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('has-plugins-manage').textContent).toBe('true');
      });
    });

    it('hasPermission returns false for missing permission', async () => {
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      server.use(
        http.get(`${BASE_URL}/api/auth/me`, () => {
          return HttpResponse.json<AuthMeFixture>({
            id: 1,
            username: 'user',
            role: 'User',
            permissions: ['plugins.use', 'kb.own'], // No plugins.manage
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('has-plugins-manage').textContent).toBe('false');
      });

      expect(screen.getByTestId('has-plugins-use').textContent).toBe('true');
    });

    it('hasAnyPermission returns true if any permission matches', async () => {
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      server.use(
        http.get(`${BASE_URL}/api/auth/me`, () => {
          return HttpResponse.json<AuthMeFixture>({
            id: 1,
            username: 'user',
            role: 'User',
            permissions: ['plugins.use'], // Only plugins.use
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        // Should be true because user has plugins.use
        expect(screen.getByTestId('has-any-plugin-perm').textContent).toBe('true');
      });
    });

    it('hasAnyPermission returns false if no permissions match', async () => {
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      server.use(
        http.get(`${BASE_URL}/api/auth/me`, () => {
          return HttpResponse.json<AuthMeFixture>({
            id: 1,
            username: 'guest',
            role: 'Guest',
            permissions: ['plugins.none', 'ha.read'], // No plugins.use or plugins.manage
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('has-any-plugin-perm').textContent).toBe('false');
      });
    });

    it('returns all permissions when auth is disabled', async () => {
      server.use(
        http.get(`${BASE_URL}/api/auth/status`, () => {
          return HttpResponse.json<AuthStatusFixture>({
            auth_enabled: false,
            allow_registration: false,
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('auth-enabled').textContent).toBe('false');
      });

      // When auth is disabled, hasPermission should return true for any permission
      expect(screen.getByTestId('has-plugins-manage').textContent).toBe('true');
      expect(screen.getByTestId('has-plugins-use').textContent).toBe('true');
    });
  });

  describe('Admin Check', () => {
    it('isAdmin returns true for admin user', async () => {
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      server.use(
        http.get(`${BASE_URL}/api/auth/me`, () => {
          return HttpResponse.json<AuthMeFixture>({
            id: 1,
            username: 'admin',
            role: 'Admin',
            permissions: ['admin', 'plugins.manage'],
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('is-admin').textContent).toBe('true');
      });
    });

    it('isAdmin returns false for non-admin user', async () => {
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      server.use(
        http.get(`${BASE_URL}/api/auth/me`, () => {
          return HttpResponse.json<AuthMeFixture>({
            id: 1,
            username: 'user',
            role: 'User',
            permissions: ['plugins.use'],
          });
        })
      );

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('is-admin').textContent).toBe('false');
      });
    });
  });

  describe('Authentication State', () => {
    it('isAuthenticated is true when token exists and user loaded', async () => {
      localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('is-authenticated').textContent).toBe('true');
      });
    });

    it('isAuthenticated is false when no token', async () => {
      render(
        <AuthProvider>
          <TestComponent />
        </AuthProvider>
      );

      await waitFor(() => {
        expect(screen.getByTestId('is-authenticated').textContent).toBe('false');
      });
    });
  });
});

describe('AuthContext with different permission levels', () => {
  beforeEach(() => {
    server.resetHandlers();
    localStorage.clear();
  });

  it('correctly identifies plugins.none permission', async () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

    server.use(
      http.get(`${BASE_URL}/api/auth/me`, () => {
        return HttpResponse.json<AuthMeFixture>({
          id: 1,
          username: 'guest',
          role: 'Guest',
          permissions: ['plugins.none'],
        });
      })
    );

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('has-plugins-none').textContent).toBe('true');
    });

    expect(screen.getByTestId('has-plugins-use').textContent).toBe('false');
    expect(screen.getByTestId('has-plugins-manage').textContent).toBe('false');
  });

  it('correctly identifies plugins.use permission', async () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

    server.use(
      http.get(`${BASE_URL}/api/auth/me`, () => {
        return HttpResponse.json<AuthMeFixture>({
          id: 1,
          username: 'user',
          role: 'User',
          permissions: ['plugins.use'],
        });
      })
    );

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('has-plugins-use').textContent).toBe('true');
    });

    expect(screen.getByTestId('has-plugins-manage').textContent).toBe('false');
  });

  it('correctly identifies plugins.manage permission', async () => {
    localStorage.setItem(ACCESS_TOKEN_KEY, 'mock-token');

    server.use(
      http.get(`${BASE_URL}/api/auth/me`, () => {
        return HttpResponse.json<AuthMeFixture>({
          id: 1,
          username: 'admin',
          role: 'Admin',
          permissions: ['plugins.manage'],
        });
      })
    );

    render(
      <AuthProvider>
        <TestComponent />
      </AuthProvider>
    );

    await waitFor(() => {
      expect(screen.getByTestId('has-plugins-manage').textContent).toBe('true');
    });
  });
});
