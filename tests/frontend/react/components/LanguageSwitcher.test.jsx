import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import LanguageSwitcher from '../../../../src/frontend/src/components/LanguageSwitcher';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../../../src/frontend/src/i18n';

// Mock axios
vi.mock('../../../../src/frontend/src/utils/axios', () => ({
  default: {
    get: vi.fn().mockResolvedValue({ data: { language: 'de' } }),
    put: vi.fn().mockResolvedValue({ data: { language: 'de' } })
  }
}));

// Mock AuthContext
vi.mock('../../../../src/frontend/src/context/AuthContext', () => ({
  useAuth: vi.fn(() => ({
    isAuthenticated: false,
    authEnabled: false
  }))
}));

// Helper to render with i18n
function renderWithI18n(ui, { language = 'de' } = {}) {
  i18n.changeLanguage(language);

  return render(
    <I18nextProvider i18n={i18n}>
      {ui}
    </I18nextProvider>
  );
}

describe('LanguageSwitcher', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
    i18n.changeLanguage('de');
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('Rendering', () => {
    it('renders the toggle button', () => {
      renderWithI18n(<LanguageSwitcher />);

      const button = screen.getByRole('button');
      expect(button).toBeInTheDocument();
    });

    it('shows current language code', () => {
      renderWithI18n(<LanguageSwitcher />);

      expect(screen.getByText('DE')).toBeInTheDocument();
    });

    it('shows flag in compact mode', () => {
      renderWithI18n(<LanguageSwitcher compact />);

      expect(screen.getByText('🇩🇪')).toBeInTheDocument();
    });

    it('shows globe icon in non-compact mode', () => {
      renderWithI18n(<LanguageSwitcher />);

      const button = screen.getByRole('button');
      expect(button.querySelector('svg')).toBeInTheDocument();
    });
  });

  describe('Dropdown Menu', () => {
    it('dropdown is closed by default', () => {
      renderWithI18n(<LanguageSwitcher />);

      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });

    it('opens dropdown when clicking button', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));

      expect(screen.getByRole('listbox')).toBeInTheDocument();
    });

    it('closes dropdown when clicking button again', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      const button = screen.getByRole('button');

      // Open
      await user.click(button);
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      // Close
      await user.click(button);
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });

    it('shows both language options', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));

      expect(screen.getByText('Deutsch')).toBeInTheDocument();
      expect(screen.getByText('English')).toBeInTheDocument();
    });

    it('shows flags for both languages', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));

      expect(screen.getByText('🇩🇪')).toBeInTheDocument();
      expect(screen.getByText('🇬🇧')).toBeInTheDocument();
    });

    it('highlights current language', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />, { language: 'de' });

      await user.click(screen.getByRole('button'));

      // German option should have checkmark
      const germanOption = screen.getByRole('option', { name: /deutsch/i });
      expect(germanOption.querySelector('svg')).toBeTruthy();
    });
  });

  describe('Language Selection', () => {
    it('changes to English when English is selected', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />, { language: 'de' });

      await user.click(screen.getByRole('button'));
      await user.click(screen.getByRole('option', { name: /english/i }));

      // Menu should close
      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

      // Language should change (i18n.language)
      await waitFor(() => {
        expect(i18n.language).toBe('en');
      });
    });

    it('changes to German when German is selected', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />, { language: 'en' });

      await user.click(screen.getByRole('button'));
      await user.click(screen.getByRole('option', { name: /deutsch/i }));

      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

      await waitFor(() => {
        expect(i18n.language).toBe('de');
      });
    });

    it('stores language preference in localStorage', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />, { language: 'de' });

      await user.click(screen.getByRole('button'));
      await user.click(screen.getByRole('option', { name: /english/i }));

      // i18next-browser-languagedetector stores in localStorage
      await waitFor(() => {
        expect(window.localStorage.getItem('renfield_language')).toBe('en');
      });
    });
  });

  describe('Accessibility', () => {
    it('has proper aria-label on button', () => {
      renderWithI18n(<LanguageSwitcher />);

      expect(screen.getByRole('button')).toHaveAttribute('aria-label');
    });

    it('has aria-expanded attribute', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      const button = screen.getByRole('button');

      expect(button).toHaveAttribute('aria-expanded', 'false');

      await user.click(button);

      expect(button).toHaveAttribute('aria-expanded', 'true');
    });

    it('has aria-haspopup attribute', () => {
      renderWithI18n(<LanguageSwitcher />);

      expect(screen.getByRole('button')).toHaveAttribute('aria-haspopup', 'listbox');
    });

    it('listbox has proper role', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));

      expect(screen.getByRole('listbox')).toBeInTheDocument();
    });

    it('options have proper role', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));

      const options = screen.getAllByRole('option');
      expect(options).toHaveLength(2);
    });

    it('current language has aria-selected', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />, { language: 'de' });

      await user.click(screen.getByRole('button'));

      const germanOption = screen.getByRole('option', { name: /deutsch/i });
      expect(germanOption).toHaveAttribute('aria-selected', 'true');
    });
  });

  describe('Keyboard Navigation', () => {
    it('closes dropdown on Escape key', async () => {
      const user = userEvent.setup();
      renderWithI18n(<LanguageSwitcher />);

      // Open dropdown
      await user.click(screen.getByRole('button'));
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      // Press Escape
      await user.keyboard('{Escape}');

      expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    });
  });

  describe('Click Outside', () => {
    it('closes dropdown when clicking outside', async () => {
      const user = userEvent.setup();
      renderWithI18n(
        <div>
          <LanguageSwitcher />
          <button data-testid="outside">Outside</button>
        </div>
      );

      // Open dropdown
      await user.click(screen.getByRole('button', { name: /sprache/i }));
      expect(screen.getByRole('listbox')).toBeInTheDocument();

      // Click outside
      await user.click(screen.getByTestId('outside'));

      await waitFor(() => {
        expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
      });
    });
  });

  describe('Visual States', () => {
    it('applies correct styling classes', () => {
      renderWithI18n(<LanguageSwitcher />);

      const button = screen.getByRole('button');
      expect(button).toHaveClass('text-gray-600');
      expect(button).toHaveClass('dark:text-gray-300');
    });

    it('compact mode uses smaller flag display', () => {
      renderWithI18n(<LanguageSwitcher compact />);

      // Should show flag without language code text
      expect(screen.getByText('🇩🇪')).toBeInTheDocument();
      expect(screen.queryByText('DE')).not.toBeInTheDocument();
    });
  });

  describe('Backend Sync', () => {
    it('does not sync to backend when not authenticated', async () => {
      const apiClient = await import('../../../../src/frontend/src/utils/axios');
      const user = userEvent.setup();

      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));
      await user.click(screen.getByRole('option', { name: /english/i }));

      // Should not call backend API
      expect(apiClient.default.put).not.toHaveBeenCalled();
    });

    it('syncs to backend when authenticated', async () => {
      const { useAuth } = await import('../../../../src/frontend/src/context/AuthContext');
      useAuth.mockReturnValue({
        isAuthenticated: true,
        authEnabled: true
      });

      const apiClient = await import('../../../../src/frontend/src/utils/axios');
      const user = userEvent.setup();

      renderWithI18n(<LanguageSwitcher />);

      await user.click(screen.getByRole('button'));
      await user.click(screen.getByRole('option', { name: /english/i }));

      await waitFor(() => {
        expect(apiClient.default.put).toHaveBeenCalledWith(
          '/api/preferences/language',
          { language: 'en' }
        );
      });
    });
  });
});
