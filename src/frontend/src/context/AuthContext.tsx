/**
 * Authentication Context
 *
 * Provides global authentication state and methods for login/logout.
 * Handles JWT token storage and automatic refresh.
 */
import { createContext, useContext, useState, useEffect, useCallback, ReactNode } from 'react';
import apiClient from '../utils/axios';
import type { User, LoginResponse } from '../types/api';

// Auth user with permissions
interface AuthUser extends User {
  permissions?: string[];
}

// Context value type
interface AuthContextValue {
  user: AuthUser | null;
  loading: boolean;
  authEnabled: boolean;
  allowRegistration: boolean;
  isAuthenticated: boolean;
  features: Record<string, boolean>;
  isFeatureEnabled: (feature: string) => boolean;
  login: (username: string, password: string) => Promise<LoginResponse>;
  logout: () => void;
  register: (username: string, password: string, email?: string | null) => Promise<User>;
  changePassword: (currentPassword: string, newPassword: string) => Promise<{ message: string }>;
  fetchUser: () => Promise<AuthUser | null>;
  hasPermission: (permission: string) => boolean;
  hasAnyPermission: (permissions: string[]) => boolean;
  isAdmin: () => boolean;
  getAccessToken: () => string | null;
}

const AuthContext = createContext<AuthContextValue | null>(null);

