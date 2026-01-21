import React from 'react';
import { Routes, Route } from 'react-router-dom';
import Layout from './components/Layout';
import ErrorBoundary from './components/ErrorBoundary';
import ChatPage from './pages/ChatPage';
import HomePage from './pages/HomePage';
import TasksPage from './pages/TasksPage';
import CameraPage from './pages/CameraPage';
import HomeAssistantPage from './pages/HomeAssistantPage';
import SpeakersPage from './pages/SpeakersPage';
import RoomsPage from './pages/RoomsPage';
import { DeviceProvider } from './context/DeviceContext';

function App() {
  return (
    <ErrorBoundary>
      <DeviceProvider>
        <Layout>
          <Routes>
            <Route path="/" element={<HomePage />} />
            <Route path="/chat" element={<ChatPage />} />
            <Route path="/tasks" element={<TasksPage />} />
            <Route path="/camera" element={<CameraPage />} />
            <Route path="/homeassistant" element={<HomeAssistantPage />} />
            <Route path="/speakers" element={<SpeakersPage />} />
            <Route path="/rooms" element={<RoomsPage />} />
          </Routes>
        </Layout>
      </DeviceProvider>
    </ErrorBoundary>
  );
}

export default App;
