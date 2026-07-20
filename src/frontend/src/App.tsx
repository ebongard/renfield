import { Suspense, lazy } from 'react';
import { Routes, Route, Navigate } from 'react-router';
import { QueryClientProvider } from '@tanstack/react-query';
import { ReactQueryDevtools } from '@tanstack/react-query-devtools';
import Layout from './components/Layout';
import ErrorBoundary from './components/ErrorBoundary';
import ProtectedRoute, { AdminRoute } from './components/ProtectedRoute';
import RedirectPreserving from './components/RedirectPreserving';
import { useFeatureFlags } from './api/resources/brain';
import ChatPage from './pages/ChatPage';
import LoginPage from './pages/LoginPage';
import RegisterPage from './pages/RegisterPage';
import { DeviceProvider } from './context/DeviceContext';
import { AuthProvider, useAuth } from './context/AuthContext';
import { ThemeProvider } from './context/ThemeContext';
import LoadingSpinner from './components/LoadingSpinner';
import { queryClient } from './api/queryClient';

// Lazy-loaded admin/secondary pages
const TasksPage = lazy(() => import('./pages/TasksPage'));
const ProjectsPage = lazy(() => import('./pages/ProjectsPage'));
const ProjectDetailPage = lazy(() => import('./pages/ProjectDetailPage'));
const NotesPage = lazy(() => import('./pages/NotesPage'));
const MeetingsPage = lazy(() => import('./pages/MeetingsPage'));
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
const MaintenancePage = lazy(() => import('./pages/MaintenancePage'));
const PaperlessAuditPage = lazy(() => import('./pages/PaperlessAuditPage'));
const RoutingDashboardPage = lazy(() => import('./pages/RoutingDashboardPage'));
const BrainPage = lazy(() => import('./pages/BrainPage'));
const BrainReviewPage = lazy(() => import('./pages/BrainReviewPage'));
const ObligationsPage = lazy(() => import('./pages/ObligationsPage'));
// WissensbasisPage was the A-LANDING 2D composed page; superseded by
// the unified 3D Wissensgraph (see /wissensbasis redirect below).
const CirclesSettingsPage = lazy(() => import('./pages/CirclesSettingsPage'));
const CirclesPeersPage = lazy(() => import('./pages/CirclesPeersPage'));
const FederationAuditPage = lazy(() => import('./pages/FederationAuditPage'));
// Self-learning admin console (v2.10).
const AdminSkillsPage = lazy(() => import('./pages/AdminSkillsPage'));
const BrainSkillsPage = lazy(() => import('./pages/BrainSkillsPage'));
const AdminToolHealthPage = lazy(() => import('./pages/AdminToolHealthPage'));
const AdminTrajectoriesPage = lazy(() => import('./pages/AdminTrajectoriesPage'));
const AdminCuratorPage = lazy(() => import('./pages/AdminCuratorPage'));
const KioskPage = lazy(() => import('./pages/KioskPage'));
const WissenLayout = lazy(() => import('./pages/wissen/WissenLayout'));
const OverviewLens = lazy(() => import('./pages/wissen/OverviewLens'));

