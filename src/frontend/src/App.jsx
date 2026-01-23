import React from 'react';
import { Routes, Route, Navigate } from 'react-router-dom';
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
import PluginsPage from './pages/PluginsPage';
import { DeviceProvider } from './context/DeviceContext';
import { AuthProvider } from './context/AuthContext';

function App() {
  return (
    <ErrorBoundary>
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
                  <Route path="/plugins" element={
                    <ProtectedRoute permission={['plugins.use', 'plugins.manage']} requireAny>
                      <PluginsPage />
                    </ProtectedRoute>
                  } />
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
                </Routes>
              </Layout>
            } />
          </Routes>
        </DeviceProvider>
      </AuthProvider>
    </ErrorBoundary>
  );
}

export default App;
