import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL } from '../mocks/handlers';
import IntegrationsPage from '../../../../src/frontend/src/pages/IntegrationsPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';

// Mock useAuth hook
vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return {
    ...actual,
    useAuth: vi.fn<() => AuthContextValue>(),
  };
});

describe('IntegrationsPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('Integrationen')).toBeInTheDocument();
      expect(screen.getByText('Verwalte MCP-Server-Verbindungen')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<IntegrationsPage />);

      expect(screen.getByText('Lade Integrationen...')).toBeInTheDocument();
    });

    it('displays MCP servers section after loading', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const mcpServerTexts = screen.getAllByText('MCP Server');
      expect(mcpServerTexts.length).toBeGreaterThan(0);
    });

    it('shows overall statistics', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getAllByText('MCP Server').length).toBeGreaterThan(0);
      expect(screen.getByText('Verbunden')).toBeInTheDocument();
      expect(screen.getByText('MCP Tools')).toBeInTheDocument();
    });

    it('displays MCP server names', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('homeassistant')).toBeInTheDocument();
      expect(screen.getByText('search')).toBeInTheDocument();
    });

    it('shows transport badges for servers', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getAllByText('stdio').length).toBeGreaterThan(0);
      expect(screen.getByText('streamable_http')).toBeInTheDocument();
    });

    it('shows online/offline status badges', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const onlineBadges = screen.getAllByText('Online');
      expect(onlineBadges.length).toBe(2);

      const offlineBadges = screen.getAllByText('Offline');
      expect(offlineBadges.length).toBe(1);
    });
  });

  describe('MCP Server Expansion', () => {
    it('expands server to show tools when clicked', async () => {
      const user = userEvent.setup();
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const haServerName = screen.getByText('homeassistant');
      await user.click(haServerName);

      await waitFor(() => {
        expect(screen.getByText(/Verfügbare Tools/)).toBeInTheDocument();
      });

      expect(screen.getByText('turn_on')).toBeInTheDocument();
      expect(screen.getByText('turn_off')).toBeInTheDocument();
    });

    it('shows last error for disconnected server', async () => {
      const user = userEvent.setup();
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      await user.click(screen.getByText('search'));

      await waitFor(() => {
        expect(screen.getByText('Connection timeout')).toBeInTheDocument();
      });
    });
  });

  describe('MCP Tool Details Modal', () => {
    it('opens tool detail modal when clicking on a tool', async () => {
      const user = userEvent.setup();
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      await user.click(screen.getByText('homeassistant'));

      await waitFor(() => {
        expect(screen.getByText('turn_on')).toBeInTheDocument();
      });

      await user.click(screen.getByText('turn_on'));

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      expect(screen.getByText('homeassistant__turn_on')).toBeInTheDocument();
      expect(screen.getByText('Turn on a Home Assistant entity')).toBeInTheDocument();
    });

    it('closes tool modal when clicking close button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      await user.click(screen.getByText('homeassistant'));

      await waitFor(() => {
        expect(screen.getByText('turn_on')).toBeInTheDocument();
      });

      await user.click(screen.getByText('turn_on'));

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      const closeButtons = screen.getAllByRole('button', { name: /schließen/i });
      await user.click(closeButtons[closeButtons.length - 1]);

      await waitFor(() => {
        expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      });
    });
  });

  describe('Refresh', () => {
    it('refreshes MCP connections when clicking refresh button', async () => {
      const user = userEvent.setup();
      let refreshCalled = false;

      server.use(
        http.post(`${BASE_URL}/api/mcp/refresh`, () => {
          refreshCalled = true;
          return HttpResponse.json({
            message: 'MCP connections refreshed',
            servers_reconnected: 1,
          });
        }),
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const refreshButton = screen.getByRole('button', { name: /aktualisieren/i });
      await user.click(refreshButton);

      await waitFor(() => {
        expect(refreshCalled).toBe(true);
      });

      await waitFor(() => {
        expect(screen.getByText('Verbindungen erfolgreich aktualisiert')).toBeInTheDocument();
      });
    });
  });

  describe('Error Handling', () => {
    it('handles API failure gracefully with empty state', async () => {
      server.use(
        http.get(`${BASE_URL}/api/mcp/status`, () => {
          return HttpResponse.json(
            { detail: 'MCP service unavailable' },
            { status: 500 },
          );
        }),
        http.get(`${BASE_URL}/api/mcp/tools`, () => {
          return HttpResponse.json(
            { detail: 'MCP service unavailable' },
            { status: 500 },
          );
        }),
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('Keine MCP-Server konfiguriert')).toBeInTheDocument();
    });

    it('shows error when refresh fails', async () => {
      const user = userEvent.setup();

      server.use(
        http.post(`${BASE_URL}/api/mcp/refresh`, () => {
          return HttpResponse.json(
            { detail: 'Refresh failed' },
            { status: 500 },
          );
        }),
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const refreshButton = screen.getByRole('button', { name: /aktualisieren/i });
      await user.click(refreshButton);

      await waitFor(() => {
        expect(screen.getByText('Refresh failed')).toBeInTheDocument();
      });
    });
  });

  describe('MCP Disabled State', () => {
    it('shows disabled badge when MCP is disabled', async () => {
      server.use(
        http.get(`${BASE_URL}/api/mcp/status`, () => {
          return HttpResponse.json({
            enabled: false,
            total_tools: 0,
            servers: [],
          });
        }),
        http.get(`${BASE_URL}/api/mcp/tools`, () => {
          return HttpResponse.json({
            tools: [],
            total: 0,
          });
        }),
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const disabledBadges = screen.getAllByText('Deaktiviert');
      expect(disabledBadges.length).toBeGreaterThan(0);
    });

    it('shows empty state when no servers configured', async () => {
      server.use(
        http.get(`${BASE_URL}/api/mcp/status`, () => {
          return HttpResponse.json({
            enabled: true,
            total_tools: 0,
            servers: [],
          });
        }),
        http.get(`${BASE_URL}/api/mcp/tools`, () => {
          return HttpResponse.json({
            tools: [],
            total: 0,
          });
        }),
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('Keine MCP-Server konfiguriert')).toBeInTheDocument();
    });
  });
});
