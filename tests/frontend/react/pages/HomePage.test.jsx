import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL } from '../mocks/handlers.js';
import HomePage from '../../../../src/frontend/src/pages/HomePage';
import { renderWithProviders } from '../test-utils.jsx';

describe('HomePage', () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the welcome message', async () => {
      renderWithProviders(<HomePage />);

      expect(screen.getByText('Willkommen bei Renfield')).toBeInTheDocument();
      expect(screen.getByText(/dein vollständig offline-fähiger/i)).toBeInTheDocument();
    });

    it('renders feature cards', async () => {
      renderWithProviders(<HomePage />);

      expect(screen.getByText('Chat')).toBeInTheDocument();
      expect(screen.getByText('Sprachsteuerung')).toBeInTheDocument();
      expect(screen.getByText('Kamera-Überwachung')).toBeInTheDocument();
      expect(screen.getByText('Smart Home')).toBeInTheDocument();
    });

    it('renders feature descriptions', async () => {
      renderWithProviders(<HomePage />);

      expect(screen.getByText(/unterhalte dich mit deinem ki-assistenten/i)).toBeInTheDocument();
      expect(screen.getByText(/nutze spracheingabe und -ausgabe/i)).toBeInTheDocument();
      expect(screen.getByText(/überwache deine kameras/i)).toBeInTheDocument();
      expect(screen.getByText(/steuere dein zuhause/i)).toBeInTheDocument();
    });

    it('shows system status indicator', async () => {
      renderWithProviders(<HomePage />);

      // Initially shows "Prüfe System..."
      expect(screen.getByText('Prüfe System...')).toBeInTheDocument();
    });
  });

  describe('Health Check', () => {
    it('shows system online when health check succeeds', async () => {
      renderWithProviders(<HomePage />);

      await waitFor(() => {
        expect(screen.getByText('System Online')).toBeInTheDocument();
      });
    });

    it('displays service status when health data is loaded', async () => {
      renderWithProviders(<HomePage />);

      await waitFor(() => {
        expect(screen.getByText('System Status')).toBeInTheDocument();
      });

      // Check for service status indicators
      expect(screen.getByText('Ollama')).toBeInTheDocument();
      expect(screen.getByText('Datenbank')).toBeInTheDocument();
      expect(screen.getByText('Redis')).toBeInTheDocument();

      // All services should show online
      const onlineIndicators = screen.getAllByText('✓ Online');
      expect(onlineIndicators.length).toBe(3);
    });

    it('shows offline status when service is down', async () => {
      server.use(
        http.get(`${BASE_URL}/health`, () => {
          return HttpResponse.json({
            status: 'degraded',
            services: {
              ollama: 'error',
              database: 'ok',
              redis: 'ok'
            }
          });
        })
      );

      renderWithProviders(<HomePage />);

      await waitFor(() => {
        expect(screen.getByText('System Status')).toBeInTheDocument();
      });

      expect(screen.getByText('✗ Offline')).toBeInTheDocument();
    });

    it('handles health check failure gracefully', async () => {
      server.use(
        http.get(`${BASE_URL}/health`, () => {
          return HttpResponse.json(
            { detail: 'Service unavailable' },
            { status: 503 }
          );
        })
      );

      renderWithProviders(<HomePage />);

      // Should still render the page without crashing
      await waitFor(() => {
        expect(screen.getByText('System Online')).toBeInTheDocument();
      });

      // Health status section should not be shown if health check fails (no services data)
      expect(screen.queryByText('System Status')).not.toBeInTheDocument();
    });

    it('handles health response without services gracefully', async () => {
      server.use(
        http.get(`${BASE_URL}/health`, () => {
          return HttpResponse.json({ status: 'ok' }); // No services property
        })
      );

      renderWithProviders(<HomePage />);

      await waitFor(() => {
        expect(screen.getByText('System Online')).toBeInTheDocument();
      });

      // Health status section should not be shown if services is undefined
      expect(screen.queryByText('System Status')).not.toBeInTheDocument();
    });
  });

  describe('Navigation Links', () => {
    it('has working navigation links', async () => {
      renderWithProviders(<HomePage />);

      // Check that feature cards are links
      const chatLink = screen.getByText('Chat').closest('a');
      expect(chatLink).toHaveAttribute('href', '/chat');

      const cameraLink = screen.getByText('Kamera-Überwachung').closest('a');
      expect(cameraLink).toHaveAttribute('href', '/camera');

      const haLink = screen.getByText('Smart Home').closest('a');
      expect(haLink).toHaveAttribute('href', '/homeassistant');
    });
  });
});
