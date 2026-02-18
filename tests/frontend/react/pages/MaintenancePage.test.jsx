import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, fireEvent } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL } from '../mocks/handlers.js';
import MaintenancePage from '../../../../src/frontend/src/pages/MaintenancePage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

const adminAuthMock = {
  getAccessToken: () => Promise.resolve('mock-token'),
  hasPermission: () => true,
  hasAnyPermission: () => true,
  isAuthenticated: true,
  authEnabled: true,
  loading: false,
  user: { username: 'admin', role: 'Admin', permissions: ['admin'] }
};

describe('MaintenancePage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title and subtitle', () => {
      renderWithProviders(<MaintenancePage />);

      expect(screen.getByText('Wartung')).toBeInTheDocument();
      expect(screen.getByText('Systemwartung und Diagnose-Tools')).toBeInTheDocument();
    });

    it('renders all three sections', () => {
      renderWithProviders(<MaintenancePage />);

      expect(screen.getByText('Suche & Indexierung')).toBeInTheDocument();
      expect(screen.getByText('Embeddings')).toBeInTheDocument();
      expect(screen.getByText('Debug')).toBeInTheDocument();
    });

    it('renders the re-embed warning box', () => {
      renderWithProviders(<MaintenancePage />);

      expect(screen.getByText(/10–30 Minuten/)).toBeInTheDocument();
    });

    it('renders the intent test input', () => {
      renderWithProviders(<MaintenancePage />);

      expect(screen.getByPlaceholderText(/Schalte das Licht/)).toBeInTheDocument();
    });
  });

  describe('FTS Reindex', () => {
    it('calls reindex endpoint and shows result', async () => {
      server.use(
        http.post(`${BASE_URL}/api/knowledge/reindex-fts`, () => {
          return HttpResponse.json({ updated_count: 42, fts_config: 'german' });
        })
      );

      renderWithProviders(<MaintenancePage />);

      const buttons = screen.getAllByRole('button', { name: /FTS neu indexieren/i });
      fireEvent.click(buttons[0]);

      await waitFor(() => {
        expect(screen.getByText(/FTS-Index erfolgreich aktualisiert/)).toBeInTheDocument();
      });

      expect(screen.getByText(/42/)).toBeInTheDocument();
      expect(screen.getByText(/german/)).toBeInTheDocument();
    });

    it('shows error when reindex fails', async () => {
      server.use(
        http.post(`${BASE_URL}/api/knowledge/reindex-fts`, () => {
          return HttpResponse.json({ detail: 'DB error' }, { status: 500 });
        })
      );

      renderWithProviders(<MaintenancePage />);

      const buttons = screen.getAllByRole('button', { name: /FTS neu indexieren/i });
      fireEvent.click(buttons[0]);

      await waitFor(() => {
        expect(screen.getByText(/DB error/)).toBeInTheDocument();
      });
    });
  });

  describe('HA Keywords', () => {
    it('calls refresh-keywords endpoint and shows result', async () => {
      server.use(
        http.post(`${BASE_URL}/admin/refresh-keywords`, () => {
          return HttpResponse.json({ keywords_count: 15, sample: ['Wohnzimmer Licht', 'Küche'] });
        })
      );

      renderWithProviders(<MaintenancePage />);

      const buttons = screen.getAllByRole('button', { name: /HA-Keywords aktualisieren/i });
      fireEvent.click(buttons[0]);

      await waitFor(() => {
        expect(screen.getByText(/Keywords erfolgreich aktualisiert/)).toBeInTheDocument();
      });

      expect(screen.getByText(/15/)).toBeInTheDocument();
      expect(screen.getByText(/Wohnzimmer Licht, Küche/)).toBeInTheDocument();
    });
  });

  describe('Re-embed', () => {
    it('asks for confirmation before re-embedding', () => {
      const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);

      renderWithProviders(<MaintenancePage />);

      const buttons = screen.getAllByRole('button', { name: /Alle neu einbetten/i });
      fireEvent.click(buttons[0]);

      expect(confirmSpy).toHaveBeenCalled();
      confirmSpy.mockRestore();
    });

    it('calls reembed endpoint when confirmed', async () => {
      vi.spyOn(window, 'confirm').mockReturnValue(true);

      server.use(
        http.post(`${BASE_URL}/admin/reembed`, () => {
          return HttpResponse.json({
            model: 'qwen3-embedding:4b',
            counts: { rag_chunks: 100, memories: 20 }
          });
        })
      );

      renderWithProviders(<MaintenancePage />);

      const buttons = screen.getAllByRole('button', { name: /Alle neu einbetten/i });
      fireEvent.click(buttons[0]);

      await waitFor(() => {
        expect(screen.getByText(/Vektoren erfolgreich neu berechnet/)).toBeInTheDocument();
      });

      expect(screen.getByText(/qwen3-embedding:4b/)).toBeInTheDocument();

      window.confirm.mockRestore();
    });
  });

  describe('Intent Test', () => {
    it('calls debug/intent endpoint and shows JSON result', async () => {
      server.use(
        http.post(`${BASE_URL}/debug/intent`, ({ request }) => {
          const url = new URL(request.url);
          const message = url.searchParams.get('message');
          return HttpResponse.json({
            intent: 'mcp.ha.light_turn_on',
            confidence: 0.95,
            message
          });
        })
      );

      renderWithProviders(<MaintenancePage />);

      const input = screen.getByPlaceholderText(/Schalte das Licht/);
      fireEvent.change(input, { target: { value: 'Licht an' } });

      const testButton = screen.getByRole('button', { name: /Intent testen/i });
      fireEvent.click(testButton);

      await waitFor(() => {
        expect(screen.getByText(/Erkannter Intent/)).toBeInTheDocument();
      });

      expect(screen.getByText(/mcp.ha.light_turn_on/)).toBeInTheDocument();
    });

    it('disables test button when input is empty', () => {
      renderWithProviders(<MaintenancePage />);

      const testButton = screen.getByRole('button', { name: /Intent testen/i });
      expect(testButton).toBeDisabled();
    });

    it('supports Enter key to submit', async () => {
      server.use(
        http.post(`${BASE_URL}/debug/intent`, () => {
          return HttpResponse.json({ intent: 'general.conversation' });
        })
      );

      renderWithProviders(<MaintenancePage />);

      const input = screen.getByPlaceholderText(/Schalte das Licht/);
      fireEvent.change(input, { target: { value: 'Hallo' } });
      fireEvent.keyDown(input, { key: 'Enter' });

      await waitFor(() => {
        expect(screen.getByText(/general.conversation/)).toBeInTheDocument();
      });
    });
  });
});
