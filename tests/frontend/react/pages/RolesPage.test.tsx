import { createElement, Fragment } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL, mockRoles } from '../mocks/handlers';
import RolesPage from '../../../../src/frontend/src/pages/RolesPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';
import type { ModalProps } from '../../../../src/frontend/src/components/Modal';
import type { UseConfirmDialogResult } from '../../../../src/frontend/src/components/ConfirmDialog';
import type { Role } from '../../../../src/frontend/src/api/resources/roles';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', async () => {
  const actual = await vi.importActual<typeof import('../../../../src/frontend/src/context/AuthContext')>(
    '../../../../src/frontend/src/context/AuthContext',
  );
  return {
    ...actual,
    useAuth: vi.fn<() => AuthContextValue>(),
  };
});

// Mock ConfirmDialog
vi.mock('../../../../src/frontend/src/components/ConfirmDialog', () => {
  const result: UseConfirmDialogResult = {
    confirm: () => Promise.resolve(true),
    ConfirmDialogComponent: createElement(Fragment),
  };
  return {
    useConfirmDialog: (): UseConfirmDialogResult => result,
  };
});

// Mock Modal component
vi.mock('../../../../src/frontend/src/components/Modal', () => ({
  default: ({ isOpen, onClose, title, children }: ModalProps) => {
    if (!isOpen) return null;
    return (
      <div data-testid="modal">
        <h2>{title}</h2>
        <button onClick={onClose}>Modal schließen</button>
        {children}
      </div>
    );
  },
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

      const modal = screen.getByTestId('modal');
      expect(within(modal).getByRole('heading', { name: /rolle erstellen/i })).toBeInTheDocument();
    });
  });

  describe('Edit Role Modal', () => {
    it('opens edit modal when clicking edit button', async () => {
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

      const nameInput = screen.getByPlaceholderText(/z\.B\. Techniker/i);
      expect(nameInput).toHaveValue('Admin');
    });
  });

  describe('Delete Role', () => {
    it('shows delete button only for non-system roles', async () => {
      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Admin')).toBeInTheDocument();
      });

      const deleteButtons = screen.getAllByTitle('Rolle löschen');
      expect(deleteButtons.length).toBe(1);
    });
  });

  describe('API Integration', () => {
    it('creates role with basic info', async () => {
      const user = userEvent.setup();
      let createdRole: Partial<Role> | null = null;

      server.use(
        http.post(`${BASE_URL}/api/roles`, async ({ request }) => {
          createdRole = (await request.json()) as Partial<Role>;
          return HttpResponse.json(
            {
              id: 3,
              ...createdRole,
              is_system: false,
              user_count: 0,
              created_at: new Date().toISOString(),
              updated_at: new Date().toISOString(),
            },
            { status: 201 },
          );
        }),
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

      const nameInput = within(modal).getByPlaceholderText(/z\.B\. Techniker/i);
      await user.type(nameInput, 'TestRole');

      const descInput = within(modal).getByPlaceholderText(/kurze beschreibung/i);
      await user.type(descInput, 'A test role');

      const submitButton = within(modal).getByRole('button', { name: /rolle erstellen/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(createdRole).not.toBeNull();
      });

      expect(createdRole!.name).toBe('TestRole');
      expect(createdRole!.description).toBe('A test role');
    });

    it('updates role via PATCH', async () => {
      const user = userEvent.setup();
      let updatedData: Partial<Role> | null = null;

      server.use(
        http.patch(`${BASE_URL}/api/roles/:id`, async ({ request }) => {
          updatedData = (await request.json()) as Partial<Role>;
          return HttpResponse.json({
            ...mockRoles[1],
            ...updatedData,
            updated_at: new Date().toISOString(),
          });
        }),
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('User')).toBeInTheDocument();
      });

      const editButtons = screen.getAllByTitle('Rolle bearbeiten');
      await user.click(editButtons[1]);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

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
            { status: 500 },
          );
        }),
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
        }),
      );

      renderWithProviders(<RolesPage />);

      await waitFor(() => {
        expect(screen.getByText('Keine Rollen gefunden')).toBeInTheDocument();
      });
    });
  });
});
