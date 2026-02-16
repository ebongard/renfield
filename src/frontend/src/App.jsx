import React, { Suspense, lazy } from 'react';
import { Routes, Route, Navigate } from 'react-router';
import Layout from './components/Layout';
import ErrorBoundary from './components/ErrorBoundary';
import ProtectedRoute, { AdminRoute } from './components/ProtectedRoute';
import ChatPage from './pages/ChatPage';
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';
import { DeviceProvider } from './context/DeviceContext';
import { AuthProvider, useAuth } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';
import LoadingSpinner from './components/LoadingSpinner';

// Lazy-loaded admin/secondary pages
const TasksPage = lazy(() => import('./pages/TasksPage'));
const CameraPage = lazy(() => import('./pages/CameraPage'));
const HomeAssistantPage = lazy(() => import('./pages/HomeAssistantPage'));
const SpeakersPage = lazy(() => import('./pages/SpeakersPage'));
const RoomsPage = lazy(() => import('./pages/RoomsPage'));
const KnowledgePage = lazy(() => import('./pages/KnowledgePage'));
const MemoryPage = lazy(() => import('./pages/MemoryPage'));
const UsersPage = lazy(() => import('./pages/UsersPage'));
const RolesPage = lazy(() => import('./pages/RolesPage'));
const IntegrationsPage = lazy(() => import('./pages/IntegrationsPage'));
const SettingsPage = lazy(() => import('./pages/SettingsPage'));
const SatellitesPage = lazy(() => import('./pages/SatellitesPage'));
const IntentsPage = lazy(() => import('./pages/IntentsPage'));
const PresencePage = lazy(() => import('./pages/PresencePage'));
const KnowledgeGraphPage = lazy(() => import('./pages/KnowledgeGraphPage'));

function AppRoutes() {
  const { isFeatureEnabled } = useAuth();

  return (
    <Routes>
      {/* Public routes without layout */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      {/* Routes with layout */}
      <Route path="/*" element={
        <Layout>
          <Routes>
            <Route path="/" element={<ChatPage />} />
            <Route path="/chat" element={<Navigate to="/" replace />} />
            <Route path="/tasks" element={<TasksPage />} />
            {isFeatureEnabled('cameras') && (
              <Route path="/camera" element={
                <ProtectedRoute permission={['cam.view', 'cam.full']} requireAny>
                  <CameraPage />
                </ProtectedRoute>
              } />
            )}
            {isFeatureEnabled('smart_home') && (
              <Route path="/homeassistant" element={
                <ProtectedRoute permission={['ha.read', 'ha.control', 'ha.full']} requireAny>
                  <HomeAssistantPage />
                </ProtectedRoute>
              } />
            )}
            <Route path="/speakers" element={
              <ProtectedRoute permission={['speakers.own', 'speakers.all']} requireAny>
                <SpeakersPage />
              </ProtectedRoute>
            } />
            <Route path="/rooms" element={
              <ProtectedRoute permission={['rooms.read', 'rooms.manage']} requireAny>
                <RoomsPage />
              </ProtectedRoute>
            } />
            <Route path="/knowledge" element={
              <ProtectedRoute permission={['kb.own', 'kb.shared', 'kb.all']} requireAny>
                <KnowledgePage />
              </ProtectedRoute>
            } />
            <Route path="/memory" element={<MemoryPage />} />
            {/* Redirect old /plugins route to new integrations page */}
            <Route path="/plugins" element={<Navigate to="/admin/integrations" replace />} />
            {/* Admin routes */}
            <Route path="/admin/users" element={
              <AdminRoute>
                <UsersPage />
              </AdminRoute>
            } />
            <Route path="/admin/roles" element={
              <AdminRoute>
                <RolesPage />
              </AdminRoute>
            } />
            <Route path="/admin/settings" element={
              <AdminRoute>
                <SettingsPage />
              </AdminRoute>
            } />
            {isFeatureEnabled('satellites') && (
              <Route path="/admin/satellites" element={
                <AdminRoute>
                  <SatellitesPage />
                </AdminRoute>
              } />
            )}
            <Route path="/admin/integrations" element={
              <AdminRoute>
                <IntegrationsPage />
              </AdminRoute>
            } />
            <Route path="/admin/intents" element={
              <AdminRoute>
                <IntentsPage />
              </AdminRoute>
            } />
            <Route path="/admin/presence" element={
              <AdminRoute>
                <PresencePage />
              </AdminRoute>
            } />
            <Route path="/admin/knowledge-graph" element={
              <AdminRoute>
                <KnowledgeGraphPage />
              </AdminRoute>
            } />
          </Routes>
        </Layout>
      } />
    </Routes>
  );
}

function App() {
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <AuthProvider>
          <DeviceProvider>
          <Suspense fallback={<LoadingSpinner />}>
            <AppRoutes />
          </Suspense>
          </DeviceProvider>
        </AuthProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
