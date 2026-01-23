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
      expect(screen.getByText('Create your account')).toBeInTheDocument();
      expect(screen.getByLabelText(/username/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/email/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/^password/i)).toBeInTheDocument();
      expect(screen.getByLabelText(/confirm password/i)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /create account/i })).toBeInTheDocument();
    });

    it('shows loading state while checking auth', () => {
      vi.mocked(useAuth).mockReturnValue({
        ...defaultMock,
        loading: true
      });

      renderWithProviders(<RegisterPage />);

      // Should show loading spinner, not the form
      expect(screen.queryByLabelText(/username/i)).not.toBeInTheDocument();
    });

    it('shows login link', () => {
      renderWithProviders(<RegisterPage />);

      expect(screen.getByText(/already have an account/i)).toBeInTheDocument();
      expect(screen.getByRole('link', { name: /sign in/i })).toBeInTheDocument();
    });
  });

  describe('Form Interaction', () => {
    it('allows typing in form fields', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      const usernameInput = screen.getByLabelText(/username/i);
      const emailInput = screen.getByLabelText(/email/i);
      const passwordInput = screen.getByLabelText(/^password/i);
      const confirmPasswordInput = screen.getByLabelText(/confirm password/i);

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

      const passwordInput = screen.getByLabelText(/^password/i);
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

      const submitButton = screen.getByRole('button', { name: /create account/i });
      await user.click(submitButton);

      await waitFor(() => {
        expect(screen.getByText(/please fill in all required fields/i)).toBeInTheDocument();
      });
    });

    it('shows error for short username', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/username/i), 'ab');
      await user.type(screen.getByLabelText(/^password/i), 'password123');
      await user.type(screen.getByLabelText(/confirm password/i), 'password123');
      await user.click(screen.getByRole('button', { name: /create account/i }));

      await waitFor(() => {
        expect(screen.getByText(/username must be at least 3 characters/i)).toBeInTheDocument();
      });
    });

    it('shows error for short password', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/username/i), 'newuser');
      await user.type(screen.getByLabelText(/^password/i), 'short');
      await user.type(screen.getByLabelText(/confirm password/i), 'short');
      await user.click(screen.getByRole('button', { name: /create account/i }));

      await waitFor(() => {
        expect(screen.getByText(/password must be at least 8 characters/i)).toBeInTheDocument();
      });
    });

    it('shows error when passwords do not match', async () => {
      const user = userEvent.setup();
      renderWithProviders(<RegisterPage />);

      await user.type(screen.getByLabelText(/username/i), 'newuser');
      await user.type(screen.getByLabelText(/^password/i), 'password123');
      await user.type(screen.getByLabelText(/confirm password/i), 'password456');
      await user.click(screen.getByRole('button', { name: /create account/i }));

      await waitFor(() => {
        expect(screen.getByText(/passwords do not match/i)).toBeInTheDocument();
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

      await user.type(screen.getByLabelText(/username/i), 'newuser');
      await user.type(screen.getByLabelText(/email/i), 'new@example.com');
      await user.type(screen.getByLabelText(/^password/i), 'password123');
      await user.type(screen.getByLabelText(/confirm password/i), 'password123');
      await user.click(screen.getByRole('button', { name: /create account/i }));

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

      await user.type(screen.getByLabelText(/username/i), 'newuser');
      await user.type(screen.getByLabelText(/^password/i), 'password123');
      await user.type(screen.getByLabelText(/confirm password/i), 'password123');
      await user.click(screen.getByRole('button', { name: /create account/i }));

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

      await user.type(screen.getByLabelText(/username/i), 'newuser');
      await user.type(screen.getByLabelText(/^password/i), 'password123');
      await user.type(screen.getByLabelText(/confirm password/i), 'password123');
      await user.click(screen.getByRole('button', { name: /create account/i }));

      await waitFor(() => {
        expect(screen.getByText(/account created successfully/i)).toBeInTheDocument();
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

      await user.type(screen.getByLabelText(/username/i), 'existing_user');
      await user.type(screen.getByLabelText(/^password/i), 'password123');
      await user.type(screen.getByLabelText(/confirm password/i), 'password123');
      await user.click(screen.getByRole('button', { name: /create account/i }));

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
