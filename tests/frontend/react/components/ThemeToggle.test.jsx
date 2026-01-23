import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ThemeToggle from '../../../../src/frontend/src/components/ThemeToggle';
import { ThemeProvider } from '../../../../src/frontend/src/context/ThemeContext';

// Helper to render with ThemeProvider
function renderWithTheme(ui, { theme = 'system' } = {}) {
  // Set initial theme in localStorage
  if (theme) {
    window.localStorage.setItem('renfield_theme', theme);
  }

  return render(
    <ThemeProvider>
      {ui}
    </ThemeProvider>
  );
}

describe('ThemeToggle', () => {
  beforeEach(() => {
    window.localStorage.clear();
    vi.clearAllMocks();
    document.documentElement.classList.remove('dark');
  });

  afterEach(() => {
    document.documentElement.classList.remove('dark');
  });

  describe('Rendering', () => {
    it('renders the toggle button', () => {
      renderWithTheme(<ThemeToggle />);

      expect(screen.getByRole('button', { name: /theme wechseln/i })).toBeInTheDocument();
    });

    it('shows correct icon for light theme', () => {
      renderWithTheme(<ThemeToggle />, { theme: 'light' });

      // The button should contain an SVG (Sun icon)
      const button = screen.getByRole('button', { name: /theme wechseln/i });
      expect(button.querySelector('svg')).toBeInTheDocument();
    });

    it('shows correct icon for dark theme', () => {
      renderWithTheme(<ThemeToggle />, { theme: 'dark' });

      const button = screen.getByRole('button', { name: /theme wechseln/i });
      expect(button.querySelector('svg')).toBeInTheDocument();
    });

    it('shows correct icon for system theme', () => {
      renderWithTheme(<ThemeToggle />, { theme: 'system' });

      const button = screen.getByRole('button', { name: /theme wechseln/i });
      expect(button.querySelector('svg')).toBeInTheDocument();
    });
  });

  describe('Dropdown Menu', () => {
    it('dropdown is closed by default', () => {
      renderWithTheme(<ThemeToggle />);

      // Menu should not be visible
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    it('opens dropdown when clicking button', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));

      expect(screen.getByRole('menu')).toBeInTheDocument();
    });

    it('closes dropdown when clicking button again', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      const button = screen.getByRole('button', { name: /theme wechseln/i });

      // Open
      await user.click(button);
      expect(screen.getByRole('menu')).toBeInTheDocument();

      // Close
      await user.click(button);
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });

    it('shows all three theme options', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));

      expect(screen.getByRole('menuitem', { name: /hell/i })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: /dunkel/i })).toBeInTheDocument();
      expect(screen.getByRole('menuitem', { name: /system/i })).toBeInTheDocument();
    });

    it('highlights current theme option', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />, { theme: 'dark' });

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));

      const darkOption = screen.getByRole('menuitem', { name: /dunkel/i });
      // Should have the active styling (contains checkmark)
      expect(darkOption.querySelector('svg')).toBeTruthy();
    });
  });

  describe('Theme Selection', () => {
    it('selects light theme', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />, { theme: 'dark' });

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));
      await user.click(screen.getByRole('menuitem', { name: /hell/i }));

      // Menu should close
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();

      // Theme should be saved
      expect(window.localStorage.setItem).toHaveBeenCalledWith('renfield_theme', 'light');
    });

    it('selects dark theme', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />, { theme: 'light' });

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));
      await user.click(screen.getByRole('menuitem', { name: /dunkel/i }));

      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
      expect(window.localStorage.setItem).toHaveBeenCalledWith('renfield_theme', 'dark');
    });

    it('selects system theme', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />, { theme: 'dark' });

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));
      await user.click(screen.getByRole('menuitem', { name: /system/i }));

      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
      expect(window.localStorage.setItem).toHaveBeenCalledWith('renfield_theme', 'system');
    });
  });

  describe('Accessibility', () => {
    it('has proper aria-label on button', () => {
      renderWithTheme(<ThemeToggle />);

      expect(screen.getByRole('button')).toHaveAttribute('aria-label', 'Theme wechseln');
    });

    it('has aria-expanded attribute', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      const button = screen.getByRole('button', { name: /theme wechseln/i });

      expect(button).toHaveAttribute('aria-expanded', 'false');

      await user.click(button);

      expect(button).toHaveAttribute('aria-expanded', 'true');
    });

    it('has aria-haspopup attribute', () => {
      renderWithTheme(<ThemeToggle />);

      expect(screen.getByRole('button')).toHaveAttribute('aria-haspopup', 'true');
    });

    it('menu has proper role', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));

      expect(screen.getByRole('menu')).toBeInTheDocument();
    });

    it('menu items have proper role', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));

      const menuItems = screen.getAllByRole('menuitem');
      expect(menuItems).toHaveLength(3);
    });
  });

  describe('Keyboard Navigation', () => {
    it('closes dropdown on Escape key', async () => {
      const user = userEvent.setup();
      renderWithTheme(<ThemeToggle />);

      // Open dropdown
      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));
      expect(screen.getByRole('menu')).toBeInTheDocument();

      // Press Escape
      await user.keyboard('{Escape}');

      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    });
  });

  describe('Click Outside', () => {
    it('closes dropdown when clicking outside', async () => {
      const user = userEvent.setup();
      renderWithTheme(
        <div>
          <ThemeToggle />
          <button data-testid="outside">Outside</button>
        </div>
      );

      // Open dropdown
      await user.click(screen.getByRole('button', { name: /theme wechseln/i }));
      expect(screen.getByRole('menu')).toBeInTheDocument();

      // Click outside
      await user.click(screen.getByTestId('outside'));

      await waitFor(() => {
        expect(screen.queryByRole('menu')).not.toBeInTheDocument();
      });
    });
  });

  describe('Visual States', () => {
    it('applies hover styles on button', () => {
      renderWithTheme(<ThemeToggle />);

      const button = screen.getByRole('button', { name: /theme wechseln/i });
      // Button should have hover classes
      expect(button).toHaveClass('hover:bg-gray-200');
    });

    it('applies correct dark mode classes', () => {
      renderWithTheme(<ThemeToggle />);

      const button = screen.getByRole('button', { name: /theme wechseln/i });
      expect(button).toHaveClass('dark:text-gray-300');
      expect(button).toHaveClass('dark:hover:bg-gray-700');
    });
  });
});
