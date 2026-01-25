import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import RegisterPage from '../../../../src/frontend/src/pages/RegisterPage';
import { renderWithProviders } from '../test-utils.jsx';
import { useAuth } from '../../../../src/frontend/src/context/AuthContext';

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn()
}));

// Mock react-router-dom navigate
const mockNavigate = vi.fn();
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useNavigate: () => mockNavigate,
    useLocation: () => ({ state: null, pathname: '/register' })
  };
});

// Default mock values for unauthenticated user with registration enabled
const defaultMock = {
  register: vi.fn(),
  isAuthenticated: false,
  authEnabled: true,
  allowRegistration: true,
  loading: false
};

describe('RegisterPage', () => {
  beforeEach(() => {
    vi.mocked(useAuth).mockReturnValue(defaultMock);
    mockNavigate.mockClear();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  describe('Rendering', () => {
    it('renders the registration form', () => {
      renderWithProviders(<RegisterPage />);

      expect(screen.getByText('Renfield')).toBeInTheDocument();
      expect(screen.getByText('Erstelle dein Konto')).toBeInTheDocument();
      expect(screen.getByLabelText(/benutzername/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/e-mail/i)).toBeInTheDocument();
      // Use placeholder text since labels include asterisks
      expect(screen.getByPlaceholderText(/passwort erstellen/i)).toBeInTheDocument();
      expect(screen.getByPlaceholderText(/passwort bestätigen/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /konto erstellen/i })).toBeInTheDocument();
    });

    it('shows loading state while checking auth', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        loading: true
      });

      renderWithProviders(<RegisterPage />);

      // Should show loading spinner, not the form
      expect(screen.queryByLabelText(/benutzername/i)).not.toBeInTheDocument();
    });

    it('shows login link', () => {
      renderWithProviders(<RegisterPage />);

      expect(screen.getByText(/bereits ein konto/i)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /anmelden/i })).toBeInTheDocument();
    });
  });

  describe('Form Interaction', () => {
    it('allows typing in form fields', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      const usernameInput = screen.getByLabelText(/benutzername/i);
      const emailInput = screen.getByLabelText(/e-mail/i);
      const passwordInput = screen.getByPlaceholderText(/passwort erstellen/i);
      const confirmPasswordInput = screen.getByPlaceholderText(/passwort bestätigen/i);

      await user.type(usernameInput, 'newuser');
      await user.type(emailInput, 'new@example.com');
      await user.type(passwordInput, 'password123');
      await user.type(confirmPasswordInput, 'password123');

      expect(usernameInput).toHaveValue('newuser');
      expect(emailInput).toHaveValue('new@example.com');
      expect(passwordInput).toHaveValue('password123');
      expect(confirmPasswordInput).toHaveValue('password123');
    });

    it('toggles password visibility', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      const passwordInput = screen.getByPlaceholderText(/passwort erstellen/i);
      expect(passwordInput).toHaveAttribute('type', 'password');

      // Find and click the toggle button (the button after password input)
      const toggleButtons = screen.getAllByRole('button');
      const toggleButton = toggleButtons.find(btn => btn.querySelector('svg'));
      await user.click(toggleButton);

      expect(passwordInput).toHaveAttribute('type', 'text');
    });
  });

  describe('Form Validation', () => {
    it('shows error when submitting empty form', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      const submitButton = screen.getByRole('button', { name: /konto erstellen/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(screen.getByText(/bitte alle pflichtfelder ausfüllen/i)).toBeInTheDocument();
      });
    });

    it('shows error for short username', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'ab');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'password123');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'password123');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(screen.getByText(/benutzername muss mindestens 3 zeichen haben/i)).toBeInTheDocument();
      });
    });

    it('shows error for short password', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'newuser');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'short');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'short');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(screen.getByText(/passwort muss mindestens 8 zeichen haben/i)).toBeInTheDocument();
      });
    });

    it('shows error when passwords do not match', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'newuser');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'password123');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'password456');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(screen.getByText(/passwörter stimmen nicht überein/i)).toBeInTheDocument();
      });
    });
  });

  describe('Form Submission', () => {
    it('calls register function on valid submission', async () => {
      const mockRegister = vi.fn().mockResolvedValue(undefined);
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        register: mockRegister
      });

      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'newuser');
      await user.type(screen.getByLabelText(/e-mail/i), 'new@example.com');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'password123');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'password123');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(mockRegister).toHaveBeenCalledWith('newuser', 'password123', 'new@example.com');
      });
    });

    it('calls register with null email when not provided', async () => {
      const mockRegister = vi.fn().mockResolvedValue(undefined);
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        register: mockRegister
      });

      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'newuser');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'password123');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'password123');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(mockRegister).toHaveBeenCalledWith('newuser', 'password123', null);
      });
    });

    it('shows success message after successful registration', async () => {
      const mockRegister = vi.fn().mockResolvedValue(undefined);
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        register: mockRegister
      });

      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'newuser');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'password123');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'password123');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(screen.getByText(/konto erfolgreich erstellt/i)).toBeInTheDocument();
      });
    });

    it('shows error message on registration failure', async () => {
      const mockRegister = vi.fn().mockRejectedValue({
        response: { data: { detail: 'Username already exists' } }
      });
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        register: mockRegister
      });

      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/benutzername/i), 'existing_user');
      await user.type(screen.getByPlaceholderText(/passwort erstellen/i), 'password123');
      await user.type(screen.getByPlaceholderText(/passwort bestätigen/i), 'password123');
      await user.click(screen.getByRole('button', { name: /konto erstellen/i }));

      await waitFor(() => {
        expect(screen.getByText('Username already exists')).toBeInTheDocument();
      });
    });
  });

  describe('Redirects', () => {
    it('redirects to home if already authenticated', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        isAuthenticated: true
      });

      renderWithProviders(<RegisterPage />);

      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
    });

    it('redirects to home if auth is disabled', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        authEnabled: false
      });

      renderWithProviders(<RegisterPage />);

      expect(mockNavigate).toHaveBeenCalledWith('/', { replace: true });
    });

    it('redirects to login if registration is not allowed', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        allowRegistration: false
      });

      renderWithProviders(<RegisterPage />);

      expect(mockNavigate).toHaveBeenCalledWith('/login', { replace: true });
    });
  });
});
