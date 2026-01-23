/**
 * Authentication Context
 *
 * Provides global authentication state and methods for login/logout.
 * Handles JWT token storage and automatic refresh.
 */
import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import apiClient from '../utils/axios';

const AuthContext = createContext(null);

// Token storage keys
const ACCESS_TOKEN_KEY = 'renfield_access_token';
const REFRESH_TOKEN_KEY = 'renfield_refresh_token';

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [authEnabled, setAuthEnabled] = useState(false);
  const [allowRegistration, setAllowRegistration] = useState(false);

  // Get stored tokens
  const getAccessToken = useCallback(() => localStorage.getItem(ACCESS_TOKEN_KEY), []);
  const getRefreshToken = useCallback(() => localStorage.getItem(REFRESH_TOKEN_KEY), []);

  // Store tokens
  const setTokens = useCallback((accessToken, refreshToken) => {
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
  const hasPermission = useCallback((permission) => {
    if (!user) return false;
    if (!authEnabled) return true; // Auth disabled = full access
    return user.permissions?.includes(permission) || user.permissions?.includes('admin');
  }, [user, authEnabled]);

  // Check if user has any of the specified permissions
  const hasAnyPermission = useCallback((permissions) => {
    if (!user) return false;
    if (!authEnabled) return true;
    if (user.permissions?.includes('admin')) return true;
    return permissions.some(p => user.permissions?.includes(p));
  }, [user, authEnabled]);

  // Check if user is admin
  const isAdmin = useCallback(() => {
    return hasPermission('admin');
  }, [hasPermission]);

  // Fetch current user info
  const fetchUser = useCallback(async () => {
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
    } catch (error) {
      // Token might be expired, try to refresh
      if (error.response?.status === 401) {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
          return fetchUser();
        }
      }
      clearTokens();
      setUser(null);
      return null;
    }
  }, [getAccessToken, clearTokens]);

  // Refresh access token
  const refreshAccessToken = useCallback(async () => {
    const refreshToken = getRefreshToken();
    if (!refreshToken) return false;

    try {
      const response = await apiClient.post('/api/auth/refresh', {
        refresh_token: refreshToken
      });
      setTokens(response.data.access_token, response.data.refresh_token);
      return true;
    } catch (error) {
      clearTokens();
      setUser(null);
      return false;
    }
  }, [getRefreshToken, setTokens, clearTokens]);

  // Login
  const login = useCallback(async (username, password) => {
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
  const register = useCallback(async (username, password, email = null) => {
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
  const changePassword = useCallback(async (currentPassword, newPassword) => {
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

        if (response.data.auth_enabled) {
          await fetchUser();
        } else {
          // Auth disabled - create a pseudo-admin user for UI purposes
          setUser({
            id: 0,
            username: 'anonymous',
            role: 'Admin',
            permissions: ['admin'],
            is_active: true
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

  const value = {
    user,
    loading,
    authEnabled,
    allowRegistration,
    isAuthenticated: !!user,
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

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}

export default AuthContext;
