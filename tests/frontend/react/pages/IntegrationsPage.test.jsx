import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL, mockMcpStatus, mockMcpTools, mockPlugins } from '../mocks/handlers.js';
import IntegrationsPage from '../../../../src/frontend/src/pages/IntegrationsPage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock useAuth hook
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

// Default mock values for admin user
const adminAuthMock = {
  getAccessToken: () => 'mock-token',
  hasPermission: (perm) => perm === 'plugins.manage' || perm === 'admin',
  hasAnyPermission: () => true,
  isAuthenticated: true,
  authEnabled: true,
  loading: false,
  user: { username: 'admin', role: 'Admin', permissions: ['admin', 'plugins.manage'] }
};

// Mock values for user without manage permission
const userAuthMock = {
  getAccessToken: () => 'mock-token',
  hasPermission: () => false,
  hasAnyPermission: () => false,
  isAuthenticated: true,
  authEnabled: true,
  loading: false,
  user: { username: 'user', role: 'User', permissions: ['plugins.use'] }
};

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

      // Wait for loading to complete
      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('Integrationen')).toBeInTheDocument();
      expect(screen.getByText('Verwalte MCP-Server und Plugin-Verbindungen')).toBeInTheDocument();
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

      // MCP Server appears in both stats and section header
      const mcpServerTexts = screen.getAllByText('MCP Server');
      expect(mcpServerTexts.length).toBeGreaterThan(0);
    });

    it('displays YAML Plugins section', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // YAML Plugins appears in both stats and section header
      const yamlPluginsTexts = screen.getAllByText('YAML Plugins');
      expect(yamlPluginsTexts.length).toBeGreaterThan(0);
    });

    it('shows overall statistics', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // MCP stats - using getAllByText for items that appear multiple times
      expect(screen.getAllByText('MCP Server').length).toBeGreaterThan(0);
      expect(screen.getByText('Verbunden')).toBeInTheDocument();
      expect(screen.getByText('MCP Tools')).toBeInTheDocument();
      // Plugin stats
      expect(screen.getAllByText('YAML Plugins').length).toBeGreaterThan(0);
      expect(screen.getByText('Plugins aktiv')).toBeInTheDocument();
      expect(screen.getByText('Intents')).toBeInTheDocument();
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
        // Modal title shows the tool's original_name
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check for tool name in the modal (the MCP prefixed name)
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

  describe('YAML Plugins Section', () => {
    it('shows plugin list with details', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('calendar')).toBeInTheDocument();
    });

    it('shows enabled/disabled badges for plugins', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // weather plugin is enabled, calendar is disabled
      const enabledBadges = screen.getAllByText('Aktiviert');
      expect(enabledBadges.length).toBeGreaterThan(0);

      const disabledBadges = screen.getAllByText('Deaktiviert');
      expect(disabledBadges.length).toBeGreaterThan(0);
    });

    it('shows empty state when no plugins', async () => {
      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: [],
            total: 0,
            plugins_enabled: true
          });
        })
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('Keine YAML-Plugins gefunden')).toBeInTheDocument();
    });
  });

  describe('Plugin Details Modal', () => {
    it('opens plugin detail modal when clicking info button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // Find and click the info button for a plugin
      const infoButtons = screen.getAllByTitle('Details anzeigen');
      await user.click(infoButtons[0]);

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // Check modal content
      expect(screen.getByText('Version')).toBeInTheDocument();
    });

    it('shows plugin intents in detail modal', async () => {
      const user = userEvent.setup();
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // weather plugin has 1 intent
      const infoButtons = screen.getAllByTitle('Details anzeigen');
      // First plugin is weather (sorted alphabetically by mock data order: weather, calendar)
      await user.click(infoButtons[0]);

      await waitFor(() => {
        expect(screen.getByRole('dialog')).toBeInTheDocument();
      });

      // The modal shows "Intents (1)" for the weather plugin
      expect(screen.getByText(/Intents \(1\)/)).toBeInTheDocument();
    });
  });

  describe('Plugin Toggle', () => {
    it('shows toggle button for users with plugins.manage permission', async () => {
      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const toggleButtons = screen.getAllByTitle(/Plugin (aktivieren|deaktivieren)/i);
      expect(toggleButtons.length).toBeGreaterThan(0);
    });

    it('calls toggle API when clicking toggle button', async () => {
      const user = userEvent.setup();
      let toggleCalled = false;

      server.use(
        http.post(`${BASE_URL}/api/plugins/:name/toggle`, () => {
          toggleCalled = true;
          return HttpResponse.json({
            name: 'weather',
            enabled: false,
            message: 'Plugin disabled',
            requires_restart: true
          });
        })
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const toggleButtons = screen.getAllByTitle(/Plugin (aktivieren|deaktivieren)/i);
      await user.click(toggleButtons[0]);

      await waitFor(() => {
        expect(toggleCalled).toBe(true);
      });
    });

    it('hides toggle buttons for users without plugins.manage permission', async () => {
      vi.mocked(useAuth).mockReturnValue(userAuthMock);

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      const infoButtons = screen.getAllByTitle('Details anzeigen');
      expect(infoButtons.length).toBeGreaterThan(0);

      const toggleButtons = screen.queryAllByTitle(/Plugin (aktivieren|deaktivieren)/i);
      expect(toggleButtons.length).toBe(0);
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
            servers_reconnected: 1
          });
        })
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
      // When MCP API fails, component catches error and shows empty state
      server.use(
        http.get(`${BASE_URL}/api/mcp/status`, () => {
          return HttpResponse.json(
            { detail: 'MCP service unavailable' },
            { status: 500 }
          );
        }),
        http.get(`${BASE_URL}/api/mcp/tools`, () => {
          return HttpResponse.json(
            { detail: 'MCP service unavailable' },
            { status: 500 }
          );
        })
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // Component shows "no servers" state when API fails (catches error gracefully)
      expect(screen.getByText('Keine MCP-Server konfiguriert')).toBeInTheDocument();
    });

    it('shows error when refresh fails', async () => {
      const user = userEvent.setup();

      server.use(
        http.post(`${BASE_URL}/api/mcp/refresh`, () => {
          return HttpResponse.json(
            { detail: 'Refresh failed' },
            { status: 500 }
          );
        })
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
            servers: []
          });
        }),
        http.get(`${BASE_URL}/api/mcp/tools`, () => {
          return HttpResponse.json({
            tools: [],
            total: 0
          });
        }),
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: [],
            total: 0,
            plugins_enabled: false
          });
        })
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
            servers: []
          });
        }),
        http.get(`${BASE_URL}/api/mcp/tools`, () => {
          return HttpResponse.json({
            tools: [],
            total: 0
          });
        })
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      expect(screen.getByText('Keine MCP-Server konfiguriert')).toBeInTheDocument();
    });
  });

  describe('Plugins System Disabled', () => {
    it('shows warning when plugins system is disabled', async () => {
      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: mockPlugins,
            total: mockPlugins.length,
            plugins_enabled: false
          });
        })
      );

      renderWithProviders(<IntegrationsPage />);

      await waitFor(() => {
        expect(screen.queryByText('Lade Integrationen...')).not.toBeInTheDocument();
      });

      // Component shows warning when plugins_enabled is false
      expect(screen.getByText(/PLUGINS_ENABLED=true/)).toBeInTheDocument();
    });
  });
});
