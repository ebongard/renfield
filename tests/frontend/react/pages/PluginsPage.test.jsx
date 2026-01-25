import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL, mockPlugins } from '../mocks/handlers.js';
import PluginsPage from '../../../../src/frontend/src/pages/PluginsPage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock useAuth hook
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

// Default mock values for admin user
const adminAuthMock = {
  getAccessToken: () => 'mock-token',
  hasPermission: (perm) => perm === 'plugins.manage',
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

describe('PluginsPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<PluginsPage />);

      expect(screen.getByText('Plugins')).toBeInTheDocument();
      expect(screen.getByText('Verwalte Plugins und Erweiterungen')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<PluginsPage />);

      expect(screen.getByText('Lade Plugins...')).toBeInTheDocument();
    });

    it('displays plugins after loading', async () => {
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      expect(screen.getByText('calendar')).toBeInTheDocument();
    });

    it('shows plugin statistics', async () => {
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('Plugins gesamt')).toBeInTheDocument();
      });

      // Use getAllByText since 'Aktiviert' and 'Deaktiviert' appear in both stats and badges
      expect(screen.getAllByText('Aktiviert').length).toBeGreaterThan(0);
      expect(screen.getAllByText('Deaktiviert').length).toBeGreaterThan(0);
      expect(screen.getByText('Intents gesamt')).toBeInTheDocument();
    });

    it('shows enabled/disabled badges correctly', async () => {
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      // Weather plugin should be enabled
      const enabledBadges = screen.getAllByText('Aktiviert');
      expect(enabledBadges.length).toBeGreaterThan(0);

      // Calendar plugin should be disabled
      const disabledBadges = screen.getAllByText('Deaktiviert');
      expect(disabledBadges.length).toBeGreaterThan(0);
    });
  });

  describe('Plugin Details Modal', () => {
    it('opens detail modal when clicking info button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      // Find and click the info button (first one should be for weather)
      const infoButtons = screen.getAllByTitle('Details anzeigen');
      await user.click(infoButtons[0]);

      // Modal should show plugin details
      await waitFor(() => {
        expect(screen.getByText('Version')).toBeInTheDocument();
      });
    });

    it('shows plugin intents in detail modal', async () => {
      const user = userEvent.setup();
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      const infoButtons = screen.getAllByTitle('Details anzeigen');
      await user.click(infoButtons[0]);

      await waitFor(() => {
        expect(screen.getByText('Intents (1)')).toBeInTheDocument();
      });
    });

    it('closes modal when clicking close button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      const infoButtons = screen.getAllByTitle('Details anzeigen');
      await user.click(infoButtons[0]);

      await waitFor(() => {
        expect(screen.getByText('Version')).toBeInTheDocument();
      });

      // Click close button (first one in the modal)
      const closeButtons = screen.getAllByRole('button', { name: /schließen/i });
      await user.click(closeButtons[0]);

      // Modal should be closed - Version text should not be visible in main content
      await waitFor(() => {
        expect(screen.queryByText('Aktivierungsvariable')).not.toBeInTheDocument();
      });
    });
  });

  describe('Plugin Toggle', () => {
    it('shows toggle button for users with plugins.manage permission', async () => {
      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      // Should have toggle buttons (power icons)
      const toggleButtons = screen.getAllByTitle(/plugin (aktivieren|deaktivieren)/i);
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

      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      // Click the toggle button for weather (which is enabled, so it says "Deaktivieren")
      const toggleButtons = screen.getAllByTitle(/plugin (aktivieren|deaktivieren)/i);
      await user.click(toggleButtons[0]);

      await waitFor(() => {
        expect(toggleCalled).toBe(true);
      });
    });
  });

  describe('Error Handling', () => {
    it('shows error message when API fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json(
            { detail: 'Plugins konnten nicht geladen werden' },
            { status: 500 }
          );
        })
      );

      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText(/plugins konnten nicht geladen werden/i)).toBeInTheDocument();
      });
    });

    it('shows empty state when no plugins available', async () => {
      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: [],
            total: 0,
            plugins_enabled: true
          });
        })
      );

      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('Keine Plugins gefunden')).toBeInTheDocument();
      });
    });

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

      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText(/plugin-system ist deaktiviert/i)).toBeInTheDocument();
      });
    });
  });

  describe('Refresh', () => {
    it('refreshes plugin list when clicking refresh button', async () => {
      const user = userEvent.setup();
      let fetchCount = 0;

      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          fetchCount++;
          return HttpResponse.json({
            plugins: mockPlugins,
            total: mockPlugins.length,
            plugins_enabled: true
          });
        })
      );

      renderWithProviders(<PluginsPage />);

      await waitFor(() => {
        expect(screen.getByText('weather')).toBeInTheDocument();
      });

      // Click refresh button
      const refreshButton = screen.getByRole('button', { name: /aktualisieren/i });
      await user.click(refreshButton);

      await waitFor(() => {
        expect(fetchCount).toBe(2);
      });
    });
  });
});

describe('PluginsPage without manage permission', () => {
  beforeEach(() => {
    server.resetHandlers();
    // Override the mock to not have manage permission
    vi.mocked(useAuth).mockReturnValue(userAuthMock);
  });

  it('hides toggle buttons for users without plugins.manage permission', async () => {
    renderWithProviders(<PluginsPage />);

    await waitFor(() => {
      expect(screen.getByText('weather')).toBeInTheDocument();
    });

    // Info buttons should exist
    const infoButtons = screen.getAllByTitle('Details anzeigen');
    expect(infoButtons.length).toBeGreaterThan(0);

    // Toggle buttons should not exist (no manage permission)
    const toggleButtons = screen.queryAllByTitle(/plugin (aktivieren|deaktivieren)/i);
    expect(toggleButtons.length).toBe(0);
  });
});