function AppRoutes() {
  const { isFeatureEnabled } = useAuth();
  // D10: the unified workspace is gated by a runtime flag (/api/config/features).
  // Until it resolves (or when off) we render the legacy flat corpus routes — the
  // safe default, so a slow/failed flag fetch never breaks navigation.
  const { data: featureFlags } = useFeatureFlags();
  const wissenWorkspace = featureFlags?.wissen_workspace_enabled ?? false;
  // Business-instance Phase 1: the /projects surface is gated by a runtime flag
  // (/api/config/features). Off (the household default) => the route is absent.
  const projectsEnabled = featureFlags?.projects_enabled ?? false;
  const notesEnabled = featureFlags?.notes_enabled ?? false;
  // §2 meeting transcription: the /meetings surface is gated by a runtime flag
  // (/api/config/features). Off (the default on both instances) => route absent.
  const meetingsEnabled = featureFlags?.meeting_transcription_enabled ?? false;

  return (
    <Routes>
      {/* Public routes without layout */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      {/* Fullscreen wall-display kiosk — deliberately OUTSIDE the app Layout
          (no sidebar/header). Admin-gated (auth-off = open, like the board). */}
      <Route path="/kiosk" element={
        <AdminRoute>
          <KioskPage />
        </AdminRoute>
      } />

      {/* Routes with layout */}
      <Route path="/*" element={
        <Layout>
          <Routes>
            <Route path="/" element={
              <ProtectedRoute>
                <ChatPage />
              </ProtectedRoute>
            } />
            <Route path="/chat" element={<Navigate to="/" replace />} />
            <Route path="/tasks" element={<TasksPage />} />
            {projectsEnabled && (
              <Route path="/projects" element={
                <ProtectedRoute>
                  <ProjectsPage />
                </ProtectedRoute>
              } />
            )}
            {projectsEnabled && (
              <Route path="/projects/:id" element={
                <ProtectedRoute>
                  <ProjectDetailPage />
                </ProtectedRoute>
              } />
            )}
            {notesEnabled && (
              <Route path="/notes" element={
                <ProtectedRoute>
                  <NotesPage />
                </ProtectedRoute>
              } />
            )}
            {meetingsEnabled && (
              <Route path="/meetings" element={
                <ProtectedRoute>
                  <MeetingsPage />
                </ProtectedRoute>
              } />
            )}
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
            {wissenWorkspace ? (
              <>
                {/* Unified Wissen workspace (D10 flag ON): one persistent shell,
                    lenses in the Outlet, old corpus URLs redirect in. */}
                <Route path="/wissen" element={
                  <ProtectedRoute>
                    <WissenLayout />
                  </ProtectedRoute>
                }>
                  {/* Übersicht dashboard (PR2). */}
                  <Route index element={<OverviewLens />} />
                  <Route path="dokumente" element={
                    <ProtectedRoute permission={['kb.own', 'kb.shared', 'kb.all']} requireAny>
                      <KnowledgePage />
                    </ProtectedRoute>
                  } />
                  {isFeatureEnabled('knowledge_graph') && (
                    <Route path="graph" element={<KnowledgeGraphPage />} />
                  )}
                  <Route path="erinnerungen" element={<MemoryPage />} />
                  <Route path="fristen" element={<ObligationsPage />} />
                  <Route path="review" element={
                    <ProtectedRoute>
                      <BrainReviewPage />
                    </ProtectedRoute>
                  } />
                </Route>
                {/* Old corpus routes → workspace lenses (search + hash preserved). */}
                <Route path="/knowledge" element={<RedirectPreserving to="/wissen/dokumente" />} />
                <Route path="/memory" element={<RedirectPreserving to="/wissen/erinnerungen" />} />
                <Route path="/knowledge-graph" element={<RedirectPreserving to="/wissen/graph" />} />
                <Route path="/wissensbasis" element={<RedirectPreserving to="/wissen/graph" />} />
                <Route path="/brain" element={<RedirectPreserving to="/wissen" />} />
                <Route path="/brain/review" element={<RedirectPreserving to="/wissen/review" />} />
                <Route path="/brain/fristen" element={<RedirectPreserving to="/wissen/fristen" />} />
              </>
            ) : (
              <>
                {/* Legacy flat corpus routes (D10 flag OFF — the safe default). */}
                <Route path="/knowledge" element={
                  <ProtectedRoute permission={['kb.own', 'kb.shared', 'kb.all']} requireAny>
                    <KnowledgePage />
                  </ProtectedRoute>
                } />
                <Route path="/memory" element={<MemoryPage />} />
                <Route path="/knowledge-graph" element={<KnowledgeGraphPage />} />
                {/* /wissensbasis: the A-LANDING 2D page, superseded 2026-05-12 by
                    the unified 3D Wissensgraph; ?focus= URLs resolve there. */}
                <Route path="/wissensbasis" element={
                  <Navigate to={`/knowledge-graph${window.location.search}`} replace />
                } />
                <Route path="/brain" element={
                  <ProtectedRoute>
                    <BrainPage />
                  </ProtectedRoute>
                } />
                <Route path="/brain/review" element={
                  <ProtectedRoute>
                    <BrainReviewPage />
                  </ProtectedRoute>
                } />
                <Route path="/brain/fristen" element={
                  <ProtectedRoute>
                    <ObligationsPage />
                  </ProtectedRoute>
                } />
              </>
            )}
            <Route path="/brain/audit" element={
              <ProtectedRoute>
                <FederationAuditPage />
              </ProtectedRoute>
            } />
            <Route path="/settings/circles" element={
              <ProtectedRoute>
                <CirclesSettingsPage />
              </ProtectedRoute>
            } />
            <Route path="/settings/circles/peers" element={
              <ProtectedRoute>
                <CirclesPeersPage />
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
            {/* Redirect old admin route */}
            <Route path="/admin/knowledge-graph" element={<Navigate to="/knowledge-graph" replace />} />
            <Route path="/admin/maintenance" element={
              <AdminRoute>
                <MaintenancePage />
              </AdminRoute>
            } />
            <Route path="/admin/paperless-audit" element={
              <AdminRoute>
                <PaperlessAuditPage />
              </AdminRoute>
            } />
            <Route path="/admin/routing" element={
              <AdminRoute>
                <RoutingDashboardPage />
              </AdminRoute>
            } />
            <Route path="/brain/skills" element={
              <ProtectedRoute>
                <BrainSkillsPage />
              </ProtectedRoute>
            } />
            <Route path="/admin/skills" element={
              <AdminRoute>
                <AdminSkillsPage />
              </AdminRoute>
            } />
            <Route path="/admin/tool-health" element={
              <AdminRoute>
                <AdminToolHealthPage />
              </AdminRoute>
            } />
            <Route path="/admin/trajectories" element={
              <AdminRoute>
                <AdminTrajectoriesPage />
              </AdminRoute>
            } />
            <Route path="/admin/curator" element={
              <AdminRoute>
                <AdminCuratorPage />
              </AdminRoute>
            } />
          </Routes>
        </Layout>
      } />
    </Routes>
  );
}

function App() {
  // Provider order matters: AuthProvider must mount BEFORE QueryClientProvider so
  // its axios interceptors (bearer-token injection + 401-refresh) are installed
  // before any React Query fetcher can fire. See plan E11 / D6.
  return (
    <ErrorBoundary>
      <ThemeProvider>
        <AuthProvider>
          <QueryClientProvider client={queryClient}>
            <DeviceProvider>
              <Suspense fallback={<LoadingSpinner />}>
                <AppRoutes />
              </Suspense>
            </DeviceProvider>
            {import.meta.env.DEV && <ReactQueryDevtools initialIsOpen={false} />}
          </QueryClientProvider>
        </AuthProvider>
      </ThemeProvider>
    </ErrorBoundary>
  );
}

export default App;
