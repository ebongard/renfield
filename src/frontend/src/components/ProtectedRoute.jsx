/**
 * Protected Route Component
 *
 * Wraps routes that require authentication or specific permissions.
 */
import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { Loader, ShieldOff } from 'lucide-react';

/**
 * ProtectedRoute - Requires authentication
 *
 * @param {Object} props
 * @param {React.ReactNode} props.children - Child components to render
 * @param {string|string[]} props.permission - Required permission(s)
 * @param {boolean} props.requireAny - If true, user needs any of the permissions (default: all)
 */
export default function ProtectedRoute({ children, permission = null, requireAny = false }) {
  const { isAuthenticated, authEnabled, loading, hasPermission, hasAnyPermission } = useAuth();
  const location = useLocation();

  // Show loading while checking auth status
  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader className="w-8 h-8 animate-spin text-primary-500" />
      </div>
    );
  }

  // If auth is disabled, allow access
  if (!authEnabled) {
    return children;
  }

  // If not authenticated, redirect to login
  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  // Check permissions if specified
  if (permission) {
    const permissions = Array.isArray(permission) ? permission : [permission];
    const hasAccess = requireAny
      ? hasAnyPermission(permissions)
      : permissions.every(p => hasPermission(p));

    if (!hasAccess) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[50vh] text-center px-4">
          <ShieldOff className="w-16 h-16 text-red-500 mb-4" />
          <h2 className="text-2xl font-bold text-white mb-2">Access Denied</h2>
          <p className="text-gray-400 max-w-md">
            You don't have permission to access this page.
            Please contact an administrator if you believe this is an error.
          </p>
        </div>
      );
    }
  }

  return children;
}

/**
 * AdminRoute - Requires admin permission
 */
export function AdminRoute({ children }) {
  return (
    <ProtectedRoute permission="admin">
      {children}
    </ProtectedRoute>
  );
}
