import React from 'react';
import { Routes, Route, Navigate } from 'react-router';
import Layout from './components/Layout';
import ErrorBoundary from './components/ErrorBoundary';
import ProtectedRoute, { AdminRoute } from './components/ProtectedRoute';
import ChatPage from './pages/ChatPage';
import TasksPage from './pages/TasksPage';
import CameraPage from './pages/CameraPage';
import HomeAssistantPage from './pages/HomeAssistantPage';
import SpeakersPage from './pages/SpeakersPage';
import RoomsPage from './pages/RoomsPage';
import KnowledgePage from './pages/KnowledgePage';
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';
import UsersPage from './pages/UsersPage';
import RolesPage from './pages/RolesPage';
import IntegrationsPage from './pages/IntegrationsPage';
import SettingsPage from './pages/SettingsPage';
import SatellitesPage from './pages/SatellitesPage';
import IntentsPage from './pages/IntentsPage';
import { DeviceProvider } from './context/DeviceContext';
import { AuthProvider } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';

function App() {
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <AuthProvider>
          <DeviceProvider>
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
                  <Route path="/camera" element={
                    <ProtectedRoute permission={['cam.view', 'cam.full']} requireAny>
                      <CameraPage />
                    </ProtectedRoute>
                  } />
                  <Route path="/homeassistant" element={
                    <ProtectedRoute permission={['ha.read', 'ha.control', 'ha.full']} requireAny>
                      <HomeAssistantPage />
                    </ProtectedRoute>
                  } />
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
                  <Route path="/admin/satellites" element={
                    <AdminRoute>
                      <SatellitesPage />
                    </AdminRoute>
                  } />
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
                </Routes>
              </Layout>
            } />
          </Routes>
          </DeviceProvider>
        </AuthProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
