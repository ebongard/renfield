import { createElement, Fragment } from 'react';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';
import { BASE_URL, mockUsers } from '../mocks/handlers';
import UsersPage from '../../../../src/frontend/src/pages/UsersPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { adminAuthMock } from '../test-auth-mock';
import type { ModalProps } from '../../../../src/frontend/src/components/Modal';
import type { UseConfirmDialogResult } from '../../../../src/frontend/src/components/ConfirmDialog';
import type { CreateUserInput } from '../../../../src/frontend/src/api/resources/users';

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

describe('UsersPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(adminAuthMock);
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the page title', async () => {
      renderWithProviders(<UsersPage />);

      expect(screen.getByText('Benutzerverwaltung')).toBeInTheDocument();
      expect(screen.getByText('Verwalte Benutzerkonten und Berechtigungen')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<UsersPage />);

      expect(screen.getByText('Lade Benutzer...')).toBeInTheDocument();
    });

    it('displays users after loading', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      expect(screen.getByText('user1')).toBeInTheDocument();
      expect(screen.getByText('inactive_user')).toBeInTheDocument();
    });

    it('shows role badges for users', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      expect(screen.getAllByText('Admin').length).toBeGreaterThan(0);
      expect(screen.getAllByText('User').length).toBeGreaterThan(0);
    });

    it('shows inactive badge for inactive users', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('inactive_user')).toBeInTheDocument();
      });

      expect(screen.getByText('Inaktiv')).toBeInTheDocument();
    });

    it('shows "You" badge for current user', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      expect(screen.getByText('Du')).toBeInTheDocument();
    });

    it('shows voice linked indicator for users with speaker', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('user1')).toBeInTheDocument();
      });

      expect(screen.getByText('Stimme verknüpft')).toBeInTheDocument();
    });
  });

  describe('Create User Modal', () => {
    it('opens create modal when clicking create button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /benutzer erstellen/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');
      expect(within(modal).getByRole('heading', { name: 'Benutzer erstellen' })).toBeInTheDocument();
    });

    it('shows required form fields in create modal', async () => {
      const user = userEvent.setup();
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: /benutzer erstellen/i }));

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');
      expect(within(modal).getByPlaceholderText(/benutzernamen eingeben/i)).toBeInTheDocument();
      expect(within(modal).getByPlaceholderText(/deine@email.de/i)).toBeInTheDocument();
      expect(within(modal).getByPlaceholderText(/passwort eingeben/i)).toBeInTheDocument();
      expect(within(modal).getByText(/rolle auswählen/i)).toBeInTheDocument();
    });
  });

  describe('Edit User Modal', () => {
    it('opens edit modal when clicking edit button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      const editButtons = screen.getAllByTitle('Benutzer bearbeiten');
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      expect(screen.getByText('Benutzer bearbeiten')).toBeInTheDocument();
    });
  });

  describe('API Integration', () => {
    it('creates user with form data', async () => {
      const user = userEvent.setup();
      let createdUser: Partial<CreateUserInput> | null = null;

      server.use(
        http.post(`${BASE_URL}/api/users`, async ({ request }) => {
          createdUser = (await request.json()) as Partial<CreateUserInput>;
          return HttpResponse.json(
            {
              id: 4,
              ...createdUser,
              role_name: 'User',
              is_active: true,
            },
            { status: 201 },
          );
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: /benutzer erstellen/i }));

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');

      await user.type(within(modal).getByPlaceholderText(/benutzernamen eingeben/i), 'newuser');
      await user.type(within(modal).getByPlaceholderText(/deine@email.de/i), 'new@example.com');
      await user.type(within(modal).getByPlaceholderText(/passwort eingeben/i), 'password123');

      const submitButton = within(modal).getByRole('button', { name: /benutzer erstellen/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(createdUser).not.toBeNull();
      });

      expect(createdUser!.username).toBe('newuser');
      expect(createdUser!.email).toBe('new@example.com');
    });

    it('deletes user when clicking delete button', async () => {
      const user = userEvent.setup();
      let deleteUserId: string | readonly string[] | null = null;

      server.use(
        http.delete(`${BASE_URL}/api/users/:id`, ({ params }) => {
          deleteUserId = params.id ?? null;
          return HttpResponse.json({ message: 'User deleted' });
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('user1')).toBeInTheDocument();
      });

      const deleteButtons = screen.getAllByTitle('Benutzer löschen');
      await user.click(deleteButtons[1]);

      await waitFor(() => {
        expect(deleteUserId).not.toBeNull();
      });
    });

    it('unlinks speaker via DELETE /api/users/:id/link-speaker', async () => {
      // Regression: previous code called DELETE /unlink-speaker (typo); the
      // backend route is DELETE /link-speaker (REST verb-on-noun).
      // The 404 was swallowed silently and the unlink button looked broken.
      const user = userEvent.setup();
      let unlinkUrl: string | null = null;

      server.use(
        http.delete(`${BASE_URL}/api/users/:id/link-speaker`, ({ request, params }) => {
          unlinkUrl = new URL(request.url).pathname;
          return HttpResponse.json({ id: parseInt(params.id as string, 10), speaker_id: null });
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('user1')).toBeInTheDocument();
      });

      // user1 (id=2) has speaker_id=1, so its row shows the unlink button.
      const unlinkButtons = screen.getAllByTitle('Sprecher trennen');
      await user.click(unlinkButtons[0]);

      await waitFor(() => {
        expect(unlinkUrl).toBe('/api/users/2/link-speaker');
      });
    });

    it('links speaker via POST /api/users/:id/link-speaker', async () => {
      const user = userEvent.setup();
      let linkPostBody: { speaker_id?: number } | null = null;
      let linkUrl: string | null = null;

      server.use(
        http.post<{ id: string }>(`${BASE_URL}/api/users/:id/link-speaker`, async ({ request, params }) => {
          linkUrl = new URL(request.url).pathname;
          linkPostBody = (await request.json()) as { speaker_id?: number };
          return HttpResponse.json({ id: parseInt(params.id, 10), speaker_id: linkPostBody.speaker_id });
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      // admin (id=1) has speaker_id=null — its row shows the link button.
      // Speaker 2 in the mock is the only unlinked one (Speaker 1 is on user1).
      const linkButtons = screen.getAllByTitle('Sprecher verknüpfen');
      await user.click(linkButtons[0]);

      // Modal opens with available speakers; click Speaker 2.
      await waitFor(() => {
        expect(screen.getByText('Speaker 2')).toBeInTheDocument();
      });
      await user.click(screen.getByText('Speaker 2'));

      await waitFor(() => {
        expect(linkUrl).toBe('/api/users/1/link-speaker');
        expect(linkPostBody).toEqual({ speaker_id: 2 });
      });
    });
  });

  describe('Error Handling', () => {
    it('shows error message when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/users`, () => {
          return HttpResponse.json(
            { detail: 'Benutzer konnten nicht geladen werden' },
            { status: 500 },
          );
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText(/benutzer konnten nicht geladen werden/i)).toBeInTheDocument();
      });
    });

    it('shows empty state when no users exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/users`, () => {
          return HttpResponse.json({
            users: [],
            total: 0,
            page: 1,
            page_size: 20,
          });
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('Keine Benutzer gefunden')).toBeInTheDocument();
      });
    });
  });

  describe('Refresh', () => {
    it('refreshes user list when clicking refresh button', async () => {
      const user = userEvent.setup();
      let fetchCount = 0;

      server.use(
        http.get(`${BASE_URL}/api/users`, () => {
          fetchCount++;
          return HttpResponse.json({
            users: mockUsers,
            total: mockUsers.length,
            page: 1,
            page_size: 20,
          });
        }),
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      const refreshButton = screen.getByRole('button', { name: /aktualisieren/i });
      await user.click(refreshButton);

      await waitFor(() => {
        expect(fetchCount).toBeGreaterThanOrEqual(2);
      });
    });
  });
});
