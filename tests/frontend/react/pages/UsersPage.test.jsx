import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server.js';
import { BASE_URL, mockUsers, mockRoles } from '../mocks/handlers.js';
import UsersPage from '../../../../src/frontend/src/pages/UsersPage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

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
        <button onClick={onClose}>Close Modal</button>
        {children}
      </div>
    );
  }
}));

// Default mock values for admin user
const adminAuthMock = {
  getAccessToken: () => 'mock-token',
  user: { id: 1, username: 'admin', role: 'Admin' }
};

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

      expect(screen.getByText('User Management')).toBeInTheDocument();
      expect(screen.getByText('Manage user accounts and permissions')).toBeInTheDocument();
    });

    it('shows loading state initially', () => {
      renderWithProviders(<UsersPage />);

      expect(screen.getByText('Loading users...')).toBeInTheDocument();
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

      // Admin user should have Admin role badge
      expect(screen.getAllByText('Admin').length).toBeGreaterThan(0);
      // Regular users should have User role badge
      expect(screen.getAllByText('User').length).toBeGreaterThan(0);
    });

    it('shows inactive badge for inactive users', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('inactive_user')).toBeInTheDocument();
      });

      expect(screen.getByText('Inactive')).toBeInTheDocument();
    });

    it('shows "You" badge for current user', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      expect(screen.getByText('You')).toBeInTheDocument();
    });

    it('shows voice linked indicator for users with speaker', async () => {
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('user1')).toBeInTheDocument();
      });

      expect(screen.getByText('Voice linked')).toBeInTheDocument();
    });
  });

  describe('Create User Modal', () => {
    it('opens create modal when clicking create button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      const createButton = screen.getByRole('button', { name: /create user/i });
      await user.click(createButton);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');
      expect(within(modal).getByRole('heading', { name: 'Create User' })).toBeInTheDocument();
    });

    it('shows required form fields in create modal', async () => {
      const user = userEvent.setup();
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      await user.click(screen.getByRole('button', { name: /create user/i }));

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');
      expect(within(modal).getByPlaceholderText(/enter username/i)).toBeInTheDocument();
      expect(within(modal).getByPlaceholderText(/user@example.com/i)).toBeInTheDocument();
      expect(within(modal).getByPlaceholderText(/enter password/i)).toBeInTheDocument();
      expect(within(modal).getByText(/select a role/i)).toBeInTheDocument();
    });
  });

  describe('Edit User Modal', () => {
    it('opens edit modal when clicking edit button', async () => {
      const user = userEvent.setup();
      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      // Find and click edit button for a user
      const editButtons = screen.getAllByTitle('Edit user');
      await user.click(editButtons[0]);

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      expect(screen.getByText('Edit User')).toBeInTheDocument();
    });
  });

  describe('API Integration', () => {
    it('creates user with form data', async () => {
      const user = userEvent.setup();
      let createdUser = null;

      server.use(
        http.post(`${BASE_URL}/api/users`, async ({ request }) => {
          createdUser = await request.json();
          return HttpResponse.json({
            id: 4,
            ...createdUser,
            role_name: 'User',
            is_active: true
          }, { status: 201 });
        })
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      // Open create modal
      await user.click(screen.getByRole('button', { name: /create user/i }));

      await waitFor(() => {
        expect(screen.getByTestId('modal')).toBeInTheDocument();
      });

      const modal = screen.getByTestId('modal');

      // Fill in form
      await user.type(within(modal).getByPlaceholderText(/enter username/i), 'newuser');
      await user.type(within(modal).getByPlaceholderText(/user@example.com/i), 'new@example.com');
      await user.type(within(modal).getByPlaceholderText(/enter password/i), 'password123');

      // Submit form
      const submitButton = within(modal).getByRole('button', { name: /create user/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(createdUser).not.toBeNull();
      });

      expect(createdUser.username).toBe('newuser');
      expect(createdUser.email).toBe('new@example.com');
    });

    it('deletes user when clicking delete button', async () => {
      const user = userEvent.setup();
      let deleteUserId = null;

      server.use(
        http.delete(`${BASE_URL}/api/users/:id`, ({ params }) => {
          deleteUserId = params.id;
          return HttpResponse.json({ message: 'User deleted' });
        })
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('user1')).toBeInTheDocument();
      });

      // Find delete button for user1 (not the current user)
      const deleteButtons = screen.getAllByTitle('Delete user');
      // Click on a delete button (not for current user)
      await user.click(deleteButtons[1]);

      await waitFor(() => {
        expect(deleteUserId).not.toBeNull();
      });
    });
  });

  describe('Error Handling', () => {
    it('shows error message when loading fails', async () => {
      server.use(
        http.get(`${BASE_URL}/api/users`, () => {
          return HttpResponse.json(
            { detail: 'Failed to load users' },
            { status: 500 }
          );
        })
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText(/failed to load users/i)).toBeInTheDocument();
      });
    });

    it('shows empty state when no users exist', async () => {
      server.use(
        http.get(`${BASE_URL}/api/users`, () => {
          return HttpResponse.json({
            users: [],
            total: 0,
            page: 1,
            page_size: 20
          });
        })
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('No users found')).toBeInTheDocument();
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
            page_size: 20
          });
        })
      );

      renderWithProviders(<UsersPage />);

      await waitFor(() => {
        expect(screen.getByText('admin')).toBeInTheDocument();
      });

      // Click refresh button
      const refreshButton = screen.getByRole('button', { name: /refresh/i });
      await user.click(refreshButton);

      await waitFor(() => {
        expect(fetchCount).toBeGreaterThanOrEqual(2);
      });
    });
  });
});