// Token storage keys
const ACCESS_TOKEN_KEY = 'renfield_access_token';
const REFRESH_TOKEN_KEY = 'renfield_refresh_token';

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [allowRegistration, setAllowRegistration] = useState(false);
  const [features, setFeatures] = useState<Record<string, boolean>>({});

  // Get stored tokens
  const getAccessToken = useCallback(() => localStorage.getItem(ACCESS_TOKEN_KEY), []);
  const getRefreshToken = useCallback(() => localStorage.getItem(REFRESH_TOKEN_KEY), []);

  // Store tokens
  const setTokens = useCallback((accessToken?: string, refreshToken?: string) => {
    if (accessToken) {
      localStorage.setItem(ACCESS_TOKEN_KEY, accessToken);
    }
    if (refreshToken) {
      localStorage.setItem(REFRESH_TOKEN_KEY, refreshToken);
    }
  }, []);

  // Clear tokens
  const clearTokens = useCallback(() => {
    localStorage.removeItem(ACCESS_TOKEN_KEY);
    localStorage.removeItem(REFRESH_TOKEN_KEY);
  }, []);

  // Check if user has a specific permission
  const hasPermission = useCallback((permission: string): boolean => {
    if (!user) return false;
    if (!authEnabled) return true; // Auth disabled = full access
    return user.permissions?.includes(permission) || user.permissions?.includes('admin') || false;
  }, [user, authEnabled]);

  // Check if user has any of the specified permissions
  const hasAnyPermission = useCallback((permissions: string[]): boolean => {
    if (!user) return false;
    if (!authEnabled) return true;
    if (user.permissions?.includes('admin')) return true;
    return permissions.some(p => user.permissions?.includes(p));
  }, [user, authEnabled]);

  // Check if user is admin
  const isAdmin = useCallback((): boolean => {
    return hasPermission('admin');
  }, [hasPermission]);

  // Check if a feature is enabled (default true if unknown)
  const isFeatureEnabled = useCallback((feature: string): boolean => {
    return features[feature] !== false;
  }, [features]);

  // Refresh access token
  const refreshAccessToken = useCallback(async (): Promise<boolean> => {
    const refreshToken = getRefreshToken();
    if (!refreshToken) return false;

    try {
      const response = await apiClient.post('/api/auth/refresh', {
        refresh_token: refreshToken
      });
      setTokens(response.data.access_token, response.data.refresh_token);
      return true;
    } catch {
      clearTokens();
      setUser(null);
      return false;
    }
  }, [getRefreshToken, setTokens, clearTokens]);

  // Fetch current user info
  const fetchUser = useCallback(async (): Promise<AuthUser | null> => {
    const token = getAccessToken();
    if (!token) {
      setUser(null);
      return null;
    }

    try {
      const response = await apiClient.get('/api/auth/me', {
        headers: { Authorization: `Bearer ${token}` }
      });
      setUser(response.data);
      return response.data;
    } catch (error: unknown) {
      // Token might be expired, try to refresh
      if (error && typeof error === 'object' && 'response' in error) {
        const axiosError = error as { response?: { status?: number } };
        if (axiosError.response?.status === 401) {
          const refreshed = await refreshAccessToken();
          if (refreshed) {
            return fetchUser();
          }
        }
      }
      clearTokens();
      setUser(null);
      return null;
    }
  }, [getAccessToken, clearTokens, refreshAccessToken]);

  // Login
  const login = useCallback(async (username: string, password: string): Promise<LoginResponse> => {
    const formData = new URLSearchParams();
    formData.append('username', username);
    formData.append('password', password);

    const response = await apiClient.post('/api/auth/login', formData, {
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
    });

    setTokens(response.data.access_token, response.data.refresh_token);
    await fetchUser();
    return response.data;
  }, [setTokens, fetchUser]);

  // Register
  const register = useCallback(async (username: string, password: string, email: string | null = null): Promise<User> => {
    const response = await apiClient.post('/api/auth/register', {
      username,
      password,
      email
    });
    return response.data;
  }, []);

  // Logout
  const logout = useCallback(() => {
    clearTokens();
    setUser(null);
  }, [clearTokens]);

  // Change password
  const changePassword = useCallback(async (currentPassword: string, newPassword: string): Promise<{ message: string }> => {
    const token = getAccessToken();
    const response = await apiClient.post('/api/auth/change-password', {
      current_password: currentPassword,
      new_password: newPassword
    }, {
      headers: { Authorization: `Bearer ${token}` }
    });
    return response.data;
  }, [getAccessToken]);

  // Check auth status on mount
  useEffect(() => {
    const checkAuthStatus = async () => {
      try {
        const response = await apiClient.get('/api/auth/status');
        setAuthEnabled(response.data.auth_enabled);
        setAllowRegistration(response.data.allow_registration);
        setFeatures(response.data.features || {});

        if (response.data.auth_enabled) {
          await fetchUser();
        } else {
          // Auth disabled - create a pseudo-admin user for UI purposes
          setUser({
            id: 0,
            username: 'anonymous',
            role: { id: 0, name: 'Admin', permissions: ['admin'], created_at: '', updated_at: '' },
            permissions: ['admin'],
            is_active: true,
            role_id: 0,
            created_at: '',
            updated_at: ''
          });
        }
      } catch (error) {
        console.error('Failed to check auth status:', error);
      } finally {
        setLoading(false);
      }
    };

    checkAuthStatus();
  }, [fetchUser]);

  // Setup axios interceptor for auth header
  useEffect(() => {
    const interceptor = apiClient.interceptors.request.use(
      (config) => {
        const token = getAccessToken();
        if (token && !config.headers.Authorization) {
          config.headers.Authorization = `Bearer ${token}`;
        }
        return config;
      },
      (error) => Promise.reject(error)
    );

    return () => {
      apiClient.interceptors.request.eject(interceptor);
    };
  }, [getAccessToken]);

  // Setup response interceptor for token refresh
  useEffect(() => {
    const interceptor = apiClient.interceptors.response.use(
      (response) => response,
      async (error) => {
        const originalRequest = error.config;

        if (error.response?.status === 401 && !originalRequest._retry && authEnabled) {
          originalRequest._retry = true;
          const refreshed = await refreshAccessToken();
          if (refreshed) {
            originalRequest.headers.Authorization = `Bearer ${getAccessToken()}`;
            return apiClient(originalRequest);
          }
        }

        return Promise.reject(error);
      }
    );

    return () => {
      apiClient.interceptors.response.eject(interceptor);
    };
  }, [authEnabled, refreshAccessToken, getAccessToken]);

  const value: AuthContextValue = {
    user,
    loading,
    authEnabled,
    allowRegistration,
    isAuthenticated: !!user,
    features,
    isFeatureEnabled,
    login,
    logout,
    register,
    changePassword,
    fetchUser,
    hasPermission,
    hasAnyPermission,
    isAdmin,
    getAccessToken
  };

  return (
    <AuthContext.Provider value={value}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

export default AuthContext;
