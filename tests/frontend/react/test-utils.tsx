import { render, type RenderOptions, type RenderResult } from '@testing-library/react';
import userEventDefault from '@testing-library/user-event';
import { BrowserRouter } from 'react-router';
import {
  createContext,
  useContext,
  type ReactElement,
  type ReactNode,
} from 'react';
import { I18nextProvider } from 'react-i18next';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import i18n from '../../../src/frontend/src/i18n';
import type { useAuth } from '../../../src/frontend/src/context/AuthContext';

/**
 * The real AuthContextValue interface is private to AuthContext.tsx, but
 * `useAuth(): AuthContextValue` is the public type-anchor — pulling its return
 * type gives us the same shape the production app sees, without copy-pasting.
 */
export type AuthValue = ReturnType<typeof useAuth>;

/**
 * Build a fresh QueryClient for each test to avoid cross-test cache pollution.
 * Both queries and mutations have retry disabled — tests should observe error
 * states immediately, not after a retry attempt.
 */
export function createTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

// Set default language to German for tests (matching production default)
i18n.changeLanguage('de');

// Create a mock auth context for testing.
// The defaultMockAuth below is fully populated; the test surface allows
// callers to pass `Partial<AuthValue>` to override individual fields.
const MockAuthContext = createContext<AuthValue | null>(null);

// Default mock auth values — must satisfy the real AuthContextValue shape
// returned by useAuth(). The user.role field is the embedded Role object
// (User.role: Role | undefined in src/frontend/src/types/api.ts), not a
// display string — keep it shaped that way so callers passing
// `authValues={{ user: ... }}` overrides also see the real shape.
const mockRoleObject = {
  id: 1,
  name: 'Admin',
  description: 'Admin role',
  permissions: ['admin', 'plugins.manage'],
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const mockUserBase = {
  id: 1,
  username: 'admin',
  is_active: true,
  role_id: 1,
  role: mockRoleObject,
  permissions: ['admin', 'plugins.manage'],
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const defaultMockAuth: AuthValue = {
  user: mockUserBase,
  isAuthenticated: true,
  authEnabled: true,
  allowRegistration: false,
  loading: false,
  features: { smart_home: true, cameras: true, satellites: true },
  isFeatureEnabled: (_feature: string) => true,
  login: async () => ({
    access_token: 'mock-token',
    refresh_token: 'mock-refresh',
    token_type: 'bearer' as const,
    user: mockUserBase,
  }),
  logout: () => {},
  register: async () => mockUserBase,
  changePassword: async () => ({ message: 'ok' }),
  fetchUser: async () => null,
  hasPermission: (_perm: string) => true,
  hasAnyPermission: (_perms: string[]) => true,
  isAdmin: () => true,
  getAccessToken: () => 'mock-token',
};

interface MockAuthProviderProps {
  children: ReactNode;
  authValues?: Partial<AuthValue>;
}

// Mock AuthProvider for testing
function MockAuthProvider({ children, authValues = {} }: MockAuthProviderProps) {
  const value: AuthValue = { ...defaultMockAuth, ...authValues };
  return (
    <MockAuthContext.Provider value={value}>
      {children}
    </MockAuthContext.Provider>
  );
}

// Mock useAuth hook for testing
export function useMockAuth(): AuthValue {
  const context = useContext(MockAuthContext);
  if (!context) {
    throw new Error('useMockAuth must be used within a MockAuthProvider');
  }
  return context;
}

export interface RenderWithProvidersOptions extends Omit<RenderOptions, 'wrapper'> {
  /** Initial route pushed onto window.history before render */
  route?: string;
  /** Partial overrides for the mock auth context */
  authValues?: Partial<AuthValue>;
  /** Pass an existing QueryClient (e.g. to assert cache state across actions) */
  queryClient?: QueryClient;
}

export interface RenderWithProvidersResult extends RenderResult {
  queryClient: QueryClient;
}

/**
 * Custom render function that wraps components with necessary providers
 * Use this for tests that need auth context
 */
export function renderWithProviders(
  ui: ReactElement,
  options: RenderWithProvidersOptions = {},
): RenderWithProvidersResult {
  const {
    route = '/',
    authValues = {},
    queryClient = createTestQueryClient(),
    ...renderOptions
  } = options;

  // Set initial route
  window.history.pushState({}, 'Test page', route);

  // Provider order matches production (AuthProvider → QueryClientProvider) so
  // that hooks calling extractApiError + extractFieldErrors see auth state and
  // a real QueryClient.
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nextProvider i18n={i18n}>
        <BrowserRouter>
          <MockAuthProvider authValues={authValues}>
            <QueryClientProvider client={queryClient}>
              {children}
            </QueryClientProvider>
          </MockAuthProvider>
        </BrowserRouter>
      </I18nextProvider>
    );
  }

  return {
    queryClient,
    ...render(ui, { wrapper: Wrapper, ...renderOptions }),
  };
}

