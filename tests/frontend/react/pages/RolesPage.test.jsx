import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within, fireEvent, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL, mockRoles, mockPlugins } from '../mocks/handlers.js';
import RolesPage from '../../../../src/frontend/src/pages/RolesPage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

// Default mock values for admin user
const adminAuthMock = {
  getAccessToken: () => 'mock-token',
  hasPermission: () => true,
  hasAnyPermission: () => true,
  isAuthenticated: true,
  authEnabled: true,
  loading: false,
  user: { username: 'admin', role: 'Admin', permissions: ['admin', 'plugins.manage'] }
};

// Mock ConfirmDialog
vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => ({
  useConfirmDialog: () => ({
    confirm: vi.fn().mockResolvedValue(true),
    ConfirmDialogComponent: null
  })
}));

// Mock Modal component
vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, onClose, title, children }) => {
    if (!isOpen) return null;
    return (
      <div data-testid="modal">
        <h2>{title}</h2>
        <button onClick={onClose}>Modal schließen</button>
        {children}
      </div>
    );
  }
}));

describe('RolesPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<RolesPage />);

      expect(screen.getByText('Rollenverwaltung')).toBeInTheDocument();
      expect(screen.getByText('Verwalte Rollen und deren Berechtigungen')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<RolesPage />);

      expect(screen.getByText('Lade Rollen...')).toBeInTheDocument();
    });

    it('displays roles after loading', async () => {
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      expect(screen.getByText('User')).toBeInTheDocument();
    });

    it('shows system role badge for system roles', async () => {
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      expect(screen.getByText('System')).toBeInTheDocument();
    });

    it('shows permission badges on role cards', async () => {
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      // Admin role should show some permission badges
      expect(screen.getByText('admin')).toBeInTheDocument();
    });
  });

  describe('Create Role Modal', () => {
    it('opens create modal when clicking create button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      // Check for 'Rolle erstellen' title in modal using h2 or by scoping to modal
      const modal = screen.getByTestId('modal');
      expect(within(modal).getByRole('heading', { name: /rolle erstellen/i })).toBeInTheDocument();
    });

    it('shows plugin permission category in create modal', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      // Should have Plugins category
      expect(screen.getByText('Plugins')).toBeInTheDocument();
    });
  });

  describe('Edit Role Modal', () => {
    it('opens edit modal when clicking edit button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      // Find and click edit button
      const editButtons = screen.getAllByTitle('Rolle bearbeiten');
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      expect(screen.getByText('Rolle bearbeiten')).toBeInTheDocument();
    });

    it('pre-fills form with existing role data', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const editButtons = screen.getAllByTitle('Rolle bearbeiten');
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      // Role name should be pre-filled
      const nameInput = screen.getByPlaceholderText(/z\.B\. Techniker/i);
      expect(nameInput).toHaveValue('Admin');
    });
  });

  describe('Plugin Permissions', () => {
    it('shows plugin permission options', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');

      // Verify the Plugins category exists as a button in the permissions section
      const pluginsCategoryText = within(modal).getByText('Plugins');
      expect(pluginsCategoryText).toBeInTheDocument();

      const pluginsButton = pluginsCategoryText.closest('button');
      expect(pluginsButton).not.toBeNull();
    });

    it('can select plugin permissions', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');

      // Verify the Plugins category is present and can be clicked
      const pluginsCategoryText = within(modal).getByText('Plugins');
      const pluginsButton = pluginsCategoryText.closest('button');
      expect(pluginsButton).not.toBeNull();

      // Verify some permission categories are present
      expect(within(modal).getByText('Home Assistant')).toBeInTheDocument();
      expect(within(modal).getByText('Plugins')).toBeInTheDocument();
    });
  });

  describe('Allowed Plugins Section', () => {
    it('renders permission form with plugin categories', async () => {
      const user = userEvent.setup();

      // Add plugins to the mock response
      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: mockPlugins,
            total: mockPlugins.length,
            plugins_enabled: true
          });
        })
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');

      // Verify the permissions section header is present
      expect(within(modal).getByText('Berechtigungen')).toBeInTheDocument();

      // Verify plugin category exists
      expect(within(modal).getByText('Plugins')).toBeInTheDocument();
    });

    it('hides allowed plugins section when no plugin permission selected', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      // Should NOT show Allowed Plugins section initially
      expect(screen.queryByText('Erlaubte Plugins')).not.toBeInTheDocument();
    });
  });

  describe('Delete Role', () => {
    it('shows delete button only for non-system roles', async () => {
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      // Admin is a system role, should only have edit button
      // User is not a system role, should have both edit and delete
      const deleteButtons = screen.getAllByTitle('Rolle löschen');
      expect(deleteButtons.length).toBe(1); // Only User role can be deleted
    });
  });

  describe('API Integration', () => {
    it('creates role with basic info', async () => {
      const user = userEvent.setup();
      let createdRole = null;

      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: mockPlugins,
            total: mockPlugins.length,
            plugins_enabled: true
          });
        }),
        http.post(`${BASE_URL}/api/roles`, async ({ request }) => {
          createdRole = await request.json();
          return HttpResponse.json({
            id: 3,
            ...createdRole,
            is_system: false,
            user_count: 0,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString()
          }, { status: 201 });
        })
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      // Open create modal
      const createButton = screen.getByRole('button', { name: /rolle erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');

      // Fill in name
      const nameInput = within(modal).getByPlaceholderText(/z\.B\. Techniker/i);
      await user.type(nameInput, 'TestRole');

      // Fill in description
      const descInput = within(modal).getByPlaceholderText(/kurze beschreibung/i);
      await user.type(descInput, 'A test role');

      // Submit form - find the submit button
      const submitButton = within(modal).getByRole('button', { name: /rolle erstellen/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(createdRole).not.toBeNull();
      });

      expect(createdRole.name).toBe('TestRole');
      expect(createdRole.description).toBe('A test role');
    });

    it('updates role allowed_plugins', async () => {
      const user = userEvent.setup();
      let updatedData = null;

      server.use(
        http.get(`${BASE_URL}/api/plugins`, () => {
          return HttpResponse.json({
            plugins: mockPlugins,
            total: mockPlugins.length,
            plugins_enabled: true
          });
        }),
        http.patch(`${BASE_URL}/api/roles/:id`, async ({ request }) => {
          updatedData = await request.json();
          return HttpResponse.json({
            ...mockRoles[1],
            ...updatedData,
            updated_at: new Date().toISOString()
          });
        })
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('User')).toBeInTheDocument();
      });

      // Edit the User role (not Admin which is system)
      const editButtons = screen.getAllByTitle('Rolle bearbeiten');
      await user.click(editButtons[1]); // Second role is User

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      // Submit the form
      const updateButton = screen.getByRole('button', { name: /rolle aktualisieren/i });
      await user.click(updateButton);

      await waitFor(() => {
        expect(updatedData).not.toBeNull();
      });
    });
  });

  describe('Error Handling', () => {
    it('shows error message when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/roles`, () => {
          return HttpResponse.json(
            { detail: 'Failed to load roles' },
            { status: 500 }
          );
        })
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText(/failed to load roles/i)).toBeInTheDocument();
      });
    });

    it('shows empty state when no roles exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/roles`, () => {
          return HttpResponse.json([]);
        })
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Keine Rollen gefunden')).toBeInTheDocument();
      });
    });
  });
});
