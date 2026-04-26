/**
 * Protected Route Component
 *
 * Wraps routes that require authentication or specific permissions.
 */
import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router';
import { useAuth } from '../context/AuthContext';
import { Loader, ShieldOff } from 'lucide-react';

interface ProtectedRouteProps {
  children: ReactNode;
  /** Required permission(s) — string or array. */
  permission?: string | string[] | null;
  /** When true, user needs ANY of the permissions; otherwise ALL are required. */
  requireAny?: boolean;
}

export default function ProtectedRoute({
  children,
  permission = null,
  requireAny = false,
}: ProtectedRouteProps) {
  const { isAuthenticated, authEnabled, loading, hasPermission, hasAnyPermission } = useAuth();
  const location = useLocation();

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[50vh]">
        <Loader className="w-8 h-8 animate-spin text-primary-500" />
      </div>
    );
  }

  if (!authEnabled) {
    return <>{children}</>;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (permission) {
    const permissions = Array.isArray(permission) ? permission : [permission];
    const hasAccess = requireAny
      ? hasAnyPermission(permissions)
      : permissions.every((p) => hasPermission(p));

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

  return <>{children}</>;
}

interface AdminRouteProps {
  children: ReactNode;
}

export function AdminRoute({ children }: AdminRouteProps) {
  return <ProtectedRoute permission="admin">{children}</ProtectedRoute>;
}
