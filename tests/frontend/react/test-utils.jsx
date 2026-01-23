import { render } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { createContext, useContext } from 'react';

// Create a mock auth context for testing
const MockAuthContext = createContext(null);

// Default mock auth values
const defaultMockAuth = {
  user: { username: 'admin', role: 'Admin', permissions: ['admin', 'plugins.manage'] },
  isAuthenticated: true,
  authEnabled: true,
  allowRegistration: false,
  loading: false,
  login: async () => {},
  logout: () => {},
  register: async () => {},
  changePassword: async () => {},
  fetchUser: async () => {},
  hasPermission: (perm) => true,
  hasAnyPermission: (perms) => true,
  isAdmin: () => true,
  getAccessToken: () => 'mock-token',
};

// Mock AuthProvider for testing
function MockAuthProvider({ children, authValues = {} }) {
  const value = { ...defaultMockAuth, ...authValues };
  return (
    <MockAuthContext.Provider value={value}>
      {children}
    </MockAuthContext.Provider>
  );
}

// Mock useAuth hook for testing
export function useMockAuth() {
  const context = useContext(MockAuthContext);
  if (!context) {
    throw new Error('useMockAuth must be used within a MockAuthProvider');
  }
  return context;
}

/**
 * Custom render function that wraps components with necessary providers
 * Use this for tests that need auth context
 */
export function renderWithProviders(ui, options = {}) {
  const {
    route = '/',
    authValues = {},
    ...renderOptions
  } = options;

  // Set initial route
  window.history.pushState({}, 'Test page', route);

  function Wrapper({ children }) {
    return (
      <BrowserRouter>
        <MockAuthProvider authValues={authValues}>
          {children}
        </MockAuthProvider>
      </BrowserRouter>
    );
  }

  return {
    ...render(ui, { wrapper: Wrapper, ...renderOptions }),
  };
}

/**
 * Render with just Router (no auth context)
 */
export function renderWithRouter(ui, { route = '/' } = {}) {
  window.history.pushState({}, 'Test page', route);

  return render(ui, { wrapper: BrowserRouter });
}

/**
 * Create mock API response
 */
export function createMockResponse(data, status = 200) {
  return {
    data,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    headers: {},
    config: {},
  };
}

/**
 * Create mock plugin data
 */
export function createMockPlugin(overrides = {}) {
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
 * Create mock role data
 */
export function createMockRole(overrides = {}) {
  return {
    id: 1,
    name: 'Test Role',
    description: 'A test role',
    permissions: ['plugins.use', 'ha.read'],
    allowed_plugins: [],
    is_system: false,
    user_count: 0,
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-01T00:00:00Z',
    ...overrides,
  };
}

/**
 * Create mock user data
 */
export function createMockUser(overrides = {}) {
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

// Re-export everything from testing-library
export * from '@testing-library/react';
export { default as userEvent } from '@testing-library/user-event';

// Export MockAuthContext for use in tests that need to override auth behavior
export { MockAuthContext, MockAuthProvider };