export interface RenderWithRouterOptions {
  route?: string;
  queryClient?: QueryClient;
}

/**
 * Render with just Router (no auth context)
 */
export function renderWithRouter(
  ui: ReactElement,
  { route = '/', queryClient = createTestQueryClient() }: RenderWithRouterOptions = {},
): RenderResult {
  window.history.pushState({}, 'Test page', route);

  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <I18nextProvider i18n={i18n}>
        <BrowserRouter>
          <QueryClientProvider client={queryClient}>
            {children}
          </QueryClientProvider>
        </BrowserRouter>
      </I18nextProvider>
    );
  }

  return render(ui, { wrapper: Wrapper });
}

/**
 * Mock API response (axios-style) used by tests that intercept apiClient
 * methods directly instead of going through MSW. The shape mirrors what tests
 * actually consume — `data`, `status`, `statusText`, plus loose `headers` /
 * `config` placeholders.
 */
export interface MockApiResponse<T> {
  data: T;
  status: number;
  statusText: string;
  headers: Record<string, string>;
  config: Record<string, unknown>;
}

export function createMockResponse<T>(data: T, status = 200): MockApiResponse<T> {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: {},
    config: {},
  };
}

/**
 * Mock plugin fixture. Mirrors the shape returned by /api/plugins (name,
 * version, description, author, enabled, enabled_var, has_config, config_vars,
 * intents, rate_limit). Kept loose enough to allow extra fields via overrides.
 */
export interface MockPlugin {
  name: string;
  version: string;
  description: string;
  author: string;
  enabled: boolean;
  enabled_var: string;
  has_config: boolean;
  config_vars: unknown[];
  intents: string[];
  rate_limit: number | null;
}

export function createMockPlugin(overrides: Partial<MockPlugin> = {}): MockPlugin {
  return {
    name: 'test-plugin',
    version: '1.0.0',
    description: 'A test plugin',
    author: 'Test Author',
    enabled: true,
    enabled_var: 'TEST_PLUGIN_ENABLED',
    has_config: false,
    config_vars: [],
    intents: [],
    rate_limit: null,
    ...overrides,
  };
}

/**
 * Mock role fixture (matches the /api/roles list shape — same fields used by
 * mocks/handlers.ts MockRole).
 */
export interface MockRoleFixture {
  id: number;
  name: string;
  description: string;
  permissions: string[];
  is_system: boolean;
  user_count: number;
  created_at: string;
  updated_at: string;
}

export function createMockRole(overrides: Partial<MockRoleFixture> = {}): MockRoleFixture {
  return {
    id: 1,
    name: 'Test Role',
    description: 'A test role',
    permissions: ['ha.read'],
    is_system: false,
    user_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

/**
 * Mock user fixture. The `role` field here is a string (display name) to match
 * what /api/auth/me returns to the frontend, not the embedded Role object.
 */
export interface MockUserFixture {
  id: number;
  username: string;
  email: string;
  role: string;
  role_id: number;
  permissions: string[];
  is_active: boolean;
}

export function createMockUser(overrides: Partial<MockUserFixture> = {}): MockUserFixture {
  return {
    id: 1,
    username: 'testuser',
    email: 'test@example.com',
    role: 'Admin',
    role_id: 1,
    permissions: ['admin', 'plugins.manage'],
    is_active: true,
    ...overrides,
  };
}

// Re-export everything from testing-library (typed by the library itself)
export * from '@testing-library/react';
export const userEvent = userEventDefault;

// Export MockAuthContext for use in tests that need to override auth behavior
export { MockAuthContext, MockAuthProvider };
