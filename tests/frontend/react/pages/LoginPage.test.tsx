import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import LoginPage from '../../../../src/frontend/src/pages/LoginPage';
import { renderWithProviders } from '../test-utils';
import { useAuth, type AuthContextValue } from '../../../../src/frontend/src/context/AuthContext';
import { unauthenticatedAuthMock } from '../test-auth-mock';
import type { LoginResponse } from '../../../../src/frontend/src/types/api';
import { server } from '../mocks/server';

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

// Mock react-router navigate
const mockNavigate = vi.fn<(to: string, opts?: { replace?: boolean }) => void>();
vi.mock('react-router', async () => {
  const actual = await vi.importActual<typeof import('react-router')>('react-router');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useLocation: () => ({ state: null, pathname: '/login' }),
  };
});

describe('LoginPage', () => {
  beforeEach(() => {
    server.resetHandlers();
    vi.mocked(useAuth).mockReturnValue(unauthenticatedAuthMock);
    mockNavigate.mockClear();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the login form', () => {
      renderWithProviders(<LoginPage />);

      expect(screen.getByText('Renfield')).toBeInTheDocument();
      expect(screen.getByText('Melden Sie sich an')).toBeInTheDocument();
      expect(screen.getByLabelText(/benutzername/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/passwort/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /anmelden/i })).toBeInTheDocument();
    });

    it('shows loading state while checking auth', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        loading: true,
      });

      renderWithProviders(<LoginPage />);

      expect(screen.queryByLabelText(/benutzername/i)).not.toBeInTheDocument();
    });

    it('shows registration link when allowed', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        allowRegistration: true,
      });

      renderWithProviders(<LoginPage />);

      expect(screen.getByText(/noch kein konto/i)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /erstellen sie eines/i })).toBeInTheDocument();
    });

    it('hides registration link when not allowed', () => {
      renderWithProviders(<LoginPage />);

      expect(screen.queryByText(/noch kein konto/i)).not.toBeInTheDocument();
    });
  });

  describe('Form Interaction', () => {
    it('allows typing in username and password fields', async () => {
      const user = userEvent.setup();
      renderWithProviders(<LoginPage />);

      const usernameInput = screen.getByLabelText(/benutzername/i);
      const passwordInput = screen.getByLabelText(/passwort/i);

      await user.type(usernameInput, 'testuser');
      await user.type(passwordInput, 'testpass');

      expect(usernameInput).toHaveValue('testuser');
      expect(passwordInput).toHaveValue('testpass');
    });

    it('toggles password visibility', async () => {
      const user = userEvent.setup();
      renderWithProviders(<LoginPage />);

      const passwordInput = screen.getByLabelText(/passwort/i);
      expect(passwordInput).toHaveAttribute('type', 'password');

      const toggleButton = screen.getByRole('button', { name: '' });
      await user.click(toggleButton);

      expect(passwordInput).toHaveAttribute('type', 'text');
    });
  });

  describe('Form Submission', () => {
    it('shows error when submitting empty form', async () => {
      const user = userEvent.setup();
      renderWithProviders(<LoginPage />);

      const submitButton = screen.getByRole('button', { name: /anmelden/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(screen.getByText(/bitte benutzername und passwort eingeben/i)).toBeInTheDocument();
      });
    });

    it('calls login function on valid submission', async () => {
      const mockLogin = vi.fn<AuthContextValue['login']>().mockResolvedValue({
        access_token: 'mock',
        refresh_token: 'mock',
        token_type: 'bearer',
        user: { id: 1, username: 'admin', is_active: true, role_id: 1, created_at: '', updated_at: '' },
      } satisfies LoginResponse);
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        login: mockLogin,
      });

      const user = userEvent.setup();
      renderWithProviders(<LoginPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'admin');
      await user.type(screen.getByLabelText(/passwort/i), 'password123');
      await user.click(screen.getByRole('button', { name: /anmelden/i }));

      await waitFor(() => {
        expect(mockLogin).toHaveBeenCalledWith('admin', 'password123');
      });
    });

    it('shows error message on login failure', async () => {
      const mockLogin = vi.fn<AuthContextValue['login']>().mockRejectedValue({
        response: { data: { detail: 'Invalid credentials' } },
      });
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        login: mockLogin,
      });

      const user = userEvent.setup();
      renderWithProviders(<LoginPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'admin');
      await user.type(screen.getByLabelText(/passwort/i), 'wrongpassword');
      await user.click(screen.getByRole('button', { name: /anmelden/i }));

      await waitFor(() => {
        expect(screen.getByText('Invalid credentials')).toBeInTheDocument();
      });
    });

    it('navigates to home after successful login', async () => {
      const mockLogin = vi.fn<AuthContextValue['login']>().mockResolvedValue({
        access_token: 'mock',
        refresh_token: 'mock',
        token_type: 'bearer',
        user: { id: 1, username: 'admin', is_active: true, role_id: 1, created_at: '', updated_at: '' },
      });
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        login: mockLogin,
      });

      const user = userEvent.setup();
      renderWithProviders(<LoginPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'admin');
      await user.type(screen.getByLabelText(/passwort/i), 'password123');
      await user.click(screen.getByRole('button', { name: /anmelden/i }));

      await waitFor(() => {
        expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
      });
    });
  });

  describe('Redirects', () => {
    it('redirects to home if already authenticated', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        isAuthenticated: true,
      });

      renderWithProviders(<LoginPage />);

      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
    });

    it('redirects to home if auth is disabled', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...unauthenticatedAuthMock,
        authEnabled: false,
      });

      renderWithProviders(<LoginPage />);

      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
    });
  });
});
